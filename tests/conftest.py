import pytest
from app import create_app, db as _db
from app.models import Drink, CustomizationOption, Setting, ActivePeriod


@pytest.fixture
def app():
    app = create_app(testing=True)
    with app.app_context():
        _db.create_all()
        _seed_test_data()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_token(client):
    """Get a valid admin JWT token."""
    response = client.post('/api/admin/login', json={'password': 'admin'})
    return response.get_json()['token']


@pytest.fixture
def auth_headers(auth_token):
    """Headers dict with Bearer token for admin endpoints."""
    return {'Authorization': f'Bearer {auth_token}'}


def _seed_test_data():
    """Seed minimal test data."""
    _db.session.add(ActivePeriod())

    _db.session.add_all([
        Drink(name='Latte', description='Test latte', ratio_summary='75% milk, 25% espresso'),
        Drink(name='Cold Brew', description='Test cold brew', ratio_summary='100% cold coffee'),
        Drink(name='Disabled Drink', description='Should not appear', ratio_summary='N/A', enabled=False),
    ])

    _db.session.add_all([
        CustomizationOption(type='temperature', label='Hot'),
        CustomizationOption(type='temperature', label='Iced'),
        CustomizationOption(type='milk_type', label='Oat'),
        CustomizationOption(type='addon', label='Vanilla Syrup'),
    ])

    _db.session.add_all([
        Setting(key='orders_accepting', value=True),
        Setting(key='business_hours_start', value='08:00'),
        Setting(key='business_hours_end', value='17:00'),
        Setting(key='slot_interval_minutes', value=15),
    ])

    _db.session.commit()
