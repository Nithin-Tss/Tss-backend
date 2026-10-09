"""
End-to-end tests: all apps working as one system.

    python manage.py test --settings=config.test_settings
"""
from django.core.cache import cache
from rest_framework.test import APITestCase


class StoreClientMixin:
    def sign_up(self, email, store_name):
        cache.clear()  # sign-ups are rate limited; give every sign-up a fresh allowance
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
        self.client.cookies.clear()  # sign-up also left a session cookie
        self.ok("get", "/api/v1/orders/", expect=401)


class HomeSectionsTest(StoreClientMixin, APITestCase):
    """Theme customizer: sections saved per store and rendered on the home page in order."""

    def home(self, slug):
        r = self.client.get(f"/s/{slug}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def index(self, order, sections):
        return {"templates": {"index": {"order": order, "sections": sections}}}

    def test_sections_render_in_order_and_hidden_ones_are_skipped(self):
        _, store = self.sign_up("theme@example.com", "Theme Shop")
        product = self.ok("post", "/api/v1/catalog/products/", {"title": "Picked lamp", "status": "active", "price": "5"}, expect=201)
        self.ok("post", "/api/v1/catalog/products/", {"title": "Other chair", "status": "active", "price": "5"}, expect=201)

        saved = self.ok("put", "/api/v1/themes/settings/", self.index(
            ["about", "picks", "promo", "slides"],
            {
                "about": {"type": "rich-text", "settings": {
                    "heading": "Our story",
                    "text": "<p><strong>Hand made</strong></p><script>alert(1)</script><a href='javascript:x'>bad</a>",
                }},
                "picks": {"type": "product-grid", "settings": {"heading": "Staff picks", "products": [product["id"]]}},
                "promo": {"type": "hero", "hidden": True, "settings": {"heading": "Secret sale"}},
                "slides": {"type": "slideshow", "settings": {"heading": "Lookbook"}, "blocks": [
                    {"type": "slide", "settings": {"heading": "Spring", "button_link": "/collections/all"}},
                ]},
            },
        ))
        self.assertTrue(saved["templates"]["index"]["sections"]["promo"]["hidden"])

        html = self.home(store["storeSlug"])
        self.assertLess(html.index("Our story"), html.index("Staff picks"))
        self.assertLess(html.index("Staff picks"), html.index("Lookbook"))
        self.assertIn("<strong>Hand made</strong>", html)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("Secret sale", html)
        self.assertIn("Picked lamp", html)
        self.assertNotIn("Other chair", html)  # only the chosen product

    def test_all_hidden_shows_default_layout(self):
        _, store = self.sign_up("empty@example.com", "Empty Shop")
        self.ok("put", "/api/v1/themes/settings/", self.index(
            ["x"], {"x": {"type": "hero", "hidden": True, "settings": {"heading": "Hidden one"}}},
        ))
        html = self.home(store["storeSlug"])
        self.assertNotIn("Hidden one", html)
        self.assertIn("Welcome to our store", html)

    def test_custom_section_blocks_render_in_order(self):
        _, store = self.sign_up("custom@example.com", "Custom Shop")
        product = self.ok("post", "/api/v1/catalog/products/", {"title": "Blue vase", "status": "active", "price": "9"}, expect=201)
        collection = self.ok("post", "/api/v1/catalog/collections/", {"name": "Summer"}, expect=201)

        self.ok("put", "/api/v1/themes/settings/", self.index(["story", "quiet"], {
            "story": {"type": "custom", "settings": {"heading": "Our story"}, "blocks": [
                {"type": "heading", "settings": {"text": "Since 1999", "size": "large"}},
                {"type": "text", "settings": {"body": "<p>Made <em>by hand</em></p><script>bad()</script>"}},
                {"type": "button", "settings": {"label": "Visit", "link": "/collections/all", "style": "outline"}},
                {"type": "products", "settings": {"products": [product["id"]]}},
                {"type": "collections", "settings": {"collections": [collection["id"]]}},
                {"type": "html", "settings": {"html": (
                    '<div onclick="steal()">Hi<img src="x" onerror="steal()"></div>'
                    '<iframe src="https://evil.example/x"></iframe>'
                    '<iframe src="https://www.google.com/maps/embed?pb=1"></iframe><script>steal()</script>'
                )}},
            ]},
            "quiet": {"type": "custom", "settings": {"heading": "No title shown", "show_heading": False}, "blocks": [
                {"type": "heading", "settings": {"text": "Quiet section"}},
            ]},
        }))

        html = self.home(store["storeSlug"])
        order = ["Our story", "Since 1999", "by hand", "Visit", "Blue vase", 'class="tss-collection"', "Quiet section"]
        positions = [html.index(text) for text in order]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("No title shown", html)
        for bad in ("bad()", "steal()", "onclick", "onerror", "evil.example"):
            self.assertNotIn(bad, html)
        self.assertIn("https://www.google.com/maps/embed?pb=1", html)

    def test_custom_blocks_are_validated(self):
        self.sign_up("valid@example.com", "Valid Shop")
        bad_block = self.index(["c"], {"c": {"type": "custom", "settings": {}, "blocks": [
            {"type": "marquee", "settings": {}},
        ]}})
        self.ok("put", "/api/v1/themes/settings/", bad_block, expect=400)

    def test_preview_renders_draft_without_saving(self):
        _, store = self.sign_up("draft@example.com", "Draft Shop")
        r = self.ok("post", "/api/v1/themes/preview/", self.index(
            ["d"], {"d": {"type": "hero", "settings": {"heading": "Draft banner"}}},
        ))
        self.assertIn("Draft banner", r["html"])
        self.assertIn("<base href=", r["html"])
        self.assertNotIn("Draft banner", self.home(store["storeSlug"]))

        self.ok("post", "/api/v1/themes/preview/", self.index(["d"], {"d": {"type": "nope"}}), expect=400)

    def test_preview_marks_sections_for_dragging(self):
        self.sign_up("drag@example.com", "Drag Shop")
        r = self.client.post("/api/v1/themes/preview/?scroll=240&selected=a", self.index(["a", "b"], {
            "a": {"type": "custom", "settings": {"heading": "First"}},
            "b": {"type": "hero", "settings": {"heading": "Second"}},
        }), format="json")
        self.assertEqual(r.status_code, 200, r.content)
        html = r.json()["html"]
        self.assertIn('data-tss-section="a"', html)
        self.assertIn('data-tss-section="b"', html)
        self.assertIn('"scroll": 240', html)
        self.assertLess(html.index("First"), html.index("Second"))

    def test_preview_config_cannot_break_out_of_script(self):
        self.sign_up("xss@example.com", "Xss Shop")
        r = self.client.post(
            "/api/v1/themes/preview/?selected=a</script><img src=x onerror=alert(1)>",
            self.index(["a"], {"a": {"type": "custom", "settings": {"heading": "Safe"}}}),
            format="json",
        )
        self.assertEqual(r.status_code, 200, r.content)
        html = r.json()["html"]
        self.assertNotIn("a</script>", html)
        self.assertNotIn("<img src=x", html)

    def test_theme_image_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.sign_up("img@example.com", "Image Shop")
        png = SimpleUploadedFile("banner.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        r = self.client.post("/api/v1/themes/images/", {"image": png}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertIn("/media/themes/", r.json()["url"])

        bad = SimpleUploadedFile("banner.png", b"plain text")
        self.assertEqual(self.client.post("/api/v1/themes/images/", {"image": bad}, format="multipart").status_code, 400)

    def test_each_store_keeps_its_own_sections(self):
        _, store_a = self.sign_up("a1@example.com", "Shop A")
        self.ok("put", "/api/v1/themes/settings/", self.index(
            ["h"], {"h": {"type": "custom", "settings": {"heading": "Only in A"}}},
        ))
        _, store_b = self.sign_up("b1@example.com", "Shop B")
        self.assertIn("Only in A", self.home(store_a["storeSlug"]))
        self.assertNotIn("Only in A", self.home(store_b["storeSlug"]))
