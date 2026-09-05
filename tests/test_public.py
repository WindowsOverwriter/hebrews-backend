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
        'items': []
    })
    assert response.status_code == 400


def test_submit_order_empty_cart(client):
    response = client.post('/api/orders', json={
        'customer_name': fake.name(),
        'phone_number': fake.phone_number(),
        'pickup_slot': '9:00 AM',
        'items': []
    })
    assert response.status_code == 400


def test_get_order_by_code(client):
    # Place an order first
    submit = client.post('/api/orders', json={
        'customer_name': 'Jane Doe',
        'phone_number': '5551234567',
        'pickup_slot': '10:00 AM',
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
    base = {'phone_number': '5551234567', 'pickup_slot': '9:00 AM', 'items': [{'drink_id': 1}]}
    assert client.post('/api/orders', json={**base, 'customer_name': None}).status_code == 400
    assert client.post('/api/orders', json={**base, 'customer_name': 42}).status_code == 400
    assert client.post('/api/orders', json={**base, 'customer_name': 'A', 'pickup_slot': ['9:00 AM']}).status_code == 400
    assert client.post('/api/orders', data='null', content_type='application/json').status_code == 400


def test_order_insert_retries_on_confirmation_code_collision(client, monkeypatch):
    """A15: a unique-constraint collision is retried with a fresh code."""
    import app.routes.public as public
    first = client.post('/api/orders', json={
        'customer_name': 'First', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'items': [{'drink_id': 1}],
    }).get_json()['confirmation_code']

    codes = iter([first, 'HB-FRESH1'])
    monkeypatch.setattr(public, '_generate_confirmation_code', lambda: next(codes))
    r = client.post('/api/orders', json={
        'customer_name': 'Second', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'items': [{'drink_id': 1}],
    })
    assert r.status_code == 201
    assert r.get_json()['confirmation_code'] == 'HB-FRESH1'
    assert r.get_json()['order_number'] == 2


def test_order_insert_gives_up_after_repeated_collisions(client, monkeypatch):
    """A15"""
    import app.routes.public as public
    first = client.post('/api/orders', json={
        'customer_name': 'First', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'items': [{'drink_id': 1}],
    }).get_json()['confirmation_code']
    monkeypatch.setattr(public, '_generate_confirmation_code', lambda: first)
    r = client.post('/api/orders', json={
        'customer_name': 'Second', 'phone_number': '5551234567',
        'pickup_slot': '9:00 AM', 'items': [{'drink_id': 1}],
    })
    assert r.status_code == 503
