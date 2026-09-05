from app import create_app, db
from app.schema import ensure_schema

app = create_app()

with app.app_context():
    db.create_all()
    ensure_schema()

if __name__ == '__main__':
    app.run(debug=True, port=3000)
