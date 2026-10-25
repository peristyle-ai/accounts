from flask import Flask

from app import _PLACEHOLDER_SECRET_KEYS
from app.models import db


def create_hmi_app(config_path=None):
    import os
    template_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'hmi', 'templates')
    app = Flask(__name__, template_folder=template_dir)

    if config_path:
        import yaml
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
    else:
        import os
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

    # Explicit driver: a bare 'postgresql://' resolves to psycopg (v3) on
    # SQLAlchemy 2.1, which breaks an install that only has psycopg2.
    from sqlalchemy.engine import URL as _SA_URL
    db_url = _SA_URL.create(
        'postgresql+psycopg2',
        username=config['database']['user'],
        host=config['database']['host'],
        port=config['database']['port'],
        database=config['database']['name'],
    ).render_as_string(hide_password=False)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    secret_key = os.environ.get('SECRET_KEY') or config['app'].get('secret_key')
    if not secret_key or secret_key in _PLACEHOLDER_SECRET_KEYS:
        raise RuntimeError(
            'SECRET_KEY environment variable is required. '
            'Set it in .env (or config.yaml) to a real random value. '
            'The placeholder values shipped in config.yaml.example are refused.'
        )
    app.config['SECRET_KEY'] = secret_key
    app.config['UPLOAD_FOLDER'] = config['app'].get('upload_folder', 'uploads')
    app.config['MAX_CONTENT_LENGTH'] = config['app'].get('max_content_length', 10485760)

    db.init_app(app)

    from app.hmi.routes import hmi_bp
    from app.pwa.routes import pwa_bp
    app.register_blueprint(hmi_bp)
    app.register_blueprint(pwa_bp)

    return app
