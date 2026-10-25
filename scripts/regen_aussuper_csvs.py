"""Regenerate the AustralianSuper mapped CSVs for every pay event.

Why this exists
---------------
The AusSuper mapped CSV carries the fund member number in its `Employee ID`
column, and it is read from the employee record at build time. When the
employee's AustralianSuper member number was corrected from the placeholder
`123456789` to the real value, every previously generated CSV was stale --
it still contained the placeholder, and the files already uploaded to
AustralianSuper were lodged against the wrong member.

This script rebuilds the files from the database so they reflect the
current employee record. It is safe to re-run: it overwrites.

Usage:
    python scripts/regen_aussuper_csvs.py
    python scripts/regen_aussuper_csvs.py --out-dir /path/to/dir
    python scripts/regen_aussuper_csvs.py --fy 2026
"""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for line in (ROOT / '.env').read_text().splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, _, v = line.partition('=')
        os.environ.setdefault(k.strip(), v.strip())

DEFAULT_OUT = Path(os.environ.get('PAYROLL_DIR',
                             '/opt/accounts/workspace/payroll'))


def _month_name(dt):
    return dt.strftime('%b').lower()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out-dir', default=str(DEFAULT_OUT))
    ap.add_argument('--employer-abn', default=None,
                    help='defaults to the ABN in config.yaml')
    ap.add_argument('--fy', type=int, default=None,
                    help='also write an FY file for this Australian FY year')
    args = ap.parse_args()

    import yaml
    from app import create_app
    from app.models.pay_event import PayEvent
    from app.models.employee import Employee
    from app.shared.mapped_csv import build_mapped_csv

    # Read config.yaml directly. Do NOT `import config` here -- the repo-root
    # config.py is the app's own YAML loader and importing it by that name
    # can resolve to a different module already in sys.modules.
    cfg = yaml.safe_load((ROOT / 'config.yaml').read_text()) or {}
    employer_abn = (args.employer_abn
                    or (cfg.get('stp') or {}).get('entity_abn')
                    or (cfg.get('employer') or {}).get('abn'))
    if not employer_abn:
        raise SystemExit('No employer ABN in config.yaml; pass --employer-abn')

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = create_app()
    written = []

    with app.app_context():
        events = (PayEvent.query
                  .filter(PayEvent.employee_id.isnot(None))
                  .order_by(PayEvent.payment_date).all())
        if not events:
            raise SystemExit('No pay events found')

        for pe in events:
            emp = Employee.query.get(pe.employee_id)
            if emp is None:
                continue
            name_parts = (emp.legal_name or '').strip().split(maxsplit=1)
            row = {
                'member_number':      emp.super_fund_member_no,
                'family_name':        name_parts[-1] if name_parts else '',
                'given_name':         name_parts[0] if name_parts else '',
                'tfn':                emp.tfn_plain,
                'date_of_birth':      emp.date_of_birth_plain,
                'sex':                emp.sex,
                'address_line1':      emp.address_line1,
                'city':               emp.city,
                'state':              emp.state,
                'postcode':           emp.postcode,
                'phone':              (emp.phone or '').replace(' ', '').replace('-', ''),
                'fund_name':          emp.super_fund_name,
                'pay_period_start':   pe.pay_period_start,
                'pay_period_end':     pe.pay_period_end,
                'transaction_date':   pe.payment_date,
                'payment_reference':  pe.bank_reference or '',
                'sgc_amount':         pe.super_payable_amount,
            }
            csv_text = build_mapped_csv([row], employer_id=employer_abn)
            fname = f'aussuper-mapped-{pe.payment_date.strftime("%d%b%Y").lower()}.csv'
            path = out_dir / fname
            path.write_text(csv_text, encoding='utf-8')
            os.chmod(path, 0o600)
            written.append((fname, row['member_number'], len(csv_text.splitlines()) - 1))
            print(f'  {fname}  member={row["member_number"]}  rows=1')

        if args.fy:
            fy_start, fy_end = _fy_bounds(args.fy)
            fy_events = [pe for pe in events
                         if pe.payment_date and fy_start <= pe.payment_date <= fy_end]
            rows = []
            for pe in fy_events:
                emp = Employee.query.get(pe.employee_id)
                if emp is None:
                    continue
                name_parts = (emp.legal_name or '').strip().split(maxsplit=1)
                rows.append({
                    'member_number':      emp.super_fund_member_no,
                    'family_name':        name_parts[-1] if name_parts else '',
                    'given_name':         name_parts[0] if name_parts else '',
                    'tfn':                emp.tfn_plain,
                    'date_of_birth':      emp.date_of_birth_plain,
                    'sex':                emp.sex,
                    'address_line1':      emp.address_line1,
                    'city':               emp.city,
                    'state':              emp.state,
                    'postcode':           emp.postcode,
                    'phone':              (emp.phone or '').replace(' ', '').replace('-', ''),
                    'fund_name':          emp.super_fund_name,
                    'pay_period_start':   pe.pay_period_start,
                    'pay_period_end':     pe.pay_period_end,
                    'transaction_date':   pe.payment_date,
                    'payment_reference':  pe.bank_reference or '',
                    'sgc_amount':         pe.super_payable_amount,
                })
            if rows:
                csv_text = build_mapped_csv(rows, employer_id=employer_abn)
                # Match the established convention: aussuper-mapped-FY2025-26-Sample Employee.csv
                # where 2025-26 is the Australian financial year ENDING in June 2026.
                fy_label = f'{args.fy - 1}-{str(args.fy)[-2:]}'
                fname = (f'aussuper-mapped-FY{fy_label}-'
                         f'{rows[0].get("given_name") or "employee"}.csv')
                path = out_dir / fname
                path.write_text(csv_text, encoding='utf-8')
                os.chmod(path, 0o600)
                print(f'  {fname}  rows={len(rows)}')

    print(f'\nWrote {len(written)} file(s) to {out_dir}')


def _fy_bounds(fy_year):
    """Australian financial year: 1 July (fy_year-1) to 30 June (fy_year)."""
    import datetime as dt
    return (dt.date(fy_year - 1, 7, 1), dt.date(fy_year, 6, 30))


if __name__ == '__main__':
    main()
