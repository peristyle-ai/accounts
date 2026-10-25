"""Role hierarchy and access-control helpers.

The 2026-10-25 security sweep found that `User.role` was **never
consulted anywhere** (S6): every authenticated account was effectively an
admin. The column existed, was returned by `User.to_dict()`, and was
documented as `admin`/`member`/`viewer` -- but nothing enforced it.

This module gives the column meaning:

  viewer -- read-only. Any state-changing request is refused (403).
  member -- full day-to-day use of the tenant's own records.
  admin -- additionally may perform the operations that are global to
            the installation rather than to one tenant's records
            (business settings, destructive deletes).

`role_required` is a decorator for the second and third cases; the
read-only rule for `viewer` is enforced centrally in `api_key_required`
so it cannot be forgotten on a route.
"""

from functools import wraps

from flask import jsonify, request

# Higher number == more authority.
ROLE_RANK = {
    'viewer': 1,
    'member': 2,
    'admin': 3,
}

# Methods that do not change state. A `viewer` may use these.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})

DEFAULT_ROLE = 'viewer'


def role_rank(user) -> int:
    """Authority rank of a user. Unknown/absent roles rank lowest."""
    if user is None:
        return 0
    role = (getattr(user, 'role', '') or '').strip().lower()
    return ROLE_RANK.get(role, 0)


def is_read_only(user) -> bool:
    """True when the account may only issue safe (read) requests."""
    return role_rank(user) <= ROLE_RANK['viewer']


def role_required(minimum_role: str):
    """Require the caller's role to be at least `minimum_role`.

    Must be applied *below* `@api_key_required` (i.e. listed after it),
    so the decorator has already resolved `request.current_user`::

        @api_bp.route('/x', methods=['DELETE'])
        @api_key_required
        @role_required('admin')
        def delete_x(): ...
    """
    required_rank = ROLE_RANK.get(str(minimum_role).strip().lower(), 99)

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = getattr(request, 'current_user', None)
            if user is None:
                # No authenticated user -> the auth decorator already
                # rejected it; if it did not, refuse anyway.
                return jsonify({'error': 'Authentication required'}), 401
            if role_rank(user) < required_rank:
                return jsonify({
                    'error': f'Requires {minimum_role} role',
                    'your_role': getattr(user, 'role', None),
                }), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator
