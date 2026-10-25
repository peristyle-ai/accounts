"""Shared client for the local mail server (HTTPS, self-signed cert).

Extracted 2026-10-22 from scripts/singletouch_export.py so that the
weekly payroll run and the STP wrapper talk to the mail server through
ONE implementation. Previously the MIME-sending code was copy-pasted
per script, which is how the two drifted apart in the first place.

Facts encoded here (all learned the hard way):

  * The mail server is HTTPS-only since 2026-09-13 and serves a
    self-signed cert. The per-user CA bundle lives at
    the workspace's ssl/py_pg_email.crt -- the system-wide symlink that
    MEMORY.md mentions is NOT present on this host, so we point at the
    per-user copy explicitly.
  * /api/emails/mime requires a Bearer token: authenticate with
    POST /auth/login first and pass the token as `Authorization`.
  * The endpoint rejects any byte >= 0x80 in the submitted MIME, so
    subject and body must be pure ASCII (base64 attachments are fine --
    they are all < 0x80).
  * Cloudflare 403s Python's default urllib User-Agent with code 1010,
    so every request sends a browser UA.

The MAIL credentials live in the workspace .env, not in the
accounts repo .env, so the workspace has to be located at runtime.
"""
from __future__ import annotations

import json
import os
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


# The repository's own .env -- the deployment's configuration.
REPO_ROOT = Path(__file__).resolve().parent.parent

WORKSPACE_VARS = ("OPERATING_AGENT_WORKSPACE", "PAYROLL_WORKSPACE")


def find_workspace() -> Path | None:
    """Locate the workspace that holds .env, ssl/ and the payroll dir.

    Resolution order: OPERATING_AGENT_WORKSPACE, PAYROLL_WORKSPACE (from the
    environment, then from this repository's own .env), the sibling checkout
    under the operator's home directory, then the documented install path.
    The first candidate that contains a .env wins.

    The repository .env is consulted because these scripts are often run
    from a plain shell where nothing is exported: without it the workspace
    was never found, its .env was never loaded, and the script died with
    "missing PY_PG_ACCOUNTS_API_KEY" -- the documented default behaviour.
    """
    candidates = []
    for var in WORKSPACE_VARS:
        val = os.environ.get(var)
        if val:
            candidates.append(Path(val))

    repo_env = REPO_ROOT / ".env"
    if repo_env.is_file():
        for line in repo_env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in WORKSPACE_VARS and v.strip():
                candidates.append(Path(v.strip()))

    candidates.append(Path.home() / "harness" / "workspace")
    candidates.append(Path("/opt/accounts/workspace"))
    for c in candidates:
        if (c / ".env").is_file():
            return c
    return None


WORKSPACE = find_workspace()
DEFAULT_CA = (str(WORKSPACE / "ssl" / "py_pg_email.crt") if WORKSPACE
              else "/opt/accounts/workspace/ssl/py_pg_email.crt")


def read_env_file(path: Path) -> dict:
    env: dict = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()
    return env


WS_ENV = read_env_file(WORKSPACE / ".env") if WORKSPACE else {}

class MailConfigError(RuntimeError):
    """The mail configuration is missing or unusable."""


# Reserved for documentation by RFC 2606 / 6761. A mail server can never
# be one of these, and a message can never legitimately be sent from one.
# The app's own resolver (app/shared/mail_config.py) refuses them; these
# scripts have to refuse them too, or a misconfiguration turns into a
# silent no-op -- a "sent" message that went nowhere.
RESERVED_DOMAINS = {
    "example.com", "example.org", "example.net", "example.edu",
    "localhost", "invalid", "test",
}


def _is_reserved(address: str | None) -> bool:
    if not address:
        return False
    domain = address.rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return True
    if domain in RESERVED_DOMAINS:
        return True
    return any(domain.endswith("." + d) or domain.endswith(d)
               for d in (".example", ".invalid", ".test", ".localhost"))


def _first_configured(*values):
    for v in values:
        if v and v.strip():
            return v.strip()
    return None


# The sending address and the default recipient are CONFIGURATION, not code:
# a deployment overrides them from the workspace .env.
# The workspace's own mailbox wins: these scripts are workspace tools, and
# the app's MAIL_FROM_ADDRESS belongs to the app's own mail flows.
#
# There is deliberately NO built-in default (2026-10-25). The old fallback was
# `payroll@example.com` -- a reserved documentation domain. A deployment that
# had not configured its addresses therefore appeared to send mail, and the
# message went nowhere. Now an unconfigured address is an error, not a
# silent success.
SENDER = _first_configured(
    WS_ENV.get("EMAIL_ADDRESS"),
    os.environ.get("MAIL_FROM_ADDRESS"),
)
DEFAULT_RECIPIENT = _first_configured(
    os.environ.get("PAYROLL_FILES_TO"),
    WS_ENV.get("PAYROLL_FILES_TO"),
)


def require_sender() -> str:
    """The sending address, or a loud error explaining how to set it."""
    if not SENDER:
        raise MailConfigError(
            "No sending address configured. Set EMAIL_ADDRESS in the "
            "workspace .env, or MAIL_FROM_ADDRESS in the environment."
        )
    if _is_reserved(SENDER):
        raise MailConfigError(
            f"The configured sending address {SENDER!r} is a reserved "
            "documentation domain and cannot send mail. Set EMAIL_ADDRESS "
            "or MAIL_FROM_ADDRESS to a real address."
        )
    return SENDER


def require_recipient() -> str:
    """The default recipient, or a loud error explaining how to set it."""
    if not DEFAULT_RECIPIENT:
        raise MailConfigError(
            "No recipient configured. Set PAYROLL_FILES_TO in the workspace "
            ".env or the environment, or pass --email-files-to / --recipient."
        )
    if _is_reserved(DEFAULT_RECIPIENT):
        raise MailConfigError(
            f"The configured recipient {DEFAULT_RECIPIENT!r} is a reserved "
            "documentation domain and cannot receive mail. Set "
            "PAYROLL_FILES_TO to a real address."
        )
    return DEFAULT_RECIPIENT


def load_env(paths: list[Path]) -> dict:
    """Merge env files, earliest file wins; os.environ beats them all."""
    env: dict = {}
    for p in paths:
        for k, v in read_env_file(p).items():
            env.setdefault(k, v)
    for k, v in os.environ.items():
        env[k] = v
    return env


def mail_ssl_context(env: dict) -> ssl.SSLContext | None:
    """SSL context trusting the mail server's self-signed cert."""
    ca = env.get("EMAIL_SSL_CERT") or DEFAULT_CA
    ca_path = Path(ca)
    if not ca_path.exists():
        print(f"WARNING: mail CA cert not found at {ca_path}; "
              f"falling back to system trust store", file=__import__("sys").stderr)
        return None
    ctx = ssl.create_default_context(cafile=str(ca_path))
    if not env.get("EMAIL_VERIFY_TLS", "true").lower() in ("true", "1", "yes"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def login(server: str, email: str, password: str,
          ctx: ssl.SSLContext | None) -> str:
    """Authenticate with the mail server and return a bearer token."""
    req = Request(
        f"{server.rstrip('/')}/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
        method="POST",
    )
    with urlopen(req, timeout=15, context=ctx) as r:
        return json.loads(r.read())["token"]


def send_mime(server: str, token: str, msg: EmailMessage, recipient: str,
              ctx: ssl.SSLContext | None) -> tuple[int, dict]:
    """POST a MIME message to the mail server. Returns (status, json)."""
    raw = msg.as_bytes()
    if any(b >= 128 for b in raw):
        for i, b in enumerate(raw):
            if b >= 128:
                raise RuntimeError(
                    f"non-ASCII byte 0x{b:02x} at offset {i}: "
                    f"...{raw[max(0, i - 40): i + 30]!r}"
                )
        raise RuntimeError("non-ASCII bytes in MIME - abort")
    req = Request(
        f"{server.rstrip('/')}/api/emails/mime",
        data=json.dumps({"to": recipient,
                         "mime_content": raw.decode("ascii")}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": BROWSER_UA,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30, context=ctx) as r:
            return r.status, json.loads(r.read())
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        return exc.code, {"error": str(exc), "body": body}


def build_message(subject: str, body: str, recipient: str,
                  sender: str | None = None) -> EmailMessage:
    """Build a plain-text MIME message (ASCII-only, enforced later)."""
    sender = sender or require_sender()
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    # The Message-ID domain follows the sender. It used to be hardcoded to
    # "example.com", which is a reserved documentation domain and makes the
    # message look like it came from a placeholder.
    msg["Message-ID"] = make_msgid(
        domain=(sender.rsplit("@", 1)[-1].strip() or "localhost")
    )
    msg.set_content(body)
    return msg


def send_files(server: str, token: str, recipient: str, subject: str,
               body: str, files: list[tuple[str, bytes, str, str]],
               ctx: ssl.SSLContext | None) -> tuple[int, dict]:
    """Send one MIME message with the given files attached.

    files: list of (filename, bytes, maintype, subtype).
    """
    msg = build_message(subject, body, recipient)
    for name, data, maintype, subtype in files:
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=name)
    return send_mime(server, token, msg, recipient, ctx)
