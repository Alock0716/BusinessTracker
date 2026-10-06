from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    description = db.Column(db.Text)
    products = db.relationship('Product', backref='category', lazy=True)

class Platform(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    active = db.Column(db.Boolean, default=True)

class PaymentMethod(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    active = db.Column(db.Boolean, default=True)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(160))
    username = db.Column(db.String(160))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sales = db.relationship('Sale', backref='customer', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    description = db.Column(db.Text)
    base_price = db.Column(db.Numeric(12,2), default=0)
    active = db.Column(db.Boolean, default=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    fields = db.relationship('ProductField', backref='product', cascade='all, delete-orphan', lazy=True)
    addons = db.relationship('ProductAddon', backref='product', cascade='all, delete-orphan', lazy=True)
    bundles = db.relationship('ProductBundle', backref='product', cascade='all, delete-orphan', lazy=True)
    items = db.relationship('SaleItem', backref='product', lazy=True)

class ProductField(db.Model):
    """Product-specific information to capture when recording a sale."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    field_type = db.Column(db.String(30), nullable=False, default='text')
    unit = db.Column(db.String(30))
    required = db.Column(db.Boolean, default=False)

class ProductAddon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(12,2), default=0)
    labor_hours = db.Column(db.Numeric(12,2), default=0)
    active = db.Column(db.Boolean, default=True)

class ProductBundle(db.Model):
    """A named price option for a product at a different quantity, duration, or unit."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    amount = db.Column(db.Numeric(12,2), default=1)
    unit = db.Column(db.String(40))
    price = db.Column(db.Numeric(12,2), default=0)
    labor_hours = db.Column(db.Numeric(12,2), default=0)
    active = db.Column(db.Boolean, default=True)

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    platform_id = db.Column(db.Integer, db.ForeignKey('platform.id'))
    payment_method_id = db.Column(db.Integer, db.ForeignKey('payment_method.id'))
    payment_detail = db.Column(db.String(255))
    total_amount = db.Column(db.Numeric(12,2), default=0)
    notes = db.Column(db.Text)
    platform = db.relationship('Platform')
    payment_method = db.relationship('PaymentMethod')
    items = db.relationship('SaleItem', backref='sale', cascade='all, delete-orphan', lazy=True)

class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    bundle_id = db.Column(db.Integer, db.ForeignKey('product_bundle.id'))
    quantity = db.Column(db.Numeric(12,2), default=1)
    unit_price = db.Column(db.Numeric(12,2), default=0)
    labor_hours = db.Column(db.Numeric(12,2), default=0)
    bundle = db.relationship('ProductBundle')
    fields = db.relationship('SaleItemField', backref='sale_item', cascade='all, delete-orphan', lazy=True)
    addons = db.relationship('SaleItemAddon', backref='sale_item', cascade='all, delete-orphan', lazy=True)

class SaleItemField(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('sale_item.id'), nullable=False)
    product_field_id = db.Column(db.Integer, db.ForeignKey('product_field.id'), nullable=False)
    value = db.Column(db.Text)
    product_field = db.relationship('ProductField')

class SaleItemAddon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('sale_item.id'), nullable=False)
    product_addon_id = db.Column(db.Integer, db.ForeignKey('product_addon.id'), nullable=False)
    quantity = db.Column(db.Numeric(12,2), default=1)
    unit_price = db.Column(db.Numeric(12,2), default=0)
    product_addon = db.relationship('ProductAddon')
