"""Fresh-install regression tests.

Every test here corresponds to a bug that made a FRESH install of this
repository fail. They are cheap (they read files and inspect the ORM
metadata) and they exist so the bugs cannot come back silently.

The bugs, in the order they were found:

1. `schema/init.sql` created the LEGACY fat `users` table (VARCHAR id,
   business columns) instead of the auth table the ORM expects, and
   hardcoded the `accounts.` schema prefix -- so a standalone install
   failed with `schema "accounts" does not exist`.
2. `update_updated_at_column()` was defined AFTER the first trigger that
   uses it, so `CREATE TRIGGER` failed with "function does not exist".
3. `seed_defaults()` ran inside `create_app()`, i.e. BEFORE
   `db.create_all()`, so a fresh install aborted with
   `relation "system_settings" does not exist` -- and `--init-db` could
   never create the tables it was meant to create.
4. The models hardcoded `public.users.id` as the FK target and
   `{'schema': 'accounts'}` on the two split tables, so the ORM and the
   DDL disagreed about where things live.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# --- 1. the canonical DDL must be schema-agnostic --------------------------

class TestInitSqlIsSchemaAgnostic:
    """init.sql must work in whatever schema the install targets."""

    def test_no_hardcoded_accounts_schema_prefix(self):
        """No statement may name `accounts.<table>`.

        The install schema is chosen by ACCOUNTS_SCHEMA and put on the
        search_path by init_db.py; a hardcoded prefix means the DDL only
        works in one specific schema.
        """
        text = _read("schema/init.sql")
        offenders = re.findall(r"accounts\.\w+", text)
        assert not offenders, (
            f"schema/init.sql hardcodes the 'accounts.' schema: {offenders}. "
            "Use unqualified names -- the search_path resolves them."
        )

    def test_migration_files_have_no_hardcoded_schema_prefix(self):
        """The same applies to every migrate_*.sql."""
        offenders = {}
        for path in sorted((REPO_ROOT / "schema").glob("migrate_*.sql")):
            hits = re.findall(r"accounts\.\w+", path.read_text(encoding="utf-8"))
            if hits:
                offenders[path.name] = hits
        assert not offenders, f"migrate_*.sql hardcode the 'accounts.' schema: {offenders}"

    def test_users_table_is_the_auth_shape(self):
        """`users` holds authentication only.

        The business/bank columns live in user_profiles and
        user_payment_settings (the 1:3 split). If init.sql grows the fat
        legacy shape back, the ORM and the DDL drift apart again.
        """
        text = _read("schema/init.sql")
        block = re.search(
            r"CREATE TABLE IF NOT EXISTS users \((.*?)\n\);", text, re.DOTALL
        )
        assert block, "could not find the CREATE TABLE for users"
        body = block.group(1)
        for col in ("id", "email", "password_hash", "display_name",
                    "role", "is_active", "created_at", "updated_at"):
            assert col in body, f"users table is missing the auth column {col!r}"
        for col in ("business_name", "abn", "logo_path", "account_number",
                    "bsb", "de_user_id", "payment_terms"):
            assert col not in body, (
                f"users table still carries the business column {col!r} -- "
                "it belongs in user_profiles / user_payment_settings"
            )

    def test_no_legacy_users_ddl_statements(self):
        """Nothing may ALTER the auth table with business columns."""
        text = _read("schema/init.sql")
        offenders = re.findall(r"ALTER TABLE users[^;]*;", text, re.IGNORECASE)
        assert not offenders, (
            f"schema/init.sql still ALTERs the auth `users` table: {offenders}. "
            "Those columns moved to user_profiles / user_payment_settings."
        )

    def test_update_function_is_defined_before_first_use(self):
        """CREATE TRIGGER requires the function to already exist.

        Matches the STATEMENTS (start of line), not the words -- the
        explanatory comment mentions CREATE TRIGGER by name.
        """
        text = _read("schema/init.sql")
        fn = re.search(
            r"^CREATE OR REPLACE FUNCTION update_updated_at_column",
            text, re.MULTILINE,
        )
        first_trigger = re.search(r"^CREATE TRIGGER", text, re.MULTILINE)
        assert fn, "update_updated_at_column() is not defined at all"
        assert first_trigger, "no CREATE TRIGGER found to check against"
        assert fn.start() < first_trigger.start(), (
            "update_updated_at_column() is defined AFTER the first CREATE "
            "TRIGGER that uses it -- a fresh install fails with "
            "'function update_updated_at_column() does not exist'"
        )


# --- 2. the ORM must agree with the DDL ------------------------------------

class TestOrmIsSchemaAgnostic:
    """The models must derive their schema and FK target from config."""

    def test_models_do_not_hardcode_public_users_fk(self):
        """No model may hardcode `public.users.id` as its FK target.

        Matches the ARGUMENT in any form -- with or without an
        `ondelete=` clause -- so `ForeignKey('public.users.id',
        ondelete='CASCADE')` is caught too.
        """
        offenders = {}
        for path in sorted((REPO_ROOT / "app" / "models").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            hits = re.findall(r"ForeignKey\(\s*'public\.users\.id'", text)
            if hits:
                offenders[path.name] = len(hits)
        assert not offenders, (
            f"models hardcode the FK target 'public.users.id': {offenders}. "
            "Use accounts_config.users_fk so it follows USERS_TABLE."
        )

    def test_split_tables_do_not_hardcode_the_accounts_schema(self):
        """user_profiles / user_payment_settings must follow ACCOUNTS_SCHEMA.

        The check is on the __table_args__ schema ARGUMENT specifically --
        the word 'accounts' appears legitimately in the docstrings.
        """
        for name in ("user_profile.py", "user_payment_settings.py"):
            text = (REPO_ROOT / "app" / "models" / name).read_text(encoding="utf-8")
            m = re.search(r"__table_args__\s*=\s*\{[^}]*\}", text)
            assert m, f"{name} has no __table_args__"
            args = m.group(0)
            assert "'schema': 'accounts'" not in args, (
                f"{name} hardcodes the 'accounts' schema in {args} -- use "
                "accounts_config.accounts_schema"
            )
            assert "accounts_config.accounts_schema" in args, (
                f"{name} must derive its schema from accounts_config: {args}"
            )

    def test_config_exposes_users_schema_and_fk(self):
        """config.py must provide the helpers the models rely on."""
        text = _read("config.py")
        assert "def users_schema" in text, "config.py is missing users_schema"
        assert "def users_fk" in text, "config.py is missing users_fk"

    def test_users_fk_follows_the_users_table(self, monkeypatch):
        """users_fk must track USERS_TABLE."""
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        try:
            from config import AccountsConfig
        finally:
            sys.path.pop(0)

        cfg = AccountsConfig()
        # An external auth table in `public`.
        monkeypatch.setenv("USERS_TABLE", "public.users")
        assert cfg.users_schema == "public"
        assert cfg.users_fk == "public.users.id"
        # An app-owned auth table in a dedicated schema.
        monkeypatch.setenv("USERS_TABLE", "my_schema.users")
        assert cfg.users_schema == "my_schema"
        assert cfg.users_fk == "my_schema.users.id"


# --- 3. seeding must not run before the tables exist -----------------------

class TestSeedOrdering:
    """create_app() seeds system_settings before create_all() runs."""

    def test_seed_defaults_tolerates_a_missing_table(self):
        """seed_defaults must not raise when the table does not exist yet."""
        text = _read("app/models/system_setting.py")
        assert "ProgrammingError" in text and "OperationalError" in text, (
            "seed_defaults() must catch the 'relation does not exist' error, "
            "otherwise a fresh install aborts inside create_app()"
        )

    def test_run_py_seeds_after_creating_tables(self):
        """run.py --init-db must seed once the tables exist."""
        text = _read("run.py")
        create = text.find("db.create_all()")
        seed = text.find("seed_defaults(app)")
        assert create != -1 and seed != -1, "run.py must create tables then seed"
        assert seed > create, "seed_defaults() must run AFTER db.create_all()"


# --- 4. the shipped examples must describe a working install ----------------

class TestShippedExamples:
    """config.yaml.example / .env.example must describe a real install."""

    def test_config_example_defaults_to_a_standalone_install(self):
        """A fresh clone has no external auth table, so the example must
        not point USERS_TABLE at one."""
        text = _read("config.yaml.example")
        assert re.search(r"^\s*#\s*users_table:", text, re.MULTILINE), (
            "config.yaml.example must NOT set users_table -- a fresh install "
            "has no external auth table and init_db.py refuses to continue"
        )
        assert re.search(r"^\s*schema:\s*public", text, re.MULTILINE), (
            "config.yaml.example must default to the 'public' schema"
        )

    def test_env_example_uses_https_for_the_mail_server(self):
        """The mail server is HTTPS-only with a self-signed cert."""
        text = _read(".env.example")
        m = re.search(r"^MAIL_SERVER_API=(.*)$", text, re.MULTILINE)
        assert m, "MAIL_SERVER_API missing from .env.example"
        assert m.group(1).strip().startswith("https://"), (
            "MAIL_SERVER_API must use https:// -- the mail server is "
            "HTTPS-only with a self-signed certificate"
        )

    def test_env_example_documents_the_ca_bundle(self):
        text = _read(".env.example")
        assert "MAIL_SERVER_CA_BUNDLE" in text, (
            ".env.example must document MAIL_SERVER_CA_BUNDLE -- a "
            "self-signed cert fails verification without it"
        )

    def test_env_example_documents_the_schema_settings(self):
        text = _read(".env.example")
        assert "ACCOUNTS_SCHEMA" in text, ".env.example must document ACCOUNTS_SCHEMA"
        assert "USERS_TABLE" in text, ".env.example must document USERS_TABLE"
