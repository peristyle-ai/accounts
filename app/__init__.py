from flask import Flask
# NOTE: flask_wtf.csrf.CSRFProtect is imported INSIDE create_app() because
# flask_wtf isn't installed in dev environments. The import is only needed
# when the app is actually created (at runtime), not at module load time.
import yaml
import os

from app.db import db
from config import accounts_config  # Phase 1a: schema-mode config

# Placeholder SECRET_KEY values that are refused at startup. These are the
# values shipped in config.yaml.example (and the historical dev default) --
# accepting them would mean a fresh install silently runs with a secret that
# is published in the repo.
_PLACEHOLDER_SECRET_KEYS = frozenset({
    'dev-secret-key',
    'change-me-generate-a-random-32-byte-hex-key',
    'change-me-in-production-use-strong-random-key',
})


def create_app(config_path=None):
    template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'app', 'hmi', 'templates')
    app = Flask(__name__, template_folder=template_dir)

    if config_path and os.path.exists(config_path):
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
    else:
        config = {
            'database': {
                'host': os.environ.get('DB_HOST', 'localhost'),
                'port': os.environ.get('DB_PORT', '5432'),
                'name': os.environ.get('DB_NAME', 'accounts'),
                'user': os.environ.get('DB_USER', 'postgres')
            },
            'app': {
                'secret_key': os.environ.get('SECRET_KEY'),
                'upload_folder': os.environ.get('UPLOAD_FOLDER', 'uploads'),
                'max_content_length': 10485760
            }
        }

    # Build the DSN with an EXPLICIT driver. A bare 'postgresql://' URL is
    # ambiguous: SQLAlchemy 2.1 resolves it to psycopg (v3), so an install
    # that only has psycopg2-binary fails with a bare
    # "No module named 'psycopg'". Naming the driver keeps it working
    # whatever SQLAlchemy version is installed. URL.create() also escapes
    # passwords containing '@', ':' or '/'.
    from sqlalchemy.engine import URL as _SA_URL
    _db = config.get('database') or {}
    if 'password' in _db:
        db_url = _SA_URL.create(
            'postgresql+psycopg2',
            username=_db.get('user'),
            password=_db.get('password'),
            host=_db.get('host'),
            port=_db.get('port'),
            database=_db.get('name'),
        ).render_as_string(hide_password=False)
    else:
        db_url = _SA_URL.create(
            'postgresql+psycopg2',
            username=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', ''),
            host=os.environ.get('DB_HOST', 'localhost'),
            port=os.environ.get('DB_PORT', '5432'),
            database=os.environ.get('DB_NAME', 'accounts'),
        ).render_as_string(hide_password=False)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    # SECRET_KEY: no default — fail to start without it.
    # The environment wins over config.yaml, so a real deployment can keep
    # the secret in .env (mode 600, git-ignored) and never in a file that
    # might be committed. config.yaml is only the fallback for installs that
    # do not use .env.
    secret_key = os.environ.get('SECRET_KEY') or config['app'].get('secret_key')
    if not secret_key or secret_key in _PLACEHOLDER_SECRET_KEYS:
        raise RuntimeError(
            'SECRET_KEY environment variable is required. '
            'Set it in .env (or config.yaml) to a real random value. '
            'The placeholder values shipped in config.yaml.example are refused.'
        )
    app.config['SECRET_KEY'] = secret_key

    app.config['UPLOAD_FOLDER'] = config['app'].get('upload_folder', 'uploads')
    # Git-tracked seed logo used when a business's uploaded logo file has
    # gone missing (see app/shared/pdf.py::_resolve_logo_path).
    app.config['LOGO_FALLBACK_PATH'] = config['app'].get('logo_fallback_path')
    app.config['MAX_CONTENT_LENGTH'] = config['app'].get('max_content_length', 10485760)

    # Session cookie security
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'True').lower() == 'true'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours

    # ---- Phase 1a: schema-mode config (mirrors the sibling mail project be631f2) ----
    # The DDL installs into ACCOUNTS_SCHEMA (default 'public' for legacy
    # standalone, 'accounts' for the peristyle-ai install). The runtime
    # uses search_path on the connection (set by Phase 1d in app/db.py)
    # so unqualified table names still work. These two values are exposed
    # here so the rest of the app can read them via app.config.
    app.config['ACCOUNTS_SCHEMA'] = accounts_config.accounts_schema
    app.config['USERS_TABLE'] = accounts_config.users_table
    # The t() and users_t() helpers are used by Phase 1c (the model FK
    # + raw SQL refactor) for fully-qualified table references.
    app.config['TABLE_PREFIX'] = f"{accounts_config.accounts_schema}."

    # CSRF protection needs flask_wtf; defer the import to runtime
    from flask_wtf.csrf import CSRFProtect
    db.init_app(app)
    # Phase 1d: install the search_path event listener on the engine
    # so every connection sets search_path TO <ACCOUNTS_SCHEMA>, public
    # (the safety net for any unqualified table names in SQLAlchemy
    # ORM queries or raw SQL strings)
    # NOTE: db.engine requires current_app, so we push an app context
    # to register the listener (the listener will fire on the engine
    # for the lifetime of the Flask app)
    from app.db import install_search_path_listener
    with app.app_context():
        install_search_path_listener()

    from app.api.routes import api_bp
    from app.api.reports import reports_bp
    from app.api.payroll import payroll_api_bp
    from app.api.bas import bas_bp
    from app.api.bank_transactions import bank_txn_bp
    from app.api.invoice_reminders import invoice_reminders_bp
    from app.api.payment_reconciliations import payment_recon_bp
    from app.api.xero_export import xero_export_bp
    from app.api.payroll_exports import payroll_exports_bp
    from app.api.stp_exports import stp_exports_bp
    from app.api.settings import settings_bp
    from app.hmi.routes import hmi_bp
    from app.hmi.payroll import payroll_hmi_bp
    from app.pwa.routes import pwa_bp

    # CSRF protection for HMI/PWA forms (cookie-based auth)
    # API blueprints are exempt — they use X-API-Key header auth, not cookies
    csrf = CSRFProtect(app)
    for bp in (api_bp, reports_bp, payroll_api_bp, bas_bp, bank_txn_bp,
               invoice_reminders_bp, payment_recon_bp, xero_export_bp,
               payroll_exports_bp, stp_exports_bp, settings_bp):
        csrf.exempt(bp)

    app.register_blueprint(api_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(payroll_api_bp)
    app.register_blueprint(bas_bp)
    app.register_blueprint(bank_txn_bp)
    app.register_blueprint(invoice_reminders_bp)
    app.register_blueprint(payment_recon_bp)
    app.register_blueprint(xero_export_bp)
    app.register_blueprint(payroll_exports_bp)
    app.register_blueprint(stp_exports_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(hmi_bp)
    app.register_blueprint(payroll_hmi_bp)
    app.register_blueprint(pwa_bp)

    @app.route('/health')
    def health():
        from datetime import datetime, timezone
        return {'status': 'ok', 'timestamp': datetime.now(timezone.utc).isoformat()}

    @app.route('/uploads/<path:filename>')
    def serve_upload(filename):
        from flask import send_from_directory, session, abort
        from app.shared.decorators import _resolve_current_user
        from app.shared.upload_access import resolve_upload_owner
        # Require authentication — receipts and logos contain sensitive data.
        # Accepts both session cookie (HMI <img> tags) and X-API-Key header.
        user = _resolve_current_user()
        if not user:
            abort(401)
        # Tenant scoping: the file must belong to the caller. Ownership is
        # resolved from the data (payslips/<user_id>/…, or the stored path
        # column of the owning record). If ownership cannot be established
        # the file is refused — "who owns this?" with no answer means no.
        # (2026-10-25 security sweep S8: authentication only, no tenant
        # check, so any account could read any other account's payslips.)
        owner_id = resolve_upload_owner(filename)
        if owner_id is None or int(owner_id) != int(user.id):
            abort(404)
        upload_folder = app.config.get('UPLOAD_FOLDER', 'uploads')
        return send_from_directory(os.path.abspath(upload_folder), filename)

    # Seed the system_settings table with default business constants
    # (BUSINESS_NAME, DEFAULT_FUND_ABN, etc.) on first boot. Idempotent —
    # only inserts missing keys, never overwrites values an admin has
    # already set via PUT /api/settings/<key>.
    with app.app_context():
        from app.models.system_setting import seed_defaults
        seed_defaults(app)

    return app
