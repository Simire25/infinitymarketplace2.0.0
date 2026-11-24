"""API tests. Run from the project folder:  python -m unittest backend.test_api -v

Uses a temporary database. Paystack calls are mocked, so no network or keys are needed.
"""
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from unittest import mock

from backend import db as dbm
from backend.app import create_app
from backend.payments import Paystack

SECRET = "sk_test_unit"


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(self.tmp.name, "test.db"),
            "PAYSTACK_SECRET_KEY": SECRET,
            "SECRET_KEY": "test-secret",
        })
        with self.app.app_context():
            dbm.seed_demo()
        self.client = self.app.test_client()
        self.anon = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    # helpers -----------------------------------------------------------
    def db_one(self, sql, args=()):
        with self.app.app_context():
            return dbm.q_one(sql, args)

    def db_all(self, sql, args=()):
        with self.app.app_context():
            return dbm.q_all(sql, args)

    def make_user(self, client, email="ada@example.com", password="secret123", name="Ada"):
        r = client.post("/api/auth/register", json={"name": name, "email": email, "password": password, "phone": "08012345678"})
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["user"]

    def make_admin(self, email="boss@example.com", password="adminpass1"):
        runner = self.app.test_cli_runner()
        result = runner.invoke(args=["create-admin", "--email", email, "--name", "Boss",
                                     "--password", password])
        self.assertEqual(result.exit_code, 0, result.output)
        c = self.app.test_client()
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(r.status_code, 200)
        return c

    def product_id(self, name):
        return self.db_one("SELECT id FROM products WHERE name = ?", (name,))["id"]

    def stock(self, pid):
        return self.db_one("SELECT stock FROM products WHERE id = ?", (pid,))["stock"]

    def checkout_body(self, items, method="cash_on_delivery"):
        return {"items": items, "name": "Ada", "phone": "08012345678",
                "address": "12 Ring Road, Ibadan", "payment_method": method}

    # storefront ----------------------------------------------------------
    def test_config_lists_payment_methods(self):
        r = self.client.get("/api/config")
        body = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertIn("paystack", body["payment_methods"])
        self.assertIn("cash_on_delivery", body["payment_methods"])
        self.assertEqual(body["delivery_fee"], 1500)

    def test_products_filtering_and_sorting(self):
        all_products = self.client.get("/api/products").get_json()
        self.assertEqual(len(all_products), 8)
        self.assertNotIn("stock", all_products[0])          # stock levels are not public
        self.assertIn("in_stock", all_products[0])
        beauty = self.client.get("/api/products?category=beauty").get_json()
        self.assertTrue(all(p["category_slug"] == "beauty" for p in beauty))
        cheap = self.client.get("/api/products?sort=price-low").get_json()
        self.assertEqual(cheap[0]["price"], min(p["price"] for p in all_products))
        found = self.client.get("/api/products?q=lamp").get_json()
        self.assertEqual([p["name"] for p in found], ["Modern Table Lamp"])

    def test_product_detail_and_missing_product(self):
        pid = self.product_id("Daily Fragrance")
        self.assertEqual(self.client.get(f"/api/products/{pid}").get_json()["name"], "Daily Fragrance")
        self.assertEqual(self.client.get("/api/products/9999").status_code, 404)

    def test_archived_products_are_hidden(self):
        pid = self.product_id("Daily Fragrance")
        self.make_admin()
        admin = self.app.test_client()
        admin.post("/api/auth/login", json={"email": "boss@example.com", "password": "adminpass1"})
        self.assertEqual(admin.delete(f"/api/admin/products/{pid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/products/{pid}").status_code, 404)

    # auth ----------------------------------------------------------------
    def test_register_login_logout_flow(self):
        user = self.make_user(self.client)
        self.assertEqual(user["role"], "customer")
        self.assertEqual(self.client.get("/api/auth/me").get_json()["user"]["email"], "ada@example.com")
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])
        self.assertEqual(self.client.post("/api/auth/login", json={"email": "ada@example.com", "password": "wrong-pass"}).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={"email": "ada@example.com", "password": "secret123"}).status_code, 200)

    def test_duplicate_email_and_weak_password_rejected(self):
        self.make_user(self.client)
        dup = self.anon.post("/api/auth/register", json={"name": "X", "email": "ADA@example.com", "password": "longenough"})
        self.assertEqual(dup.status_code, 409)
        weak = self.anon.post("/api/auth/register", json={"name": "Y", "email": "y@example.com", "password": "short"})
        self.assertEqual(weak.status_code, 400)

    def test_password_is_never_returned(self):
        self.make_user(self.client)
        self.assertNotIn("password_hash", json.dumps(self.client.get("/api/auth/me").get_json()))

    # checkout ------------------------------------------------------------
    def test_checkout_requires_login(self):
        pid = self.product_id("Modern Table Lamp")
        r = self.anon.post("/api/checkout", json=self.checkout_body([{"product_id": pid, "quantity": 1}]))
        self.assertEqual(r.status_code, 401)

    def test_checkout_ignores_client_prices_and_reduces_stock(self):
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")  # 18500, stock 8
        body = self.checkout_body([{"product_id": pid, "quantity": 2}])
        body["items"][0]["price"] = 1  # tampering attempt, must be ignored
        r = self.client.post("/api/checkout", json=body)
        self.assertEqual(r.status_code, 201, r.get_json())
        self.assertEqual(r.get_json()["total"], 18500 * 2 + 1500)
        self.assertEqual(self.stock(pid), 6)
        order = self.db_one("SELECT * FROM orders WHERE reference = ?", (r.get_json()["reference"],))
        self.assertEqual(order["status"], "processing")  # cash on delivery is accepted straight away
        self.assertEqual(order["payment_status"], "unpaid")

    def test_insufficient_stock_is_atomic(self):
        self.make_user(self.client)
        lamp = self.product_id("Modern Table Lamp")          # stock 8
        watch = self.product_id("Smart Fitness Watch")       # stock 3
        before_lamp, before_watch = self.stock(lamp), self.stock(watch)
        body = self.checkout_body([{"product_id": lamp, "quantity": 2},
                                   {"product_id": watch, "quantity": 5}])
        r = self.client.post("/api/checkout", json=body)
        self.assertEqual(r.status_code, 409)
        self.assertIn("Not enough stock", r.get_json()["error"])
        self.assertEqual(self.stock(lamp), before_lamp)     # earlier line rolled back too
        self.assertEqual(self.stock(watch), before_watch)
        self.assertEqual(self.db_one("SELECT COUNT(*) AS n FROM orders")["n"], 0)

    def test_invalid_quantity_and_method_rejected(self):
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")
        self.assertEqual(self.client.post("/api/checkout", json=self.checkout_body(
            [{"product_id": pid, "quantity": 0}])).status_code, 400)
        self.assertEqual(self.client.post("/api/checkout", json=self.checkout_body(
            [{"product_id": pid, "quantity": 1}], method="bitcoin")).status_code, 400)

    def test_customer_sees_only_own_orders(self):
        self.make_user(self.client)
        pid = self.product_id("Daily Fragrance")
        ref = self.client.post("/api/checkout", json=self.checkout_body(
            [{"product_id": pid, "quantity": 1}])).get_json()["reference"]
        self.assertEqual(len(self.client.get("/api/orders").get_json()), 1)
        self.assertEqual(self.client.get(f"/api/orders/{ref}").get_json()["reference"], ref)
        other = self.app.test_client()
        self.make_user(other, email="other@example.com")
        self.assertEqual(other.get(f"/api/orders/{ref}").status_code, 404)

    # Paystack ------------------------------------------------------------
    def paystack_mocks(self, verify_status="success", amount_kobo=None):
        init = mock.patch.object(Paystack, "initialize", return_value={
            "authorization_url": "https://checkout.paystack.com/test-abc", "reference": "x"})
        def fake_verify(_self, reference):
            return {"status": verify_status, "currency": "NGN", "channel": "card",
                    "amount": amount_kobo, "reference": reference, "paid_at": "now"}
        verify = mock.patch.object(Paystack, "verify", new=fake_verify)
        return init, verify

    def test_paystack_checkout_and_verified_callback_marks_paid(self):
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")
        init, _ = self.paystack_mocks()
        with init as mocked_init:
            r = self.client.post("/api/checkout", json=self.checkout_body(
                [{"product_id": pid, "quantity": 1}], method="paystack"))
        self.assertEqual(r.status_code, 201, r.get_json())
        body = r.get_json()
        self.assertTrue(body["authorization_url"].startswith("https://checkout.paystack.com"))
        self.assertEqual(mocked_init.call_args.kwargs["amount_naira"], 18500 + 1500)
        ref = body["reference"]
        pay = self.db_one("SELECT reference, amount FROM payments WHERE order_id = (SELECT id FROM orders WHERE reference = ?)", (ref,))
        self.assertEqual(pay["amount"], 20000)

        # Callback: Paystack confirms the exact amount in kobo.
        _, verify = self.paystack_mocks(amount_kobo=20000 * 100)
        with verify:
            r = self.anon.get(f"/payment/callback?reference={pay['reference']}")
        self.assertEqual(r.status_code, 302)
        self.assertIn(f"ref={ref}", r.headers["Location"])
        order = self.db_one("SELECT payment_status, status FROM orders WHERE reference = ?", (ref,))
        self.assertEqual(order["payment_status"], "paid")
        self.assertEqual(order["status"], "processing")

    def test_callback_rejects_wrong_amount(self):
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")
        init, _ = self.paystack_mocks()
        with init:
            ref = self.client.post("/api/checkout", json=self.checkout_body(
                [{"product_id": pid, "quantity": 1}], method="paystack")).get_json()["reference"]
        pay = self.db_one("SELECT reference FROM payments WHERE order_id = (SELECT id FROM orders WHERE reference = ?)", (ref,))
        _, verify = self.paystack_mocks(amount_kobo=100)  # underpaid
        with verify:
            self.anon.get(f"/payment/callback?reference={pay['reference']}")
        self.assertEqual(self.db_one("SELECT payment_status FROM orders WHERE reference = ?", (ref,))["payment_status"], "unpaid")

    def test_failed_paystack_init_restocks_and_cancels(self):
        from backend.payments import PaystackError
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")
        before = self.stock(pid)
        with mock.patch.object(Paystack, "initialize", side_effect=PaystackError("down")):
            r = self.client.post("/api/checkout", json=self.checkout_body(
                [{"product_id": pid, "quantity": 2}], method="paystack"))
        self.assertEqual(r.status_code, 502)
        self.assertEqual(self.stock(pid), before)
        self.assertEqual(self.db_one("SELECT status FROM orders")["status"], "cancelled")

    def test_webhook_requires_valid_signature(self):
        raw = json.dumps({"event": "charge.success", "data": {"reference": "nope"}}).encode()
        r = self.anon.post("/api/paystack/webhook", data=raw, content_type="application/json",
                           headers={"X-Paystack-Signature": "bad"})
        self.assertEqual(r.status_code, 400)
        good = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
        r = self.anon.post("/api/paystack/webhook", data=raw, content_type="application/json",
                           headers={"X-Paystack-Signature": good})
        self.assertEqual(r.status_code, 200)  # valid signature, unknown reference is ignored

    def test_webhook_marks_order_paid(self):
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")
        init, _ = self.paystack_mocks()
        with init:
            ref = self.client.post("/api/checkout", json=self.checkout_body(
                [{"product_id": pid, "quantity": 1}], method="paystack")).get_json()["reference"]
        pay_ref = self.db_one("SELECT reference FROM payments WHERE order_id = (SELECT id FROM orders WHERE reference = ?)", (ref,))["reference"]
        raw = json.dumps({"event": "charge.success", "data": {"reference": pay_ref}}).encode()
        sig = hmac.new(SECRET.encode(), raw, hashlib.sha512).hexdigest()
        _, verify = self.paystack_mocks(amount_kobo=20000 * 100)
        with verify:
            r = self.anon.post("/api/paystack/webhook", data=raw, content_type="application/json",
                               headers={"X-Paystack-Signature": sig})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db_one("SELECT payment_status FROM orders WHERE reference = ?", (ref,))["payment_status"], "paid")

    def test_signature_helper(self):
        raw = b'{"a":1}'
        sig = hmac.new(b"k", raw, hashlib.sha512).hexdigest()
        self.assertTrue(Paystack.signature_is_valid("k", raw, sig))
        self.assertFalse(Paystack.signature_is_valid("k", raw + b" ", sig))
        self.assertFalse(Paystack.signature_is_valid("", raw, sig))

    # admin: access control ----------------------------------------------
    def test_customers_cannot_use_admin_api(self):
        self.make_user(self.client)
        self.assertEqual(self.client.get("/api/admin/orders").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/dashboard").status_code, 403)
        self.assertEqual(self.anon.get("/api/admin/orders").status_code, 401)

    def test_staff_permissions_follow_settings(self):
        admin = self.make_admin()
        self.make_user(self.anon, email="staffer@example.com")
        uid = self.db_one("SELECT id FROM users WHERE email = 'staffer@example.com'")["id"]
        admin.patch(f"/api/admin/users/{uid}", json={"role": "staff"})
        staff = self.app.test_client()
        staff.post("/api/auth/login", json={"email": "staffer@example.com", "password": "secret123"})
        self.assertEqual(staff.get("/api/admin/orders").status_code, 200)   # default: orders allowed
        self.assertEqual(staff.get("/api/admin/payments").status_code, 403)  # default: payments denied
        self.assertEqual(staff.get("/api/admin/dashboard").status_code, 200)

        # Staff cannot grant themselves more access.
        r = staff.put("/api/admin/permissions", json={"staff": {"payments": True}})
        self.assertEqual(r.status_code, 403)
        admin.put("/api/admin/permissions", json={"staff": {"payments": True}})
        self.assertEqual(staff.get("/api/admin/payments").status_code, 200)

    def test_admin_cannot_remove_own_admin_role(self):
        admin = self.make_admin()
        me_id = admin.get("/api/auth/me").get_json()["user"]["id"]
        r = admin.patch(f"/api/admin/users/{me_id}", json={"role": "customer"})
        self.assertEqual(r.status_code, 409)

    # admin: operations ----------------------------------------------------
    def test_order_lifecycle_and_cancel_restocks(self):
        self.make_user(self.client)
        admin = self.make_admin()
        pid = self.product_id("Modern Table Lamp")
        before = self.stock(pid)
        ref = self.client.post("/api/checkout", json=self.checkout_body(
            [{"product_id": pid, "quantity": 3}])).get_json()["reference"]
        oid = self.db_one("SELECT id FROM orders WHERE reference = ?", (ref,))["id"]
        self.assertEqual(admin.patch(f"/api/admin/orders/{oid}", json={"status": "shipped"}).status_code, 200)
        self.assertEqual(self.db_one("SELECT status FROM deliveries WHERE order_id = ?", (oid,))["status"], "picked_up")
        r = admin.patch(f"/api/admin/orders/{oid}", json={"status": "cancelled"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.stock(pid), before)
        self.assertEqual(admin.patch(f"/api/admin/orders/{oid}", json={"status": "processing"}).status_code, 409)

    def test_delivery_update_syncs_order_status(self):
        self.make_user(self.client)
        admin = self.make_admin()
        pid = self.product_id("Daily Fragrance")
        ref = self.client.post("/api/checkout", json=self.checkout_body(
            [{"product_id": pid, "quantity": 1}])).get_json()["reference"]
        oid = self.db_one("SELECT id FROM orders WHERE reference = ?", (ref,))["id"]
        r = admin.patch(f"/api/admin/deliveries/{oid}", json={
            "rider_name": "Tunde", "rider_phone": "08099999999", "status": "delivered"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.db_one("SELECT status FROM orders WHERE id = ?", (oid,))["status"], "delivered")
        self.assertIsNotNone(self.db_one("SELECT delivered_at FROM deliveries WHERE order_id = ?", (oid,))["delivered_at"])

    def test_cash_on_delivery_can_be_marked_paid(self):
        self.make_user(self.client)
        admin = self.make_admin()
        pid = self.product_id("Daily Fragrance")
        ref = self.client.post("/api/checkout", json=self.checkout_body(
            [{"product_id": pid, "quantity": 1}])).get_json()["reference"]
        oid = self.db_one("SELECT id FROM orders WHERE reference = ?", (ref,))["id"]
        r = admin.post(f"/api/admin/orders/{oid}/mark-paid")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["payment_status"], "paid")
        self.assertEqual(admin.post(f"/api/admin/orders/{oid}/mark-paid").status_code, 409)

    def test_inventory_adjust_blocks_negative_stock(self):
        admin = self.make_admin()
        pid = self.product_id("Smart Fitness Watch")  # stock 3
        self.assertEqual(admin.post("/api/admin/inventory/adjust", json={
            "product_id": pid, "change": -5, "reason": "damaged"}).status_code, 409)
        self.assertEqual(self.stock(pid), 3)
        self.assertEqual(admin.post("/api/admin/inventory/adjust", json={
            "product_id": pid, "change": 7, "reason": "recount"}).status_code, 200)
        self.assertEqual(self.stock(pid), 10)

    def test_purchase_order_receive_increases_stock(self):
        admin = self.make_admin()
        supplier = self.db_one("SELECT id FROM suppliers LIMIT 1")["id"]
        pid = self.product_id("Decorative Ceramic Vase")
        before = self.stock(pid)
        r = admin.post("/api/admin/purchase-orders", json={
            "supplier_id": supplier, "items": [{"product_id": pid, "quantity": 10, "unit_cost": 9000}]})
        self.assertEqual(r.status_code, 201, r.get_json())
        po_id = r.get_json()["id"]
        self.assertEqual(admin.get(f"/api/admin/purchase-orders/{po_id}").get_json()["total_cost"], 90000)
        self.assertEqual(admin.post(f"/api/admin/purchase-orders/{po_id}/receive").status_code, 200)
        self.assertEqual(self.stock(pid), before + 10)
        self.assertEqual(admin.post(f"/api/admin/purchase-orders/{po_id}/receive").status_code, 409)

    def test_category_delete_blocked_when_in_use(self):
        admin = self.make_admin()
        cat = self.db_one("SELECT id FROM categories WHERE slug = 'beauty'")["id"]
        self.assertEqual(admin.delete(f"/api/admin/categories/{cat}").status_code, 409)
        r = admin.post("/api/admin/categories", json={"name": "Kids & Toys"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.db_one("SELECT slug FROM categories WHERE name = 'Kids & Toys'")["slug"], "kids-toys")

    def test_product_create_and_validation(self):
        admin = self.make_admin()
        cat = self.db_one("SELECT id FROM categories LIMIT 1")["id"]
        ok = admin.post("/api/admin/products", json={
            "name": "Desk Organiser", "sku": "home-99", "category_id": cat, "price": 5000, "stock": 4})
        self.assertEqual(ok.status_code, 201, ok.get_json())
        self.assertEqual(self.db_one("SELECT stock FROM products WHERE sku = 'HOME-99'")["stock"], 4)
        dup = admin.post("/api/admin/products", json={
            "name": "Again", "sku": "HOME-99", "category_id": cat, "price": 5000})
        self.assertEqual(dup.status_code, 409)
        bad_img = admin.post("/api/admin/products", json={
            "name": "Bad", "sku": "BAD-1", "category_id": cat, "price": 5000, "image_url": "javascript:alert(1)"})
        self.assertEqual(bad_img.status_code, 400)

    def test_settings_validation(self):
        admin = self.make_admin()
        self.assertEqual(admin.patch("/api/admin/settings", json={"whatsapp_number": "abc"}).status_code, 400)
        r = admin.patch("/api/admin/settings", json={"whatsapp_number": "+234 801 234 5678", "delivery_fee": 2000})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["whatsapp_number"], "2348012345678")
        self.assertEqual(self.client.get("/api/config").get_json()["delivery_fee"], 2000)

    def test_reports_and_dashboard(self):
        self.make_user(self.client)
        admin = self.make_admin()
        pid = self.product_id("Modern Table Lamp")
        self.client.post("/api/checkout", json=self.checkout_body([{"product_id": pid, "quantity": 1}]))
        report = admin.get("/api/admin/reports?days=30").get_json()
        self.assertEqual(report["totals"]["orders"], 1)
        self.assertEqual(report["totals"]["revenue"], 20000)
        self.assertEqual(report["top_products"][0]["name"], "Modern Table Lamp")
        dash = admin.get("/api/admin/dashboard").get_json()
        self.assertEqual(dash["open_orders"], 1)
        self.assertEqual(dash["customers"], 1)
        self.assertEqual(admin.get("/api/admin/reports?days=0").status_code, 400)

    def test_unknown_api_route_returns_json(self):
        r = self.client.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.get_json())

    def test_frontend_pages_are_served(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/admin/orders").status_code, 200)
        self.assertEqual(self.client.get("/admin/orders.html").status_code, 200)
        self.assertEqual(self.client.get("/js/theme.js").status_code, 200)
        self.assertEqual(self.client.get("/cart.html").status_code, 200)

    # admin: Orders Desk ---------------------------------------------------
    def place(self, name, qty, method="cash_on_delivery"):
        pid = self.product_id(name)
        r = self.client.post("/api/checkout", json=self.checkout_body([{"product_id": pid, "quantity": qty}], method))
        self.assertEqual(r.status_code, 201, r.get_json())
        ref = r.get_json()["reference"]
        return self.db_one("SELECT id FROM orders WHERE reference = ?", (ref,))["id"]

    def test_orders_desk_filters_and_summary(self):
        self.make_user(self.client)
        admin = self.make_admin()
        cod = self.place("Modern Table Lamp", 2)
        self.place("Daily Fragrance", 1)
        self.assertEqual(admin.patch(f"/api/admin/orders/{cod}", json={"status": "shipped"}).status_code, 200)
        self.assertEqual(admin.post(f"/api/admin/orders/{cod}/mark-paid", json={}).status_code, 200)

        rows = admin.get("/api/admin/orders").get_json()
        self.assertEqual(len(rows), 2)
        first = next(o for o in rows if o["id"] == cod)
        self.assertEqual(first["item_count"], 2)
        self.assertIn("Modern Table Lamp", first["items_summary"])
        self.assertIn("delivery_status", first)

        self.assertEqual(len(admin.get("/api/admin/orders?payment_status=paid").get_json()), 1)
        self.assertEqual(len(admin.get("/api/admin/orders?payment_status=unpaid").get_json()), 1)
        self.assertEqual(len(admin.get("/api/admin/orders?payment_method=paystack").get_json()), 0)
        self.assertEqual(len(admin.get("/api/admin/orders?status=shipped").get_json()), 1)
        self.assertEqual(len(admin.get("/api/admin/orders?date_from=2999-01-01").get_json()), 0)
        self.assertEqual(len(admin.get("/api/admin/orders?date_from=2000-01-01&date_to=2999-01-01").get_json()), 2)
        # junk filter values are ignored rather than breaking the query
        self.assertEqual(len(admin.get("/api/admin/orders?date_from=x'; DROP TABLE orders;--&payment_status=bogus").get_json()), 2)
        self.assertEqual(admin.get("/api/admin/orders?limit=0").status_code, 400)

        summary = admin.get("/api/admin/orders/summary").get_json()
        self.assertEqual(summary["counts"]["shipped"], 1)
        self.assertEqual(summary["counts"]["processing"], 1)
        self.assertEqual(summary["latest_id"], max(o["id"] for o in rows))
        self.assertEqual(summary["unpaid"], 1)
        self.assertEqual(summary["today"]["orders"], 2)

    def test_orders_desk_requires_orders_permission(self):
        self.make_user(self.client)
        self.assertEqual(self.client.get("/api/admin/orders/summary").status_code, 403)
        self.assertEqual(self.anon.get("/api/admin/orders/summary").status_code, 401)


class ReadinessTests(unittest.TestCase):
    """Protections that matter once real customers and real money are involved."""

    db_one = MarketplaceTests.db_one
    db_all = MarketplaceTests.db_all
    make_user = MarketplaceTests.make_user
    make_admin = MarketplaceTests.make_admin
    product_id = MarketplaceTests.product_id
    stock = MarketplaceTests.stock
    checkout_body = MarketplaceTests.checkout_body
    paystack_mocks = MarketplaceTests.paystack_mocks

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mail = []
        self.app = create_app({
            "TESTING": True,
            "DATABASE": os.path.join(self.tmp.name, "test.db"),
            "PAYSTACK_SECRET_KEY": SECRET,
            "SECRET_KEY": "test-secret",
            "MAIL_CAPTURE": self.mail,
            "STORE_ORDER_EMAIL": "owner@shop.example",
            "UNPAID_ORDER_MINUTES": 0,
        })
        with self.app.app_context():
            dbm.seed_demo()
        self.client = self.app.test_client()
        self.anon = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def place_online(self, name="Modern Table Lamp", qty=1, client=None):
        pid = self.product_id(name)
        init, _ = self.paystack_mocks()
        with init:
            r = (client or self.client).post("/api/checkout", json=self.checkout_body(
                [{"product_id": pid, "quantity": qty}], method="paystack"))
        self.assertEqual(r.status_code, 201, r.get_json())
        ref = r.get_json()["reference"]
        order = self.db_one("SELECT id, total FROM orders WHERE reference = ?", (ref,))
        pay = self.db_one("SELECT reference FROM payments WHERE order_id = ?", (order["id"],))
        return ref, order, pay["reference"]

    def pay(self, payment_ref, kobo):
        _, verify = self.paystack_mocks(amount_kobo=kobo)
        with verify:
            return self.anon.get(f"/payment/callback?reference={payment_ref}")

    # money safety ------------------------------------------------------------
    def test_payment_after_cancel_is_flagged_for_refund(self):
        self.make_user(self.client)
        admin = self.make_admin()
        pid = self.product_id("Modern Table Lamp")
        before = self.stock(pid)
        ref, order, pay_ref = self.place_online()
        admin.patch(f"/api/admin/orders/{order['id']}", json={"status": "cancelled"})
        self.assertEqual(self.stock(pid), before)
        self.pay(pay_ref, order["total"] * 100)  # the customer pays on the old link anyway
        row = self.db_one("SELECT status, payment_status FROM orders WHERE id = ?", (order["id"],))
        self.assertEqual((row["status"], row["payment_status"]), ("cancelled", "paid"))
        self.assertEqual(self.stock(pid), before)  # stock was not taken again
        issues = admin.get("/api/admin/payment-issues").get_json()
        self.assertEqual([i["kind"] for i in issues], ["paid_after_cancel"])
        self.assertEqual(issues[0]["amount"], order["total"])
        self.assertEqual(admin.get("/api/admin/orders/summary").get_json()["refunds_due"], 1)
        # a second notice from Paystack must not create a second issue
        self.pay(pay_ref, order["total"] * 100)
        self.assertEqual(len(admin.get("/api/admin/payment-issues").get_json()), 1)
        self.assertEqual(admin.post(f"/api/admin/payment-issues/{issues[0]['id']}/resolve", json={"note": "Refunded in Paystack"}).status_code, 200)
        self.assertEqual(admin.get("/api/admin/orders/summary").get_json()["refunds_due"], 0)
        self.assertEqual(admin.post(f"/api/admin/payment-issues/{issues[0]['id']}/resolve", json={}).status_code, 409)

    def test_paying_twice_is_flagged(self):
        user = self.make_user(self.client)
        admin = self.make_admin()
        ref, order, first = self.place_online()
        init, _ = self.paystack_mocks()
        with init:
            self.assertEqual(self.client.post(f"/api/orders/{ref}/pay").status_code, 200)  # a second link
        second = self.db_one("SELECT reference FROM payments WHERE order_id = ? AND reference != ?", (order["id"], first))["reference"]
        self.pay(first, order["total"] * 100)
        self.pay(second, order["total"] * 100)
        issues = admin.get("/api/admin/payment-issues").get_json()
        self.assertEqual([i["kind"] for i in issues], ["duplicate_payment"])
        self.assertEqual(issues[0]["payment_reference"], second)

    def test_cancelling_a_paid_online_order_flags_refund(self):
        self.make_user(self.client)
        admin = self.make_admin()
        ref, order, pay_ref = self.place_online()
        self.pay(pay_ref, order["total"] * 100)
        admin.patch(f"/api/admin/orders/{order['id']}", json={"status": "cancelled"})
        kinds = [i["kind"] for i in admin.get("/api/admin/payment-issues").get_json()]
        self.assertEqual(kinds, ["cancelled_after_paid"])

    def test_wrong_amount_is_flagged_not_accepted(self):
        self.make_user(self.client)
        admin = self.make_admin()
        ref, order, pay_ref = self.place_online()
        self.pay(pay_ref, 100)
        self.assertEqual(self.db_one("SELECT payment_status FROM orders WHERE id = ?", (order["id"],))["payment_status"], "unpaid")
        self.assertEqual([i["kind"] for i in admin.get("/api/admin/payment-issues").get_json()], ["amount_mismatch"])

    def test_staff_without_payment_rights_cannot_resolve_refunds(self):
        self.make_user(self.client)
        admin = self.make_admin()
        ref, order, pay_ref = self.place_online()
        admin.patch(f"/api/admin/orders/{order['id']}", json={"status": "cancelled"})
        self.pay(pay_ref, order["total"] * 100)
        issue_id = admin.get("/api/admin/payment-issues").get_json()[0]["id"]
        staff_client = self.app.test_client()
        self.make_user(staff_client, email="staff@example.com", name="Sam")
        uid = self.db_one("SELECT id FROM users WHERE email = 'staff@example.com'")["id"]
        admin.patch(f"/api/admin/users/{uid}", json={"role": "staff"})
        staff_client.post("/api/auth/login", json={"email": "staff@example.com", "password": "secret123"})
        self.assertEqual(staff_client.get("/api/admin/payment-issues").status_code, 200)  # can see it (orders right)
        self.assertEqual(staff_client.post(f"/api/admin/payment-issues/{issue_id}/resolve", json={}).status_code, 403)

    def test_unpaid_online_orders_expire_and_return_stock(self):
        self.make_user(self.client)
        pid = self.product_id("Modern Table Lamp")
        before = self.stock(pid)
        ref_unpaid, o1, _ = self.place_online(qty=2)
        ref_paid, o2, paid_ref = self.place_online(qty=1)
        self.assertEqual(self.stock(pid), before - 3)
        # Paystack says the second order WAS paid (the customer's return page just never reached us).
        _, verify = self.paystack_mocks(amount_kobo=o2["total"] * 100)
        # verify() answers "success" for the paid one and "abandoned" for the unpaid one
        def fake_verify(_self, reference):
            if reference == paid_ref:
                return {"status": "success", "currency": "NGN", "amount": o2["total"] * 100}
            return {"status": "abandoned", "currency": "NGN", "amount": None}
        with mock.patch.object(Paystack, "verify", new=fake_verify):
            result = self.app.test_cli_runner().invoke(args=["expire-unpaid"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Cancelled 1", result.output)
        self.assertEqual(self.db_one("SELECT status FROM orders WHERE id = ?", (o1["id"],))["status"], "cancelled")
        paid = self.db_one("SELECT status, payment_status FROM orders WHERE id = ?", (o2["id"],))
        self.assertEqual((paid["status"], paid["payment_status"]), ("processing", "paid"))
        self.assertEqual(self.stock(pid), before - 1)

    def test_expiry_leaves_order_alone_if_paystack_is_unreachable(self):
        from backend.payments import PaystackError
        self.make_user(self.client)
        ref, order, _ = self.place_online()
        with mock.patch.object(Paystack, "verify", side_effect=PaystackError("down")):
            result = self.app.test_cli_runner().invoke(args=["expire-unpaid"])
        self.assertIn("Cancelled 0", result.output)
        self.assertEqual(self.db_one("SELECT status FROM orders WHERE id = ?", (order["id"],))["status"], "pending")

    # protection ----------------------------------------------------------------
    def test_login_is_rate_limited_after_repeated_failures(self):
        self.make_user(self.client, email="victim@example.com")
        for _ in range(8):
            r = self.anon.post("/api/auth/login", json={"email": "victim@example.com", "password": "wrong-guess"})
            self.assertEqual(r.status_code, 401)
        r = self.anon.post("/api/auth/login", json={"email": "victim@example.com", "password": "secret123"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        # A different account on the same network is not locked by this.
        self.make_user(self.app.test_client(), email="other@example.com")
        self.assertEqual(self.anon.post("/api/auth/login", json={"email": "other@example.com", "password": "secret123"}).status_code, 200)

    def test_successful_logins_never_lock_anyone_out(self):
        self.make_user(self.client, email="busy@example.com")
        for _ in range(15):
            self.assertEqual(self.anon.post("/api/auth/login", json={"email": "busy@example.com", "password": "secret123"}).status_code, 200)

    def test_registration_is_rate_limited_and_rejects_weak_passwords(self):
        r = self.anon.post("/api/auth/register", json={"name": "A", "email": "a@example.com", "password": "password123"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("easy to guess", r.get_json()["error"])
        r = self.anon.post("/api/auth/register", json={"name": "A", "email": "a@example.com", "password": "a@example.com"})
        self.assertEqual(r.status_code, 400)
        codes = []
        for i in range(12):
            codes.append(self.anon.post("/api/auth/register", json={"name": "N", "email": f"n{i}@example.com", "password": "Str0ng-pass!"}).status_code)
        # 10 sign-ups per hour per address; the two rejected attempts above already used up two of them
        self.assertEqual(codes.count(429), 4)

    def test_security_headers_and_no_store(self):
        r = self.anon.get("/")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.anon.get("/api/config").headers["Cache-Control"], "no-store")
        self.assertEqual(self.anon.get("/admin/").headers["Cache-Control"], "no-store")

    def test_cross_site_writes_are_blocked(self):
        body = {"email": "x@example.com", "password": "whatever1"}
        r = self.anon.post("/api/auth/login", json=body, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.anon.post("/api/auth/login", json=body, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 401)  # allowed through; wrong credentials

    def test_unknown_page_gets_friendly_404_and_health_check_works(self):
        r = self.anon.get("/no-such-page.html")
        self.assertEqual(r.status_code, 404)
        self.assertIn(b"could not find that page", r.data)
        self.assertEqual(self.anon.get("/healthz").get_json(), {"ok": True})

    def test_production_refuses_unsafe_settings(self):
        base = {"TESTING": True, "DATABASE": os.path.join(self.tmp.name, "p.db"), "PRODUCTION": True}
        with self.assertRaises(RuntimeError) as ctx:
            create_app(base)
        self.assertIn("SECRET_KEY", str(ctx.exception))
        with self.assertRaises(RuntimeError) as ctx:
            create_app({**base, "SECRET_KEY": "x" * 40, "PUBLIC_BASE_URL": "http://shop.example.com"})
        self.assertIn("PUBLIC_BASE_URL", str(ctx.exception))
        app = create_app({**base, "SECRET_KEY": "x" * 40, "PUBLIC_BASE_URL": "https://shop.example.com"})
        self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
        r = app.test_client().get("/", base_url="https://shop.example.com")
        self.assertIn("max-age=", r.headers["Strict-Transport-Security"])

    # accounts --------------------------------------------------------------------
    def test_password_reset_by_email(self):
        self.make_user(self.client, email="ada@example.com")
        r = self.anon.post("/api/auth/forgot", json={"email": "nobody@example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.mail, [])  # same answer, nothing sent, nothing revealed
        self.assertEqual(self.anon.post("/api/auth/forgot", json={"email": "ada@example.com"}).status_code, 200)
        self.assertEqual(len(self.mail), 1)
        token = self.mail[0]["body"].split("token=")[1].split()[0]
        self.assertEqual(self.anon.post("/api/auth/reset", json={"token": token, "password": "password123"}).status_code, 400)
        self.assertEqual(self.anon.post("/api/auth/reset", json={"token": "bogus", "password": "Br4nd-new-pass"}).status_code, 400)
        self.assertEqual(self.anon.post("/api/auth/reset", json={"token": token, "password": "Br4nd-new-pass"}).status_code, 200)
        self.assertEqual(self.anon.post("/api/auth/reset", json={"token": token, "password": "An0ther-pass!"}).status_code, 400)  # one use only
        # The session that existed before the reset no longer works; the new password does.
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])
        self.assertEqual(self.anon.post("/api/auth/login", json={"email": "ada@example.com", "password": "secret123"}).status_code, 401)
        self.assertEqual(self.anon.post("/api/auth/login", json={"email": "ada@example.com", "password": "Br4nd-new-pass"}).status_code, 200)

    def test_expired_reset_link_is_refused(self):
        self.make_user(self.client, email="ada@example.com")
        self.anon.post("/api/auth/forgot", json={"email": "ada@example.com"})
        token = self.mail[0]["body"].split("token=")[1].split()[0]
        with self.app.app_context():
            dbm.execute("UPDATE password_resets SET expires_at = '2000-01-01 00:00:00'")
            dbm.commit()
        self.assertEqual(self.anon.post("/api/auth/reset", json={"token": token, "password": "Br4nd-new-pass"}).status_code, 400)

    def test_admin_can_reset_a_customer_password(self):
        self.make_user(self.client, email="ada@example.com")
        admin = self.make_admin()
        uid = self.db_one("SELECT id FROM users WHERE email = 'ada@example.com'")["id"]
        temp = admin.post(f"/api/admin/customers/{uid}/reset-password").get_json()["temporary_password"]
        self.assertGreaterEqual(len(temp), 10)
        self.assertIsNone(self.client.get("/api/auth/me").get_json()["user"])
        self.assertEqual(self.anon.post("/api/auth/login", json={"email": "ada@example.com", "password": temp}).status_code, 200)
        # staff and admin accounts cannot be reset this way
        boss = self.db_one("SELECT id FROM users WHERE email = 'boss@example.com'")["id"]
        self.assertEqual(admin.post(f"/api/admin/customers/{boss}/reset-password").status_code, 404)
        self.assertEqual(self.client.post(f"/api/admin/customers/{uid}/reset-password").status_code, 401)

    def test_order_emails_go_to_customer_and_store(self):
        self.make_user(self.client, email="ada@example.com")
        pid = self.product_id("Modern Table Lamp")
        r = self.client.post("/api/checkout", json=self.checkout_body([{"product_id": pid, "quantity": 1}]))
        self.assertEqual(r.status_code, 201)
        ref = r.get_json()["reference"]
        self.assertEqual(sorted(m["to"] for m in self.mail), ["ada@example.com", "owner@shop.example"])
        customer = next(m for m in self.mail if m["to"] == "ada@example.com")
        self.assertIn(ref, customer["subject"])
        self.assertIn("Modern Table Lamp", customer["body"])
        self.assertIn("20,000", customer["body"])

    def test_online_order_emails_only_after_payment(self):
        self.make_user(self.client, email="ada@example.com")
        ref, order, pay_ref = self.place_online()
        self.assertEqual(self.mail, [])
        self.pay(pay_ref, order["total"] * 100)
        self.assertEqual(len(self.mail), 2)
        self.pay(pay_ref, order["total"] * 100)  # repeated callback: no repeated email
        self.assertEqual(len(self.mail), 2)

    def test_backup_is_admin_only_and_a_real_database(self):
        self.make_user(self.client)
        admin = self.make_admin()
        r = admin.get("/api/admin/backup")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data.startswith(b"SQLite format 3"))
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertEqual(self.client.get("/api/admin/backup").status_code, 403)
        self.assertEqual(self.anon.get("/api/admin/backup").status_code, 401)


if __name__ == "__main__":
    unittest.main()
