"""
End-to-end tests: all apps working as one system.

    python manage.py test --settings=config.test_settings
"""
from rest_framework.test import APITestCase


class StoreClientMixin:
    def sign_up(self, email, store_name):
        r = self.client.post("/api/v1/auth/signup/", {
            "firstName": "Test", "lastName": "User", "email": email,
            "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
            "mobileNumber": "9876543210", "agreeTerms": True,
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        token = r.json()["access"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        r = self.client.post("/api/v1/stores/", {"storeName": store_name}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        store = r.json()

        self.use(token, store["storeId"])
        return token, store

    def use(self, token, store_id):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=store_id)

    def ok(self, method, url, data=None, expect=200):
        r = getattr(self.client, method)(url, data, format="json")
        self.assertEqual(r.status_code, expect, f"{method.upper()} {url}: {r.content[:500]}")
        return r.json() if r.content else None

    def stock(self, variant):
        rows = self.ok("get", f"/api/v1/inventory/items/?variant={variant}")
        return {k: sum(row[k] for row in rows) for k in ("available", "committed", "on_hand")}


class OrderJourneyTest(StoreClientMixin, APITestCase):
    def test_full_journey(self):
        token, store = self.sign_up("owner@example.com", "Journey Store")

        # catalog + inventory: a product with 10 in stock at the warehouse
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Linen Shirt", "status": "active", "price": "1000", "sku": "LS-1",
        }, expect=201)
        variant = product["variants"][0]["id"]
        warehouse = self.ok("post", "/api/v1/inventory/locations/", {"name": "Warehouse"}, expect=201)
        self.ok("post", "/api/v1/inventory/items/set/", {
            "variant": variant, "location": warehouse["id"], "available": 10,
        })
        self.assertEqual(self.stock(variant), {"available": 10, "committed": 0, "on_hand": 10})

        # customers
        customer = self.ok("post", "/api/v1/customers/", {
            "first_name": "Priya", "email": "PRIYA@example.com",
        }, expect=201)
        self.assertEqual(customer["email"], "priya@example.com")

        # discounts: code check
        self.ok("post", "/api/v1/discounts/", {
            "name": "Summer", "discount_type": "percentage", "value": "10", "codes": ["summer10"],
        }, expect=201)
        check = self.ok("post", "/api/v1/discounts/check/", {"code": "SUMMER10", "subtotal": "3000"})
        self.assertEqual(check["amount"], "300.00")

        # orders: 3 shirts, discount + shipping. Stock moves to committed.
        order = self.ok("post", "/api/v1/orders/", {
            "customer": customer["id"],
            "items": [{"product": product["id"], "quantity": 3}],
            "discount_amount": "300", "shipping_amount": "50",
        }, expect=201)
        self.assertEqual(order["order_number"], "#1001")
        self.assertEqual((order["subtotal"], order["total"]), ("3000.00", "2750.00"))
        self.assertEqual(self.stock(variant), {"available": 7, "committed": 3, "on_hand": 10})

        # can't sell more than is available
        self.ok("post", "/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 8}],
        }, expect=400)
        self.assertEqual(self.stock(variant)["available"], 7)  # nothing half-done

        # payments: can't overpay; partial refund
        self.ok("post", "/api/v1/payments/", {"order": order["id"], "amount": "9999"}, expect=400)
        payment = self.ok("post", "/api/v1/payments/", {"order": order["id"], "amount": "2750"}, expect=201)
        payment = self.ok("post", f"/api/v1/payments/{payment['id']}/refund/", {"amount": "750"})
        self.assertEqual(payment["status"], "partially_refunded")
        self.ok("post", f"/api/v1/payments/{payment['id']}/refund/", {"amount": "2001"}, expect=400)

        # fulfillment: pack 2 of 3, then the rest. Stock leaves the building.
        item = order["items"][0]["id"]
        self.ok("post", "/api/v1/fulfillments/", {
            "order": order["id"], "items": [{"order_item": item, "quantity": 2}],
        }, expect=201)
        self.assertEqual(self.ok("get", f"/api/v1/orders/{order['id']}/")["fulfillment_status"], "partial")
        self.assertEqual(self.stock(variant), {"available": 7, "committed": 1, "on_hand": 8})
        fulfillment = self.ok("post", "/api/v1/fulfillments/", {"order": order["id"]}, expect=201)
        self.assertEqual(self.ok("get", f"/api/v1/orders/{order['id']}/")["fulfillment_status"], "fulfilled")
        self.assertEqual(self.stock(variant), {"available": 7, "committed": 0, "on_hand": 7})

        # a fulfilled order can't be cancelled
        self.ok("post", f"/api/v1/orders/{order['id']}/cancel/", expect=400)

        # delivery: ship, then delivered -> order delivery status follows
        gateway = self.ok("post", "/api/v1/delivery/gateways/", {"name": "BlueDart", "code": "BLUEDART"}, expect=201)
        shipment = self.ok("post", "/api/v1/delivery/shipments/", {
            "fulfillment": fulfillment["id"], "delivery_gateway": gateway["id"], "tracking_number": "BD123",
        }, expect=201)
        self.assertEqual(self.ok("get", f"/api/v1/orders/{order['id']}/")["delivery_status"], "shipped")
        shipment = self.ok("post", f"/api/v1/delivery/shipments/{shipment['id']}/events/", {
            "status": "delivered", "location": "Hyderabad",
        })
        self.assertEqual(shipment["status"], "delivered")
        self.assertEqual(self.ok("get", f"/api/v1/orders/{order['id']}/")["delivery_status"], "delivered")

        # purchasing: receive 20 more from a supplier
        po = self.ok("post", "/api/v1/purchase-orders/", {
            "supplier_name": "Cotton Co", "items": [{"variant": variant, "quantity": 20}],
        }, expect=201)
        self.ok("post", f"/api/v1/purchase-orders/{po['id']}/order/")
        self.ok("post", f"/api/v1/purchase-orders/{po['id']}/receive/", {"location": warehouse["id"]})
        self.assertEqual(self.stock(variant), {"available": 27, "committed": 0, "on_hand": 27})

        # inventory transfer between locations
        shop = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop floor"}, expect=201)
        transfer = self.ok("post", "/api/v1/inventory/transfers/", {
            "source_location": warehouse["id"], "destination_location": shop["id"],
            "items": [{"variant": variant, "quantity": 5}],
        }, expect=201)
        self.ok("post", f"/api/v1/inventory/transfers/{transfer['id']}/complete/")
        at_shop = self.ok("get", f"/api/v1/inventory/items/?variant={variant}&location={shop['id']}")
        self.assertEqual(at_shop[0]["available"], 5)

        # cancelling an unfulfilled order puts stock back
        order2 = self.ok("post", "/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 4}],
        }, expect=201)
        self.assertEqual(order2["order_number"], "#1002")
        self.assertEqual(self.stock(variant)["available"], 23)
        self.ok("post", f"/api/v1/orders/{order2['id']}/cancel/")
        self.assertEqual(self.stock(variant)["available"], 27)

        # draft order -> real order
        draft = self.ok("post", "/api/v1/orders/drafts/", {
            "customer": customer["id"],
            "details": [{"product": product["id"], "quantity": 2}],
        }, expect=201)
        self.assertEqual(draft["total"], "2000.00")
        created = self.ok("post", f"/api/v1/orders/drafts/{draft['id']}/complete/", expect=201)
        self.assertEqual(created["total"], "2000.00")

        # gift cards: issue, spend, can't overspend
        card = self.ok("post", "/api/v1/gift-cards/", {"initial_balance": "500"}, expect=201)
        self.assertRegex(card["code"], r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$")
        card = self.ok("post", f"/api/v1/gift-cards/{card['id']}/adjust/", {"amount": "-200"})
        self.assertEqual(card["current_balance"], "300.00")
        self.ok("post", f"/api/v1/gift-cards/{card['id']}/adjust/", {"amount": "-301"}, expect=400)
        self.ok("post", f"/api/v1/gift-cards/{card['id']}/adjust/", {"amount": "201"}, expect=400)

        # wishlist
        wishlist = self.ok("post", "/api/v1/wishlists/", {"customer": customer["id"], "name": "Birthday"}, expect=201)
        self.ok("post", "/api/v1/wishlists/items/", {"wishlist": wishlist["id"], "product": product["id"]}, expect=201)
        self.assertEqual(len(self.ok("get", f"/api/v1/wishlists/{wishlist['id']}/")["items"]), 1)

        # commerce (read-only for staff)
        self.assertEqual(self.ok("get", "/api/v1/carts/"), [])
        self.ok("post", "/api/v1/carts/", {}, expect=405)


class StoreIsolationTest(StoreClientMixin, APITestCase):
    """Store B must never see or use store A's data."""

    def test_stores_are_isolated(self):
        token_a, store_a = self.sign_up("a@example.com", "Store A")
        product_a = self.ok("post", "/api/v1/catalog/products/", {"title": "A-only", "price": "10"}, expect=201)
        customer_a = self.ok("post", "/api/v1/customers/", {"first_name": "Alice"}, expect=201)
        location_a = self.ok("post", "/api/v1/inventory/locations/", {"name": "A warehouse"}, expect=201)

        token_b, store_b = self.sign_up("b@example.com", "Store B")

        # B can't list or read A's records
        self.assertEqual(self.ok("get", "/api/v1/customers/"), [])
        self.ok("get", f"/api/v1/customers/{customer_a['id']}/", expect=404)
        self.ok("get", f"/api/v1/catalog/products/{product_a['id']}/", expect=404)

        # B can't use A's records in its own orders, stock or wishlists
        self.ok("post", "/api/v1/orders/", {"items": [{"product": product_a["id"], "quantity": 1}]}, expect=400)
        self.ok("post", "/api/v1/inventory/items/set/", {
            "variant": product_a["variants"][0]["id"], "location": location_a["id"], "available": 99,
        }, expect=400)
        self.ok("post", "/api/v1/wishlists/", {"customer": customer_a["id"], "name": "x"}, expect=400)

        # B can't act as store A by changing the header
        self.use(token_b, store_a["storeId"])
        self.ok("get", "/api/v1/customers/", expect=403)

        # and nothing needs a login? No - everything does.
        self.client.credentials()
        self.ok("get", "/api/v1/orders/", expect=401)
