# Infinity MarketPlace

A complete online marketplace: storefront, cart and checkout, customer accounts, Paystack payments, WhatsApp support, and an administrative dashboard for orders, inventory, products, suppliers, customers, payments, delivery, reports and settings.

The Flask backend is in `backend/`. The storefront and admin console are static pages in `frontend/`, served by the same app.

## 1. Run it locally

> **Important:** open the shop through the Flask address below (`http://127.0.0.1:5000`), not through VS Code's Live Server or by double-clicking the HTML files. Those only show the pages. They have no backend, so sign-in, the cart and checkout fail (you would see "Request failed (405)").

Requirements: Python 3.10 or newer.

```bash
cd Infinity-MarketPlace
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt

flask --app backend.app seed-demo           # optional: sample categories and products
flask --app backend.app create-admin        # your first administrator account
flask --app backend.app run --debug
```

Then open:

- Storefront: http://127.0.0.1:5000/
- Admin console: http://127.0.0.1:5000/admin/
- Orders Desk (live order board): http://127.0.0.1:5000/admin/orders

The database is created automatically at `instance/marketplace.db` on first run.

Run the automated tests (no internet or Paystack keys needed):

```bash
python -m unittest backend.test_api -v
```

## 2. Configuration

Set these as environment variables. Never put real keys in the frontend files.

| Variable | Purpose |
|---|---|
| `APP_ENV` | Set to `production` on the live site. It turns on the safety checks: the app **refuses to start** without a proper `SECRET_KEY` and an `https://` public address, and it forces secure cookies. Leave it unset on your own computer. |
| `SECRET_KEY` | Signs login sessions. **Required in production**, 32+ random characters. |
| `PUBLIC_BASE_URL` | The public address, e.g. `https://shop.example.com`. Used for Paystack and email links. On Render it defaults to the service address; set it if you use your own domain. |
| `PAYSTACK_SECRET_KEY` | `sk_test_...` while testing (no real money), `sk_live_...` to take real payments. Online payment is hidden until this is set. |
| `DATABASE_PATH` | SQLite file location. Put it on a persistent disk in production. Defaults to `instance/marketplace.db`. |
| `UNPAID_ORDER_MINUTES` | Unpaid online orders are cancelled and their stock released after this long. Default 90. |
| `TRUSTED_PROXIES` | How many proxies sit in front of the app (Render: 1, the default). Needed so rate limits see each visitor's real address. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_SSL`, `MAIL_FROM` | Optional email. See section 3b. |
| `STORE_ORDER_EMAIL` | Optional. Where new-order alert emails go. |

A ready-to-copy list is in `.env.example`. On your own Windows computer, set a value for the current terminal with `$env:SECRET_KEY = "..."` (PowerShell) before `flask run`.

Store name, WhatsApp number, delivery fee and pay-on-delivery on/off are set in the admin console under **Settings & Permissions**. They do not need a restart.

## 3. Paystack setup

1. Create a Paystack account and copy your secret key from the dashboard (**Settings → API Keys & Webhooks**).
2. Set `PAYSTACK_SECRET_KEY` and restart. Use the test key first.
3. In the Paystack dashboard, set the webhook URL to:
   `https://YOUR-DOMAIN/api/paystack/webhook`
4. Test a card payment using Paystack's test cards. Orders switch to *Paid* automatically.

How payment confirmation works: the customer is sent to Paystack's hosted checkout. When they return, and when Paystack sends the webhook, the server asks Paystack directly whether the transaction succeeded and whether the amount matches the order total. The order is marked paid only if both checks pass. The browser's report of success is never trusted, and webhook requests without a valid `x-paystack-signature` are rejected.

**Odd payments are never guessed at.** If a customer pays after an order was cancelled, pays twice, or Paystack reports a different amount, the money is **not** silently accepted: it appears as a red **Refunds due** alert on the Orders Desk and in Payments. Refund it in the Paystack dashboard (Transactions, find the reference, Refund), then click **Mark refunded** so it leaves the list. Unpaid online orders are cancelled automatically after `UNPAID_ORDER_MINUTES` (Paystack is asked one last time first, so a payment that was made is never cancelled). Cancelling an order that was already paid online also raises a refund alert.

Flutterwave is not included in this version. The payment code is isolated in `backend/payments.py`, so a Flutterwave adapter can be added alongside it.

## 3b. Email (optional but recommended)

With email switched on, customers get an order receipt, you get a new-order alert at `STORE_ORDER_EMAIL`, and customers can reset a forgotten password themselves. Any provider with SMTP works (Brevo, Zoho Mail, Mailgun, SendGrid, or Gmail with an app password). Set `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` (and `SMTP_PORT`, default 587; use `SMTP_SSL=1` with port 465). Use an address on your own domain for `MAIL_FROM`, or receipts may land in spam.

Without email the shop still works: the forgot-password page tells customers to contact you, and you reset their password from **Customers → Reset password** (it shows a temporary password once).

## 4. WhatsApp support

Enter your WhatsApp number in international format with no `+` (for example `2348012345678`) in **Settings & Permissions**. A "Chat with us" button then appears on every storefront page. Customers' order pages also have an "Ask about this order" button that opens WhatsApp with the order reference filled in.

This uses WhatsApp click-to-chat links, so messages go to the number's WhatsApp app. Full WhatsApp Business API automation (templates, automatic notifications, a shared inbox) is a separate integration and is not included.

## 5. Roles and permissions

| Role | Access |
|---|---|
| Customer | Shop, checkout, own orders. |
| Staff | Sections granted by an administrator. Defaults: orders, products, categories, inventory, customers, delivery. |
| Admin | Everything, including changing staff permissions and user roles. |

Only administrators can change staff permissions or promote users. Staff cannot grant themselves more access, and admins cannot remove their own admin role. These rules are enforced by the API, not just hidden in the interface.

To make a customer into staff: sign in as an admin, open **Settings & Permissions → Users and roles**, and change the role.

## 5b. Orders Desk

A separate page at `/admin/orders` (also linked as **Orders Desk** at the top of the admin sidebar) for staff who receive orders all day. It needs the *orders* permission.

- **Live board** with five columns: Awaiting payment, Ready to process, Shipped, Out for delivery, Delivered. It checks for new orders every 15 seconds.
- **New-order alerts:** a chime (switch it off with the Sound button), a toast, a green *NEW* tag on the card, and a count in the browser tab title. *Mark as seen* clears them.
- **One-click progress:** each card has a button for its next step. Marking a cash-on-delivery order delivered offers to record the cash as collected.
- **Filters and list view:** search by reference, name or phone; filter by payment status, method and date (Today, 7 or 30 days, all); switch to a table with a status filter to find cancelled orders.
- **Order details:** items, totals, call and WhatsApp buttons, rider assignment, mark cash collected, cancel (returns stock), and a printable packing slip.

## 5c. Light and dark mode, text size

Every page has a sun/moon button in the header. The choice is remembered on that device and shared between the storefront and admin. With no saved choice, the device's own light/dark setting is used. Colours live in `frontend/css/style.css` as tokens (the `:root` and `html[data-theme="dark"]` blocks), so changing the palette is a one-place edit. Base text is 17px; the size scale is the `--fs-*` variables in the same block.

## 6. Admin console sections

Dashboard Overview · Orders · Inventory & Stock · Products · Categories · Suppliers & Procurement · Customers · Payments & Transactions · Delivery Management · Reports & Analytics · Settings & Permissions

Key workflows:

- **Orders:** change status from the list, or open an order to see items, payments and delivery. Cancelling an order returns its items to stock. Cash on delivery orders can be marked as collected.
- **Inventory:** adjust stock with a recorded reason. Every change is logged, and items at or below their alert level are flagged.
- **Suppliers & Procurement:** create purchase orders. Marking one as received adds the quantities to stock.
- **Delivery:** assign a rider, track progress. Delivery updates move the order status automatically, and the reverse also applies.
- **Reports:** revenue, average order, online versus cash split, daily sales, top products.

## 7. Deploying on Render

The project includes `render.yaml`, which Render reads to create the service and its disk.

1. Push the project to a GitHub repository (the zip contents, with the folder as the repo root).
2. In Render, choose **New → Blueprint** and select the repository.
3. When prompted, enter `PAYSTACK_SECRET_KEY` (use `sk_test_...` first) and, optionally, the email settings. `SECRET_KEY` is generated automatically and `APP_ENV=production` is already set. If you use your own domain, also set `PUBLIC_BASE_URL` to it.
4. Click **Apply**. Render builds the app and starts it with gunicorn.
5. Create your admin account from the service's **Shell** tab:

   ```bash
   flask --app backend.app create-admin
   ```

   Shell access depends on your Render plan, so check Render's current documentation if the tab is missing.
6. In the Paystack dashboard, set the webhook URL to `https://YOUR-SERVICE.onrender.com/api/paystack/webhook`.

Notes for Render:

- **Data persistence:** the database is stored on a persistent disk at `/var/data`. Without a disk (for example on the free plan), all orders and products are lost on every restart or redeploy. The disk requires a paid plan.
- **Single instance:** SQLite works best with one instance, so `render.yaml` runs one gunicorn worker with several threads and does not scale horizontally. Moving to Render's managed PostgreSQL would be needed to run multiple instances.
- **Public URL:** Render provides `RENDER_EXTERNAL_URL` automatically, and the app uses it for Paystack callbacks. Render also provides HTTPS.
- **Backups:** the disk is one copy of your data. Download a backup regularly from **Settings & Permissions → Backup** (administrators only), and before any update.
- **Free-plan sleep:** free web services spin down when idle, which slows the first request. It is one reason to use a paid plan for a live store.

## 7b. Deploying on another host

1. Use a Linux server with HTTPS (for example through Nginx and Let's Encrypt).
2. Install the app, then run it with a production server:

   ```bash
   export APP_ENV=production
   export SECRET_KEY='replace-with-a-long-random-string-of-32-or-more-characters'
   export PAYSTACK_SECRET_KEY='sk_live_...'
   export PUBLIC_BASE_URL='https://shop.example.com'
   export DATABASE_PATH=/var/lib/marketplace/marketplace.db
   gunicorn --workers 1 --threads 8 --timeout 60 --bind 127.0.0.1:8000 "backend.app:create_app()"
   ```

3. Point Nginx at `127.0.0.1:8000` and pass the usual `X-Forwarded-For`, `X-Forwarded-Proto` and `Host` headers.
4. Keep the database file on persistent storage and back it up regularly (`flask --app backend.app backup-db /path/backup.db` is safe to run while the shop is live, for example from a nightly cron job).
5. Create the first admin with `flask --app backend.app create-admin` on the server.

## 8. Project structure

```
Infinity-MarketPlace/
├── backend/
│   ├── app.py          Flask app: storefront API, checkout, payments, admin API, CLI commands
│   ├── db.py           Schema, connection helpers, default settings and demo data
│   ├── payments.py     Paystack client and webhook signature check
│   └── test_api.py     Automated tests
├── frontend/
│   ├── index.html      Homepage: categories, product browsing, search, filters
│   ├── product.html    Product details
│   ├── cart.html       Shopping cart
│   ├── checkout.html   Delivery details and payment choice
│   ├── login.html      Sign in and registration
│   ├── orders.html     Order history and order status
│   ├── css/            style.css (theme tokens + homepage), pages.css (customer pages)
│   ├── js/app.js       Storefront logic: API client, cart, pages, WhatsApp button
│   ├── js/theme.js     Light/dark mode switch (loaded on every page)
│   ├── js/forgot.js    Forgot / reset password page
│   ├── policies.html   Terms, delivery & returns, privacy (edit before launch)
│   └── admin/          Admin console: index.html, admin.js, admin.css
│                       Orders Desk: orders.html, orders.js, orders.css
├── requirements.txt
└── README.md
```

## 9. Going live checklist

Work through this once, in order:

1. **Deploy** with `APP_ENV=production`, a generated `SECRET_KEY`, HTTPS and a persistent disk (section 7). The app will refuse to start if something unsafe is missing, and the message says what.
2. **Test with `sk_test_` first.** Place an online order with a Paystack test card, then a cash on delivery order, and receive and ship both on the Orders Desk.
3. **Switch to live:** put your `sk_live_...` key in, and set the webhook in Paystack to `https://YOUR-DOMAIN/api/paystack/webhook`. Place one small real order and refund it from Paystack to prove the whole loop.
4. **Create your admin account** (`flask --app backend.app create-admin`) with a strong password and remove any demo products (**Products**). Set the store name, WhatsApp number and delivery fee in **Settings**.
5. **Read and edit `frontend/policies.html`.** The delivery times, 48-hour damage window, 7-day return window and refund timing are sensible defaults, not your business's real policy and not legal advice. Change them to what you actually do, ideally after a lawyer has looked at them.
6. **Turn on email** (section 3b) so customers get receipts and can reset passwords.
7. **Make a first backup** and put a reminder in your calendar to take one every week.
8. **Check Refunds due** on the Orders Desk each day it shows red.

## 9b. What protects the shop

- Prices and stock are decided by the server only; the browser cannot change them.
- A payment is accepted only after Paystack confirms the exact amount; webhooks must carry a valid signature; two simultaneous confirmations cannot both complete one payment.
- Repeated wrong passwords are blocked for 15 minutes (per account and per address); sign-ups, checkouts and reset requests are rate limited. A stranger cannot lock a real customer out by guessing, because only failed attempts count.
- Passwords are stored hashed; weak ones are refused; changing a password signs out every old session.
- Browser protections are on: strict Content-Security-Policy, no framing, HTTPS-only cookies and HSTS in production, cross-site write requests refused, and no caching of order or admin pages.
- Errors show customers a plain message and are logged for you; no technical details leak.

## 9c. Notes and limitations

- **Database:** SQLite (with write-ahead logging) handles a small to medium shop well on one server. For heavy traffic or several servers, move to PostgreSQL; the database layer is isolated in `backend/db.py`.
- **Single worker:** rate limiting and the unpaid-order timer keep their state in memory, which is why the deployment runs one gunicorn worker with several threads. If you ever run more workers or servers, move that state to Redis first.
- **Refunds are still done by you** in the Paystack dashboard. The shop tells you exactly who to refund and how much, and tracks that you did it, but does not move money itself.
- **Payments:** Paystack only. Flutterwave is not included.
- **Delivery fee** is one flat fee for all addresses.
- **Cart:** stored in the browser. It is checked against live prices and stock at checkout.
- **Product photos** are linked from external URLs. If an image site removes a photo, the product shows without one. Upload storage is not included.
- **Not included:** discount codes, product reviews, customer-facing order cancellation, and two-factor login for staff.

## 10. Upgrading from the Episode 2 prototype

The prototype's demo product array in `js/app.js` has been replaced by the database. Products now come from `/api/products`, and categories from `/api/categories`. Run `seed-demo` to load the same eight sample products.
