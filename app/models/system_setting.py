"""System-wide settings stored in Postgres.

Why this exists (2026-09-17, open-source prep):

    Constants like the business name, the default super fund's
    ABN/USI, and similar were hardcoded in source.
    For a multi-tenant / multi-fund deployment, those need to come from
    configuration rather than source. This module provides a tiny key-value
    table for runtime-tunable settings.

Lookup precedence (highest priority first):

    1. Explicit override passed to `get_setting()` (caller-provided value)
    2. PostgreSQL `system_settings` table (seeded by `seed_defaults()` on
       app boot; can be edited via `set_setting()` from anywhere — API,
       admin UI, cron)
    3. Environment variable `SETTING_<KEY>` (uppercased, dot → underscore)
    4. Hard-coded default passed as `default=` to `get_setting()`

Typical usage:

    from app.models.system_setting import get_setting, set_setting

    # In a route handler:
    fund_name = get_setting('DEFAULT_FUND_NAME')
    fund_abn = get_setting('DEFAULT_FUND_ABN')

    # In a startup hook (seed_defaults runs on app boot if the table is empty):
    set_setting('BUSINESS_NAME', 'Your Business Name')

The table is intentionally simple — no audit trail, no versioning, no
encryption. If you need those, use a separate config store; this is for
runtime-tunable business constants.

Schema:
    key    VARCHAR(100) PRIMARY KEY
    value  TEXT NOT NULL
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
"""

import os

from sqlalchemy import event
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.db import db


class SystemSetting(db.Model):
    __tablename__ = 'system_settings'

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=False)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=db.func.now(),
        onupdate=db.func.now(),
    )

    def __repr__(self):
        return f'<SystemSetting {self.key}={self.value!r}>'


# ---------------------------------------------------------------------------
# Default values seeded into the table on first boot (when the table is empty).
# These match the values that were previously hardcoded in source. Override
# via SETTING_* env vars or via `set_setting()` at runtime.
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    # Business identity (appears on payslips, invoices, SAFF files, and the
    # top-right header block of every PDF the system generates).
    # The user row is the primary source per-tenant; these defaults are the
    # fallback when the user row has an empty address component.
    'BUSINESS_NAME':           'Your Business Name',
    'BUSINESS_ADDRESS_LINE1':  '1 Example Street',
    'BUSINESS_ADDRESS_LINE2':  '',
    'BUSINESS_CITY':           'Example Suburb',
    'BUSINESS_STATE':          'QLD',
    'BUSINESS_POSTCODE':       '4000',
    'BUSINESS_COUNTRY':        'Australia',

    # Which inboxes the payslip sender uses by default.
    # 'work,personal' (both) | 'work' | 'personal'.
    'PAYSLIP_RECIPIENT_MODES': 'work,personal',

    # Default super fund (used as a fallback when an employee record
    # doesn't specify their own fund name). The fund ABN/USI are PUBLIC
    # data — published by the ATO Fund USI/SPIN Lookup.
    # Replace these placeholders with the fund you actually pay into;
    # a real Australian ABN is 11 digits.
    'DEFAULT_FUND_NAME': 'ExampleFund',
    'DEFAULT_FUND_ABN': '8888888888',
    'DEFAULT_FUND_USI': 'ABCD1234',

    # The employer ID your super fund assigns to YOU as a customer.
    # NOT the same as your own ABN, which is what the mapped CSV's
    # 'Employer ID' column carries. Funds' guidance is generally:
    # "we also recommend keeping a record of any employer IDs that are
    # provided by the funds that you're paying into, as these can often
    # be needed when investigating a payment or a contribution issue."
    # Read it off your fund's employer portal.
    'DEFAULT_FUND_EMPLOYER_ID': '123456789',
}


def _env_key(setting_key):
    """Convert a settings key to its env var form.

    Examples:
        BUSINESS_NAME      -> SETTING_BUSINESS_NAME
        DEFAULT_FUND_ABN   -> SETTING_DEFAULT_FUND_ABN
    """
    return 'SETTING_' + setting_key.upper().replace('.', '_').replace('-', '_')


def get_setting(key, default=None):
    """Look up a setting. See module docstring for precedence.

    Returns the explicit override if provided, otherwise checks the
    database, then env var, then the hard-coded default.
    """
    # 1. DB
    row = SystemSetting.query.filter_by(key=key).first()
    if row is not None:
        return row.value
    # 2. Env var
    env_val = os.environ.get(_env_key(key))
    if env_val is not None and env_val != '':
        return env_val
    # 3. Hard-coded default
    return default


def set_setting(key, value):
    """Set a setting (insert or update). Commits immediately.

    Use `get_setting()` with the same key to read it back.
    """
    row = SystemSetting.query.filter_by(key=key).first()
    if row is None:
        row = SystemSetting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    db.session.commit()


def seed_defaults(app=None):
    """Insert any keys from DEFAULT_SETTINGS that don't already exist.

    Safe to call multiple times — it only inserts missing keys, never
    overwrites existing values. Idempotent across app restarts.

    Called from app/__init__.py so a fresh install picks up sensible
    defaults without manual setup.

    TOLERANT OF A MISSING TABLE: create_app() runs this BEFORE
    db.create_all() (run.py calls create_app() first, then
    init_database()). On a genuinely fresh database the table does not
    exist yet, and the query raised UndefinedTable — which aborted the
    whole app creation, so `--init-db` could never create the tables it
    was supposed to. A fresh install now boots: the seeding is skipped
    here and happens on the next start (or the run.py --init-db path
    calls it explicitly after create_all).
    """
    try:
        for key, default in DEFAULT_SETTINGS.items():
            if SystemSetting.query.filter_by(key=key).first() is None:
                db.session.add(SystemSetting(key=key, value=default))
        db.session.commit()
    except (ProgrammingError, OperationalError):
        # Table not created yet (fresh install). Roll back the failed
        # transaction and let the caller create the tables first.
        db.session.rollback()
        print(
            "[system_settings] table not present yet — skipping default "
            "seeding (it runs on the next start, or via "
            "`python run.py --init-db`)."
        )
