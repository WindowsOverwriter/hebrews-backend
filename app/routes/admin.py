import json
import os
from functools import wraps
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import bcrypt
import jwt
from flask import Blueprint, jsonify, request, current_app

from sqlalchemy.orm import joinedload
from app import db, limiter
from app.models import Order, OrderItem, Drink, DrinkCustomizationType, CustomizationOption, DrinkCustomizationOption, Setting, ActivePeriod, Location, LocationDate
from app.routes.public import purge_expired_locations, get_current_period, PICKUP_SLOT_RE, HHMM_RE

admin_bp = Blueprint('admin', __name__)

# S3: only these setting keys may be created/updated via the admin API.
# Adding a new setting requires updating both this set and any consumer.
VALID_SETTING_KEYS = frozenset({
    'orders_accepting',
    'business_hours_start',
    'business_hours_end',
    'slot_interval_minutes',
})

# Constrain customization option types to the four the trends aggregator
# (get_trends) and drink-type assignment know about. Adding a new type requires
# code changes elsewhere, not just an admin-UI action.
VALID_CUSTOMIZATION_TYPES = frozenset({
    'temperature',
    'espresso_type',
    'milk_type',
    'syrup',
})
MAX_CUSTOMIZATION_LABEL_LEN = 100
MAX_DRINK_NAME_LEN = 100
MAX_LOCATION_NAME_LEN = 150
MAX_LOCATION_ADDRESS_LEN = 300
MAX_SLOT_INTERVAL_MINUTES = 240

_BAD_BODY = 'Request body must be a JSON object.'


def _json_body():
    """The request's JSON object, or None if the body is missing, null, or
    not an object (A7: every handler used to 500 on `null`)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _is_bool(value):
    return isinstance(value, bool)


def _slot_minutes(slot):
    """'8:30 AM' -> 510. Unparseable slots sort last (A5)."""
    m = PICKUP_SLOT_RE.match(slot or '')
    if not m:
        return 24 * 60
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == 'PM' else 0)
    return hour * 60 + int(m.group(2))


def _validate_customization_types(types):
    """Return (deduped list, None) or (None, error). A10: types were stored
    unchecked, so unknown names were accepted and duplicates 500'd."""
    if not isinstance(types, list):
        return None, 'customization_types must be a list.'
    bad = [t for t in types if not isinstance(t, str) or t not in VALID_CUSTOMIZATION_TYPES]
    if bad:
        return None, f'Unknown customization type(s): {bad}. Valid types: {sorted(VALID_CUSTOMIZATION_TYPES)}'
    return list(dict.fromkeys(types)), None


def _validate_setting(key, value):
    """Return (normalized value, None) or (None, error) for a whitelisted key.
    A6: an unparseable business_hours_* value used to 500 the public /slots
    endpoint and block every order."""
    if key == 'orders_accepting':
        if not _is_bool(value):
            return None, 'orders_accepting must be true or false.'
        return value, None
    if key in ('business_hours_start', 'business_hours_end'):
        if not isinstance(value, str) or not HHMM_RE.match(value.strip()):
            return None, f'{key} must be a 24-hour time in HH:MM format.'
        return value.strip(), None
    if key == 'slot_interval_minutes':
        if _is_bool(value) or not isinstance(value, int) or not 1 <= value <= MAX_SLOT_INTERVAL_MINUTES:
            return None, f'slot_interval_minutes must be a whole number from 1 to {MAX_SLOT_INTERVAL_MINUTES}.'
        return value, None
    return None, 'Unknown setting key.'


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid token.'}), 401
        token = auth_header[7:]
        try:
            claims = jwt.decode(token, current_app.config['JWT_SECRET'], algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired.'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Invalid token.'}), 401
        # A11: accept only tokens this app issued for the admin role, and only
        # while the password they were issued under is still the current one.
        if (
            claims.get('role') != 'admin'
            or claims.get('pwv') != current_app.config['ADMIN_PASSWORD_FINGERPRINT']
        ):
            return jsonify({'error': 'Invalid token.'}), 401
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = _json_body()
    password = data.get('password') if data else None
    if not password or not isinstance(password, str):
        return jsonify({'error': 'Password is required.'}), 400

    stored_hash = current_app.config['ADMIN_PASSWORD_HASH']
    if not bcrypt.checkpw(password.encode('utf-8'), stored_hash):
        return jsonify({'error': 'Invalid password.'}), 401

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            'role': 'admin',
            'pwv': current_app.config['ADMIN_PASSWORD_FINGERPRINT'],
            'iat': now,
            'exp': now + timedelta(hours=8)
        },
        current_app.config['JWT_SECRET'],
        algorithm='HS256'
    )
    return jsonify({'token': token})


@admin_bp.route('/orders', methods=['GET'])
@require_auth
def get_orders():
    period = get_current_period()
    if not period:
        return jsonify({'orders': []})

    orders = (
        Order.query
        .filter_by(period_id=period.id)
        .options(joinedload(Order.items).joinedload(OrderItem.drink))
        .all()
    )
    # A5: pickup_slot is a display string; sorting it lexically put
    # '10:00 AM' before '8:00 AM'. Sort by clock time, then arrival.
    orders.sort(key=lambda o: (_slot_minutes(o.pickup_slot), o.id))
    return jsonify({
        'orders': [
            {
                'id': o.id,
                'order_number': o.order_number,
                'confirmation_code': o.confirmation_code,
                'customer_name': o.customer_name,
                'phone_number': o.phone_number,
                'pickup_slot': o.pickup_slot,
                'status': o.status,
                'items': [
                    {
                        'drink_name': item.drink.name if item.drink else 'Unknown',
                        'customizations': item.customizations
                    }
                    for item in o.items
                ],
                'created_at': o.created_at.isoformat() if o.created_at else None
            }
            for o in orders
        ]
    })


@admin_bp.route('/orders/<int:order_id>/status', methods=['PATCH'])
@require_auth
def update_order_status(order_id):
    order = db.get_or_404(Order, order_id)
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400
    status = data.get('status')
    if status not in ('queued', 'received', 'completed'):
        return jsonify({'error': 'Invalid status.'}), 400
    order.status = status
    db.session.commit()
    return jsonify({'status': order.status})


_TRACKED_CUSTOMIZATION_KEYS = ('temperature', 'espresso_type', 'milk_type', 'syrup')


def _tally_customizations(custs, cust_counts):
    """Accumulate {type: {label: count}} from one item's customizations.
    Tolerates malformed rows (non-dict, or unhashable values) so a single
    bad order can't break the dashboard for the whole period."""
    if not isinstance(custs, dict):
        return
    for key in _TRACKED_CUSTOMIZATION_KEYS:
        val = custs.get(key)
        if val and isinstance(val, (str, int, float, bool)):
            cust_counts.setdefault(key, {})
            cust_counts[key][val] = cust_counts[key].get(val, 0) + 1


@admin_bp.route('/trends', methods=['GET'])
@require_auth
def get_trends():
    period = get_current_period()
    if not period:
        return jsonify({'period_start': None, 'total_orders': 0, 'drinks': [], 'customizations': {}})

    # Drink frequency
    drink_results = (
        db.session.query(Drink.name, db.func.count())
        .join(OrderItem, OrderItem.drink_id == Drink.id)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.period_id == period.id)
        .group_by(Drink.name)
        .all()
    )

    # Customization breakdowns — parse JSONB from order items
    order_items = (
        db.session.query(OrderItem.customizations)
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.period_id == period.id)
        .all()
    )

    cust_counts = {}  # { type: { label: count } }
    for (custs,) in order_items:
        _tally_customizations(custs, cust_counts)

    total_orders = Order.query.filter_by(period_id=period.id).count()

    return jsonify({
        'period_start': period.started_at.isoformat() if period.started_at else None,
        'total_orders': total_orders,
        'drinks': [{'drink_name': name, 'count': count} for name, count in drink_results],
        'customizations': {
            ctype: [{'label': label, 'count': count} for label, count in sorted(vals.items(), key=lambda x: -x[1])]
            for ctype, vals in cust_counts.items()
        }
    })


@admin_bp.route('/period/reset', methods=['POST'])
@require_auth
def reset_period():
    # A12: a race can leave more than one period open. Close all of them so
    # the system self-heals; the newest is the one reported/exported.
    open_periods = (
        ActivePeriod.query
        .filter_by(ended_at=None)
        .order_by(ActivePeriod.id.desc())
        .all()
    )
    if not open_periods:
        new_period = ActivePeriod()
        db.session.add(new_period)
        db.session.commit()
        return jsonify({'message': 'New period started.', 'export_file': None})

    current_period = open_periods[0]
    ended_at = datetime.now(timezone.utc)
    for p in open_periods:
        p.ended_at = ended_at

    # Gather orders for export before anonymizing
    orders = (
        Order.query
        .filter(Order.period_id.in_([p.id for p in open_periods]))
        .options(joinedload(Order.items).joinedload(OrderItem.drink))
        .all()
    )

    # Build anonymized export data
    export_orders = []
    for o in orders:
        export_orders.append({
            'pickup_slot': o.pickup_slot,
            'status': o.status,
            'created_at': o.created_at.isoformat() if o.created_at else None,
            'items': [
                {
                    'drink_name': item.drink.name if item.drink else 'Unknown',
                    'customizations': item.customizations
                }
                for item in o.items
            ]
        })

    # Aggregate stats for the summary
    drink_counts = {}
    cust_counts = {}
    for o in orders:
        for item in o.items:
            dname = item.drink.name if item.drink else 'Unknown'
            drink_counts[dname] = drink_counts.get(dname, 0) + 1
            _tally_customizations(item.customizations, cust_counts)

    export_data = {
        'period': {
            'id': current_period.id,
            'started_at': current_period.started_at.isoformat() if current_period.started_at else None,
            'ended_at': current_period.ended_at.isoformat()
        },
        'summary': {
            'total_orders': len(orders),
            'drinks': sorted(
                [{'name': k, 'count': v} for k, v in drink_counts.items()],
                key=lambda x: -x['count']
            ),
            'customizations': {
                ctype: sorted(
                    [{'label': k, 'count': v} for k, v in vals.items()],
                    key=lambda x: -x['count']
                )
                for ctype, vals in cust_counts.items()
            }
        },
        'orders': export_orders
    }

    # Write export file
    exports_dir = Path(current_app.root_path).parent / 'exports'
    exports_dir.mkdir(exist_ok=True)
    date_str = current_period.ended_at.strftime('%Y-%m-%d_%H%M%S')
    filename = f'period-{current_period.id}-{date_str}.json'
    filepath = exports_dir / filename
    with open(filepath, 'w') as f:
        json.dump(export_data, f, indent=2)

    # Anonymize orders in the database
    for o in orders:
        o.customer_name = 'Anonymous'
        o.phone_number = ''
        o.confirmation_code = f'ANON-{o.id}'
        # order_number is kept: it is not PII, and zeroing it would violate
        # UNIQUE(order_number, period_id) as soon as the period has 2+ orders.

    # Start new period
    new_period = ActivePeriod()
    db.session.add(new_period)
    db.session.commit()

    return jsonify({
        'message': 'Session ended. Data exported and anonymized.',
        'export_file': filename,
        'total_orders': len(orders)
    })


@admin_bp.route('/drinks/<int:drink_id>', methods=['PATCH'])
@require_auth
def toggle_drink(drink_id):
    drink = db.get_or_404(Drink, drink_id)
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400
    if 'enabled' in data:
        if not _is_bool(data['enabled']):
            return jsonify({'error': 'enabled must be true or false.'}), 400
        drink.enabled = data['enabled']
    db.session.commit()
    return jsonify({'id': drink.id, 'enabled': drink.enabled})


@admin_bp.route('/customizations/<int:option_id>', methods=['PATCH'])
@require_auth
def update_customization(option_id):
    option = db.get_or_404(CustomizationOption, option_id)
    data = request.get_json() or {}
    if 'label' in data:
        label = (data.get('label') or '').strip()
        if not label:
            return jsonify({'error': 'label must be a non-empty string.'}), 400
        if len(label) > MAX_CUSTOMIZATION_LABEL_LEN:
            return jsonify({'error': f'label must be {MAX_CUSTOMIZATION_LABEL_LEN} characters or fewer.'}), 400
        duplicate = CustomizationOption.query.filter(
            CustomizationOption.type == option.type,
            CustomizationOption.label == label,
            CustomizationOption.id != option.id,
        ).first()
        if duplicate:
            return jsonify({'error': f'A {option.type} option named "{label}" already exists.'}), 400
        option.label = label
    if 'enabled' in data:
        if not _is_bool(data['enabled']):
            return jsonify({'error': 'enabled must be true or false.'}), 400
        option.enabled = data['enabled']
    db.session.commit()
    return jsonify({
        'id': option.id,
        'type': option.type,
        'label': option.label,
        'enabled': option.enabled,
    })


@admin_bp.route('/customizations', methods=['POST'])
@require_auth
def create_customization():
    data = request.get_json() or {}
    type_ = (data.get('type') or '').strip()
    label = (data.get('label') or '').strip()
    if type_ not in VALID_CUSTOMIZATION_TYPES:
        return jsonify({'error': f'Unknown type. Valid types: {sorted(VALID_CUSTOMIZATION_TYPES)}'}), 400
    if not label:
        return jsonify({'error': 'label must be a non-empty string.'}), 400
    if len(label) > MAX_CUSTOMIZATION_LABEL_LEN:
        return jsonify({'error': f'label must be {MAX_CUSTOMIZATION_LABEL_LEN} characters or fewer.'}), 400
    duplicate = CustomizationOption.query.filter_by(type=type_, label=label).first()
    if duplicate:
        return jsonify({'error': f'A {type_} option named "{label}" already exists.'}), 400
    option = CustomizationOption(type=type_, label=label)
    db.session.add(option)
    db.session.commit()
    return jsonify({
        'id': option.id,
        'type': option.type,
        'label': option.label,
        'enabled': option.enabled,
    }), 201


@admin_bp.route('/customizations/<int:option_id>', methods=['DELETE'])
@require_auth
def delete_customization(option_id):
    option = db.get_or_404(CustomizationOption, option_id)
    # Cascade at the app level so SQLite (no PRAGMA foreign_keys) also cleans up.
    DrinkCustomizationOption.query.filter_by(customization_option_id=option_id).delete()
    db.session.delete(option)
    db.session.commit()
    return jsonify({'message': 'Customization option deleted.'})


@admin_bp.route('/drinks', methods=['POST'])
@require_auth
def create_drink():
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400
    name = data.get('name')
    if not isinstance(name, str) or not name.strip():
        return jsonify({'error': 'name is required.'}), 400
    name = name.strip()
    if len(name) > MAX_DRINK_NAME_LEN:
        return jsonify({'error': f'name must be {MAX_DRINK_NAME_LEN} characters or fewer.'}), 400
    for field in ('description', 'ratio_summary'):
        if data.get(field) is not None and not isinstance(data[field], str):
            return jsonify({'error': f'{field} must be a string.'}), 400
    enabled = data.get('enabled', True)
    if not _is_bool(enabled):
        return jsonify({'error': 'enabled must be true or false.'}), 400
    types, type_error = _validate_customization_types(data.get('customization_types', []))
    if type_error:
        return jsonify({'error': type_error}), 400

    drink = Drink(
        name=name,
        description=(data.get('description') or '').strip(),
        ratio_summary=(data.get('ratio_summary') or '').strip(),
        enabled=enabled
    )
    db.session.add(drink)
    db.session.flush()
    for ct in types:
        db.session.add(DrinkCustomizationType(drink_id=drink.id, customization_type=ct))
    db.session.commit()
    return jsonify({
        'id': drink.id,
        'name': drink.name,
        'customization_types': [ct.customization_type for ct in drink.customization_types]
    }), 201


@admin_bp.route('/drinks/<int:drink_id>', methods=['DELETE'])
@require_auth
def delete_drink(drink_id):
    drink = db.get_or_404(Drink, drink_id)
    # order_items.drink_id has no ON DELETE rule, so Postgres raises an
    # IntegrityError (500) if any order ever referenced this drink, and SQLite
    # silently orphans the rows. Refuse cleanly and point the admin at the
    # toggle instead -- order history must stay intact.
    order_count = OrderItem.query.filter_by(drink_id=drink.id).count()
    if order_count:
        return jsonify({
            'error': (
                f'"{drink.name}" appears in {order_count} order item(s) and cannot be deleted. '
                'Disable it to hide it from the menu instead.'
            )
        }), 409
    db.session.delete(drink)
    db.session.commit()
    return jsonify({'message': 'Drink deleted.'})


@admin_bp.route('/drinks/<int:drink_id>/customization-types', methods=['PUT'])
@require_auth
def set_drink_customization_types(drink_id):
    drink = db.get_or_404(Drink, drink_id)
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400
    types, type_error = _validate_customization_types(data.get('types', []))
    if type_error:
        return jsonify({'error': type_error}), 400
    # Replace all
    DrinkCustomizationType.query.filter_by(drink_id=drink.id).delete()
    for ct in types:
        db.session.add(DrinkCustomizationType(drink_id=drink.id, customization_type=ct))
    db.session.commit()
    return jsonify({
        'customization_types': [ct.customization_type for ct in drink.customization_types]
    })


@admin_bp.route('/drinks/<int:drink_id>/customization-options', methods=['PUT'])
@require_auth
def set_drink_customization_options(drink_id):
    """Replace this drink's per-type option allowlist. Body:
        {"overrides": {"temperature": [2], "milk_type": [7, 8]}}
    Types omitted or given an empty list clear their override
    (drink falls back to all globally-enabled options of that type)."""
    drink = db.get_or_404(Drink, drink_id)
    data = request.get_json() or {}
    overrides = data.get('overrides', {})

    if not isinstance(overrides, dict):
        return jsonify({'error': 'overrides must be an object mapping type -> list of option IDs.'}), 400

    validated = {}  # type -> list of ids
    for type_, ids in overrides.items():
        if type_ not in VALID_CUSTOMIZATION_TYPES:
            return jsonify({'error': f'Unknown type "{type_}".'}), 400
        if not isinstance(ids, list):
            return jsonify({'error': f'overrides["{type_}"] must be a list of option IDs.'}), 400
        for opt_id in ids:
            if not isinstance(opt_id, int):
                return jsonify({'error': f'overrides["{type_}"] must contain integer IDs.'}), 400
        if not ids:
            continue  # Empty list = clear override for this type
        valid_ids = {
            o.id for o in CustomizationOption.query.filter(
                CustomizationOption.type == type_,
                CustomizationOption.id.in_(ids),
            ).all()
        }
        invalid = set(ids) - valid_ids
        if invalid:
            return jsonify({'error': f'Invalid option IDs for type "{type_}": {sorted(invalid)}.'}), 400
        validated[type_] = list(dict.fromkeys(ids))  # de-dup, preserve order

    DrinkCustomizationOption.query.filter_by(drink_id=drink.id).delete()
    for type_, ids in validated.items():
        for opt_id in ids:
            db.session.add(DrinkCustomizationOption(
                drink_id=drink.id,
                customization_option_id=opt_id,
            ))
    db.session.commit()

    return jsonify({'allowed_customization_options': _drink_override_map(drink.id)})


def _drink_override_map(drink_id):
    """Return {type: [option_ids]} for this drink; empty dict if no overrides."""
    rows = (
        db.session.query(CustomizationOption.type, CustomizationOption.id)
        .join(
            DrinkCustomizationOption,
            DrinkCustomizationOption.customization_option_id == CustomizationOption.id,
        )
        .filter(DrinkCustomizationOption.drink_id == drink_id)
        .order_by(CustomizationOption.type, CustomizationOption.id)
        .all()
    )
    result = {}
    for type_, opt_id in rows:
        result.setdefault(type_, []).append(opt_id)
    return result


def build_all_override_maps():
    """Build {drink_id: {type: [option_ids]}} for every drink with overrides."""
    rows = (
        db.session.query(
            DrinkCustomizationOption.drink_id,
            CustomizationOption.type,
            DrinkCustomizationOption.customization_option_id,
        )
        .join(
            CustomizationOption,
            CustomizationOption.id == DrinkCustomizationOption.customization_option_id,
        )
        .order_by(CustomizationOption.type, CustomizationOption.id)
        .all()
    )
    result = {}
    for drink_id, type_, opt_id in rows:
        result.setdefault(drink_id, {}).setdefault(type_, []).append(opt_id)
    return result


@admin_bp.route('/menu', methods=['GET'])
@require_auth
def get_admin_menu():
    drinks = Drink.query.order_by(Drink.name).all()
    options = CustomizationOption.query.order_by(CustomizationOption.type, CustomizationOption.id).all()
    override_maps = build_all_override_maps()

    customizations = {}
    for opt in options:
        customizations.setdefault(opt.type, []).append({
            'id': opt.id,
            'label': opt.label,
            'enabled': opt.enabled
        })

    def _drink_dict(d):
        result = {
            'id': d.id,
            'name': d.name,
            'description': d.description,
            'ratio_summary': d.ratio_summary,
            'enabled': d.enabled,
            'customization_types': [ct.customization_type for ct in d.customization_types],
        }
        if d.id in override_maps:
            result['allowed_customization_options'] = override_maps[d.id]
        return result

    return jsonify({
        'drinks': [_drink_dict(d) for d in drinks],
        'customizations': customizations,
    })


@admin_bp.route('/settings', methods=['GET'])
@require_auth
def get_settings():
    settings = Setting.query.all()
    return jsonify({s.key: s.value for s in settings})


@admin_bp.route('/settings', methods=['PATCH'])
@require_auth
def update_setting():
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400
    key = data.get('key')
    if not key:
        return jsonify({'error': 'key is required.'}), 400
    if key not in VALID_SETTING_KEYS:
        return jsonify({'error': f'Unknown setting key. Valid keys: {sorted(VALID_SETTING_KEYS)}'}), 400
    value, value_error = _validate_setting(key, data.get('value'))
    if value_error:
        return jsonify({'error': value_error}), 400
    setting = db.session.get(Setting, key)
    if not setting:
        setting = Setting(key=key, value=value)
        db.session.add(setting)
    else:
        setting.value = value
    db.session.commit()
    return jsonify({'key': setting.key, 'value': setting.value})


@admin_bp.route('/locations', methods=['GET'])
@require_auth
def get_all_locations():
    # M5: admin dashboard visit is the natural cleanup moment for expired
    # locations. Auth-gated + admins expect the list to be authoritative.
    if purge_expired_locations():
        db.session.commit()
    locations = Location.query.order_by(Location.name).all()
    return jsonify({
        'locations': [
            {
                'id': loc.id,
                'name': loc.name,
                'address': loc.address,
                'active': loc.active,
                'delete_after': loc.delete_after.isoformat() if loc.delete_after else None,
                'dates': sorted([d.date.isoformat() for d in loc.dates])
            }
            for loc in locations
        ]
    })


@admin_bp.route('/locations', methods=['POST'])
@require_auth
def create_location():
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400
    name = data.get('name')
    address = data.get('address')
    if not isinstance(name, str) or not isinstance(address, str):
        return jsonify({'error': 'name and address must be strings.'}), 400
    name, address = name.strip(), address.strip()
    if not name or not address:
        return jsonify({'error': 'name and address are required.'}), 400
    if len(name) > MAX_LOCATION_NAME_LEN or len(address) > MAX_LOCATION_ADDRESS_LEN:
        return jsonify({'error': f'name must be {MAX_LOCATION_NAME_LEN} and address {MAX_LOCATION_ADDRESS_LEN} characters or fewer.'}), 400
    location = Location(name=name, address=address)
    db.session.add(location)
    db.session.commit()
    return jsonify({
        'id': location.id,
        'name': location.name,
        'address': location.address,
        'active': location.active
    }), 201


@admin_bp.route('/locations/<int:location_id>', methods=['PATCH'])
@require_auth
def update_location(location_id):
    location = db.get_or_404(Location, location_id)
    data = _json_body()
    if data is None:
        return jsonify({'error': _BAD_BODY}), 400

    # A14: validate everything before mutating so a bad field can't leave a
    # half-applied update or a 500.
    updates = {}
    if 'name' in data:
        name = data['name']
        if not isinstance(name, str) or not name.strip():
            return jsonify({'error': 'name must be a non-empty string.'}), 400
        if len(name.strip()) > MAX_LOCATION_NAME_LEN:
            return jsonify({'error': f'name must be {MAX_LOCATION_NAME_LEN} characters or fewer.'}), 400
        updates['name'] = name.strip()
    if 'address' in data:
        address = data['address']
        if not isinstance(address, str) or not address.strip():
            return jsonify({'error': 'address must be a non-empty string.'}), 400
        if len(address.strip()) > MAX_LOCATION_ADDRESS_LEN:
            return jsonify({'error': f'address must be {MAX_LOCATION_ADDRESS_LEN} characters or fewer.'}), 400
        updates['address'] = address.strip()
    if 'active' in data:
        if not _is_bool(data['active']):
            return jsonify({'error': 'active must be true or false.'}), 400
        updates['active'] = data['active']
    if 'delete_after' in data:
        raw = data['delete_after']
        if raw in (None, ''):
            updates['delete_after'] = None
        elif isinstance(raw, str):
            try:
                updates['delete_after'] = date.fromisoformat(raw)
            except ValueError:
                return jsonify({'error': f'Invalid delete_after "{raw}". Expected YYYY-MM-DD.'}), 400
        else:
            return jsonify({'error': 'delete_after must be a YYYY-MM-DD string or null.'}), 400

    for field, value in updates.items():
        setattr(location, field, value)
    db.session.commit()
    return jsonify({
        'id': location.id,
        'name': location.name,
        'address': location.address,
        'active': location.active,
        'delete_after': location.delete_after.isoformat() if location.delete_after else None
    })


@admin_bp.route('/locations/<int:location_id>', methods=['DELETE'])
@require_auth
def delete_location(location_id):
    location = db.get_or_404(Location, location_id)
    db.session.delete(location)
    db.session.commit()
    return jsonify({'message': 'Location deleted.'})


@admin_bp.route('/locations/<int:location_id>/dates', methods=['PUT'])
@require_auth
def set_location_dates(location_id):
    """Replace all dates for a location with the provided list."""
    location = db.get_or_404(Location, location_id)
    data = request.get_json() or {}
    raw_dates = data.get('dates', [])

    # M8: validate every date string BEFORE mutating anything, so a bad
    # input can't wipe the existing schedule and 500.
    if not isinstance(raw_dates, list):
        return jsonify({'error': 'dates must be a list of ISO date strings (YYYY-MM-DD).'}), 400
    parsed = []
    for d in raw_dates:
        if not isinstance(d, str):
            return jsonify({'error': 'Each date must be an ISO date string (YYYY-MM-DD).'}), 400
        try:
            parsed.append(date.fromisoformat(d))
        except ValueError:
            return jsonify({'error': f'Invalid date "{d}". Expected YYYY-MM-DD.'}), 400

    parsed = list(dict.fromkeys(parsed))  # de-dup: duplicates would 500 on uq_location_date
    LocationDate.query.filter_by(location_id=location.id).delete()
    for parsed_date in parsed:
        db.session.add(LocationDate(location_id=location.id, date=parsed_date))
    db.session.commit()
    return jsonify({
        'dates': sorted([d.date.isoformat() for d in location.dates])
    })
