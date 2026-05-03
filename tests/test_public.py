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
