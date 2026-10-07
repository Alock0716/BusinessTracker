import os
import secrets

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    database_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(BASE_DIR, 'instance', 'business_tracker.db'))
    # Render/managed Postgres providers may expose the URL as postgres://.
    if database_url.startswith('postgres://'):
        database_url = 'postgresql://' + database_url[len('postgres://'):]
    if database_url.startswith('postgresql://'):
        database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)
    SQLALCHEMY_DATABASE_URI = database_url
    LOGIN_USERNAME = os.environ.get('LOGIN_USERNAME', 'KayKohl').strip().lower()
    LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD', 'KayKohl3!')
    LOGIN_EMAIL = os.environ.get('LOGIN_EMAIL', 'admin@example.invalid').strip().lower()
    SQLALCHEMY_ENGINE_OPTIONS = {'pool_pre_ping': True, 'pool_recycle': 1800}
    if database_url.startswith('sqlite:'):
        SQLALCHEMY_ENGINE_OPTIONS['connect_args'] = {'timeout': 30}
    else:
        SQLALCHEMY_ENGINE_OPTIONS.update({
            'pool_size': int(os.environ.get('DB_POOL_SIZE', '5')),
            'max_overflow': int(os.environ.get('DB_MAX_OVERFLOW', '5')),
            'pool_timeout': int(os.environ.get('DB_POOL_TIMEOUT', '30')),
        })
    MAIL_SERVER = os.environ.get('MAIL_SERVER', '')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', '587'))
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in {'1', 'true', 'yes'}
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_USERNAME', ''))
    STRIPE_SECRET_KEY = os.environ.get('STRIPE_SECRET_KEY', '')
    STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    STRIPE_SELLER_PRICE_ID = os.environ.get('STRIPE_SELLER_PRICE_ID', '')
    DEVELOPER_CONTACT_EMAIL = os.environ.get('DEVELOPER_CONTACT_EMAIL', '').strip()
    DEVELOPER_DISPLAY_NAME = os.environ.get('DEVELOPER_DISPLAY_NAME', 'the developer').strip()
    STRIPE_CONNECT_COUNTRY = os.environ.get('STRIPE_CONNECT_COUNTRY', 'US')
    STRIPE_CURRENCY = os.environ.get('STRIPE_CURRENCY', 'usd').lower()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
