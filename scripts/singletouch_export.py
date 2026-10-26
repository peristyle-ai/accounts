#!/usr/bin/env python3
"""Download the STP (One Touch Payroll) CSV(s) from the accounts API and
email them to the operator for review / portal upload.

This is a thin wrapper. It does NOT build the CSV itself -- the accounts
API (`GET /api/exports/payroll/one-touch-payroll`) is the single source of
truth for the file bytes, so the file that lands in the inbox is
byte-identical to the one the portal will be fed. (Re-implementing
build_stp_csv here would create a second code path that drifts.)

The endpoint returns two different shapes depending on the date range:

  * one pay run in range   -> `text/csv` (the CSV itself)
  * several pay runs       -> `application/zip` (one CSV per pay run)

We handle both. The ZIP is unpacked and each CSV is attached SEPARATELY,
because the ATO portal validates each uploaded file independently and
requires "Pay or update date" to be identical across every record in a
file -- a combined multi-run file would be rejected. One attachment per
pay run is exactly what the portal wants.

Usage
-----
    python3 scripts/singletouch_export.py --from-date 2026-06-22 --to-date 2026-06-22
    python3 scripts/singletouch_export.py --from-date 2026-06-01 --to-date 2026-06-30
    python3 scripts/singletouch_export.py --from-date 2026-06-01 --to-date 2026-06-30 --dry-run

Why ASCII-only: the mail server's /api/emails/mime endpoint rejects any
byte >= 0x80 in the payload. Subject/body use "--" instead of em-dash.
The CSV itself is fine -- it rides as base64 inside the MIME payload,
which is all bytes < 0x80.

Why a browser User-Agent: Cloudflare 403s Python's default urllib UA
with error code 1010. We send a Chrome UA plus the X-API-Key header so
the Flask backend still authenticates us.

The mail-server plumbing lives in scripts/mailclient.py, shared with
scripts/run_weekly_payroll.py.

This script is READ-ONLY against the accounts API. It does not change any
pay event status and does not upload anything to the portal -- the portal
upload stays a manual, human step.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import ssl
import sys
import zipfile
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# Shared mail client (scripts/mailclient.py) -- one implementation for the
# mail server, extracted from this file on 2026-10-22.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mailclient

# --- path resolution -----------------------------------------------------
# This script lives in the accounts repo, but the MAIL credentials live in
# the operating agent's workspace .env, which is NOT version controlled (the
# harness .gitignore excludes pie/). So we look for the workspace in a few
# candidate places and merge env files: os.environ wins, then the workspace
# .env, then this repo's .env.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent

_WORKSPACE = mailclient.find_workspace()
DEFAULT_OUT_DIR = ((_WORKSPACE / "payroll" / "stp") if _WORKSPACE
                   else Path("/opt/accounts/workspace/payroll/stp"))
SENDER = mailclient.SENDER
DEFAULT_RECIPIENT = mailclient.DEFAULT_RECIPIENT
_BROWSER_UA = mailclient.BROWSER_UA

# Columns we summarise in the email body. Kept as names (not indexes) so a
# column-order change upstream surfaces as a KeyError we can read, rather
# than silently reporting the wrong number.
_SUMMARY_FIELDS = (
    "Pay or update date",
    "Payrun date time",
    "Family name",
    "Given name",
    "Employee gross pay",
    "Employee tax",
    "Super guarantee amount",
    "Entity ABN",
    "Product ID",
)


def fetch_export(api_base: str, api_key: str, from_date: str | None,
                 to_date: str | None, employee_id: str | None,
                 ctx: ssl.SSLContext | None) -> tuple[bytes, str]:
    """Call the STP export endpoint. Returns (payload_bytes, content_type).

    NOTE: `ctx` is deliberately NOT used here. The accounts API host is a
    public Cloudflare origin with a valid certificate, so it must be
    validated against the SYSTEM trust store. The custom CA is only needed
    for the mail server's self-signed cert.
    """
    url = f"{api_base.rstrip('/')}/api/exports/payroll/one-touch-payroll"
    params = []
    if from_date:
        params.append(f"from_date={from_date}")
    if to_date:
        params.append(f"to_date={to_date}")
    if employee_id:
        params.append(f"employee_id={employee_id}")
    if params:
        url = f"{url}?{'&'.join(params)}"

    req = Request(url, headers={
        "X-API-Key": api_key,
        "User-Agent": _BROWSER_UA,
        "Accept": "text/csv, application/zip, application/json",
    })
    try:
        with urlopen(req, timeout=60) as r:
            return r.read(), r.headers.get("Content-Type", "")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:1000]
        raise SystemExit(
            f"HTTP {exc.code} from the STP export endpoint\n"
            f"URL: {url}\nBody: {body}\n\n"
            "Note: a 404 here usually means USER SCOPING, not a broken "
            "route -- pay events are scoped by user_id, so the API key "
            "must belong to the user who owns the pay events."
        )


def split_payload(payload: bytes, content_type: str) -> list[tuple[str, bytes]]:
    """Return [(filename, csv_bytes)] for either a CSV or a ZIP response."""
    ct = (content_type or "").lower()
    if "zip" in ct or payload[:2] == b"PK":
        out = []
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for name in sorted(zf.namelist()):
                if name.lower().endswith(".csv"):
                    out.append((Path(name).name, zf.read(name)))
        if not out:
            raise SystemExit("ZIP response contained no .csv members")
        return out
    # Single CSV. Derive a filename from the first data row's pay date if we
    # can, else fall back to a generic name.
    name = "stp-payrun.csv"
    try:
        reader = csv.DictReader(io.StringIO(payload.decode("utf-8")))
        first = next(reader, None)
        if first:
            d = (first.get("Pay or update date") or "").strip()
            if d:
                d = d.split()[0]
                parts = d.split("/")
                if len(parts) == 3:
                    name = f"stp-payrun-{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}.csv"
    except Exception:  # noqa: BLE001 - filename is cosmetic
        pass
    return [(name, payload)]


def summarise(csv_bytes: bytes) -> dict:
    """Pull a few headline numbers out of one STP CSV for the email body."""
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")))
    rows = list(reader)
    if not rows:
        return {"rows": 0}
    first = rows[0]
    info = {"rows": len(rows)}
    for field in _SUMMARY_FIELDS:
        if field in first:
            info[field] = (first.get(field) or "").strip()
    return info


def _fmt_money(raw: str) -> str:
    try:
        return f"${float(raw):,.2f}"
    except (TypeError, ValueError):
        return raw or "-"


def build_body(files: list[tuple[str, bytes]], summaries: list[dict],
               recipient: str) -> str:
    """ASCII-only body. Never embed $<digits> in a shell-quoted string."""
    lines = [
        "Hi the operator,",
        "",
        "The Single Touch Payroll (STP) export for the requested date range "
        "is attached, ready for upload to the ATO portal.",
        "",
    ]
    for (name, _), s in zip(files, summaries):
        pay_date = s.get("Pay or update date", "-")
        who = " ".join(x for x in (s.get("Given name"), s.get("Family name")) if x)
        lines.append(f"  File:       {name}")
        lines.append(f"  Pay date:   {pay_date}")
        lines.append(f"  Employee:   {who or '-'}")
        lines.append(f"  Gross pay:  {_fmt_money(s.get('Employee gross pay', ''))}")
        lines.append(f"  PAYG tax:   {_fmt_money(s.get('Employee tax', ''))}")
        lines.append(f"  Super:      {_fmt_money(s.get('Super guarantee amount', ''))}")
        lines.append(f"  Rows:       {s.get('rows', 0)}")
        lines.append(f"  Entity ABN: {s.get('Entity ABN', '-')}")
        lines.append(f"  Product ID: {s.get('Product ID', '-')}")
        lines.append("")

    if len(files) > 1:
        lines += [
            f"There are {len(files)} attachments -- one per pay run. The ATO "
            "portal validates each file on its own and requires the pay date "
            "to be identical across all records in a file, so they must be "
            "uploaded as SEPARATE files, not merged.",
            "",
        ]
    lines += [
        "NOTE: the Product ID above is still the PLACEHOLDER value. This "
        "export validates the file FORMAT only -- it is not yet lodged "
        "against the employer's real ATO-issued product ID.",
        "",
        "No upload has been made to the portal -- that step is still manual.",
        "",
        "the operating agent",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-date", help="YYYY-MM-DD (default: no lower bound)")
    p.add_argument("--to-date", help="YYYY-MM-DD (default: no upper bound)")
    p.add_argument("--employee-id", help="Optional employee UUID filter")
    p.add_argument("--recipient", default=None,
                   help="Recipient address. Default: PAYROLL_FILES_TO from "
                        "the environment (there is no built-in default)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--env", type=Path, action="append", dest="envs",
                   help="Env file to load (repeatable). Default: the operating "
                        "workspace .env then the accounts repo .env.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch and save the CSVs but do not send email")
    args = p.parse_args()

    env_paths = args.envs or [p for p in (
        (_WORKSPACE / ".env") if _WORKSPACE else None,
        _REPO / ".env",
    ) if p is not None]
    env = mailclient.load_env(env_paths)
    print("Env files: " + ", ".join(str(p) for p in env_paths))
    missing = [k for k in ("PY_PG_ACCOUNTS_BASE_URL", "PY_PG_ACCOUNTS_API_KEY")
               if not env.get(k)]
    if missing:
        print(f"Missing required env var(s): {missing}", file=sys.stderr)
        return 2
    api_base = env["PY_PG_ACCOUNTS_BASE_URL"]
    api_key = env["PY_PG_ACCOUNTS_API_KEY"]
    mail_server = env.get("EMAIL_SERVER", "")
    mail_password = env.get("EMAIL_PASSWORD", "")

    mail_ctx = mailclient.mail_ssl_context(env)

    label = f"{args.from_date or 'start'} to {args.to_date or 'end'}"
    print(f"Fetching STP export for {label} from {api_base} ...")
    payload, content_type = fetch_export(
        api_base, api_key, args.from_date, args.to_date, args.employee_id, None)
    print(f"Response content-type: {content_type or '(none)'} "
          f"({len(payload)} bytes)")

    files = split_payload(payload, content_type)
    print(f"Extracted {len(files)} CSV file(s):")
    for name, data in files:
        print(f"  {name}  {len(data)} bytes")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    summaries = []
    for name, data in files:
        path = args.out_dir / name
        path.write_bytes(data)
        saved.append(path)
        summaries.append(summarise(data))
        print(f"Saved -> {path}")

    for s in summaries:
        print(f"  summary: {s}")

    if args.dry_run:
        print("\nDRY RUN -- no email sent.")
        return 0

    if not mail_password or not mail_server:
        print("EMAIL_SERVER / EMAIL_PASSWORD missing from .env",
              file=sys.stderr)
        return 2

    try:
        sender = mailclient.require_sender()
        recipient = args.recipient or mailclient.require_recipient()
    except mailclient.MailConfigError as exc:
        print(f"MAIL CONFIG ERROR: {exc}", file=sys.stderr)
        return 2

    token = login(mail_server, sender, mail_password, mail_ctx)
    print(f"Logged in as {sender}")

    subject = f"STP (One Touch Payroll) export for review - {label}"
    body = build_body(files, summaries, recipient)
    msg = mailclient.build_message(subject, body, recipient)
    for (name, data), path in zip(files, saved):
        msg.add_attachment(data, maintype="text", subtype="csv", filename=name)

    status, resp = send_mime(mail_server, token, msg, recipient, mail_ctx)
    print(f"HTTP {status}")
    print(json.dumps(resp, indent=2, default=str))
    if status not in (200, 201):
        return 1

    print(f"\nEmail ID:   {resp.get('id')}")
    print(f"To:         {recipient}")
    print(f"Subject:    {subject}")
    for path in saved:
        print(f"Attachment: {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
