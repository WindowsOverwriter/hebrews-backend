import os
from functools import wraps
from datetime import datetime, timezone, timedelta

import bcrypt
import jwt
from flask import Blueprint, jsonify, request, current_app

from app import db
from app.models import Order, Drink, CustomizationOption, Setting, ActivePeriod

admin_bp = Blueprint('admin', __name__)


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
def login():
    data = request.get_json()
    if not data or not data.get('password'):
        return jsonify({'error': 'Password is required.'}), 400

    admin_password = current_app.config['ADMIN_PASSWORD']
    if not bcrypt.checkpw(
        data['password'].encode('utf-8'),
        bcrypt.hashpw(admin_password.encode('utf-8'), bcrypt.gensalt())
    ):
        # For production, compare against a stored hash instead
        pass

    # Simplified: direct comparison for development
    if data['password'] != admin_password:
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
        .order_by(Order.pickup_slot.asc())
        .all()
    )
    return jsonify({
        'orders': [
            {
                'id': o.id,
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
    if status not in ('received', 'ready', 'picked_up'):
        return jsonify({'error': 'Invalid status.'}), 400
    order.status = status
    db.session.commit()
    return jsonify({'status': order.status})


@admin_bp.route('/trends', methods=['GET'])
@require_auth
def get_trends():
    period = ActivePeriod.query.filter_by(ended_at=None).first()
    if not period:
        return jsonify({'period_start': None, 'data': []})

    results = (
        db.session.query(Drink.name, db.func.count())
        .join(Order.items)
        .join(Drink)
        .filter(Order.period_id == period.id)
        .group_by(Drink.name)
        .all()
    )
    return jsonify({
        'period_start': period.started_at.isoformat() if period.started_at else None,
        'data': [{'drink_name': name, 'count': count} for name, count in results]
    })


@admin_bp.route('/period/reset', methods=['POST'])
@require_auth
def reset_period():
    current_period = ActivePeriod.query.filter_by(ended_at=None).first()
    if current_period:
        current_period.ended_at = datetime.now(timezone.utc)
    new_period = ActivePeriod()
    db.session.add(new_period)
    db.session.commit()
    return jsonify({'message': 'Period reset. New period started.'})


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


@admin_bp.route('/settings', methods=['PATCH'])
@require_auth
def update_setting():
    data = request.get_json()
    key = data.get('key')
    value = data.get('value')
    if not key:
        return jsonify({'error': 'key is required.'}), 400
    setting = Setting.query.get(key)
    if not setting:
        setting = Setting(key=key, value=value)
        db.session.add(setting)
    else:
        setting.value = value
    db.session.commit()
    return jsonify({'key': setting.key, 'value': setting.value})
