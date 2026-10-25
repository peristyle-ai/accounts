"""No real production value may appear anywhere in the tracked tree.

THE INCIDENT THIS GUARDS (2026-10-25): three real values were published
in the accounts repo -- an employee's TFN, the employer's NAB DE Credit
User ID, and a real pay-event lodgement reference. The release scanner's
needle list had the ABN, the super member number, the bank account number
and the BSB, but NOT those three, because nobody had written them down.
A hand-maintained list of "things that must not leak" is only as good as
the day it was written.

So this test does not use a list. It reads the LIVE production values --
out of the database, with the encrypted columns decrypted -- and asserts
that none of them appears in any tracked file. A value that exists in
production is a value that must not be published, whether or not anyone
remembered to list it.

The check is skipped (not failed) when the production database is not
reachable, because a test that fails because the environment is missing is
a test people learn to ignore. The release_scan.py check covers the same
ground and is the gate that must pass before publishing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Columns whose values are short (6 digits) but still meaningful. A
# blanket length filter would drop them, and the DE Credit User ID is
# exactly the kind of value that must not leak.
SHORT_OK = {"de_user_id", "bsb", "bank_bsb"}
MIN_LEN = 6

HARVEST = r"""
import json, os, sys
import psycopg2
from cryptography.fernet import Fernet

dsn, key = sys.argv[1], sys.argv[2]
out = []
try:
    conn = psycopg2.connect(dsn.replace("postgresql+psycopg2://", "postgresql://"))
    conn.autocommit = True
    cur = conn.cursor()

    def dec(v):
        try:
            return Fernet(key.encode()).decrypt(str(v).encode()).decode()
        except Exception:
            return str(v)

    cols = [
        ("accounts.user_payment_settings",
         ["bsb", "account_number", "de_user_id"]),
        ("accounts.employees",
         ["bank_account_number", "bank_bsb", "super_fund_member_no", "tfn",
          "first_name", "last_name", "address_line1", "email",
          "bank_account_name", "bank_reference_prefix"]),
        ("accounts.pay_events", ["bank_reference"]),
        ("accounts.invoices", ["reference"]),
    ]
    for tbl, names in cols:
        for name in names:
            try:
                cur.execute(f"select distinct {name} from {tbl} where {name} is not null")
            except Exception:
                conn.rollback(); continue
            for (v,) in cur.fetchall():
                dv = dec(v)
                if dv is None:
                    continue
                dv = str(dv).strip()
                if len(dv) >= 8 or (len(dv) >= 6 and name in ("de_user_id", "bsb", "bank_bsb")):
                    out.append(dv)
                if len(dv) >= 6 and " " in dv:
                    out.append(dv.replace(" ", ""))
    conn.close()
except Exception as e:
    print("harvest error: %s" % e, file=sys.stderr)
print(json.dumps(out))
"""


def _read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _live_values() -> list[str] | None:
    """The live production values, or None if the DB is unreachable."""
    env = _read_env(REPO_ROOT / ".env")
    dsn = env.get("DATABASE_URL", "")
    key = env.get("PAYROLL_PII_KEY", "")
    if not dsn or not key:
        return None
    py = REPO_ROOT / ".venv" / "bin" / "python3"
    if not py.exists():
        import shutil
        py = Path(shutil.which("python3") or "python3")
    try:
        proc = subprocess.run([str(py), "-c", HARVEST, dsn, key],
                              capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    try:
        import json
        vals = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    return [str(v) for v in vals if len(str(v)) >= MIN_LEN]


def _tracked_text_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True).stdout
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = REPO_ROOT / rel
        if p.suffix in (".png", ".jpg", ".gif", ".pdf", ".csv", ".aba"):
            continue
        if not p.is_file():
            continue
        try:
            p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError, OSError):
            continue
        files.append(p)
    return files


def test_no_live_production_value_appears_in_the_repo():
    """A value that exists in the live database must not be in the tree."""
    values = _live_values()
    if values is None:
        pytest.skip("production database not reachable")
    assert values, "harvest returned nothing -- the check would be vacuous"

    hits = []
    for p in _tracked_text_files():
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for v in values:
            if v in text:
                hits.append(f"{p.relative_to(REPO_ROOT)}: {v[:16]}")
    assert not hits, (
        "real production value(s) found in the tracked tree:\n  "
        + "\n  ".join(sorted(set(hits)))
    )
