from datetime import date, datetime, timezone
import logging
import re
from zoneinfo import ZoneInfo
from flask import Blueprint, jsonify, request, current_app
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
from app import db, limiter
from app.models import Drink, DrinkCustomizationType, CustomizationOption, DrinkCustomizationOption, Order, OrderItem, Setting, ActivePeriod, Location, LocationDate
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
PICKUP_SLOT_RE = re.compile(r'^(1[0-2]|[1-9]):([0-5]\d)\s(AM|PM)$')
# 24-hour 'HH:MM' as stored in the business_hours_* settings.
HHMM_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')
_PHONE_ALLOWED_RE = re.compile(r'^[\d\s\-().+]{7,20}$')

# customizations must be a flat JSON object of scalar values. The admin
# trends/reset aggregators call .get() on it and use values as dict keys, so
# a list, string, or nested object here would 500 the admin dashboard for
# the rest of the period. Keys are not whitelisted (see pending F1) -- only
# shape and size are enforced.
_MAX_CUSTOMIZATION_KEYS = 20
_MAX_CUSTOMIZATION_KEY_LEN = 50
_MAX_CUSTOMIZATION_VALUE_LEN = 500

# A15: attempts at inserting one order before giving up on unique-constraint
# collisions (confirmation code, or order number on SQLite where the
# period row lock is a no-op).
_MAX_ORDER_ATTEMPTS = 3

public_bp = Blueprint('public', __name__)


def business_today():
    """Calendar date in the business's timezone (BUSINESS_TIMEZONE), not the
    server's. Use this for anything that means 'today' to a customer."""
    return datetime.now(ZoneInfo(current_app.config['BUSINESS_TIMEZONE'])).date()


def get_current_period():
    """The open ordering period. If a race ever leaves two open, the newest
    wins deterministically (A12); reset_period closes all of them."""
    return (
        ActivePeriod.query
        .filter_by(ended_at=None)
        .order_by(ActivePeriod.id.desc())
        .first()
    )


def _parse_hhmm(value, default):
    """'HH:MM' -> minutes since midnight, or `default` if unparseable."""
    if isinstance(value, str):
        m = HHMM_RE.match(value.strip())
        if m:
            return int(m.group(1)) * 60 + int(m.group(2))
    return default


@public_bp.route('/menu', methods=['GET'])
def get_menu():
    # M2: eager-load customization_types to avoid N+1 (one query per drink)
    drinks = (
        Drink.query
        .filter_by(enabled=True)
        .options(joinedload(Drink.customization_types))
        .all()
    )
    options = CustomizationOption.query.filter_by(enabled=True).order_by(CustomizationOption.id).all()

    customizations = {}
    for opt in options:
        customizations.setdefault(opt.type, []).append({
            'id': opt.id,
            'label': opt.label
        })

    # Per-drink option allowlists — when present for (drink, type), the client
    # filters the global customizations map to only these ids.
    override_rows = (
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
    override_maps = {}
    for drink_id, type_, opt_id in override_rows:
        override_maps.setdefault(drink_id, {}).setdefault(type_, []).append(opt_id)

    # S4: surface the orders_accepting flag so the client can show a
    # banner and block submission before hitting the 503 at /orders.
    accepting = db.session.get(Setting, 'orders_accepting')
    orders_accepting = bool(accepting.value) if accepting else True

    def _drink_dict(d):
        result = {
            'id': d.id,
            'name': d.name,
            'description': d.description,
            'ratio_summary': d.ratio_summary,
            'customization_types': [ct.customization_type for ct in d.customization_types],
        }
        if d.id in override_maps:
            result['allowed_customization_options'] = override_maps[d.id]
        return result

    return jsonify({
        'drinks': [_drink_dict(d) for d in drinks],
        'customizations': customizations,
        'orders_accepting': orders_accepting,
    })


@public_bp.route('/slots', methods=['GET'])
def get_slots():
    start_setting = db.session.get(Setting, 'business_hours_start')
    end_setting = db.session.get(Setting, 'business_hours_end')
    interval_setting = db.session.get(Setting, 'slot_interval_minutes')

    # A6: admin PATCH now validates these, but rows written before that (or
    # by hand in the DB) may still hold garbage. Fall back to the defaults
    # rather than 500 the whole order flow.
    start_minutes = _parse_hhmm(start_setting.value if start_setting else None, 8 * 60)
    end_minutes = _parse_hhmm(end_setting.value if end_setting else None, 17 * 60)
    # M7: settings values are JSON — coerce to int; fall back to 15 if the
    # stored value can't be parsed (avoids an infinite loop below).
    try:
        interval = int(interval_setting.value) if interval_setting else 15
    except (TypeError, ValueError):
        interval = 15
    if interval <= 0:
        interval = 15

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
    prefix = DAY_PREFIXES[business_today().day]
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


def _validate_customizations(value):
    """Return (cleaned_dict, None) or (None, error_message)."""
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, 'customizations must be an object.'
    if len(value) > _MAX_CUSTOMIZATION_KEYS:
        return None, f'customizations may have at most {_MAX_CUSTOMIZATION_KEYS} keys.'
    for key, val in value.items():
        if not isinstance(key, str) or not key or len(key) > _MAX_CUSTOMIZATION_KEY_LEN:
            return None, f'customization keys must be non-empty strings of {_MAX_CUSTOMIZATION_KEY_LEN} characters or fewer.'
        if isinstance(val, str):
            if len(val) > _MAX_CUSTOMIZATION_VALUE_LEN:
                return None, f'customization "{key}" must be {_MAX_CUSTOMIZATION_VALUE_LEN} characters or fewer.'
        elif val is not None and not isinstance(val, (bool, int, float)):
            return None, f'customization "{key}" must be a string, number, boolean, or null.'
    return value, None


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
    commit — this only stages deletes on the session.

    delete_after is the LAST day the location is shown (A9): a location is
    purged strictly after that date, in business-local time."""
    today = business_today()
    expired = Location.query.filter(
        Location.delete_after != None,
        Location.delete_after < today
    ).all()
    for loc in expired:
        db.session.delete(loc)
    return len(expired)


@public_bp.route('/locations', methods=['GET'])
def get_locations():
    # M5: GET stays idempotent — filter expired out in the query rather than
    # deleting them here. Actual cleanup runs when admin views their list
    # (see admin.get_all_locations).
    today = business_today()
    locations = (
        Location.query
        .filter_by(active=True)
        .filter(
            (Location.delete_after == None) | (Location.delete_after >= today)
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

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({'error': 'Request body is required.'}), 400

    # A7: a null or non-string field is a client error, not a 500 from .strip().
    for field in ('customer_name', 'phone_number', 'pickup_slot'):
        value = data.get(field)
        if value is not None and not isinstance(value, str):
            return jsonify({'error': f'{field} must be a string.'}), 400
    customer_name = (data.get('customer_name') or '').strip()
    phone_number = (data.get('phone_number') or '').strip()
    pickup_slot = (data.get('pickup_slot') or '').strip()
    items = data.get('items')
    if items is None:
        items = []

    if not customer_name or not phone_number or not pickup_slot:
        return jsonify({'error': 'customer_name, phone_number, and pickup_slot are required.'}), 400
    if len(customer_name) > 100:
        return jsonify({'error': 'customer_name must be 100 characters or fewer.'}), 400
    if len(phone_number) > 20:
        return jsonify({'error': 'phone_number must be 20 characters or fewer.'}), 400
    if len(pickup_slot) > 20:
        return jsonify({'error': 'pickup_slot must be 20 characters or fewer.'}), 400
    if not PICKUP_SLOT_RE.match(pickup_slot):
        return jsonify({'error': 'pickup_slot must look like "8:30 AM" or "12:15 PM".'}), 400
    if not _PHONE_ALLOWED_RE.match(phone_number) or sum(c.isdigit() for c in phone_number) < 7:
        return jsonify({'error': 'phone_number must contain 7–15 digits with only digits, spaces, dashes, parens, dots, or a leading +.'}), 400
    if not isinstance(items, list):
        return jsonify({'error': 'items must be a list.'}), 400
    if not items:
        return jsonify({'error': 'At least one item is required.'}), 400
    if len(items) > 20:
        return jsonify({'error': 'Maximum 20 items per order.'}), 400

    # Validate every item before writing anything, so the insert below is a
    # short transaction that can be retried wholesale.
    validated_items = []
    for item in items:
        if not isinstance(item, dict):
            return jsonify({'error': 'Each item must be an object.'}), 400
        drink_id = item.get('drink_id')
        if not isinstance(drink_id, int) or isinstance(drink_id, bool):
            return jsonify({'error': 'Each item must have a valid drink_id.'}), 400
        drink = db.session.get(Drink, drink_id)
        if not drink or not drink.enabled:
            return jsonify({'error': f'Drink {drink_id} is not available.'}), 400
        customizations, cust_error = _validate_customizations(item.get('customizations'))
        if cust_error:
            return jsonify({'error': cust_error}), 400
        validated_items.append((drink_id, customizations))

    # A15: the confirmation code is checked-then-inserted, and on SQLite the
    # period row lock is a no-op, so two simultaneous orders can still collide
    # on a unique constraint. Retry with a fresh code/number instead of 500ing.
    order = None
    for attempt in range(_MAX_ORDER_ATTEMPTS):
        try:
            order = _create_order(customer_name, phone_number, pickup_slot, validated_items)
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
            order = None
            logger.warning('Order insert collided on a unique constraint (attempt %d)', attempt + 1)
    if order is None:
        return jsonify({'error': 'Could not place your order right now. Please try again.'}), 503

    return jsonify({
        'order_number': order.order_number,
        'confirmation_code': order.confirmation_code,
        'message': 'Order placed successfully.'
    }), 201


def _create_order(customer_name, phone_number, pickup_slot, validated_items):
    """Stage one order (and a period, if none is open) on the session.
    The caller commits and handles IntegrityError."""
    period = get_current_period()
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
    for drink_id, customizations in validated_items:
        db.session.add(OrderItem(
            order_id=order.id,
            drink_id=drink_id,
            customizations=customizations
        ))
    return order


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
    # A13: orders from a closed session have been anonymized and exported;
    # their ANON-<id> codes are not customer-facing.
    if order.period is not None and order.period.ended_at is not None:
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
