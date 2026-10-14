"""Tests for app/schema.py (post-create_all reconciliation)."""
from sqlalchemy import inspect, text

from app import db as _db
from app.models import OrderItem
from app.schema import ensure_schema


# The orders table exactly as create_all() emitted it before location_id was
# added -- i.e. what the live database looks like on first boot after deploy.
_ORDERS_BEFORE_LOCATION_ID = """
CREATE TABLE orders (
    id INTEGER NOT NULL PRIMARY KEY,
    order_number INTEGER NOT NULL,
    confirmation_code VARCHAR(12) NOT NULL UNIQUE,
    customer_name VARCHAR(100) NOT NULL,
    phone_number VARCHAR(20) NOT NULL,
    pickup_slot VARCHAR(20) NOT NULL,
    status VARCHAR(20),
    period_id INTEGER NOT NULL REFERENCES active_periods (id),
    created_at DATETIME,
    CONSTRAINT uq_order_number_period UNIQUE (order_number, period_id)
)
"""


def _order_columns():
    return {c['name'] for c in inspect(_db.engine).get_columns('orders')}


def _rebuild_orders_without_location_id():
    """SQLite cannot DROP COLUMN a column named in a table-level FOREIGN KEY
    (which is how create_all() emits orders.location_id), so recreate the
    table from the pre-feature DDL instead. order_items references orders
    and must go first; it is recreated from the model afterwards."""
    _db.session.execute(text('DROP TABLE order_items'))
    _db.session.execute(text('DROP TABLE orders'))
    _db.session.execute(text(_ORDERS_BEFORE_LOCATION_ID))
    _db.session.commit()
    OrderItem.__table__.create(_db.engine)


def test_ensure_schema_adds_missing_location_id_column(app, client):
    with app.app_context():
        _rebuild_orders_without_location_id()
        assert 'location_id' not in _order_columns()

        assert ensure_schema() == ['location_id']
        assert 'location_id' in _order_columns()

        # Idempotent: nothing left to add on the second pass.
        assert ensure_schema() == []

    # The column added by ALTER TABLE is usable end to end.
    response = client.post('/api/orders', json={
        'customer_name': 'Migrated', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'location_id': 1, 'items': [{'drink_id': 1}],
    })
    assert response.status_code == 201
    code = response.get_json()['confirmation_code']
    assert client.get(f'/api/orders/{code}').get_json()['location_name'] == 'Main Street Pop-up'


def test_ensure_schema_is_noop_when_column_present(app):
    with app.app_context():
        assert 'location_id' in _order_columns()
        assert ensure_schema() == []
