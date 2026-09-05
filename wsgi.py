from app import create_app, db
from app.schema import ensure_schema

application = create_app()

with application.app_context():
    db.create_all()
    ensure_schema()

if __name__ == '__main__':
    application.run(debug=True, port=3000)
