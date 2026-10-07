from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.message import EmailMessage
import json
import logging
import re
import smtplib
import ssl
from io import BytesIO
from urllib.parse import urlsplit
import stripe
from flask import Flask, abort, g, has_request_context, render_template, request, redirect, send_file, url_for, flash, session
from sqlalchemy import case, event, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload, with_loader_criteria
from sqlalchemy.orm.exc import StaleDataError
from werkzeug.security import check_password_hash, generate_password_hash
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from config import Config
from models import db, CreatorAccount, StoreCustomer, SellerNotification, Category, Platform, PaymentMethod, Customer, Leaderboard, LeaderboardEntry, Product, ProductTag, ProductField, ProductAddon, ProductBundle, Sale, SaleItem, SaleItemField, SaleItemAddon

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)

LOGIN_USERNAME = app.config.get('LOGIN_USERNAME', 'admin')
LOGIN_PASSWORD = app.config.get('LOGIN_PASSWORD', 'change-me')
TENANT_MODELS = (Category, Platform, PaymentMethod, Customer, Leaderboard, Product, ProductTag, Sale, SellerNotification)
PUBLIC_ENDPOINTS = {
    'login', 'register', 'logout', 'storefront', 'public_leaderboards', 'seller_store',
    'public_leaderboard', 'store_customer_login', 'store_customer_register',
    'store_customer_logout', 'store_customer_account', 'forgot_password', 'reset_password', 'public_product_checkout',
    'checkout_success', 'checkout_cancel', 'stripe_webhook', 'static',
}
SALE_STATUSES = ('New', 'In Progress', 'Not Started', 'On-Hold', 'Waiting on Payment', 'Canceled', 'Completed')
ACTIVE_SUBSCRIPTION_STATUSES = ('active', 'trialing')
BILLING_ACCESS_ENDPOINTS = {
    'seller_billing', 'seller_billing_checkout', 'seller_billing_portal', 'account_settings',
}

def _seller_subscription_access():
    return or_(
        CreatorAccount.is_admin.is_(True),
        CreatorAccount.subscription_exempt.is_(True),
        CreatorAccount.stripe_subscription_status.in_(ACTIVE_SUBSCRIPTION_STATUSES),
    )

@event.listens_for(Session, 'do_orm_execute')
def scope_creator_queries(execute_state):
    if not execute_state.is_select or not execute_state.is_orm_statement or not has_request_context():
        return
    creator_id = getattr(g, 'creator_id', None)
    if getattr(g, 'is_admin', False):
        return
    if not creator_id or request.endpoint in PUBLIC_ENDPOINTS:
        return
    execute_state.statement = execute_state.statement.options(*(
        with_loader_criteria(model, lambda entity: entity.owner_id == creator_id, include_aliases=True)
        for model in TENANT_MODELS
    ))

@app.before_request
def require_creator():
    if request.endpoint in PUBLIC_ENDPOINTS:
        store_customer_id = session.get('store_customer_id')
        g.store_customer = db.session.get(StoreCustomer, store_customer_id) if store_customer_id else None
        creator_id = session.get('creator_id')
        if creator_id:
            creator = db.session.get(CreatorAccount, creator_id)
            if creator and creator.is_approved:
                g.creator = creator
                g.is_admin = creator.is_admin
            else:
                session.clear()
        return
    creator_id = session.get('creator_id')
    creator = db.session.get(CreatorAccount, creator_id) if creator_id else None
    if creator is None or not creator.is_approved:
        session.clear()
        return redirect(url_for('login', next=request.full_path))
    g.creator = creator
    g.creator_id = creator.id
    g.is_admin = creator.is_admin
    if request.endpoint and request.endpoint.startswith('admin_') and not creator.is_admin:
        abort(403)
    if not creator.is_admin and not creator.subscription_exempt and creator.stripe_subscription_status not in ACTIVE_SUBSCRIPTION_STATUSES:
        if request.endpoint not in BILLING_ACCESS_ENDPOINTS:
            return redirect(url_for('seller_billing'))

def _write_conflict_response(message):
    db.session.rollback()
    if request.endpoint == 'stripe_webhook':
        return '', 409
    flash(message, 'error')
    referrer = request.referrer
    if referrer and urlsplit(referrer).netloc == request.host:
        return redirect(referrer)
    return redirect(url_for('dashboard'))

@app.errorhandler(StaleDataError)
def stale_write_error(error):
    db.session.rollback()
    sale_endpoints = {'sale_edit', 'sale_delete', 'sale_status_update', 'sale_paid_toggle'}
    view_args = request.view_args or {}
    if request.endpoint in sale_endpoints and getattr(g, 'creator_id', None):
        sale_id = view_args.get('id')
        latest = Sale.query.filter_by(id=sale_id, owner_id=g.creator_id).first()
        submitted_version = request.form.get('version_id', type=int)
        if latest is not None and submitted_version is not None:
            message = (
                f'Sale #{latest.id} changed during save (form version {submitted_version}; '
                f'latest version {latest.version_id}). Your changes were not saved. '
                'Reload the sale and reapply your edits.'
            )
        else:
            message = 'This sale changed during save. Reload it and reapply your edits.'
        logging.warning('Stale sale write on %s for sale id %s', request.endpoint, sale_id)
    else:
        message = 'This record changed during save. Reload it and reapply your edits.'
    return _write_conflict_response(message)

@app.errorhandler(IntegrityError)
def integrity_write_error(error):
    message = 'The save conflicts with existing data or a database constraint. Reload the page and try again.'
    return _write_conflict_response(message)

def _form_version_matches(record):
    submitted_version = request.form.get('version_id', type=int)
    if submitted_version is not None and submitted_version != record.version_id:
        flash(
            f'This record changed in another session (form version {submitted_version}; '
            f'current version {record.version_id}). Your changes were not saved. '
            'Reopen the current sale and reapply your edits.',
            'error',
        )
        return False
    return True

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('creator_id'):
        return redirect(url_for('dashboard'))
    if session.get('store_customer_id'):
        return redirect(url_for('store_customer_account'))
    if request.method == 'POST':
        identity = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        creator = CreatorAccount.query.filter(or_(
            func.lower(CreatorAccount.email) == identity,
            func.lower(CreatorAccount.username) == identity,
        )).first()
        buyer = StoreCustomer.query.filter(func.lower(StoreCustomer.email) == identity).first()
        if creator and check_password_hash(creator.password_hash, password):
            if not creator.is_approved:
                flash('Your seller account is awaiting admin approval.', 'error')
            else:
                session.clear()
                session['creator_id'] = creator.id
                next_url = request.form.get('next', '')
                if not next_url or not next_url.startswith('/') or next_url.startswith('//'):
                    next_url = url_for('dashboard')
                return redirect(next_url)
        elif buyer and check_password_hash(buyer.password_hash, password):
            session.clear()
            session['store_customer_id'] = buyer.id
            return redirect(url_for('store_customer_account'))
        else:
            flash('Invalid email or password.', 'error')
    next_url = request.form.get('next', '') if request.method == 'POST' else request.args.get('next', '')
    return render_template('login.html', next=next_url)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if session.get('creator_id'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirmation = request.form.get('password_confirm', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', username):
            flash('Choose a username with 3 to 64 letters, numbers, dots, dashes, or underscores.', 'error')
        elif not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            flash('Enter a valid email address.', 'error')
        elif CreatorAccount.query.filter(func.lower(CreatorAccount.email) == email).first() or StoreCustomer.query.filter(func.lower(StoreCustomer.email) == email).first():
            flash('That email is already in use.', 'error')
        elif CreatorAccount.query.filter_by(username=username).first():
            flash('That username is already taken.', 'error')
        elif len(password) < 10:
            flash('Use a password with at least 10 characters.', 'error')
        elif password != confirmation:
            flash('The passwords do not match.', 'error')
        else:
            creator = CreatorAccount(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                is_admin=False,
                is_approved=False,
            )
            db.session.add(creator)
            db.session.flush()
            db.session.add_all([
                Category(owner_id=creator.id, name=name)
                for name in ('General', 'Services', 'Products')
            ])
            db.session.add_all([
                Platform(owner_id=creator.id, name=name)
                for name in ('Website', 'Etsy', 'In Person', 'Other')
            ])
            db.session.add_all([
                PaymentMethod(owner_id=creator.id, name=name)
                for name in ('Cash', 'Card', 'Gift Card', 'Product Exchange')
            ])
            db.session.commit()
            flash('Account submitted. An admin must approve it before you can manage a storefront.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('storefront'))

@app.route('/customer/register', methods=['GET', 'POST'])
def store_customer_register():
    if g.store_customer:
        return redirect(url_for('store_customer_account'))
    if request.method == 'POST':
        display_name = request.form.get('display_name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirmation = request.form.get('password_confirm', '')
        if not display_name:
            flash('Enter your name.', 'error')
        elif not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            flash('Enter a valid email address.', 'error')
        elif StoreCustomer.query.filter(func.lower(StoreCustomer.email) == email).first() or CreatorAccount.query.filter(func.lower(CreatorAccount.email) == email).first():
            flash('An account already uses that email.', 'error')
        elif len(password) < 10:
            flash('Use a password with at least 10 characters.', 'error')
        elif password != confirmation:
            flash('The passwords do not match.', 'error')
        else:
            buyer = StoreCustomer(
                display_name=display_name, email=email,
                password_hash=generate_password_hash(password),
            )
            db.session.add(buyer)
            db.session.commit()
            session['store_customer_id'] = buyer.id
            flash('Buyer account created.', 'success')
            return redirect(url_for('store_customer_account'))
    return render_template('customer_register.html')

@app.route('/customer/login', methods=['GET', 'POST'])
def store_customer_login():
    return redirect(url_for('login', next=request.args.get('next', '')))

@app.route('/customer/logout')
def store_customer_logout():
    return logout()

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        creator = CreatorAccount.query.filter(func.lower(CreatorAccount.email) == email).first()
        buyer = StoreCustomer.query.filter(func.lower(StoreCustomer.email) == email).first()
        account_type = 'seller' if creator else 'buyer' if buyer else None
        account = creator or buyer
        if account and not account.email.endswith('@example.invalid'):
            serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='password-reset-v1')
            token = serializer.dumps({'type': account_type, 'id': account.id})
            reset_url = url_for('reset_password', token=token, _external=True)
            _send_email(
                account.email,
                'Reset your Business Tracker password',
                f'Use this link within one hour to reset your password:\n\n{reset_url}\n\nIf you did not request this, ignore this email.',
            )
        flash('If an account exists for that email, a password reset link will be sent.', 'success')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='password-reset-v1')
    try:
        payload = serializer.loads(token, max_age=3600)
        model = CreatorAccount if payload.get('type') == 'seller' else StoreCustomer if payload.get('type') == 'buyer' else None
        account = db.session.get(model, payload.get('id')) if model else None
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        account = None
    if account is None:
        flash('That password reset link is invalid or has expired.', 'error')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirmation = request.form.get('password_confirm', '')
        if len(password) < 10:
            flash('Use a password with at least 10 characters.', 'error')
        elif password != confirmation:
            flash('The passwords do not match.', 'error')
        else:
            account.password_hash = generate_password_hash(password)
            db.session.commit()
            flash('Password updated. Sign in with your new password.', 'success')
            return redirect(url_for('login'))
    return render_template('reset_password.html')

@app.route('/customer/account')
def store_customer_account():
    buyer = g.store_customer
    if buyer is None:
        return redirect(url_for('store_customer_login'))
    purchases = db.session.query(Sale, CreatorAccount).join(
        CreatorAccount, CreatorAccount.id == Sale.owner_id
    ).filter(Sale.store_customer_id == buyer.id).order_by(Sale.created_at.desc()).all()
    return render_template('customer_account.html', buyer=buyer, purchases=purchases)

@app.route('/notifications')
def notifications():
    rows = SellerNotification.query.filter_by(owner_id=g.creator.id).order_by(
        SellerNotification.created_at.desc()
    ).limit(100).all()
    return render_template('notifications.html', notifications=rows)

@app.route('/notifications/read-all', methods=['POST'])
def notifications_read_all():
    SellerNotification.query.filter_by(owner_id=g.creator.id, read_at=None).update(
        {SellerNotification.read_at: datetime.utcnow()}, synchronize_session=False
    )
    db.session.commit()
    return redirect(url_for('notifications'))

@app.route('/notifications/<int:id>/read', methods=['POST'])
def notification_read(id):
    notification = SellerNotification.query.filter_by(id=id, owner_id=g.creator.id).first_or_404()
    if notification.read_at is None:
        notification.read_at = datetime.utcnow()
        db.session.commit()
    return redirect(request.form.get('next') or url_for('notifications'))

TENANT_TABLES = ('category', 'platform', 'payment_method', 'customer', 'leaderboard', 'product', 'product_tag', 'sale')
TENANT_NAME_TABLES = {
    'category': (
        'id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES creator_account(id), '
        'name VARCHAR(120) NOT NULL, description TEXT, '
        'CONSTRAINT uq_category_owner_name UNIQUE (owner_id, name)',
        'id, owner_id, name, description',
    ),
    'platform': (
        'id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES creator_account(id), '
        'name VARCHAR(120) NOT NULL, active BOOLEAN, '
        'CONSTRAINT uq_platform_owner_name UNIQUE (owner_id, name)',
        'id, owner_id, name, active',
    ),
    'payment_method': (
        'id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES creator_account(id), '
        'name VARCHAR(120) NOT NULL, active BOOLEAN, '
        'CONSTRAINT uq_payment_method_owner_name UNIQUE (owner_id, name)',
        'id, owner_id, name, active',
    ),
    'product': (
        'id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES creator_account(id), '
        'name VARCHAR(160) NOT NULL, description TEXT, base_price NUMERIC(12, 2), '
        'active BOOLEAN, category_id INTEGER REFERENCES category(id), created_at DATETIME, '
        'CONSTRAINT uq_product_owner_name UNIQUE (owner_id, name)',
        'id, owner_id, name, description, base_price, active, category_id, created_at',
    ),
    'product_tag': (
        'id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES creator_account(id), '
        'name VARCHAR(40) NOT NULL, '
        'CONSTRAINT uq_product_tag_owner_name UNIQUE (owner_id, name)',
        'id, owner_id, name',
    ),
}

def _sqlite_rebuild_tenant_table(table, definition, columns):
    connection = db.engine.connect()
    try:
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
        connection.commit()
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        temporary_table = f'{table}_tenant_migration'
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS {temporary_table}')
        connection.exec_driver_sql(f'CREATE TABLE {temporary_table} ({definition})')
        connection.exec_driver_sql(
            f'INSERT INTO {temporary_table} ({columns}) SELECT {columns} FROM {table}'
        )
        connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql(f'ALTER TABLE {temporary_table} RENAME TO {table}')
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.commit()
        connection.close()

def _migrate_tenant_schema(legacy_creator_id):
    for table in TENANT_TABLES:
        columns = {column['name'] for column in inspect(db.engine).get_columns(table)}
        if 'owner_id' not in columns:
            db.session.execute(text(
                f'ALTER TABLE {table} ADD COLUMN owner_id INTEGER REFERENCES creator_account(id)'
            ))
        db.session.execute(text(
            f'UPDATE {table} SET owner_id = :owner_id WHERE owner_id IS NULL'
        ), {'owner_id': legacy_creator_id})
    if db.engine.dialect.name == 'postgresql':
        for table in TENANT_TABLES:
            db.session.execute(text(
                f'ALTER TABLE "{table}" ALTER COLUMN owner_id SET NOT NULL'
            ))
    db.session.commit()

    inspector = inspect(db.engine)
    for table, (definition, columns) in TENANT_NAME_TABLES.items():
        unique_constraints = inspector.get_unique_constraints(table)
        global_name_constraints = [
            constraint for constraint in unique_constraints
            if constraint.get('column_names') == ['name']
        ]
        if db.engine.dialect.name == 'sqlite':
            with db.engine.connect() as connection:
                global_name_index = any(
                    index[2] and [column[2] for column in connection.exec_driver_sql(
                        f'PRAGMA index_info("{index[1]}")'
                    )] == ['name']
                    for index in connection.exec_driver_sql(f'PRAGMA index_list("{table}")')
                )
            if global_name_constraints or global_name_index:
                _sqlite_rebuild_tenant_table(table, definition, columns)
            continue
        if db.engine.dialect.name == 'mysql':
            global_name_indexes = {
                constraint['name'] for constraint in global_name_constraints if constraint.get('name')
            }
            global_name_indexes.update(
                index['name'] for index in inspector.get_indexes(table)
                if index.get('unique') and index.get('column_names') == ['name']
            )
            for index_name in global_name_indexes:
                db.session.execute(text(f'ALTER TABLE {table} DROP INDEX `{index_name}`'))
        else:
            constraint_names = {
                constraint['name'] for constraint in global_name_constraints if constraint.get('name')
            }
            for constraint_name in constraint_names:
                db.session.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT "{constraint_name}"'))
            for index in inspector.get_indexes(table):
                if index.get('unique') and index.get('column_names') == ['name'] and index['name'] not in constraint_names:
                    db.session.execute(text(f'DROP INDEX "{index["name"]}"'))
        if not any(
            constraint.get('column_names') == ['owner_id', 'name']
            for constraint in unique_constraints
        ):
            constraint_name = f'uq_{table}_owner_name'
            db.session.execute(text(
                f'ALTER TABLE {table} ADD CONSTRAINT {constraint_name} UNIQUE (owner_id, name)'
            ))
    db.session.commit()

def _migration_datetime_type(dialect_name):
    return 'TIMESTAMP' if dialect_name == 'postgresql' else 'DATETIME'

with app.app_context():
    db.create_all()
    if db.engine.dialect.name == 'sqlite':
        with db.engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA busy_timeout=30000')
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
            connection.commit()
    versioned_tables = (
        'creator_account', 'category', 'platform', 'payment_method', 'customer',
        'leaderboard', 'leaderboard_entry', 'product', 'product_tag', 'sale',
    )
    for table_name in versioned_tables:
        columns = {column['name'] for column in inspect(db.engine).get_columns(table_name)}
        if 'version_id' not in columns:
            db.session.execute(text(
                f'ALTER TABLE {table_name} ADD COLUMN version_id INTEGER NOT NULL DEFAULT 1'
            ))
    db.session.commit()
    legacy_username = LOGIN_USERNAME.strip().lower()
    creator_columns = {column['name'] for column in inspect(db.engine).get_columns('creator_account')}
    needs_admin_bootstrap = 'is_admin' not in creator_columns
    if needs_admin_bootstrap:
        db.session.execute(text(
            'ALTER TABLE creator_account ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT FALSE'
        ))
    if 'is_approved' not in creator_columns:
        db.session.execute(text(
            'ALTER TABLE creator_account ADD COLUMN is_approved BOOLEAN NOT NULL DEFAULT TRUE'
        ))
        needs_admin_bootstrap = True
    if 'display_name' not in creator_columns:
        db.session.execute(text(
            'ALTER TABLE creator_account ADD COLUMN display_name VARCHAR(120)'
        ))
    creator_email_definition = 'VARCHAR(255)'
    if 'email' not in creator_columns:
        db.session.execute(text(
            f'ALTER TABLE creator_account ADD COLUMN email {creator_email_definition}'
        ))
    creator_preference_migrations = {
        'banner_notifications': 'BOOLEAN NOT NULL DEFAULT TRUE',
        'email_notifications': 'BOOLEAN NOT NULL DEFAULT FALSE',
    }
    for column_name, definition in creator_preference_migrations.items():
        if column_name not in creator_columns:
            db.session.execute(text(
                f'ALTER TABLE creator_account ADD COLUMN {column_name} {definition}'
            ))
    datetime_column_type = _migration_datetime_type(db.engine.dialect.name)
    creator_stripe_migrations = {
        'stripe_account_id': 'VARCHAR(255)',
        'stripe_customer_id': 'VARCHAR(255)',
        'stripe_subscription_id': 'VARCHAR(255)',
        'stripe_subscription_checkout_session_id': 'VARCHAR(255)',
        'stripe_subscription_status': "VARCHAR(40) NOT NULL DEFAULT 'inactive'",
        'stripe_subscription_period_end': datetime_column_type,
        'subscription_exempt': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'stripe_charges_enabled': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'stripe_details_submitted': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'accepting_orders': 'BOOLEAN NOT NULL DEFAULT FALSE',
    }
    for column_name, definition in creator_stripe_migrations.items():
        if column_name not in creator_columns:
            db.session.execute(text(
                f'ALTER TABLE creator_account ADD COLUMN {column_name} {definition}'
            ))
    if db.engine.dialect.name == 'mysql':
        db.session.execute(text(
            "UPDATE creator_account SET email = CONCAT(LOWER(username), '@example.invalid') "
            "WHERE email IS NULL OR TRIM(email) = ''"
        ))
    else:
        db.session.execute(text(
            "UPDATE creator_account SET email = LOWER(username) || '@example.invalid' "
            "WHERE email IS NULL OR TRIM(email) = ''"
        ))
    if db.engine.dialect.name == 'mysql':
        if not any(
            index.get('unique') and index.get('column_names') == ['email']
            for index in inspect(db.engine).get_indexes('creator_account')
        ):
            db.session.execute(text(
                'CREATE UNIQUE INDEX uq_creator_account_email ON creator_account(email)'
            ))
    else:
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_creator_account_email ON creator_account(email)'
        ))
    if needs_admin_bootstrap:
        db.session.execute(text(
            'UPDATE creator_account SET is_admin = TRUE, is_approved = TRUE '
            'WHERE lower(username) = :legacy_username OR lower(username) = :bootstrap_username'
        ), {'legacy_username': legacy_username, 'bootstrap_username': 'kaykohl'})
    db.session.commit()
    legacy_creator = CreatorAccount.query.filter_by(username=legacy_username).first()
    if legacy_creator is None:
        legacy_creator = CreatorAccount(
            username=legacy_username,
            email=app.config['LOGIN_EMAIL'],
            password_hash=generate_password_hash(LOGIN_PASSWORD),
            is_admin=True,
            is_approved=True,
        )
        db.session.add(legacy_creator)
        db.session.commit()
    migration_owner = CreatorAccount.query.filter_by(username='kaykohl').first() or legacy_creator
    _migrate_tenant_schema(migration_owner.id)
    for table_name in ('product', 'leaderboard'):
        columns = {column['name'] for column in inspect(db.engine).get_columns(table_name)}
        if 'is_public' not in columns:
            db.session.execute(text(
                f'ALTER TABLE {table_name} ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT TRUE'
            ))
    example_note = 'EXAMPLE DATA: Starter sale demonstrates related records.'
    for table_name, example_name in (
        ('product', 'Example: Custom consultation'),
        ('leaderboard', 'Example: Customer challenge'),
    ):
        db.session.execute(text(
            f'UPDATE {table_name} SET is_public = FALSE '
            'WHERE name = :name AND owner_id IN '
            '(SELECT owner_id FROM sale WHERE notes = :example_note)'
        ), {'name': example_name, 'example_note': example_note})
    db.session.commit()
    leaderboard_entry_columns = {
        column['name'] for column in inspect(db.engine).get_columns('leaderboard_entry')
    }
    if 'use_username' not in leaderboard_entry_columns:
        default_value = '0' if db.engine.dialect.name == 'sqlite' else 'FALSE'
        db.session.execute(text(
            'ALTER TABLE leaderboard_entry ADD COLUMN use_username BOOLEAN NOT NULL '
            f'DEFAULT {default_value}'
        ))
        db.session.commit()
    sale_columns = {column['name'] for column in inspect(db.engine).get_columns('sale')}
    sale_migrations = {
        'created_at': datetime_column_type,
        'due_date': 'DATE',
        'sale_category': 'VARCHAR(120)',
        'status': "VARCHAR(30) NOT NULL DEFAULT 'New'",
        'paid': 'BOOLEAN NOT NULL DEFAULT FALSE',
        'store_customer_id': 'INTEGER REFERENCES store_customer(id)',
        'stripe_checkout_session_id': 'VARCHAR(255)',
        'stripe_payment_intent_id': 'VARCHAR(255)',
    }
    for column_name, definition in sale_migrations.items():
        if column_name not in sale_columns:
            db.session.execute(text(f'ALTER TABLE sale ADD COLUMN {column_name} {definition}'))
    db.session.execute(text('UPDATE sale SET created_at = sale_date WHERE created_at IS NULL'))
    db.session.commit()
    addon_columns = {column['name'] for column in inspect(db.engine).get_columns('product_addon')}
    if 'price_mode' not in addon_columns:
        db.session.execute(text(
            "ALTER TABLE product_addon ADD COLUMN price_mode VARCHAR(20) NOT NULL DEFAULT 'per_quantity'"
        ))
    if 'quantity_unit' not in addon_columns:
        db.session.execute(text('ALTER TABLE product_addon ADD COLUMN quantity_unit VARCHAR(40)'))
    sale_addon_columns = {
        column['name'] for column in inspect(db.engine).get_columns('sale_item_addon')
    }
    if 'price_mode' not in sale_addon_columns:
        db.session.execute(text(
            "ALTER TABLE sale_item_addon ADD COLUMN price_mode VARCHAR(20) NOT NULL DEFAULT 'per_quantity'"
        ))
    db.session.commit()
    # Small migrations for databases created by earlier SQLite versions.
    # PostgreSQL databases are expected to be created from the current models.
    if db.engine.dialect.name == 'sqlite':
        try:
            cols = {r[1] for r in db.session.execute(text('PRAGMA table_info(product_addon)')).fetchall()}
            if 'description' not in cols:
                db.session.execute(text('ALTER TABLE product_addon ADD COLUMN description TEXT'))
            if 'labor_hours' not in cols:
                db.session.execute(text('ALTER TABLE product_addon ADD COLUMN labor_hours NUMERIC(12,2) DEFAULT 0'))
            sale_cols = {r[1] for r in db.session.execute(text('PRAGMA table_info(sale_item)')).fetchall()}
            if 'bundle_id' not in sale_cols:
                db.session.execute(text('ALTER TABLE sale_item ADD COLUMN bundle_id INTEGER'))
            db.session.commit()
        except Exception:
            db.session.rollback()
    if legacy_creator:
        if not Category.query.filter_by(owner_id=legacy_creator.id).first():
            db.session.add_all([Category(owner_id=legacy_creator.id, name=name) for name in ('General', 'Services', 'Products')])
        if not Platform.query.filter_by(owner_id=legacy_creator.id).first():
            db.session.add_all([Platform(owner_id=legacy_creator.id, name=name) for name in ('Website', 'Etsy', 'In Person', 'Other')])
        if not PaymentMethod.query.filter_by(owner_id=legacy_creator.id).first():
            db.session.add_all([PaymentMethod(owner_id=legacy_creator.id, name=name) for name in ('Cash', 'Card', 'Gift Card', 'Product Exchange')])
        db.session.commit()
    for table in db.metadata.tables.values():
        for index in table.indexes:
            index.create(bind=db.engine, checkfirst=True)

def money(v):
    try: return Decimal(str(v or 0))
    except (InvalidOperation, ValueError, TypeError): return Decimal('0')

def _stripe_cents(value):
    return int((money(value) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))

def _sales_customer_for_buyer(owner_id, email, name):
    if not email:
        return None
    email = email.strip().lower()
    customer = Customer.query.filter(
        Customer.owner_id == owner_id, func.lower(Customer.email) == email
    ).first()
    if customer is None:
        customer = Customer(owner_id=owner_id, name=(name or email)[:160], email=email)
        db.session.add(customer)
        db.session.flush()
    return customer

def _send_email(recipient, subject, body):
    if not (app.config.get('MAIL_SERVER') and app.config.get('MAIL_DEFAULT_SENDER')):
        return False
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = app.config['MAIL_DEFAULT_SENDER']
    message['To'] = recipient
    message.set_content(body)
    try:
        with smtplib.SMTP(app.config['MAIL_SERVER'], app.config['MAIL_PORT'], timeout=10) as server:
            if app.config.get('MAIL_USE_TLS'):
                server.starttls(context=ssl.create_default_context())
            if app.config.get('MAIL_USERNAME'):
                server.login(app.config['MAIL_USERNAME'], app.config.get('MAIL_PASSWORD', ''))
            server.send_message(message)
        return True
    except (OSError, smtplib.SMTPException):
        logging.exception('Could not send email to %s', recipient)
        return False

def _create_sale_notification(sale):
    notification = SellerNotification(
        owner_id=sale.owner_id,
        sale_id=sale.id,
        title='New sale received',
        message=f'Sale #{sale.id} has arrived for {money(sale.total_amount):.2f}.',
    )
    db.session.add(notification)
    return notification

def _send_sale_notification_email(sale):
    seller = db.session.get(CreatorAccount, sale.owner_id)
    if seller is None or not seller.email_notifications or seller.email.endswith('@example.invalid'):
        return
    _send_email(
        seller.email,
        f'New sale #{sale.id}',
        f'You received a new sale for {money(sale.total_amount):.2f}. Sign in to view the sale.',
    )

def _stripe_resource_id(resource):
    return getattr(resource, 'id', None) or resource

def _record_subscription_checkout(checkout):
    metadata = checkout.get('metadata') or {}
    seller_value = metadata.get('seller_id') or checkout.get('client_reference_id')
    try:
        seller_id = int(seller_value)
    except (TypeError, ValueError):
        return False
    seller = db.session.get(CreatorAccount, seller_id)
    if seller is None:
        return False
    customer_id = _stripe_resource_id(checkout.get('customer'))
    subscription_id = _stripe_resource_id(checkout.get('subscription'))
    if customer_id:
        seller.stripe_customer_id = customer_id
    if subscription_id:
        seller.stripe_subscription_id = subscription_id
    if seller.stripe_subscription_checkout_session_id == checkout.get('id'):
        seller.stripe_subscription_checkout_session_id = None
    db.session.commit()
    return True

def _sync_seller_subscription(subscription):
    metadata = subscription.get('metadata') or {}
    seller_value = metadata.get('seller_id')
    customer_id = _stripe_resource_id(subscription.get('customer'))
    subscription_id = _stripe_resource_id(subscription.get('id'))
    seller = None
    try:
        seller_id = int(seller_value)
        seller = db.session.get(CreatorAccount, seller_id)
    except (TypeError, ValueError):
        pass
    if seller is None and subscription_id:
        seller = CreatorAccount.query.filter_by(stripe_subscription_id=subscription_id).first()
    if seller is None and customer_id:
        seller = CreatorAccount.query.filter_by(stripe_customer_id=customer_id).first()
    if seller is None:
        return False
    seller.stripe_customer_id = customer_id or seller.stripe_customer_id
    seller.stripe_subscription_id = subscription_id or seller.stripe_subscription_id
    seller.stripe_subscription_status = subscription.get('status') or 'inactive'
    seller.stripe_subscription_checkout_session_id = None
    period_end = subscription.get('current_period_end')
    if period_end:
        seller.stripe_subscription_period_end = datetime.fromtimestamp(
            int(period_end), timezone.utc
        ).replace(tzinfo=None)
    else:
        seller.stripe_subscription_period_end = None
    db.session.commit()
    return True

def _expire_subscription_checkout(checkout):
    metadata = checkout.get('metadata') or {}
    try:
        seller_id = int(metadata.get('seller_id', ''))
    except (TypeError, ValueError):
        return False
    seller = db.session.get(CreatorAccount, seller_id)
    if seller is None or seller.stripe_subscription_checkout_session_id != checkout.get('id'):
        return False
    seller.stripe_subscription_checkout_session_id = None
    seller.stripe_subscription_status = 'inactive'
    db.session.commit()
    return True

def _complete_stripe_checkout(checkout):
    metadata = checkout.get('metadata') or {}
    try:
        sale_id = int(metadata.get('sale_id', ''))
        seller_id = int(metadata.get('seller_id', ''))
    except (TypeError, ValueError):
        return False
    sale = Sale.query.filter_by(id=sale_id, owner_id=seller_id).first()
    if sale is None or sale.stripe_checkout_session_id != checkout.get('id'):
        return False
    if checkout.get('payment_status') != 'paid':
        return False
    is_newly_paid = not sale.paid
    details = checkout.get('customer_details') or {}
    email = details.get('email') or checkout.get('customer_email')
    name = details.get('name')
    if sale.store_customer:
        email = sale.store_customer.email
        name = sale.store_customer.display_name
    customer = _sales_customer_for_buyer(sale.owner_id, email, name)
    if customer:
        sale.customer = customer
    sale.paid = True
    if sale.status == 'Waiting on Payment':
        sale.status = 'New'
    payment_intent = checkout.get('payment_intent')
    sale.stripe_payment_intent_id = getattr(payment_intent, 'id', None) or payment_intent
    sale.payment_detail = 'Stripe Checkout'
    db.session.commit()
    if is_newly_paid:
        _create_sale_notification(sale)
        db.session.commit()
        _send_sale_notification_email(sale)
    return True

def _leaderboard_score(value):
    try:
        score = Decimal(value)
        if not score.is_finite() or abs(score) > Decimal('9999999999.99'):
            return None
        return score.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError, TypeError):
        return None

@app.context_processor
def helpers():
    creator = getattr(g, 'creator', None)
    unread_notifications = []
    if creator and creator.banner_notifications:
        unread_notifications = SellerNotification.query.filter_by(
            owner_id=creator.id, read_at=None
        ).order_by(SellerNotification.created_at.desc()).limit(3).all()
    return {
        'money': lambda x: f'${money(x):,.2f}',
        'current_creator': creator,
        'current_store_customer': getattr(g, 'store_customer', None),
        'unread_notifications': unread_notifications,
    }

EXAMPLE_SALE_NOTE = 'EXAMPLE DATA: Starter sale demonstrates related records.'

def _seed_creator_examples(owner_id):
    if Sale.query.filter_by(owner_id=owner_id, notes=EXAMPLE_SALE_NOTE).first():
        return
    category = Category(owner_id=owner_id, name='Example: Services', description='Sample product type for tutorial records.')
    platform = Platform(owner_id=owner_id, name='Example: Online Store')
    payment = PaymentMethod(owner_id=owner_id, name='Example: Card')
    customer = Customer(
        owner_id=owner_id, name='Example: Jamie Sample', username='example_customer',
        email='example@example.test',
        notes='Tutorial customer. Replace these details or remove the example records when ready.',
    )
    product = Product(
        owner_id=owner_id, name='Example: Custom consultation',
        description='Tutorial product with a trackable field, add-on, bundle, and tag.',
        base_price=Decimal('30.00'), is_public=False,
    )
    tag = ProductTag(owner_id=owner_id, name='Example: Service')
    board = Leaderboard(
        owner_id=owner_id, name='Example: Customer challenge',
        description='Tutorial leaderboard. Add customer scores from the seller dashboard.',
        score_label='Points', sort_order='desc', is_public=False,
    )
    db.session.add_all([category, platform, payment, customer, product, tag, board])
    db.session.flush()
    product.category_id = category.id
    product.tags.append(tag)
    field = ProductField(
        product_id=product.id, name='Example: Project reference',
        field_type='text', unit='code', required=False,
    )
    addon = ProductAddon(
        product_id=product.id, name='Example: Rush processing',
        description='A flat add-on charge applied once to this sale item.',
        price=Decimal('5.00'), price_mode='flat', labor_hours=Decimal('0.25'),
    )
    bundle = ProductBundle(
        product_id=product.id, name='Example: Three consultations',
        description='An alternate package with its own amount and price.',
        amount=Decimal('3'), unit='sessions', price=Decimal('60.00'),
        labor_hours=Decimal('1.00'),
    )
    db.session.add_all([field, addon, bundle])
    db.session.flush()
    sale = Sale(
        owner_id=owner_id, customer_id=customer.id, platform_id=platform.id,
        payment_method_id=payment.id, sale_date=datetime.utcnow(),
        due_date=date.today() + timedelta(days=7), sale_category='Example',
        status='In Progress', paid=False, total_amount=Decimal('65.00'),
        notes=EXAMPLE_SALE_NOTE,
    )
    db.session.add(sale)
    db.session.flush()
    item = SaleItem(
        sale_id=sale.id, product_id=product.id, bundle_id=bundle.id,
        quantity=Decimal('1'), unit_price=bundle.price, labor_hours=bundle.labor_hours,
    )
    db.session.add(item)
    db.session.flush()
    db.session.add_all([
        SaleItemField(sale_item_id=item.id, product_field_id=field.id, value='EX-001'),
        SaleItemAddon(
            sale_item_id=item.id, product_addon_id=addon.id, quantity=Decimal('1'),
            unit_price=addon.price, price_mode=addon.price_mode,
        ),
        LeaderboardEntry(
            leaderboard_id=board.id, customer_id=customer.id,
            score=Decimal('100'), use_username=True,
        ),
    ])

def _remove_creator_examples(owner_id):
    for sale in Sale.query.filter_by(owner_id=owner_id, notes=EXAMPLE_SALE_NOTE).all():
        db.session.delete(sale)
    for board in Leaderboard.query.filter(
        Leaderboard.owner_id == owner_id, Leaderboard.name.like('Example:%')
    ).all():
        db.session.delete(board)
    for product in Product.query.filter(
        Product.owner_id == owner_id, Product.name.like('Example:%')
    ).all():
        db.session.delete(product)
    for customer in Customer.query.filter(
        Customer.owner_id == owner_id, Customer.name.like('Example:%')
    ).all():
        db.session.delete(customer)
    for model in (ProductTag, Category, Platform, PaymentMethod):
        for record in model.query.filter(
            model.owner_id == owner_id, model.name.like('Example:%')
        ).all():
            db.session.delete(record)

def _delete_creator_records(owner_id):
    for sale in Sale.query.filter_by(owner_id=owner_id).all():
        db.session.delete(sale)
    for board in Leaderboard.query.filter_by(owner_id=owner_id).all():
        db.session.delete(board)
    for product in Product.query.filter_by(owner_id=owner_id).all():
        db.session.delete(product)
    for customer in Customer.query.filter_by(owner_id=owner_id).all():
        db.session.delete(customer)
    for model in (ProductTag, Category, Platform, PaymentMethod):
        for record in model.query.filter_by(owner_id=owner_id).all():
            db.session.delete(record)

def _admin_return_url():
    target = request.form.get('next', '')
    return target if target == '/admin' or target.startswith('/admin?') else url_for('admin_dashboard')

@app.route('/admin')
def admin_dashboard():
    query = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    accounts_query = CreatorAccount.query
    if query:
        pattern = f'%{query}%'
        accounts_query = accounts_query.filter(or_(
            CreatorAccount.username.ilike(pattern), CreatorAccount.display_name.ilike(pattern),
            CreatorAccount.email.ilike(pattern),
        ))
    if status == 'pending':
        accounts_query = accounts_query.filter(CreatorAccount.is_approved.is_(False))
    elif status == 'active':
        accounts_query = accounts_query.filter(CreatorAccount.is_approved.is_(True))
    elif status == 'admins':
        accounts_query = accounts_query.filter(CreatorAccount.is_admin.is_(True))
    elif status == 'billing':
        accounts_query = accounts_query.filter(
            CreatorAccount.is_admin.is_(False),
            CreatorAccount.subscription_exempt.is_(False),
            CreatorAccount.stripe_subscription_status.notin_(ACTIVE_SUBSCRIPTION_STATUSES),
        )
    pagination = accounts_query.order_by(CreatorAccount.created_at.desc()).paginate(
        page=max(request.args.get('page', 1, type=int), 1), per_page=50, error_out=False
    )
    account_ids = [account.id for account in pagination.items]
    owner_counts = {}
    for name, model in (
        ('customers', Customer), ('products', Product),
        ('sales', Sale), ('leaderboards', Leaderboard),
    ):
        rows = db.session.query(model.owner_id, func.count(model.id)).filter(
            model.owner_id.in_(account_ids)
        ).group_by(model.owner_id).all() if account_ids else []
        owner_counts[name] = dict(rows)
    account_rows = []
    for account in pagination.items:
        account_rows.append({
            'account': account,
            'customers': owner_counts['customers'].get(account.id, 0),
            'products': owner_counts['products'].get(account.id, 0),
            'sales': owner_counts['sales'].get(account.id, 0),
            'leaderboards': owner_counts['leaderboards'].get(account.id, 0),
        })
    database_counts = [
        ('Creators', CreatorAccount.query.count()),
        ('Buyer accounts', StoreCustomer.query.count()),
        ('Customers', Customer.query.count()),
        ('Products', Product.query.count()),
        ('Product tags', ProductTag.query.count()),
        ('Sales', Sale.query.count()),
        ('Sale items', SaleItem.query.count()),
        ('Leaderboards', Leaderboard.query.count()),
        ('Notifications', SellerNotification.query.count()),
    ]
    filters = request.args.to_dict()
    previous_url = url_for('admin_dashboard', **{**filters, 'page': pagination.prev_num}) if pagination.has_prev else None
    next_url = url_for('admin_dashboard', **{**filters, 'page': pagination.next_num}) if pagination.has_next else None
    return render_template(
        'admin/index.html', accounts=account_rows, pagination=pagination,
        database_counts=database_counts, database_engine=db.engine.dialect.name,
        backup_available=db.engine.dialect.name == 'sqlite', query=query, status=status,
        previous_url=previous_url, next_url=next_url,
    )

@app.route('/admin/users/<int:id>/approval', methods=['POST'])
def admin_user_approval(id):
    account = CreatorAccount.query.get_or_404(id)
    if not _form_version_matches(account):
        return redirect(_admin_return_url())
    approved = request.form.get('approved') == '1'
    if account.id == g.creator.id and not approved:
        flash('You cannot deactivate your own admin account.', 'error')
    else:
        was_approved = account.is_approved
        account.is_approved = approved
        if approved and not was_approved:
            _seed_creator_examples(account.id)
        db.session.commit()
        flash(f'@{account.username} is now {"active" if approved else "pending"}.', 'success')
    return redirect(_admin_return_url())

@app.route('/admin/users/<int:id>/role', methods=['POST'])
def admin_user_role(id):
    account = CreatorAccount.query.get_or_404(id)
    if not _form_version_matches(account):
        return redirect(_admin_return_url())
    make_admin = request.form.get('is_admin') == '1'
    if account.id == g.creator.id and not make_admin:
        flash('You cannot remove your own admin role.', 'error')
    else:
        account.is_admin = make_admin
        db.session.commit()
        flash(f'Admin access {"granted to" if make_admin else "removed from"} @{account.username}.', 'success')
    return redirect(_admin_return_url())

@app.route('/admin/users/<int:id>/subscription-exemption', methods=['POST'])
def admin_user_subscription_exemption(id):
    account = CreatorAccount.query.get_or_404(id)
    if account.is_admin:
        flash('Administrator accounts are already exempt from seller subscriptions.', 'info')
        return redirect(_admin_return_url())
    if not _form_version_matches(account):
        return redirect(_admin_return_url())
    exempt = request.form.get('subscription_exempt') == '1'
    if exempt == account.subscription_exempt:
        return redirect(_admin_return_url())
    can_modify_subscription = account.stripe_subscription_id and account.stripe_subscription_status in (
        'active', 'trialing', 'past_due', 'unpaid', 'incomplete'
    )
    if can_modify_subscription:
        if not app.config.get('STRIPE_SECRET_KEY'):
            flash('Configure STRIPE_SECRET_KEY before changing a waivered seller with an existing subscription.', 'error')
            return redirect(_admin_return_url())
        try:
            stripe.Subscription.modify(
                account.stripe_subscription_id,
                cancel_at_period_end=exempt,
                api_key=app.config['STRIPE_SECRET_KEY'],
            )
        except stripe.StripeError as error:
            flash(getattr(error, 'user_message', None) or 'Stripe could not update the seller subscription.', 'error')
            return redirect(_admin_return_url())
    account.subscription_exempt = exempt
    db.session.commit()
    flash(
        f'Subscription fee {"waived" if exempt else "restored"} for @{account.username}.',
        'success',
    )
    return redirect(_admin_return_url())

@app.route('/admin/users/<int:id>/password', methods=['POST'])
def admin_user_password(id):
    account = CreatorAccount.query.get_or_404(id)
    if not _form_version_matches(account):
        return redirect(_admin_return_url())
    password = request.form.get('password', '')
    if len(password) < 10:
        flash('A reset password must have at least 10 characters.', 'error')
    else:
        account.password_hash = generate_password_hash(password)
        db.session.commit()
        flash(f'Password reset for @{account.username}.', 'success')
    return redirect(_admin_return_url())

@app.route('/admin/users/<int:id>/delete', methods=['POST'])
def admin_user_delete(id):
    account = CreatorAccount.query.get_or_404(id)
    if not _form_version_matches(account):
        return redirect(_admin_return_url())
    if account.id == g.creator.id:
        flash('You cannot delete your own admin account.', 'error')
    else:
        username = account.username
        _delete_creator_records(account.id)
        db.session.delete(account)
        db.session.commit()
        flash(f'@{username} and their business data were deleted.', 'success')
    return redirect(_admin_return_url())

@app.route('/admin/database/backup')
def admin_database_backup():
    if db.engine.dialect.name != 'sqlite':
        abort(501, description='Downloadable backups are currently available for SQLite databases.')
    connection = db.engine.raw_connection()
    try:
        snapshot = connection.driver_connection.serialize()
    finally:
        connection.close()
    return send_file(
        BytesIO(snapshot), mimetype='application/vnd.sqlite3', as_attachment=True,
        download_name=f'business-tracker-{date.today().isoformat()}.sqlite3',
    )

@app.route('/examples/remove', methods=['POST'])
def remove_creator_examples():
    _remove_creator_examples(g.creator.id)
    db.session.commit()
    flash('Example records removed.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/')
def storefront():
    query = request.args.get('q', '').strip()
    products_query = db.session.query(Product, CreatorAccount, Category).join(
        CreatorAccount, CreatorAccount.id == Product.owner_id
    ).outerjoin(Category, (Category.id == Product.category_id) & (Category.owner_id == Product.owner_id)).filter(
        Product.active.is_(True), Product.is_public.is_(True), CreatorAccount.is_approved.is_(True),
        _seller_subscription_access(),
    )
    boards_query = db.session.query(Leaderboard, CreatorAccount).join(
        CreatorAccount, CreatorAccount.id == Leaderboard.owner_id
    ).filter(Leaderboard.is_public.is_(True), CreatorAccount.is_approved.is_(True))
    boards_query = boards_query.filter(_seller_subscription_access())
    if query:
        pattern = f'%{query}%'
        products_query = products_query.filter(or_(
            Product.name.ilike(pattern), Product.description.ilike(pattern),
            CreatorAccount.username.ilike(pattern), CreatorAccount.display_name.ilike(pattern),
            Category.name.ilike(pattern),
        ))
        boards_query = boards_query.filter(or_(
            Leaderboard.name.ilike(pattern), Leaderboard.description.ilike(pattern),
            CreatorAccount.username.ilike(pattern), CreatorAccount.display_name.ilike(pattern),
        ))
        sellers = CreatorAccount.query.filter(or_(
            CreatorAccount.username.ilike(pattern), CreatorAccount.display_name.ilike(pattern)
        )).filter(CreatorAccount.is_approved.is_(True), _seller_subscription_access()).order_by(
            CreatorAccount.username
        ).limit(20).all()
    else:
        sellers = CreatorAccount.query.filter_by(is_approved=True).filter(
            _seller_subscription_access()
        ).order_by(CreatorAccount.username).limit(20).all()
    product_results = products_query.order_by(CreatorAccount.username, Product.name).limit(60).all()
    board_results = boards_query.order_by(CreatorAccount.username, Leaderboard.name).limit(30).all()
    return render_template(
        'storefront/index.html', query=query, sellers=sellers,
        product_results=product_results, board_results=board_results,
    )

@app.route('/sellers/<username>')
def seller_store(username):
    seller = CreatorAccount.query.filter(
        func.lower(CreatorAccount.username) == username.lower(), CreatorAccount.is_approved.is_(True),
        _seller_subscription_access(),
    ).first_or_404()
    products = Product.query.filter_by(owner_id=seller.id, active=True, is_public=True).order_by(
        Product.category_id, Product.name
    ).all()
    board_rows = db.session.query(Leaderboard, func.count(LeaderboardEntry.id).label('entry_count')).outerjoin(
        LeaderboardEntry, Leaderboard.id == LeaderboardEntry.leaderboard_id
    ).filter(Leaderboard.owner_id == seller.id, Leaderboard.is_public.is_(True)).group_by(Leaderboard.id).order_by(
        func.count(LeaderboardEntry.id).desc(), Leaderboard.name
    ).limit(3).all()
    boards = []
    for board, entry_count in board_rows:
        score_order = LeaderboardEntry.score.desc() if board.sort_order == 'desc' else LeaderboardEntry.score.asc()
        top_entry = db.session.query(LeaderboardEntry).join(Customer).filter(
            LeaderboardEntry.leaderboard_id == board.id, Customer.owner_id == seller.id
        ).order_by(score_order, LeaderboardEntry.id).first()
        boards.append({'board': board, 'entry_count': entry_count, 'top_entry': top_entry})
    return render_template('storefront/seller.html', seller=seller, products=products, boards=boards)

@app.route('/sellers/<username>/products/<int:product_id>/checkout', methods=['POST'])
def public_product_checkout(username, product_id):
    seller = CreatorAccount.query.filter(
        func.lower(CreatorAccount.username) == username.lower(),
        CreatorAccount.is_approved.is_(True),
        _seller_subscription_access(),
    ).first_or_404()
    if not (seller.accepting_orders and seller.stripe_account_id and seller.stripe_charges_enabled and seller.stripe_details_submitted):
        abort(404)
    if not app.config.get('STRIPE_SECRET_KEY'):
        flash('Online payments are temporarily unavailable.', 'error')
        return redirect(url_for('seller_store', username=seller.username))
    product = Product.query.filter_by(
        id=product_id, owner_id=seller.id, active=True, is_public=True
    ).first_or_404()
    try:
        quantity_value = Decimal(request.form.get('quantity', '1'))
        if not quantity_value.is_finite() or quantity_value != quantity_value.to_integral_value():
            raise ValueError
        quantity = int(quantity_value)
    except (InvalidOperation, TypeError, ValueError):
        flash('Enter a whole-number quantity.', 'error')
        return redirect(url_for('seller_store', username=seller.username))
    if quantity < 1 or quantity > 99:
        flash('Choose a quantity between 1 and 99.', 'error')
        return redirect(url_for('seller_store', username=seller.username))

    bundle_id = request.form.get('bundle_id', type=int)
    bundle = None
    if bundle_id:
        bundle = next((option for option in product.bundles if option.id == bundle_id and option.active), None)
        if bundle is None:
            abort(400, description='That pricing option is not available for this product.')
    unit_price = money(bundle.price if bundle else product.base_price)
    if unit_price < 0:
        abort(400, description='Product prices cannot be negative.')

    selected_addon_ids = request.form.getlist('addon_ids')
    active_addons = {addon.id: addon for addon in product.addons if addon.active}
    if any(not addon_id.isdigit() or int(addon_id) not in active_addons for addon_id in selected_addon_ids):
        abort(400, description='An add-on is not available for this product.')
    addon_choices = []
    for addon_id in dict.fromkeys(selected_addon_ids):
        addon = active_addons[int(addon_id)]
        if addon.price_mode == 'flat':
            addon_quantity = Decimal('1')
        else:
            try:
                addon_quantity = Decimal(request.form.get(f'addon_quantity_{addon.id}', '1'))
                if not addon_quantity.is_finite() or addon_quantity != addon_quantity.to_integral_value():
                    raise ValueError
            except (InvalidOperation, TypeError, ValueError):
                flash('Enter a whole number for add-on quantity.', 'error')
                return redirect(url_for('seller_store', username=seller.username))
            if addon_quantity < 1 or addon_quantity > 9999:
                flash('Add-on quantities must be between 1 and 9,999.', 'error')
                return redirect(url_for('seller_store', username=seller.username))
        addon_choices.append((addon, addon_quantity))

    field_values = []
    for product_field in product.fields:
        value = request.form.get(f'field_value_{product_field.id}', '').strip()
        if product_field.required and not value:
            flash(f'Enter {product_field.name}.', 'error')
            return redirect(url_for('seller_store', username=seller.username))
        if value:
            field_values.append((product_field, value))

    line_items = []
    base_cents = _stripe_cents(unit_price)
    if base_cents > 0:
        line_items.append({
            'price_data': {
                'currency': app.config.get('STRIPE_CURRENCY', 'usd'),
                'product_data': {
                    'name': f'{product.name} — {bundle.name}' if bundle else product.name,
                    'description': bundle.description or product.description if bundle else product.description,
                },
                'unit_amount': base_cents,
            },
            'quantity': quantity,
        })
    total = unit_price * quantity
    addon_total = Decimal('0')
    for addon, addon_quantity in addon_choices:
        addon_price = money(addon.price)
        quantity_factor = Decimal('1') if addon.price_mode == 'flat' else Decimal(quantity)
        addon_total += addon_price * addon_quantity * quantity_factor
        addon_cents = _stripe_cents(addon_price)
        if addon_cents <= 0:
            continue
        stripe_quantity = int(addon_quantity if addon.price_mode == 'flat' else addon_quantity * quantity)
        line_items.append({
            'price_data': {
                'currency': app.config.get('STRIPE_CURRENCY', 'usd'),
                'product_data': {
                    'name': f'{product.name} — {addon.name}',
                    'description': f'One-time add-on' if addon.price_mode == 'flat' else f'Per {addon.quantity_unit or "item"}',
                },
                'unit_amount': addon_cents,
            },
            'quantity': stripe_quantity,
        })
    total += addon_total
    if total <= 0:
        flash('This item does not have a payable price yet.', 'error')
        return redirect(url_for('seller_store', username=seller.username))

    buyer = g.store_customer
    crm_customer = _sales_customer_for_buyer(seller.id, buyer.email, buyer.display_name) if buyer else None
    sale = Sale(
        owner_id=seller.id,
        store_customer_id=buyer.id if buyer else None,
        customer_id=crm_customer.id if crm_customer else None,
        sale_date=datetime.utcnow(), sale_category=product.category.name if product.category else None,
        status='Waiting on Payment', paid=False, total_amount=total,
        notes='Public menu checkout via Stripe.',
    )
    db.session.add(sale)
    db.session.flush()
    item = SaleItem(
        sale_id=sale.id, product_id=product.id, bundle_id=bundle.id if bundle else None,
        quantity=quantity, unit_price=unit_price,
        labor_hours=money(bundle.labor_hours if bundle else 0),
    )
    db.session.add(item)
    db.session.flush()
    for product_field, value in field_values:
        db.session.add(SaleItemField(
            sale_item_id=item.id, product_field_id=product_field.id, value=value
        ))
    for addon, addon_quantity in addon_choices:
        db.session.add(SaleItemAddon(
            sale_item_id=item.id, product_addon_id=addon.id, quantity=addon_quantity,
            unit_price=addon.price, price_mode=addon.price_mode,
        ))
        item.labor_hours = (item.labor_hours or Decimal('0')) + money(addon.labor_hours) * addon_quantity
    db.session.commit()

    try:
        checkout = stripe.checkout.Session.create(
            mode='payment',
            line_items=line_items,
            customer_email=buyer.email if buyer else None,
            client_reference_id=str(sale.id),
            metadata={'sale_id': str(sale.id), 'seller_id': str(seller.id)},
            payment_intent_data={'transfer_data': {'destination': seller.stripe_account_id}},
            success_url=f"{url_for('checkout_success', _external=True)}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=url_for('checkout_cancel', username=seller.username, _external=True),
            api_key=app.config['STRIPE_SECRET_KEY'],
        )
        sale.stripe_checkout_session_id = checkout.id
        db.session.commit()
        return redirect(checkout.url, code=303)
    except stripe.StripeError as error:
        sale.status = 'Canceled'
        db.session.commit()
        flash(getattr(error, 'user_message', None) or 'Stripe checkout could not be started. Please try again.', 'error')
        return redirect(url_for('seller_store', username=seller.username))

@app.route('/checkout/success')
def checkout_success():
    session_id = request.args.get('session_id', '')
    if not session_id or not app.config.get('STRIPE_SECRET_KEY'):
        return render_template('storefront/checkout_result.html', paid=False, seller=None)
    try:
        checkout = stripe.checkout.Session.retrieve(session_id, api_key=app.config['STRIPE_SECRET_KEY'])
        _complete_stripe_checkout(checkout)
        sale_id = int((checkout.get('metadata') or {}).get('sale_id', '0'))
        sale = db.session.get(Sale, sale_id) if sale_id else None
        seller = db.session.get(CreatorAccount, sale.owner_id) if sale else None
        return render_template('storefront/checkout_result.html', paid=bool(sale and sale.paid), seller=seller)
    except stripe.StripeError:
        return render_template('storefront/checkout_result.html', paid=False, seller=None)

@app.route('/checkout/cancel/<username>')
def checkout_cancel(username):
    seller = CreatorAccount.query.filter_by(username=username, is_approved=True).first_or_404()
    return render_template('storefront/checkout_result.html', paid=False, seller=seller, canceled=True)

@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    webhook_secret = app.config.get('STRIPE_WEBHOOK_SECRET')
    if not webhook_secret:
        abort(503, description='Stripe webhook is not configured.')
    payload = request.get_data()
    signature = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except Exception:
        abort(400, description='Invalid Stripe webhook signature or payload.')
    checkout = event.data.object
    if event.type == 'checkout.session.completed' and checkout.get('mode') == 'subscription':
        _record_subscription_checkout(checkout)
    elif event.type in ('customer.subscription.created', 'customer.subscription.updated', 'customer.subscription.deleted'):
        _sync_seller_subscription(checkout)
    elif event.type == 'checkout.session.expired' and checkout.get('mode') == 'subscription':
        _expire_subscription_checkout(checkout)
    elif event.type in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
        _complete_stripe_checkout(checkout)
    elif event.type in ('checkout.session.expired', 'checkout.session.async_payment_failed'):
        metadata = checkout.get('metadata') or {}
        try:
            sale = Sale.query.filter_by(
                id=int(metadata.get('sale_id', '0')),
                owner_id=int(metadata.get('seller_id', '0')),
                stripe_checkout_session_id=checkout.get('id'),
            ).first()
        except (TypeError, ValueError):
            sale = None
        if sale and not sale.paid:
            sale.status = 'Canceled'
            db.session.commit()
    return '', 200

@app.route('/browse/leaderboards')
def public_leaderboards():
    query = request.args.get('q', '').strip()
    seller_username = request.args.get('seller', '').strip()
    boards_query = db.session.query(Leaderboard, CreatorAccount).join(
        CreatorAccount, CreatorAccount.id == Leaderboard.owner_id
    ).filter(
        Leaderboard.is_public.is_(True), CreatorAccount.is_approved.is_(True),
        _seller_subscription_access(),
    )
    if query:
        pattern = f'%{query}%'
        boards_query = boards_query.filter(or_(
            Leaderboard.name.ilike(pattern), Leaderboard.description.ilike(pattern),
            CreatorAccount.username.ilike(pattern), CreatorAccount.display_name.ilike(pattern),
        ))
    if seller_username:
        boards_query = boards_query.filter(func.lower(CreatorAccount.username) == seller_username.lower())
    boards = boards_query.order_by(CreatorAccount.username, Leaderboard.name).limit(60).all()
    return render_template(
        'storefront/boards.html', query=query, seller_username=seller_username, boards=boards
    )

@app.route('/sellers/<username>/leaderboards/<int:id>')
def public_leaderboard(username, id):
    seller = CreatorAccount.query.filter(
        func.lower(CreatorAccount.username) == username.lower(), CreatorAccount.is_approved.is_(True),
        _seller_subscription_access(),
    ).first_or_404()
    board = Leaderboard.query.filter_by(id=id, owner_id=seller.id, is_public=True).first_or_404()
    score_order = LeaderboardEntry.score.desc() if board.sort_order == 'desc' else LeaderboardEntry.score.asc()
    entries = db.session.query(LeaderboardEntry).join(Customer).filter(
        LeaderboardEntry.leaderboard_id == board.id, Customer.owner_id == seller.id
    ).order_by(score_order, LeaderboardEntry.id).all()
    return render_template(
        'storefront/leaderboard.html', seller=seller, board=board, entries=entries
    )

@app.route('/dashboard')
def dashboard():
    now = datetime.utcnow(); month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    sales_summary = db.session.query(
        func.coalesce(func.sum(Sale.total_amount), 0).label('revenue'),
        func.coalesce(func.sum(case((Sale.sale_date >= month_start, Sale.total_amount), else_=0)), 0).label('monthly'),
        func.count(Sale.id).label('orders'),
        func.coalesce(func.sum(case((Sale.sale_date >= month_start, 1), else_=0)), 0).label('monthly_orders'),
    ).filter(Sale.owner_id == g.creator.id).one()
    revenue, monthly = sales_summary.revenue, sales_summary.monthly
    orders, monthly_orders = sales_summary.orders, sales_summary.monthly_orders
    hours = db.session.query(func.coalesce(func.sum(SaleItem.labor_hours * SaleItem.quantity),0)).join(
        Sale, SaleItem.sale_id == Sale.id).filter(Sale.owner_id == g.creator.id).scalar() or 0
    top = db.session.query(Product.name, func.sum(SaleItem.quantity).label('qty'), func.sum(SaleItem.quantity*SaleItem.unit_price).label('rev')).join(
        SaleItem, Product.id == SaleItem.product_id).join(Sale, Sale.id == SaleItem.sale_id).filter(
        Product.owner_id == g.creator.id, Sale.owner_id == g.creator.id).group_by(Product.id).order_by(
        func.sum(SaleItem.quantity).desc()).limit(5).all()
    has_example_data = Sale.query.filter_by(
        owner_id=g.creator.id, notes=EXAMPLE_SALE_NOTE
    ).first() is not None
    return render_template('dashboard.html', revenue=revenue, monthly=monthly,
        orders=orders, monthly_orders=monthly_orders, hours=hours, top=top,
        has_example_data=has_example_data)

@app.route('/products')
def products():
    products_query = Product.query.options(
        joinedload(Product.category), selectinload(Product.tags),
        selectinload(Product.fields), selectinload(Product.addons),
        selectinload(Product.bundles),
    ).filter(Product.owner_id == g.creator.id).order_by(Product.name)
    pagination = products_query.paginate(
        page=max(request.args.get('page', 1, type=int), 1), per_page=50, error_out=False
    )
    return render_template(
        'products/list.html', products=pagination.items, pagination=pagination,
        previous_url=url_for('products', page=pagination.prev_num) if pagination.has_prev else None,
        next_url=url_for('products', page=pagination.next_num) if pagination.has_next else None,
    )

@app.route('/products/new', methods=['GET','POST'])
def product_new():
    if request.method == 'POST':
        category_id = request.form.get('category_id') or None
        category = Category.query.filter_by(id=category_id, owner_id=g.creator.id).first() if category_id else None
        if category_id and not category:
            flash('Choose a category from your account.', 'error')
            return redirect(url_for('product_new'))
        p=Product(owner_id=g.creator.id, name=request.form['name'], description=request.form.get('description'), base_price=money(request.form.get('base_price')), category_id=category.id if category else None, is_public=request.form.get('is_public', '1') == '1')
        db.session.add(p); db.session.flush(); _save_product_children(p)
        db.session.commit(); flash('Product created.','success'); return redirect(url_for('products'))
    return render_template('products/form.html', product=None, categories=Category.query.filter_by(owner_id=g.creator.id).order_by(Category.name).all())

@app.route('/products/<int:id>/edit', methods=['GET','POST'])
def product_edit(id):
    p = Product.query.options(
        selectinload(Product.tags), selectinload(Product.fields),
        selectinload(Product.addons), selectinload(Product.bundles),
    ).get_or_404(id)
    if request.method == 'POST' and not _form_version_matches(p):
        return redirect(url_for('product_edit', id=p.id))
    if request.method=='POST':
        category_id = request.form.get('category_id') or None
        category = Category.query.filter_by(id=category_id, owner_id=g.creator.id).first() if category_id else None
        if category_id and not category:
            flash('Choose a category from your account.', 'error')
            return redirect(url_for('product_edit', id=p.id))
        p.name=request.form['name']; p.description=request.form.get('description'); p.base_price=money(request.form.get('base_price')); p.category_id=category.id if category else None; p.is_public=request.form.get('is_public') == '1'
        ProductField.query.filter_by(product_id=p.id).delete(); ProductAddon.query.filter_by(product_id=p.id).delete(); ProductBundle.query.filter_by(product_id=p.id).delete()
        _save_product_children(p); db.session.commit(); flash('Product updated.','success'); return redirect(url_for('products'))
    return render_template('products/form.html', product=p, categories=Category.query.filter_by(owner_id=g.creator.id).order_by(Category.name).all())

@app.route('/products/<int:id>/delete', methods=['POST'])
def product_delete(id):
    p=Product.query.get_or_404(id)
    if not _form_version_matches(p):
        return redirect(url_for('products'))
    if SaleItem.query.filter_by(product_id=p.id).first():
        flash('This product cannot be deleted because it has sales history. Deactivate it instead.','error')
    else:
        db.session.delete(p); db.session.commit(); flash('Product deleted.','success')
    return redirect(url_for('products'))

def _save_product_children(p):
    tags = []
    seen_tags = set()
    for raw_tag in request.form.get('tags', '').split(','):
        tag_name = raw_tag.strip()[:40]
        tag_key = tag_name.casefold()
        if not tag_name or tag_key in seen_tags:
            continue
        seen_tags.add(tag_key)
        tag = ProductTag.query.filter(
            ProductTag.owner_id == p.owner_id,
            func.lower(ProductTag.name) == tag_key,
        ).first()
        if tag is None:
            tag = ProductTag(owner_id=p.owner_id, name=tag_name)
            db.session.add(tag)
        tags.append(tag)
    p.tags = tags
    names=request.form.getlist('field_name'); types=request.form.getlist('field_type'); units=request.form.getlist('field_unit'); required=request.form.getlist('field_required')
    for i,n in enumerate(names):
        if n.strip(): db.session.add(ProductField(product=p,name=n.strip(),field_type=types[i] if i<len(types) else 'text',unit=units[i] if i<len(units) else None,required=(str(i) in required)))
    an=request.form.getlist('addon_name'); ad=request.form.getlist('addon_description'); ap=request.form.getlist('addon_price'); ah=request.form.getlist('addon_labor'); am=request.form.getlist('addon_price_mode'); au=request.form.getlist('addon_quantity_unit')
    for i,n in enumerate(an):
        if n.strip():
            price_mode = am[i] if i < len(am) and am[i] in {'flat', 'per_quantity'} else 'per_quantity'
            db.session.add(ProductAddon(
                product=p, name=n.strip(), description=ad[i] if i<len(ad) else None,
                price=money(ap[i] if i<len(ap) else 0),
                price_mode=price_mode, quantity_unit=au[i].strip()[:40] if i<len(au) else None,
                labor_hours=money(ah[i] if i<len(ah) else 0),
            ))
    bn=request.form.getlist('bundle_name'); bd=request.form.getlist('bundle_description'); ba=request.form.getlist('bundle_amount'); bu=request.form.getlist('bundle_unit'); bp=request.form.getlist('bundle_price'); bh=request.form.getlist('bundle_labor')
    for i,n in enumerate(bn):
        if n.strip(): db.session.add(ProductBundle(product=p,name=n.strip(),description=bd[i] if i<len(bd) else None,amount=money(ba[i] if i<len(ba) else 1),unit=bu[i] if i<len(bu) else None,price=money(bp[i] if i<len(bp) else 0),labor_hours=money(bh[i] if i<len(bh) else 0)) )

def _product_json(products):
    return {str(p.id): {
        'price': str(p.base_price or 0),
        'tags': [tag.name for tag in p.tags],
        'fields': [{'id': f.id, 'name': f.name, 'type': f.field_type, 'unit': f.unit or '', 'required': bool(f.required)} for f in p.fields],
        'addons': [{'id': a.id, 'name': a.name, 'description': a.description or '', 'price': str(a.price or 0), 'price_mode': a.price_mode or 'per_quantity', 'quantity_unit': a.quantity_unit or '', 'labor': str(a.labor_hours or 0)} for a in p.addons if a.active],
        'bundles': [{'id': b.id, 'name': b.name, 'description': b.description or '', 'amount': str(b.amount or 1), 'unit': b.unit or '', 'price': str(b.price or 0), 'labor': str(b.labor_hours or 0)} for b in p.bundles if b.active]
    } for p in products}

@app.route('/sales')
def sales():
    today = date.today()
    query = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '')
    paid_filter = request.args.get('paid', '')
    category_filter = request.args.get('category', '').strip()
    product_type_id = request.args.get('product_type', type=int)
    product_tag_id = request.args.get('tag', type=int)
    addon_id = request.args.get('addon', type=int)
    bundle_id = request.args.get('bundle', type=int)
    field_id = request.args.get('field', type=int)
    has_addons_filter = request.args.get('has_addons', '')
    due_state_filter = request.args.get('due_state', '')
    sort = request.args.get('sort', 'created_desc')

    sale_query = Sale.query.filter(Sale.owner_id == g.creator.id)
    if query:
        pattern = f'%{query}%'
        sale_query = sale_query.filter(or_(
            Sale.sale_category.ilike(pattern), Sale.notes.ilike(pattern),
            Sale.payment_detail.ilike(pattern),
            Sale.customer.has(or_(
                Customer.name.ilike(pattern), Customer.username.ilike(pattern), Customer.email.ilike(pattern)
            )),
            Sale.platform.has(Platform.name.ilike(pattern)),
            Sale.payment_method.has(PaymentMethod.name.ilike(pattern)),
            Sale.items.any(or_(
                SaleItem.product.has(or_(
                    Product.name.ilike(pattern), Product.description.ilike(pattern),
                    Product.category.has(Category.name.ilike(pattern)),
                    Product.tags.any(ProductTag.name.ilike(pattern)),
                )),
                SaleItem.bundle.has(ProductBundle.name.ilike(pattern)),
                SaleItem.addons.any(SaleItemAddon.product_addon.has(ProductAddon.name.ilike(pattern))),
                SaleItem.fields.any(or_(
                    SaleItemField.value.ilike(pattern),
                    SaleItemField.product_field.has(ProductField.name.ilike(pattern)),
                )),
            )),
        ))
    if status_filter in SALE_STATUSES:
        sale_query = sale_query.filter(Sale.status == status_filter)
    if paid_filter == 'paid':
        sale_query = sale_query.filter(Sale.paid.is_(True))
    elif paid_filter == 'unpaid':
        sale_query = sale_query.filter(Sale.paid.is_(False))
    if category_filter:
        sale_query = sale_query.filter(Sale.sale_category == category_filter)
    if product_type_id:
        sale_query = sale_query.filter(Sale.items.any(
            SaleItem.product.has(Product.category_id == product_type_id)
        ))
    if product_tag_id:
        sale_query = sale_query.filter(Sale.items.any(
            SaleItem.product.has(Product.tags.any(ProductTag.id == product_tag_id))
        ))
    if addon_id:
        sale_query = sale_query.filter(Sale.items.any(
            SaleItem.addons.any(SaleItemAddon.product_addon_id == addon_id)
        ))
    if bundle_id:
        sale_query = sale_query.filter(Sale.items.any(SaleItem.bundle_id == bundle_id))
    if field_id:
        sale_query = sale_query.filter(Sale.items.any(
            SaleItem.fields.any(SaleItemField.product_field_id == field_id)
        ))
    if has_addons_filter == 'yes':
        sale_query = sale_query.filter(Sale.items.any(SaleItem.addons.any()))
    elif has_addons_filter == 'no':
        sale_query = sale_query.filter(~Sale.items.any(SaleItem.addons.any()))
    active_sale_statuses = Sale.status.notin_(('Completed', 'Canceled'))
    if due_state_filter == 'due_today':
        sale_query = sale_query.filter(Sale.due_date == today, active_sale_statuses)
    elif due_state_filter == 'overdue':
        sale_query = sale_query.filter(
            Sale.due_date < today, active_sale_statuses
        )
    elif due_state_filter == 'on_schedule':
        sale_query = sale_query.filter(Sale.due_date > today, active_sale_statuses)

    product_type_name = db.session.query(Category.name).select_from(SaleItem).join(
        Product, Product.id == SaleItem.product_id
    ).outerjoin(Category, (Category.id == Product.category_id) & (Category.owner_id == g.creator.id)).filter(
        SaleItem.sale_id == Sale.id, Product.owner_id == g.creator.id
    ).order_by(Product.name).limit(1).scalar_subquery()
    sale_type = func.coalesce(Sale.sale_category, product_type_name)
    sort_options = {
        'created_asc': (Sale.created_at.asc(), Sale.id.asc()),
        'created_desc': (Sale.created_at.desc(), Sale.id.desc()),
        'due_asc': (Sale.due_date.asc().nullslast(), Sale.id.desc()),
        'due_desc': (Sale.due_date.desc().nullslast(), Sale.id.desc()),
        'type_asc': (sale_type.asc().nullslast(), Sale.id.desc()),
        'type_desc': (sale_type.desc().nullslast(), Sale.id.desc()),
        'price_asc': (Sale.total_amount.asc(), Sale.id.desc()),
        'price_desc': (Sale.total_amount.desc(), Sale.id.desc()),
    }
    if sort not in sort_options:
        sort = 'created_desc'
    active_filter_count = sum(bool(value) for value in (
        query, status_filter, paid_filter, category_filter, product_type_id,
        product_tag_id, addon_id, bundle_id, field_id, has_addons_filter, due_state_filter,
    )) + (sort != 'created_desc')
    sale_query = sale_query.options(
        joinedload(Sale.customer),
        selectinload(Sale.items).joinedload(SaleItem.product).joinedload(Product.category),
        selectinload(Sale.items).joinedload(SaleItem.product).selectinload(Product.tags),
        selectinload(Sale.items).joinedload(SaleItem.bundle),
        selectinload(Sale.items).selectinload(SaleItem.addons).joinedload(SaleItemAddon.product_addon),
        selectinload(Sale.items).selectinload(SaleItem.fields).joinedload(SaleItemField.product_field),
    )
    pagination = sale_query.order_by(*sort_options[sort]).paginate(
        page=max(request.args.get('page', 1, type=int), 1), per_page=50, error_out=False
    )
    previous_url = None
    next_url = None
    page_filters = request.args.to_dict()
    if pagination.has_prev:
        previous_url = url_for('sales', **{**page_filters, 'page': pagination.prev_num})
    if pagination.has_next:
        next_url = url_for('sales', **{**page_filters, 'page': pagination.next_num})

    active_status = Sale.status.notin_(('Completed', 'Canceled'))
    summary_row = db.session.query(
        func.count(Sale.id).label('total'),
        func.coalesce(func.sum(case((active_status, 1), else_=0)), 0).label('open'),
        func.coalesce(func.sum(case((
            (Sale.due_date < today) & active_status, 1,
        ), else_=0)), 0).label('overdue'),
        func.coalesce(func.sum(case((Sale.paid.is_(False), 1), else_=0)), 0).label('unpaid'),
    ).filter(Sale.owner_id == g.creator.id).one()
    summary = {
        'total': summary_row.total,
        'open': summary_row.open,
        'overdue': summary_row.overdue,
        'unpaid': summary_row.unpaid,
    }
    categories = [row[0] for row in db.session.query(Sale.sale_category).filter(
        Sale.owner_id == g.creator.id, Sale.sale_category.isnot(None)
    ).distinct().order_by(Sale.sale_category).all()]
    product_types = Category.query.join(Product, Product.category_id == Category.id).filter(
        Category.owner_id == g.creator.id, Product.owner_id == g.creator.id
    ).distinct().order_by(Category.name).all()
    product_tags = ProductTag.query.filter(
        ProductTag.owner_id == g.creator.id,
        ProductTag.products.any(Product.owner_id == g.creator.id)
    ).order_by(ProductTag.name).all()
    addons = ProductAddon.query.options(joinedload(ProductAddon.product)).join(Product).filter(
        Product.owner_id == g.creator.id, ProductAddon.active.is_(True)
    ).order_by(ProductAddon.name).all()
    bundles = ProductBundle.query.options(joinedload(ProductBundle.product)).join(Product).filter(
        Product.owner_id == g.creator.id, ProductBundle.active.is_(True)
    ).order_by(ProductBundle.name).all()
    trackable_fields = ProductField.query.options(joinedload(ProductField.product)).join(Product).filter(
        Product.owner_id == g.creator.id
    ).order_by(ProductField.name).all()
    return render_template(
        'sales/list.html', sales=pagination.items, pagination=pagination, summary=summary,
        sale_statuses=SALE_STATUSES, categories=categories, product_types=product_types,
        addons=addons, bundles=bundles, trackable_fields=trackable_fields,
        product_tags=product_tags,
        filters=request.args.to_dict(), query=query, status_filter=status_filter,
        paid_filter=paid_filter, category_filter=category_filter,
        product_type_filter=product_type_id, product_tag_filter=product_tag_id,
        addon_filter=addon_id, bundle_filter=bundle_id,
        field_filter=field_id, has_addons_filter=has_addons_filter,
        due_state_filter=due_state_filter, active_filter_count=active_filter_count,
        sort=sort, today=today,
        previous_url=previous_url, next_url=next_url,
    )

@app.route('/sales/new', methods=['GET','POST'])
def sale_new(): return _sale_form(None)

@app.route('/sales/<int:id>/edit', methods=['GET','POST'])
def sale_edit(id): return _sale_form(Sale.query.get_or_404(id))

def _sale_form(sale):
    is_new_sale = sale is None
    if sale is not None and request.method == 'POST' and not _form_version_matches(sale):
        return redirect(url_for('sale_edit', id=sale.id))
    products = Product.query.filter_by(owner_id=g.creator.id, active=True).options(
        selectinload(Product.tags), selectinload(Product.fields),
        selectinload(Product.addons), selectinload(Product.bundles),
    ).order_by(Product.name).all()
    customers=Customer.query.filter_by(owner_id=g.creator.id).order_by(Customer.name).all()
    sale_categories = [row[0] for row in db.session.query(Sale.sale_category).filter(
        Sale.owner_id == g.creator.id, Sale.sale_category.isnot(None)
    ).distinct().order_by(Sale.sale_category).all()]
    if request.method=='POST':
        status = request.form.get('status', 'New')
        due_date_value = request.form.get('due_date', '').strip()
        if status not in SALE_STATUSES:
            flash('Choose a valid sale status.', 'error')
            return redirect(request.path)
        try:
            due_date = date.fromisoformat(due_date_value) if due_date_value else None
        except ValueError:
            flash('Enter a valid due date.', 'error')
            return redirect(request.path)
        sale_date = datetime.fromisoformat(request.form['sale_date']) if request.form.get('sale_date') else datetime.utcnow()
        sale_category = request.form.get('sale_category', '').strip()[:120] or None
        paid = request.form.get('paid') == '1'
        if sale is None:
            sale = Sale(owner_id=g.creator.id)
            db.session.add(sale)
            db.session.flush()
        else:
            SaleItem.query.filter_by(sale_id=sale.id).delete(synchronize_session=False)
        selected_customer = None
        customer_choice = request.form.get('customer_id', '').strip()
        if customer_choice == '__new__':
            name = request.form.get('new_customer_name', '').strip()
            if not name:
                db.session.rollback()
                flash('Enter a name for the new customer.', 'error')
                return redirect(request.path)
            customer = Customer(
                owner_id=g.creator.id,
                name=name,
                email=request.form.get('new_customer_email', '').strip() or None,
                username=request.form.get('new_customer_username', '').strip() or None,
                notes=request.form.get('new_customer_notes', '').strip() or None,
            )
            db.session.add(customer)
            db.session.flush()
            selected_customer = customer
        elif customer_choice:
            try:
                customer_id = int(customer_choice)
            except ValueError:
                customer_id = None
            customer = Customer.query.filter_by(id=customer_id, owner_id=g.creator.id).first() if customer_id else None
            if not customer:
                db.session.rollback()
                flash('Choose a customer from your account.', 'error')
                return redirect(request.path)
            selected_customer = customer
        else:
            # Keep accepting requests from older clients that send only customer_name.
            name = request.form.get('customer_name', '').strip()
            if name:
                customer = Customer.query.filter(
                    Customer.owner_id == g.creator.id,
                    func.lower(Customer.name) == name.lower(),
                ).first()
                if not customer:
                    customer = Customer(owner_id=g.creator.id, name=name)
                    db.session.add(customer)
                    db.session.flush()
                selected_customer = customer
        platform_id = request.form.get('platform_id') or None
        payment_method_id = request.form.get('payment_method_id') or None
        platform = Platform.query.filter_by(id=platform_id, owner_id=g.creator.id, active=True).first() if platform_id else None
        payment = PaymentMethod.query.filter_by(id=payment_method_id, owner_id=g.creator.id, active=True).first() if payment_method_id else None
        if platform_id and not platform or payment_method_id and not payment:
            db.session.rollback()
            flash('Choose an active platform and payment method from your account.', 'error')
            return redirect(request.path)
        total=Decimal('0')
        pids=request.form.getlist('product_id'); bids=request.form.getlist('bundle_id'); qtys=request.form.getlist('quantity'); prices=request.form.getlist('unit_price'); hours=request.form.getlist('labor_hours')
        for i,pid in enumerate(pids):
            if not pid: continue
            try:
                product = Product.query.filter_by(id=int(pid), owner_id=g.creator.id, active=True).first()
            except ValueError:
                product = None
            if not product:
                db.session.rollback()
                flash('Choose an active product from your account.', 'error')
                return redirect(request.path)
            q=money(qtys[i] if i<len(qtys) else 1); price=money(prices[i] if i<len(prices) else 0); h=money(hours[i] if i<len(hours) else 0)
            bundle_id = None
            if i<len(bids) and bids[i]:
                try:
                    bundle_id = int(bids[i])
                except ValueError:
                    bundle_id = -1
                if not any(bundle.id == bundle_id and bundle.active for bundle in product.bundles):
                    db.session.rollback()
                    flash('Choose a pricing option from the selected product.', 'error')
                    return redirect(request.path)
            item=SaleItem(sale=sale, product_id=product.id, bundle_id=bundle_id, quantity=q, unit_price=price, labor_hours=h); db.session.add(item); db.session.flush()
            field_ids=request.form.getlist(f'field_id_{i}'); field_values=request.form.getlist(f'field_value_{i}')
            for j,fid in enumerate(field_ids):
                try:
                    field_id = int(fid)
                except ValueError:
                    continue
                if any(field.id == field_id for field in product.fields):
                    db.session.add(SaleItemField(sale_item=item, product_field_id=field_id, value=field_values[j] if j<len(field_values) else ''))
            addon_ids=request.form.getlist(f'addon_id_{i}'); addon_qtys=request.form.getlist(f'addon_quantity_{i}')
            product_addons={a.id:a for a in product.addons if a.active}
            for aid in addon_ids:
                try:
                    aid_int = int(aid)
                except ValueError:
                    continue
                if aid_int not in product_addons: continue
                a=product_addons[aid_int]
                aq=money(request.form.get(f'addon_quantity_{i}_{aid_int}', '1'))
                db.session.add(SaleItemAddon(
                    sale_item=item, product_addon_id=a.id, quantity=aq,
                    unit_price=a.price, price_mode=a.price_mode,
                ))
                item.labor_hours = (item.labor_hours or Decimal('0')) + (a.labor_hours or Decimal('0')) * aq
                quantity_factor = q if a.price_mode == 'per_quantity' else Decimal('1')
                total += aq*a.price*quantity_factor
            total += q*price
        sale.sale_date = sale_date
        sale.due_date = due_date
        sale.sale_category = sale_category
        sale.status = status
        sale.paid = paid
        sale.customer = selected_customer
        sale.platform_id = platform.id if platform else None
        sale.payment_method_id = payment.id if payment else None
        sale.payment_detail = request.form.get('payment_detail')
        sale.notes = request.form.get('notes')
        sale.total_amount = total
        db.session.commit()
        if is_new_sale:
            _create_sale_notification(sale)
            db.session.commit()
            _send_sale_notification_email(sale)
        flash('Sale recorded.' if is_new_sale else 'Sale updated.', 'success')
        return redirect(url_for('sales'))
    selected_items=[]
    if sale:
        for item in sale.items:
            selected_items.append({'product_id': item.product_id, 'bundle_id': item.bundle_id, 'quantity': str(item.quantity or 1), 'unit_price': str(item.unit_price or 0), 'labor_hours': str(item.labor_hours or 0), 'fields': {str(x.product_field_id): x.value for x in item.fields}, 'addons': {str(x.product_addon_id): str(x.quantity or 1) for x in item.addons}})
    return render_template('sales/form.html', sale=sale, products=products, customers=customers,
        platforms=Platform.query.filter_by(owner_id=g.creator.id, active=True).all(), payments=PaymentMethod.query.filter_by(owner_id=g.creator.id, active=True).all(),
        product_data=_product_json(products), selected_items=selected_items, sale_categories=sale_categories,
        sale_statuses=SALE_STATUSES,
        now=(sale.sale_date if sale else datetime.utcnow()).strftime('%Y-%m-%dT%H:%M'))

@app.route('/sales/<int:id>/delete', methods=['POST'])
def sale_delete(id):
    sale = Sale.query.get_or_404(id)
    if not _form_version_matches(sale):
        return redirect(url_for('sales'))
    db.session.delete(sale); db.session.commit(); flash('Sale removed.','success'); return redirect(url_for('sales'))

def _sales_return_url():
    target = request.form.get('next', '')
    if target == '/sales' or target.startswith('/sales?'):
        return target
    return url_for('sales')

@app.route('/sales/<int:id>/status', methods=['POST'])
def sale_status_update(id):
    sale = Sale.query.get_or_404(id)
    if not _form_version_matches(sale):
        return redirect(_sales_return_url())
    status = request.form.get('status', '')
    if status not in SALE_STATUSES:
        flash('Choose a valid sale status.', 'error')
    else:
        sale.status = status
        db.session.commit()
        flash('Sale status updated.', 'success')
    return redirect(_sales_return_url())

@app.route('/sales/<int:id>/paid', methods=['POST'])
def sale_paid_toggle(id):
    sale = Sale.query.get_or_404(id)
    if not _form_version_matches(sale):
        return redirect(_sales_return_url())
    sale.paid = not sale.paid
    db.session.commit()
    flash('Payment status updated.', 'success')
    return redirect(_sales_return_url())

@app.route('/customers')
def customers():
    customers_query = db.session.query(Customer, func.count(Sale.id).label('purchase_count'),
        func.coalesce(func.sum(Sale.total_amount), 0).label('total_spent'),
        func.coalesce(func.avg(Sale.total_amount), 0).label('average_spent'),
        func.max(Sale.sale_date).label('last_purchase')
    ).outerjoin(Sale, (Customer.id == Sale.customer_id) & (Sale.owner_id == g.creator.id)).filter(
        Customer.owner_id == g.creator.id
    ).group_by(Customer.id).order_by(Customer.name)
    pagination = customers_query.paginate(
        page=max(request.args.get('page', 1, type=int), 1), per_page=50, error_out=False
    )
    rows = pagination.items
    customer_ids = [row[0].id for row in rows]
    favorite_rows = db.session.query(
        Sale.customer_id, Product.name,
        func.sum(SaleItem.quantity).label('quantity'),
    ).join(SaleItem, SaleItem.product_id == Product.id).join(
        Sale, Sale.id == SaleItem.sale_id
    ).join(Customer, Customer.id == Sale.customer_id).filter(
        Customer.owner_id == g.creator.id, Sale.owner_id == g.creator.id,
        Product.owner_id == g.creator.id,
        Sale.customer_id.in_(customer_ids),
    ).group_by(Sale.customer_id, Product.id, Product.name).order_by(
        Sale.customer_id, func.sum(SaleItem.quantity).desc(), Product.id,
    ).all()
    favorites = {}
    for customer_id, product_name, quantity in favorite_rows:
        favorites.setdefault(customer_id, (product_name, quantity))
    customer_stats = []
    for customer, purchase_count, total_spent, average_spent, last_purchase in rows:
        favorite = favorites.get(customer.id)
        customer_stats.append({'customer': customer, 'purchase_count': purchase_count, 'total_spent': total_spent,
            'average_spent': average_spent, 'last_purchase': last_purchase,
            'favorite_product': favorite[0] if favorite else None,
            'favorite_quantity': favorite[1] if favorite else 0})
    top_spender_row = db.session.query(
        Customer, func.coalesce(func.sum(Sale.total_amount), 0).label('total_spent')
    ).outerjoin(Sale, (Customer.id == Sale.customer_id) & (Sale.owner_id == g.creator.id)).filter(
        Customer.owner_id == g.creator.id
    ).group_by(Customer.id).order_by(func.coalesce(func.sum(Sale.total_amount), 0).desc(), Customer.name).first()
    frequent_customer_row = db.session.query(
        Customer, func.count(Sale.id).label('purchase_count')
    ).outerjoin(Sale, (Customer.id == Sale.customer_id) & (Sale.owner_id == g.creator.id)).filter(
        Customer.owner_id == g.creator.id
    ).group_by(Customer.id).order_by(func.count(Sale.id).desc(), Customer.name).first()
    top_spender = {'customer': top_spender_row[0], 'total_spent': top_spender_row[1]} if top_spender_row else None
    most_frequent = {'customer': frequent_customer_row[0], 'purchase_count': frequent_customer_row[1]} if frequent_customer_row else None
    return render_template(
        'customers/list.html', customers=customer_stats, top_spender=top_spender,
        most_frequent=most_frequent, total_customers=pagination.total,
        pagination=pagination,
        previous_url=url_for('customers', page=pagination.prev_num) if pagination.has_prev else None,
        next_url=url_for('customers', page=pagination.next_num) if pagination.has_next else None,
    )

@app.route('/customers/new', methods=['GET','POST'])
def customer_new(): return _customer_form(None)

@app.route('/customers/<int:id>/edit', methods=['GET','POST'])
def customer_edit(id): return _customer_form(Customer.query.get_or_404(id))

def _customer_form(customer):
    if customer is not None and request.method == 'POST' and not _form_version_matches(customer):
        return redirect(url_for('customer_edit', id=customer.id))
    if request.method=='POST':
        if customer is None: customer=Customer(owner_id=g.creator.id); db.session.add(customer)
        customer.name=request.form['name'].strip(); customer.email=request.form.get('email'); customer.username=request.form.get('username'); customer.notes=request.form.get('notes'); db.session.commit(); flash('Customer saved.','success'); return redirect(url_for('customers'))
    return render_template('customers/form.html', customer=customer)

@app.route('/customers/<int:id>/delete', methods=['POST'])
def customer_delete(id):
    c=Customer.query.get_or_404(id)
    if not _form_version_matches(c):
        return redirect(url_for('customers'))
    if c.sales:
        flash('Customer cannot be deleted while sales are attached. Edit the customer or remove/assign those sales first.','error')
    else:
        db.session.delete(c); db.session.commit(); flash('Customer deleted.','success')
    return redirect(url_for('customers'))

@app.route('/leaderboards')
def leaderboards():
    boards_query = db.session.query(Leaderboard, func.count(LeaderboardEntry.id).label('entry_count')).outerjoin(
        LeaderboardEntry, Leaderboard.id == LeaderboardEntry.leaderboard_id
    ).filter(Leaderboard.owner_id == g.creator.id).group_by(Leaderboard.id).order_by(Leaderboard.name)
    pagination = boards_query.paginate(
        page=max(request.args.get('page', 1, type=int), 1), per_page=50, error_out=False
    )
    return render_template(
        'leaderboards/list.html', leaderboards=pagination.items, pagination=pagination,
        previous_url=url_for('leaderboards', page=pagination.prev_num) if pagination.has_prev else None,
        next_url=url_for('leaderboards', page=pagination.next_num) if pagination.has_next else None,
    )

@app.route('/leaderboards/new', methods=['POST'])
def leaderboard_new():
    name = request.form.get('name', '').strip()
    score_label = request.form.get('score_label', 'Score').strip() or 'Score'
    sort_order = request.form.get('sort_order', 'desc')
    if not name:
        flash('Enter a name for this leaderboard.', 'error')
        return redirect(url_for('leaderboards'))
    if sort_order not in {'asc', 'desc'}:
        sort_order = 'desc'
    board = Leaderboard(
        owner_id=g.creator.id,
        name=name,
        description=request.form.get('description', '').strip() or None,
        score_label=score_label,
        sort_order=sort_order,
        is_public=request.form.get('is_public', '1') == '1',
    )
    db.session.add(board)
    db.session.commit()
    flash('Leaderboard created.', 'success')
    return redirect(url_for('leaderboard_detail', id=board.id))

@app.route('/leaderboards/<int:id>')
def leaderboard_detail(id):
    board = Leaderboard.query.filter_by(id=id, owner_id=g.creator.id).first_or_404()
    score_order = LeaderboardEntry.score.desc() if board.sort_order == 'desc' else LeaderboardEntry.score.asc()
    entries = db.session.query(LeaderboardEntry).join(Customer).filter(
        LeaderboardEntry.leaderboard_id == board.id, Customer.owner_id == g.creator.id
    ).order_by(score_order, func.lower(Customer.name)).all()
    existing_customer_ids = {entry.customer_id for entry in entries}
    customers = Customer.query.filter_by(owner_id=g.creator.id).order_by(Customer.name).all()
    customers = [customer for customer in customers if customer.id not in existing_customer_ids]
    return render_template(
        'leaderboards/detail.html', board=board, entries=entries, customers=customers
    )

@app.route('/leaderboards/<int:id>/settings', methods=['POST'])
def leaderboard_settings(id):
    board = Leaderboard.query.get_or_404(id)
    if not _form_version_matches(board):
        return redirect(url_for('leaderboard_detail', id=board.id))
    name = request.form.get('name', '').strip()
    score_label = request.form.get('score_label', '').strip()
    sort_order = request.form.get('sort_order', '')
    if not name or not score_label or sort_order not in {'asc', 'desc'}:
        flash('Enter a name and score label, and choose a valid ranking order.', 'error')
    else:
        board.name = name
        board.description = request.form.get('description', '').strip() or None
        board.score_label = score_label
        board.sort_order = sort_order
        board.is_public = request.form.get('is_public') == '1'
        db.session.commit()
        flash('Leaderboard settings saved.', 'success')
    return redirect(url_for('leaderboard_detail', id=board.id))

@app.route('/leaderboards/<int:id>/entries', methods=['POST'])
def leaderboard_entry_add(id):
    board = Leaderboard.query.get_or_404(id)
    customer_id = request.form.get('customer_id', type=int)
    score = _leaderboard_score(request.form.get('score', ''))
    use_username = request.form.get('use_username') == '1'
    customer = Customer.query.filter_by(id=customer_id).first() if customer_id else None
    if customer is None or score is None:
        flash('Choose a customer and enter a valid score.', 'error')
    elif use_username and not (customer.username or '').strip():
        flash('Add a username to this customer before displaying it publicly.', 'error')
    elif LeaderboardEntry.query.filter_by(leaderboard_id=board.id, customer_id=customer.id).first():
        flash('That customer is already on this leaderboard.', 'error')
    else:
        db.session.add(LeaderboardEntry(
            leaderboard=board, customer=customer, score=score, use_username=use_username
        ))
        db.session.commit()
        flash('Customer added to leaderboard.', 'success')
    return redirect(url_for('leaderboard_detail', id=board.id))

@app.route('/leaderboards/<int:id>/entries/<int:entry_id>/score', methods=['POST'])
def leaderboard_entry_score(id, entry_id):
    board = Leaderboard.query.get_or_404(id)
    entry = LeaderboardEntry.query.filter_by(id=entry_id, leaderboard_id=board.id).first_or_404()
    if not _form_version_matches(entry):
        return redirect(url_for('leaderboard_detail', id=board.id))
    score = _leaderboard_score(request.form.get('score', ''))
    use_username = request.form.get('use_username') == '1'
    if score is None:
        flash('Enter a valid score.', 'error')
    elif use_username and not (entry.customer.username or '').strip():
        flash('Add a username to this customer before displaying it publicly.', 'error')
    else:
        entry.score = score
        entry.use_username = use_username
        db.session.commit()
        flash('Score updated.', 'success')
    return redirect(url_for('leaderboard_detail', id=board.id))

@app.route('/leaderboards/<int:id>/entries/<int:entry_id>/remove', methods=['POST'])
def leaderboard_entry_remove(id, entry_id):
    board = Leaderboard.query.get_or_404(id)
    entry = LeaderboardEntry.query.filter_by(id=entry_id, leaderboard_id=board.id).first_or_404()
    if not _form_version_matches(entry):
        return redirect(url_for('leaderboard_detail', id=board.id))
    db.session.delete(entry)
    db.session.commit()
    flash('Customer removed from leaderboard.', 'success')
    return redirect(url_for('leaderboard_detail', id=board.id))

@app.route('/leaderboards/<int:id>/delete', methods=['POST'])
def leaderboard_delete(id):
    board = Leaderboard.query.get_or_404(id)
    if not _form_version_matches(board):
        return redirect(url_for('leaderboard_detail', id=board.id))
    db.session.delete(board)
    db.session.commit()
    flash('Leaderboard deleted.', 'success')
    return redirect(url_for('leaderboards'))

@app.route('/analytics')
def analytics():
    products = db.session.query(Product.name, func.coalesce(func.sum(SaleItem.quantity),0).label('qty'),
        func.coalesce(func.sum(SaleItem.quantity*SaleItem.unit_price),0).label('revenue'),
        func.coalesce(func.sum(SaleItem.quantity*SaleItem.labor_hours),0).label('hours')).join(
        SaleItem, Product.id==SaleItem.product_id).join(Sale, Sale.id==SaleItem.sale_id).filter(
        Product.owner_id == g.creator.id, Sale.owner_id == g.creator.id).group_by(Product.id).order_by(
        func.sum(SaleItem.quantity*SaleItem.unit_price).desc()).all()
    platform_rows = db.session.query(Platform.name, func.coalesce(func.sum(Sale.total_amount),0).label('revenue')).join(
        Sale, Platform.id==Sale.platform_id).filter(Platform.owner_id == g.creator.id, Sale.owner_id == g.creator.id).group_by(
        Platform.id).order_by(func.sum(Sale.total_amount).desc()).all()
    platforms = [{'name': r[0], 'revenue': float(r[1] or 0)} for r in platform_rows]
    payments = db.session.query(PaymentMethod.name, func.coalesce(func.sum(Sale.total_amount),0).label('revenue')).join(
        Sale, PaymentMethod.id==Sale.payment_method_id).filter(PaymentMethod.owner_id == g.creator.id,
        Sale.owner_id == g.creator.id).group_by(PaymentMethod.id).all()
    if db.engine.dialect.name == 'sqlite':
        monthly_rows = db.session.query(func.strftime('%Y-%m', Sale.sale_date).label('month'),
            func.coalesce(func.sum(Sale.total_amount),0).label('revenue')).filter(Sale.owner_id == g.creator.id).group_by(
            func.strftime('%Y-%m', Sale.sale_date)).order_by(func.strftime('%Y-%m', Sale.sale_date)).all()
    else:
        monthly_rows = db.session.execute(text("SELECT to_char(date_trunc('month', sale_date), 'YYYY-MM') AS month, COALESCE(SUM(total_amount),0) AS revenue FROM sale WHERE owner_id = :owner_id GROUP BY date_trunc('month', sale_date) ORDER BY date_trunc('month', sale_date)"), {'owner_id': g.creator.id}).fetchall()
    monthly_revenue = [{'month': r.month, 'revenue': float(r.revenue or 0)} for r in monthly_rows[-12:]]
    top_buyers = db.session.query(Customer.name, func.count(Sale.id).label('purchases'),
        func.coalesce(func.sum(Sale.total_amount),0).label('spent')).join(Sale, Customer.id==Sale.customer_id).group_by(
        Customer.id).filter(Customer.owner_id == g.creator.id, Sale.owner_id == g.creator.id).order_by(
        func.sum(Sale.total_amount).desc()).limit(10).all()
    addon_quantity_factor = case(
        (SaleItemAddon.price_mode == 'per_quantity', SaleItem.quantity), else_=1
    )
    addon_revenue = SaleItemAddon.quantity * SaleItemAddon.unit_price * addon_quantity_factor
    top_addons = db.session.query(ProductAddon.name, func.coalesce(func.sum(SaleItemAddon.quantity),0).label('qty'),
        func.coalesce(func.sum(addon_revenue),0).label('revenue')).join(
        SaleItemAddon, ProductAddon.id==SaleItemAddon.product_addon_id).join(
        Product, Product.id == ProductAddon.product_id).join(SaleItem, SaleItem.id == SaleItemAddon.sale_item_id).join(
        Sale, Sale.id == SaleItem.sale_id).filter(Product.owner_id == g.creator.id, Sale.owner_id == g.creator.id).group_by(ProductAddon.id).order_by(
        func.sum(addon_revenue).desc()).limit(10).all()
    return render_template('analytics/index.html', products=products, platforms=platforms, payments=payments,
        monthly_revenue=monthly_revenue, top_buyers=top_buyers, top_addons=top_addons)

@app.route('/settings/billing')
def seller_billing():
    account = g.creator
    if request.args.get('checkout') == 'complete' and account.stripe_subscription_status not in ACTIVE_SUBSCRIPTION_STATUSES:
        flash('Stripe is confirming your subscription. Access will unlock when the signed webhook arrives.', 'success')
    pending_checkout_url = None
    if account.stripe_subscription_status == 'checkout_pending' and account.stripe_subscription_checkout_session_id and app.config.get('STRIPE_SECRET_KEY'):
        try:
            pending_checkout = stripe.checkout.Session.retrieve(
                account.stripe_subscription_checkout_session_id,
                api_key=app.config['STRIPE_SECRET_KEY'],
            )
            if pending_checkout.get('status') == 'open':
                pending_checkout_url = pending_checkout.url
            elif pending_checkout.get('status') == 'expired':
                _expire_subscription_checkout(pending_checkout)
        except stripe.StripeError:
            logging.exception('Could not retrieve pending subscription checkout')
    return render_template(
        'settings/billing.html', account=account,
        billing_exempt=account.is_admin or account.subscription_exempt,
        stripe_ready=bool(app.config.get('STRIPE_SECRET_KEY') and app.config.get('STRIPE_SELLER_PRICE_ID')),
        has_billing_customer=bool(account.stripe_customer_id),
        subscription_active=account.stripe_subscription_status in ACTIVE_SUBSCRIPTION_STATUSES,
        needs_payment_update=account.stripe_subscription_status in ('past_due', 'unpaid', 'incomplete'),
        checkout_pending=account.stripe_subscription_status == 'checkout_pending',
        pending_checkout_url=pending_checkout_url,
    )

@app.route('/settings/billing/checkout', methods=['POST'])
def seller_billing_checkout():
    account = g.creator
    if account.is_admin or account.subscription_exempt:
        flash('This account is exempt from the seller subscription.', 'info')
        return redirect(url_for('dashboard'))
    if not (app.config.get('STRIPE_SECRET_KEY') and app.config.get('STRIPE_SELLER_PRICE_ID')):
        flash('Seller subscriptions are not configured yet. Set STRIPE_SECRET_KEY and STRIPE_SELLER_PRICE_ID.', 'error')
        return redirect(url_for('seller_billing'))
    if account.email.endswith('@example.invalid'):
        flash('Add a real email address in Account Settings before starting a subscription.', 'error')
        return redirect(url_for('account_settings'))
    if account.stripe_subscription_status in ACTIVE_SUBSCRIPTION_STATUSES:
        return redirect(url_for('seller_billing'))
    if account.stripe_subscription_status == 'checkout_pending':
        if account.stripe_subscription_checkout_session_id:
            try:
                pending_checkout = stripe.checkout.Session.retrieve(
                    account.stripe_subscription_checkout_session_id,
                    api_key=app.config['STRIPE_SECRET_KEY'],
                )
                if pending_checkout.get('status') == 'open':
                    return redirect(pending_checkout.url, code=303)
                if pending_checkout.get('status') == 'complete':
                    flash('Stripe is confirming your subscription. Please wait for webhook confirmation.', 'success')
                    return redirect(url_for('seller_billing'))
                _expire_subscription_checkout(pending_checkout)
                db.session.refresh(account)
            except stripe.StripeError as error:
                flash(getattr(error, 'user_message', None) or 'Could not resume the pending Stripe checkout.', 'error')
                return redirect(url_for('seller_billing'))
        else:
            flash('A subscription checkout is already being created. Refresh this page in a moment.', 'error')
            return redirect(url_for('seller_billing'))
    checkout_args = {
        'mode': 'subscription',
        'line_items': [{'price': app.config['STRIPE_SELLER_PRICE_ID'], 'quantity': 1}],
        'client_reference_id': str(account.id),
        'metadata': {'seller_id': str(account.id)},
        'subscription_data': {'metadata': {'seller_id': str(account.id)}},
        'idempotency_key': f'seller-subscription-{account.id}-{account.version_id}',
        'success_url': url_for('seller_billing', _external=True) + '?checkout=complete&session_id={CHECKOUT_SESSION_ID}',
        'cancel_url': url_for('seller_billing', _external=True) + '?checkout=canceled',
        'api_key': app.config['STRIPE_SECRET_KEY'],
    }
    if account.stripe_customer_id:
        checkout_args['customer'] = account.stripe_customer_id
    else:
        checkout_args['customer_email'] = account.email
    try:
        checkout = stripe.checkout.Session.create(**checkout_args)
        account.stripe_subscription_status = 'checkout_pending'
        account.stripe_subscription_checkout_session_id = checkout.id
        db.session.commit()
        return redirect(checkout.url, code=303)
    except stripe.StripeError as error:
        db.session.rollback()
        flash(getattr(error, 'user_message', None) or 'Stripe could not start the subscription checkout.', 'error')
        return redirect(url_for('seller_billing'))

@app.route('/settings/billing/portal', methods=['POST'])
def seller_billing_portal():
    account = g.creator
    if account.is_admin or account.subscription_exempt:
        return redirect(url_for('dashboard'))
    if not account.stripe_customer_id or not app.config.get('STRIPE_SECRET_KEY'):
        flash('No Stripe billing profile is available yet.', 'error')
        return redirect(url_for('seller_billing'))
    try:
        portal = stripe.billing_portal.Session.create(
            customer=account.stripe_customer_id,
            return_url=url_for('seller_billing', _external=True),
            api_key=app.config['STRIPE_SECRET_KEY'],
        )
        return redirect(portal.url, code=303)
    except stripe.StripeError as error:
        flash(getattr(error, 'user_message', None) or 'Stripe could not open the billing portal.', 'error')
        return redirect(url_for('seller_billing'))

@app.route('/settings/account', methods=['GET', 'POST'])
def account_settings():
    account = g.creator
    if request.method == 'POST':
        if not _form_version_matches(account):
            return redirect(url_for('account_settings'))
        username = request.form.get('username', '').strip().lower()
        email = request.form.get('email', '').strip().lower()
        display_name = request.form.get('display_name', '').strip()
        new_password = request.form.get('new_password', '')
        password_confirm = request.form.get('password_confirm', '')
        current_password = request.form.get('current_password', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', username):
            flash('Choose a username with 3 to 64 letters, numbers, dots, dashes, or underscores.', 'error')
        elif not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            flash('Enter a valid email address.', 'error')
        elif CreatorAccount.query.filter(
            func.lower(CreatorAccount.email) == email, CreatorAccount.id != account.id
        ).first() or StoreCustomer.query.filter(func.lower(StoreCustomer.email) == email).first():
            flash('That email is already in use.', 'error')
        elif CreatorAccount.query.filter(
            func.lower(CreatorAccount.username) == username, CreatorAccount.id != account.id
        ).first():
            flash('That username is already in use.', 'error')
        elif len(display_name) > 120:
            flash('Display names must be 120 characters or fewer.', 'error')
        elif (new_password or password_confirm) and not check_password_hash(account.password_hash, current_password):
            flash('Enter your current password to change it.', 'error')
        elif new_password and len(new_password) < 10:
            flash('Use a password with at least 10 characters.', 'error')
        elif new_password != password_confirm:
            flash('The new passwords do not match.', 'error')
        else:
            account.username = username
            account.email = email
            account.display_name = display_name or None
            account.banner_notifications = request.form.get('banner_notifications') == '1'
            account.email_notifications = request.form.get('email_notifications') == '1'
            if new_password:
                account.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash('Account settings saved.', 'success')
            return redirect(url_for('account_settings'))
    return render_template('settings/account.html', account=account)

def _stripe_account_link(account):
    return stripe.AccountLink.create(
        account=account.stripe_account_id,
        refresh_url=url_for('stripe_connect_setup', _external=True),
        return_url=url_for('stripe_connect_return', _external=True),
        type='account_onboarding',
        api_key=app.config['STRIPE_SECRET_KEY'],
    )

@app.route('/settings/stripe/connect', methods=['GET', 'POST'])
def stripe_connect_setup():
    account = g.creator
    if not app.config.get('STRIPE_SECRET_KEY'):
        flash('Stripe is not configured yet. Add STRIPE_SECRET_KEY on the server first.', 'error')
        return redirect(url_for('account_settings'))
    try:
        if not account.stripe_account_id:
            connected_account = stripe.Account.create(
                type='express',
                country=app.config.get('STRIPE_CONNECT_COUNTRY', 'US'),
                capabilities={
                    'card_payments': {'requested': True},
                    'transfers': {'requested': True},
                },
                api_key=app.config['STRIPE_SECRET_KEY'],
            )
            account.stripe_account_id = connected_account.id
            db.session.commit()
        link = _stripe_account_link(account)
        return redirect(link.url)
    except stripe.StripeError as error:
        db.session.rollback()
        flash(getattr(error, 'user_message', None) or 'Stripe could not start seller onboarding.', 'error')
        return redirect(url_for('account_settings'))

@app.route('/settings/stripe/return')
def stripe_connect_return():
    account = g.creator
    if not account.stripe_account_id or not app.config.get('STRIPE_SECRET_KEY'):
        flash('Connect Stripe to finish setting up seller payments.', 'error')
        return redirect(url_for('account_settings'))
    try:
        connected_account = stripe.Account.retrieve(
            account.stripe_account_id, api_key=app.config['STRIPE_SECRET_KEY']
        )
        account.stripe_charges_enabled = bool(connected_account.charges_enabled)
        account.stripe_details_submitted = bool(connected_account.details_submitted)
        if not account.stripe_charges_enabled:
            account.accepting_orders = False
        db.session.commit()
        flash('Stripe account status refreshed.', 'success')
    except stripe.StripeError as error:
        db.session.rollback()
        flash(getattr(error, 'user_message', None) or 'Could not refresh Stripe account status.', 'error')
    return redirect(url_for('account_settings'))

@app.route('/settings/stripe/purchases', methods=['POST'])
def stripe_purchase_settings():
    account = g.creator
    enable = request.form.get('accepting_orders') == '1'
    if enable and not (account.stripe_account_id and account.stripe_charges_enabled and account.stripe_details_submitted):
        flash('Finish Stripe onboarding and enable charges before accepting purchases.', 'error')
    else:
        account.accepting_orders = enable
        db.session.commit()
        flash('Public purchasing ' + ('enabled.' if enable else 'disabled.'), 'success')
    return redirect(url_for('account_settings'))

@app.route('/settings', methods=['GET','POST'])
def settings():
    if request.method=='POST':
        model={'category':Category,'platform':Platform,'payment':PaymentMethod}[request.form['kind']]
        name=request.form['name'].strip()
        if name: db.session.add(model(owner_id=g.creator.id, name=name)); db.session.commit(); flash('Setting added.','success')
        return redirect(url_for('settings'))
    return render_template('settings/index.html', categories=Category.query.filter_by(owner_id=g.creator.id).order_by(Category.name).all(), platforms=Platform.query.filter_by(owner_id=g.creator.id).order_by(Platform.name).all(), payments=PaymentMethod.query.filter_by(owner_id=g.creator.id).order_by(PaymentMethod.name).all())

@app.route('/settings/<kind>/<int:id>/edit', methods=['GET','POST'])
def setting_edit(kind,id):
    model={'category':Category,'platform':Platform,'payment':PaymentMethod}.get(kind)
    if not model: return redirect(url_for('settings'))
    obj=model.query.get_or_404(id)
    if request.method == 'POST' and not _form_version_matches(obj):
        return redirect(url_for('setting_edit', kind=kind, id=id))
    if request.method=='POST':
        obj.name=request.form['name'].strip();
        if hasattr(obj,'active'): obj.active=bool(request.form.get('active'))
        db.session.commit(); flash('Setting updated.','success'); return redirect(url_for('settings'))
    return render_template('settings/edit.html', kind=kind, title={'category':'Category','platform':'Platform','payment':'Payment Method'}[kind], obj=obj)

@app.route('/settings/<kind>/<int:id>/delete', methods=['POST'])
def setting_delete(kind,id):
    model={'category':Category,'platform':Platform,'payment':PaymentMethod}.get(kind)
    if not model: return redirect(url_for('settings'))
    obj=model.query.get_or_404(id)
    if not _form_version_matches(obj):
        return redirect(url_for('settings'))
    if kind=='category' and Product.query.filter_by(category_id=id).first(): flash('Category is in use by products.','error')
    elif kind=='platform' and Sale.query.filter_by(platform_id=id).first(): flash('Platform is in use by sales.','error')
    elif kind=='payment' and Sale.query.filter_by(payment_method_id=id).first(): flash('Payment method is in use by sales.','error')
    else: db.session.delete(obj); db.session.commit(); flash('Setting deleted.','success')
    return redirect(url_for('settings'))

if __name__ == '__main__': app.run(debug=True)
