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
        product_a = self.ok("post", "/api/v1/catalog/products/", {"title": "A-only", "sku": "SKU-A-ONLY", "price": "10"}, expect=201)
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
        product = self.ok("post", "/api/v1/catalog/products/", {"title": "Picked lamp", "sku": "SKU-PICKED-LAMP", "status": "active", "price": "5"}, expect=201)
        self.ok("post", "/api/v1/catalog/products/", {"title": "Other chair", "sku": "SKU-OTHER-CHAIR", "status": "active", "price": "5"}, expect=201)

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
        product = self.ok("post", "/api/v1/catalog/products/", {"title": "Blue vase", "sku": "SKU-BLUE-VASE", "status": "active", "price": "9"}, expect=201)
        collection = self.ok("post", "/api/v1/catalog/collections/", {"name": "Summer"}, expect=201)

        self.ok("put", "/api/v1/themes/settings/", self.index(["story", "quiet"], {
            "story": {"type": "custom", "settings": {"heading": "Our story"}, "blocks": [
                {"type": "heading", "settings": {"text": "Since 1999", "size": "large"}},
                {"type": "text", "settings": {"body": "<p>Made <em>by hand</em></p><script>bad()</script>"}},
                {"type": "button", "settings": {"label": "Visit", "link": "/collections/all", "b_style": "outline"}},
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

    def test_preview_supports_edit_mode(self):
        """Labels and block indexes for Edit mode; links stay inside the preview; the public store is untouched."""
        _, store = self.sign_up("editmode@example.com", "Edit Mode Shop")
        body = self.index(["story", "hero"], {
            "story": {"type": "custom", "settings": {"heading": "Our story"}, "blocks": [
                {"type": "heading", "settings": {"text": "Since 1999"}},
                {"type": "heading", "hidden": True, "settings": {"text": "Hidden one"}},
                {"type": "text", "settings": {"body": "<p>Made by hand</p>"}},
            ]},
            "hero": {"type": "hero", "settings": {"heading": ""}},
        })
        html = self.ok("post", "/api/v1/themes/preview/", body)["html"]
        self.assertIn('data-tss-section="story" data-tss-label="Our story"', html)
        self.assertIn('data-tss-section="hero" data-tss-label="Hero banner"', html)  # no heading: the section type
        self.assertIn('data-tss-block="0"', html)
        self.assertIn('data-tss-block="2"', html)  # indexes match the saved list, hidden blocks included
        self.assertNotIn('data-tss-block="1"', html)
        self.assertIn('target="_self"', html)  # Edit mode off: links open inside the preview
        self.assertIn('m.type === "mode"', html)

        self.ok("put", "/api/v1/themes/settings/", body)
        public = self.home(store["storeSlug"])
        self.assertNotIn("data-tss-section", public)
        self.assertNotIn("tss-edit", public)

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


class SectionOptionsTest(StoreClientMixin, APITestCase):
    """Customizer options: layout/style/advanced on every section, plus per-type options."""

    def home(self, slug):
        r = self.client.get(f"/s/{slug}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def index(self, order, sections):
        return {"templates": {"index": {"order": order, "sections": sections}}}

    def test_common_options_wrap_every_section(self):
        _, store = self.sign_up("opts@example.com", "Opts Shop")
        self.ok("put", "/api/v1/themes/settings/", self.index(["about"], {"about": {"type": "rich-text", "settings": {
            "heading": "Wrapped", "width": "narrow", "spacing_top": 80, "spacing_bottom": 8, "padding_x": 40,
            "content_alignment": "right", "background_color": "#ffeedd", "text_color": "#112233",
            "heading_size": "large", "heading_weight": "semibold", "border_radius": 12, "divider": "both",
            "visibility": "mobile", "animation": "slide-up", "anchor_id": "#Our-Story", "css_class": "promo  dark",
        }}}))
        html = self.home(store["storeSlug"])
        self.assertIn('id="our-story"', html)
        for part in ("tss-w-narrow", "tss-hs-large", "tss-hw-semibold", "tss-vis-mobile", "tss-div-both",
                     "tss-align-right", "tss-anim-slide-up", "promo dark", "--tss-pt:80px", "--tss-pb:8px",
                     "--tss-px:40px", "--tss-radius:12px", "--tss-bg:#FFEEDD", "--tss-color:#112233"):
            self.assertIn(part, html)

    def test_invalid_options_are_rejected(self):
        self.sign_up("badopts@example.com", "Bad Opts")
        for settings in (
            {"anchor_id": "has space"},
            {"background_color": "red"},
            {"css_class": "x\" onclick=\"y"},
            {"spacing_top": 500},
            {"width": "huge"},
        ):
            self.ok("put", "/api/v1/themes/settings/", self.index(["a"], {"a": {"type": "custom", "settings": settings}}), expect=400)

    def test_hero_options(self):
        _, store = self.sign_up("hero@example.com", "Hero Shop")
        self.ok("put", "/api/v1/themes/settings/", self.index(["h"], {"h": {"type": "hero", "settings": {
            "label_text": "New in", "heading": "Big news", "button_label": "Shop", "button_new_tab": True,
            "button2_label": "Learn more", "button2_link": "/collections/all", "height": "custom", "custom_height": 700,
            "vertical_position": "bottom", "text_max_width": 500, "image_url": "https://img.example/a.jpg",
            "mobile_image_url": "https://img.example/m.jpg", "overlay_color": "#102030", "b_style": "outline",
            "b_bg": "#ff0000", "image_position": "top",
        }}}))
        html = self.home(store["storeSlug"])
        for part in ("New in", "Learn more", 'target="_blank"', "min-height:700px", "hero--v-bottom",
                     "max-width: 500px", 'srcset="https://img.example/m.jpg"', "background: #102030",
                     "tss-btn", "--b-bg:transparent", "--b-tx:#FF0000", "object-position:top"):
            self.assertIn(part, html)

    def test_product_list_sources_and_layout(self):
        _, store = self.sign_up("lists@example.com", "List Shop")
        cheap = self.ok("post", "/api/v1/catalog/products/", {"title": "Rarely bought", "sku": "SKU-RARELY-BOUGHT", "status": "active", "price": "1"}, expect=201)
        popular = self.ok("post", "/api/v1/catalog/products/", {"title": "Best seller lamp", "sku": "SKU-BEST-SELLER-LAMP", "status": "active", "price": "2", "tags": ["cosy"]}, expect=201)
        self.ok("post", "/api/v1/orders/", {"items": [{"product": popular["id"], "quantity": 5}]}, expect=201)
        self.ok("post", "/api/v1/orders/", {"items": [{"product": cheap["id"], "quantity": 1}]}, expect=201)

        self.ok("put", "/api/v1/themes/settings/", self.index(["best"], {"best": {"type": "product-grid", "settings": {
            "source": "best_sellers", "limit": 2, "layout": "carousel", "columns": 5, "columns_mobile": 1,
            "gap": 10, "image_ratio": "portrait", "show_tags": True, "show_price": False, "show_add_to_cart": True,
            "view_all_label": "See everything", "card_hover": "lift", "card_border": True,
        }}}))
        html = self.home(store["storeSlug"])
        self.assertLess(html.index("Best seller lamp"), html.index("Rarely bought"))
        for part in ("product-list--carousel", "--cols: 5", "--cols-mobile: 1", "--gap: 10px",
                     "product-card__media--portrait", "cosy", "product-card__cta", "See everything",
                     "card-hover--lift", "cards--bordered"):
            self.assertIn(part, html)
        self.assertNotIn('class="price"', html)

    def test_rich_text_options(self):
        _, store = self.sign_up("rich@example.com", "Rich Shop")
        self.ok("put", "/api/v1/themes/settings/", self.index(["r"], {"r": {"type": "rich-text", "settings": {
            "text": "<h3>Title</h3><blockquote>Quote</blockquote>", "button_label": "Read more",
            "image_url": "https://img.example/r.jpg", "image_position": "right", "text_columns": "2",
            "text_size": "large", "line_spacing": "relaxed", "quote_style": "highlighted", "text_width": "wide",
        }}}))
        html = self.home(store["storeSlug"])
        for part in ("<h3>Title</h3>", "<blockquote>Quote</blockquote>", "Read more", "rich-text--image-right",
                     "rich-text__body--2col", "rich-text--size-large", "rich-text--lh-relaxed",
                     "rich-text--quote-highlighted", "rich-text--wide"):
            self.assertIn(part, html)

    def test_old_saved_values_still_load(self):
        _, store = self.sign_up("old@example.com", "Old Shop")
        from apps.tenancy.models import Store
        from apps.themes.services import get_store_theme

        store_theme = get_store_theme(Store.objects.get(pk=store["storeId"]))
        store_theme.custom_data = self.index(["g"], {"g": {"type": "product-grid", "settings": {
            "limit": 24, "columns": 9, "text_alignment": "left",
        }}})
        store_theme.save()
        resolved = self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]["sections"]["g"]["settings"]
        self.assertEqual((resolved["limit"], resolved["columns"]), (12, 6))
        self.ok("put", "/api/v1/themes/settings/", self.ok("get", "/api/v1/themes/settings/"))  # saves cleanly
        self.home(store["storeSlug"])

    def test_untouched_starter_files_are_upgraded(self):
        import hashlib

        from apps.tenancy.models import Store
        from apps.themes import services

        _, store = self.sign_up("upgrade@example.com", "Upgrade Shop")
        store_theme = services.get_store_theme(Store.objects.get(pk=store["storeId"]))
        theme = store_theme.theme
        old = "<section>old hero</section>"
        edited = "<section>my own hero</section>"
        theme.files.filter(file_path="sections/hero.liquid").update(content=old)
        theme.files.filter(file_path="sections/rich-text.liquid").update(content=edited)
        theme.theme_data = {**theme.theme_data, "starter_revision": 1}
        theme.save()

        upgrades = {
            "sections/hero.liquid": {hashlib.sha256(old.encode()).hexdigest()},
            "sections/rich-text.liquid": {"not-this-content"},
        }
        original = services.STARTER_UPGRADES
        services.STARTER_UPGRADES = upgrades
        try:
            services.get_store_theme(Store.objects.get(pk=store["storeId"]))
        finally:
            services.STARTER_UPGRADES = original

        files = dict(theme.files.values_list("file_path", "content"))
        self.assertEqual(files["sections/hero.liquid"], services.starter_files()["sections/hero.liquid"])
        self.assertEqual(files["sections/rich-text.liquid"], edited)  # edited by the owner: left alone


class SectionBlocksTest(StoreClientMixin, APITestCase):
    """Blocks inside sections: all block types, hiding, layout, and blocks in ready-made sections."""

    def home(self, slug):
        r = self.client.get(f"/s/{slug}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def index(self, order, sections):
        return {"templates": {"index": {"order": order, "sections": sections}}}

    def test_every_block_type_renders(self):
        _, store = self.sign_up("blocks@example.com", "Blocks Shop")
        blocks = [
            {"type": "divider", "settings": {"line_style": "dashed", "thickness": 3, "color": "#ff0000"}},
            {"type": "spacer", "settings": {"height": 64}},
            {"type": "icon_text", "settings": {"icon": "truck", "title": "Fast delivery", "text": "2 days", "icon_position": "left"}},
            {"type": "list", "settings": {"items": "Soft\n\nWarm\n", "list_style": "checks"}},
            {"type": "quote", "settings": {"text": "Love it", "author": "Asha", "quote_style": "highlighted"}},
            {"type": "social", "settings": {"instagram": "https://instagram.com/shop", "x": "https://x.com/shop"}},
            {"type": "countdown", "settings": {"title": "Ends soon", "end": "2030-01-01T10:00", "expired_text": "Over"}},
            {"type": "accordion", "settings": {"items": "Q one?\nA one.\n\nQ two?\nA two.", "open_first": True}},
            {"type": "map", "settings": {"address": "MG Road, Bengaluru", "height": 300}},
            {"type": "video", "settings": {"url": "https://cdn.example/clip.mp4", "autoplay": True}},
            {"type": "button", "settings": {"label": "Go", "link": "/collections/all", "new_tab": True}},
            {"type": "heading", "hidden": True, "settings": {"text": "Secret heading"}},
        ]
        self.ok("put", "/api/v1/themes/settings/", self.index(["c"], {"c": {"type": "custom", "settings": {
            "heading": "All blocks", "columns": 3, "block_gap": 12, "vertical_align": "center",
        }, "blocks": blocks}}))
        html = self.home(store["storeSlug"])

        for part in ("--tss-line:3px", "--tss-line-style:dashed", "--tss-line-color:#FF0000", "height:64px",
                     "tss-icon-text--left", "Fast delivery", "tss-list--checks", "<li>Soft</li><li>Warm</li>",
                     "tss-quote--highlighted", "Asha", "Instagram", 'href="https://x.com/shop"',
                     'data-tss-countdown="2030-01-01T10:00"', "<details open><summary>Q one?</summary>",
                     "maps?q=MG%20Road%2C%20Bengaluru&amp;output=embed", "--tss-map-h:300px",
                     'src="https://cdn.example/clip.mp4"', "autoplay muted loop", 'target="_blank"',
                     "--tss-cols:3", "--tss-gap:12px", "--tss-valign:center"):
            self.assertIn(part, html)
        self.assertNotIn("Secret heading", html)

    def test_block_spacing_alignment_width(self):
        _, store = self.sign_up("blockopts@example.com", "Block Opts")
        self.ok("put", "/api/v1/themes/settings/", self.index(["c"], {"c": {"type": "custom", "settings": {}, "blocks": [
            {"type": "heading", "settings": {"text": "Styled", "spacing_top": 24, "spacing_bottom": 8,
                                              "alignment": "right", "width": "full"}},
        ]}}))
        html = self.home(store["storeSlug"])
        for part in ("tss-bw-full", "tss-balign-right", "--tss-bpt:24px", "--tss-bpb:8px"):
            self.assertIn(part, html)

    def test_blocks_in_ready_made_sections(self):
        _, store = self.sign_up("heroblocks@example.com", "Hero Blocks")
        self.ok("put", "/api/v1/themes/settings/", self.index(["h", "s"], {
            "h": {"type": "hero", "settings": {"heading": "Banner"}, "blocks": [
                {"type": "quote", "settings": {"text": "Under the banner"}},
            ]},
            "s": {"type": "slideshow", "settings": {}, "blocks": [
                {"type": "slide", "settings": {"heading": "Slide one"}},
                {"type": "slide", "hidden": True, "settings": {"heading": "Hidden slide"}},
                {"type": "text", "settings": {"body": "<p>After the slides</p>"}},
            ]},
        }))
        html = self.home(store["storeSlug"])
        self.assertLess(html.index("Banner"), html.index("Under the banner"))
        self.assertLess(html.index("Slide one"), html.index("After the slides"))
        self.assertNotIn("Hidden slide", html)
        # The text block isn't drawn as a slide
        self.assertEqual(html.count('aria-roledescription="slide"'), 1)

    def test_block_validation(self):
        self.sign_up("blockbad@example.com", "Block Bad")
        bad = [
            self.index(["c"], {"c": {"type": "custom", "settings": {}, "blocks": [
                {"type": "countdown", "settings": {"end": "next friday"}}]}}),
            self.index(["c"], {"c": {"type": "custom", "settings": {}, "blocks": [
                {"type": "heading", "hidden": "yes", "settings": {}}]}}),
            self.index(["c"], {"c": {"type": "custom", "settings": {"columns": 6}}}),
            self.index(["s"], {"s": {"type": "slideshow", "settings": {}, "blocks": [
                {"type": "slide", "settings": {}} for _ in range(9)]}}),
        ]
        for body in bad:
            self.ok("put", "/api/v1/themes/settings/", body, expect=400)


class TypographyTest(StoreClientMixin, APITestCase):
    """Fonts, text options, button options and theme defaults."""

    def home(self, slug):
        r = self.client.get(f"/s/{slug}/")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_theme_defaults_and_only_used_fonts_load(self):
        _, store = self.sign_up("fonts@example.com", "Fonts Shop")
        self.ok("put", "/api/v1/themes/settings/", {
            "settings": {"heading_font": "playfair", "body_font": "inter", "base_font_size": 18,
                         "button_style": "outline", "button_bg": "#123456", "button_radius": "square",
                         "button_hover": "lift"},
            "templates": {"index": {"order": ["c"], "sections": {"c": {"type": "custom", "settings": {}, "blocks": [
                {"type": "heading", "settings": {"text": "Hello", "t_font": "oswald"}},
            ]}}}},
        })
        html = self.home(store["storeSlug"])
        self.assertIn("fonts.googleapis.com/css2?family=Inter:wght@", html)
        self.assertIn("family=Oswald:wght@", html)
        self.assertIn("family=Playfair+Display:wght@", html)
        self.assertNotIn("family=Poppins", html)  # not used, not loaded
        for part in ('--tss-heading-font:"Playfair Display",Georgia,serif', '--tss-body-font:"Inter",system-ui,sans-serif',
                     "--tss-base-size:18px", "--b-bg:transparent", "--b-tx:#123456", "--b-r:0px", "--b-ht:translateY(-2px)"):
            self.assertIn(part, html)

    def test_block_text_options(self):
        _, store = self.sign_up("text@example.com", "Text Shop")
        self.ok("put", "/api/v1/themes/settings/", {"templates": {"index": {"order": ["c"], "sections": {"c": {
            "type": "custom", "settings": {}, "blocks": [{"type": "quote", "settings": {
                "text": "Styled quote", "t_font": "lora", "t_size": 30, "t_size_mobile": 18, "t_weight": "300",
                "t_italic": True, "t_underline": True, "t_strike": True, "t_transform": "uppercase",
                "t_color": "#334455", "t_line_height": "1.8", "t_letter_spacing": 2, "t_word_spacing": 6,
                "t_align": "justify", "t_shadow": "soft", "t_link_color": "#ff8800", "t_link_hover": True,
            }}]}}}}})
        html = self.home(store["storeSlug"])
        for part in ("font-family:&#34;Lora&#34;,Georgia,serif", "tss-fs", "--t-fsd:30px", "--t-fsm:18px", "font-weight:300",
                     "font-style:italic", "text-decoration:underline line-through", "text-transform:uppercase",
                     "color:#334455", "line-height:1.8", "letter-spacing:2px", "word-spacing:6px", "text-align:justify",
                     "text-shadow:0 1px 2px", "--t-link:#FF8800", "tss-link-hover"):
            self.assertIn(part, html)

    def test_button_block_options(self):
        _, store = self.sign_up("buttons@example.com", "Button Shop")
        self.ok("put", "/api/v1/themes/settings/", {"templates": {"index": {"order": ["c"], "sections": {"c": {
            "type": "custom", "settings": {}, "blocks": [{"type": "button", "settings": {
                "label": "Email us", "link": "mailto:hello@example.com", "b_icon": "mail", "b_icon_position": "left",
                "b_style": "soft", "b_bg": "#00aa55", "b_hover_bg": "#008844", "b_size": "large", "b_radius": "rounded",
                "b_border_width": 2, "b_full_width": True, "b_font": "poppins", "b_font_size": 18, "b_weight": "700",
                "b_transform": "uppercase", "b_shadow": "strong", "b_hover": "grow", "b_speed": 300, "b_align": "center",
            }}]}}}}})
        html = self.home(store["storeSlug"])
        for part in ('href="mailto:hello@example.com"', "tss-btn", "tss-bi-left", "tss-ba-center",
                     "color-mix(in srgb, #00AA55 14%, transparent)", "--b-hbg:#008844", "--b-mh:56px", "--b-r:12px",
                     "--b-bw:2px", "--b-w:100%", "Poppins", "--b-fs:18px", "--b-fw:700", "--b-tt:uppercase",
                     "--b-sh:0 10px 24px", "--b-ht:scale(1.04)", "--b-sp:300ms", "--b-icon:url(", "family=Poppins"):
            self.assertIn(part, html)

    def test_typography_validation(self):
        self.sign_up("typobad@example.com", "Typo Bad")
        cases = [
            {"settings": {"heading_font": "comic-sans"}},
            {"templates": {"index": {"order": ["c"], "sections": {"c": {"type": "custom", "settings": {}, "blocks": [
                {"type": "heading", "settings": {"t_size": 5}}]}}}}},
            {"templates": {"index": {"order": ["c"], "sections": {"c": {"type": "custom", "settings": {}, "blocks": [
                {"type": "button", "settings": {"link": "javascript:alert(1)"}}]}}}}},
        ]
        for body in cases:
            self.ok("put", "/api/v1/themes/settings/", body, expect=400)
        ok = {"templates": {"index": {"order": ["c"], "sections": {"c": {"type": "custom", "settings": {}, "blocks": [
            {"type": "button", "settings": {"link": "tel:+919876543210"}},
            {"type": "button", "settings": {"link": "#faq"}}]}}}}}
        self.ok("put", "/api/v1/themes/settings/", ok)


class LivePreviewAndAutosaveTest(StoreClientMixin, APITestCase):
    """The customizer's fast path: one-section previews and partial saves."""

    def index(self, order, sections):
        return {"templates": {"index": {"order": order, "sections": sections}}}

    def test_single_section_preview(self):
        self.sign_up("one@example.com", "One Shop")
        body = self.index(["a", "b"], {
            "a": {"type": "custom", "settings": {"heading": "First one"}},
            "b": {"type": "custom", "settings": {"heading": "Second one", "t_heading_font": "lora"}},
        })
        r = self.ok("post", "/api/v1/themes/preview/?section=b", body)
        self.assertIn('data-tss-section="b"', r["html"])
        self.assertIn("Second one", r["html"])
        self.assertNotIn("First one", r["html"])
        self.assertNotIn("<html", r["html"])
        self.assertIn("family=Lora", r["fonts"])

        hidden = self.index(["a"], {"a": {"type": "custom", "hidden": True, "settings": {"heading": "Gone"}}})
        self.assertNotIn("Gone", self.ok("post", "/api/v1/themes/preview/?section=a", hidden)["html"])

    def test_partial_save_merges_changes(self):
        _, store = self.sign_up("patch@example.com", "Patch Shop")
        self.ok("put", "/api/v1/themes/settings/", self.index(["a", "b"], {
            "a": {"type": "custom", "settings": {"heading": "Keep me"}},
            "b": {"type": "custom", "settings": {"heading": "Remove me"}},
        }))

        self.ok("patch", "/api/v1/themes/settings/", {
            "settings": {"accent_color": "#AA0000"},
            "templates": {"index": {
                "order": ["c", "a"],
                "sections": {"b": None, "c": {"type": "custom", "settings": {"heading": "Brand new"}}},
            }},
        })
        saved = self.ok("get", "/api/v1/themes/settings/")
        index = saved["templates"]["index"]
        self.assertEqual(index["order"], ["c", "a"])
        self.assertEqual(set(index["sections"]), {"a", "c"})
        self.assertEqual(index["sections"]["a"]["settings"]["heading"], "Keep me")
        self.assertEqual(saved["settings"]["accent_color"], "#AA0000")
        html = self.client.get(f"/s/{store['storeSlug']}/").content.decode()
        self.assertLess(html.index("Brand new"), html.index("Keep me"))
        self.assertNotIn("Remove me", html)

    def test_partial_save_is_validated(self):
        self.sign_up("patchbad@example.com", "Patch Bad")
        for body in (
            {"templates": {"index": {"sections": {"x": {"type": "custom", "settings": {}}}}}},  # not in order
            {"templates": {"nowhere": {}}},
            {"settings": {"accent_color": "red"}},
        ):
            self.ok("patch", "/api/v1/themes/settings/", body, expect=400)


class BlogPagesTest(StoreClientMixin, APITestCase):
    """Blog and article pages: schema, page-aware preview with sample posts, and the theme files."""

    def setUp(self):
        _, self.store = self.sign_up("blog@example.com", "Sip Journal")

    def preview(self, page, data=None, expect=200, **query):
        query = "&".join(f"{k}={v}" for k, v in {"page": page, **query}.items())
        r = self.client.post(f"/api/v1/themes/preview/?{query}", data or {}, format="json")
        self.assertEqual(r.status_code, expect, r.content[:300])
        return r.json()

    def test_blog_page_previews_sample_posts_with_the_blog_settings(self):
        html = self.preview("blog", {
            "settings": {"blog_title": "The Journal", "blog_description": "Notes from the bar"},
            "templates": {"blog": {"order": ["intro", "main"], "sections": {
                "intro": {"type": "rich-text", "settings": {"heading": "Fresh stories"}},
                "main": {"type": "main-blog", "settings": {"layout": "list", "posts_per_page": 3}},
            }}},
        })["html"]
        self.assertIn("The Journal", html)
        self.assertIn("Notes from the bar", html)
        self.assertLess(html.index("Fresh stories"), html.index("Five summer drinks"))
        self.assertIn("post-list--list", html)
        self.assertIn("Page 1 of 2", html)  # 4 sample posts, 3 per page
        self.assertIn('data-tss-section="main"', html)  # Edit mode can select it

    def test_article_page_previews_a_sample_post(self):
        html = self.preview("article")["html"]
        self.assertIn('<h1 class="article__title">Five summer drinks to make at home</h1>', html)
        self.assertIn("<h2>1. Mint lemonade</h2>", html)
        self.assertIn("Keep reading", html)
        self.assertIn("Asha Rao", html)
        self.assertIn("min read", html)

    def test_one_section_of_a_blog_page_can_be_rerendered(self):
        r = self.preview("blog", section="main")
        self.assertIn('data-tss-section="main"', r["html"])
        self.assertNotIn("<html", r["html"])

    def test_blog_pages_need_their_main_section_and_no_product_sections(self):
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"blog": {"order": [], "sections": {"main": None}}}}, expect=400)
        self.ok("patch", "/api/v1/themes/settings/", {"templates": {"article": {
            "order": ["main", "p"], "sections": {"p": {"type": "product-grid", "settings": {}}},
        }}}, expect=400)
        saved = self.ok("patch", "/api/v1/themes/settings/", {"templates": {"article": {
            "order": ["main", "more"], "sections": {"more": {"type": "featured-posts", "settings": {"limit": 2}}},
        }}})
        self.assertTrue(saved["saved"])

    def test_preview_of_other_pages(self):
        self.assertIn("Add an active product", self.preview("product")["html"])
        self.ok("post", "/api/v1/catalog/products/", {"title": "Oat milk", "sku": "OAT-1", "status": "active", "price": "4"}, expect=201)
        self.assertIn("Oat milk", self.preview("product")["html"])
        self.assertIn("All products", self.preview("collection")["html"])
        self.preview("checkout", expect=400)

    def test_latest_posts_show_in_the_preview_but_not_on_the_live_store_yet(self):
        layout = {"templates": {"index": {"order": ["posts"], "sections": {
            "posts": {"type": "featured-posts", "settings": {"heading": "From the journal", "category": "Guides"}},
        }}}}
        html = self.preview("index", layout)["html"]
        self.assertIn("From the journal", html)
        self.assertIn("A beginner&#39;s guide to tasting notes", html)
        self.assertNotIn("Five summer drinks", html)  # a Recipes post

        self.ok("put", "/api/v1/themes/settings/", layout)
        live = self.client.get(f"/s/{self.store['storeSlug']}/").content.decode()
        self.assertNotIn("From the journal", live)

    def test_code_mode_lists_and_edits_the_blog_files(self):
        files = {f["path"]: f["content"] for f in self.ok("get", "/api/v1/themes/files/")["files"]}
        for path in ("templates/blog.liquid", "templates/article.liquid", "sections/main-blog.liquid",
                     "sections/main-article.liquid", "sections/featured-posts.liquid", "snippets/article-card.liquid",
                     "templates/blog.json", "templates/article.json"):
            self.assertIn(path, files)

        self.ok("post", "/api/v1/themes/files/", {
            "path": "templates/blog.liquid", "content": "<p>Custom blog wrapper</p>{{ sections }}",
        })
        self.assertIn("Custom blog wrapper", self.preview("blog")["html"])
        self.ok("delete", "/api/v1/themes/files/?path=templates/blog.liquid", expect=400)

        # The blog page's sections as JSON: a change made in code shows in the customizer's data
        self.ok("post", "/api/v1/themes/files/", {
            "path": "templates/blog.json",
            "content": '{"order": ["main"], "sections": {"main": {"type": "main-blog", "settings": {"columns": 2}}}}',
        })
        saved = self.ok("get", "/api/v1/themes/settings/")
        self.assertEqual(saved["templates"]["blog"]["sections"]["main"]["settings"]["columns"], 2)

    def test_themes_made_before_the_blog_get_its_files(self):
        from apps.tenancy.models import Store
        from apps.themes.services import get_store_theme

        store = Store.objects.get(store_id=self.store["storeId"])
        theme = get_store_theme(store).theme
        theme.files.filter(file_path__in=["templates/blog.liquid", "sections/main-blog.liquid"]).delete()

        get_store_theme(store)
        paths = set(theme.files.values_list("file_path", flat=True))
        self.assertTrue({"templates/blog.liquid", "sections/main-blog.liquid"} <= paths)
