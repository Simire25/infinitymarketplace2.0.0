"""Infinity MarketPlace - Flask backend.

Setup and run (from the project folder):
    pip install -r requirements.txt
    flask --app backend.app seed-demo          # optional sample products
    flask --app backend.app create-admin       # first administrator account
    flask --app backend.app run --debug        # then open http://127.0.0.1:5000
"""
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

import click
from flask import (Blueprint, Flask, abort, after_this_request, current_app, g, jsonify, redirect, request,
                   send_file, session)
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbm
from . import mailer, security
from .payments import Paystack, PaystackError

log = logging.getLogger("infinity")

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
    def __init__(self, message, status=400, retry_after=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


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


def client_ip():
    return request.remote_addr or "unknown"


def throttle(bucket, limit, window, ident=None, count=True):
    """Refuse the request with HTTP 429 once `limit` calls were made in `window` seconds.
    With count=False it only checks; call `limiter().hit(...)` yourself when the attempt fails."""
    if not current_app.config["RATELIMIT_ENABLED"]:
        return
    key = f"{bucket}:{ident or client_ip()}"
    lim = current_app.extensions["limiter"]
    wait = lim.hit(key, limit, window) if count else lim.peek(key, limit, window)
    if wait:
        raise ApiError(f"Too many attempts. Please try again in {math.ceil(wait / 60)} minute(s).", 429, retry_after=wait)


def note_failure(bucket, limit, window, ident):
    if current_app.config["RATELIMIT_ENABLED"]:
        current_app.extensions["limiter"].hit(f"{bucket}:{ident}", limit, window)


def password_fingerprint(password_hash):
    """Stored in the session. Changing a password changes this, which signs out every old session."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:20]


def log_in_user(row):
    session.clear()
    session["user_id"] = row["id"]
    session["pwv"] = password_fingerprint(row["password_hash"])
    session.permanent = True
    g.pop("user", None)


def sql_time(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def store_name():
    return get_settings()["store_name"]


def money_text(amount):
    return f"NGN {int(amount):,}"


def email_order_summary(order_id):
    order = order_detail(order_id)
    lines = "\n".join(f"  {i['quantity']} x {i['product_name']}  -  {money_text(i['line_total'])}" for i in order["items"])
    return order, (
        f"Order {order['reference']}\n{lines}\n"
        f"  Delivery  -  {money_text(order['delivery_fee'])}\n"
        f"  Total  -  {money_text(order['total'])}\n\n"
        f"Deliver to: {order['customer_name']}, {order['delivery_address']}\nPhone: {order['customer_phone']}\n"
    )


def send_order_emails(order_id, paid_online=False):
    """Receipt to the customer and an alert to the store. Never raises: email must not break an order."""
    try:
        cfg = current_app.config
        order, summary = email_order_summary(order_id)
        shop_name = store_name()
        if order["payment_method"] == "cash_on_delivery":
            how = f"You chose pay on delivery. Please have {money_text(order['total'])} ready when your order arrives."
        else:
            how = "We have received your online payment. Thank you!"
        mailer.send_mail(cfg, order["customer_email"], f"{shop_name}: we received your order {order['reference']}",
                         f"Hello {order['customer_name']},\n\nThank you for shopping with {shop_name}.\n{how}\n\n"
                         f"{summary}\nYou can follow your order any time under My orders on our website.\n")
        if cfg.get("STORE_ORDER_EMAIL"):
            mailer.send_mail(cfg, cfg["STORE_ORDER_EMAIL"], f"New order {order['reference']} ({money_text(order['total'])})",
                             f"A new order was placed on {shop_name}.\nPayment: {order['payment_method']} / {order['payment_status']}\n\n{summary}")
    except Exception:  # noqa: BLE001
        log.exception("Order email failed for order %s", order_id)


# A real hash of a random string, used so unknown-email logins take as long as real ones.
DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


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
        row = dbm.q_one(
            "SELECT id, name, email, phone, role, password_hash FROM users WHERE id = ? AND is_active = 1", (uid,)
        ) if uid else None
        if row and session.get("pwv") == password_fingerprint(row["password_hash"]):
            g.user = {k: row[k] for k in ("id", "name", "email", "phone", "role")}
        else:
            g.user = None
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
    if order["payment_status"] == "paid" and order["payment_method"] == "paystack":
        paid = dbm.q_one("SELECT id, amount FROM payments WHERE order_id = ? AND status = 'success' ORDER BY id LIMIT 1",
                         (order["id"],))
        record_issue(order["id"], paid["id"] if paid else None, "cancelled_after_paid",
                     paid["amount"] if paid else order["total"], "Order cancelled after the customer had paid online.")


def record_issue(order_id, payment_id, kind, amount, note=""):
    """Flag money that needs a human decision. Caller commits. Never records the same issue twice."""
    if dbm.q_one("SELECT id FROM payment_issues WHERE order_id = ? AND kind = ? AND COALESCE(payment_id, 0) = ?",
                 (order_id, kind, payment_id or 0)):
        return
    dbm.execute("INSERT INTO payment_issues (order_id, payment_id, kind, amount, note) VALUES (?, ?, ?, ?, ?)",
                (order_id, payment_id, kind, amount, note))
    log.warning("PAYMENT ISSUE %s order_id=%s payment_id=%s amount=%s %s", kind, order_id, payment_id, amount, note)


def settle_payment(reference):
    """Verify a Paystack reference with Paystack and update payment + order.

    Returns the order row, or None if the reference is unknown. Safe to call twice at once (the
    webhook and the customer's return can arrive together): only one of them completes the payment.
    Anything odd (paid after cancel, paid twice, wrong amount) is recorded as a payment issue
    instead of being guessed at.
    """
    pay = dbm.q_one("SELECT * FROM payments WHERE reference = ?", (reference,))
    if not pay:
        return None
    order = dbm.q_one("SELECT * FROM orders WHERE id = ?", (pay["order_id"],))
    if pay["status"] == "success":
        return order

    data = paystack_client().verify(reference)  # raises PaystackError if unreachable
    remote_status = data.get("status")
    amount_ok = int(data.get("amount") or 0) == int(pay["amount"]) * 100
    currency_ok = data.get("currency") == "NGN"
    succeeded = remote_status == "success" and amount_ok and currency_ok
    mismatch = remote_status == "success" and not (amount_ok and currency_ok)
    new_status = "success" if succeeded else ("failed" if remote_status == "failed" else "pending")

    summary = {"status": remote_status, "amount_kobo": data.get("amount"),
               "currency": data.get("currency"), "channel": data.get("channel"),
               "paid_at": data.get("paid_at")}
    just_paid = False
    try:
        if succeeded:
            # Claim the payment atomically. If another request already did, there is nothing left to do.
            claimed = dbm.execute(
                "UPDATE payments SET status = 'success', provider_response = ?, updated_at = ? "
                "WHERE id = ? AND status != 'success'", (json.dumps(summary), now_sql(), pay["id"]))
            if claimed.rowcount != 1:
                dbm.rollback()
                return dbm.q_one("SELECT * FROM orders WHERE id = ?", (order["id"],))
            fresh = dbm.q_one("SELECT * FROM orders WHERE id = ?", (order["id"],))
            if fresh["status"] == "cancelled":
                dbm.execute("UPDATE orders SET payment_status = 'paid', updated_at = ? WHERE id = ?", (now_sql(), order["id"]))
                record_issue(order["id"], pay["id"], "paid_after_cancel", pay["amount"],
                             "The customer paid after this order was cancelled. Refund them.")
            elif fresh["payment_status"] == "paid":
                record_issue(order["id"], pay["id"], "duplicate_payment", pay["amount"],
                             "This order was already paid. This second payment should be refunded.")
            else:
                dbm.execute(
                    """UPDATE orders SET payment_status = 'paid',
                       status = CASE WHEN status = 'pending' THEN 'processing' ELSE status END,
                       updated_at = ? WHERE id = ?""", (now_sql(), order["id"]))
                just_paid = True
        else:
            dbm.execute("UPDATE payments SET status = ?, provider_response = ?, updated_at = ? WHERE id = ?",
                        (new_status, json.dumps(summary), now_sql(), pay["id"]))
            if mismatch:
                record_issue(order["id"], pay["id"], "amount_mismatch", int(data.get("amount") or 0) // 100,
                             f"Paystack reports a different amount ({data.get('amount')} kobo, {data.get('currency')}) "
                             f"than the order total. Check it in your Paystack dashboard.")
        dbm.commit()
    except sqlite3.Error:
        dbm.rollback()
        raise
    if just_paid:
        send_order_emails(order["id"], paid_online=True)
    return dbm.q_one("SELECT * FROM orders WHERE id = ?", (order["id"],))


_expiry_lock = threading.Lock()
_last_expiry = 0.0


def expire_unpaid_orders(limit=25):
    """Cancel online orders nobody paid for, so their stock goes back on sale.

    Before cancelling, ask Paystack whether any attempt was actually paid; if Paystack cannot be reached
    the order is left alone and tried again later. Returns how many orders were cancelled.
    """
    cutoff = sql_time(datetime.now(timezone.utc) - timedelta(minutes=current_app.config["UNPAID_ORDER_MINUTES"]))
    orders = dbm.q_all(
        """SELECT * FROM orders WHERE status = 'pending' AND payment_method = 'paystack'
           AND payment_status = 'unpaid' AND created_at <= ? ORDER BY id LIMIT ?""", (cutoff, limit))
    cancelled = 0
    for order in orders:
        try:
            for pay in dbm.q_all("SELECT reference FROM payments WHERE order_id = ? AND status = 'pending'", (order["id"],)):
                settle_payment(pay["reference"])
        except PaystackError:
            continue
        fresh = dbm.q_one("SELECT * FROM orders WHERE id = ?", (order["id"],))
        if fresh["status"] != "pending" or fresh["payment_status"] == "paid":
            continue
        restock_and_cancel(fresh, "unpaid order expired")
        dbm.commit()
        cancelled += 1
        log.info("Expired unpaid order %s", fresh["reference"])
    return cancelled


def maybe_expire_unpaid(every_seconds=300):
    """Run the expiry at most every few minutes, triggered by normal traffic (checkout, the Orders Desk)."""
    global _last_expiry
    with _expiry_lock:
        if time.monotonic() - _last_expiry < every_seconds:
            return
        _last_expiry = time.monotonic()
    try:
        expire_unpaid_orders()
    except Exception:  # noqa: BLE001
        log.exception("Expiring unpaid orders failed")
        dbm.rollback()


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
    throttle("register", 10, 3600)
    data = json_body()
    name = clean_str(data, "name", True, 120)
    email = clean_str(data, "email", True, 180).lower()
    phone = clean_str(data, "phone", False, 30)
    password = data.get("password")
    if not isinstance(password, str):
        raise ApiError("Password is required.")
    if not EMAIL_RE.match(email):
        raise ApiError("Enter a valid email address.")
    problem = security.password_problem(password, email)
    if problem:
        raise ApiError(problem)
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
    log_in_user({"id": cur.lastrowid, "password_hash": dbm.q_one("SELECT password_hash FROM users WHERE id = ?", (cur.lastrowid,))["password_hash"]})
    return jsonify({"user": current_user()}), 201


@shop.post("/auth/login")
def login():
    data = json_body()
    email = clean_str(data, "email", True, 180).lower()
    password = data.get("password")
    # Only FAILED attempts count, so a stranger cannot lock a real customer out by typing their email.
    throttle("login-ip", 40, 900, count=False)
    throttle("login-email", 8, 900, ident=email, count=False)
    if not isinstance(password, str):
        raise ApiError("Incorrect email or password.", 401)
    row = dbm.q_one("SELECT * FROM users WHERE email = ? AND is_active = 1", (email,))
    # Check a dummy hash for unknown emails so the response time does not reveal which emails have accounts.
    good = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password) and row is not None
    if not good:
        note_failure("login-ip", 40, 900, client_ip())
        note_failure("login-email", 8, 900, email)
        raise ApiError("Incorrect email or password.", 401)
    log_in_user(row)
    return jsonify({"user": current_user()})


@shop.post("/auth/forgot")
def forgot_password():
    """Email a reset link. The answer is always the same, so nobody can use this to find out who has an account."""
    throttle("forgot-ip", 10, 3600)
    data = json_body()
    email = clean_str(data, "email", True, 180).lower()
    throttle("forgot-email", 3, 3600, ident=email)
    user = dbm.q_one("SELECT id, name, email FROM users WHERE email = ? AND is_active = 1", (email,))
    if user and mailer.is_configured(current_app.config):
        token = secrets.token_urlsafe(32)
        dbm.execute("INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                    (user["id"], hashlib.sha256(token.encode()).hexdigest(),
                     sql_time(datetime.now(timezone.utc) + timedelta(hours=1))))
        dbm.commit()
        link = f"{public_base_url()}/forgot.html?token={token}"
        mailer.send_mail(current_app.config, user["email"], f"{store_name()}: reset your password",
                         f"Hello {user['name']},\n\nUse this link to choose a new password (valid for 1 hour):\n{link}\n\n"
                         f"If you did not ask for this, ignore this email. Your password has not changed.\n")
    return jsonify({"ok": True, "email_enabled": mailer.is_configured(current_app.config)})


@shop.post("/auth/reset")
def reset_password():
    throttle("reset-ip", 15, 3600)
    data = json_body()
    token = data.get("token")
    password = data.get("password")
    if not isinstance(token, str) or not isinstance(password, str):
        raise ApiError("This reset link is not valid.", 400)
    row = dbm.q_one(
        """SELECT r.id, r.user_id, u.email FROM password_resets r JOIN users u ON u.id = r.user_id
           WHERE r.token_hash = ? AND r.used_at IS NULL AND r.expires_at > ? AND u.is_active = 1""",
        (hashlib.sha256(token.encode()).hexdigest(), now_sql()))
    if not row:
        raise ApiError("This reset link has expired or was already used. Ask for a new one.", 400)
    problem = security.password_problem(password, row["email"])
    if problem:
        raise ApiError(problem)
    dbm.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), row["user_id"]))
    dbm.execute("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL", (now_sql(), row["user_id"]))
    dbm.commit()
    session.clear()  # every old session of this account is now invalid anyway
    return jsonify({"ok": True})


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
    throttle("checkout", 15, 3600, ident=str(user["id"]))
    maybe_expire_unpaid()
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
    if method == "cash_on_delivery":
        send_order_emails(order_id)
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
        "refunds_due": dbm.q_one("SELECT COUNT(*) AS n FROM payment_issues WHERE resolved_at IS NULL")["n"],
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
    maybe_expire_unpaid()
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
        "refunds_due": dbm.q_one("SELECT COUNT(*) AS n FROM payment_issues WHERE resolved_at IS NULL")["n"],
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


@admin.post("/customers/<int:user_id>/reset-password")
@login_required("customers")
def admin_reset_customer_password(user_id):
    """For customers who cannot use the emailed reset link. Shows a temporary password once."""
    target = dbm.q_one("SELECT id, email, role FROM users WHERE id = ?", (user_id,))
    if not target or target["role"] != "customer":
        raise ApiError("Customer not found.", 404)
    temporary = secrets.token_urlsafe(9)
    dbm.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(temporary), user_id))
    dbm.commit()
    log.info("Password for customer %s was reset by %s", target["email"], current_user()["email"])
    return jsonify({"temporary_password": temporary})


# Payments -----------------------------------------------------------

ISSUE_LABELS = {
    "paid_after_cancel": "Paid after the order was cancelled",
    "duplicate_payment": "Paid twice",
    "amount_mismatch": "Amount does not match",
    "cancelled_after_paid": "Order cancelled after payment",
}


def can_see_payment_issues():
    user = current_user()
    return has_perm(user, "payments") or has_perm(user, "orders")


@admin.get("/payment-issues")
@login_required(staff=True)
def admin_payment_issues():
    if not can_see_payment_issues():
        raise ApiError("You do not have permission to do this.", 403)
    show = request.args.get("status", "open")
    clause = {"open": "WHERE i.resolved_at IS NULL", "resolved": "WHERE i.resolved_at IS NOT NULL"}.get(show, "")
    rows = dbm.q_all(
        f"""SELECT i.id, i.kind, i.amount, i.note, i.created_at, i.resolved_at, i.resolution_note,
                   o.id AS order_id, o.reference AS order_reference, o.customer_name, o.customer_phone,
                   o.customer_email, p.reference AS payment_reference
            FROM payment_issues i JOIN orders o ON o.id = i.order_id
            LEFT JOIN payments p ON p.id = i.payment_id
            {clause} ORDER BY i.id DESC LIMIT 200""")
    for row in rows:
        row["label"] = ISSUE_LABELS.get(row["kind"], row["kind"])
    return jsonify(rows)


@admin.post("/payment-issues/<int:issue_id>/resolve")
@login_required("payments")
def admin_resolve_payment_issue(issue_id):
    issue = dbm.q_one("SELECT id, resolved_at FROM payment_issues WHERE id = ?", (issue_id,))
    if not issue:
        raise ApiError("Issue not found.", 404)
    if issue["resolved_at"]:
        raise ApiError("This issue is already resolved.", 409)
    note = clean_str(json_body(), "note", False, 300)
    dbm.execute("UPDATE payment_issues SET resolved_at = ?, resolved_by = ?, resolution_note = ? WHERE id = ?",
                (now_sql(), current_user()["id"], note, issue_id))
    dbm.commit()
    return jsonify({"ok": True})


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


@admin.get("/backup")
@login_required(admin=True)
def admin_backup():
    """A consistent copy of the whole database (orders, customers, stock). Administrators only."""
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    target = sqlite3.connect(path)
    try:
        dbm.get_db().backup(target)
    finally:
        target.close()

    @after_this_request
    def cleanup(response):
        try:
            os.remove(path)
        except OSError:
            pass
        return response

    name = f"marketplace-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M}.db"
    log.info("Database backup downloaded by %s", current_user()["email"])
    return send_file(path, as_attachment=True, download_name=name, mimetype="application/octet-stream")


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

def env_flag(name, default="0"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def validate_production_config(config):
    """Refuse to start live with settings that would put customers or money at risk."""
    problems = []
    secret = config.get("SECRET_KEY") or ""
    if len(secret) < 32 or config.get("_SECRET_GENERATED"):
        problems.append("SECRET_KEY must be set to a long random value (at least 32 characters). "
                        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\"")
    if not str(config.get("PUBLIC_BASE_URL") or "").startswith("https://"):
        problems.append("PUBLIC_BASE_URL must be your public https address, for example https://shop.example.com "
                        "(Paystack sends customers back to it after paying).")
    if problems:
        raise RuntimeError("Not safe to start in production:\n - " + "\n - ".join(problems))
    if not config.get("PAYSTACK_SECRET_KEY"):
        log.warning("PAYSTACK_SECRET_KEY is not set: only cash on delivery will be offered.")
    elif config["PAYSTACK_SECRET_KEY"].startswith("sk_test_"):
        log.warning("PAYSTACK_SECRET_KEY is a TEST key: no real money will move. Use sk_live_... to take real payments.")


def create_app(test_config=None):
    app = Flask(__name__, static_folder=FRONTEND_DIR, static_url_path="")
    production = os.environ.get("APP_ENV", "development").strip().lower() == "production"
    app.config.update(
        PRODUCTION=production,
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "instance", "marketplace.db")),
        PAYSTACK_SECRET_KEY=os.environ.get("PAYSTACK_SECRET_KEY", ""),
        PUBLIC_BASE_URL=os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL", ""),  # Render sets RENDER_EXTERNAL_URL
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=production or env_flag("SESSION_COOKIE_SECURE"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=14),
        MAX_CONTENT_LENGTH=256 * 1024,
        RATELIMIT_ENABLED=True,
        UNPAID_ORDER_MINUTES=int(os.environ.get("UNPAID_ORDER_MINUTES") or 90),
        TRUSTED_PROXIES=int(os.environ.get("TRUSTED_PROXIES") or 1),
        # Optional email (see backend/mailer.py)
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT") or 587),
        SMTP_USER=os.environ.get("SMTP_USER", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_SSL=env_flag("SMTP_SSL"),
        MAIL_FROM=os.environ.get("MAIL_FROM", ""),
        STORE_ORDER_EMAIL=os.environ.get("STORE_ORDER_EMAIL", ""),
        MAIL_CAPTURE=None,
    )
    app.config["_SECRET_GENERATED"] = not os.environ.get("SECRET_KEY")
    if test_config:
        app.config.update(test_config)
        if "SECRET_KEY" in test_config:
            app.config["_SECRET_GENERATED"] = False
    if app.config["PRODUCTION"]:
        validate_production_config(app.config)
        app.config["SESSION_COOKIE_SECURE"] = True
        if not test_config:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if app.config["TRUSTED_PROXIES"] > 0:
            n = app.config["TRUSTED_PROXIES"]
            app.wsgi_app = ProxyFix(app.wsgi_app, x_for=n, x_proto=n, x_host=n)
    app.extensions["limiter"] = security.RateLimiter()
    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)

    app.teardown_appcontext(dbm.close_db)
    app.register_blueprint(shop)
    app.register_blueprint(admin)

    with app.app_context():
        dbm.init_db()

    @app.before_request
    def block_cross_site_writes():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/") \
                and request.path != "/api/paystack/webhook" and not security.origin_is_same_site():
            raise ApiError("This request was blocked because it came from another website.", 403)

    @app.after_request
    def add_security_headers(response):
        return security.apply_security_headers(response, secure=app.config["SESSION_COOKIE_SECURE"])

    @app.errorhandler(ApiError)
    def handle_api_error(exc):
        response = jsonify({"error": exc.message})
        response.status_code = exc.status
        if exc.retry_after:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.errorhandler(HTTPException)
    def handle_http_error(exc):
        if request.path.startswith("/api/"):
            messages = {400: "Bad request.", 403: "Forbidden.", 404: "Not found.",
                        405: "Method not allowed.", 413: "Request is too large."}
            return jsonify({"error": messages.get(exc.code, exc.description)}), exc.code
        if exc.code == 404:
            response = app.send_static_file("404.html")
            response.status_code = 404
            return response
        return exc.get_response()

    @app.errorhandler(Exception)
    def handle_unexpected(exc):
        # Log the details for the owner, but never show a stack trace to a customer.
        log.exception("Unhandled error on %s %s", request.method, request.path)
        dbm.rollback() if "db" in g else None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Something went wrong on our side. Please try again."}), 500
        return "Something went wrong. Please try again.", 500

    @app.get("/healthz")
    def healthz():
        dbm.q_one("SELECT 1 AS ok")
        return jsonify({"ok": True})

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

    @app.cli.command("expire-unpaid")
    def cli_expire_unpaid():
        """Cancel unpaid online orders older than UNPAID_ORDER_MINUTES and return their stock."""
        click.echo(f"Cancelled {expire_unpaid_orders(limit=500)} unpaid order(s).")

    @app.cli.command("backup-db")
    @click.argument("destination", type=click.Path(dir_okay=False))
    def cli_backup_db(destination):
        """Save a consistent copy of the database to DESTINATION (safe while the shop is running)."""
        target = sqlite3.connect(destination)
        try:
            dbm.get_db().backup(target)
        finally:
            target.close()
        click.echo(f"Backup saved to {destination}")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def cli_create_admin(email, name, password):
        """Create (or promote) an administrator account."""
        email = email.strip().lower()
        problem = security.password_problem(password, email)
        if not EMAIL_RE.match(email) or problem:
            raise click.ClickException(problem or "Use a valid email address.")
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
