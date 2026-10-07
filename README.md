# Business Tracker

A real Flask + SQLAlchemy business sales tracking application. It uses SQLite by default and can be pointed at MySQL/PostgreSQL through `DATABASE_URL`.

## Run locally

1. Create a virtual environment: `python -m venv .venv`
2. Activate it.
3. Install dependencies: `pip install -r requirements.txt`
4. Start the app: `python app.py`
5. Open `http://127.0.0.1:5000`

The SQLite database is created automatically at `instance/business_tracker.db`.

## Public site and seller accounts

- The public site at `/` lets visitors search product menus and leaderboards without signing in.
- Each seller has a public page at `/sellers/<username>` and public leaderboard pages under `/sellers/<username>/leaderboards/<id>`.
- Sellers can create an account at `/register`; buyers can optionally register at `/customer/register`. Both use the single email sign-in at `/login`, which routes to the seller workspace or buyer purchase history based on the matching account. New seller accounts require admin approval before seller tools are activated.
- Seller email addresses are required for new accounts and can be changed in Account Settings. Existing sellers receive a temporary `@example.invalid` placeholder during migration and can use their old username once to sign in and add a real email address.
- Configure `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_USE_TLS`, and `MAIL_DEFAULT_SENDER` to enable password-reset and optional new-sale email notifications. Reset links expire after one hour. Set a stable, secret `SECRET_KEY` in the deployment environment; without SMTP, reset requests remain private but no email can be delivered.
- Sellers can control in-site sale banners and optional email alerts in Account Settings. Notifications are also retained in the seller-only `/notifications` hub.
- Approved accounts manage separate products, categories, customers, sales, and leaderboards. They receive removable example records to demonstrate the workflows.
- Admins can manage accounts and download SQLite backups at `/admin`. `LOGIN_USERNAME`, `LOGIN_PASSWORD`, and `LOGIN_EMAIL` seed the administrator account only when that username is absent; startup does not replace an existing account's password. Set all three to private values in Render before production deployment, and never commit credentials. Existing business data remains assigned to its current seller.

## Stripe payments and seller subscriptions

- Use Stripe-hosted Checkout for both platform subscriptions and customer purchases; use the hosted Billing Portal for subscription changes, payment methods, invoices, and cancellation. No card details are collected by this app.
- Approved non-admin sellers must have an active or trialing subscription to use seller tools or show public listings. Platform administrators are exempt. Access changes only after signed Stripe subscription webhooks are processed.
- Admins can waive an individual seller's subscription requirement from `/admin`. If that seller has an active Stripe subscription, the waiver schedules it to stop renewing at period end; it does not refund paid invoices or void outstanding invoices.
- In the platform Stripe account, create a product such as **Seller access**, add a recurring monthly Price, and copy its `price_...` ID. The app does not set a subscription amount; Checkout displays the Price you configure.
- Set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and `STRIPE_SELLER_PRICE_ID` as server environment variables; never commit secrets. Use test-mode values while testing.
- Optionally set `DEVELOPER_CONTACT_EMAIL`, `DEVELOPER_CONTACT_NUMBER`, and `DEVELOPER_DISPLAY_NAME` in the host environment to show contact/video-submission details and identify the recipient of one-time developer tips. Tips use the platform Stripe account and are separate from seller payouts/subscriptions.
- Enable and configure Stripe's customer portal to let sellers update cards, view invoices, and cancel. Set `STRIPE_CONNECT_COUNTRY` for seller onboarding and optionally `STRIPE_CURRENCY` (defaults to `usd`). Connect onboarding/payouts remain separate from the platform subscription.
- Products marked as requiring delivery collect a shipping address and phone in Checkout; configure comma-separated ISO country codes with `STRIPE_SHIPPING_ALLOWED_COUNTRIES` (defaults to the Connect country). Shipping addresses are saved on the sale for seller fulfillment; this does not add a shipping fee.
- In Stripe Workbench > Webhooks, create an event destination with scope **Your account** and payload **Snapshot**, pointing to `https://<your-domain>/stripe/webhook`. Select `checkout.session.completed`, `checkout.session.async_payment_succeeded`, `checkout.session.async_payment_failed`, `checkout.session.expired`, `customer.subscription.created`, `customer.subscription.updated`, and `customer.subscription.deleted`. Copy that endpoint's `whsec_...` signing secret; test and live endpoints have different secrets.
- For local testing, install and authenticate the Stripe CLI, then run `stripe listen --forward-to 127.0.0.1:5000/stripe/webhook`. Put the CLI's `whsec_...` value in `STRIPE_WEBHOOK_SECRET` in the app's environment. The webhook, not the browser return URL, activates or revokes seller access.
- To test in PowerShell, set the test values before starting Flask:

```powershell
$env:STRIPE_SECRET_KEY = "sk_test_..."
$env:STRIPE_SELLER_PRICE_ID = "price_..."
$env:STRIPE_WEBHOOK_SECRET = "whsec_..."
python app.py
```

- Stripe dashboard checklist: activate the platform account and complete business verification; while in test mode, create a recurring monthly Seller Access product/Price; configure the customer portal to allow payment-method updates, invoice viewing, and cancellation; then add the webhook endpoint and select the events above. Use the platform account's secret key, not a seller's Connect key and not a `pk_...` publishable key.
- To use Connect payouts, also enable Stripe Connect on the platform account and complete a separate Express onboarding for each seller. Platform subscriptions pay the platform; menu purchases continue to route to sellers through Connect.
- Approve a seller account, set a real seller email, and use its Subscription page to start checkout. Use Stripe's test card `4242 4242 4242 4242` with any future expiration and CVC. To go live, create a live-mode Price, live API key, and production webhook secret, then update deployment environment variables.
- Public checkout works without a buyer account. Optional buyer accounts at `/customer/register` save purchase history across sellers.
- Checkout uses Stripe Connect destination charges and does not add a platform fee. Test mode can be used with Stripe test keys before enabling live payments.

## Included

- Dashboard
- Multi-seller accounts with isolated products, categories, customers, sales, and leaderboards
- Public storefront search by seller username, product name/description, and leaderboard name/description
- Products with configurable custom fields
- Reusable product tags for product discovery and sales filtering
- Product add-ons with descriptions, labor hours, and flat or per-quantity pricing with optional units
- Customers
- Customizable customer leaderboards with configurable score labels and ranking order
- Sales dashboard with buyer/platform/payment information, workflow and payment status, due dates, categories, advanced search/filters, and sorting
- Gift-card/product-exchange detail field
- Quantity and labor-hour tracking
- Product, platform and payment analytics
- Categories, platforms and payment method configuration

## Database

The schema is defined in `models.py` with SQLAlchemy. Set `DATABASE_URL` to a supported SQLAlchemy database URL to use MySQL or PostgreSQL instead of SQLite.

SQLite is configured for WAL mode and a 30-second busy timeout. This improves concurrent reads and allows brief write contention, but SQLite still serializes writes. Editable records use version checks so stale changes are rejected instead of silently overwriting newer data. For multiple production workers or sustained simultaneous sales, use PostgreSQL and run the app behind Gunicorn; configure the database connection pool to fit the database's connection limit.

The application currently applies compatibility schema updates during startup. Before running multiple production workers against a newly upgraded database, start one instance to complete those updates first. Moving these updates to a dedicated Alembic migration command is recommended before automated multi-worker deployments.

### Render and Neon

- In Neon, create a production database and copy its pooled connection string. In Render, set it as `DATABASE_URL`; use the Neon-provided SSL connection string (`sslmode=require`). Switching from the local SQLite URL does not copy existing rows, so migrate/import the data separately before directing users to Neon.
- Set a stable `SECRET_KEY` and the Stripe/SMTP environment variables in Render's service settings. Never put secret values in the repository.
- Tune `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, and `DB_POOL_TIMEOUT` to the Neon plan's connection limit and the number of Render workers. Each worker owns its own SQLAlchemy pool; start conservatively, for example `DB_POOL_SIZE=3`, `DB_MAX_OVERFLOW=2`, `DB_POOL_TIMEOUT=30`.
- Deploy with one worker for the first startup after schema changes. Increase workers only after startup completes, and move the current automatic compatibility updates to Alembic migrations before routine multi-worker releases.

Example MySQL URL:
`mysql+pymysql://user:password@localhost/business_tracker`

For MySQL, install `PyMySQL` in addition to the requirements.
