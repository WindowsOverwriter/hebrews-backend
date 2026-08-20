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
    data = response.get_json()
    assert 'message' in data
    assert 'export' in data['message'].lower() or 'period' in data['message'].lower()


def test_get_trends(client, auth_headers):
    response = client.get('/api/admin/trends', headers=auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert 'drinks' in data
    assert 'customizations' in data
    assert 'total_orders' in data


# ─── Customization option CRUD ───

def test_create_customization(client, auth_headers):
    response = client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': 'Peppermint'},
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.get_json()
    assert data['type'] == 'syrup'
    assert data['label'] == 'Peppermint'
    assert data['enabled'] is True
    assert isinstance(data['id'], int)


def test_create_customization_rejects_unknown_type(client, auth_headers):
    response = client.post(
        '/api/admin/customizations',
        json={'type': 'sweetener', 'label': 'Sugar'},
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_create_customization_rejects_duplicate(client, auth_headers):
    # Vanilla is seeded as a syrup option in conftest.
    response = client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': 'Vanilla'},
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_create_customization_rejects_blank_label(client, auth_headers):
    response = client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': '   '},
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_create_customization_requires_auth(client):
    response = client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': 'Peppermint'},
    )
    assert response.status_code == 401


def test_update_customization_label(client, auth_headers):
    # id=4 is the seeded Vanilla syrup.
    response = client.patch(
        '/api/admin/customizations/4',
        json={'label': 'French Vanilla'},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.get_json()['label'] == 'French Vanilla'


def test_update_customization_label_rejects_duplicate(client, auth_headers):
    client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': 'Peppermint'},
        headers=auth_headers,
    )
    response = client.patch(
        '/api/admin/customizations/4',
        json={'label': 'Peppermint'},
        headers=auth_headers,
    )
    assert response.status_code == 400


def test_update_customization_toggle_still_works(client, auth_headers):
    # Regression: PATCH with just {enabled} must still work (existing UI path).
    response = client.patch(
        '/api/admin/customizations/4',
        json={'enabled': False},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.get_json()['enabled'] is False


def test_delete_customization(client, auth_headers):
    response = client.delete('/api/admin/customizations/4', headers=auth_headers)
    assert response.status_code == 200
    # Verify it's no longer surfaced by the public menu.
    menu = client.get('/api/menu').get_json()
    syrups = menu['customizations'].get('syrup', [])
    assert not any(s['label'] == 'Vanilla' for s in syrups)


def test_delete_customization_requires_auth(client):
    response = client.delete('/api/admin/customizations/4')
    assert response.status_code == 401


def test_admin_menu_customizations_sorted_by_creation(client, auth_headers):
    # Seeded syrup: Vanilla (id=4). Add two more; verify creation order,
    # not alphabetical (Almond would come first if sorted by label).
    client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': 'Peppermint'},
        headers=auth_headers,
    )
    client.post(
        '/api/admin/customizations',
        json={'type': 'syrup', 'label': 'Almond'},
        headers=auth_headers,
    )
    response = client.get('/api/admin/menu', headers=auth_headers)
    labels = [s['label'] for s in response.get_json()['customizations']['syrup']]
    assert labels == ['Vanilla', 'Peppermint', 'Almond']
