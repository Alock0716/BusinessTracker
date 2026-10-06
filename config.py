import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'KayKohl3')
    database_url = os.environ.get('DATABASE_URL', 'postgresql://neondb_owner:npg_W4x8NjkTZdbL@ep-winter-sound-b4na4aod-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require')
    SQLALCHEMY_DATABASE_URI = database_url
    LOGIN_USERNAME = os.environ.get('LOGIN_USERNAME', 'KayKohl')
    LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD', 'KayKohl3!')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
