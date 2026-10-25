#!/usr/bin/env python3
"""Generate the Single Touch Payroll (STP) Phase 1 CSVs, one per pay run.

The ATO portal (https://portal.singletouch.com.au) validates each
uploaded file independently and REJECTS a file whose "Pay or update date"
differs between records:

    Inconsistent values found for 'Pay or update date'. This value must
    be the same for every record.

A pay event submission covers ONE pay run, so this script writes one CSV
per pay run rather than one big file. (The earlier single-file export is
what the portal rejected on 2026-10-20.)

Usage:
    source venv/bin/activate && set -a && . ./.env && set +a && \
        python3 scripts/gen_stp_payrun_csvs.py

    # Restrict to a date range / employee
    FROM_DATE=2026-06-01 TO_DATE=2026-06-30 \
    OUT_DIR=/opt/accounts/workspace/payroll/stp \
        python3 scripts/gen_stp_payrun_csvs.py

Environment:
    FROM_DATE   optional YYYY-MM-DD (filter on pay_event.payment_date)
    TO_DATE     optional YYYY-MM-DD (filter on pay_event.payment_date)
    EMPLOYEE_ID optional employee UUID
    STATUS      optional csv, default 'paid,finalized'
    OUT_DIR     output directory, default /tmp/stp-payruns
    BIZ_EMAIL   business user whose pay events to export,
                default payroll@example.com

Output:
    One CSV per pay run, named stp-payrun-YYYY-MM-DD.csv
    Prints a summary of what was written, plus any file that would fail
    the portal's consistency check.
"""
import os
import sys
from datetime import date
from pathlib import Path

# --- bootstrap (same shape as run.py) ----------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

env_path = os.path.join(os.path.dirname(HERE), '.env')
if os.path.isfile(env_path):
    with open(env_path) as _f:
        for line in _f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

from app import create_app
from app.models.user import User
from app.models.employee import Employee
from app.models.pay_event import PayEvent
from app.shared.stp_csv import (
    build_stp_csvs_by_payrun,
    build_stp_row,
    load_employer_from_config,
    payrun_consistency_errors,
)

# The business email selects whose pay events to export AND is written into
# the STP file. There is no default: the old default was
# `payroll@example.com`, a reserved documentation domain, so an unconfigured
# deployment silently exported the wrong user (or nobody) and stamped a
# placeholder address into a file destined for the ATO.
BIZ_EMAIL = os.environ.get('BIZ_EMAIL')
STATUSES = tuple(
    s.strip() for s in os.environ.get('STATUS', 'paid,finalized').split(',') if s.strip()
)
OUT_DIR = Path(os.environ.get('OUT_DIR', '/tmp/stp-payruns'))
FROM_DATE = date.fromisoformat(os.environ['FROM_DATE']) if os.environ.get('FROM_DATE') else None
TO_DATE = date.fromisoformat(os.environ['TO_DATE']) if os.environ.get('TO_DATE') else None
EMPLOYEE_ID = os.environ.get('EMPLOYEE_ID') or None


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = create_app()
    with app.app_context():
        if not BIZ_EMAIL:
            sys.exit('BIZ_EMAIL is required -- it selects the business whose '
                     'pay events are exported and is written into the STP file. '
                     'Set BIZ_EMAIL in the accounts repo .env.')
        biz = User.query.filter_by(email=BIZ_EMAIL).first()
        if biz is None:
            sys.exit(f'Business user {BIZ_EMAIL!r} not found')

        q = PayEvent.query.filter(
            PayEvent.user_id == biz.id,
            PayEvent.status.in_(STATUSES),
        )
        if FROM_DATE:
            q = q.filter(PayEvent.payment_date >= FROM_DATE)
        if TO_DATE:
            q = q.filter(PayEvent.payment_date <= TO_DATE)
        if EMPLOYEE_ID:
            q = q.filter(PayEvent.employee_id == EMPLOYEE_ID)
        q = q.join(Employee, PayEvent.employee_id == Employee.id)
        events = q.order_by(PayEvent.payment_date.asc(), Employee.legal_name.asc()).all()

        if not events:
            print(f'No pay events match status={list(STATUSES)} '
                  f'from_date={FROM_DATE} to_date={TO_DATE}.')
            return 0

        employer = load_employer_from_config()
        rows = []
        skipped = []
        for ev in events:
            emp = Employee.query.get(ev.employee_id)
            if not emp:
                skipped.append((ev.id, 'employee not found'))
                continue
            if not emp.tfn_plain:
                skipped.append((ev.id, f'{emp.legal_name} has no TFN on file'))
                continue
            rows.append(build_stp_row(ev, emp, employer))

        if not rows:
            print('No usable rows (no TFN on file).')
            return 1

        files = build_stp_csvs_by_payrun(rows, employer)
        print(f'Employer: ABN {employer.get("entity_abn")} '
              f'BMS {employer.get("bms_id")} '
              f'Product {employer.get("product_id")}')
        print(f'{len(events)} pay events -> {len(files)} pay run file(s)\n')

        problems = 0
        for f in files:
            d = f['payrun_date']
            name = f'stp-payrun-{d.isoformat()}.csv' if d else 'stp-payrun-unknown.csv'
            path = OUT_DIR / name
            path.write_bytes(f['csv'].encode('utf-8'))
            errors = payrun_consistency_errors(f['csv'])
            status = 'OK' if not errors else 'PORTAL WOULD REJECT'
            if errors:
                problems += 1
            print(f'  {name}: {f["row_count"]} row(s), '
                  f'{len(f["csv"])} bytes - {status}')
            for e in errors:
                print(f'      ! {e}')

        if skipped:
            print('\nSkipped pay events:')
            for ev_id, reason in skipped:
                print(f'  {ev_id}: {reason}')

        print(f'\nWritten to {OUT_DIR}')
        return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
