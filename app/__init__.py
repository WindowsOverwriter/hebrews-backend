import os
import bcrypt as _bcrypt
from flask import Flask
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

    # Hash the admin password once at startup for constant-time bcrypt comparison
    app.config['ADMIN_PASSWORD_HASH'] = _bcrypt.hashpw(
        admin_password.encode('utf-8'), _bcrypt.gensalt()
    )
    CORS(app, origins=[frontend_origin])

    db.init_app(app)
    limiter.init_app(app)

    from app.routes.public import public_bp
    from app.routes.admin import admin_bp
    app.register_blueprint(public_bp, url_prefix='/api')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')

    return app
