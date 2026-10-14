"""Tests for public API endpoints."""
from faker import Faker

fake = Faker()


def test_get_menu_returns_enabled_drinks(client):
    response = client.get('/api/menu')
    assert response.status_code == 200
    data = response.get_json()
    assert 'drinks' in data
    assert 'customizations' in data
    drink_names = [d['name'] for d in data['drinks']]
    assert 'Latte' in drink_names
    assert 'Disabled Drink' not in drink_names


def test_get_slots_returns_time_slots(client):
    response = client.get('/api/slots')
    assert response.status_code == 200
    data = response.get_json()
    assert 'slots' in data
    assert len(data['slots']) > 0
    assert '8:00 AM' in data['slots']


def test_submit_order_success(client):
    response = client.post('/api/orders', json={
        'customer_name': 'Test User',
        'phone_number': '(555) 123-4567',
        'pickup_slot': '9:00 AM',
        'location_id': 1,
        'items': [
            {
                'drink_id': 1,
                'customizations': {
                    'temperature': 'Iced',
                    'milk_type': 'Oat',
                    'syrup': 'Vanilla',
                    'syrup_pumps': 2,
                    'special_instructions': 'Extra hot please'
                }
            }
        ]
    })
    assert response.status_code == 201
    data = response.get_json()
    code = data['confirmation_code']
    # Format: XX-YYYYYY (2-char day prefix, dash, 6 alphanumeric)
    assert len(code) == 9
    assert code[2] == '-'


def test_submit_order_missing_fields(client):
    response = client.post('/api/orders', json={
        'customer_name': '',
        'phone_number': '',
        'pickup_slot': '',
        'location_id': 1,
        'items': []
    })
    assert response.status_code == 400
    assert 'customer_name' in response.get_json()['error']


def test_submit_order_empty_cart(client):
    response = client.post('/api/orders', json={
        'customer_name': fake.name(),
        'phone_number': fake.phone_number(),
        'pickup_slot': '9:00 AM',
        'location_id': 1,
        'items': []
    })
    assert response.status_code == 400


def test_get_order_by_code(client):
    # Place an order first
    submit = client.post('/api/orders', json={
        'customer_name': 'Jane Doe',
        'phone_number': '5551234567',
        'pickup_slot': '10:00 AM',
        'location_id': 1,
        'items': [{'drink_id': 1, 'customizations': {'temperature': 'Hot'}}]
    })
    code = submit.get_json()['confirmation_code']

    response = client.get(f'/api/orders/{code}')
    assert response.status_code == 200
    data = response.get_json()
    # Public endpoint returns first name only, no phone number
    assert data['customer_name'] == 'Jane'
    assert 'phone_number' not in data
    assert data['status'] == 'queued'
    assert data['location_name'] == 'Main Street Pop-up'


def test_get_order_not_found(client):
    response = client.get('/api/orders/XX-ZZZZZZ')
    assert response.status_code == 404



# ---------------------------------------------------------------------------
# Audit fixes (2026-09-04): customizations shape validation
# ---------------------------------------------------------------------------

def _order_with(client, customizations, items=None):
    body = {
        'customer_name': 'Shape Tester',
        'phone_number': '5551234567',
        'pickup_slot': '9:00 AM',
        'location_id': 1,
        'items': items if items is not None else [{'drink_id': 1, 'customizations': customizations}],
    }
    return client.post('/api/orders', json=body)


def test_customizations_rejects_list(client):
    r = _order_with(client, ['Oat'])
    assert r.status_code == 400
    assert 'object' in r.get_json()['error']


def test_customizations_rejects_string(client):
    assert _order_with(client, 'Oat').status_code == 400


def test_customizations_rejects_nested_object_value(client):
    r = _order_with(client, {'milk_type': {'nested': True}})
    assert r.status_code == 400
    assert 'milk_type' in r.get_json()['error']


def test_customizations_rejects_oversized_string(client):
    assert _order_with(client, {'special_instructions': 'x' * 501}).status_code == 400


def test_customizations_rejects_too_many_keys(client):
    assert _order_with(client, {f'k{i}': 'v' for i in range(21)}).status_code == 400


def test_customizations_accepts_frontend_shape(client):
    r = _order_with(client, {
        'temperature': 'Iced', 'espresso_type': None, 'milk_type': 'Oat',
        'syrup': 'Vanilla', 'syrup_pumps': 2, 'special_instructions': None,
    })
    assert r.status_code == 201


def test_customizations_null_and_missing_default_to_empty(client):
    assert _order_with(client, None).status_code == 201
    assert _order_with(client, None, items=[{'drink_id': 1}]).status_code == 201


def test_items_must_be_list_of_objects(client):
    assert _order_with(client, None, items='abc').status_code == 400
    assert _order_with(client, None, items=[5]).status_code == 400


# ---------------------------------------------------------------------------
# Audit fixes A7 / A15 (2026-09-04)
# ---------------------------------------------------------------------------

def test_null_and_non_string_fields_are_400(client):
    """A7"""
    base = {'phone_number': '5551234567', 'pickup_slot': '9:00 AM', 'location_id': 1, 'items': [{'drink_id': 1}]}
    assert client.post('/api/orders', json={**base, 'customer_name': None}).status_code == 400
    assert client.post('/api/orders', json={**base, 'customer_name': 42}).status_code == 400
    assert client.post('/api/orders', json={**base, 'customer_name': 'A', 'pickup_slot': ['9:00 AM']}).status_code == 400
    assert client.post('/api/orders', data='null', content_type='application/json').status_code == 400


def test_order_insert_retries_on_confirmation_code_collision(client, monkeypatch):
    """A15: a unique-constraint collision is retried with a fresh code."""
    import app.routes.public as public
    first = client.post('/api/orders', json={
        'customer_name': 'First', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'location_id': 1, 'items': [{'drink_id': 1}],
    }).get_json()['confirmation_code']

    codes = iter([first, 'HB-FRESH1'])
    monkeypatch.setattr(public, '_generate_confirmation_code', lambda: next(codes))
    r = client.post('/api/orders', json={
        'customer_name': 'Second', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'location_id': 1, 'items': [{'drink_id': 1}],
    })
    assert r.status_code == 201
    assert r.get_json()['confirmation_code'] == 'HB-FRESH1'
    assert r.get_json()['order_number'] == 2


def test_order_insert_gives_up_after_repeated_collisions(client, monkeypatch):
    """A15"""
    import app.routes.public as public
    first = client.post('/api/orders', json={
        'customer_name': 'First', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'location_id': 1, 'items': [{'drink_id': 1}],
    }).get_json()['confirmation_code']
    monkeypatch.setattr(public, '_generate_confirmation_code', lambda: first)
    r = client.post('/api/orders', json={
        'customer_name': 'Second', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'location_id': 1, 'items': [{'drink_id': 1}],
    })
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Orders record their location (several locations can be open per day)
# ---------------------------------------------------------------------------

def _order_at(client, location_id, **overrides):
    body = {
        'customer_name': 'Location Tester',
        'phone_number': '5551234567',
        'pickup_slot': '9:00 AM',
        'items': [{'drink_id': 1}],
    }
    if location_id is not ...:
        body['location_id'] = location_id
    body.update(overrides)
    return client.post('/api/orders', json=body)


def _add_location(app, **fields):
    from app import db
    from app.models import Location
    with app.app_context():
        loc = Location(**{'name': 'Side Street', 'address': '2 Side St', **fields})
        db.session.add(loc)
        db.session.commit()
        return loc.id


def test_order_requires_location_id(client):
    r = _order_at(client, ...)  # key absent
    assert r.status_code == 400
    assert r.get_json()['error'] == 'location_id is required.'
    r = _order_at(client, None)
    assert r.status_code == 400
    assert r.get_json()['error'] == 'location_id is required.'


def test_order_location_id_must_be_integer(client):
    for bad in ('1', True, 1.5, [1], {'id': 1}):
        r = _order_at(client, bad)
        assert r.status_code == 400, bad
        assert r.get_json()['error'] == 'location_id must be an integer.'


def test_order_rejects_nonexistent_location(client):
    r = _order_at(client, 999)
    assert r.status_code == 400
    assert r.get_json()['error'] == 'Location 999 is not available.'


def test_order_rejects_inactive_location(client, app):
    loc_id = _add_location(app, active=False)
    r = _order_at(client, loc_id)
    assert r.status_code == 400
    assert r.get_json()['error'] == f'Location {loc_id} is not available.'


def test_order_rejects_location_expired_yesterday(client, app):
    from datetime import timedelta
    from app.routes.public import business_today
    with app.app_context():
        yesterday = business_today() - timedelta(days=1)
    loc_id = _add_location(app, delete_after=yesterday)
    r = _order_at(client, loc_id)
    assert r.status_code == 400
    assert r.get_json()['error'] == f'Location {loc_id} is not available.'


def test_order_accepts_location_on_its_last_day(client, app):
    """delete_after is the last day shown (A9), so it is still orderable."""
    from app.routes.public import business_today
    with app.app_context():
        today = business_today()
    loc_id = _add_location(app, delete_after=today)
    assert _order_at(client, loc_id).status_code == 201


def test_order_validates_location_before_writing(client):
    """A14: a bad location_id with otherwise valid items creates nothing."""
    from app.models import Order
    assert _order_at(client, 999).status_code == 400
    assert Order.query.count() == 0


def test_order_success_response_unchanged(client):
    r = _order_at(client, 1)
    assert r.status_code == 201
    data = r.get_json()
    assert set(data) == {'order_number', 'confirmation_code', 'message'}
    assert data['order_number'] == 1
    assert data['message'] == 'Order placed successfully.'


def test_public_order_lookup_includes_location_name(client, app):
    loc_id = _add_location(app, name='Riverside Market')
    code = _order_at(client, loc_id).get_json()['confirmation_code']
    data = client.get(f'/api/orders/{code}').get_json()
    assert data['location_name'] == 'Riverside Market'


def test_purge_expired_location_detaches_its_orders(client, app, auth_headers):
    """Orders outlive their location: the purge nulls location_id instead of
    failing or deleting the order."""
    from datetime import timedelta
    from app import db
    from app.models import Location, Order
    from app.routes.public import business_today
    with app.app_context():
        today = business_today()
    loc_id = _add_location(app, delete_after=today)
    code = _order_at(client, loc_id).get_json()['confirmation_code']

    # Move the location into the past, then trigger the purge via admin GET.
    with app.app_context():
        db.session.get(Location, loc_id).delete_after = today - timedelta(days=1)
        db.session.commit()
    names = {l['name'] for l in client.get('/api/admin/locations', headers=auth_headers).get_json()['locations']}
    assert 'Side Street' not in names

    data = client.get(f'/api/orders/{code}').get_json()
    assert data['location_name'] is None
    with app.app_context():
        assert Order.query.filter_by(confirmation_code=code).one().location_id is None


# ---------------------------------------------------------------------------
# Pickup slot availability: lunch gap and slots that have already passed
# ---------------------------------------------------------------------------

SLOT_UNAVAILABLE = 'That pickup time is no longer available. Please choose another time.'


def _slots(client):
    r = client.get('/api/slots')
    assert r.status_code == 200
    return r.get_json()['slots']


def test_slots_exclude_lunch_gap(client):
    """Default 08:00-17:00 at 15 min: nothing from 12:00 to 12:45 PM."""
    slots = _slots(client)
    for gone in ('12:00 PM', '12:15 PM', '12:30 PM', '12:45 PM'):
        assert gone not in slots
    assert '11:45 AM' in slots
    assert '1:00 PM' in slots


def test_slots_exclude_passed_times(client, monkeypatch):
    from tests.conftest import freeze_business_time
    freeze_business_time(monkeypatch, 10, 7)
    slots = _slots(client)
    assert slots[0] == '10:15 AM'
    assert '10:00 AM' not in slots


def test_slot_disappears_at_its_own_start_minute(client, monkeypatch):
    """Boundary is inclusive: at exactly 10:00 the 10:00 AM slot is gone."""
    from tests.conftest import freeze_business_time
    freeze_business_time(monkeypatch, 10, 0)
    slots = _slots(client)
    assert '10:00 AM' not in slots
    assert slots[0] == '10:15 AM'


def test_slots_empty_after_close(client, monkeypatch):
    from tests.conftest import freeze_business_time
    freeze_business_time(monkeypatch, 17, 30)
    assert _slots(client) == []


def test_order_rejects_passed_slot(client, monkeypatch):
    from tests.conftest import freeze_business_time
    freeze_business_time(monkeypatch, 10, 7)
    r = _order_at(client, 1, pickup_slot='9:00 AM')
    assert r.status_code == 400
    assert r.get_json() == {'error': SLOT_UNAVAILABLE}


def test_order_rejects_lunch_slot(client):
    r = _order_at(client, 1, pickup_slot='12:15 PM')
    assert r.status_code == 400
    assert r.get_json() == {'error': SLOT_UNAVAILABLE}


def test_order_rejects_off_grid_slot(client):
    """'9:05 AM' passes the format regex but is not on the 15-minute grid."""
    r = _order_at(client, 1, pickup_slot='9:05 AM')
    assert r.status_code == 400
    assert r.get_json() == {'error': SLOT_UNAVAILABLE}


def test_order_rejects_slot_after_close(client):
    r = _order_at(client, 1, pickup_slot='6:00 PM')
    assert r.status_code == 400
    assert r.get_json() == {'error': SLOT_UNAVAILABLE}


def test_order_accepts_next_available_slot(client, monkeypatch):
    from tests.conftest import freeze_business_time
    freeze_business_time(monkeypatch, 10, 7)
    assert _order_at(client, 1, pickup_slot='10:15 AM').status_code == 201


def test_order_rejected_slot_writes_nothing(client, monkeypatch):
    from app.models import Order
    from tests.conftest import freeze_business_time
    freeze_business_time(monkeypatch, 10, 7)
    assert _order_at(client, 1, pickup_slot='9:00 AM').status_code == 400
    assert _order_at(client, 1, pickup_slot='12:15 PM').status_code == 400
    assert Order.query.count() == 0
