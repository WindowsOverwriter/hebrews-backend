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
from app.models import Order, OrderItem, Drink, DrinkCustomizationType, CustomizationOption, Setting, ActivePeriod, Location, LocationDate

admin_bp = Blueprint('admin', __name__)

# S3: only these setting keys may be created/updated via the admin API.
# Adding a new setting requires updating both this set and any consumer.
VALID_SETTING_KEYS = frozenset({
    'orders_accepting',
    'business_hours_start',
    'business_hours_end',
    'slot_interval_minutes',
})


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid token.'}), 401
        token = auth_header[7:]
        try:
            jwt.decode(token, current_app.config['JWT_SECRET'], algorithms=['HS256'])
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token expired.'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Invalid token.'}), 401
        return f(*args, **kwargs)
    return decorated


@admin_bp.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    data = request.get_json()
    if not data or not data.get('password'):
        return jsonify({'error': 'Password is required.'}), 400

    stored_hash = current_app.config['ADMIN_PASSWORD_HASH']
    if not bcrypt.checkpw(data['password'].encode('utf-8'), stored_hash):
        return jsonify({'error': 'Invalid password.'}), 401

    token = jwt.encode(
        {
            'role': 'admin',
            'exp': datetime.now(timezone.utc) + timedelta(hours=8)
        },
        current_app.config['JWT_SECRET'],
        algorithm='HS256'
    )
    return jsonify({'token': token})


@admin_bp.route('/orders', methods=['GET'])
@require_auth
def get_orders():
    period = ActivePeriod.query.filter_by(ended_at=None).first()
    if not period:
        return jsonify({'orders': []})

    orders = (
        Order.query
        .filter_by(period_id=period.id)
        .options(joinedload(Order.items).joinedload(OrderItem.drink))
        .order_by(Order.pickup_slot.asc())
        .all()
    )
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
    order = Order.query.get_or_404(order_id)
    data = request.get_json()
    status = data.get('status')
    if status not in ('queued', 'received', 'completed'):
        return jsonify({'error': 'Invalid status.'}), 400
    order.status = status
    db.session.commit()
    return jsonify({'status': order.status})


@admin_bp.route('/trends', methods=['GET'])
@require_auth
def get_trends():
    period = ActivePeriod.query.filter_by(ended_at=None).first()
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
        if not custs:
            continue
        for key in ('temperature', 'espresso_type', 'milk_type', 'syrup'):
            val = custs.get(key)
            if val:
                cust_counts.setdefault(key, {})
                cust_counts[key][val] = cust_counts[key].get(val, 0) + 1

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
    current_period = ActivePeriod.query.filter_by(ended_at=None).first()
    if not current_period:
        new_period = ActivePeriod()
        db.session.add(new_period)
        db.session.commit()
        return jsonify({'message': 'New period started.', 'export_file': None})

    # Close the current period
    current_period.ended_at = datetime.now(timezone.utc)

    # Gather orders for export before anonymizing
    orders = (
        Order.query
        .filter_by(period_id=current_period.id)
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
            if item.customizations:
                for key in ('temperature', 'espresso_type', 'milk_type', 'syrup'):
                    val = item.customizations.get(key)
                    if val:
                        cust_counts.setdefault(key, {})
                        cust_counts[key][val] = cust_counts[key].get(val, 0) + 1

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
        o.order_number = 0

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
    drink = Drink.query.get_or_404(drink_id)
    data = request.get_json()
    drink.enabled = data.get('enabled', drink.enabled)
    db.session.commit()
    return jsonify({'id': drink.id, 'enabled': drink.enabled})


@admin_bp.route('/customizations/<int:option_id>', methods=['PATCH'])
@require_auth
def toggle_customization(option_id):
    option = CustomizationOption.query.get_or_404(option_id)
    data = request.get_json()
    option.enabled = data.get('enabled', option.enabled)
    db.session.commit()
    return jsonify({'id': option.id, 'enabled': option.enabled})


@admin_bp.route('/drinks', methods=['POST'])
@require_auth
def create_drink():
    data = request.get_json()
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name is required.'}), 400
    drink = Drink(
        name=name,
        description=(data.get('description') or '').strip(),
        ratio_summary=(data.get('ratio_summary') or '').strip(),
        enabled=data.get('enabled', True)
    )
    db.session.add(drink)
    db.session.flush()
    # Attach customization types if provided
    for ct in data.get('customization_types', []):
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
    drink = Drink.query.get_or_404(drink_id)
    db.session.delete(drink)
    db.session.commit()
    return jsonify({'message': 'Drink deleted.'})


@admin_bp.route('/drinks/<int:drink_id>/customization-types', methods=['PUT'])
@require_auth
def set_drink_customization_types(drink_id):
    drink = Drink.query.get_or_404(drink_id)
    data = request.get_json()
    types = data.get('types', [])
    # Replace all
    DrinkCustomizationType.query.filter_by(drink_id=drink.id).delete()
    for ct in types:
        db.session.add(DrinkCustomizationType(drink_id=drink.id, customization_type=ct))
    db.session.commit()
    return jsonify({
        'customization_types': [ct.customization_type for ct in drink.customization_types]
    })


@admin_bp.route('/menu', methods=['GET'])
@require_auth
def get_admin_menu():
    drinks = Drink.query.order_by(Drink.name).all()
    options = CustomizationOption.query.order_by(CustomizationOption.type, CustomizationOption.label).all()

    customizations = {}
    for opt in options:
        customizations.setdefault(opt.type, []).append({
            'id': opt.id,
            'label': opt.label,
            'enabled': opt.enabled
        })

    return jsonify({
        'drinks': [
            {
                'id': d.id,
                'name': d.name,
                'description': d.description,
                'ratio_summary': d.ratio_summary,
                'enabled': d.enabled,
                'customization_types': [ct.customization_type for ct in d.customization_types]
            }
            for d in drinks
        ],
        'customizations': customizations
    })


@admin_bp.route('/settings', methods=['GET'])
@require_auth
def get_settings():
    settings = Setting.query.all()
    return jsonify({s.key: s.value for s in settings})


@admin_bp.route('/settings', methods=['PATCH'])
@require_auth
def update_setting():
    data = request.get_json()
    key = data.get('key')
    value = data.get('value')
    if not key:
        return jsonify({'error': 'key is required.'}), 400
    if key not in VALID_SETTING_KEYS:
        return jsonify({'error': f'Unknown setting key. Valid keys: {sorted(VALID_SETTING_KEYS)}'}), 400
    setting = Setting.query.get(key)
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
    data = request.get_json()
    name = (data.get('name') or '').strip()
    address = (data.get('address') or '').strip()
    if not name or not address:
        return jsonify({'error': 'name and address are required.'}), 400
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
    location = Location.query.get_or_404(location_id)
    data = request.get_json()
    if 'name' in data:
        location.name = data['name'].strip()
    if 'address' in data:
        location.address = data['address'].strip()
    if 'active' in data:
        location.active = data['active']
    if 'delete_after' in data:
        if data['delete_after']:
            location.delete_after = date.fromisoformat(data['delete_after'])
        else:
            location.delete_after = None
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
    location = Location.query.get_or_404(location_id)
    db.session.delete(location)
    db.session.commit()
    return jsonify({'message': 'Location deleted.'})


@admin_bp.route('/locations/<int:location_id>/dates', methods=['PUT'])
@require_auth
def set_location_dates(location_id):
    """Replace all dates for a location with the provided list."""
    location = Location.query.get_or_404(location_id)
    data = request.get_json()
    dates = data.get('dates', [])

    # Clear existing dates
    LocationDate.query.filter_by(location_id=location.id).delete()

    # Add new dates
    for d in dates:
        loc_date = LocationDate(location_id=location.id, date=date.fromisoformat(d))
        db.session.add(loc_date)

    db.session.commit()
    return jsonify({
        'dates': sorted([d.date.isoformat() for d in location.dates])
    })
