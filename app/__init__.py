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


def create_app(testing=False):
    app = Flask(__name__)

    db_url = os.environ.get('DATABASE_URL', 'sqlite:///dev.db')
    # psycopg (v3) requires the +psycopg dialect suffix for SQLAlchemy
    if db_url.startswith('postgresql://'):
        db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['JWT_SECRET'] = os.environ.get('JWT_SECRET', 'dev-secret-change-me')

    # Hash the admin password once at startup for constant-time bcrypt comparison
    admin_password = os.environ.get('ADMIN_PASSWORD', 'admin')
    app.config['ADMIN_PASSWORD_HASH'] = _bcrypt.hashpw(
        admin_password.encode('utf-8'), _bcrypt.gensalt()
    )

    if testing:
        app.config['TESTING'] = True
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'

    frontend_origin = os.environ.get(
        'FRONTEND_ORIGIN', 'http://localhost:5173'
    )
    CORS(app, origins=[frontend_origin])

    db.init_app(app)
    limiter.init_app(app)

    from app.routes.public import public_bp
    from app.routes.admin import admin_bp
    app.register_blueprint(public_bp, url_prefix='/api')
    app.register_blueprint(admin_bp, url_prefix='/api/admin')

    return app
