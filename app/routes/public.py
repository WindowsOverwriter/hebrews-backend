from datetime import date
from flask import Blueprint, jsonify, request
from sqlalchemy.orm import joinedload
from app import db, limiter
from app.models import Drink, CustomizationOption, Order, OrderItem, Setting, ActivePeriod, Location, LocationDate
import random
import string

public_bp = Blueprint('public', __name__)


@public_bp.route('/menu', methods=['GET'])
def get_menu():
    drinks = Drink.query.filter_by(enabled=True).all()
    options = CustomizationOption.query.filter_by(enabled=True).all()

    customizations = {}
    for opt in options:
        customizations.setdefault(opt.type, []).append({
            'id': opt.id,
            'label': opt.label
        })

    return jsonify({
        'drinks': [
            {
                'id': d.id,
                'name': d.name,
                'description': d.description,
                'ratio_summary': d.ratio_summary
            }
            for d in drinks
        ],
        'customizations': customizations
    })


@public_bp.route('/slots', methods=['GET'])
def get_slots():
    start_setting = Setting.query.get('business_hours_start')
    end_setting = Setting.query.get('business_hours_end')
    interval_setting = Setting.query.get('slot_interval_minutes')

    start = start_setting.value if start_setting else '08:00'
    end = end_setting.value if end_setting else '17:00'
    interval = interval_setting.value if interval_setting else 15

    start_h, start_m = map(int, start.split(':'))
    end_h, end_m = map(int, end.split(':'))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    slots = []
    current = start_minutes
    while current <= end_minutes:
        h, m = divmod(current, 60)
        period = 'AM' if h < 12 else 'PM'
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        slots.append(f'{display_h}:{m:02d} {period}')
        current += interval

    return jsonify({'slots': slots})


def _generate_confirmation_code():
    while True:
        code = 'HB-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        if not Order.query.filter_by(confirmation_code=code).first():
            return code


@public_bp.route('/locations', methods=['GET'])
def get_locations():
    today = date.today()
    # Auto-delete expired locations
    expired = Location.query.filter(Location.delete_after != None, Location.delete_after <= today).all()
    for loc in expired:
        db.session.delete(loc)
    if expired:
        db.session.commit()

    locations = Location.query.filter_by(active=True).order_by(Location.name).all()
    return jsonify({
        'locations': [
            {
                'id': loc.id,
                'name': loc.name,
                'address': loc.address,
                'dates': sorted([
                    d.date.isoformat()
                    for d in loc.dates
                    if d.date >= today
                ])
            }
            for loc in locations
        ]
    })


@public_bp.route('/orders', methods=['POST'])
@limiter.limit("10 per minute")
def submit_order():
    accepting = Setting.query.get('orders_accepting')
    if accepting and not accepting.value:
        return jsonify({'error': 'Orders are not being accepted at this time.'}), 503

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body is required.'}), 400

    customer_name = data.get('customer_name', '').strip()
    phone_number = data.get('phone_number', '').strip()
    pickup_slot = data.get('pickup_slot', '').strip()
    items = data.get('items', [])

    if not customer_name or not phone_number or not pickup_slot:
        return jsonify({'error': 'customer_name, phone_number, and pickup_slot are required.'}), 400
    if len(customer_name) > 100:
        return jsonify({'error': 'customer_name must be 100 characters or fewer.'}), 400
    if len(phone_number) > 20:
        return jsonify({'error': 'phone_number must be 20 characters or fewer.'}), 400
    if len(pickup_slot) > 20:
        return jsonify({'error': 'pickup_slot must be 20 characters or fewer.'}), 400
    if not items:
        return jsonify({'error': 'At least one item is required.'}), 400
    if len(items) > 20:
        return jsonify({'error': 'Maximum 20 items per order.'}), 400

    period = ActivePeriod.query.filter_by(ended_at=None).first()
    if not period:
        period = ActivePeriod()
        db.session.add(period)
        db.session.flush()

    code = _generate_confirmation_code()
    order = Order(
        confirmation_code=code,
        customer_name=customer_name,
        phone_number=phone_number,
        pickup_slot=pickup_slot,
        period_id=period.id
    )
    db.session.add(order)
    db.session.flush()

    for item in items:
        order_item = OrderItem(
            order_id=order.id,
            drink_id=item['drink_id'],
            customizations=item.get('customizations', {})
        )
        db.session.add(order_item)

    db.session.commit()

    return jsonify({
        'confirmation_code': code,
        'message': 'Order placed successfully.'
    }), 201


@public_bp.route('/orders/<confirmation_code>', methods=['GET'])
@limiter.limit("15 per minute")
def get_order(confirmation_code):
    order = (
        Order.query
        .filter_by(confirmation_code=confirmation_code.upper())
        .options(joinedload(Order.items).joinedload(OrderItem.drink))
        .first()
    )
    if not order:
        return jsonify({'error': 'Order not found.'}), 404

    return jsonify({
        'confirmation_code': order.confirmation_code,
        'customer_name': order.customer_name,
        'pickup_slot': order.pickup_slot,
        'status': order.status,
        'items': [
            {
                'drink_name': item.drink.name if item.drink else 'Unknown',
                'customizations': item.customizations
            }
            for item in order.items
        ]
    })
