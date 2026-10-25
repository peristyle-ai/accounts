from flask import Flask

def create_pwa_app():
    app = Flask(__name__, template_folder='templates')

    import os

    # No defaults for either of these. A hardcoded DSN points the PWA at
    # whatever Postgres happens to be on localhost, and a literal
    # SECRET_KEY means every deployment shares a known signing key --
    # which is how session cookies get forged.
    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise RuntimeError(
            'DATABASE_URL is not set. The PWA needs it to reach Postgres -- '
            'set it in .env (see .env.example) before starting the app.'
        )
    secret_key = os.environ.get('SECRET_KEY')
    if not secret_key:
        raise RuntimeError(
            'SECRET_KEY is not set. Generate one with: '
            'python3 -c "import secrets; print(secrets.token_hex(32))"'
        )

    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SECRET_KEY'] = secret_key

    from app.models import db
    db.init_app(app)

    from app.pwa.routes import pwa_bp
    app.register_blueprint(pwa_bp)

    return app
