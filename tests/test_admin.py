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


def test_drink_deletion_cascades_options_overrides(client, auth_headers):
    # Seed an override, then delete the drink; overrides must not leak.
    client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [2]},
    }, headers=auth_headers)
    client.delete('/api/admin/drinks/1', headers=auth_headers)
    # Fetching admin menu shouldn't crash or return the deleted drink's overrides.
    resp = client.get('/api/admin/menu', headers=auth_headers)
    assert resp.status_code == 200
    for d in resp.get_json()['drinks']:
        assert d['id'] != 1


def test_delete_customization_cascades_overrides(client, auth_headers):
    # Set override that references option id=4 (Vanilla), then delete Vanilla;
    # the override rows for it must be cleaned up.
    client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'syrup': [4]},
    }, headers=auth_headers)
    resp = client.delete('/api/admin/customizations/4', headers=auth_headers)
    assert resp.status_code == 200
    # Menu should now show drink 1 with no syrup override remaining.
    menu = client.get('/api/menu').get_json()
    drink1 = next(d for d in menu['drinks'] if d['id'] == 1)
    assert 'allowed_customization_options' not in drink1 or 'syrup' not in drink1.get('allowed_customization_options', {})


# ─── Per-drink option overrides ───

def test_set_drink_options_override_stores_and_returns(client, auth_headers):
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [2]},  # 2 = Iced
    }, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()['allowed_customization_options'] == {'temperature': [2]}


def test_public_menu_includes_override(client, auth_headers):
    client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [2]},
    }, headers=auth_headers)
    menu = client.get('/api/menu').get_json()
    drink1 = next(d for d in menu['drinks'] if d['id'] == 1)
    assert drink1['allowed_customization_options'] == {'temperature': [2]}


def test_public_menu_omits_override_key_when_none(client):
    menu = client.get('/api/menu').get_json()
    for d in menu['drinks']:
        # No overrides seeded → key must be absent (not present-and-empty).
        assert 'allowed_customization_options' not in d


def test_empty_list_clears_override(client, auth_headers):
    # Set then clear
    client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [2]},
    }, headers=auth_headers)
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': []},
    }, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.get_json()['allowed_customization_options'] == {}


def test_omitting_type_clears_override(client, auth_headers):
    # Set for temperature and syrup, then re-PUT with only syrup — temperature must clear.
    client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [2], 'syrup': [4]},
    }, headers=auth_headers)
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'syrup': [4]},
    }, headers=auth_headers)
    body = resp.get_json()
    assert 'temperature' not in body['allowed_customization_options']
    assert body['allowed_customization_options']['syrup'] == [4]


def test_override_rejects_unknown_type(client, auth_headers):
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'sweetener': [1]},
    }, headers=auth_headers)
    assert resp.status_code == 400


def test_override_rejects_cross_type_id(client, auth_headers):
    # id=4 is Vanilla (syrup), not a temperature option.
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [4]},
    }, headers=auth_headers)
    assert resp.status_code == 400


def test_override_rejects_nonexistent_id(client, auth_headers):
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [9999]},
    }, headers=auth_headers)
    assert resp.status_code == 400


def test_override_rejects_non_integer_id(client, auth_headers):
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': ['2']},
    }, headers=auth_headers)
    assert resp.status_code == 400


def test_override_requires_auth(client):
    resp = client.put('/api/admin/drinks/1/customization-options', json={
        'overrides': {'temperature': [2]},
    })
    assert resp.status_code == 401


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



# ---------------------------------------------------------------------------
# Audit fixes (2026-09-04)
# ---------------------------------------------------------------------------

def _place_order(client, slot='9:00 AM', drink_id=1, customizations=None):
    return client.post('/api/orders', json={
        'customer_name': 'Audit Tester',
        'phone_number': '5551234567',
        'pickup_slot': slot,
        'items': [{'drink_id': drink_id, 'customizations': customizations or {'milk_type': 'Oat'}}],
    })


def test_reset_period_with_multiple_orders(client, auth_headers):
    """Anonymizing two orders in one period must not collide on
    UNIQUE(order_number, period_id)."""
    assert _place_order(client, '8:00 AM').status_code == 201
    assert _place_order(client, '9:00 AM').status_code == 201

    response = client.post('/api/admin/period/reset', headers=auth_headers)
    assert response.status_code == 200
    assert response.get_json()['total_orders'] == 2

    # New period is empty and the old orders were anonymized in place.
    assert client.get('/api/admin/orders', headers=auth_headers).get_json()['orders'] == []
    from app.models import Order
    names = {o.customer_name for o in Order.query.all()}
    assert names == {'Anonymous'}
    assert all(o.confirmation_code.startswith('ANON-') for o in Order.query.all())


def test_trends_and_reset_tolerate_malformed_customizations(client, auth_headers, app):
    """A pre-existing row with non-dict customizations must not 500 the
    admin dashboard (defense in depth behind the public-endpoint validation)."""
    assert _place_order(client).status_code == 201
    from app import db
    from app.models import OrderItem
    with app.app_context():
        item = OrderItem.query.first()
        item.customizations = ['Oat']
        db.session.commit()

    assert client.get('/api/admin/trends', headers=auth_headers).status_code == 200
    assert client.post('/api/admin/period/reset', headers=auth_headers).status_code == 200


def test_delete_drink_with_order_history_returns_409(client, auth_headers):
    assert _place_order(client, drink_id=1).status_code == 201
    response = client.delete('/api/admin/drinks/1', headers=auth_headers)
    assert response.status_code == 409
    assert 'Disable' in response.get_json()['error']
    # Drink still exists.
    assert client.get('/api/admin/menu', headers=auth_headers).status_code == 200
    ids = [d['id'] for d in client.get('/api/admin/menu', headers=auth_headers).get_json()['drinks']]
    assert 1 in ids


def test_delete_drink_without_orders_still_works(client, auth_headers):
    response = client.delete('/api/admin/drinks/2', headers=auth_headers)
    assert response.status_code == 200


def test_login_rate_limit_keys_on_forwarded_client_ip(client):
    """Behind a proxy, X-Forwarded-For identifies the client. Exhausting the
    login limit from one address must not lock out a different address."""
    for _ in range(5):
        r = client.post('/api/admin/login', json={'password': 'wrong'},
                        headers={'X-Forwarded-For': '203.0.113.10'})
        assert r.status_code == 401
    blocked = client.post('/api/admin/login', json={'password': 'wrong'},
                          headers={'X-Forwarded-For': '203.0.113.10'})
    assert blocked.status_code == 429

    other = client.post('/api/admin/login', json={'password': 'admin'},
                        headers={'X-Forwarded-For': '203.0.113.99'})
    assert other.status_code == 200


# ---------------------------------------------------------------------------
# Audit fixes A5-A15 (2026-09-04)
# ---------------------------------------------------------------------------

def test_admin_orders_sorted_by_clock_time(client, auth_headers):
    """A5: lexical sort put '10:00 AM' before '8:00 AM'."""
    for slot in ['10:00 AM', '12:00 PM', '1:00 PM', '8:00 AM', '9:30 AM']:
        assert _place_order(client, slot).status_code == 201
    slots = [o['pickup_slot'] for o in client.get('/api/admin/orders', headers=auth_headers).get_json()['orders']]
    assert slots == ['8:00 AM', '9:30 AM', '10:00 AM', '12:00 PM', '1:00 PM']


def test_setting_rejects_bad_business_hours(client, auth_headers):
    """A6"""
    for bad in ['nine', '25:00', '9:60', None, 9]:
        r = client.patch('/api/admin/settings', headers=auth_headers,
                         json={'key': 'business_hours_start', 'value': bad})
        assert r.status_code == 400, bad
    assert client.get('/api/slots').status_code == 200


def test_setting_rejects_bad_interval_and_flag(client, auth_headers):
    """A6"""
    for bad in [0, -5, 999, '15', None, True]:
        r = client.patch('/api/admin/settings', headers=auth_headers,
                         json={'key': 'slot_interval_minutes', 'value': bad})
        assert r.status_code == 400, bad
    r = client.patch('/api/admin/settings', headers=auth_headers,
                     json={'key': 'orders_accepting', 'value': 'no'})
    assert r.status_code == 400


def test_setting_accepts_frontend_values(client, auth_headers):
    """A6: the admin UI sends HH:MM strings, ints, and booleans."""
    ok = [('business_hours_start', '07:30'), ('business_hours_end', '18:00'),
          ('slot_interval_minutes', 30), ('orders_accepting', True)]
    for key, value in ok:
        r = client.patch('/api/admin/settings', headers=auth_headers, json={'key': key, 'value': value})
        assert r.status_code == 200, key
    slots = client.get('/api/slots').get_json()['slots']
    assert slots[0] == '7:30 AM' and slots[-1] == '5:30 PM'


def test_slots_survive_garbage_already_in_db(client, app):
    """A6: values written before validation existed must not 500."""
    from app import db
    from app.models import Setting
    with app.app_context():
        db.session.get(Setting, 'business_hours_start').value = 'garbage'
        db.session.commit()
    r = client.get('/api/slots')
    assert r.status_code == 200
    assert r.get_json()['slots'][0] == '8:00 AM'  # default


def test_login_rejects_non_string_password(client):
    """A7"""
    assert client.post('/api/admin/login', json={'password': 12345}).status_code == 400
    assert client.post('/api/admin/login', json={'password': ['admin']}).status_code == 400


def test_admin_handlers_reject_null_body(client, auth_headers):
    """A7: every mutating admin handler used to 500 on a JSON null body."""
    null = {'data': 'null', 'content_type': 'application/json', 'headers': auth_headers}
    assert client.patch('/api/admin/orders/1/status', **null).status_code in (400, 404)
    assert client.patch('/api/admin/drinks/1', **null).status_code == 400
    assert client.post('/api/admin/drinks', **null).status_code == 400
    assert client.put('/api/admin/drinks/1/customization-types', **null).status_code == 400
    assert client.patch('/api/admin/settings', **null).status_code == 400
    assert client.post('/api/admin/locations', **null).status_code == 400


def test_ensure_schema_is_noop_on_fresh_db(app):
    """A8: constraint already present -> nothing added, no error."""
    from app.schema import ensure_schema
    with app.app_context():
        assert ensure_schema() == []


def test_location_delete_after_semantics(client, auth_headers, app):
    """A9: delete_after is the last day shown; purge strictly after it."""
    from datetime import timedelta
    from app import db
    from app.models import Location
    from app.routes.public import business_today
    with app.app_context():
        today = business_today()
        db.session.add_all([
            Location(name='Ends Today', address='a', delete_after=today),
            Location(name='Ended Yesterday', address='b', delete_after=today - timedelta(days=1)),
            Location(name='Ends Tomorrow', address='c', delete_after=today + timedelta(days=1)),
        ])
        db.session.commit()

    public = {l['name'] for l in client.get('/api/locations').get_json()['locations']}
    assert public == {'Ends Today', 'Ends Tomorrow'}

    admin = {l['name'] for l in client.get('/api/admin/locations', headers=auth_headers).get_json()['locations']}
    assert admin == {'Ends Today', 'Ends Tomorrow'}  # yesterday's was purged


def test_business_today_uses_configured_timezone(app):
    """A9: 'today' follows BUSINESS_TIMEZONE, not the server clock."""
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    from app.routes.public import business_today
    with app.app_context():
        app.config['BUSINESS_TIMEZONE'] = 'Pacific/Kiritimati'   # UTC+14
        east = business_today()
        app.config['BUSINESS_TIMEZONE'] = 'Pacific/Pago_Pago'    # UTC-11
        west = business_today()
    # 25 hours apart: the two dates can never be equal at the same instant.
    assert (east - west) in (timedelta(days=1), timedelta(days=2))


def test_drink_customization_types_validated(client, auth_headers):
    """A10"""
    r = client.post('/api/admin/drinks', headers=auth_headers,
                    json={'name': 'Flat White', 'customization_types': ['bogus']})
    assert r.status_code == 400
    r = client.put('/api/admin/drinks/1/customization-types', headers=auth_headers,
                   json={'types': ['syrup', 'syrup', 'milk_type']})
    assert r.status_code == 200
    assert sorted(r.get_json()['customization_types']) == ['milk_type', 'syrup']
    r = client.put('/api/admin/drinks/1/customization-types', headers=auth_headers, json={'types': 'syrup'})
    assert r.status_code == 400


def test_token_requires_admin_role_and_current_password(client, app, auth_headers):
    """A11"""
    import jwt
    from datetime import datetime, timezone, timedelta
    secret = app.config['JWT_SECRET']
    exp = datetime.now(timezone.utc) + timedelta(hours=1)

    no_role = jwt.encode({'pwv': app.config['ADMIN_PASSWORD_FINGERPRINT'], 'exp': exp}, secret, algorithm='HS256')
    assert client.get('/api/admin/orders', headers={'Authorization': f'Bearer {no_role}'}).status_code == 401

    stale = jwt.encode({'role': 'admin', 'pwv': 'oldpassword00000', 'exp': exp}, secret, algorithm='HS256')
    assert client.get('/api/admin/orders', headers={'Authorization': f'Bearer {stale}'}).status_code == 401

    # A real token still works, and stops working once the password rotates.
    assert client.get('/api/admin/orders', headers=auth_headers).status_code == 200
    app.config['ADMIN_PASSWORD_FINGERPRINT'] = 'rotated0000000000'
    assert client.get('/api/admin/orders', headers=auth_headers).status_code == 401


def test_reset_closes_every_open_period(client, auth_headers, app):
    """A12"""
    from app import db
    from app.models import ActivePeriod
    with app.app_context():
        db.session.add(ActivePeriod())  # simulate the race leaving two open
        db.session.commit()
        assert ActivePeriod.query.filter_by(ended_at=None).count() == 2
    assert client.post('/api/admin/period/reset', headers=auth_headers).status_code == 200
    with app.app_context():
        assert ActivePeriod.query.filter_by(ended_at=None).count() == 1


def test_anonymized_order_not_publicly_readable(client, auth_headers):
    """A13"""
    code = _place_order(client).get_json()['confirmation_code']
    assert client.get(f'/api/orders/{code}').status_code == 200
    client.post('/api/admin/period/reset', headers=auth_headers)
    assert client.get(f'/api/orders/{code}').status_code == 404
    assert client.get('/api/orders/ANON-1').status_code == 404


def test_boolean_fields_validated(client, auth_headers):
    """A14"""
    assert client.patch('/api/admin/drinks/1', headers=auth_headers, json={'enabled': 'no'}).status_code == 400
    assert client.patch('/api/admin/customizations/1', headers=auth_headers, json={'enabled': 1}).status_code == 400
    loc = client.post('/api/admin/locations', headers=auth_headers, json={'name': 'X', 'address': 'Y'}).get_json()
    assert client.patch(f"/api/admin/locations/{loc['id']}", headers=auth_headers, json={'active': 'yes'}).status_code == 400


def test_update_location_validates_fields(client, auth_headers):
    """A14"""
    loc = client.post('/api/admin/locations', headers=auth_headers, json={'name': 'X', 'address': 'Y'}).get_json()
    url = f"/api/admin/locations/{loc['id']}"
    assert client.patch(url, headers=auth_headers, json={'name': ''}).status_code == 400
    assert client.patch(url, headers=auth_headers, json={'name': None}).status_code == 400
    assert client.patch(url, headers=auth_headers, json={'delete_after': 'not-a-date'}).status_code == 400
    assert client.patch(url, headers=auth_headers, json={'delete_after': '2026-12-31'}).status_code == 200
    assert client.patch(url, headers=auth_headers, json={'delete_after': ''}).status_code == 200
    assert client.get('/api/admin/locations', headers=auth_headers).get_json()['locations'][0]['delete_after'] is None


def test_location_dates_dedup(client, auth_headers):
    loc = client.post('/api/admin/locations', headers=auth_headers, json={'name': 'X', 'address': 'Y'}).get_json()
    r = client.put(f"/api/admin/locations/{loc['id']}/dates", headers=auth_headers,
                   json={'dates': ['2026-10-01', '2026-10-01', '2026-10-02']})
    assert r.status_code == 200
    assert r.get_json()['dates'] == ['2026-10-01', '2026-10-02']
