#!/usr/bin/env python3
"""End-to-end weekly payroll run for Sample Employee.

Does the full workflow after the operator says 'I paid Sample Employee':
  1. Create the pay event (draft) for the week just paid.
  2. Finalize it.
  3. Generate the payslip PDF.
  4. Send the payslip to Sample Employee (work + personal inbox via the
     /api/payroll/pay-events/<id>/send-payslip endpoint).
  5. Mark the pay event as paid.
  6. Create a pending SuperPayment covering the same week.
  7. Build the AusSuper Mapped CSV upload file.
  8. Build the NAB ABA file for the super payment to the clearing house.
  9. Build the STP (One Touch Payroll) CSV for the ATO portal.
 10. Build the NAB ABA file for the WAGE payment to the employee.

Defaults assume the employee's standard weekly cadence (Tue-Mon workweek,
paid on Wednesday the next day). Override any of the constants below
for off-cycle runs. The script refuses to double-create: if a draft
or paid pay event already exists for the same period, it surfaces that
instead of clobbering it.

Usage:
  python3 scripts/run_weekly_payroll.py [--period-start YYYY-MM-DD]
                                        [--period-end YYYY-MM-DD]
                                        [--payment-date YYYY-MM-DD]
                                        [--days D]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import requests

# Bootstrap the app package (so we can use build_mapped_csv + build_aba_file).
# The repo root is derived from this file's location -- a hardcoded path
# would break the moment the checkout moves.
PPGA_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PPGA_ROOT))

env_path = PPGA_ROOT / ".env"
if env_path.is_file():
    with open(env_path) as _f:
        for line in _f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Now safe to import accounts internals.
from app import create_app  # noqa: E402
from app.models.employee import Employee  # noqa: E402
from app.models.pay_event import PayEvent  # noqa: E402
from app.shared.aba import build_aba_file  # noqa: E402
from app.shared.stp_csv import load_employer_from_config  # noqa: E402
from app.shared.mapped_csv import build_mapped_csv  # noqa: E402

# The mail client lives next to this script (scripts/mailclient.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mailclient  # noqa: E402

# --- workspace paths + API base ---
# The workspace holds .env, ssl/ and the payroll directory. It is
# discovered rather than hardcoded: a deployment points OPERATING_AGENT_WORKSPACE at
# it, and a fresh install falls back to the documented path.
WORKSPACE = (mailclient.find_workspace()
             or Path(os.environ.get("PAYROLL_WORKSPACE",
                                    "/opt/accounts/workspace")))
PAYROLL_DIR = WORKSPACE / "payroll"
# The employee's own payroll constants live in a config.py of their own.
JP_DIR = Path(os.environ.get("PAYROLL_EMPLOYEE_CONFIG_DIR",
                             str(PAYROLL_DIR / "employee")))

# --- constants from config.py ---
# Load the employee's payroll config by EXPLICIT path. A plain `import config`
# picks up the accounts repo's own config.py (the YAML loader) because the
# app already imported it, so it is present in sys.modules.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("employee_config", JP_DIR / "config.py")
jp = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(jp)

# --- env from workspace ---
ENV_PATH = WORKSPACE / ".env"


def load_workspace_env() -> dict:
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(env: dict, method: str, path: str, json_body: dict | None = None) -> dict:
    base = env["PY_PG_ACCOUNTS_BASE_URL"]
    H = {"X-API-Key": env["PY_PG_ACCOUNTS_API_KEY"], "Content-Type": "application/json"}
    r = requests.request(method, f"{base}{path}", headers=H, json=json_body, timeout=30)
    if r.status_code >= 400:
        print(f"  {method} {path} -> {r.status_code}")
        print(f"  body: {r.text[:600]}")
        raise SystemExit(f"API error {r.status_code} on {method} {path}")
    return r.json() if r.text else {}


# =============================================================================
# Default pay period inference
# =============================================================================
def infer_default_period(today: date) -> tuple[date, date, date]:
    """Standard cadence: workweek Tue->Mon, paid Wed next day.

    Last paid week in accounts is 2026-09-15 -> 2026-09-21 (paid
    2026-09-23). The next expected slot is 2026-09-22 -> 2026-09-28
    (paid 2026-09-30). On Thursday 1 Oct 2026 (today), that's the
    "last completed workweek" — the period that was paid yesterday.

    Default behaviour: return the most recently COMPLETED Tue-Mon workweek
    (the one whose payment date is in the past). For Wed/Thu/Fri/Sat/Sun
    of any week, that means the workweek just-ended. For Mon/Tue, it's
    the workweek before the current one in progress.
    """
    # Most-recently-passed Monday. weekday() is 0=Mon..6=Sun; on Monday we
    # want last week's Monday (today - 7), otherwise subtract weekday.
    days_back = today.weekday() or 7
    prev_mon = today - timedelta(days=days_back)
    period_start = prev_mon - timedelta(days=6)   # Tue
    period_end = prev_mon                          # Mon
    payment_date = period_end + timedelta(days=2) # Wed
    return period_start, period_end, payment_date


# =============================================================================
# Workflow steps
# =============================================================================
def get_or_create_pay_event(env, employee_id, period_start, period_end,
                            payment_date, gross, payg, net, super_ote,
                            super_payable, bank_reference):
    """Reuse an existing pay event for the same period if one exists,
    otherwise create a draft."""
    pe_list = api(env, "GET", f"/api/payroll/pay-events?employee_id={employee_id}")
    for pe in pe_list.get("pay_events", []):
        if (pe.get("pay_period_start") == period_start.isoformat()
                and pe.get("pay_period_end") == period_end.isoformat()):
            print(f"  Found existing pay event {pe['id'][:8]} (status={pe['status']}) — reusing")
            return pe, False  # not created, just found

    payload = {
        "employee_id": employee_id,
        "pay_period_start": period_start.isoformat(),
        "pay_period_end": period_end.isoformat(),
        "payment_date": payment_date.isoformat(),
        "gross_amount": str(gross),
        "payg_tax_amount": str(payg),
        "net_amount": str(net),
        "super_ote_amount": str(super_ote),
        "super_payable_amount": str(super_payable),
        "bank_reference": bank_reference,
    }
    r = api(env, "POST", "/api/payroll/pay-events", payload)
    pe = r["pay_event"]
    print(f"  Created draft pay event {pe['id'][:8]}")
    return pe, True


def finalize_pay_event(env, pe_id):
    r = api(env, "POST", f"/api/payroll/pay-events/{pe_id}/finalize")
    print(f"  Finalized -> {r['pay_event']['status']}")
    return r["pay_event"]


def generate_payslip_pdf(env, pe_id):
    r = api(env, "POST", f"/api/payroll/pay-events/{pe_id}/generate-pdf")
    print(f"  PDF generated: {r['payslip_pdf_path']}  ({r['size_bytes']} bytes)")
    return r


def send_payslip(env, pe_id, recipient_kinds=None):
    """Send the payslip.

    recipient_kinds: optional list of 'work' / 'personal' overriding the
    server-side PAYSLIP_RECIPIENT_MODES setting (which itself defaults to
    both). the operator set that setting to 'personal' on 2026-10-22 so
    Sample Employee's payslips go to her personal Gmail only.
    """
    body = {"recipient_kinds": list(recipient_kinds)} if recipient_kinds else {}
    r = api(env, "POST", f"/api/payroll/pay-events/{pe_id}/send-payslip",
            json_body=body)
    print(f"  Payslip email: {r.get('message')}")
    if r.get("deliveries"):
        for d in r["deliveries"]:
            print(f"    -> {d.get('recipient')}  id={d.get('email_id')}  status={d.get('status')}")
    return r


def email_generated_files(env, recipient, files, period, payment_date,
                          gross, net, super_payable, paid=False):
    """Email the generated upload files to the operator.

    ASCII-only body: the mail server's /api/emails/mime endpoint rejects
    any byte >= 0x80 in the submitted MIME.
    """
    server = env.get("EMAIL_SERVER") or ""
    password = env.get("EMAIL_PASSWORD") or ""
    if not server or not password:
        print("  !! EMAIL_SERVER / EMAIL_PASSWORD missing from .env - "
              "files not emailed")
        return None

    ctx = mailclient.mail_ssl_context(env)
    token = mailclient.login(server, mailclient.require_sender(), password, ctx)

    lines = [
        f"Hi {os.environ.get('PAYROLL_OPERATOR_NAME', 'there')},",
        "",
        ("The payroll files for the week just paid are attached, ready for "
         "upload:" if paid else
         "The payroll files for next week are attached, ready for upload. "
         "The pay event has NOT been marked paid -- that happens once the "
         "bank transfer has cleared:"),
        "",
    ]
    for f in files:
        lines.append(f"  {f.name}  ({f.stat().st_size} bytes)")
    lines += [
        "",
        f"  Pay period:     {period}",
        f"  Payment date:   {payment_date}",
        f"  Gross:          ${gross}",
        f"  Net:            ${net}",
        f"  Super payable:  ${super_payable}",
        "",
        "Upload destinations:",
        "  peristyle-wage-*.aba     -> NAB (pays the employee's wage)",
        "  peristyle-super-*.aba    -> clearing house (Wrkr Pay, the super)",
        "  aussuper-mapped-*.csv    -> AustralianSuper",
        "  stp-payrun-*.csv         -> ATO Single Touch Payroll portal",
        "",
        ("Nothing has been uploaded." if paid else
         "Nothing has been uploaded and nothing has been marked paid -- the "
         "pay event is still waiting for the bank transfer."),
        "",
        "the operating agent",
    ]
    body = "\n".join(lines)

    attachments = []
    for f in files:
        data = f.read_bytes()
        if f.suffix.lower() == ".csv":
            attachments.append((f.name, data, "text", "csv"))
        else:
            attachments.append((f.name, data, "application", "octet-stream"))

    subject = (f"Payroll files for upload - {period_start_label(period)} "
               f"- payment {payment_date}")
    status, resp = mailclient.send_files(
        server, token, recipient, subject, body, attachments, ctx)
    print(f"  Email status: {status}")
    if status not in (200, 201):
        print(f"  !! email failed: {resp}")
        return None
    print(f"  Email id: {resp.get('id')}")
    return resp.get("id")


def period_start_label(period):
    return period.split(" to ")[0]


def fetch_wage_aba(env, from_date, to_date, process_date=None):
    """Download the wage ABA from the accounts API.

    GET /api/exports/payroll/aba returns the NAB Direct Entry file for
    every matching pay event -- the wage payment to the employee. We call
    the API rather than rebuilding the file locally so there is a single
    source of truth for the bytes that get uploaded to NAB.

    status=finalized,paid covers both cases: a pay run prepared but not
    yet transferred, and one already paid.
    """
    base = env["PY_PG_ACCOUNTS_BASE_URL"].rstrip("/")
    url = (f"{base}/api/exports/payroll/aba"
           f"?status=finalized,paid&from_date={from_date}&to_date={to_date}")
    if process_date:
        url += f"&process_date={process_date}"
    H = {"X-API-Key": env["PY_PG_ACCOUNTS_API_KEY"]}
    r = requests.get(url, headers=H, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(
            f"wage ABA export failed: HTTP {r.status_code} {r.text[:400]}")
    return r.content


def mark_paid(env, pe_id, payment_reference=None):
    """Mark the pay event paid.

    payment_reference is the bank's confirmation id (e.g. the OSKO
    confirmation id, or the ABA lodgement reference). Recorded on the pay
    event so the payment can be reconciled later.
    """
    body = {}
    if payment_reference:
        body["payment_reference"] = payment_reference
    r = api(env, "POST", f"/api/payroll/pay-events/{pe_id}/mark-paid",
            json_body=body)
    print(f"  Marked paid (status={r['pay_event']['status']}"
          + (f", payment_reference={payment_reference}"
             if payment_reference else "") + ")")
    return r["pay_event"]


def find_super_payment_for_pay_event(env, employee_id, pay_event_id):
    """Return an existing SuperPayment covering this pay event, or None.

    The weekly run may be executed more than once for the same pay week
    (a timer plus a manual run, or a re-run after a correction). Creating
    a second SuperPayment for the same pay event would double-count the
    super owing, so we look for an existing one first and reuse it.
    """
    r = api(env, "GET", f"/api/payroll/super-payments?employee_id={employee_id}")
    for sp in r.get("super_payments", []):
        if sp.get("status") == "cancelled":
            continue
        if pay_event_id in (sp.get("pay_event_ids") or []):
            return sp
    return None


def create_super_payment(env, employee_id, pay_event_ids, remittance_date,
                         amount, fund_name, fund_member, notes=""):
    payload = {
        "employee_id": employee_id,
        "pay_event_ids": pay_event_ids,
        "remittance_date": remittance_date.isoformat(),
        "amount": str(amount),
        "fund_name_snapshot": fund_name,
        "fund_member_snapshot": fund_member,
        "notes": notes,
    }
    r = api(env, "POST", "/api/payroll/super-payments", payload)
    sp = r["super_payment"]
    print(f"  Created super payment {sp['id'][:8]} (status={sp['status']})")
    return sp


# =============================================================================
# AusSuper Mapped CSV + NAB ABA (direct from accounts internals)
# =============================================================================
def build_aussuper_csv(app, pe_id, fund_name, employer_abn):
    """Build the AusSuper Mapped CSV in the format that build_aussuper_mapped_csv_for_event.py
    uses (same shape as /api/payroll/exports/super-csv)."""
    with app.app_context():
        pe = PayEvent.query.get(pe_id)
        emp = Employee.query.get(pe.employee_id)
        name_parts = (emp.legal_name or "").strip().split(maxsplit=1)
        family_name = name_parts[-1] if name_parts else ""
        given_name = name_parts[0] if name_parts else ""
        phone = (emp.phone or "").replace(" ", "").replace("-", "")
        row = {
            "member_number":      emp.super_fund_member_no,
            "family_name":        family_name,
            "given_name":         given_name,
            "tfn":                emp.tfn_plain,
            "date_of_birth":      emp.date_of_birth_plain,
            "sex":                emp.sex,
            "address_line1":      emp.address_line1,
            "city":               emp.city,
            "state":              emp.state,
            "postcode":           emp.postcode,
            "phone":              phone,
            "fund_name":          emp.super_fund_name,
            "pay_period_start":   pe.pay_period_start,
            "pay_period_end":     pe.pay_period_end,
            "transaction_date":   pe.payment_date,
            "payment_reference":  pe.bank_reference or "",
            "sgc_amount":         pe.super_payable_amount,
        }
        return build_mapped_csv([row], employer_id=employer_abn), pe


def build_nab_aba(app, env, payee_label, lodgement_ref, amount, process_date):
    with app.app_context():
        from app.models.user import User
        from app.models.payee_directory import Payee
        payer = User.query.filter_by(email=env["EMAIL_ADDRESS"]).first()
        if payer is None:
            raise SystemExit(f"Payer {env['EMAIL_ADDRESS']!r} not found")
        if not payer.bsb_plain or not payer.account_number_plain:
            raise SystemExit(f"Payer {env['EMAIL_ADDRESS']!r} missing business bank details")
        payee = (Payee.query
                 .filter_by(user_id=payer.id, label=payee_label, is_active=True)
                 .first())
        if payee is None:
            raise SystemExit(f"No active Payee labelled {payee_label!r}")
        payment = {
            "bsb":             payee.bsb_plain,
            "account_number":  payee.account_number_plain,
            "account_name":    payee.account_name,
            "amount":          Decimal(amount),
            "lodgement_ref":   lodgement_ref,
        }
        return build_aba_file(payer, [payment], processing_date=process_date), payer, payee


# =============================================================================
# main
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="End-to-end weekly payroll run for Sample Employee.")
    parser.add_argument("--period-start", help="Pay period start YYYY-MM-DD (default: last Tue)")
    parser.add_argument("--period-end", help="Pay period end YYYY-MM-DD (default: following Mon)")
    parser.add_argument("--payment-date", help="Payment date YYYY-MM-DD (default: following Wed)")
    parser.add_argument("--days", type=int, default=5,
                        help="Days worked in the period (default 5, full week)")
    parser.add_argument("--rate", type=float, default=50.0,
                        help="Hourly rate AUD (default 50, locked in payroll/employee/config.py)")
    parser.add_argument("--net", type=float, default=1508.13,
                        help="Actual net amount deposited (PAYG is back-calculated as gross - net, "
                             "matching the canonical payroll send scripts. Default 1508.13 for the "
                             "standard full-week net. Override if the operator paid a different amount.)")
    parser.add_argument("--bank-ref", default=None,
                        help="Bank reference for the wage payment (e.g. an OSKO ref). "
                             "If omitted, defaults to '<YYYYMMDD>JP' using the employee's "
                             "bank_reference_prefix from the Employee record.")
    parser.add_argument("--skip-payslip-email", action="store_true",
                        help="Generate the payslip PDF but do NOT email it to Sample Employee.")
    parser.add_argument("--phase", choices=("full", "prepare", "complete"),
                        default="full",
                        help="full    = the whole run (create, payslip, mark paid, files). "
                             "prepare = everything EXCEPT the payslip email and the "
                             "mark-paid step; produces the upload files (including the "
                             "wage ABA) and emails them for upload. Use this before the "
                             "payment is made. "
                             "complete = mark an already-prepared pay event paid and "
                             "email the payslip, once the payment has cleared.")
    parser.add_argument("--payment-reference",
                        help="Bank confirmation/reference for the wage payment, used "
                             "by --phase complete. E.g. the ABA lodgement reference "
                             "or the OSKO confirmation id.")
    parser.add_argument("--email-files-to",
                        help="Email the generated upload files to this address "
                             "(default: PAYROLL_FILES_TO from the environment).")
    parser.add_argument("--aba-process-date",
                        help="'Date to be processed' for the wage ABA "
                             "(DDMMYY per the ABA spec). Default: the payment date. "
                             "NAB generally needs this to be at least the next "
                             "business day.")
    parser.add_argument("--payslip-recipient-kinds",
                        help="Comma-separated 'work' / 'personal' overrides for the "
                             "payslip email. Defaults to the server-side "
                             "PAYSLIP_RECIPIENT_MODES setting (currently 'personal').")
    parser.add_argument("--skip-mark-paid", action="store_true",
                        help="Leave the pay event at 'finalized' instead of marking it "
                             "paid (use when the bank transfer has not cleared yet).")
    args = parser.parse_args()

    env = load_workspace_env()
    api_key = env["PY_PG_ACCOUNTS_API_KEY"]
    assert api_key, "PY_PG_ACCOUNTS_API_KEY missing"

    # Phase semantics. 'prepare' runs everything up to file generation
    # but stops short of marking the event paid and emailing the payslip
    # -- those happen in 'complete', once the bank transfer has actually
    # cleared. 'full' is the original single-shot behaviour.
    phase = args.phase
    if phase == "prepare":
        args.skip_payslip_email = True
        args.skip_mark_paid = True
    elif phase == "complete":
        if not args.payment_reference:
            print("ERROR: --phase complete requires --payment-reference",
                  file=sys.stderr)
            return 2
        # 'complete' is the other half of 'prepare': the money has moved,
        # so now send the payslip and record the payment.
        args.skip_payslip_email = False
        args.skip_mark_paid = False

    today = date.today()
    default_start, default_end, default_payment = infer_default_period(today)
    period_start = date.fromisoformat(args.period_start) if args.period_start else default_start
    period_end = date.fromisoformat(args.period_end) if args.period_end else default_end
    payment_date = date.fromisoformat(args.payment_date) if args.payment_date else default_payment

    hours_per_day = Decimal(str(jp.HOURS_PER_DAY))  # 7.6
    gross = (Decimal(str(args.rate)) * hours_per_day * args.days).quantize(Decimal("0.01"))
    net = Decimal(str(args.net)).quantize(Decimal("0.01"))
    # PAYG = gross - net (matches the canonical send_*.py scripts: PAYG is back-
    # calculated from the actual net that hit the employee's bank).
    payg = (gross - net).quantize(Decimal("0.01"))
    super_rate = Decimal("0.12")
    super_payable = (gross * super_rate).quantize(Decimal("0.01"))
    # Employee record -- needed for the bank reference prefix and for the
    # super fund member number. Fetched once, up front.
    emp_id = api(env, "GET", "/api/payroll/employees")["employees"][0]["id"]
    emp_detail = api(env, "GET", f"/api/payroll/employees/{emp_id}")["employee"]
    if args.bank_ref:
        bank_reference = args.bank_ref
    else:
        # Default to '<YYYYMMDD><JP>' using the employee's bank_reference_prefix.
        prefix = emp_detail.get("bank_reference_prefix") or "JP"
        bank_reference = f"{payment_date.strftime('%Y%m%d')}{prefix}"

    print(f"=== Weekly payroll run — Sample Employee ===")
    print(f"Period:          {period_start.isoformat()} -> {period_end.isoformat()}")
    print(f"Payment date:    {payment_date.isoformat()}")
    print(f"Days worked:     {args.days}")
    print(f"Gross:           ${gross}")
    print(f"PAYG:            ${payg}")
    print(f"Net:             ${net}")
    print(f"Super (12% OTE): ${super_payable}")
    print(f"Bank reference:  {bank_reference}")
    print()

    # Discover employee (already fetched for bank_reference_prefix above; just print the id).
    print(f"Employee ID:     {emp_id}")
    employee_id = emp_id
    print()

    # Step 1: pay event
    print("Step 1: create/reuse pay event...")
    pe, _created = get_or_create_pay_event(
        env, employee_id, period_start, period_end, payment_date,
        gross, payg, net, gross, super_payable, bank_reference,
    )
    pe_id = pe["id"]
    print()

    # Step 2: finalize (only if draft)
    if pe["status"] == "draft":
        print("Step 2: finalize...")
        finalize_pay_event(env, pe_id)
        print()
    else:
        print(f"Step 2: already status={pe['status']}, skipping finalize")
        print()

    # Step 3: generate PDF
    print("Step 3: generate PDF...")
    generate_payslip_pdf(env, pe_id)
    print()

    # Step 4: send payslip to Sample Employee (work + personal inboxes)
    if args.skip_payslip_email:
        print("Step 4: SKIPPED (--skip-payslip-email) - PDF generated, not emailed")
        print()
    else:
        print("Step 4: send payslip to Sample Employee...")
        kinds = ([k.strip() for k in args.payslip_recipient_kinds.split(",")
                  if k.strip()] if args.payslip_recipient_kinds else None)
        r = send_payslip(env, pe_id, recipient_kinds=kinds)
        for d in r.get("deliveries") or []:
            print(f"    -> {d.get('recipient_email') or d.get('recipient')}  "
                  f"status={d.get('delivery_status') or d.get('status')}")
        print()

    # Step 5: mark paid (assumes the wage transfer has cleared — the operator said 'I paid')
    if args.skip_mark_paid:
        print("Step 5: SKIPPED (--skip-mark-paid) - event left at 'finalized'")
        print()
    else:
        print("Step 5: mark paid...")
        mark_paid(env, pe_id, payment_reference=args.payment_reference)
        print()

    # Step 6: create super payment record
    print("Step 6: create super payment record (pending)...")
    notes = (f"For pay period {period_start} -> {period_end} "
             f"(paid {payment_date}). Goes via Wrkr Pay clearing house.")
    # The super fund member number comes from the employee record. It was
    # previously hardcoded as '123456789' here, which meant a corrected
    # member number on the employee would silently NOT reach the
    # SuperPayment snapshot.
    existing_sp = find_super_payment_for_pay_event(env, employee_id, pe_id)
    if existing_sp:
        sp = existing_sp
        print(f"  Reusing existing super payment {sp['id'][:8]} "
              f"(status={sp['status']})")
    else:
        sp = create_super_payment(
            env, employee_id, [pe_id], payment_date, super_payable,
            fund_name=emp_detail.get("super_fund_name") or "AustralianSuper",
            fund_member=emp_detail.get("super_fund_member_no") or "",
            notes=notes,
        )
    print()

    # Step 7 + 8: build AusSuper CSV + NAB ABA (need accounts app context)
    print("Step 7 + 8: build AusSuper Mapped CSV + NAB ABA...")
    # The employer's ABN is deployment configuration (config.yaml ->
    # stp.entity_abn), not a constant in code.
    employer = load_employer_from_config()
    employer_abn = (employer.get("entity_abn") or "").strip()
    if not employer_abn:
        raise SystemExit("config.yaml stp.entity_abn is not set")
    app = create_app()

    # AusSuper CSV
    csv_text, _pe = build_aussuper_csv(app, pe_id, fund_name="AustralianSuper",
                                       employer_abn=employer_abn)
    csv_name = f"aussuper-mapped-{payment_date.strftime('%d%b%Y').lower()}.csv"
    csv_path = PAYROLL_DIR / csv_name
    csv_path.write_text(csv_text, encoding="utf-8")
    os.chmod(csv_path, 0o600)
    print(f"  AusSuper CSV: {csv_path}  ({csv_path.stat().st_size} bytes)")

    # NAB ABA
    payee_label = "Wrkr Super (clearing house)"
    lodgement_ref = f"JESS SUPER {period_end.strftime('%d%m%y')}".upper()
    aba_bytes, payer, payee = build_nab_aba(
        app, env, payee_label, lodgement_ref, super_payable, payment_date,
    )
    aba_name = f"peristyle-super-{payment_date.strftime('%d%b%Y').lower()}.aba"
    aba_path = PAYROLL_DIR / aba_name
    aba_path.write_bytes(aba_bytes)
    os.chmod(aba_path, 0o600)
    print(f"  NAB ABA:      {aba_path}  ({aba_path.stat().st_size} bytes)")
    print(f"  Payer:        {payer.account_name}  bsb={payer.bsb_plain}  acct={payer.account_number_plain}")
    print(f"  Payee:        {payee.account_name}  bsb={payee.bsb_plain}  acct={payee.account_number_plain}")
    print(f"  Lodgement ref: {lodgement_ref!r}")
    print()

    # Step 9: STP (One Touch Payroll) CSV for this pay run.
    # The ATO portal requires ONE CSV per pay run (all records share the same
    # "Pay or update date"), so we generate exactly the pay run we just paid.
    print("Step 9: build STP (One Touch Payroll) CSV for this pay run...")
    stp_dir = PAYROLL_DIR / "stp"
    stp_dir.mkdir(parents=True, exist_ok=True)
    gen_stp = PPGA_ROOT / "scripts" / "gen_stp_payrun_csvs.py"
    stp_env = dict(os.environ)
    stp_env.update({
        "FROM_DATE": payment_date.isoformat(),
        "TO_DATE": payment_date.isoformat(),
        "OUT_DIR": str(stp_dir),
        "BIZ_EMAIL": env.get("BIZ_EMAIL") or env.get("EMAIL_ADDRESS") or "",
    })
    proc = subprocess.run([sys.executable, str(gen_stp)], env=stp_env,
                          capture_output=True, text=True)
    for line in (proc.stdout or "").strip().splitlines():
        print(f"  {line}")
    if proc.returncode != 0:
        print(f"  !! STP generation FAILED (rc={proc.returncode})")
        print((proc.stderr or "")[:2000])
    stp_files = sorted(stp_dir.glob(f"stp-payrun-{payment_date.isoformat()}.csv"))
    for f in stp_files:
        print(f"  STP CSV:        {f}  ({f.stat().st_size} bytes)")
    print()

    # Step 10: wage ABA -- the NAB Direct Entry file that pays Sample Employee.
    # Downloaded from the accounts API rather than rebuilt locally, so the
    # bytes uploaded to NAB are the same ones the endpoint serves to
    # anyone else (one code path, no drift).
    print("Step 10: build wage ABA (NAB Direct Entry)...")
    wage_aba_path = None
    try:
        # 'Date to be processed' in the ABA header. the operator pays on the
        # Wednesday, and NAB generally needs the processing date to be at
        # least the next business day, so the default is the payment
        # date and --aba-process-date overrides it.
        wage_process_date = (date.fromisoformat(args.aba_process_date)
                             if args.aba_process_date else payment_date)
        wage_aba_bytes = fetch_wage_aba(env, payment_date, payment_date,
                                        process_date=wage_process_date)
        aba_name = f"peristyle-wage-{payment_date.strftime('%d%b%Y').lower()}.aba"
        wage_aba_path = PAYROLL_DIR / aba_name
        wage_aba_path.write_bytes(wage_aba_bytes)
        os.chmod(wage_aba_path, 0o600)
        print(f"  Wage ABA:      {wage_aba_path}  ({wage_aba_path.stat().st_size} bytes)")
    except Exception as exc:  # noqa: BLE001 - report, don't abort the run
        print(f"  !! Wage ABA generation FAILED: {exc}")
    print()

    # Email the upload files to the operator so he can action them (upload the
    # wage ABA to NAB, the super ABA to the clearing house, the AusSuper
    # CSV to AustralianSuper, the STP CSV to the ATO portal).
    emailed = None
    email_to = args.email_files_to or mailclient.require_recipient()
    if email_to:
        emailed = email_generated_files(
            env, email_to,
            files=[f for f in (csv_path, aba_path, wage_aba_path,
                               *(stp_files or [])) if f],
            period=f"{period_start} to {period_end}",
            payment_date=payment_date,
            gross=gross, net=net, super_payable=super_payable,
            paid=(phase == "full"),
        )
        print()

    # Summary
    print(f"=== DONE (phase={phase}) ===")
    final_pe = api(env, "GET", f"/api/payroll/pay-events/{pe_id}")["pay_event"]
    print(f"Pay event:          {pe_id}  (status={final_pe['status']})")
    if args.skip_payslip_email:
        print(f"Payslip email:      NOT SENT (--skip-payslip-email); PDF at {final_pe.get('payslip_pdf_path')}")
    else:
        print("Payslip email:      sent via /send-payslip endpoint (above)")
    print(f"Super payment:      {sp['id']}  (status=pending, awaiting AusSuper upload)")
    print(f"AusSuper CSV:       {csv_path}")
    print(f"NAB ABA file:       {aba_path}")
    print(f"STP CSV:            {stp_files[0] if stp_files else 'NOT GENERATED'}")
    print(f"Wage ABA file:      {wage_aba_path or 'NOT GENERATED'}")
    if emailed:
        print(f"Files emailed to:   {email_to} (email id {emailed})")
    else:
        # The files were still written -- but saying "DONE" without saying
        # they were never emailed is how a week goes missing.
        print("Files emailed to:   NOT EMAILED (mail configuration missing) "
              "-- the files listed above were still written to disk")


if __name__ == "__main__":
    main()