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


if __name__ == "__main__":
    unittest.main()
