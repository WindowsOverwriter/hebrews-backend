import hashlib
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import bcrypt as _bcrypt
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

load_dotenv()

db = SQLAlchemy()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["5000 per day", "1000 per hour"],
    storage_uri="memory://",
)


REQUIRED_ENV_VARS = ('JWT_SECRET', 'ADMIN_PASSWORD', 'FRONTEND_ORIGIN')


def create_app(testing=False):
    app = Flask(__name__)
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

    if testing:
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        app.config['JWT_SECRET'] = 'test-secret'
        admin_password = 'admin'
        frontend_origin = 'http://localhost:5173'
    else:
        missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
        if missing:
            raise RuntimeError(
                f"Refusing to start: missing required environment variable(s): "
                f"{', '.join(missing)}. Set these in .env, docker-compose.yml, "
                f"or your hosting platform's environment config."
            )

        db_url = os.environ.get('DATABASE_URL', 'sqlite:///dev.db')
        # psycopg (v3) requires the +psycopg dialect suffix for SQLAlchemy
        if db_url.startswith('postgresql://'):
            db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
        app.config['SQLALCHEMY_DATABASE_URI'] = db_url
        app.config['JWT_SECRET'] = os.environ['JWT_SECRET']
        admin_password = os.environ['ADMIN_PASSWORD']
        frontend_origin = os.environ['FRONTEND_ORIGIN']

    # A9: calendar dates (location schedule, auto-delete, confirmation-code
    # prefix) are computed in the business's local zone, not the server's.
    # Render runs in UTC, which is already "tomorrow" in US evenings.
    tz_name = os.environ.get('BUSINESS_TIMEZONE', 'America/Los_Angeles')
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise RuntimeError(
            f"Refusing to start: BUSINESS_TIMEZONE '{tz_name}' is not a valid IANA zone name."
        )
    app.config['BUSINESS_TIMEZONE'] = tz_name

    # A11: a short fingerprint of the admin password is embedded in every
    # issued token, so rotating ADMIN_PASSWORD invalidates outstanding sessions.
    app.config['ADMIN_PASSWORD_FINGERPRINT'] = hashlib.sha256(
        admin_password.encode('utf-8')
    ).hexdigest()[:16]

    # Hash the admin password once at startup for constant-time bcrypt comparison
    app.config['ADMIN_PASSWORD_HASH'] = _bcrypt.hashpw(
        admin_password.encode('utf-8'), _bcrypt.gensalt()
    )
    CORS(app, origins=[frontend_origin])

    # Render (and any reverse proxy) terminates the client connection, so
    # request.remote_addr would be the proxy's IP and every client would share
    # one rate-limit bucket. Trust exactly one X-Forwarded-For hop so the
    # limiter keys on the real client. Also applied in testing so the
    # behavior is covered by tests.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    db.init_app(app)
    limiter.init_app(app)

    from app.routes.public import public_bp
    from app.routes.admin import admin_bp
    app.register_blueprint(public_bp, url_prefix='/api')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')

    return app
