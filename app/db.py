"""Database setup module.

Phase 1b: extracted from app/models/__init__.py to break the circular
import — app/__init__.py needs `db` to do `db.init_app(app)`, but the
model files (and the original `from . import db` in app/models/__init__.py)
need the models to be importable WITHOUT triggering app/__init__.py
(which imports flask_wtf, which isn't installed in this phase yet).

By moving `db = SQLAlchemy()` + `get_utc_now()` into this module, the
import graph becomes:

    config.py          (no app deps, just yaml + env)
    app/db.py          (flask_sqlalchemy + datetime)
    app/models/*.py    (import db from app.db; no app package init)
    app/__init__.py    (import db from app.db FIRST, then flask_wtf)
    app/api/*.py       (use db + models)

Mirrors the the sibling mail project's app/db.py pattern (commit 6247ca2).

Phase 1d: SQLAlchemy event listener that sets `search_path TO
accounts, public` at the start of every transaction. This is the
safety net for any unqualified table name in SQLAlchemy ORM queries
or raw SQL strings. Mirrors the sibling mail project 6247ca2 (which sets the
search_path inside get_db_connection for raw psycopg2).

NOTE on the event choice: I tried 3 different SQLAlchemy events:
1. event.listen(Engine, 'connect', ...) on the base Engine class
   — did NOT fire (SA 2.x dispatches 'connect' through the dialect
   for the engine instance, not the base class)
2. event.listen(engine, 'checkout', ...) on the engine instance
   — fired on every pool checkout but the search_path got reset by
   the next ROLLBACK (PostgreSQL's ROLLBACK resets session state
   INCLUDING search_path set via plain 'SET' not 'SET LOCAL')
3. event.listen(engine, 'begin', ...) on the engine instance
   — this is the one that works. It fires at the start of every
   transaction (after a commit or rollback, when the next query
   starts a new transaction). So the search_path is set for the
   entire transaction, and persists across ROLLBACK (because the
   'begin' event fires AGAIN for the next transaction).

The 'begin' event passes a SQLAlchemy Connection (not the raw DBAPI
connection). To get a DBAPI cursor, use conn.connection.dbapi_connection.
"""
import weakref
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event

db = SQLAlchemy()


def get_utc_now():
    """Return the current UTC time as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


# Engines that already have the search_path 'begin' listener installed.
# Weak so a discarded engine can still be garbage-collected.
_LISTENED_ENGINES = weakref.WeakSet()


def _set_search_path_on_begin(conn):
    """SQLAlchemy 'begin' event listener: fires at the start of every
    transaction. Sets the search_path to `<ACCOUNTS_SCHEMA>, public`
    so any unqualified table name resolves to the accounts schema
    FIRST (then falls through to public).

    The schema name is interpolated from accounts_config.accounts_schema
    which is set at import time (the singleton reads env vars at import).
    We validate the schema name is a valid PostgreSQL identifier
    (alphanumeric + underscore, max 63 chars) as defense in depth.
    """
    from config import accounts_config
    schema = accounts_config.accounts_schema
    if not schema or schema == 'public':
        # Default behavior; no need to set search_path for the public schema
        return
    if not all(c.isalnum() or c == '_' for c in schema) or len(schema) > 63:
        raise ValueError(f"Invalid schema name for search_path: {schema!r}")
    # The 'begin' event passes a SQLAlchemy Connection. Get the DBAPI
    # connection via .connection.dbapi_connection to get a raw cursor.
    raw_conn = conn.connection.dbapi_connection
    cursor = raw_conn.cursor()
    try:
        # Direct SQL (psycopg v3 needs a real string, not SQLAlchemy's text()).
        # Schema name is validated above so this is safe from SQL injection.
        cursor.execute(f'SET search_path TO "{schema}", public')
    finally:
        cursor.close()


def install_search_path_listener(flask_app=None):
    """Register the search_path event listener on the SQLAlchemy engine.

    Must be called AFTER db.init_app(app) so the engine is created.
    The listener is registered on the engine instance (not the base
    Engine class) because SA 2.x dispatches events through the engine
    instance, not the base class.

    Idempotent: guarded with a module-level flag to be safe.

    For the peristyle-ai install (ACCOUNTS_SCHEMA=accounts), this fires
    on every transaction begin and sets search_path TO accounts, public.
    For the legacy standalone install (ACCOUNTS_SCHEMA=public), the
    listener is a no-op (returns early).
    """
    # Register on the engine instance (not the base Engine class).
    # We get the engine from db.engine (the SQLAlchemy() singleton's engine
    # bound to the most recently init_app'd Flask app).
    engine = db.engine
    # Guard per-ENGINE, not per-process. The old module-level `_installed`
    # flag meant that only the FIRST engine created in a process ever got
    # the listener -- so any second create_app() (the test suite creates
    # one per test) silently lost schema resolution and every unqualified
    # table name failed with "relation does not exist".
    #
    # A WeakSet keeps the engines weakly referenced so a discarded engine
    # can still be garbage-collected.
    if engine in _LISTENED_ENGINES:
        return
    event.listen(engine, 'begin', _set_search_path_on_begin)
    _LISTENED_ENGINES.add(engine)
