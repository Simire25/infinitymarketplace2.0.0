"""Infinity MarketPlace - Flask backend.

Setup and run (from the project folder):
    pip install -r requirements.txt
    flask --app backend.app seed-demo          # optional sample products
    flask --app backend.app create-admin       # first administrator account
    flask --app backend.app run --debug        # then open http://127.0.0.1:5000
"""
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps

import click
from flask import Blueprint, Flask, abort, current_app, g, jsonify, redirect, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbm
from .payments import Paystack, PaystackError

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLES = ("customer", "staff", "admin")
ORDER_STATUSES = ("pending", "processing", "shipped", "out_for_delivery", "delivered", "cancelled")
DELIVERY_STATUSES = ("unassigned", "assigned", "picked_up", "out_for_delivery", "delivered", "failed")
# A delivery step moves the order forward, and an order step moves the delivery.
DELIVERY_TO_ORDER = {"picked_up": "shipped", "out_for_delivery": "out_for_delivery", "delivered": "delivered"}
ORDER_TO_DELIVERY = {"shipped": "picked_up", "out_for_delivery": "out_for_delivery", "delivered": "delivered"}
MAX_QTY_PER_LINE = 50


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- helpers

def json_body():
    if not request.is_json:
        raise ApiError("Send the request as JSON.", 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("The request body must be a JSON object.", 400)
    return data


def clean_str(data, key, required=False, max_len=255):
    value = data.get(key)
    value = "" if value is None else str(value).strip()
    label = key.replace("_", " ").capitalize()
    if required and not value:
        raise ApiError(f"{label} is required.")
    if len(value) > max_len:
        raise ApiError(f"{label} must be {max_len} characters or fewer.")
    return value


def to_int(value, label, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise ApiError(f"{label} must be a whole number.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ApiError(f"{label} must be a whole number.")
    if minimum is not None and number < minimum:
        raise ApiError(f"{label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ApiError(f"{label} must be at most {maximum}.")
    return number


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or secrets.token_hex(3)


def now_sql():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def new_order_reference():
    return f"IM{datetime.now(timezone.utc):%y%m%d}{secrets.token_hex(3).upper()}"


def paystack_client():
    return Paystack(current_app.config["PAYSTACK_SECRET_KEY"])


def public_base_url():
    # Set PUBLIC_BASE_URL in production (e.g. https://shop.example.com) so callbacks use the public address.
    return (current_app.config.get("PUBLIC_BASE_URL") or request.host_url).rstrip("/")


def get_settings():
    rows = dbm.q_all("SELECT key, value FROM settings")
    settings = dict(dbm.DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in rows if r["key"] in dbm.DEFAULT_SETTINGS})
    return settings


def enabled_payment_methods():
    methods = []
    if paystack_client().configured:
        methods.append("paystack")
    if get_settings()["cod_enabled"] == "1":
        methods.append("cash_on_delivery")
    return methods


# ---------------------------------------------------------------- auth

def current_user():
    if "user" not in g:
        uid = session.get("user_id")
        g.user = dbm.q_one(
            "SELECT id, name, email, phone, role FROM users WHERE id = ? AND is_active = 1", (uid,)
        ) if uid else None
    return g.user


def has_perm(user, permission):
    if user is None or user["role"] == "customer":
        return False
    if user["role"] == "admin":
        return True
    row = dbm.q_one(
        "SELECT allowed FROM role_permissions WHERE role = ? AND permission = ?",
        (user["role"], permission),
    )
    return bool(row and row["allowed"])


def login_required(permission=None, staff=False, admin=False):
    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if user is None:
                raise ApiError("Please log in to continue.", 401)
            if admin and user["role"] != "admin":
                raise ApiError("Only administrators can do this.", 403)
            if staff and user["role"] == "customer":
                raise ApiError("Staff access is required.", 403)
            if permission and not has_perm(user, permission):
                raise ApiError("You do not have permission to do this.", 403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------- order logic

def order_row_or_404(order_id):
    row = dbm.q_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not row:
        raise ApiError("Order not found.", 404)
    return row


def order_detail(order_id):
    order = dbm.q_one("SELECT * FROM orders WHERE id = ?", (order_id,))
    if not order:
        return None
    order["items"] = dbm.q_all(
        "SELECT product_id, product_name, unit_price, quantity, line_total FROM order_items WHERE order_id = ?",
        (order_id,),
    )
    order["delivery"] = dbm.q_one(
        "SELECT rider_name, rider_phone, status, notes, delivered_at FROM deliveries WHERE order_id = ?",
        (order_id,),
    )
    order["payments"] = dbm.q_all(
        "SELECT provider, reference, amount, status, created_at FROM payments WHERE order_id = ? ORDER BY id DESC",
        (order_id,),
    )
    return order


def restock_and_cancel(order, reason):
    """Return reserved stock to inventory and mark the order cancelled. Caller commits."""
    if order["status"] in ("delivered", "cancelled"):
        raise ApiError(f"This order is already {order['status']}.", 409)
    items = dbm.q_all("SELECT product_id, quantity FROM order_items WHERE order_id = ?", (order["id"],))
    for item in items:
        dbm.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (item["quantity"], item["product_id"]))
        dbm.execute(
            "INSERT INTO stock_movements (product_id, change, reason, reference) VALUES (?, ?, ?, ?)",
            (item["product_id"], item["quantity"], reason, order["reference"]),
        )
    dbm.execute(
        "UPDATE orders SET status = 'cancelled', updated_at = ? WHERE id = ?", (now_sql(), order["id"])
    )


def settle_payment(reference):
    """Verify a Paystack reference with Paystack and update payment + order.

    Returns the order row, or None if the reference is unknown.
    """
    pay = dbm.q_one("SELECT * FROM payments WHERE reference = ?", (reference,))
    if not pay:
        return None
    order = dbm.q_one("SELECT * FROM orders WHERE id = ?", (pay["order_id"],))
    if pay["status"] == "success":
        return order

    data = paystack_client().verify(reference)  # raises PaystackError if unreachable
    amount_ok = int(data.get("amount") or 0) == int(pay["amount"]) * 100
    currency_ok = data.get("currency") == "NGN"
    succeeded = data.get("status") == "success" and amount_ok and currency_ok
    new_status = "success" if succeeded else ("failed" if data.get("status") == "failed" else "pending")

    summary = {"status": data.get("status"), "amount_kobo": data.get("amount"),
               "currency": data.get("currency"), "channel": data.get("channel"),
               "paid_at": data.get("paid_at")}
    try:
        dbm.execute(
            "UPDATE payments SET status = ?, provider_response = ?, updated_at = ? WHERE id = ?",
            (new_status, json.dumps(summary), now_sql(), pay["id"]),
        )
        if succeeded:
            dbm.execute(
                """UPDATE orders SET payment_status = 'paid',
                   status = CASE WHEN status = 'pending' THEN 'processing' ELSE status END,
                   updated_at = ? WHERE id = ?""",
                (now_sql(), order["id"]),
            )
        dbm.commit()
    except sqlite3.Error:
        dbm.rollback()
        raise
    return dbm.q_one("SELECT * FROM orders WHERE id = ?", (order["id"],))


def start_paystack_payment(order, email, base_url):
    reference = f"{order['reference']}-{secrets.token_hex(3).upper()}"
    data = paystack_client().initialize(
        email=email,
        amount_naira=order["total"],
        reference=reference,
        callback_url=base_url + "/payment/callback",
        metadata={"order_reference": order["reference"]},
    )
    dbm.execute(
        """INSERT INTO payments (order_id, provider, reference, amount, currency, status)
           VALUES (?, 'paystack', ?, ?, 'NGN', 'pending')""",
        (order["id"], reference, order["total"]),
    )
    dbm.commit()
    return data["authorization_url"]


# ---------------------------------------------------------------- storefront blueprint

shop = Blueprint("shop", __name__, url_prefix="/api")


@shop.get("/config")
def public_config():
    settings = get_settings()
    return jsonify({
        "store_name": settings["store_name"],
        "whatsapp_number": settings["whatsapp_number"],
        "delivery_fee": int(settings["delivery_fee"]),
        "payment_methods": enabled_payment_methods(),
        "currency": "NGN",
    })


@shop.get("/categories")
def list_categories():
    rows = dbm.q_all(
        """SELECT c.id, c.name, c.slug, c.description, COUNT(p.id) AS product_count
           FROM categories c LEFT JOIN products p ON p.category_id = c.id AND p.is_active = 1
           GROUP BY c.id ORDER BY c.name"""
    )
    return jsonify(rows)


def public_products(args):
    where, params = ["p.is_active = 1"], []
    if args.get("category"):
        where.append("c.slug = ?")
        params.append(args["category"])
    term = (args.get("q") or "").strip()
    if term:
        where.append("(p.name LIKE ? OR p.description LIKE ? OR c.name LIKE ?)")
        params += [f"%{term}%"] * 3
    order = {"price-low": "p.price ASC", "price-high": "p.price DESC", "name": "p.name ASC"}.get(
        args.get("sort"), "p.id ASC")
    rows = dbm.q_all(
        f"""SELECT p.id, p.name, p.description, p.price, p.stock, p.image_url, p.tag,
                   c.name AS category, c.slug AS category_slug
            FROM products p JOIN categories c ON c.id = p.category_id
            WHERE {' AND '.join(where)} ORDER BY {order}""",
        params,
    )
    for row in rows:
        row["in_stock"] = row.pop("stock") > 0  # do not expose exact stock levels publicly
    return rows


@shop.get("/products")
def list_products():
    return jsonify(public_products(request.args))


@shop.get("/products/<int:product_id>")
def product_detail(product_id):
    row = dbm.q_one(
        """SELECT p.id, p.name, p.description, p.price, p.stock, p.image_url, p.tag,
                  c.name AS category, c.slug AS category_slug
           FROM products p JOIN categories c ON c.id = p.category_id
           WHERE p.id = ? AND p.is_active = 1""",
        (product_id,),
    )
    if not row:
        raise ApiError("Product not found.", 404)
    row["in_stock"] = row.pop("stock") > 0
    return jsonify(row)


@shop.post("/auth/register")
def register():
    data = json_body()
    name = clean_str(data, "name", True, 120)
    email = clean_str(data, "email", True, 180).lower()
    phone = clean_str(data, "phone", False, 30)
    password = data.get("password")
    if not isinstance(password, str):
        raise ApiError("Password is required.")
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    if len(password) < 8:
        raise ApiError("Password must be at least 8 characters.")
    if dbm.q_one("SELECT id FROM users WHERE email = ?", (email,)):
        raise ApiError("An account with this email already exists.", 409)
    try:
        cur = dbm.execute(
            "INSERT INTO users (name, email, phone, password_hash, role) VALUES (?, ?, ?, ?, 'customer')",
            (name, email, phone, generate_password_hash(password)),
        )
        dbm.commit()
    except sqlite3.IntegrityError:
        dbm.rollback()
        raise ApiError("An account with this email already exists.", 409)
    session.clear()
    session["user_id"] = cur.lastrowid
    return jsonify({"user": current_user()}), 201


@shop.post("/auth/login")
def login():
    data = json_body()
    email = clean_str(data, "email", True, 180).lower()
    password = data.get("password")
    if not isinstance(password, str):
        raise ApiError("Incorrect email or password.", 401)
    row = dbm.q_one("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,))
    if not row or not check_password_hash(row["password_hash"], password):
        raise ApiError("Incorrect email or password.", 401)
    session.clear()
    session["user_id"] = row["id"]
    g.pop("user", None)  # reload the user for this new session
    return jsonify({"user": current_user()})


@shop.post("/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@shop.get("/auth/me")
def me():
    return jsonify({"user": current_user()})


@shop.post("/checkout")
@login_required()
def checkout():
    user = current_user()
    data = json_body()
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise ApiError("Your cart is empty.")
    name = clean_str(data, "name", True, 120)
    phone = clean_str(data, "phone", True, 30)
    address = clean_str(data, "address", True, 400)
    notes = clean_str(data, "notes", False, 500)
    method = data.get("payment_method")
    if method not in enabled_payment_methods():
        raise ApiError("The selected payment method is not available.")

    # Merge duplicate lines and validate quantities. Prices come from the database, never the client.
    wanted = {}
    for line in items:
        if not isinstance(line, dict):
            raise ApiError("Invalid cart item.")
        pid = to_int(line.get("product_id"), "Product", 1)
        qty = to_int(line.get("quantity"), "Quantity", 1, MAX_QTY_PER_LINE)
        wanted[pid] = wanted.get(pid, 0) + qty
        if wanted[pid] > MAX_QTY_PER_LINE:
            raise ApiError(f"You can order at most {MAX_QTY_PER_LINE} of one item.")

    settings = get_settings()
    try:
        lines, subtotal = [], 0
        for pid, qty in wanted.items():
            product = dbm.q_one("SELECT id, name, price FROM products WHERE id = ? AND is_active = 1", (pid,))
            if not product:
                raise ApiError("One of the products in your cart is no longer available.", 409)
            # Conditional update: succeeds only if enough stock is still there (guards against overselling).
            cur = dbm.execute(
                "UPDATE products SET stock = stock - ?, updated_at = ? WHERE id = ? AND stock >= ?",
                (qty, now_sql(), pid, qty),
            )
            if cur.rowcount != 1:
                raise ApiError(f"Not enough stock for {product['name']}. Please update your cart.", 409)
            lines.append((product, qty))
            subtotal += product["price"] * qty

        fee = int(settings["delivery_fee"])
        total = subtotal + fee
        reference = new_order_reference()
        cur = dbm.execute(
            """INSERT INTO orders (reference, user_id, customer_name, customer_email, customer_phone,
               delivery_address, notes, subtotal, delivery_fee, total, payment_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (reference, user["id"], name, user["email"], phone, address, notes,
             subtotal, fee, total, method),
        )
        order_id = cur.lastrowid
        for product, qty in lines:
            dbm.execute(
                """INSERT INTO order_items (order_id, product_id, product_name, unit_price, quantity, line_total)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, product["id"], product["name"], product["price"], qty, product["price"] * qty),
            )
            dbm.execute(
                "INSERT INTO stock_movements (product_id, change, reason, reference) VALUES (?, ?, 'sale', ?)",
                (product["id"], -qty, reference),
            )
        dbm.execute("INSERT INTO deliveries (order_id) VALUES (?)", (order_id,))
        if method == "cash_on_delivery":
            dbm.execute("UPDATE orders SET status = 'processing' WHERE id = ?", (order_id,))
        dbm.commit()
    except ApiError:
        dbm.rollback()
        raise
    except sqlite3.Error:
        dbm.rollback()
        raise ApiError("We could not place your order. Please try again.", 500)

    authorization_url = None
    if method == "paystack":
        order = dbm.q_one("SELECT * FROM orders WHERE id = ?", (order_id,))
        try:
            authorization_url = start_paystack_payment(order, user["email"], public_base_url())
        except PaystackError as exc:
            # Put the stock back and cancel, so the customer can try again without a ghost order.
            restock_and_cancel(order, "payment could not start")
            dbm.commit()
            raise ApiError(str(exc), 502)

    return jsonify({
        "reference": reference,
        "total": total,
        "payment_method": method,
        "authorization_url": authorization_url,
    }), 201


@shop.get("/orders")
@login_required()
def my_orders():
    user = current_user()
    rows = dbm.q_all(
        """SELECT id, reference, status, payment_status, payment_method, total, created_at
           FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 100""",
        (user["id"],),
    )
    return jsonify(rows)


@shop.get("/orders/<reference>")
@login_required()
def my_order_detail(reference):
    user = current_user()
    order = dbm.q_one("SELECT id, user_id FROM orders WHERE reference = ?", (reference,))
    if not order or (order["user_id"] != user["id"] and not has_perm(user, "orders")):
        raise ApiError("Order not found.", 404)
    return jsonify(order_detail(order["id"]))


@shop.post("/orders/<reference>/pay")
@login_required()
def retry_payment(reference):
    user = current_user()
    order = dbm.q_one("SELECT * FROM orders WHERE reference = ? AND user_id = ?", (reference, user["id"]))
    if not order:
        raise ApiError("Order not found.", 404)
    if order["payment_method"] != "paystack" or order["payment_status"] == "paid" or order["status"] == "cancelled":
        raise ApiError("This order cannot be paid online.", 409)
    try:
        url = start_paystack_payment(order, user["email"], public_base_url())
    except PaystackError as exc:
        raise ApiError(str(exc), 502)
    return jsonify({"authorization_url": url})


@shop.post("/paystack/webhook")
def paystack_webhook():
    secret = current_app.config["PAYSTACK_SECRET_KEY"]
    raw = request.get_data()
    if not Paystack.signature_is_valid(secret, raw, request.headers.get("X-Paystack-Signature", "")):
        abort(400)
    event = request.get_json(silent=True) or {}
    if event.get("event") == "charge.success":
        reference = (event.get("data") or {}).get("reference", "")
        if reference:
            try:
                settle_payment(reference)
            except PaystackError:
                return "", 500  # ask Paystack to retry later
    return "", 200


# ---------------------------------------------------------------- admin blueprint

admin = Blueprint("admin", __name__, url_prefix="/api/admin")


@admin.get("/access")
@login_required(staff=True)
def admin_access():
    user = current_user()
    return jsonify({
        "user": user,
        "permissions": {perm: has_perm(user, perm) for perm in dbm.PERMISSIONS},
    })


@admin.get("/dashboard")
@login_required(staff=True)
def dashboard():
    today = dbm.q_one(
        """SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue FROM orders
           WHERE status != 'cancelled' AND date(created_at) = date('now')"""
    )
    return jsonify({
        "today": today,
        "open_orders": dbm.q_one(
            "SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending','processing','shipped','out_for_delivery')")["n"],
        "awaiting_payment": dbm.q_one(
            "SELECT COUNT(*) AS n FROM orders WHERE payment_status = 'unpaid' AND status != 'cancelled'")["n"],
        "low_stock": dbm.q_one(
            "SELECT COUNT(*) AS n FROM products WHERE is_active = 1 AND stock <= low_stock_threshold")["n"],
        "customers": dbm.q_one("SELECT COUNT(*) AS n FROM users WHERE role = 'customer'")["n"],
        "active_deliveries": dbm.q_one(
            "SELECT COUNT(*) AS n FROM deliveries WHERE status IN ('assigned','picked_up','out_for_delivery')")["n"],
        "recent_orders": dbm.q_all(
            """SELECT id, reference, customer_name, total, status, payment_status, created_at
               FROM orders ORDER BY id DESC LIMIT 6"""),
        "low_stock_items": dbm.q_all(
            """SELECT id, name, sku, stock, low_stock_threshold FROM products
               WHERE is_active = 1 AND stock <= low_stock_threshold ORDER BY stock ASC LIMIT 6"""),
    })


# Orders -------------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@admin.get("/orders")
@login_required("orders")
def admin_orders():
    """Order list for the Orders Desk. All filters are optional and combine with AND."""
    where, params = [], []
    args = request.args
    if args.get("status") in ORDER_STATUSES:
        where.append("o.status = ?")
        params.append(args["status"])
    if args.get("payment_status") in ("paid", "unpaid"):
        where.append("o.payment_status = ?")
        params.append(args["payment_status"])
    if args.get("payment_method") in ("paystack", "cash_on_delivery"):
        where.append("o.payment_method = ?")
        params.append(args["payment_method"])
    if DATE_RE.match(args.get("date_from") or ""):
        where.append("date(o.created_at) >= date(?)")
        params.append(args["date_from"])
    if DATE_RE.match(args.get("date_to") or ""):
        where.append("date(o.created_at) <= date(?)")
        params.append(args["date_to"])
    term = (args.get("q") or "").strip()
    if term:
        where.append("(o.reference LIKE ? OR o.customer_name LIKE ? OR o.customer_phone LIKE ?)")
        params += [f"%{term}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    limit = to_int(args.get("limit", 300), "Limit", 1, 500)
    return jsonify(dbm.q_all(
        f"""SELECT o.id, o.reference, o.customer_name, o.customer_phone, o.delivery_address, o.notes,
                   o.total, o.payment_method, o.payment_status, o.status, o.created_at, o.updated_at,
                   (SELECT COALESCE(SUM(quantity), 0) FROM order_items WHERE order_id = o.id) AS item_count,
                   (SELECT group_concat(quantity || ' x ' || product_name, ', ')
                      FROM order_items WHERE order_id = o.id) AS items_summary,
                   d.rider_name, d.rider_phone, d.status AS delivery_status
            FROM orders o LEFT JOIN deliveries d ON d.order_id = o.id
            {clause} ORDER BY o.id DESC LIMIT {limit}""",
        params,
    ))


@admin.get("/orders/summary")
@login_required("orders")
def admin_orders_summary():
    """Small, cheap payload the Orders Desk polls to notice new orders and refresh its counters."""
    counts = {s: 0 for s in ORDER_STATUSES}
    for row in dbm.q_all("SELECT status, COUNT(*) AS n FROM orders GROUP BY status"):
        counts[row["status"]] = row["n"]
    today = dbm.q_one(
        """SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue FROM orders
           WHERE status != 'cancelled' AND date(created_at) = date('now')"""
    )
    return jsonify({
        "counts": counts,
        "latest_id": dbm.q_one("SELECT COALESCE(MAX(id), 0) AS n FROM orders")["n"],
        "unpaid": dbm.q_one(
            "SELECT COUNT(*) AS n FROM orders WHERE payment_status = 'unpaid' AND status != 'cancelled'")["n"],
        "today": today,
    })


@admin.get("/orders/<int:order_id>")
@login_required("orders")
def admin_order_detail(order_id):
    order_row_or_404(order_id)
    return jsonify(order_detail(order_id))


@admin.patch("/orders/<int:order_id>")
@login_required("orders")
def admin_update_order(order_id):
    order = order_row_or_404(order_id)
    status = json_body().get("status")
    if status not in ORDER_STATUSES:
        raise ApiError("Choose a valid order status.")
    if status == "cancelled":
        restock_and_cancel(order, "order cancelled by staff")
    else:
        if order["status"] == "cancelled":
            raise ApiError("Cancelled orders cannot be reopened. Place a new order instead.", 409)
        dbm.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (status, now_sql(), order_id))
        if status in ORDER_TO_DELIVERY:
            dbm.execute(
                """UPDATE deliveries SET status = ?, updated_at = ?,
                   delivered_at = CASE WHEN ? = 'delivered' THEN ? ELSE delivered_at END WHERE order_id = ?""",
                (ORDER_TO_DELIVERY[status], now_sql(), status, now_sql(), order_id),
            )
    dbm.commit()
    return jsonify(order_detail(order_id))


@admin.post("/orders/<int:order_id>/mark-paid")
@login_required("payments")
def admin_mark_cash_paid(order_id):
    order = order_row_or_404(order_id)
    if order["payment_method"] != "cash_on_delivery":
        raise ApiError("Only cash on delivery orders are marked paid manually.", 409)
    if order["payment_status"] == "paid" or order["status"] == "cancelled":
        raise ApiError("This order cannot be marked as paid.", 409)
    dbm.execute(
        """INSERT INTO payments (order_id, provider, reference, amount, currency, status, provider_response)
           VALUES (?, 'cash', ?, ?, 'NGN', 'success', 'Collected by staff on delivery')""",
        (order_id, f"CASH-{order['reference']}", order["total"]),
    )
    dbm.execute("UPDATE orders SET payment_status = 'paid', updated_at = ? WHERE id = ?", (now_sql(), order_id))
    dbm.commit()
    return jsonify(order_detail(order_id))


# Products -----------------------------------------------------------

def product_fields(data, partial=False):
    out = {}
    if not partial or "name" in data:
        out["name"] = clean_str(data, "name", True, 160)
    if not partial or "sku" in data:
        out["sku"] = clean_str(data, "sku", True, 60).upper()
    if not partial or "category_id" in data:
        out["category_id"] = to_int(data.get("category_id"), "Category", 1)
    if "supplier_id" in data:
        raw = data.get("supplier_id")
        out["supplier_id"] = None if raw in (None, "", 0, "0") else to_int(raw, "Supplier", 1)
    if "description" in data:
        out["description"] = clean_str(data, "description", False, 2000)
    if not partial or "price" in data:
        out["price"] = to_int(data.get("price"), "Price", 1, 100_000_000)
    if "low_stock_threshold" in data:
        out["low_stock_threshold"] = to_int(data.get("low_stock_threshold"), "Low stock threshold", 0, 100000)
    if "image_url" in data:
        url = clean_str(data, "image_url", False, 500)
        if url and not url.startswith(("https://", "http://")):
            raise ApiError("Image link must start with https://")
        out["image_url"] = url
    if "tag" in data:
        out["tag"] = clean_str(data, "tag", False, 30)
    if "is_active" in data:
        out["is_active"] = 1 if data.get("is_active") in (True, 1, "1", "true") else 0
    return out


def insert_row(table, fields):
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return dbm.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(fields.values()))


def update_row(table, row_id, fields):
    if not fields:
        raise ApiError("Nothing to update.")
    sets = ", ".join(f"{col} = ?" for col in fields)
    dbm.execute(f"UPDATE {table} SET {sets} WHERE id = ?", list(fields.values()) + [row_id])


@admin.get("/products")
@login_required("products")
def admin_products():
    where, params = [], []
    term = (request.args.get("q") or "").strip()
    if term:
        where.append("(p.name LIKE ? OR p.sku LIKE ?)")
        params += [f"%{term}%"] * 2
    if request.args.get("category_id", "").isdigit():
        where.append("p.category_id = ?")
        params.append(int(request.args["category_id"]))
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return jsonify(dbm.q_all(
        f"""SELECT p.*, c.name AS category, s.name AS supplier
            FROM products p JOIN categories c ON c.id = p.category_id
            LEFT JOIN suppliers s ON s.id = p.supplier_id {clause} ORDER BY p.id DESC""",
        params,
    ))


@admin.post("/products")
@login_required("products")
def admin_create_product():
    data = json_body()
    fields = product_fields(data)
    fields["stock"] = to_int(data.get("stock", 0), "Opening stock", 0, 1_000_000)
    try:
        cur = insert_row("products", fields)
    except sqlite3.IntegrityError as exc:
        dbm.rollback()
        raise ApiError("SKU already exists or category/supplier is invalid.", 409) from exc
    if fields["stock"]:
        dbm.execute(
            "INSERT INTO stock_movements (product_id, change, reason) VALUES (?, ?, 'opening stock')",
            (cur.lastrowid, fields["stock"]),
        )
    dbm.commit()
    return jsonify({"id": cur.lastrowid}), 201


@admin.patch("/products/<int:product_id>")
@login_required("products")
def admin_update_product(product_id):
    data = json_body()
    if not dbm.q_one("SELECT id FROM products WHERE id = ?", (product_id,)):
        raise ApiError("Product not found.", 404)
    fields = product_fields(data, partial=True)
    fields["updated_at"] = now_sql()
    try:
        update_row("products", product_id, fields)
    except sqlite3.IntegrityError as exc:
        dbm.rollback()
        raise ApiError("SKU already exists or category/supplier is invalid.", 409) from exc
    dbm.commit()
    return jsonify({"ok": True})


@admin.delete("/products/<int:product_id>")
@login_required("products")
def admin_archive_product(product_id):
    # Products appear in past orders, so they are archived (hidden) rather than deleted.
    dbm.execute("UPDATE products SET is_active = 0, updated_at = ? WHERE id = ?", (now_sql(), product_id))
    dbm.commit()
    return jsonify({"ok": True})


# Categories ---------------------------------------------------------

@admin.get("/categories")
@login_required("categories")
def admin_categories():
    return jsonify(dbm.q_all(
        """SELECT c.*, COUNT(p.id) AS product_count FROM categories c
           LEFT JOIN products p ON p.category_id = c.id GROUP BY c.id ORDER BY c.name"""))


@admin.post("/categories")
@login_required("categories")
def admin_create_category():
    data = json_body()
    name = clean_str(data, "name", True, 80)
    try:
        cur = insert_row("categories", {
            "name": name,
            "slug": slugify(name),
            "description": clean_str(data, "description", False, 300),
        })
    except sqlite3.IntegrityError as exc:
        dbm.rollback()
        raise ApiError("A category with this name already exists.", 409) from exc
    dbm.commit()
    return jsonify({"id": cur.lastrowid}), 201


@admin.patch("/categories/<int:category_id>")
@login_required("categories")
def admin_update_category(category_id):
    data = json_body()
    fields = {}
    if "name" in data:
        fields["name"] = clean_str(data, "name", True, 80)
    if "description" in data:
        fields["description"] = clean_str(data, "description", False, 300)
    try:
        update_row("categories", category_id, fields)
    except sqlite3.IntegrityError as exc:
        dbm.rollback()
        raise ApiError("A category with this name already exists.", 409) from exc
    dbm.commit()
    return jsonify({"ok": True})


@admin.delete("/categories/<int:category_id>")
@login_required("categories")
def admin_delete_category(category_id):
    used = dbm.q_one("SELECT COUNT(*) AS n FROM products WHERE category_id = ?", (category_id,))["n"]
    if used:
        raise ApiError("Move or archive the products in this category before deleting it.", 409)
    dbm.execute("DELETE FROM categories WHERE id = ?", (category_id,))
    dbm.commit()
    return jsonify({"ok": True})


# Inventory ----------------------------------------------------------

@admin.get("/inventory")
@login_required("inventory")
def admin_inventory():
    return jsonify(dbm.q_all(
        """SELECT p.id, p.name, p.sku, p.stock, p.low_stock_threshold, p.is_active,
                  c.name AS category, (p.stock <= p.low_stock_threshold) AS low
           FROM products p JOIN categories c ON c.id = p.category_id ORDER BY low DESC, p.stock ASC"""))


@admin.post("/inventory/adjust")
@login_required("inventory")
def admin_adjust_stock():
    data = json_body()
    product_id = to_int(data.get("product_id"), "Product", 1)
    change = to_int(data.get("change"), "Change", -1_000_000, 1_000_000)
    reason = clean_str(data, "reason", True, 120)
    if change == 0:
        raise ApiError("The change must not be zero.")
    cur = dbm.execute(
        "UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ? AND stock + ? >= 0",
        (change, now_sql(), product_id, change),
    )
    if cur.rowcount != 1:
        dbm.rollback()
        raise ApiError("Stock cannot go below zero, or the product does not exist.", 409)
    dbm.execute(
        "INSERT INTO stock_movements (product_id, change, reason) VALUES (?, ?, ?)",
        (product_id, change, reason),
    )
    dbm.commit()
    return jsonify({"ok": True})


@admin.get("/inventory/movements")
@login_required("inventory")
def admin_stock_movements():
    params, clause = [], ""
    if request.args.get("product_id", "").isdigit():
        clause = "WHERE m.product_id = ?"
        params.append(int(request.args["product_id"]))
    return jsonify(dbm.q_all(
        f"""SELECT m.id, p.name AS product, m.change, m.reason, m.reference, m.created_at
            FROM stock_movements m JOIN products p ON p.id = m.product_id {clause}
            ORDER BY m.id DESC LIMIT 200""",
        params,
    ))


# Suppliers & procurement -------------------------------------------

@admin.get("/suppliers")
@login_required("suppliers")
def admin_suppliers():
    return jsonify(dbm.q_all("SELECT * FROM suppliers ORDER BY name"))


@admin.post("/suppliers")
@login_required("suppliers")
def admin_create_supplier():
    data = json_body()
    cur = insert_row("suppliers", {
        "name": clean_str(data, "name", True, 120),
        "email": clean_str(data, "email", False, 180),
        "phone": clean_str(data, "phone", False, 30),
        "address": clean_str(data, "address", False, 300),
    })
    dbm.commit()
    return jsonify({"id": cur.lastrowid}), 201


@admin.patch("/suppliers/<int:supplier_id>")
@login_required("suppliers")
def admin_update_supplier(supplier_id):
    data = json_body()
    fields = {}
    for key, limit in (("name", 120), ("email", 180), ("phone", 30), ("address", 300)):
        if key in data:
            fields[key] = clean_str(data, key, key == "name", limit)
    if "is_active" in data:
        fields["is_active"] = 1 if data.get("is_active") in (True, 1, "1", "true") else 0
    update_row("suppliers", supplier_id, fields)
    dbm.commit()
    return jsonify({"ok": True})


@admin.get("/purchase-orders")
@login_required("suppliers")
def admin_purchase_orders():
    return jsonify(dbm.q_all(
        """SELECT po.id, po.status, po.total_cost, po.created_at, po.received_at, s.name AS supplier,
                  (SELECT COUNT(*) FROM purchase_order_items i WHERE i.purchase_order_id = po.id) AS line_count
           FROM purchase_orders po JOIN suppliers s ON s.id = po.supplier_id ORDER BY po.id DESC"""))


@admin.post("/purchase-orders")
@login_required("suppliers")
def admin_create_purchase_order():
    data = json_body()
    supplier_id = to_int(data.get("supplier_id"), "Supplier", 1)
    if not dbm.q_one("SELECT id FROM suppliers WHERE id = ? AND is_active = 1", (supplier_id,)):
        raise ApiError("Choose an active supplier.")
    lines = data.get("items")
    if not isinstance(lines, list) or not lines:
        raise ApiError("Add at least one product to the purchase order.")
    total, clean_lines = 0, []
    for line in lines:
        if not isinstance(line, dict):
            raise ApiError("Invalid purchase line.")
        pid = to_int(line.get("product_id"), "Product", 1)
        qty = to_int(line.get("quantity"), "Quantity", 1, 1_000_000)
        cost = to_int(line.get("unit_cost"), "Unit cost", 0, 100_000_000)
        if not dbm.q_one("SELECT id FROM products WHERE id = ?", (pid,)):
            raise ApiError("One of the products does not exist.", 404)
        clean_lines.append((pid, qty, cost))
        total += qty * cost
    try:
        cur = dbm.execute(
            "INSERT INTO purchase_orders (supplier_id, total_cost) VALUES (?, ?)", (supplier_id, total))
        for pid, qty, cost in clean_lines:
            dbm.execute(
                "INSERT INTO purchase_order_items (purchase_order_id, product_id, quantity, unit_cost) VALUES (?, ?, ?, ?)",
                (cur.lastrowid, pid, qty, cost))
        dbm.commit()
    except sqlite3.Error:
        dbm.rollback()
        raise ApiError("Could not save the purchase order.", 500)
    return jsonify({"id": cur.lastrowid}), 201


@admin.get("/purchase-orders/<int:po_id>")
@login_required("suppliers")
def admin_purchase_order_detail(po_id):
    po = dbm.q_one(
        "SELECT po.*, s.name AS supplier FROM purchase_orders po JOIN suppliers s ON s.id = po.supplier_id WHERE po.id = ?",
        (po_id,))
    if not po:
        raise ApiError("Purchase order not found.", 404)
    po["items"] = dbm.q_all(
        """SELECT i.product_id, p.name AS product, i.quantity, i.unit_cost
           FROM purchase_order_items i JOIN products p ON p.id = i.product_id WHERE i.purchase_order_id = ?""",
        (po_id,))
    return jsonify(po)


@admin.post("/purchase-orders/<int:po_id>/receive")
@login_required("suppliers")
def admin_receive_purchase_order(po_id):
    po = dbm.q_one("SELECT * FROM purchase_orders WHERE id = ?", (po_id,))
    if not po:
        raise ApiError("Purchase order not found.", 404)
    if po["status"] != "ordered":
        raise ApiError("Only open purchase orders can be received.", 409)
    try:
        items = dbm.q_all("SELECT product_id, quantity FROM purchase_order_items WHERE purchase_order_id = ?", (po_id,))
        for item in items:
            dbm.execute("UPDATE products SET stock = stock + ?, updated_at = ? WHERE id = ?",
                        (item["quantity"], now_sql(), item["product_id"]))
            dbm.execute(
                "INSERT INTO stock_movements (product_id, change, reason, reference) VALUES (?, ?, 'purchase receipt', ?)",
                (item["product_id"], item["quantity"], f"PO-{po_id}"))
        dbm.execute("UPDATE purchase_orders SET status = 'received', received_at = ? WHERE id = ?", (now_sql(), po_id))
        dbm.commit()
    except sqlite3.Error:
        dbm.rollback()
        raise ApiError("Could not receive this purchase order.", 500)
    return jsonify({"ok": True})


# Customers ----------------------------------------------------------

@admin.get("/customers")
@login_required("customers")
def admin_customers():
    return jsonify(dbm.q_all(
        """SELECT u.id, u.name, u.email, u.phone, u.created_at, u.is_active,
                  COUNT(o.id) AS order_count,
                  COALESCE(SUM(CASE WHEN o.status != 'cancelled' THEN o.total END), 0) AS total_spent
           FROM users u LEFT JOIN orders o ON o.user_id = u.id
           WHERE u.role = 'customer' GROUP BY u.id ORDER BY u.id DESC LIMIT 500"""))


# Payments -----------------------------------------------------------

@admin.get("/payments")
@login_required("payments")
def admin_payments():
    params, clause = [], ""
    if request.args.get("status") in ("pending", "success", "failed"):
        clause = "WHERE p.status = ?"
        params.append(request.args["status"])
    return jsonify(dbm.q_all(
        f"""SELECT p.id, p.provider, p.reference, p.amount, p.currency, p.status, p.created_at,
                   o.reference AS order_reference, o.customer_name, o.payment_method, o.payment_status
            FROM payments p JOIN orders o ON o.id = p.order_id {clause}
            ORDER BY p.id DESC LIMIT 300""",
        params,
    ))


# Delivery -----------------------------------------------------------

@admin.get("/deliveries")
@login_required("delivery")
def admin_deliveries():
    params, clause = [], ""
    if request.args.get("status") in DELIVERY_STATUSES:
        clause = "WHERE d.status = ?"
        params.append(request.args["status"])
    return jsonify(dbm.q_all(
        f"""SELECT d.order_id, d.rider_name, d.rider_phone, d.status, d.notes, d.delivered_at,
                   o.reference, o.customer_name, o.customer_phone, o.delivery_address, o.status AS order_status,
                   o.payment_method, o.payment_status, o.total
            FROM deliveries d JOIN orders o ON o.id = d.order_id {clause}
            ORDER BY d.id DESC LIMIT 300""",
        params,
    ))


@admin.patch("/deliveries/<int:order_id>")
@login_required("delivery")
def admin_update_delivery(order_id):
    data = json_body()
    delivery = dbm.q_one("SELECT * FROM deliveries WHERE order_id = ?", (order_id,))
    if not delivery:
        raise ApiError("Delivery record not found.", 404)
    order = order_row_or_404(order_id)
    fields = {}
    if "rider_name" in data:
        fields["rider_name"] = clean_str(data, "rider_name", False, 120)
    if "rider_phone" in data:
        fields["rider_phone"] = clean_str(data, "rider_phone", False, 30)
    if "notes" in data:
        fields["notes"] = clean_str(data, "notes", False, 500)
    if "status" in data:
        if data["status"] not in DELIVERY_STATUSES:
            raise ApiError("Choose a valid delivery status.")
        if order["status"] == "cancelled":
            raise ApiError("This order was cancelled.", 409)
        fields["status"] = data["status"]
        if data["status"] == "delivered":
            fields["delivered_at"] = now_sql()
        if data["status"] in DELIVERY_TO_ORDER:
            dbm.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                        (DELIVERY_TO_ORDER[data["status"]], now_sql(), order_id))
    fields["updated_at"] = now_sql()
    update_row("deliveries", delivery["id"], fields)
    dbm.commit()
    return jsonify({"ok": True})


# Reports ------------------------------------------------------------

@admin.get("/reports")
@login_required("reports")
def admin_reports():
    days = to_int(request.args.get("days", 30), "Days", 1, 365)
    since = f"-{days} days"
    daily = dbm.q_all(
        """SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS orders, SUM(total) AS revenue
           FROM orders WHERE status != 'cancelled' AND created_at >= datetime('now', ?)
           GROUP BY day ORDER BY day""", (since,))
    top_products = dbm.q_all(
        """SELECT oi.product_name AS name, SUM(oi.quantity) AS units, SUM(oi.line_total) AS revenue
           FROM order_items oi JOIN orders o ON o.id = oi.order_id
           WHERE o.status != 'cancelled' AND o.created_at >= datetime('now', ?)
           GROUP BY oi.product_name ORDER BY revenue DESC LIMIT 8""", (since,))
    by_status = dbm.q_all("SELECT status, COUNT(*) AS count FROM orders GROUP BY status")
    by_method = dbm.q_all(
        """SELECT payment_method AS method, COUNT(*) AS count, COALESCE(SUM(total), 0) AS amount
           FROM orders WHERE status != 'cancelled' AND created_at >= datetime('now', ?)
           GROUP BY payment_method""", (since,))
    totals = dbm.q_one(
        """SELECT COUNT(*) AS orders, COALESCE(SUM(total), 0) AS revenue,
                  COALESCE(AVG(total), 0) AS average_order
           FROM orders WHERE status != 'cancelled' AND created_at >= datetime('now', ?)""", (since,))
    return jsonify({"days": days, "totals": totals, "daily": daily, "top_products": top_products,
                    "by_status": by_status, "by_method": by_method})


# Settings & permissions --------------------------------------------

@admin.get("/settings")
@login_required("settings")
def admin_get_settings():
    return jsonify(get_settings())


@admin.patch("/settings")
@login_required("settings")
def admin_update_settings():
    data = json_body()
    updates = {}
    if "store_name" in data:
        updates["store_name"] = clean_str(data, "store_name", True, 80)
    if "whatsapp_number" in data:
        digits = re.sub(r"[\s+()-]", "", str(data.get("whatsapp_number") or ""))
        if digits and not re.fullmatch(r"\d{8,15}", digits):
            raise ApiError("WhatsApp number must be digits with country code, e.g. 2348012345678.")
        updates["whatsapp_number"] = digits
    if "delivery_fee" in data:
        updates["delivery_fee"] = str(to_int(data.get("delivery_fee"), "Delivery fee", 0, 1_000_000))
    if "cod_enabled" in data:
        updates["cod_enabled"] = "1" if data.get("cod_enabled") in (True, 1, "1", "true") else "0"
    for key, value in updates.items():
        dbm.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value))
    dbm.commit()
    return jsonify(get_settings())


@admin.get("/permissions")
@login_required("settings")
def admin_get_permissions():
    rows = dbm.q_all("SELECT permission, allowed FROM role_permissions WHERE role = 'staff'")
    granted = {r["permission"]: bool(r["allowed"]) for r in rows}
    return jsonify({
        "permissions": [{"key": k, "label": v} for k, v in dbm.PERMISSIONS.items()],
        "staff": {k: granted.get(k, False) for k in dbm.PERMISSIONS},
    })


@admin.put("/permissions")
@login_required(admin=True)
def admin_set_permissions():
    # Only administrators may change what staff can do, otherwise staff could grant themselves more access.
    staff = json_body().get("staff") or {}
    for perm in dbm.PERMISSIONS:
        if perm in staff:
            dbm.execute(
                "INSERT INTO role_permissions (role, permission, allowed) VALUES ('staff', ?, ?) "
                "ON CONFLICT(role, permission) DO UPDATE SET allowed = excluded.allowed",
                (perm, 1 if staff[perm] in (True, 1, "1", "true") else 0))
    dbm.commit()
    return jsonify({"ok": True})


@admin.get("/users")
@login_required("settings")
def admin_users():
    return jsonify(dbm.q_all(
        "SELECT id, name, email, phone, role, is_active, created_at FROM users ORDER BY role, name LIMIT 500"))


@admin.patch("/users/<int:user_id>")
@login_required("settings", admin=True)
def admin_update_user(user_id):
    me_user = current_user()
    data = json_body()
    target = dbm.q_one("SELECT id, role FROM users WHERE id = ?", (user_id,))
    if not target:
        raise ApiError("User not found.", 404)
    fields = {}
    if "role" in data:
        if data["role"] not in ROLES:
            raise ApiError("Choose a valid role.")
        if target["id"] == me_user["id"] and data["role"] != "admin":
            raise ApiError("You cannot remove your own admin access.", 409)
        fields["role"] = data["role"]
    if "is_active" in data:
        active = 1 if data["is_active"] in (True, 1, "1", "true") else 0
        if target["id"] == me_user["id"] and not active:
            raise ApiError("You cannot deactivate your own account.", 409)
        fields["is_active"] = active
    update_row("users", user_id, fields)
    dbm.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- app factory

def create_app(test_config=None):
    app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "instance", "marketplace.db")),
        PAYSTACK_SECRET_KEY=os.environ.get("PAYSTACK_SECRET_KEY", ""),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", ""),  # Render sets RENDER_EXTERNAL_URL
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    app.teardown_appcontext(dbm.close_db)
    app.register_blueprint(shop)
    app.register_blueprint(admin)

    with app.app_context():
        dbm.init_db()

    @app.errorhandler(ApiError)
    def handle_api_error(exc):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        if request.path.startswith("/api/"):
            messages = {400: "Bad request.", 403: "Forbidden.", 404: "Not found.",
                        405: "Method not allowed.", 413: "Request is too large."}
            return jsonify({"error": messages.get(exc.code, exc.description)}), exc.code
        return exc.get_response()

    # Pages. Static files come from /frontend; the admin console lives at /admin/.
    @app.get("/")
    def home():
        return app.send_static_file("index.html")

    @app.get("/admin")
    @app.get("/admin/")
    def admin_console():
        return app.send_static_file("admin/index.html")

    @app.get("/admin/orders")
    def admin_orders_desk():
        return app.send_static_file("admin/orders.html")

    @app.get("/payment/callback")
    def payment_callback():
        reference = request.args.get("reference") or request.args.get("trxref") or ""
        try:
            order = settle_payment(reference) if reference else None
        except PaystackError:
            return redirect("/orders.html?payment=unverified")
        if not order:
            return redirect("/orders.html?payment=unknown")
        return redirect(f"/orders.html?ref={order['reference']}&payment=checked")

    @app.cli.command("init-db")
    def cli_init_db():
        """Create database tables."""
        dbm.init_db()
        click.echo("Database ready.")

    @app.cli.command("seed-demo")
    def cli_seed_demo():
        """Add sample categories, a supplier and products."""
        click.echo("Demo data added." if dbm.seed_demo() else "Data already exists; nothing added.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def cli_create_admin(email, name, password):
        """Create (or promote) an administrator account."""
        email = email.strip().lower()
        if not EMAIL_RE.match(email) or len(password) < 8:
            raise click.ClickException("Use a valid email and a password of at least 8 characters.")
        existing = dbm.q_one("SELECT id FROM users WHERE email = ?", (email,))
        if existing:
            dbm.execute("UPDATE users SET role = 'admin', is_active = 1 WHERE id = ?", (existing["id"],))
        else:
            dbm.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
                (name, email, generate_password_hash(password)))
        dbm.commit()
        click.echo(f"Administrator ready: {email}")

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
