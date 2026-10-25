"""Password verification helpers.

The 2026-10-25 security sweep found a **user-enumeration timing oracle**
(S9). The login handlers used:

    if not user or not user.is_active or not user.check_password(password):

Python short-circuits that expression: when the email does not exist the
hash comparison never runs, so an unknown email answers measurably
faster than a known one. scrypt is deliberately expensive (~100 ms), so
the difference is large enough to enumerate accounts with over the
network.

The fix is to always perform a hash comparison, even when there is no
user row to compare against. `_DUMMY_HASH` is a real scrypt hash of a
value nobody knows; verifying against it costs the same as verifying
against a real user's hash and can never succeed.

The hash method and parameters MUST match the ones the live accounts
use (``scrypt:32768:8:1``) or the timing advantage returns.
"""

from werkzeug.security import check_password_hash

# scrypt:32768:8:1 -- same method/parameters the live accounts use.
# The plaintext behind it is meaningless; it exists only to be burned.
DUMMY_PASSWORD_HASH = (
    'scrypt:32768:8:1$UHlAahVZZ7oXCI2K$be484794c86141d10c6175ab900227760b3117624'
    'd3617e57675a8c2db5146f5d1f4d6a9bb0b0bb60ec8549812c734f4cb533300006a38dde531'
    '728eaf18039c'
)


def verify_credentials(user, password) -> bool:
    """Return True iff `password` is the correct password for `user`.

    Always performs a password-hash comparison, so the response time
    does not depend on whether the account exists or is active. Callers
    must NOT short-circuit on `user is None`.
    """
    if user is None:
        # Burn the same CPU we would have burned for a real account.
        check_password_hash(DUMMY_PASSWORD_HASH, password or '')
        return False
    try:
        return user.check_password(password or '')
    except (ValueError, TypeError):
        # A malformed/empty stored hash must not become a fast-fail
        # oracle of its own.
        check_password_hash(DUMMY_PASSWORD_HASH, password or '')
        return False
