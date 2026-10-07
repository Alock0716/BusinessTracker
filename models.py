from datetime import date, datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

product_tag_link = db.Table(
    'product_tag_link',
    db.Column('product_id', db.Integer, db.ForeignKey('product.id'), primary_key=True),
    db.Column('product_tag_id', db.Integer, db.ForeignKey('product_tag.id'), primary_key=True),
)

class CreatorAccount(db.Model):
    __tablename__ = 'creator_account'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    display_name = db.Column(db.String(120))
    bio = db.Column(db.Text)
    is_active = db.Column(db.Boolean, nullable=False, default=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    is_approved = db.Column(db.Boolean, nullable=False, default=False)
    stripe_account_id = db.Column(db.String(255), unique=True)
    stripe_customer_id = db.Column(db.String(255))
    stripe_subscription_id = db.Column(db.String(255))
    stripe_subscription_checkout_session_id = db.Column(db.String(255))
    stripe_subscription_status = db.Column(db.String(40), nullable=False, default='inactive')
    stripe_subscription_period_end = db.Column(db.DateTime)
    subscription_exempt = db.Column(db.Boolean, nullable=False, default=False)
    stripe_charges_enabled = db.Column(db.Boolean, nullable=False, default=False)
    stripe_details_submitted = db.Column(db.Boolean, nullable=False, default=False)
    accepting_orders = db.Column(db.Boolean, nullable=False, default=False)
    banner_notifications = db.Column(db.Boolean, nullable=False, default=True)
    email_notifications = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.Index('uq_creator_stripe_customer_id', 'stripe_customer_id', unique=True),
        db.Index('uq_creator_stripe_subscription_id', 'stripe_subscription_id', unique=True),
    )
    __mapper_args__ = {'version_id_col': version_id}

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('owner_id', 'name', name='uq_category_owner_name'),)
    __mapper_args__ = {'version_id_col': version_id}
    products = db.relationship('Product', backref='category', lazy=True)

class Platform(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('owner_id', 'name', name='uq_platform_owner_name'),)
    __mapper_args__ = {'version_id_col': version_id}

class PaymentMethod(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    active = db.Column(db.Boolean, default=True)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.UniqueConstraint('owner_id', 'name', name='uq_payment_method_owner_name'),)
    __mapper_args__ = {'version_id_col': version_id}

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(160))
    username = db.Column(db.String(160))
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (db.Index('ix_customer_owner_name', 'owner_id', 'name'),)
    __mapper_args__ = {'version_id_col': version_id}
    sales = db.relationship('Sale', backref='customer', lazy=True)
    leaderboard_entries = db.relationship('LeaderboardEntry', back_populates='customer', cascade='all, delete-orphan', lazy=True)
    service_subscriptions = db.relationship('ServiceSubscription', back_populates='customer', lazy=True)

class StoreCustomer(db.Model):
    __tablename__ = 'store_customer'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True)
    display_name = db.Column(db.String(160), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    sales = db.relationship('Sale', back_populates='store_customer', lazy=True)

class Leaderboard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    score_label = db.Column(db.String(80), nullable=False, default='Score')
    sort_order = db.Column(db.String(4), nullable=False, default='desc')
    is_public = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.Index('ix_leaderboard_owner_name', 'owner_id', 'name'),
        db.Index('ix_leaderboard_public', 'is_public', 'owner_id'),
    )
    __mapper_args__ = {'version_id_col': version_id}
    entries = db.relationship('LeaderboardEntry', back_populates='leaderboard', cascade='all, delete-orphan', lazy=True)

class LeaderboardEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    leaderboard_id = db.Column(db.Integer, db.ForeignKey('leaderboard.id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    score = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    use_username = db.Column(db.Boolean, nullable=False, default=False)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.UniqueConstraint('leaderboard_id', 'customer_id', name='uq_leaderboard_customer'),
        db.Index('ix_leaderboard_entry_score', 'leaderboard_id', 'score'),
    )
    __mapper_args__ = {'version_id_col': version_id}
    leaderboard = db.relationship('Leaderboard', back_populates='entries')
    customer = db.relationship('Customer', back_populates='leaderboard_entries')

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text)
    base_price = db.Column(db.Numeric(12,2), default=0)
    base_price_enabled = db.Column(db.Boolean, nullable=False, default=True)
    quantity_label = db.Column(db.String(40), nullable=False, default='Quantity')
    active = db.Column(db.Boolean, default=True)
    is_public = db.Column(db.Boolean, nullable=False, default=True)
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.UniqueConstraint('owner_id', 'name', name='uq_product_owner_name'),
        db.Index('ix_product_owner_active_name', 'owner_id', 'active', 'name'),
    )
    __mapper_args__ = {'version_id_col': version_id}
    fields = db.relationship('ProductField', backref='product', cascade='all, delete-orphan', lazy=True)
    addons = db.relationship('ProductAddon', backref='product', cascade='all, delete-orphan', lazy=True)
    bundles = db.relationship('ProductBundle', backref='product', cascade='all, delete-orphan', lazy=True)
    tags = db.relationship('ProductTag', secondary=product_tag_link, back_populates='products')
    items = db.relationship('SaleItem', backref='product', lazy=True)
    service_subscriptions = db.relationship('ServiceSubscription', back_populates='product', lazy=True)

class ProductField(db.Model):
    """Product-specific information to capture when recording a sale."""
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    field_type = db.Column(db.String(30), nullable=False, default='text')
    unit = db.Column(db.String(30))
    required = db.Column(db.Boolean, default=False)
    __table_args__ = (db.Index('ix_product_field_product', 'product_id'),)

class ProductAddon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(12,2), default=0)
    price_mode = db.Column(db.String(20), nullable=False, default='per_quantity')
    quantity_unit = db.Column(db.String(40))
    labor_hours = db.Column(db.Numeric(12,2), default=0)
    active = db.Column(db.Boolean, default=True)
    __table_args__ = (db.Index('ix_product_addon_product_active', 'product_id', 'active'),)

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
    __table_args__ = (db.Index('ix_product_bundle_product_active', 'product_id', 'active'),)

class ServiceSubscription(db.Model):
    __tablename__ = 'service_subscription'
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='active')
    started_at = db.Column(db.Date, nullable=False, default=date.today)
    current_period_end = db.Column(db.Date, nullable=False)
    last_notified_period_end = db.Column(db.Date)
    notes = db.Column(db.Text)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.Index('ix_service_subscription_owner_status_end', 'owner_id', 'status', 'current_period_end'),
        db.Index('ix_service_subscription_customer', 'customer_id'),
    )
    __mapper_args__ = {'version_id_col': version_id}
    customer = db.relationship('Customer', back_populates='service_subscriptions')
    product = db.relationship('Product', back_populates='service_subscriptions')

class ProductTag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    name = db.Column(db.String(40), nullable=False)
    __table_args__ = (
        db.UniqueConstraint('owner_id', 'name', name='uq_product_tag_owner_name'),
        db.Index('ix_product_tag_name', 'name'),
    )
    products = db.relationship('Product', secondary=product_tag_link, back_populates='tags')

class Sale(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    sale_date = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    due_date = db.Column(db.Date)
    sale_category = db.Column(db.String(120))
    status = db.Column(db.String(30), nullable=False, default='New')
    paid = db.Column(db.Boolean, nullable=False, default=False)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'))
    store_customer_id = db.Column(db.Integer, db.ForeignKey('store_customer.id'))
    platform_id = db.Column(db.Integer, db.ForeignKey('platform.id'))
    payment_method_id = db.Column(db.Integer, db.ForeignKey('payment_method.id'))
    payment_detail = db.Column(db.String(255))
    stripe_checkout_session_id = db.Column(db.String(255), unique=True)
    stripe_payment_intent_id = db.Column(db.String(255))
    total_amount = db.Column(db.Numeric(12,2), default=0)
    notes = db.Column(db.Text)
    version_id = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (
        db.Index('ix_sale_owner_created', 'owner_id', 'created_at', 'id'),
        db.Index('ix_sale_owner_due', 'owner_id', 'due_date'),
        db.Index('ix_sale_owner_status_paid', 'owner_id', 'status', 'paid'),
        db.Index('ix_sale_owner_customer', 'owner_id', 'customer_id'),
    )
    __mapper_args__ = {'version_id_col': version_id}
    platform = db.relationship('Platform')
    payment_method = db.relationship('PaymentMethod')
    store_customer = db.relationship('StoreCustomer', back_populates='sales')
    items = db.relationship('SaleItem', backref='sale', cascade='all, delete-orphan', lazy=True)

class SaleItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    bundle_id = db.Column(db.Integer, db.ForeignKey('product_bundle.id'))
    quantity = db.Column(db.Numeric(12,2), default=1)
    unit_price = db.Column(db.Numeric(12,2), default=0)
    labor_hours = db.Column(db.Numeric(12,2), default=0)
    __table_args__ = (
        db.Index('ix_sale_item_sale', 'sale_id'),
        db.Index('ix_sale_item_product', 'product_id'),
    )
    bundle = db.relationship('ProductBundle')
    fields = db.relationship('SaleItemField', backref='sale_item', cascade='all, delete-orphan', lazy=True)
    addons = db.relationship('SaleItemAddon', backref='sale_item', cascade='all, delete-orphan', lazy=True)

class SaleItemField(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('sale_item.id'), nullable=False)
    product_field_id = db.Column(db.Integer, db.ForeignKey('product_field.id'), nullable=False)
    value = db.Column(db.Text)
    __table_args__ = (db.Index('ix_sale_item_field_item', 'sale_item_id'),)
    product_field = db.relationship('ProductField')

class SaleItemAddon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sale_item_id = db.Column(db.Integer, db.ForeignKey('sale_item.id'), nullable=False)
    product_addon_id = db.Column(db.Integer, db.ForeignKey('product_addon.id'), nullable=False)
    quantity = db.Column(db.Numeric(12,2), default=1)
    unit_price = db.Column(db.Numeric(12,2), default=0)
    price_mode = db.Column(db.String(20), nullable=False, default='per_quantity')
    __table_args__ = (db.Index('ix_sale_item_addon_item', 'sale_item_id'),)
    product_addon = db.relationship('ProductAddon')

class SellerNotification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('creator_account.id'), nullable=False)
    sale_id = db.Column(db.Integer, db.ForeignKey('sale.id', ondelete='SET NULL'))
    service_subscription_id = db.Column(db.Integer, db.ForeignKey('service_subscription.id', ondelete='SET NULL'))
    title = db.Column(db.String(160), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    read_at = db.Column(db.DateTime)
    __table_args__ = (
        db.Index('ix_notification_owner_read_created', 'owner_id', 'read_at', 'created_at'),
        db.Index('ix_notification_service_subscription', 'service_subscription_id'),
    )
