# Infinity MarketPlace

A complete online marketplace: storefront, cart and checkout, customer accounts, Paystack payments, WhatsApp support, and an administrative dashboard for orders, inventory, products, suppliers, customers, payments, delivery, reports and settings.

The Flask backend is in `backend/`. The storefront and admin console are static pages in `frontend/`, served by the same app.

## 1. Run it locally

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
| `SECRET_KEY` | Signs login sessions. **Required in production**, use a long random value. |
| `PAYSTACK_SECRET_KEY` | Paystack secret key (`sk_test_...` while testing, `sk_live_...` when live). Online payment is hidden until this is set. |
| `PUBLIC_BASE_URL` | The public address of the site, e.g. `https://shop.example.com`. Used for Paystack callbacks. Not needed on Render (uses `RENDER_EXTERNAL_URL`). |
| `DATABASE_PATH` | Optional. SQLite file location. Defaults to `instance/marketplace.db`. |
| `SESSION_COOKIE_SECURE` | Set to `1` once the site runs on HTTPS. |

Store name, WhatsApp number, delivery fee and pay-on-delivery on/off are set in the admin console under **Settings & Permissions**. They do not need a restart.

## 3. Paystack setup

1. Create a Paystack account and copy your secret key from the dashboard (**Settings → API Keys & Webhooks**).
2. Set `PAYSTACK_SECRET_KEY` and restart. Use the test key first.
3. In the Paystack dashboard, set the webhook URL to:
   `https://YOUR-DOMAIN/api/paystack/webhook`
4. Test a card payment using Paystack's test cards. Orders switch to *Paid* automatically.

How payment confirmation works: the customer is sent to Paystack's hosted checkout. When they return, and when Paystack sends the webhook, the server asks Paystack directly whether the transaction succeeded and whether the amount matches the order total. The order is marked paid only if both checks pass. The browser's report of success is never trusted, and webhook requests without a valid `x-paystack-signature` are rejected.

Flutterwave is not included in this version. The payment code is isolated in `backend/payments.py`, so a Flutterwave adapter can be added alongside it.

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
3. When prompted, enter `PAYSTACK_SECRET_KEY` (use `sk_test_...` first). `SECRET_KEY` is generated automatically.
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
- **Public URL:** Render provides `RENDER_EXTERNAL_URL` automatically, and the app uses it for Paystack callbacks.
- **Free-plan sleep:** free web services spin down when idle, which slows the first request. It is one reason to use a paid plan for a live store.

## 7b. Deploying on another host

1. Use a Linux server with HTTPS (for example through Nginx and Let's Encrypt).
2. Install the app, then run it with a production server:

   ```bash
   export SECRET_KEY='replace-with-a-long-random-string'
   export PAYSTACK_SECRET_KEY='sk_live_...'
   export PUBLIC_BASE_URL='https://shop.example.com'   # optional on Render, which sets RENDER_EXTERNAL_URL
   export SESSION_COOKIE_SECURE=1
   gunicorn --workers 3 --bind 127.0.0.1:8000 "backend.app:create_app()"
   ```

3. Point Nginx at `127.0.0.1:8000`.
4. Keep the database file on persistent storage and back it up regularly.
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
│   └── admin/          Admin console: index.html, admin.js, admin.css
│                       Orders Desk: orders.html, orders.js, orders.css
├── requirements.txt
└── README.md
```

## 9. Notes and limitations

- **Database:** SQLite is used, so setup is one command. For heavy traffic or several servers, move to MySQL or PostgreSQL. The queries use standard SQL, and the database layer is isolated in `backend/db.py`.
- **Unpaid online orders hold stock.** If a customer starts a Paystack payment and never completes it, the order stays *Awaiting payment* and its stock stays reserved. Staff can cancel these from the Orders section. Automatic expiry is not included yet.
- **Refunds are manual.** Cancelling an order that has already been paid online does not refund it. Refund in the Paystack dashboard. The same applies if a payment arrives for an order that was cancelled in the meantime: the order stays cancelled but is marked as paid, so check the Payments section and refund it.
- **Login protection:** there is no rate limiting on login yet. Add it (for example with Flask-Limiter) before going live.
- **Email notifications:** not included. Order confirmations go through WhatsApp links and the order page.
- **Cart:** stored in the browser (local storage). It is checked against live prices and stock at checkout, so stale carts cannot change prices.
- **Product photos** are linked from external URLs. Upload storage is not included.

## 10. Upgrading from the Episode 2 prototype

The prototype's demo product array in `js/app.js` has been replaced by the database. Products now come from `/api/products`, and categories from `/api/categories`. Run `seed-demo` to load the same eight sample products.
