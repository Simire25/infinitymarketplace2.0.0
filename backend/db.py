"""Data layer for Infinity MarketPlace.

Uses Python's built-in sqlite3 module so the project runs with no extra
database server. Money is stored as whole naira (integers). All queries use
parameter placeholders; table and column names in the code are constants.
"""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  phone TEXT DEFAULT '',
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'customer' CHECK (role IN ('customer','staff','admin')),
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  slug TEXT NOT NULL UNIQUE,
  description TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  email TEXT DEFAULT '',
  phone TEXT DEFAULT '',
  address TEXT DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  sku TEXT NOT NULL UNIQUE,
  category_id INTEGER NOT NULL REFERENCES categories(id),
  supplier_id INTEGER REFERENCES suppliers(id),
  description TEXT DEFAULT '',
  price INTEGER NOT NULL CHECK (price >= 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
  low_stock_threshold INTEGER NOT NULL DEFAULT 5,
  image_url TEXT DEFAULT '',
  tag TEXT DEFAULT '',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS stock_movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  product_id INTEGER NOT NULL REFERENCES products(id),
  change INTEGER NOT NULL,
  reason TEXT NOT NULL,
  reference TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS purchase_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  status TEXT NOT NULL DEFAULT 'ordered' CHECK (status IN ('ordered','received','cancelled')),
  total_cost INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  received_at TEXT
);
CREATE TABLE IF NOT EXISTS purchase_order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  purchase_order_id INTEGER NOT NULL REFERENCES purchase_orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  unit_cost INTEGER NOT NULL CHECK (unit_cost >= 0)
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reference TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id),
  customer_name TEXT NOT NULL,
  customer_email TEXT NOT NULL,
  customer_phone TEXT NOT NULL,
  delivery_address TEXT NOT NULL,
  notes TEXT DEFAULT '',
  subtotal INTEGER NOT NULL,
  delivery_fee INTEGER NOT NULL,
  total INTEGER NOT NULL,
  payment_method TEXT NOT NULL CHECK (payment_method IN ('paystack','cash_on_delivery')),
  payment_status TEXT NOT NULL DEFAULT 'unpaid' CHECK (payment_status IN ('unpaid','paid')),
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
         ('pending','processing','shipped','out_for_delivery','delivered','cancelled')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  product_name TEXT NOT NULL,
  unit_price INTEGER NOT NULL,
  quantity INTEGER NOT NULL,
  line_total INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  provider TEXT NOT NULL,
  reference TEXT NOT NULL UNIQUE,
  amount INTEGER NOT NULL,
  currency TEXT NOT NULL DEFAULT 'NGN',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','success','failed')),
  provider_response TEXT DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
  rider_name TEXT DEFAULT '',
  rider_phone TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'unassigned' CHECK (status IN
         ('unassigned','assigned','picked_up','out_for_delivery','delivered','failed')),
  notes TEXT DEFAULT '',
  delivered_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS role_permissions (
  role TEXT NOT NULL,
  permission TEXT NOT NULL,
  allowed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (role, permission)
);
"""

# Admins always have every permission. These are the switches an admin can
# change for the "staff" role in Settings & Permissions.
PERMISSIONS = {
    "orders": "Orders",
    "products": "Products",
    "categories": "Categories",
    "inventory": "Inventory & stock",
    "suppliers": "Suppliers & procurement",
    "customers": "Customers",
    "payments": "Payments & transactions",
    "delivery": "Delivery management",
    "reports": "Reports & analytics",
    "settings": "Settings & permissions",
}
DEFAULT_STAFF_PERMISSIONS = {"orders", "products", "categories", "inventory", "customers", "delivery"}

DEFAULT_SETTINGS = {
    "store_name": "Infinity MarketPlace",
    "whatsapp_number": "",   # international format without +, e.g. 2348012345678
    "delivery_fee": "1500",  # naira
    "cod_enabled": "1",      # pay on delivery on/off
}


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q_all(sql, args=()):
    return [dict(row) for row in get_db().execute(sql, args).fetchall()]


def q_one(sql, args=()):
    row = get_db().execute(sql, args).fetchone()
    return dict(row) if row else None


def execute(sql, args=()):
    return get_db().execute(sql, args)


def commit():
    get_db().commit()


def rollback():
    get_db().rollback()


def init_db():
    """Create tables and default settings/permissions. Safe to run repeatedly."""
    conn = get_db()
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    for perm in PERMISSIONS:
        conn.execute(
            "INSERT OR IGNORE INTO role_permissions (role, permission, allowed) VALUES ('staff', ?, ?)",
            (perm, 1 if perm in DEFAULT_STAFF_PERMISSIONS else 0),
        )
    conn.commit()


DEMO_CATEGORIES = [
    ("Electronics", "electronics", "Gadgets and devices"),
    ("Fashion", "fashion", "Style for everyday and special occasions"),
    ("Home & Living", "home-living", "Essentials for a comfortable home"),
    ("Beauty", "beauty", "Everyday care and fragrance"),
]

DEMO_PRODUCTS = [
    ("Wireless Bluetooth Headphones", "Electronics", 28500, "Comfortable everyday audio with a clean, modern design.",
     "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=700&q=80", "Popular", 20),
    ("Classic Minimal Wristwatch", "Fashion", 42000, "A versatile timepiece for casual and smart outfits.",
     "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=700&q=80", "Featured", 12),
    ("Modern Table Lamp", "Home & Living", 18500, "A simple accent light for a comfortable living space.",
     "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=700&q=80", "New", 8),
    ("Everyday Skincare Set", "Beauty", 22500, "A practical set for a simple daily skincare routine.",
     "https://images.unsplash.com/photo-1608248543803-ba4f8c70ae0b?auto=format&fit=crop&w=700&q=80", "Featured", 15),
    ("Smart Fitness Watch", "Electronics", 36500, "Keep useful activity and notification features close by.",
     "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=700&q=80", "Popular", 3),
    ("Everyday Casual Sneakers", "Fashion", 31500, "An easy-to-style pair for everyday wear.",
     "https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=700&q=80", "New", 10),
    ("Decorative Ceramic Vase", "Home & Living", 14500, "A subtle decorative piece for shelves and tables.",
     "https://images.unsplash.com/photo-1578500494198-246f612d3b3d?auto=format&fit=crop&w=700&q=80", "Featured", 6),
    ("Daily Fragrance", "Beauty", 27000, "A fragrance option for everyday use and special moments.",
     "https://images.unsplash.com/photo-1594035910387-fea47794261f?auto=format&fit=crop&w=700&q=80", "Popular", 9),
]


def seed_demo():
    """Insert sample categories, a supplier and products. Skips if data exists."""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
        return False
    for name, slug, desc in DEMO_CATEGORIES:
        conn.execute("INSERT INTO categories (name, slug, description) VALUES (?, ?, ?)", (name, slug, desc))
    cat_ids = {row[0]: row[1] for row in conn.execute("SELECT name, id FROM categories")}
    cur = conn.execute(
        "INSERT INTO suppliers (name, email, phone, address) VALUES (?, ?, ?, ?)",
        ("Demo Wholesale Ltd", "sales@example.com", "08000000000", "Lagos, Nigeria"),
    )
    supplier_id = cur.lastrowid
    for i, (name, cat, price, desc, image, tag, stock) in enumerate(DEMO_PRODUCTS, start=1):
        cur = conn.execute(
            """INSERT INTO products (name, sku, category_id, supplier_id, description, price, stock,
               low_stock_threshold, image_url, tag) VALUES (?, ?, ?, ?, ?, ?, ?, 5, ?, ?)""",
            (name, f"IM-{i:03d}", cat_ids[cat], supplier_id, desc, price, stock, image, tag),
        )
        conn.execute(
            "INSERT INTO stock_movements (product_id, change, reason) VALUES (?, ?, 'opening stock')",
            (cur.lastrowid, stock),
        )
    conn.commit()
    return True
