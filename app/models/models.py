from app import db
from datetime import datetime, timezone


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


class CustomizationOption(db.Model):
    __tablename__ = 'customization_options'

    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)
    label = db.Column(db.String(100), nullable=False)
    enabled = db.Column(db.Boolean, default=True)


class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    confirmation_code = db.Column(db.String(10), unique=True, nullable=False)
    customer_name = db.Column(db.String(100), nullable=False)
    phone_number = db.Column(db.String(20), nullable=False)
    pickup_slot = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), default='received')
    period_id = db.Column(db.Integer, db.ForeignKey('active_periods.id'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')


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
