#!/usr/bin/env python3
"""
Database init script for accounts.

Mode-aware install (Phase 1a, mirrors the sibling mail project a6f0d79):
  --mode=schema     Install as a PostgreSQL schema in an existing DB.
                    Default ACCOUNTS_SCHEMA=accounts. The schema is created
                    if it doesn't exist. Tables are installed into the schema.
                    USE THIS FOR THE PERISTYLE-AI INSTALL.

  --mode=standalone Create tables in the DB public schema. Default
                    ACCOUNTS_SCHEMA=public. The DB itself must already
                    exist (e.g. via `createdb accounts`).
                    USE THIS FOR THE LEGACY STANDALONE INSTALL.

Environment:
  DATABASE_URL    Postgres connection URL (required)
  ACCOUNTS_SCHEMA Schema to install into (default: public for --mode=standalone,
                  accounts for --mode=schema)
  USERS_TABLE     Fully-qualified users table name (default: <ACCOUNTS_SCHEMA>.users)
                  Opt-in override for the peristyle-ai install:
                    USERS_TABLE=public.users (the new shared auth table)
                  When set to an external table, init_db.py does NOT create
                  a `users` table inside the accounts schema.

Examples:
  # peristyle-ai install (Phase 0 + 1a: schema mode, external auth)
  DATABASE_URL=postgresql+psycopg2://peristyle@127.0.0.1:5432/peristyle \\
      ACCOUNTS_SCHEMA=accounts \\
      USERS_TABLE=public.users \\
      python3 init_db.py --mode=schema

  # Legacy standalone install
  DATABASE_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/accounts \\
      python3 init_db.py --mode=standalone
"""

import os
import sys
import glob
import psycopg2
from dotenv import load_dotenv
from config import accounts_config  # Phase 1a: schema-mode config

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL')


def _psycopg2_dsn(url):
    """Normalise a SQLAlchemy-style DSN into one psycopg2 understands.

    SQLAlchemy URLs carry the driver in the scheme
    (postgresql+psycopg2://...), but psycopg2's own parser rejects the
    '+driver' part with 'missing "=" after ... in connection info string'.
    Strip it. The driver is chosen by the caller anyway -- this function
    only speaks psycopg2.
    """
    if not url or '://' not in url:
        return url
    scheme, rest = url.split('://', 1)
    if '+' in scheme:
        scheme = scheme.split('+', 1)[0]
        return f'{scheme}://{rest}'
    return url


def _parse_args():
    """Minimal argv parser. --mode=schema (default) | --mode=standalone."""
    mode = "schema"  # default for the peristyle-ai install
    for arg in sys.argv[1:]:
        if arg == "--mode=schema":
            mode = "schema"
        elif arg == "--mode=standalone":
            mode = "standalone"
        elif arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        else:
            print(
                f"Unknown argument: {arg!r}. Use --mode=schema (default) "
                f"or --mode=standalone.",
                file=sys.stderr,
            )
            sys.exit(2)
    return mode


def _assert_external_users_table(cursor, users_table: str, accounts_schema: str):
    """Phase 1a: assert the external USERS_TABLE exists with the required columns.

    When USERS_TABLE is set to an external table (e.g. 'public.users'),
    init_db.py does NOT create a `users` table inside the accounts schema.
    The app's User model composition (Phase 1b) reads from this external
    table. We sanity-check that it exists with the right columns.

    Required columns: id (SERIAL or BIGSERIAL), email, password_hash.
    """
    if users_table == f"{accounts_schema}.users":
        # default — no external table check needed
        return
    print(f"[1a] USERS_TABLE is set to {users_table!r} (external).")
    print(f"[1a] init_db.py will NOT create a `users` table inside {accounts_schema!r}.")
    print(f"[1a] The owner of {users_table!r} must create it first.")
    try:
        schema_name, table_name = users_table.split(".", 1)
    except ValueError:
        print(
            f"[1a] ERROR: USERS_TABLE={users_table!r} is not fully-qualified "
            f"(expected 'schema.table').",
            file=sys.stderr,
        )
        sys.exit(3)
    cursor.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema_name, table_name),
    )
    columns = {row[0] for row in cursor.fetchall()}
    if not columns:
        print(
            f"[1a] ERROR: external users table {users_table!r} does not exist.",
            file=sys.stderr,
        )
        sys.exit(4)
    required = {"id", "email", "password_hash"}
    missing = required - columns
    if missing:
        print(
            f"[1a] ERROR: external users table {users_table!r} is missing "
            f"required columns: {sorted(missing)}",
            file=sys.stderr,
        )
        sys.exit(5)
    print(f"[1a] OK: external users table {users_table!r} has the required columns.")


def _ensure_schema(cursor, schema: str):
    """Create the schema if it doesn't exist (schema mode only)."""
    if schema == "public":
        # public schema is always there
        return
    cursor.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    print(f"[1a] ensured schema {schema!r} exists")


def _set_search_path(cursor, schema: str):
    """Set the search_path to <schema>, public for this session.

    This makes unqualified table names in the DDL files (CREATE TABLE
    without a schema prefix) install into the configured schema. The
    public schema is kept in the search_path so cross-schema queries
    (e.g. USERS_TABLE=public.users) still work.
    """
    cursor.execute(f'SET search_path TO "{schema}", public')
    print(f"[1a] set search_path TO {schema}, public")


def _execute_sql_file(cursor, path: str, skip_users: bool = False):
    """Execute a single SQL file. Idempotent (uses IF NOT EXISTS throughout).

    If skip_users=True, strip out the CREATE TABLE for the `users` table
    (when USERS_TABLE is set to an external table, we don't want to
    create a `users` table inside the accounts schema).
    """
    print(f"[1a] applying {path}")
    with open(path, 'r') as f:
        sql = f.read()
    if skip_users:
        # Strip the CREATE TABLE for `users` (and its indexes). All
        # dependent tables (expenses, invoices, etc.) that reference
        # users(id) are left in place; they'll get the external USERS_TABLE
        # via the FK after Phase 1c (when the model FKs get repointed).
        import re
        sql = re.sub(
            r'CREATE TABLE(?:\s+IF NOT EXISTS)?\s+users\s*\([^;]*\);',
            '-- CREATE TABLE users: skipped (USERS_TABLE is external)',
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        sql = re.sub(
            r'CREATE (?:UNIQUE )?INDEX(?:\s+IF NOT EXISTS)?\s+\S+\s+ON\s+users\s*\([^;]*\);',
            '-- CREATE INDEX on users: skipped (USERS_TABLE is external)',
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
    cursor.execute(sql)


def init_db(mode: str = "schema"):
    """Initialise the accounts database.

    Reads ACCOUNTS_SCHEMA + USERS_TABLE from the config (env-overridable),
    sets the search_path, and applies all SQL files in schema/ in
    alphabetical order.
    """
    if not DATABASE_URL:
        print(
            "ERROR: DATABASE_URL environment variable is not set. Set it in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    accounts_schema = accounts_config.accounts_schema
    users_table = accounts_config.users_table
    skip_users = users_table != f"{accounts_schema}.users"

    print(f"[1a] mode = {mode}")
    print(f"[1a] ACCOUNTS_SCHEMA = {accounts_schema!r}")
    print(f"[1a] USERS_TABLE = {users_table!r}")
    if skip_users:
        print(f"[1a] USERS_TABLE is external; will NOT create a `users` table in {accounts_schema!r}")

    conn = psycopg2.connect(_psycopg2_dsn(DATABASE_URL))
    conn.autocommit = False
    try:
        with conn.cursor() as cursor:
            if mode == "schema":
                _ensure_schema(cursor, accounts_schema)
            _set_search_path(cursor, accounts_schema)
            _assert_external_users_table(cursor, users_table, accounts_schema)

            # Apply schema/*.sql in alphabetical order
            schema_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                'schema',
            )
            sql_files = sorted(glob.glob(os.path.join(schema_dir, '*.sql')))
            if not sql_files:
                print(
                    f"[1a] WARNING: no SQL files found in {schema_dir!r}",
                    file=sys.stderr,
                )
            for sql_file in sql_files:
                _execute_sql_file(cursor, sql_file, skip_users=skip_users)
        conn.commit()
        print(f"[1a] OK: schema applied to {accounts_schema!r}")
    except Exception as e:
        conn.rollback()
        print(f"[1a] ERROR: migration failed: {e}", file=sys.stderr)
        sys.exit(10)
    finally:
        conn.close()


if __name__ == '__main__':
    mode = _parse_args()
    init_db(mode)
