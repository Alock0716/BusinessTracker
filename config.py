import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'change-this-secret-key')
    database_url = os.environ.get('DATABASE_URL', 'sqlite:///' + os.path.join(BASE_DIR, 'instance', 'business_tracker.db'))
    # Render/managed Postgres providers may expose the URL as postgres://.
    if database_url.startswith('postgres://'):
        database_url = 'postgresql://' + database_url[len('postgres://'):]
    if database_url.startswith('postgresql://'):
        database_url = database_url.replace('postgresql://', 'postgresql+psycopg2://', 1)
    SQLALCHEMY_DATABASE_URI = database_url
    LOGIN_USERNAME = os.environ.get('LOGIN_USERNAME', 'admin')
    LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD', 'change-me')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
