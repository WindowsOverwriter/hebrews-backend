from datetime import date, datetime, timezone
import logging
import re
from flask import Blueprint, jsonify, request
from sqlalchemy.orm import joinedload
from app import db, limiter
from app.models import Drink, DrinkCustomizationType, CustomizationOption, Order, OrderItem, Setting, ActivePeriod, Location, LocationDate
import random
import string

logger = logging.getLogger(__name__)

# Day-of-month prefix mapping — coffee-themed 2-char codes
DAY_PREFIXES = {
    1: 'HB',  # HeBrews
    2: 'BW',  # Brew
    3: 'CF',  # Coffee
    4: 'LT',  # Latte
    5: 'CP',  # Cappuccino
    6: 'AM',  # Americano
    7: 'CB',  # Cold Brew
    8: 'DR',  # Drip
    9: 'MT',  # Matcha
    10: 'ES',  # Espresso
    11: 'MK',  # Milk
    12: 'CM',  # Cream
    13: 'BN',  # Bean
    14: 'RS',  # Roast
    15: 'GR',  # Grind
    16: 'ST',  # Steam
    17: 'FM',  # Foam
    18: 'BR',  # Barista
    19: 'SH',  # Shot
    20: 'PR',  # Pour
    21: 'SP',  # Sip
    22: 'CU',  # Cup
    23: 'MG',  # Mug
    24: 'AR',  # Aroma
    25: 'SM',  # Smooth
    26: 'BL',  # Blend
    27: 'DK',  # Dark
    28: 'JV',  # Java
    29: 'MC',  # Mocha
    30: 'VN',  # Vanilla
    31: 'CR',  # Caramel
}

STALE_CODE_HOURS = 48

# m15: cap the confirmation-code retry loop so a pathological state
# (profanity-heavy RNG streak or an exhausted namespace) can't hang the
# server thread. 10 attempts * 36^6 suffixes = astronomically unlikely
# to hit in practice.
_MAX_CODE_ATTEMPTS = 10

# Format validation (S2): pickup_slot matches "8:30 AM" / "12:15 PM";
# phone_number allows digits, spaces, dashes, parens, dots, and optional
# leading + — must contain 7–15 digits total.
_PICKUP_SLOT_RE = re.compile(r'^(1[0-2]|[1-9]):[0-5]\d\s(AM|PM)$')
_PHONE_ALLOWED_RE = re.compile(r'^[\d\s\-().+]{7,20}$')

public_bp = Blueprint('public', __name__)


@public_bp.route('/menu', methods=['GET'])
def get_menu():
    # M2: eager-load customization_types to avoid N+1 (one query per drink)
    drinks = (
        Drink.query
        .filter_by(enabled=True)
        .options(joinedload(Drink.customization_types))
        .all()
    )
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
                'ratio_summary': d.ratio_summary,
                'customization_types': [ct.customization_type for ct in d.customization_types]
            }
            for d in drinks
        ],
        'customizations': customizations
    })


@public_bp.route('/slots', methods=['GET'])
def get_slots():
    start_setting = db.session.get(Setting, 'business_hours_start')
    end_setting = db.session.get(Setting, 'business_hours_end')
    interval_setting = db.session.get(Setting, 'slot_interval_minutes')

    start = start_setting.value if start_setting else '08:00'
    end = end_setting.value if end_setting else '17:00'
    # M7: settings values are JSON — coerce to int; fall back to 15 if the
    # stored value can't be parsed (avoids an infinite loop below).
    try:
        interval = int(interval_setting.value) if interval_setting else 15
    except (TypeError, ValueError):
        interval = 15
    if interval <= 0:
        interval = 15

    start_h, start_m = map(int, start.split(':'))
    end_h, end_m = map(int, end.split(':'))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    slots = []
    current = start_minutes
    # m16: closing time is not a valid pickup slot — use < not <=
    while current < end_minutes:
        h, m = divmod(current, 60)
        period = 'AM' if h < 12 else 'PM'
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        slots.append(f'{display_h}:{m:02d} {period}')
        current += interval

    return jsonify({'slots': slots})


# Patterns checked against the full code (prefix + suffix) with dash removed.
# Covers common profanity and slurs including leetspeak variants (0=O, 1=I/L, 3=E, 4=A, 5=S, 7=T, 8=B).
_PROFANITY_PATTERNS = re.compile('|'.join([
    r'F[U\W]?[CK]{2}',
    r'SH[1I]T',
    r'[A4][S5]{2}',
    r'D[1I]CK',
    r'C[O0]CK',
    r'CUM',
    r'C[U\W]?NT',
    r'P[U\W]?[S5]{2}[YI]',
    r'T[1I]T[S5]?',
    r'B[1I]TCH',
    r'[S5]LUT',
    r'WH[O0]R[E3]',
    r'D[A4]MN',
    r'H[E3]LL',
    r'N[1I]G',
    r'F[A4]G',
    r'K[1I]K[E3]',
    r'SP[1I]C',
    r'W[E3]TB',
    r'[S5]H[1I]T',
    r'J[1I]ZZ',
    r'[A4]NUS',
    r'[A4]N[A4]L',
    r'P[E3]N[1I][S5]',
    r'V[A4]G',
    r'D[1I]LD',
    r'R[A4]P[E3]',
    r'KKK',
    r'N[A4]Z[1I]',
    r'WTF',
    r'STF[U\W]',
]), re.IGNORECASE)


def _code_is_clean(code):
    flat = code.replace('-', '')
    return not _PROFANITY_PATTERNS.search(flat)


def _generate_confirmation_code():
    prefix = DAY_PREFIXES[date.today().day]
    for _ in range(_MAX_CODE_ATTEMPTS):
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        code = f'{prefix}-{suffix}'
        if not _code_is_clean(code):
            continue
        if not Order.query.filter_by(confirmation_code=code).first():
            return code
    raise RuntimeError(
        f'Could not generate a unique confirmation code after {_MAX_CODE_ATTEMPTS} attempts'
    )


def _next_order_number(period_id):
    # Lock the period row to serialize order number assignment
    db.session.query(ActivePeriod).filter_by(id=period_id).with_for_update().first()
    max_num = (
        db.session.query(db.func.max(Order.order_number))
        .filter_by(period_id=period_id)
        .scalar()
    )
    return (max_num or 0) + 1


def purge_expired_locations():
    """Delete locations whose delete_after date has passed. Callers own the
    commit — this only stages deletes on the session."""
    today = date.today()
    expired = Location.query.filter(
        Location.delete_after != None,
        Location.delete_after <= today
    ).all()
    for loc in expired:
        db.session.delete(loc)
    return len(expired)


@public_bp.route('/locations', methods=['GET'])
def get_locations():
    # M5: GET stays idempotent — filter expired out in the query rather than
    # deleting them here. Actual cleanup runs when admin views their list
    # (see admin.get_all_locations).
    today = date.today()
    locations = (
        Location.query
        .filter_by(active=True)
        .filter(
            (Location.delete_after == None) | (Location.delete_after > today)
        )
        .order_by(Location.name)
        .all()
    )
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
    accepting = db.session.get(Setting, 'orders_accepting')
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
    if not _PICKUP_SLOT_RE.match(pickup_slot):
        return jsonify({'error': 'pickup_slot must look like "8:30 AM" or "12:15 PM".'}), 400
    if not _PHONE_ALLOWED_RE.match(phone_number) or sum(c.isdigit() for c in phone_number) < 7:
        return jsonify({'error': 'phone_number must contain 7–15 digits with only digits, spaces, dashes, parens, dots, or a leading +.'}), 400
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
    order_num = _next_order_number(period.id)
    order = Order(
        order_number=order_num,
        confirmation_code=code,
        customer_name=customer_name,
        phone_number=phone_number,
        pickup_slot=pickup_slot,
        period_id=period.id
    )
    db.session.add(order)
    db.session.flush()

    for item in items:
        drink_id = item.get('drink_id')
        if not isinstance(drink_id, int):
            db.session.rollback()
            return jsonify({'error': 'Each item must have a valid drink_id.'}), 400
        drink = db.session.get(Drink, drink_id)
        if not drink or not drink.enabled:
            db.session.rollback()
            return jsonify({'error': f'Drink {drink_id} is not available.'}), 400
        order_item = OrderItem(
            order_id=order.id,
            drink_id=drink_id,
            customizations=item.get('customizations', {})
        )
        db.session.add(order_item)

    db.session.commit()

    return jsonify({
        'order_number': order_num,
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

    # Flag stale code lookups (>48 hours after order creation)
    if order.created_at:
        created = order.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if age_hours > STALE_CODE_HOURS:
            logger.warning(
                'Stale code lookup: %s (%.0fh old) from %s',
                order.confirmation_code, age_hours,
                request.remote_addr
            )

    # Return first name only — no phone number on public endpoint
    first_name = order.customer_name.split()[0] if order.customer_name else ''

    return jsonify({
        'confirmation_code': order.confirmation_code,
        'customer_name': first_name,
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
