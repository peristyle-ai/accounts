from functools import wraps
from flask import request, session, jsonify

from app.models import db
from app.models.activity_log import ActivityLog


# How the caller authenticated. The distinction matters because one of
# these credentials is AMBIENT and the other is not:
#
#   AUTH_API_KEY -- the caller had to present a secret deliberately.
#   AUTH_COOKIE  -- the browser attached it automatically. A third-party
#                   site can cause the browser to send it.
#
# (2026-10-25 security sweep S7: the API accepted the session cookie for
# state-changing calls, so a cookie alone could perform a write. The
# ambient credential is now read-only.)
AUTH_API_KEY = 'api_key'
AUTH_COOKIE = 'cookie'


def _resolve_auth():
    """Resolve the calling user AND how they authenticated.

    Returns a (user, auth_method) tuple. `user` is the User row, or
    None if neither auth method succeeded. `auth_method` is
    AUTH_API_KEY, AUTH_COOKIE, or None.

    Use this rather than `_resolve_current_user()` wherever the *kind*
    of credential matters, not just the identity.
    """
    # Lazy import to avoid circular dependency:
    #   user.py → app.shared.pii → app.shared.__init__ → decorators.py → app.models.user
    from app.models.user import User

    # 1. Try X-API-Key header (programmatic API clients)
    api_key = request.headers.get('X-API-Key')
    if api_key:
        # The api_key lives in accounts.user_payment_settings (not in
        # public.users directly). Resolve the row whose key matches, then
        # map back to the User via the relationship.
        # Phase 1b (schema-mode install refactor, 2026-10-14)
        #
        # `find_by_api_key` handles BOTH storage formats: legacy rows
        # that still hold the plaintext token, and rows where the column
        # holds a Fernet ciphertext. The column is documented as
        # ciphertext, so matching on the raw column alone would 401
        # every client the moment the row is encrypted.
        from app.models.user_payment_settings import UserPaymentSettings
        ups = UserPaymentSettings.find_by_api_key(api_key)
        if ups:
            # A deactivated account must not authenticate, even with a valid
            # key. (2026-10-25 security sweep: `is_active` was defined but
            # never consulted anywhere, so retiring an account did nothing.)
            if not getattr(ups.user, 'is_active', True):
                return None, None
            return ups.user, AUTH_API_KEY  # relationship backref
        # An API key was provided but didn't match — don't silently fall
        # through to session auth, since that would let a typo'd key pass
        # via cookie. Reject explicitly.
        return None, None

    # 2. Try session cookie (HMI users logged in via the web UI)
    user_id = session.get('user_id')
    if user_id:
        user = db.session.get(User, user_id)
        if user is not None and not getattr(user, 'is_active', True):
            return None, None
        return user, AUTH_COOKIE
    return None, None


def _resolve_current_user():
    """Resolve the calling user via either X-API-Key or session cookie.

    Returns the User row, or None if neither auth method succeeded.
    """
    user, _auth_method = _resolve_auth()
    return user


def _log_cookie_write_refusal(user):
    """Record a refused cookie-authenticated write in the activity log.

    A refusal is a security event worth keeping: a burst of them is the
    signature of a CSRF attempt. Logging must never be able to break the
    refusal itself, so failures here are swallowed — the request is
    refused either way.
    """
    try:
        import uuid
        log_activity(
            user_id=getattr(user, 'id', None),
            action='cookie_write_refused',
            # activity_logs.table_name and .record_id are NOT NULL, so they
            # must carry real values -- a NULL here would make the INSERT
            # fail and the refusal would go unlogged (silently).
            table_name='api_request',
            record_id=str(uuid.uuid4()),
            new_values={'method': request.method, 'path': request.path},
            ip_address=request.remote_addr,
        )
        db.session.commit()
    except Exception:  # pragma: no cover - defensive only
        try:
            db.session.rollback()
        except Exception:
            pass


def api_key_required(f):
    """Auth decorator that accepts either:
      - X-API-Key header (programmatic API clients — cron jobs, scripts)
      - Flask session cookie (HMI users — the web UI session)

    The route body uses `request.current_user` to identify the caller, so
    this is transparent to the underlying handler.

    Returns 401 with a clear error if neither method authenticated the
    request.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user, auth_method = _resolve_auth()
        if not user:
            # Distinguish between the two failure modes for easier
            # debugging. A bare HTTP client (no cookies, no API key)
            # gets the 'API key required' message; a logged-in HMI user
            # shouldn't ever see this — if they do, the session cookie
            # has expired.
            if request.headers.get('X-API-Key'):
                # A key WAS supplied and it did not resolve. Say so, rather
                # than blaming the session -- a caller with a bad key and a
                # valid cookie deserves to know which credential failed.
                return jsonify({'error': 'Invalid API key'}), 401
            if 'user_id' in session:
                # Logged-in user but session lookup failed (user deleted?)
                return jsonify({'error': 'Session expired or user not found'}), 401
            return jsonify({'error': 'API key required (or HMI session login)'}), 401

        request.current_user = user
        request.auth_method = auth_method

        # A session cookie is an AMBIENT credential: the browser attaches
        # it to every request, including requests another site can cause
        # the browser to make. So a cookie may authenticate READS only.
        # Any state-changing API call must present the X-API-Key header,
        # which a browser will not send on an attacker's behalf.
        #
        # This matches how the application is actually used: every
        # browser → /api/ interaction in the templates is a GET link.
        # Nothing in the HMI or PWA performs a fetch/ajax POST to the
        # API, so no front-end change is required.
        #
        # (2026-10-25 security sweep S7 — see
        # docs/csrf_cookie_auth_proposal.md for the option analysis.)
        from app.shared.roles import SAFE_METHODS, is_read_only
        if auth_method == AUTH_COOKIE and request.method not in SAFE_METHODS:
            _log_cookie_write_refusal(user)
            return jsonify({
                'error': 'State-changing API calls require the X-API-Key header',
                'detail': (
                    'The session cookie authenticates reads only. Send the '
                    'X-API-Key header with the request to change state.'
                ),
                'method': request.method,
            }), 403

        # A read-only role may not change state. Enforced centrally so a
        # route cannot forget it. (2026-10-25 security sweep S6: the
        # `role` column was never consulted anywhere.)
        if request.method not in SAFE_METHODS and is_read_only(user):
            return jsonify({
                'error': 'Read-only account: this role may only read',
                'your_role': getattr(user, 'role', None),
            }), 403

        return f(*args, **kwargs)
    return decorated_function


def log_activity(user_id, action, table_name, record_id, old_values=None, new_values=None, ip_address=None):
    log = ActivityLog(
        user_id=user_id,
        action=action,
        table_name=table_name,
        record_id=record_id,
        old_values=old_values,
        new_values=new_values,
        ip_address=ip_address
    )
    db.session.add(log)
    return log
