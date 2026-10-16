from django.core.cache import cache
from rest_framework.test import APITestCase

from apps.inventory.models import InventoryItem

from .models import Product, ProductVariant
from .services import ProductInUse, delete_product, save_product

URL = "/api/v1/catalog/products/"
VARIANTS = "/api/v1/catalog/variants/"


def signup(client, email):
    cache.clear()  # sign-ups are rate limited; each test starts with a fresh allowance
    r = client.post("/api/v1/auth/signup/", {
        "firstName": "A", "lastName": "B", "email": email,
        "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
        "mobileNumber": "9876543210", "agreeTerms": True,
    }, format="json")
    token = r.json()["access"]
    client.credentials(HTTP_AUTHORIZATION="Bearer " + token)
    r = client.post("/api/v1/stores/", {"storeName": "Shop " + email}, format="json")
    return token, r.json()["storeId"]


class ProductApiTests(APITestCase):
    def setUp(self):
        token, self.store_id = signup(self.client, "a@example.com")
        self.auth = {"HTTP_AUTHORIZATION": "Bearer " + token}
        self.client.credentials(**self.auth, HTTP_X_STORE_ID=self.store_id)

    def create(self, **extra):
        title = extra.get("title", "Shirt")
        body = {"title": title, "sku": "SKU-" + title.upper(), "status": "active", "price": "10", **extra}
        r = self.client.post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def test_create_and_retrieve(self):
        p = self.create(collections=["Summer"], tags=["linen"], category="Tops")
        r = self.client.get(f"{URL}{p['id']}/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["collections"], ["Summer"])
        self.assertEqual(body["tags"], ["linen"])
        self.assertEqual(body["category"], "Tops")
        self.assertEqual(body["price"], "10.00")

    def test_title_required(self):
        r = self.client.post(URL, {"price": "10"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_list(self):
        self.create(title="One")
        self.create(title="Two")
        self.assertEqual(len(self.client.get(URL).json()), 2)

    def test_patch_keeps_untouched_fields(self):
        p = self.create(vendor="Acme", tags=["x"])
        r = self.client.patch(f"{URL}{p['id']}/", {"title": "Renamed"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual(body["title"], "Renamed")
        self.assertEqual(body["vendor"], "Acme")
        self.assertEqual(body["tags"], ["x"])
        self.assertEqual(body["variants"][0]["id"], p["variants"][0]["id"])

    def test_put_replaces_tags(self):
        p = self.create(tags=["x"])
        r = self.client.put(f"{URL}{p['id']}/", {"title": "Shirt", "tags": ["y"]}, format="json")
        self.assertEqual(r.json()["tags"], ["y"])

    def test_delete(self):
        p = self.create(collections=["Summer"], tags=["x"])
        self.assertEqual(self.client.delete(f"{URL}{p['id']}/").status_code, 204)
        self.assertEqual(self.client.get(f"{URL}{p['id']}/").status_code, 404)
        self.assertFalse(Product.objects.filter(pk=p["id"]).exists())

    def test_bulk_delete(self):
        a, b, c = self.create(title="A"), self.create(title="B"), self.create(title="C")
        r = self.client.post(URL + "bulk-delete/", {"ids": [a["id"], b["id"]]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"deleted": 2, "blocked": []})
        self.assertEqual(list(Product.objects.values_list("title", flat=True)), ["C"])
        self.assertEqual(self.client.post(URL + "bulk-delete/", {"ids": []}, format="json").status_code, 400)
        self.assertEqual(self.client.post(URL + "bulk-delete/", {"ids": ["x"]}, format="json").status_code, 400)

    def test_bulk_delete_keeps_stocked_products(self):
        stocked, plain = self.create(title="Stocked", collections=["C"]), self.create(title="Plain")
        variant = ProductVariant.objects.get(pk=stocked["variants"][0]["id"])
        self.stock(variant)
        r = self.client.post(URL + "bulk-delete/", {"ids": [stocked["id"], plain["id"]]}, format="json")
        self.assertEqual(r.json(), {"deleted": 1, "blocked": ["Stocked"]})
        self.assertEqual(self.client.get(f"{URL}{stocked['id']}/").json()["collections"], ["C"])

    def test_delete_blocked_when_stocked(self):
        p = self.create()
        variant = ProductVariant.objects.get(pk=p["variants"][0]["id"])
        self.stock(variant)
        r = self.client.delete(f"{URL}{p['id']}/")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(Product.objects.filter(pk=p["id"]).exists())

    def stock(self, variant):
        from apps.inventory.models import Location
        loc = Location.objects.create(store_id=self.store_id, name="Main")
        InventoryItem.objects.create(
            variant=variant, location=loc, available=1, committed=0,
            unavailable=0, on_hand=1, incoming=0,
        )

    def test_other_store_cannot_see_or_touch(self):
        p = self.create()
        token, other_store = signup(self.client, "b@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_store)
        self.assertEqual(self.client.get(URL).json(), [])
        self.assertEqual(self.client.get(f"{URL}{p['id']}/").status_code, 404)
        self.assertEqual(self.client.delete(f"{URL}{p['id']}/").status_code, 404)

    def test_non_member_is_refused(self):
        _, other_store = signup(self.client, "c@example.com")
        self.client.credentials(**self.auth, HTTP_X_STORE_ID=other_store)
        self.assertEqual(self.client.get(URL).status_code, 403)

    def test_requires_auth(self):
        self.client.credentials()
        self.client.cookies.clear()  # sign-up also left a session cookie
        self.assertIn(self.client.get(URL).status_code, (401, 403))


class VariantApiTests(APITestCase):
    def setUp(self):
        token, self.store_id = signup(self.client, "v@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        r = self.client.post(URL, {"title": "Shirt", "price": "10", "sku": "S1"}, format="json")
        self.product = r.json()
        self.first = self.product["variants"][0]["id"]

    def test_add_list_update_delete(self):
        r = self.client.post(VARIANTS, {"product": self.product["id"], "sku": "S2", "price": "12"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        r = self.client.get(VARIANTS, {"product": self.product["id"]})
        self.assertEqual(len(r.json()), 2)
        r = self.client.patch(f"{VARIANTS}{vid}/", {"price": "15"}, format="json")
        self.assertEqual(r.json()["price"], "15.00")
        self.assertEqual(r.json()["sku"], "S2")
        self.assertEqual(self.client.delete(f"{VARIANTS}{vid}/").status_code, 204)
        self.assertEqual(self.client.get(f"{VARIANTS}{vid}/").status_code, 404)

    def test_cannot_delete_last_variant(self):
        self.assertEqual(self.client.delete(f"{VARIANTS}{self.first}/").status_code, 400)

    def test_cannot_move_to_other_product(self):
        other = self.client.post(URL, {"title": "Hat", "sku": "SKU-HAT", "price": "3"}, format="json").json()
        r = self.client.patch(f"{VARIANTS}{self.first}/", {"product": other["id"]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_cannot_use_other_stores_product(self):
        token, other_store = signup(self.client, "w@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_store)
        r = self.client.post(VARIANTS, {"product": self.product["id"], "price": "1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get(f"{VARIANTS}{self.first}/").status_code, 404)

    def test_negative_price_rejected(self):
        r = self.client.post(VARIANTS, {"product": self.product["id"], "price": "-1"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_duplicate_sku_rejected_across_products(self):
        self.client.patch(f"{VARIANTS}{self.first}/", {"sku": "A-1"}, format="json")
        r = self.client.post(VARIANTS, {"product": self.product["id"], "price": "1", "sku": "A-1"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(URL, {"title": "Other", "price": "1", "sku": "A-1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Product.objects.filter(title="Other").count(), 0)

    def test_variant_can_keep_its_own_sku(self):
        self.client.patch(f"{VARIANTS}{self.first}/", {"sku": "A-1"}, format="json")
        r = self.client.patch(f"{VARIANTS}{self.first}/", {"sku": "A-1", "price": "5"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_duplicate_sku_within_payload_rejected(self):
        r = self.client.post(URL, {"title": "Two", "variants": [
            {"sku": "D", "price": "1"}, {"sku": "D", "price": "2"}]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_sku_required(self):
        r = self.client.post(URL, {"title": "No sku", "price": "1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("sku", r.json())
        self.assertEqual(Product.objects.filter(title="No sku").count(), 0)
        r = self.client.post(VARIANTS, {"product": self.product["id"], "price": "1"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(URL + "bulk/", {"products": [{"title": "x"}]}, format="json")
        self.assertEqual(r.json()["created"], 0)
        self.assertEqual(len(r.json()["errors"]), 1)

    def test_price_update_keeps_sku(self):
        r = self.client.patch(f"{URL}{self.product['id']}/", {"price": "20"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["sku"], "S1")

    def test_bulk_rejects_non_object_body(self):
        r = self.client.post(URL + "bulk/", [{"title": "x"}], format="json")
        self.assertEqual(r.status_code, 400)

    def test_product_update_matches_by_id(self):
        second = self.client.post(VARIANTS, {"product": self.product["id"], "sku": "S2", "price": "12"}, format="json").json()["id"]
        # reorder and reprice: ids must stay attached to their own sku
        body = {"title": "Shirt", "variants": [
            {"id": second, "sku": "S2", "price": "20"},
            {"id": self.first, "sku": "S1", "price": "10"},
            {"sku": "S3", "price": "5"},
        ]}
        r = self.client.put(f"{URL}{self.product['id']}/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        by_sku = {v["sku"]: v for v in r.json()["variants"]}
        self.assertEqual(by_sku["S1"]["id"], self.first)
        self.assertEqual(by_sku["S2"]["id"], second)
        self.assertEqual(by_sku["S2"]["price"], "20.00")
        self.assertEqual(len(by_sku), 3)

    def test_product_update_drops_unlisted_and_rejects_foreign_id(self):
        self.client.post(VARIANTS, {"product": self.product["id"], "sku": "S2", "price": "12"}, format="json")
        r = self.client.put(f"{URL}{self.product['id']}/", {"title": "Shirt", "variants": [
            {"id": self.first, "sku": "S1", "price": "10"}]}, format="json")
        self.assertEqual(len(r.json()["variants"]), 1)
        bad = self.client.put(f"{URL}{self.product['id']}/", {"title": "Shirt", "variants": [
            {"id": "11111111-1111-1111-1111-111111111111", "price": "1"}]}, format="json")
        self.assertEqual(bad.status_code, 400)


class ProductServiceTests(APITestCase):
    def setUp(self):
        from apps.tenancy.models import Store
        _, store_id = signup(self.client, "s@example.com")
        self.store = Store.objects.get(pk=store_id)

    def test_save_product_creates_variant(self):
        p = save_product(self.store, {"title": "T", "sku": "SKU-T", "price": "5"})
        self.assertEqual(p.variants.get().price, 5)
        self.assertEqual(p.status, "draft")

    def test_delete_product_raises_when_in_use(self):
        from apps.inventory.models import Location
        p = save_product(self.store, {"title": "T", "sku": "SKU-T", "price": "5"})
        loc = Location.objects.create(store=self.store, name="Main")
        InventoryItem.objects.create(
            variant=p.variants.get(), location=loc, available=1, committed=0,
            unavailable=0, on_hand=1, incoming=0,
        )
        with self.assertRaises(ProductInUse):
            delete_product(p)
        self.assertTrue(Product.objects.filter(pk=p.pk).exists())


class ProductFormSchemaTests(APITestCase):
    """The "Add product" form: photos, category, stock, channels, template and status."""

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

    def setUp(self):
        token, self.store_id = signup(self.client, "form@example.com")
        self.token = token
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)

    def upload(self, content, name="photo.png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            f"{URL}images/", {"image": SimpleUploadedFile(name, content)}, format="multipart"
        )

    def test_full_form_saves_everything(self):
        urls = [self.upload(self.PNG).json()["url"] for _ in range(2)]
        parent = self.client.post("/api/v1/catalog/categories/", {"name": "Wine"}, format="json").json()
        child = self.client.post(
            "/api/v1/catalog/categories/", {"name": "White", "parent": parent["id"]}, format="json"
        ).json()
        channels = self.client.get("/api/v1/catalog/sales-channels/").json()
        self.assertEqual([c["name"] for c in channels], ["Online Store", "Point of Sale", "Shop"])
        location = self.client.post("/api/v1/inventory/locations/", {"name": "Shop"}, format="json").json()

        r = self.client.post(URL, {
            "title": "Chardonnay", "status": "archived", "price": "19.50", "sku": "CH-1",
            "category_id": child["id"], "theme_template": "featured",
            "images": list(reversed(urls)), "sales_channels": [channels[0]["id"]],
            "location": location["id"], "quantity": 7, "tags": ["dry"],
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()

        self.assertEqual(body["status"], "archived")
        self.assertEqual(body["category"], "White")
        self.assertEqual(body["theme_template"], "featured")
        self.assertEqual([i["url"] for i in body["images"]], list(reversed(urls)))
        self.assertEqual(body["sales_channels"], [channels[0]["id"]])
        self.assertEqual(body["price"], "19.50")

        stock = self.client.get(f"/api/v1/inventory/items/?product={body['id']}").json()
        self.assertEqual([(s["location"], s["available"]) for s in stock], [(location["id"], 7)])

    def test_edit_form_updates_in_place(self):
        """What the shared form sends in edit mode: a PATCH that never creates a second product."""
        location = self.client.post("/api/v1/inventory/locations/", {"name": "Shop"}, format="json").json()
        channels = self.client.get("/api/v1/catalog/sales-channels/").json()
        created = self.client.post(URL, {
            "title": "Chardonnay", "price": "19.50", "sku": "CH-1", "location": location["id"], "quantity": 7,
        }, format="json").json()
        # A second variant (e.g. from an import or the variants API) must survive the edit
        extra = self.client.post(VARIANTS, {"product": created["id"], "sku": "CH-2", "price": "30"}, format="json").json()
        first = created["variants"][0]["id"]
        url = self.upload(self.PNG).json()["url"]

        r = self.client.patch(f"{URL}{created['id']}/", {
            "title": "Chardonnay Reserve", "description": "Oaky.", "status": "active",
            "images": [url], "location": location["id"], "quantity": 3,
            "category_id": None, "product_type": "White wine", "vendor": "Sipnow",
            "collections": ["Summer"], "tags": ["oak"], "sales_channels": [channels[1]["id"]],
            "theme_template": "minimal", "seo_title": "Reserve", "seo_description": "Buy it.",
            "variants": [
                {"id": first, "sku": "CH-1R", "price": "21.00"},
                {"id": extra["id"], "sku": "CH-2", "price": "30.00"},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()

        self.assertEqual(Product.objects.filter(store_id=self.store_id).count(), 1)
        self.assertEqual(body["id"], created["id"])
        self.assertEqual((body["title"], body["status"], body["theme_template"]), ("Chardonnay Reserve", "active", "minimal"))
        self.assertEqual(body["sales_channels"], [channels[1]["id"]])
        self.assertEqual([i["url"] for i in body["images"]], [url])
        self.assertEqual(
            [(v["id"], v["sku"], v["price"]) for v in body["variants"]],
            [(first, "CH-1R", "21.00"), (extra["id"], "CH-2", "30.00")],
        )
        stock = self.client.get(f"/api/v1/inventory/items/?product={created['id']}").json()
        self.assertEqual([(s["variant"], s["available"]) for s in stock], [(first, 3)])

        # Another store can't edit it
        token, other_store = signup(self.client, "edit-other@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_store)
        self.assertEqual(self.client.patch(f"{URL}{created['id']}/", {"title": "Hijack"}, format="json").status_code, 404)

    def test_only_name_required(self):
        r = self.client.post(URL, {"title": "Bare", "sku": "SKU-BARE"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["status"], "draft")

    def test_field_errors(self):
        r = self.client.post(URL, {
            "title": " ", "status": "deleted", "theme_template": "nope",
            "quantity": 3, "images": ["javascript:alert(1)"],
        }, format="json")
        self.assertEqual(r.status_code, 400)
        errors = r.json()
        for field in ("title", "status", "theme_template", "images"):
            self.assertIn(field, errors)

        r = self.client.post(URL, {"title": "No location", "sku": "SKU-NO-LOCATION", "quantity": 3}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("location", r.json())

    def test_upload_checks_type_and_size(self):
        self.assertEqual(self.upload(self.PNG).status_code, 201)
        # A text file renamed to .png is still rejected
        self.assertEqual(self.upload(b"not really an image", "fake.png").status_code, 400)
        # 500KB is fine; 2MB is over the 1MB limit and the error says so
        self.assertEqual(self.upload(self.PNG + b"\x00" * (500 * 1024), "ok.png").status_code, 201)
        too_big = self.upload(self.PNG + b"\x00" * (2 * 1024 * 1024), "big.png")
        self.assertEqual(too_big.status_code, 400)
        self.assertIn("Maximum size is 1MB per image", str(too_big.json()))

    def test_cannot_use_other_stores_records(self):
        location = self.client.post("/api/v1/inventory/locations/", {"name": "Mine"}, format="json").json()
        category = self.client.post("/api/v1/catalog/categories/", {"name": "Mine"}, format="json").json()
        channel = self.client.get("/api/v1/catalog/sales-channels/").json()[0]

        _, other_store = signup(self.client, "other@example.com")
        self.client.credentials(HTTP_AUTHORIZATION=self.client._credentials["HTTP_AUTHORIZATION"], HTTP_X_STORE_ID=other_store)
        r = self.client.post(URL, {
            "title": "Sneaky", "location": location["id"], "quantity": 1,
            "category_id": category["id"], "sales_channels": [channel["id"]],
        }, format="json")
        self.assertEqual(r.status_code, 400)
        for field in ("location", "category_id", "sales_channels"):
            self.assertIn(field, r.json())


class AllProductsStorefrontTests(APITestCase):
    """The storefront's "All products" grid (home page) and the all-products page."""

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "all@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.slug = Store.objects.get(pk=self.store_id).store_slug
        self.channels = {c["type"]: c["id"] for c in self.client.get("/api/v1/catalog/sales-channels/").json()}
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()
        self.n = 0

    def product(self, title, **extra):
        self.n += 1
        body = {"title": title, "status": "active", "price": "10", "sku": f"ALL-{self.n}",
                "location": self.location["id"], "quantity": 5, **extra}
        r = self.client.post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def page(self, path=""):
        r = self.client.get(f"/s/{self.slug}/{path}")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_home_lists_active_online_products_with_sold_out_badge(self):
        self.product("Shown lamp")
        self.product("Empty vase", quantity=0)
        self.product("Draft chair", status="draft")
        self.product("Archived desk", status="archived")
        self.product("Shop-only rug", sales_channels=[self.channels["pos"]])

        html = self.page()  # nothing saved for the home page: the default layout
        self.assertIn(">All products</h2>", html)
        self.assertIn("Shown lamp", html)
        self.assertRegex(html, r'product-card__badge">Sold out</span>(?s:.)*Empty vase')
        for hidden in ("Draft chair", "Archived desk", "Shop-only rug"):
            self.assertNotIn(hidden, html)
        self.assertIn(f'href="/s/{self.slug}/collections/all">View all', html)

    def test_home_shows_12_then_load_more(self):
        for i in range(15):
            self.product(f"Item {i:02d}")
        html = self.page()
        self.assertEqual(html.count('<div data-more style="display:none">'), 3)  # 12 shown, 3 waiting
        self.assertIn("data-load-more>Load more</button>", html)

    def test_empty_store_says_no_products_yet(self):
        self.assertIn("No products yet.", self.page())
        self.assertIn("No products yet.", self.page("collections/all"))

    def test_all_products_page_sorts_and_pages(self):
        self.product("Cheap", price="1")
        self.product("Dear", price="99")
        self.product("Middle", price="50")

        def order(query=""):
            html = self.page(f"collections/all{query}")
            return sorted(("Cheap", "Dear", "Middle"), key=lambda t: html.index(f">{t}<"))

        self.assertIn('<h1 class="section-heading">All products</h1>', self.page("collections/all"))
        self.assertEqual(order(), ["Middle", "Dear", "Cheap"])  # newest first
        self.assertEqual(order("?sort=price-asc"), ["Cheap", "Middle", "Dear"])
        self.assertEqual(order("?sort=price-desc"), ["Dear", "Middle", "Cheap"])

        for i in range(24):
            self.product(f"Bulk {i:02d}")
        html = self.page("collections/all?page=2")
        self.assertIn("Page 2 of 2", html)
        self.assertIn(">Cheap<", html)  # 27 products: the oldest 3 are on page 2

    def test_only_this_stores_products(self):
        self.product("Mine")
        token, other = signup(self.client, "other-all@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other)
        location = self.client.post("/api/v1/inventory/locations/", {"name": "W"}, format="json").json()
        self.client.post(URL, {"title": "Theirs", "status": "active", "price": "1", "sku": "THEIRS-1",
                               "location": location["id"], "quantity": 1}, format="json")
        html = self.page("collections/all")
        self.assertIn("Mine", html)
        self.assertNotIn("Theirs", html)

    def test_import_links_active_products_with_price_and_stock(self):
        r = self.client.post(f"{URL}bulk/", {"products": [
            {"title": "Imported active", "status": "active", "sku": "IMP-A", "quantity": 4},
            {"title": "Imported draft", "status": "draft", "sku": "IMP-D"},
        ]}, format="json")
        self.assertEqual(r.json()["created"], 2, r.content)

        active = Product.objects.get(title="Imported active")
        draft = Product.objects.get(title="Imported draft")
        self.assertEqual(list(active.product_sales_channels.values_list("sales_channel__type", flat=True)), ["online"])
        # Every import is on the Online Store; the Draft one stays hidden until it is made Active
        self.assertEqual(list(draft.product_sales_channels.values_list("sales_channel__type", flat=True)), ["online"])
        self.assertEqual(str(active.variants.get().price), "0.00")
        self.assertEqual(InventoryItem.objects.get(variant__product=active).available, 4)
        self.assertEqual(InventoryItem.objects.get(variant__product=draft).available, 0)
        listing = self.page("collections/all")
        self.assertIn("Imported active", listing)
        self.assertNotIn("Imported draft", listing)

    def test_a_product_made_active_shows_in_the_store(self):
        # Saved with no channel at all (as older imports were), then made Active from the product form
        from .models import ProductSalesChannel

        p = self.product("Late starter", status="draft")
        ProductSalesChannel.objects.filter(product_id=p["id"]).delete()
        r = self.client.patch(f"{URL}{p['id']}/", {"status": "active", "sales_channels": []}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("Late starter", self.page("collections/all"))
        self.assertEqual(self.client.get(f"/s/{self.slug}/products/{p['id']}").status_code, 200)

        # Point of Sale only stays the owner's choice
        pos = self.product("Shop counter only", sales_channels=[self.channels["pos"]])
        self.assertNotIn("Shop counter only", self.page("collections/all"))
        self.assertEqual(pos["sales_channels"], [self.channels["pos"]])

    def test_one_time_fix_links_unlisted_active_products(self):
        from .models import ProductSalesChannel
        from .services import link_active_products_to_online_store

        before = self.product("Old import")
        kept_off = self.product("Shop only", sales_channels=[self.channels["pos"]])
        ProductSalesChannel.objects.filter(product_id=before["id"]).delete()  # as imports used to leave it
        self.assertNotIn("Old import", self.page("collections/all"))

        self.assertEqual(link_active_products_to_online_store(), 1)
        self.assertEqual(link_active_products_to_online_store(), 0)  # running it again changes nothing
        self.assertIn("Old import", self.page("collections/all"))
        self.assertNotIn("Shop only", self.page("collections/all"))  # an owner's choice is kept
        self.assertEqual(kept_off["sales_channels"], [self.channels["pos"]])


class SoldOutBadgeTests(APITestCase):
    """Quantity 0 shows "Sold out" on the product card and the product page; stock above 0 does not."""

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "soldout@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.slug = Store.objects.get(pk=self.store_id).store_slug
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()

    def product(self, title, sku, quantity):
        body = {"title": title, "status": "active", "price": "10", "sku": sku}
        if quantity is not None:
            body.update(location=self.location["id"], quantity=quantity)
        r = self.client.post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def html(self, path):
        return self.client.get(f"/s/{self.slug}/{path}").content.decode()

    def card(self, listing, title):
        start = listing.rindex('<a class="product-card', 0, listing.index(f'product-card__title">{title}<'))
        return listing[start:listing.index("</a>", start)]

    def test_quantity_zero_shows_sold_out_and_ten_does_not(self):
        empty = self.product("Empty vase", "SO-0", 0)
        stocked = self.product("Full vase", "SO-10", 10)
        listing = self.html("collections/all")

        card = self.card(listing, "Empty vase")
        self.assertIn("product-card--sold-out", card)
        self.assertIn('product-card__badge">Sold out</span>', card)
        self.assertIn(f'href="/s/{self.slug}/products/{empty["id"]}"', card)  # still clickable
        self.assertNotIn("Sold out", self.card(listing, "Full vase"))

        page = self.html(f"products/{empty['id']}")
        self.assertRegex(page, r"data-add[^>]*\sdisabled>\s*Sold out")
        self.assertIn("data-quantity disabled", page)
        self.assertNotRegex(page, r"data-add[^>]*>\s*Add to cart")  # the button itself

        page = self.html(f"products/{stocked['id']}")
        # ("Sold out" still appears on this page: the "You may also like" card of the empty vase)
        self.assertNotIn('price--muted">Sold out', page)
        self.assertNotRegex(page, r"data-add[^>]*\sdisabled")
        self.assertRegex(page, r"data-add[^>]*>\s*Add to cart")
        self.assertRegex(page, r"data-stock[^>]*>\s*In stock")

    def test_few_left_and_no_stock_record(self):
        few = self.product("Last ones", "SO-3", 3)
        self.assertRegex(self.html(f"products/{few['id']}"), r"data-stock[^>]*>\s*Only a few left")

        untracked = self.product("Never stocked", "SO-NONE", None)  # no stock record at all
        self.assertIn('product-card__badge">Sold out</span>', self.card(self.html("collections/all"), "Never stocked"))
        self.assertRegex(self.html(f"products/{untracked['id']}"), r"data-add[^>]*\sdisabled>\s*Sold out")

    def test_stock_value_is_a_number(self):
        from apps.themes.rendering import product_drop

        self.product("Counted", "SO-7", 7)
        product = published_products_for(self.store_id).get(title="Counted")
        drop = product_drop(product, "/s/x")
        self.assertIs(type(drop["available_total"]), int)
        self.assertEqual((drop["available_total"], drop["is_sold_out"]), (7, False))


def published_products_for(store_id):
    from apps.tenancy.models import Store

    from .services import published_products

    return published_products(Store.objects.get(pk=store_id))


class BackToAdminBarTests(APITestCase):
    """"View store" opens the storefront with a "Back to admin" bar, for the store's own users only."""

    def setUp(self):
        from django.conf import settings
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "bar@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        self.back = settings.FRONTEND_URL.rstrip("/") + "/admin/online-store/products?page=2"

    def view_store(self, return_url=None, path="/"):
        r = self.client.post("/api/v1/themes/storefront-link/", {"path": path, "return_url": return_url or self.back}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        url = r.json()["url"]
        landing = self.client.get(url[url.index("/s/"):])  # swaps the token for this visit's cookie
        self.assertEqual(landing.status_code, 302)
        self.assertNotIn("admin_bar", landing["Location"])  # the token doesn't stay in the address bar
        return landing["Location"]

    def page(self, path=None, **headers):
        return self.client.get(path or f"/s/{self.store.store_slug}/", **headers).content.decode()

    def test_member_sees_bar_with_way_back(self):
        self.assertEqual(self.view_store(), f"/s/{self.store.store_slug}/")
        html = self.page()
        self.assertIn('id="tss-admin-bar"', html)
        self.assertIn(f'href="{self.back.replace("&", "&amp;")}"', html)
        self.assertIn("Back to admin", html)
        self.assertIn(f"<span>{self.store.store_name}</span>", html)
        self.assertLess(html.index('id="tss-admin-bar"'), html.index('class="site-header"'))  # above the store's header
        self.assertIn('id="tss-admin-bar"', self.page(f"/s/{self.store.store_slug}/collections/all"))  # every page

    def test_visitors_and_previews_never_see_it(self):
        self.view_store()
        self.assertNotIn("tss-admin-bar", self.page(HTTP_SEC_FETCH_DEST="iframe"))  # customizer preview

        from rest_framework.test import APIClient
        shopper = APIClient()
        self.assertNotIn("tss-admin-bar", shopper.get(f"/s/{self.store.store_slug}/").content.decode())
        forged = shopper.get(f"/s/{self.store.store_slug}/?admin_bar=not-a-real-token")
        self.assertNotIn("tss_admin_bar", forged.cookies)

    def test_only_while_still_a_member(self):
        self.view_store()
        self.store.memberships.update(status="removed")
        self.assertNotIn("tss-admin-bar", self.page())

    def test_hide_for_this_visit(self):
        self.view_store()
        r = self.client.get(f"/s/{self.store.store_slug}/collections/all?admin_bar_hide=1")
        self.assertEqual(r["Location"], f"/s/{self.store.store_slug}/collections/all")
        self.assertNotIn("tss-admin-bar", self.page())

    def test_unknown_return_page_goes_to_admin_home(self):
        from django.conf import settings

        self.view_store(return_url="https://evil.example.com/phish")
        self.assertIn(f'href="{settings.FRONTEND_URL.rstrip("/")}/admin/online-store/home"', self.page())

    def test_link_for_one_store_does_not_work_on_another(self):
        r = self.client.post("/api/v1/themes/storefront-link/", {"return_url": self.back}, format="json")
        token = r.json()["url"].split("admin_bar=")[1]
        token2, other_id = signup(self.client, "bar-other@example.com")
        from apps.tenancy.models import Store

        other = Store.objects.get(pk=other_id)
        self.client.get(f"/s/{other.store_slug}/?admin_bar={token}")
        self.assertNotIn("tss-admin-bar", self.client.get(f"/s/{other.store_slug}/").content.decode())
