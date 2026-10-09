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
        body = {"title": "Shirt", "status": "active", "price": "10", **extra}
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
        other = self.client.post(URL, {"title": "Hat", "price": "3"}, format="json").json()
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
        p = save_product(self.store, {"title": "T", "price": "5"})
        self.assertEqual(p.variants.get().price, 5)
        self.assertEqual(p.status, "draft")

    def test_delete_product_raises_when_in_use(self):
        from apps.inventory.models import Location
        p = save_product(self.store, {"title": "T", "price": "5"})
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
        self.assertEqual([c["name"] for c in channels], ["Online Store", "Point of Sale"])
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

    def test_only_name_required(self):
        r = self.client.post(URL, {"title": "Bare"}, format="json")
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

        r = self.client.post(URL, {"title": "No location", "quantity": 3}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("location", r.json())

    def test_upload_checks_type_and_size(self):
        self.assertEqual(self.upload(self.PNG).status_code, 201)
        # A text file renamed to .png is still rejected
        self.assertEqual(self.upload(b"not really an image", "fake.png").status_code, 400)
        self.assertEqual(self.upload(self.PNG + b"\x00" * (5 * 1024 * 1024), "big.png").status_code, 400)

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
