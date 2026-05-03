"""Tests for admin API endpoints."""


def test_login_success(client):
    response = client.post('/api/admin/login', json={'password': 'admin'})
    assert response.status_code == 200
    assert 'token' in response.get_json()


def test_login_wrong_password(client):
    response = client.post('/api/admin/login', json={'password': 'wrong'})
    assert response.status_code == 401


def test_admin_orders_requires_auth(client):
    response = client.get('/api/admin/orders')
    assert response.status_code == 401


def test_admin_orders_with_auth(client, auth_headers):
    response = client.get('/api/admin/orders', headers=auth_headers)
    assert response.status_code == 200
    assert 'orders' in response.get_json()


def test_toggle_drink(client, auth_headers):
    response = client.patch('/api/admin/drinks/1', json={'enabled': False}, headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()['enabled'] is False


def test_update_setting(client, auth_headers):
    response = client.patch('/api/admin/settings', json={
        'key': 'orders_accepting',
        'value': False
    }, headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()['value'] is False


def test_orders_rejected_when_disabled(client, auth_headers):
    # Disable orders
    client.patch('/api/admin/settings', json={
        'key': 'orders_accepting',
        'value': False
    }, headers=auth_headers)

    # Try to submit an order
    response = client.post('/api/orders', json={
        'customer_name': 'Test',
        'phone_number': '5551234567',
        'pickup_slot': '9:00 AM',
        'items': [{'drink_id': 1, 'customizations': {}}]
    })
    assert response.status_code == 503

    # Re-enable orders so other tests aren't affected
    client.patch('/api/admin/settings', json={
        'key': 'orders_accepting',
        'value': True
    }, headers=auth_headers)


def test_reset_period(client, auth_headers):
    response = client.post('/api/admin/period/reset', headers=auth_headers)
    assert response.status_code == 200
    assert 'Period reset' in response.get_json()['message']


def test_get_trends(client, auth_headers):
    response = client.get('/api/admin/trends', headers=auth_headers)
    assert response.status_code == 200
    assert 'data' in response.get_json()
