"""
Configuration loader for accounts.

Phase 1a (schema-mode install): adds ACCOUNTS_SCHEMA + USERS_TABLE config
plus `t(table)` and `users_t()` helpers so the rest of the codebase can
use fully-qualified table names without hardcoding the schema.

When ACCOUNTS_SCHEMA is unset (default), the DDL installs into the public
schema (legacy standalone-DB install). Tables created in the public schema
are referenced as `public.<table>` (or just `<table>` if the search_path
includes public — the default).

When ACCOUNTS_SCHEMA=accounts (the new install), the DDL installs into
the accounts schema. Tables are referenced as `accounts.<table>`. The
public schema is still in the search_path so cross-schema queries (e.g.
the new public.users auth table) still work.

When USERS_TABLE is set to a fully-qualified table (e.g. 'public.users'),
init_db.py does NOT create a `users` table inside the accounts schema
(the auth source is external). The app's User model composition (Phase 1b)
will read from this external table.

Pattern modelled on the the sibling mail project be631f2 refactor.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
    PROJECT_ROOT = Path(__file__).parent.parent
    ENV_FILE = PROJECT_ROOT / '.env'
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
except ImportError:
    pass  # python-dotenv not installed; env vars must be set manually

PROJECT_ROOT = Path(__file__).parent.parent
# config.yaml sits NEXT TO THIS FILE, not one level up. The
# `Path(__file__).parent.parent` arithmetic below is legacy (config.py
# used to live in a subdirectory); it pointed at /opt/accounts/config.yaml
# which does not exist, so config.yaml was silently NEVER LOADED and
# AccountsConfig._config was always empty. Fixed 2026-10-20.
# NOTE: PROJECT_ROOT is deliberately left alone — it is only used for
# ENV_FILE, and run.py already calls load_dotenv() from the service
# WorkingDirectory (the repo root), so the repo .env is loaded there.
REPO_ROOT = Path(__file__).parent
DEFAULT_CONFIG_PATH = REPO_ROOT / 'config.yaml'


class AccountsConfig:
    """
    Configuration manager for accounts.

    Loads settings from config.yaml with env-var overrides. Exposes
    schema-mode helpers (`accounts_schema`, `users_table`, `t()`, `users_t()`)
    so the rest of the code can use fully-qualified table names without
    hardcoding the schema.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._config: Dict[str, Any] = {}
        self._load_config()

    def _load_config(self):
        if self.config_path.exists():
            try:
                import yaml
                with open(self.config_path, 'r') as f:
                    self._config = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"Warning: Could not load config from {self.config_path}: {e}")
                self._config = {}
        else:
            self._config = {}

    def _get_nested(self, *keys: str, default: Any = None) -> Any:
        value = self._config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default
        return value

    def get(self, *keys: str, default: Any = None) -> Any:
        """Read a nested config value by key path.

        Usage: accounts_config.get('stp', 'bms_id', default='')
        Returns `default` when any level of the path is missing.
        """
        return self._get_nested(*keys, default=default)

    # ---- Phase 1a: schema-mode config ----

    @property
    def accounts_schema(self) -> str:
        """PostgreSQL schema where the accounts tables live.

        Default: 'public' (legacy standalone-DB install).
        Recommended for the new peristyle-ai install: 'accounts' (cohabits
        with other peristyle-ai apps in the peristyle DB).

        All accounts tables (expenses, invoices, customers, employees, etc.)
        live in this schema. Tables are referenced as fully-qualified
        <ACCOUNTS_SCHEMA>.<table> throughout the code.
        """
        return os.getenv('ACCOUNTS_SCHEMA', self._get_nested('database', 'schema', default='public'))

    @property
    def users_table(self) -> str:
        """Fully-qualified name of the users table for auth.

        Default: <ACCOUNTS_SCHEMA>.users (the app owns its own users table).

        Opt-in override for the new peristyle-ai install:
        'public.users' (the new peristyle-ai shared auth table created in
        Phase 0; matches the harness project's spec convention).

        When set to an external table, init_db.py does NOT create a `users`
        table inside the accounts schema. The app's User model composition
        (Phase 1b) reads from this external table.
        """
        # config.yaml's database.users_table is honoured when the env var
        # is absent. Previously the config value was read by nobody and
        # the fallback always won, so a non-env context silently pointed
        # at <schema>.users instead of the real auth table.
        return os.getenv(
            'USERS_TABLE',
            self._get_nested('database', 'users_table',
                             default=f"{self.accounts_schema}.users"),
        )

    @property
    def users_schema(self) -> str:
        """PostgreSQL schema that holds the auth (`users`) table.

        Derived from USERS_TABLE so the ORM and the DDL always agree on
        where the auth table lives. Examples:
            'public.users'  -> 'public'   (platform auth table)
            'accounts.users'-> 'accounts' (app owns its own auth table)
            'users'         -> ACCOUNTS_SCHEMA (unqualified)
        """
        table = self.users_table
        return table.split('.', 1)[0] if '.' in table else self.accounts_schema

    @property
    def users_fk(self) -> str:
        """Fully-qualified FK target for the auth table: '<schema>.users.id'.

        Every model that hangs off the business owner points here, so the
        FK follows USERS_TABLE instead of being hardcoded.
        """
        return f"{self.users_schema}.users.id"

    def t(self, table: str) -> str:
        """Return the fully-qualified name of an accounts table.

        Usage: cursor.execute("SELECT * FROM " + cfg.t("expenses"))
        """
        return f"{self.accounts_schema}.{table}"

    def users_t(self) -> str:
        """Return the fully-qualified name of the users table (auth).

        Equivalent to cfg.t("users") when USERS_TABLE is unset; returns
        the external USERS_TABLE value when set.
        """
        return self.users_table


# Module-level singleton for convenient access
accounts_config = AccountsConfig()
