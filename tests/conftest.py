from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from flask import current_app
from app import create_app, db as _db
from app.models import Drink, DrinkCustomizationType, CustomizationOption, Setting, ActivePeriod, Location
from app.routes import public as _public


def freeze_business_time(monkeypatch, hour, minute):
    """Freeze app.routes.public.business_now at today's real date at
    hour:minute in the business timezone.

    What is frozen is the *instant*: on first call the wall-clock time is
    anchored in whatever BUSINESS_TIMEZONE is configured, and every call
    returns that same instant converted to the zone configured at call time.
    For nearly every test the zone never changes, so business_now() is simply
    today at hour:minute; tests that swap BUSINESS_TIMEZONE still see 'today'
    move with the zone, as it does in production.
    """
    anchor = {}

    def frozen_business_now():
        tz = ZoneInfo(current_app.config['BUSINESS_TIMEZONE'])
        if 'instant' not in anchor:
            today = datetime.now(tz).date()
            anchor['instant'] = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)
        return anchor['instant'].astimezone(tz)

    monkeypatch.setattr(_public, 'business_now', frozen_business_now)


@pytest.fixture(autouse=True)
def _frozen_business_time(monkeypatch):
    """Pin 'now' to 07:30 business-local for every test, so slots from 8:00 AM
    onward are always offered no matter when the suite runs. Call
    freeze_business_time(monkeypatch, hour, minute) in a test to move it."""
    freeze_business_time(monkeypatch, 7, 30)


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

    latte = Drink(name='Latte', description='Test latte', ratio_summary='75% milk, 25% espresso')
    cold_brew = Drink(name='Cold Brew', description='Test cold brew', ratio_summary='100% cold coffee')
    disabled = Drink(name='Disabled Drink', description='Should not appear', ratio_summary='N/A', enabled=False)
    _db.session.add_all([latte, cold_brew, disabled])
    _db.session.flush()

    _db.session.add_all([
        DrinkCustomizationType(drink_id=latte.id, customization_type='temperature'),
        DrinkCustomizationType(drink_id=latte.id, customization_type='milk_type'),
        DrinkCustomizationType(drink_id=latte.id, customization_type='syrup'),
        DrinkCustomizationType(drink_id=cold_brew.id, customization_type='syrup'),
    ])

    _db.session.add_all([
        CustomizationOption(type='temperature', label='Hot'),
        CustomizationOption(type='temperature', label='Iced'),
        CustomizationOption(type='milk_type', label='Oat'),
        CustomizationOption(type='syrup', label='Vanilla'),
    ])

    # Orders must name a location; this one gets id 1.
    _db.session.add(Location(name='Main Street Pop-up', address='1 Main St'))

    _db.session.add_all([
        Setting(key='orders_accepting', value=True),
        Setting(key='business_hours_start', value='08:00'),
        Setting(key='business_hours_end', value='17:00'),
        Setting(key='slot_interval_minutes', value=15),
    ])

    _db.session.commit()
