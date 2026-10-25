"""Outbound mail (mail-server API) configuration.

Single source of truth for the mail-server settings used by the payslip
sender (app/api/payroll.py) and the invoice sender.

There is deliberately NO default for MAIL_SERVER_API. A silent fallback
to a placeholder host turns a misconfigured deployment into an opaque
DNS failure at send time -- and, because the send endpoint records the
failure in `payslip_deliveries` and still answers HTTP 200, that
failure can be reported to the operator as "sent".

example.com / example.org / example.net / .invalid are reserved by
RFC 2606 for documentation, so they can never be a real mail server. A
configuration still pointing at one is a placeholder that was never
filled in, and is refused here rather than dialled.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional
from urllib.parse import urlsplit

# RFC 2606 reserved documentation domains, plus RFC 6761 .invalid.
# RFC 2606 reserves example.{com,net,org} and the .test / .example TLDs;
# RFC 6761 reserves .invalid. None of them can ever name a real service.
PLACEHOLDER_DOMAIN_SUFFIXES = (
    '.example.com',
    '.example.org',
    '.example.net',
    '.example',
    '.test',
    '.invalid',
)
PLACEHOLDER_DOMAINS = (
    'example.com',
    'example.org',
    'example.net',
    'example',
    'test',
    'invalid',
)


class MailServerNotConfigured(RuntimeError):
    """The mail server is unset, incomplete, or still a placeholder.

    Raised deliberately instead of falling back to a default: a wrong
    mail host is invisible until someone real misses a payslip.
    """


@dataclass(frozen=True)
class MailServerSettings:
    """Resolved mail-server settings.

    ca_bundle: path to a CA bundle, or '' meaning "use the system trust
    store" (correct only for a publicly-trusted certificate).
    """

    api: str
    user: str
    password: str
    ca_bundle: str


def host_of(url: str) -> str:
    """Hostname of a URL, lowercased. '' when it cannot be parsed."""
    try:
        return (urlsplit(url).hostname or '').lower()
    except ValueError:
        return ''


def is_placeholder_host(url: str) -> bool:
    """True when the URL points at a documentation-only domain."""
    host = host_of(url)
    if not host:
        return False
    if host in PLACEHOLDER_DOMAINS:
        return True
    return host.endswith(PLACEHOLDER_DOMAIN_SUFFIXES)


def resolve_mail_server(config: Optional[Mapping[str, object]] = None,
                        environ: Optional[Mapping[str, str]] = None
                        ) -> MailServerSettings:
    """Resolve the mail-server settings, or raise MailServerNotConfigured.

    Precedence: Flask/app config first, then the process environment.
    Every failure mode raises with a message that names the variable to
    set -- a missing mail server must never look like a successful send.
    """
    config = config or {}
    environ = environ if environ is not None else os.environ

    def _get(key: str) -> str:
        value = config.get(key)
        if value is None or value == '':
            value = environ.get(key)
        if value is None:
            return ''
        return str(value).strip()

    api = _get('MAIL_SERVER_API').rstrip('/')
    if not api:
        raise MailServerNotConfigured(
            "MAIL_SERVER_API is not set. It is the base URL of the mail "
            "server API and is required to send payslips and invoices. "
            "Set it in the deployment .env (e.g. https://mail.your-domain:5003)."
        )
    if not api.startswith('https://'):
        raise MailServerNotConfigured(
            "MAIL_SERVER_API must use https:// -- the mail server serves "
            f"HTTPS with a self-signed certificate (got {api!r})."
        )
    if is_placeholder_host(api):
        raise MailServerNotConfigured(
            f"MAIL_SERVER_API still points at the documentation placeholder "
            f"{host_of(api)!r} ({api!r}). Set it to the real base URL of "
            "your mail server."
        )

    user = _get('MAIL_SERVER_USER')
    password = _get('MAIL_SERVER_PASSWORD')
    if not user or not password:
        missing = [name for name, value in
                   (('MAIL_SERVER_USER', user), ('MAIL_SERVER_PASSWORD', password))
                   if not value]
        raise MailServerNotConfigured(
            f"{' and '.join(missing)} must both be set -- the mail server's "
            "send endpoint requires an authenticated session, and an "
            "unauthenticated POST is rejected with 401 'Token is missing'."
        )

    return MailServerSettings(
        api=api,
        user=user,
        password=password,
        ca_bundle=_get('MAIL_SERVER_CA_BUNDLE'),
    )
