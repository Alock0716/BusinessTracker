# Business Tracker

A real Flask + SQLAlchemy business sales tracking application. It uses SQLite by default and can be pointed at MySQL/PostgreSQL through `DATABASE_URL`.

## Run locally

1. Create a virtual environment: `python -m venv .venv`
2. Activate it.
3. Install dependencies: `pip install -r requirements.txt`
4. Start the app: `python app.py`
5. Open `http://127.0.0.1:5000`

The SQLite database is created automatically at `instance/business_tracker.db`.

## Included

- Dashboard
- Products with configurable custom fields
- Product add-ons with descriptions, prices, and labor hours
- Customers
- Sales with buyer/platform/payment information
- Gift-card/product-exchange detail field
- Quantity and labor-hour tracking
- Product, platform and payment analytics
- Categories, platforms and payment method configuration

## Database

The schema is defined in `models.py` with SQLAlchemy. Set `DATABASE_URL` to a supported SQLAlchemy database URL to use MySQL or PostgreSQL instead of SQLite.

Example MySQL URL:
`mysql+pymysql://user:password@localhost/business_tracker`

For MySQL, install `PyMySQL` in addition to the requirements.
