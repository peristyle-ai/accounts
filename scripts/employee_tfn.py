#!/usr/bin/env python3
"""Print an employee's TFN (decrypted) to stdout.

Why this exists (2026-10-23): the legacy one-off payslip generators under
the payroll workspace used to carry the employee's TFN as a plaintext literal
in a config file and in four copies inside the scripts themselves. That
meant a live TFN sat in plaintext, unversioned, in a tree that the harness
.gitignore excludes -- one typo away from a public commit.

The TFN already lives encrypted at rest in accounts.employees.tfn (Fernet,
key in the accounts .env). This helper reads it and decrypts it on demand,
so no plaintext TFN needs to be written anywhere.

Usage:
    .venv/bin/python3 scripts/employee_tfn.py --employee-id <uuid>
    .venv/bin/python3 scripts/employee_tfn.py --name "Sample Employee"

Must be run with the accounts venv interpreter (it needs `cryptography`
and `psycopg2`). Decryption happens in this process; the plaintext is
written to stdout and nowhere else.
"""

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / '.env')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--employee-id', help='employee UUID')
    ap.add_argument('--name', help='employee legal or preferred name (substring)')
    ap.add_argument('--masked', action='store_true',
                    help='print the masked form instead of the full TFN')
    args = ap.parse_args()

    if not args.employee_id and not args.name:
        ap.error('need --employee-id or --name')

    # The app factory needs the full app context (it wires up SQLAlchemy).
    from app import create_app
    from app.models.employee import Employee

    app = create_app()
    with app.app_context():
        q = Employee.query
        if args.employee_id:
            emp = q.filter_by(id=args.employee_id).first()
        else:
            emp = q.filter(
                db_or_like(Employee.legal_name, args.name)
            ).first()
        if emp is None:
            print(f"No employee matched {args.employee_id or args.name}", file=sys.stderr)
            return 1
        if not emp._tfn:
            print(f"Employee {emp.legal_name} has no TFN on file", file=sys.stderr)
            return 2
        if args.masked:
            print(emp.tfn_masked)
        else:
            print(emp.tfn_plain)
    return 0


def db_or_like(column, value):
    """Case-insensitive LIKE on a column (SQLAlchemy `ilike`)."""
    return column.ilike(f'%{value}%')


if __name__ == '__main__':
    raise SystemExit(main())
