"""In-memory rate limiter for auth endpoints.

Single-process Flask app, so in-memory is sufficient. Tracks failed
login attempts per client IP **and per account**, and locks out after
repeated failures.

Limits:
  - 5 failed attempts per IP per 15-minute window -> 15-minute lockout
  - 5 failed attempts per ACCOUNT per 15-minute window -> 15-minute lockout
  - Successful login resets both counters

Why the per-account limit exists (2026-10-25 security sweep):

    The lockout used to be keyed ONLY on the client IP, and the client IP
    was taken from `CF-Connecting-IP` / `X-Forwarded-For` -- both of which
    are client-supplied headers. Rotating a fake IP per request therefore
    reset the bucket every time and the lockout never engaged: unlimited
    password guessing. That was reproduced against the running service
    (10 wrong passwords, 10 HTTP 200s, no lockout).

    Rate limiting on the IP alone cannot be fixed by changing how the IP
    is derived -- whoever controls the connection controls the header.
    Limiting on the ACCOUNT as well makes the attack expensive: the
    attacker must vary the target account too, which is exactly what a
    password-spraying attack needs to do. Either limit trips the lockout.
"""

import os
import time
import threading
from collections import defaultdict
from functools import wraps

from flask import request, jsonify, flash, render_template

_lock = threading.Lock()

# {key: [(timestamp, success), ...]}
_attempts: dict[str, list[tuple[float, bool]]] = defaultdict(list)

# {key: lockout_until_timestamp}
_lockouts: dict[str, float] = {}

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60       # 15 minutes
LOCKOUT_SECONDS = 15 * 60      # 15 minutes


def _trust_proxy_headers() -> bool:
    """True when forwarded-for headers may be trusted as the client IP.

    Off by default. A deployment fronted by a reverse proxy that
    overwrites `X-Forwarded-For` can opt in explicitly.
    """
    return os.environ.get('TRUST_PROXY_HEADERS', '').strip().lower() in ('1', 'true', 'yes')


def _get_client_ip() -> str:
    """Get the client IP for rate-limiting purposes.

    `CF-Connecting-IP` is set (and overwritten) by Cloudflare, so it is
    used when present. `X-Forwarded-For` is client-controlled and was the
    vector for bypassing the lockout -- it is now only honoured when
    TRUST_PROXY_HEADERS is explicitly enabled, otherwise the socket peer
    address is used.
    """
    cf_ip = (request.headers.get('CF-Connecting-IP') or '').strip()
    if cf_ip:
        return cf_ip
    if _trust_proxy_headers():
        xff = (request.headers.get('X-Forwarded-For', '') or '').split(',')[0].strip()
        if xff:
            return xff
    return request.remote_addr or 'unknown'


def _account_key(account) -> str:
    """Lockout key for an account (email). Case-folded so 'A@b' and
    'a@b' share one bucket."""
    if not account:
        return ''
    return str(account).strip().lower()


def _cleanup_old_attempts(key: str, now: float):
    cutoff = now - WINDOW_SECONDS
    _attempts[key] = [(ts, ok) for ts, ok in _attempts[key] if ts > cutoff]


def _keys_for(ip: str, account=None) -> list[str]:
    keys = [f'ip:{ip}']
    acct = _account_key(account)
    if acct:
        keys.append(f'acct:{acct}')
    return keys


def is_locked_out(ip: str, account=None) -> bool:
    with _lock:
        now = time.time()
        for key in _keys_for(ip, account):
            until = _lockouts.get(key, 0)
            if until > now:
                return True
            if key in _lockouts:
                del _lockouts[key]
        return False


def record_attempt(ip: str, success: bool, account=None):
    """Record a login attempt against the IP and the account.

    On success, every counter for that IP/account is cleared.
    """
    now = time.time()
    with _lock:
        keys = _keys_for(ip, account)
        if success:
            for key in keys:
                _attempts.pop(key, None)
                _lockouts.pop(key, None)
            return

        for key in keys:
            _cleanup_old_attempts(key, now)
            _attempts[key].append((now, False))
            failed = sum(1 for _, ok in _attempts[key] if not ok)
            if failed >= MAX_ATTEMPTS:
                _lockouts[key] = now + LOCKOUT_SECONDS
                _attempts.pop(key, None)


def remaining_lockout_seconds(ip: str, account=None) -> int:
    with _lock:
        now = time.time()
        until = 0
        for key in _keys_for(ip, account):
            until = max(until, _lockouts.get(key, 0))
        return max(0, int(until - now))


def rate_limit_api(f):
    """Rate-limit decorator for API auth endpoints (returns JSON 429)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = _get_client_ip()
        account = None
        try:
            data = request.get_json(silent=True) or {}
            if isinstance(data, dict):
                account = data.get('email')
        except Exception:
            account = None
        if is_locked_out(ip, account):
            secs = remaining_lockout_seconds(ip, account)
            return jsonify({
                'error': f'Too many failed attempts. Try again in {secs // 60 + 1} minutes.'
            }), 429
        return f(*args, **kwargs)
    return wrapper


def rate_limit_form(template_name):
    """Rate-limit decorator for form-based auth endpoints.

    Renders `template_name` with a flash message and a 429 status. Used
    by the HMI login and the PWA login -- both are cookie/CSRF protected
    browser forms and both must lock out on repeated failures.
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if request.method == 'POST':
                ip = _get_client_ip()
                account = request.form.get('email')
                if is_locked_out(ip, account):
                    secs = remaining_lockout_seconds(ip, account)
                    mins = secs // 60 + 1
                    flash(
                        f'Too many failed login attempts. Try again in {mins} minute{"s" if mins != 1 else ""}.',
                        'error',
                    )
                    return render_template(template_name), 429
            return f(*args, **kwargs)
        return wrapper
    return decorator


def rate_limit_hmi(template_name):
    """Backwards-compatible alias for the HMI login form."""
    return rate_limit_form(template_name)
