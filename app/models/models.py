from app import db
from datetime import datetime, timezone, date


class ActivePeriod(db.Model):
    __tablename__ = 'active_periods'

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ended_at = db.Column(db.DateTime(timezone=True), nullable=True)

    orders = db.relationship('Order', backref='period', lazy=True)


class Drink(db.Model):
    __tablename__ = 'drinks'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text)
    ratio_summary = db.Column(db.Text)
    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    customization_types = db.relationship('DrinkCustomizationType', backref='drink', lazy=True, cascade='all, delete-orphan')
    option_overrides = db.relationship('DrinkCustomizationOption', backref='drink', lazy=True, cascade='all, delete-orphan')


class DrinkCustomizationType(db.Model):
    __tablename__ = 'drink_customization_types'

    id = db.Column(db.Integer, primary_key=True)
    drink_id = db.Column(db.Integer, db.ForeignKey('drinks.id'), nullable=False)
    customization_type = db.Column(db.String(50), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('drink_id', 'customization_type', name='uq_drink_cust_type'),
    )


class CustomizationOption(db.Model):
    __tablename__ = 'customization_options'

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    enabled = db.Column(db.Boolean, default=True)


class DrinkCustomizationOption(db.Model):
    """Per-drink allowlist override: if any rows exist for (drink, type),
    only the listed options apply to that drink. Absence of rows means
    all globally-enabled options of that type apply (default behavior)."""

    __tablename__ = 'drink_customization_options'

    id = db.Column(db.Integer, primary_key=True)
    drink_id = db.Column(db.Integer, db.ForeignKey('drinks.id'), nullable=False)
    customization_option_id = db.Column(
        db.Integer, db.ForeignKey('customization_options.id'), nullable=False
    )

    __table_args__ = (
        db.UniqueConstraint('drink_id', 'customization_option_id', name='uq_drink_option'),
    )


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.Integer, nullable=False)
    confirmation_code = db.Column(db.String(12), unique=True, nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    pickup_slot = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='queued')
    period_id = db.Column(db.Integer, db.ForeignKey('active_periods.id'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('order_number', 'period_id', name='uq_order_number_period'),
    )


class OrderItem(db.Model):
    __tablename__ = 'order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    drink_id = db.Column(db.Integer, db.ForeignKey('drinks.id'), nullable=False)
    customizations = db.Column(db.JSON)

    drink = db.relationship('Drink', lazy=True)


class Setting(db.Model):
    __tablename__ = 'settings'

    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.JSON)


class Location(db.Model):
    __tablename__ = 'locations'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    address = db.Column(db.String(300), nullable=False)
    active = db.Column(db.Boolean, default=True)
    delete_after = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    dates = db.relationship('LocationDate', backref='location', lazy=True, cascade='all, delete-orphan')


class LocationDate(db.Model):
    __tablename__ = 'location_dates'

    id = db.Column(db.Integer, primary_key=True)
    location_id = db.Column(db.Integer, db.ForeignKey('locations.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('location_id', 'date', name='uq_location_date'),
    )
