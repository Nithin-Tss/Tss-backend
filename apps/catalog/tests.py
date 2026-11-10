from django.core.cache import cache
import unittest
import re
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from apps.inventory.models import InventoryItem
from apps.core.tests.test_flow import StoreClientMixin

from apps.identity.models import User
from apps.tenancy.models import Store

from .models import (
    Brand, Category, Collection, CollectionSalesChannel, Product, ProductCollection,
    ProductSalesChannel, ProductVariant, SalesChannel,
)
from apps.media.images import clean_image
from apps.media.tests import make_image

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

    def test_bulk_status_and_delete_by_filter_reach_every_match(self):
        shirts = [self.create(title=f"Shirt {i}") for i in range(3)]
        self.create(title="Hat")
        r = self.client.post(URL + "bulk-status/", {"filter": {"status": "all", "q": "shirt"}, "status": "draft"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"updated": 3})
        self.assertEqual(Product.objects.filter(status="draft").count(), 3)
        self.assertEqual(Product.objects.get(title="Hat").status, "active")
        r = self.client.post(URL + "bulk-status/", {"filter": {"status": "draft"}, "status": "archived"}, format="json")
        self.assertEqual(r.json(), {"updated": 3})
        self.assertEqual(
            self.client.post(URL + "bulk-status/", {"filter": {"status": "bogus"}, "status": "draft"}, format="json").status_code, 400
        )
        r = self.client.post(URL + "bulk-delete/", {"filter": {"status": "archived", "q": shirts[0]["title"]}}, format="json")
        self.assertEqual(r.json(), {"deleted": 1, "blocked": []})
        r = self.client.post(URL + "bulk-delete/", {"filter": {"status": "all", "q": ""}}, format="json")
        self.assertEqual(r.json(), {"deleted": 3, "blocked": []})
        self.assertFalse(Product.objects.exists())

    def test_delete_all_removes_stocked_products(self):
        stocked, plain = self.create(title="Stocked", collections=["C"]), self.create(title="Plain")
        self.stock(ProductVariant.objects.get(pk=stocked["variants"][0]["id"]))
        self.assertEqual(self.client.post(URL + "delete-all/", {}, format="json").status_code, 400)
        r = self.client.post(URL + "delete-all/", {"confirm": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"deleted": 2, "blocked": []})
        self.assertFalse(Product.objects.exists())

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

    def test_signed_out_requests_cannot_access_variants(self):
        self.client.credentials()
        self.client.cookies.clear()
        self.assertEqual(self.client.get(VARIANTS).status_code, 401)
        response = self.client.post(VARIANTS, {
            "product": self.product["id"], "sku": "SIGNED-OUT", "price": "1",
        }, format="json")
        self.assertEqual(response.status_code, 401)

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

    def test_weight_is_saved_through_product_and_variant_endpoints(self):
        self.assertEqual(self.product["variants"][0]["weight"], "0.000")
        r = self.client.patch(f"{URL}{self.product['id']}/", {"weight": "1.250"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["variants"][0]["weight"], "1.250")
        r = self.client.patch(f"{VARIANTS}{self.first}/", {"weight": "2"}, format="json")
        self.assertEqual(r.json()["weight"], "2.000")
        r = self.client.patch(f"{VARIANTS}{self.first}/", {"weight": "-1"}, format="json")
        self.assertEqual(r.status_code, 400)

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


class CatalogResourceApiTests(StoreClientMixin, APITestCase):
    """The remaining store scoped catalog resources and their API access rules."""

    def setUp(self):
        self.token, store = self.sign_up("catalog-resources@example.com", "Catalog resources")
        self.store_id = store["storeId"]

    def product(self, title="Shirt", sku="R-1"):
        return self.ok("post", URL, {"title": title, "sku": sku, "price": "10", "vendor": "Acme"}, expect=201)

    def test_categories_create_update_list_and_reject_unknown_parent(self):
        parent = self.ok("post", "/api/v1/catalog/categories/", {"name": " Apparel "}, expect=201)
        child = self.ok("post", "/api/v1/catalog/categories/", {
            "name": " Tops ", "parent": parent["id"],
        }, expect=201)
        self.assertEqual((parent["name"], child["name"], child["parent"]), ("Apparel", "Tops", parent["id"]))
        self.assertEqual([row["id"] for row in self.ok("get", "/api/v1/catalog/categories/")],
                         [parent["id"], child["id"]])
        self.assertEqual(self.ok("patch", f"/api/v1/catalog/categories/{child['id']}/", {"name": "Tees"})["name"], "Tees")
        self.ok("post", "/api/v1/catalog/categories/", {
            "name": "Bad", "parent": "11111111-1111-1111-1111-111111111111",
        }, expect=400)

    def test_collections_crud_products_and_preview(self):
        item = self.product()
        created = self.ok("post", "/api/v1/catalog/collections/", {
            "name": " Summer ", "products": [item["id"]],
        }, expect=201)
        self.assertEqual(created["name"], "Summer")
        self.assertEqual(created["products_count"], 1)
        self.assertEqual(self.ok("get", f"/api/v1/catalog/collections/{created['id']}/")["products_count"], 1)
        self.assertEqual(self.ok("patch", f"/api/v1/catalog/collections/{created['id']}/", {"description": "Seasonal"})["description"], "Seasonal")
        preview = self.ok("post", "/api/v1/catalog/collections/preview/", {
            "rules": [{"field": "vendor", "operator": "equals", "value": "Acme"}],
        })
        self.assertEqual(preview["count"], 1)
        self.assertEqual(preview["products"][0]["id"], item["id"])
        self.ok("post", "/api/v1/catalog/collections/", {"name": "Bad", "products": ["11111111-1111-1111-1111-111111111111"]}, expect=400)
        self.assertEqual(self.ok("delete", f"/api/v1/catalog/collections/{created['id']}/", expect=204), None)

    def test_catalog_resources_are_store_isolated_and_members_need_permissions(self):
        product = self.product("Permission target", "PERM-1")
        category = self.ok("post", "/api/v1/catalog/categories/", {"name": "Tops"}, expect=201)
        collection = self.ok("post", "/api/v1/catalog/collections/", {"name": "Featured"}, expect=201)
        self.use(self.token, self.store_id)
        role = self.ok("post", "/api/v1/roles/", {"name": "No catalog", "permissions": []}, expect=201)
        member_token, _ = self.sign_up("catalog-member@example.com", "Member own shop")
        self.use(self.token, self.store_id)
        self.ok("post", "/api/v1/members/", {"email": "catalog-member@example.com", "roleIds": [role["id"]]}, expect=201)
        self.use(member_token, self.store_id)
        for method, path, body in (
            ("get", URL, None),
            ("post", URL, {"title": "Denied", "sku": "DENIED", "price": "1"}),
            ("get", VARIANTS, None),
            ("post", VARIANTS, {"product": product["id"], "sku": "DENIED-V", "price": "1"}),
            ("get", "/api/v1/catalog/categories/", None),
            ("post", "/api/v1/catalog/categories/", {"name": "Denied"}),
            ("get", "/api/v1/catalog/collections/", None),
            ("post", "/api/v1/catalog/collections/", {"name": "Denied"}),
            ("get", "/api/v1/catalog/sales-channels/", None),
            ("post", URL + "bulk/", {"products": [{"title": "Denied", "sku": "DENIED-B"}]}),
        ):
            self.ok(method, path, body, expect=403)
        self.use(self.token, self.store_id)
        _, second = self.sign_up("catalog-second@example.com", "Second shop")
        self.assertEqual(self.ok("get", "/api/v1/catalog/categories/"), [])
        self.assertEqual(self.ok("get", "/api/v1/catalog/collections/"), [])
        self.ok("get", f"/api/v1/catalog/categories/{category['id']}/", expect=404)
        self.ok("patch", f"/api/v1/catalog/collections/{collection['id']}/", {"name": "Changed"}, expect=404)
        self.use(self.token, self.store_id)
        self.client.credentials()
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/v1/catalog/categories/").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/catalog/collections/").status_code, 401)
        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertNotEqual(second["storeId"], self.store_id)

    def test_product_bulk_actions_reject_signed_out_and_wrong_role_requests(self):
        product = self.product("Bulk permission target", "BULK-PERM-1")
        bulk_actions = (
            ("bulk-status/", {"ids": [product["id"]], "status": "active"}),
            ("bulk-delete/", {"ids": [product["id"]]}),
            ("delete-all/", {"confirm": True}),
        )

        self.client.credentials()
        self.client.cookies.clear()
        for action, body in bulk_actions:
            response = self.client.post(URL + action, body, format="json")
            self.assertEqual(response.status_code, 401, action)

        self.use(self.token, self.store_id)
        role = self.ok("post", "/api/v1/roles/", {"name": "No bulk permissions", "permissions": []}, expect=201)
        member_token, _ = self.sign_up("catalog-bulk-member@example.com", "Bulk member shop")
        self.use(self.token, self.store_id)
        self.ok("post", "/api/v1/members/", {
            "email": "catalog-bulk-member@example.com", "roleIds": [role["id"]],
        }, expect=201)
        self.use(member_token, self.store_id)
        for action, body in bulk_actions:
            self.ok("post", URL + action, body, expect=403)

    def test_products_validate_price_sku_and_bulk_limit_with_messages(self):
        missing = self.client.post(URL, {"price": "10", "sku": "NO-TITLE"}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("title", str(missing.json()).lower())
        negative = self.client.post(URL, {"title": "Negative", "sku": "NEG", "price": "-1"}, format="json")
        self.assertEqual(negative.status_code, 400)
        self.assertIn("price", str(negative.json()).lower())
        self.product(title="First", sku="DUP")
        duplicate = self.client.post(URL, {"title": "Second", "sku": "DUP", "price": "1"}, format="json")
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("sku", str(duplicate.json()).lower())
        too_many = self.client.post(URL + "bulk/", {"products": [{"title": "T"}] * 501}, format="json")
        self.assertEqual(too_many.status_code, 400)
        self.assertIn("500", str(too_many.json()))

    def test_product_list_supports_filters_and_pagination(self):
        self.product("Alpha", "P-A")
        self.product("Beta", "P-B")
        self.product("Gamma", "P-C")
        response = self.client.get(URL + "?search=Beta")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any(row["title"] == "Beta" for row in response.json()))
        paged = self.client.get(URL + "?page=1&page_size=1")
        self.assertEqual(paged.status_code, 200)


class CatalogStockDeletionIntegrationTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.sign_up(f"catalog-integration-{self._testMethodName}@example.com", "Catalog Integration")
        self.location = self.ok(
            "post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201
        )["id"]

    def create_product(self, title="Integration product", sku="INT-1", quantity=10):
        return self.ok("post", URL, {
            "title": title, "sku": sku, "price": "10", "location": self.location,
            "quantity": quantity,
        }, expect=201)

    def history(self, variant_id):
        return self.ok("get", f"/api/v1/inventory/items/history/?variant={variant_id}")

    def test_06_editing_same_quantity_does_not_add_history(self):
        product = self.create_product()
        variant_id = product["variants"][0]["id"]
        before_history = self.history(variant_id)

        response = self.client.patch(
            f"{URL}{product['id']}/", {"quantity": 10, "location": self.location}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(self.history(variant_id)), len(before_history))
        stock = self.ok("get", f"/api/v1/inventory/items/?variant={variant_id}")
        self.assertEqual(sum(row["available"] for row in stock), 10)

    def test_07_price_and_title_edit_preserves_stock_and_history(self):
        product = self.create_product()
        variant_id = product["variants"][0]["id"]
        order = self.ok("post", "/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 3}],
        }, expect=201)
        before_history = self.history(variant_id)
        before_stock = self.ok("get", f"/api/v1/inventory/items/?variant={variant_id}")
        self.assertEqual(len(before_stock), 1)
        before = {key: before_stock[0][key] for key in ("available", "committed", "on_hand")}
        self.assertEqual((before["available"], before["committed"], before["on_hand"]), (7, 3, 10))

        response = self.client.patch(
            f"{URL}{product['id']}/", {"title": "Renamed integration product", "price": "12"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        after_stock = self.ok("get", f"/api/v1/inventory/items/?variant={variant_id}")
        self.assertEqual(
            {key: after_stock[0][key] for key in ("available", "committed", "on_hand")}, before
        )
        self.assertEqual(len(self.history(variant_id)), len(before_history))
        self.assertEqual(self.ok("get", f"/api/v1/orders/{order['id']}/")["items"][0]["product"], product["id"])

    def test_14_product_with_order_item_cannot_be_deleted_or_break_order(self):
        product = self.create_product(quantity=None)
        order = self.ok("post", "/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 1}],
        }, expect=201)

        response = self.client.delete(f"{URL}{product['id']}/")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json(), [
            "This product is used by orders or inventory. Set it to Draft instead."
        ])
        self.assertTrue(Product.objects.filter(pk=product["id"]).exists())
        retrieved = self.ok("get", f"/api/v1/orders/{order['id']}/")
        self.assertEqual(len(retrieved["items"]), 1)
        self.assertEqual(retrieved["items"][0]["product"], product["id"])

    def test_15_variants_with_stock_or_order_item_cannot_be_deleted(self):
        product = self.create_product(title="Variant stock", sku="VAR-BASE", quantity=None)
        stocked = self.ok("post", "/api/v1/catalog/variants/", {
            "product": product["id"], "sku": "VAR-STOCK", "price": "10",
        }, expect=201)
        stock = self.ok("post", "/api/v1/inventory/items/set/", {
            "variant": stocked["id"], "location": self.location, "available": 4,
        })
        stock_delete = self.client.delete(f"/api/v1/catalog/variants/{stocked['id']}/")
        self.assertEqual(stock_delete.status_code, 400, stock_delete.content)
        self.assertEqual(stock_delete.json(), [
            "This variant is used by orders or inventory, so it can't be deleted."
        ])
        self.assertEqual(self.ok("get", f"/api/v1/inventory/items/{stock['id']}/")["available"], 4)
        self.assertEqual(self.ok("get", f"/api/v1/catalog/variants/{stocked['id']}/")["id"], stocked["id"])

        ordered_product = self.create_product(title="Variant order", sku="ORD-BASE", quantity=None)
        ordered_variant = self.ok("post", "/api/v1/catalog/variants/", {
            "product": ordered_product["id"], "sku": "ORD-ITEM", "price": "10",
        }, expect=201)
        order = self.ok("post", "/api/v1/orders/", {
            "items": [{"product": ordered_product["id"], "variant": ordered_variant["id"], "quantity": 1}],
        }, expect=201)
        order_delete = self.client.delete(f"/api/v1/catalog/variants/{ordered_variant['id']}/")
        self.assertEqual(order_delete.status_code, 400, order_delete.content)
        self.assertEqual(order_delete.json(), [
            "This variant is used by orders or inventory, so it can't be deleted."
        ])
        self.assertEqual(self.ok("get", f"/api/v1/catalog/variants/{ordered_variant['id']}/")["id"], ordered_variant["id"])
        loaded_order = self.ok("get", f"/api/v1/orders/{order['id']}/")
        self.assertEqual(loaded_order["items"][0]["variant"], ordered_variant["id"])


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

    PNG = make_image("PNG")
    CLEAN_PNG = clean_image(PNG)[0]  # what is kept after an upload

    def setUp(self):
        token, self.store_id = signup(self.client, "form@example.com")
        self.token = token
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)

    def upload(self, content, name="photo.png"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            f"{URL}images/", {"image": SimpleUploadedFile(name, content)}, format="multipart"
        )

    def test_uploaded_photo_lives_in_the_database_and_is_served(self):
        from urllib.parse import urlsplit

        from apps.media.models import MediaAsset
        from apps.themes.models import ThemeImage

        url = self.upload(self.PNG).json()["url"]
        stored = ThemeImage.objects.get()
        self.assertEqual((str(stored.store_id), stored.content_type), (self.store_id, "image/png"))
        self.assertEqual(bytes(stored.data), self.CLEAN_PNG)
        self.assertFalse(MediaAsset.objects.exists())  # product photos are not media assets

        public = APIClient()  # shoppers aren't signed in
        r = public.get(urlsplit(url).path)
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        self.assertEqual(r.content, self.CLEAN_PNG)

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

        self.assertIn('<h1 class="section-heading collection-heading">All products</h1>', self.page("collections/all"))
        self.assertEqual(order(), ["Cheap", "Dear", "Middle"])  # default: alphabetical, A-Z
        self.assertEqual(order("?sort=title-desc"), ["Middle", "Dear", "Cheap"])
        self.assertEqual(order("?sort=newest"), ["Middle", "Dear", "Cheap"])  # "Featured": the catalog's own order
        self.assertEqual(order("?sort=price-asc"), ["Cheap", "Middle", "Dear"])
        self.assertEqual(order("?sort=price-desc"), ["Dear", "Middle", "Cheap"])

        for i in range(24):
            self.product(f"Bulk {i:02d}")
        html = self.page("collections/all?sort=newest&page=2")
        self.assertIn('aria-current="page">2<', html)  # numbered pages, 20 to a page by default
        self.assertIn(">Cheap<", html)  # 27 products: the oldest 7 are on page 2
        self.assertNotIn("Bulk 23", html)  # the newest are on page 1
        self.assertIn("?sort=newest&amp;page=1", html)  # page links keep the sort

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
            {"title": "Imported active", "status": "active", "sku": "IMP-A", "quantity": 4, "price": "12.50"},
            {"title": "Imported draft", "status": "draft", "sku": "IMP-D"},
        ]}, format="json")
        self.assertEqual(r.json()["created"], 2, r.content)

        active = Product.objects.get(title="Imported active")
        draft = Product.objects.get(title="Imported draft")
        self.assertEqual(list(active.product_sales_channels.values_list("sales_channel__type", flat=True)), ["online"])
        # Every import is on the Online Store; the Draft one stays hidden until it is made Active
        self.assertEqual(list(draft.product_sales_channels.values_list("sales_channel__type", flat=True)), ["online"])
        self.assertEqual(str(active.variants.get().price), "12.50")
        self.assertEqual(str(draft.variants.get().price), "0.00")  # a Draft may still be imported without a price
        self.assertEqual(InventoryItem.objects.get(variant__product=active).available, 4)
        self.assertEqual(InventoryItem.objects.get(variant__product=draft).available, 0)
        listing = self.page("collections/all")
        self.assertIn("Imported active", listing)
        self.assertNotIn("Imported draft", listing)

    def test_reimporting_a_sheet_updates_quantities_of_existing_skus(self):
        self.client.post(f"{URL}bulk/", {"products": [
            {"title": "Restock me", "sku": "RS-1", "quantity": 4},
            {"title": "Leave me", "sku": "RS-2", "quantity": 7},
        ]}, format="json")
        r = self.client.post(f"{URL}bulk/", {"products": [
            {"title": "Restock me", "sku": "RS-1", "quantity": 25},
            {"title": "Leave me", "sku": "RS-2"},
        ]}, format="json")
        data = r.json()
        self.assertEqual((data["created"], data["skipped"], data["stockUpdated"]), (0, 2, 1), r.content)
        self.assertEqual(InventoryItem.objects.get(variant__sku="RS-1").available, 25)
        self.assertEqual(InventoryItem.objects.get(variant__sku="RS-2").available, 7)

    def test_reimport_updates_stock_where_the_variant_already_is(self):
        from apps.inventory.models import Location

        self.client.post(f"{URL}bulk/", {"products": [{"title": "Elsewhere", "sku": "EL-1", "quantity": 3}]}, format="json")
        item = InventoryItem.objects.get(variant__sku="EL-1")
        other = Location.objects.create(store=item.variant.product.store, name="Warehouse")
        item.location = other
        item.save()
        self.client.post(f"{URL}bulk/", {"products": [{"title": "Elsewhere", "sku": "EL-1", "quantity": 9}]}, format="json")
        self.assertEqual(InventoryItem.objects.filter(variant__sku="EL-1").count(), 1)
        self.assertEqual(InventoryItem.objects.get(variant__sku="EL-1").available, 9)

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
        # No stock record = not tracked: stock never limits it, so it is not sold out (like Shopify)
        self.assertNotIn("Sold out", self.card(self.html("collections/all"), "Never stocked"))
        page = self.html(f"products/{untracked['id']}")
        self.assertNotRegex(page, r"data-add[^>]*\sdisabled")
        self.assertRegex(page, r"data-add[^>]*>\s*Add to cart")

    def set_low_stock_setting(self, value):
        """The product page's "Only a few left at or below" setting, as the store owner sets it in the customizer."""
        data = self.client.get("/api/v1/themes/settings/").json()
        sections = data["templates"]["product"]["sections"]
        main = next(s for s in sections.values() if s["type"] == "main-product")
        main["settings"]["low_stock_threshold"] = value
        r = self.client.patch("/api/v1/themes/settings/", {"templates": {"product": {
            "order": data["templates"]["product"]["order"], "sections": sections}}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_cards_use_the_same_few_left_setting_as_the_product_page(self):
        from apps.themes import rendering

        badge = 'product-card__badge">Few left</span>'
        eight = self.product("Eight left", "SO-8", 8)

        # Default 5: eight is plenty, on the card and on the page
        self.assertNotIn(badge, self.card(self.html("collections/all"), "Eight left"))
        self.assertRegex(self.html(f"products/{eight['id']}"), r"data-stock[^>]*>\s*In stock")

        # The owner raises it to 10: the card and the page both say so
        self.set_low_stock_setting(10)
        self.assertIn(badge, self.card(self.html("collections/all"), "Eight left"))
        self.assertRegex(self.html(f"products/{eight['id']}"), r"data-stock[^>]*>\s*Only a few left")

        # Lowered to 3: four is not "few" anywhere, three is
        self.set_low_stock_setting(3)
        four, three = self.product("Four left", "SO-4", 4), self.product("Three left", "SO-3B", 3)
        listing = self.html("collections/all")
        self.assertNotIn(badge, self.card(listing, "Four left"))
        self.assertIn(badge, self.card(listing, "Three left"))
        self.assertRegex(self.html(f"products/{four['id']}"), r"data-stock[^>]*>\s*In stock")
        self.assertRegex(self.html(f"products/{three['id']}"), r"data-stock[^>]*>\s*Only a few left")
        self.assertEqual(rendering._few_left_at.get(), rendering.FEW_LEFT)  # the setting does not leak into the next page

    def test_an_option_that_keeps_selling_stays_orderable_on_the_product_page(self):
        from apps.catalog.models import ProductVariant
        from apps.inventory.models import Location
        from apps.inventory.services import set_available

        sold = self.product("Made to order", "SO-MTO", 0)
        ProductVariant.objects.filter(product_id=sold["id"]).update(continue_selling=True)
        # A second option (one that stops at its stock) so the page has the option picker the script works on
        second = ProductVariant.objects.create(product_id=sold["id"], sku="SO-MTO-2", price="10.00")
        set_available(second, Location.objects.get(pk=self.location["id"]), 0)  # tracked, so it does stop at its stock
        plain = self.product("Plain", "SO-PLAIN", 3)

        page = self.html(f"products/{sold['id']}")
        self.assertNotRegex(page, r"data-add[^>]*\sdisabled")  # the button is on
        self.assertRegex(page, r"data-stock[^>]*>\s*In stock")  # not "Only a few left" at 0 stock
        self.assertIn('data-variant-continue="true"', page)  # so the script knows when an option is switched
        self.assertIn('data-variant-continue="false"', page)
        self.assertIn('variantContinue !== "true"', page)
        self.assertNotIn("Sold out", self.card(self.html("collections/all"), "Made to order"))

        page = self.html(f"products/{plain['id']}")
        self.assertRegex(page, r"data-stock[^>]*>\s*Only a few left")  # real low stock still says so

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


class CatalogAdminStoreGuardTests(TestCase):
    """Django Admin: platform admins only, and no record crosses from one store to another."""

    def setUp(self):
        self.a = Store.objects.create(store_name="Alpha Shop", store_slug="alpha", status="active")
        self.b = Store.objects.create(store_name="Beta Shop", store_slug="beta", status="active")
        self.root = User.objects.create_superuser(email="root@example.com", password="Root-pass-123",
                                                  first_name="R", last_name="T")
        self.client.force_login(self.root)

    def collection_form(self, name, handle, **extra):
        return {"name": name, "description": "", "image_url": "", "handle": handle,
                "type": "manual", "status": "active", "match": "all", "excluded_products": "[]",
                "conditions": "[]", "theme_template": "Default collection", "seo_title": "",
                "seo_description": "", **extra}

    def errors(self, response):
        return response.context["adminform"].form.errors

    def test_only_platform_admins_enter_admin(self):
        self.client.logout()
        User.objects.create_user(email="plain@example.com", password="Plain-pass-123", first_name="P", last_name="L")
        self.assertTrue(self.client.login(username="plain@example.com", password="Plain-pass-123"))
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get("/admin/catalog/collection/").status_code, 302)

        self.client.logout()
        signup(APIClient(), "owner@example.com")  # signs up and opens a store, as its owner
        owner = User.objects.get(email="owner@example.com")
        self.assertFalse(owner.is_staff)
        self.client.force_login(owner)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(self.client.get("/admin/catalog/collection/").status_code, 302)
        r = self.client.post("/admin/catalog/collection/add/", self.collection_form("x", "x", store=self.a.pk))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Collection.objects.filter(name="x").exists())

    def test_superadmin_sees_collections_of_every_store_with_store_name_and_filter(self):
        Collection.objects.create(store=self.a, name="promotions")
        Collection.objects.create(store=self.b, name="promotions")
        r = self.client.get("/admin/catalog/collection/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["cl"].result_count, 2)
        self.assertContains(r, "Alpha Shop")
        self.assertContains(r, "Beta Shop")
        self.assertContains(r, f"?store__store_id__exact={self.a.pk}")
        r = self.client.get(f"/admin/catalog/collection/?store__store_id__exact={self.b.pk}")
        self.assertEqual(r.context["cl"].result_count, 1)
        self.assertEqual(r.context["cl"].result_list[0].store_id, self.b.pk)

    def test_same_collection_name_in_two_stores(self):
        for store in (self.a, self.b):
            r = self.client.post("/admin/catalog/collection/add/",
                                 self.collection_form("promotions", "promotions", store=store.pk))
            self.assertEqual(r.status_code, 302)
        self.assertEqual(set(Collection.objects.filter(name="promotions").values_list("store__store_name", flat=True)),
                         {"Alpha Shop", "Beta Shop"})

    def test_collection_store_cannot_be_changed(self):
        c = Collection.objects.create(store=self.a, name="promotions")
        url = f"/admin/catalog/collection/{c.pk}/change/"
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, 'name="store"')
        self.assertContains(r, "Alpha Shop")
        r = self.client.post(url, self.collection_form("Renamed", c.handle, store=self.b.pk))  # crafted POST
        self.assertEqual(r.status_code, 302)
        c.refresh_from_db()
        self.assertEqual((c.name, c.store_id), ("Renamed", self.a.pk))

    def product_form(self, store, **extra):
        return {"store": store.pk, "title": "Shirt", "status": "active", **extra}

    def test_product_cannot_use_another_stores_category_or_brand(self):
        cat_b = Category.objects.create(store=self.b, name="Tops")
        brand_b = Brand.objects.create(store=self.b, name="Acme")
        r = self.client.post("/admin/catalog/product/add/", self.product_form(self.a, category=cat_b.pk))
        self.assertEqual(r.status_code, 200)
        self.assertIn("category", self.errors(r))
        r = self.client.post("/admin/catalog/product/add/", self.product_form(self.a, brand=brand_b.pk))
        self.assertIn("brand", self.errors(r))
        self.assertFalse(Product.objects.exists())

        # The same store is fine
        r = self.client.post("/admin/catalog/product/add/",
                             self.product_form(self.b, category=cat_b.pk, brand=brand_b.pk))
        self.assertEqual(r.status_code, 302)

        # An existing product can't be pointed at store B's category, or moved to store B
        p = Product.objects.create(store=self.a, title="Hat", status="active")
        url = f"/admin/catalog/product/{p.pk}/change/"
        r = self.client.post(url, {"title": "Hat", "status": "active", "category": cat_b.pk})
        self.assertIn("category", self.errors(r))
        r = self.client.post(url, {"title": "Hat", "status": "active", "store": self.b.pk})
        self.assertEqual(r.status_code, 302)
        p.refresh_from_db()
        self.assertEqual((p.store_id, p.category_id), (self.a.pk, None))

    def test_category_parent_must_be_same_store(self):
        parent_b = Category.objects.create(store=self.b, name="Clothes")
        r = self.client.post("/admin/catalog/category/add/",
                             {"store": self.a.pk, "name": "Tops", "parent": parent_b.pk})
        self.assertIn("parent", self.errors(r))

    def test_product_collection_must_be_one_store(self):
        p = Product.objects.create(store=self.a, title="Hat", status="active")
        c_b = Collection.objects.create(store=self.b, name="promotions")
        c_a = Collection.objects.create(store=self.a, name="promotions")
        r = self.client.post("/admin/catalog/productcollection/add/", {"product": p.pk, "collection": c_b.pk})
        self.assertEqual(r.status_code, 200)
        self.assertIn("collection", self.errors(r))
        self.assertFalse(ProductCollection.objects.exists())
        r = self.client.post("/admin/catalog/productcollection/add/", {"product": p.pk, "collection": c_a.pk})
        self.assertEqual(r.status_code, 302)
        link = ProductCollection.objects.get()
        r = self.client.post(f"/admin/catalog/productcollection/{link.pk}/change/",
                             {"product": p.pk, "collection": c_b.pk})
        self.assertIn("collection", self.errors(r))
        link.refresh_from_db()
        self.assertEqual(link.collection_id, c_a.pk)

    def test_sales_channel_links_must_be_one_store(self):
        p = Product.objects.create(store=self.a, title="Hat", status="active")
        c = Collection.objects.create(store=self.a, name="promotions")
        ch_b = SalesChannel.objects.create(store=self.b, name="Online", type="online")
        ch_a = SalesChannel.objects.create(store=self.a, name="Online", type="online")
        r = self.client.post("/admin/catalog/productsaleschannel/add/", {"product": p.pk, "sales_channel": ch_b.pk})
        self.assertIn("sales_channel", self.errors(r))
        r = self.client.post("/admin/catalog/collectionsaleschannel/add/",
                             {"collection": c.pk, "sales_channel": ch_b.pk})
        self.assertIn("sales_channel", self.errors(r))
        self.assertFalse(ProductSalesChannel.objects.exists() or CollectionSalesChannel.objects.exists())
        r = self.client.post("/admin/catalog/productsaleschannel/add/", {"product": p.pk, "sales_channel": ch_a.pk})
        self.assertEqual(r.status_code, 302)
        r = self.client.post("/admin/catalog/collectionsaleschannel/add/",
                             {"collection": c.pk, "sales_channel": ch_a.pk})
        self.assertEqual(r.status_code, 302)

    def test_sales_channel_store_cannot_be_changed(self):
        ch = SalesChannel.objects.create(store=self.a, name="Online", type="online")
        r = self.client.post(f"/admin/catalog/saleschannel/{ch.pk}/change/",
                             {"name": "Online", "type": "online", "store": self.b.pk})
        self.assertEqual(r.status_code, 302)
        ch.refresh_from_db()
        self.assertEqual(ch.store_id, self.a.pk)

    def test_variant_cannot_move_to_another_stores_product(self):
        p_a = Product.objects.create(store=self.a, title="Hat", status="active")
        p_b = Product.objects.create(store=self.b, title="Cap", status="active")
        v = ProductVariant.objects.create(product=p_a, price="5")
        r = self.client.post(f"/admin/catalog/productvariant/{v.pk}/change/",
                             {"product": p_b.pk, "price": "6", "weight": "0", "sku": ""})
        self.assertEqual(r.status_code, 302)
        v.refresh_from_db()
        self.assertEqual((v.product_id, str(v.price)), (p_a.pk, "6.00"))


class CurrencySymbolTests(APITestCase):
    """Prices are shown with the symbol of the store's currency; the theme's own symbol only when the owner set one."""

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "symbol@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.slug = Store.objects.get(pk=self.store_id).store_slug
        r = self.client.post(URL, {"title": "Vase", "status": "active", "price": "10", "sku": "SYM-1"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.product_id = r.json()["id"]

    def html(self, path):
        return self.client.get(f"/s/{self.slug}/{path}").content.decode()

    def set_currency(self, code):
        from apps.tenancy.models import Store

        Store.objects.filter(pk=self.store_id).update(currency=code)

    def set_theme_symbol(self, symbol):
        r = self.client.patch("/api/v1/themes/settings/", {"settings": {"currency_symbol": symbol}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def shown(self):
        """The Vase's price on its card and on its own page."""
        import re

        return (
            re.search(r'<span class="price">([^<]*10\.00)</span>', self.html("collections/all")).group(1),
            re.search(r'data-price>.*?<span class="price">([^<]*10\.00)</span>', self.html(f"products/{self.product_id}"), re.S).group(1),
        )

    def test_an_existing_inr_store_looks_exactly_the_same(self):
        self.assertEqual(self.shown(), ("₹10.00", "₹10.00"))

    def test_the_symbol_follows_the_stores_currency(self):
        for code, symbol in (("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("INR", "₹"), ("CHF", "CHF ")):
            with self.subTest(code=code):
                self.set_currency(code)
                self.assertEqual(self.shown(), (f"{symbol}10.00", f"{symbol}10.00"))

    def test_a_symbol_the_owner_set_in_the_theme_wins(self):
        self.set_theme_symbol("Rs.")
        self.assertEqual(self.shown(), ("Rs.10.00", "Rs.10.00"))  # an INR store
        self.set_currency("USD")
        self.assertEqual(self.shown(), ("Rs.10.00", "Rs.10.00"))  # the owner's choice stays
        self.set_theme_symbol("$")
        self.set_currency("INR")
        self.assertEqual(self.shown(), ("$10.00", "$10.00"))

    def test_the_default_the_code_compares_with_is_the_themes_default(self):
        from apps.themes.rendering import DEFAULT_CURRENCY_SYMBOL, currency_symbol
        from apps.themes.schemas import THEME_SETTINGS

        self.assertEqual(DEFAULT_CURRENCY_SYMBOL, THEME_SETTINGS["currency_symbol"]["default"])
        store = type("S", (), {"currency": "usd"})()  # the code is not case sensitive
        self.assertEqual(currency_symbol(store, DEFAULT_CURRENCY_SYMBOL), "$")
        self.assertEqual(currency_symbol(store, ""), "$")
        self.assertEqual(currency_symbol(type("S", (), {"currency": ""})(), DEFAULT_CURRENCY_SYMBOL), DEFAULT_CURRENCY_SYMBOL)


class ProductPriceRequiredTests(APITestCase):
    """A product that is for sale (Active) has a price on every option; a typed 0 is a price, an empty one is not."""

    MESSAGE = "Enter a price for every option before making this product active."

    def setUp(self):
        token, self.store_id = signup(self.client, "pricerule@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.count = 0

    def create(self, **body):
        self.count += 1
        data = {"title": f"Lamp {self.count}", "sku": f"LAMP-{self.count}", "status": "active", **body}
        return self.client.post(URL, data, format="json")

    def prices(self, product_id):
        from .models import ProductVariant

        return [str(p) for p in ProductVariant.objects.filter(product_id=product_id).order_by("created_at").values_list("price", flat=True)]

    def refused(self, response):
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(self.MESSAGE, str(response.json()))

    def test_creating_an_active_product_needs_a_price_but_a_typed_zero_is_one(self):
        from .models import Product

        self.refused(self.create(price=""))
        self.refused(self.create(price=None))
        self.refused(self.create())  # no price at all
        self.assertEqual(Product.objects.count(), 0)  # nothing half-saved

        for typed in ("0", 0, "0.00"):
            r = self.create(price=typed)
            self.assertEqual(r.status_code, 201, r.content)
            self.assertEqual(self.prices(r.json()["id"]), ["0.00"])
        r = self.create(price="10", variants=[{"sku": "FREE-1", "price": "0"}])  # a typed 0 on an option stays 0
        self.assertEqual((r.status_code, self.prices(r.json()["id"])), (201, ["0.00"]), r.content)

        r = self.create(status="draft", price="")  # a Draft may keep an empty price
        self.assertEqual((r.status_code, self.prices(r.json()["id"])), (201, ["0.00"]))
        self.assertEqual(self.create(status="draft").status_code, 201)

    def test_making_a_draft_active_checks_the_price(self):
        from apps.catalog.services import matching_products
        from apps.tenancy.models import Store

        draft = self.create(status="draft", price="").json()
        r = self.client.patch(f"{URL}{draft['id']}/", {"status": "active"}, format="json")  # nothing says it is a typed 0
        self.refused(r)
        self.refused(self.client.patch(f"{URL}{draft['id']}/", {"status": "active", "price": ""}, format="json"))
        self.assertEqual(self.client.get(f"{URL}{draft['id']}/").json()["status"], "draft")

        # Staff find products still at 0.00 with an automated collection: Price is equal to 0
        store = Store.objects.get(pk=self.store_id)
        listed = matching_products(store, [{"field": "price", "operator": "equals", "value": "0"}])
        self.assertIn(draft["id"], {str(pk) for pk in listed.values_list("pk", flat=True)})

        free = self.client.patch(f"{URL}{draft['id']}/", {"status": "active", "price": "0"}, format="json")
        self.assertEqual((free.status_code, free.json()["status"]), (200, "active"), free.content)
        other = self.create(status="draft", price="").json()
        paid = self.client.patch(f"{URL}{other['id']}/", {"status": "active", "price": "25"}, format="json")
        self.assertEqual((paid.status_code, self.prices(other["id"])), (200, ["25.00"]))

    def test_an_active_products_price_cannot_be_cleared_and_other_edits_keep_it(self):
        from .models import Product, ProductVariant

        product = self.create(price="30").json()
        self.refused(self.client.patch(f"{URL}{product['id']}/", {"price": ""}, format="json"))
        self.assertEqual(self.prices(product["id"]), ["30.00"])  # rolled back

        for change in ({"title": "Renamed"}, {"weight": "2"}, {"vendor": "Acme"}):
            r = self.client.patch(f"{URL}{product['id']}/", change, format="json")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(self.prices(product["id"]), ["30.00"], change)  # not zeroed by an edit of something else

        # A product already for sale at 0.00 is not touched: renaming it still works and leaves its price alone
        old = self.create(price="5").json()
        ProductVariant.objects.filter(product_id=old["id"]).update(price="0.00")
        r = self.client.patch(f"{URL}{old['id']}/", {"title": "Old free thing"}, format="json")
        self.assertEqual((r.status_code, self.prices(old["id"])), (200, ["0.00"]), r.content)
        self.assertEqual(Product.objects.get(pk=old["id"]).status, "active")

    def test_every_option_needs_its_own_price(self):
        rows = [{"sku": "OPT-A", "price": "10"}, {"sku": "OPT-B", "price": ""}]
        self.refused(self.create(variants=rows))
        rows[1]["price"] = "0"
        r = self.create(variants=rows)
        self.assertEqual((r.status_code, self.prices(r.json()["id"])), (201, ["10.00", "0.00"]), r.content)

        product = self.create(price="10").json()
        new_option = {"sku": "OPT-NEW"}  # an option added with no price
        existing = self.client.get(f"{URL}{product['id']}/").json()["variants"][0]
        self.refused(self.client.patch(
            f"{URL}{product['id']}/", {"variants": [{"id": existing["id"]}, new_option]}, format="json"))

    def test_the_bulk_status_change_refuses_products_priced_0_00(self):
        from .models import Product

        unpriced = self.create(status="draft", price="").json()
        priced = self.create(status="draft", price="12").json()

        r = self.client.post(f"{URL}bulk-status/", {"ids": [unpriced["id"], priced["id"]], "status": "active"}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertIn("Enter a price for every option before making them active.", str(r.json()))
        self.assertEqual(set(Product.objects.values_list("status", flat=True)), {"draft"})  # all or nothing

        r = self.client.post(f"{URL}bulk-status/", {"ids": [priced["id"]], "status": "active"}, format="json")
        self.assertEqual((r.status_code, r.json()["updated"]), (200, 1))
        r = self.client.post(f"{URL}bulk-status/", {"ids": [priced["id"], unpriced["id"]], "status": "draft"}, format="json")
        self.assertEqual(r.status_code, 200)  # taking products off sale is not checked

    def test_an_import_row_with_no_price_for_an_active_product_is_refused_with_its_message(self):
        from .models import Product, ProductVariant

        r = self.client.post(f"{URL}bulk/", {"products": [
            {"title": "Priced", "status": "active", "sku": "IMP-1", "price": "9.50"},
            {"title": "No price", "status": "active", "sku": "IMP-2"},
            {"title": "Empty price", "status": "active", "sku": "IMP-3", "price": ""},
            {"title": "Free", "status": "active", "sku": "IMP-4", "price": "0"},
            {"title": "Draft no price", "status": "draft", "sku": "IMP-5"},
            {"title": "No status no price", "sku": "IMP-6"},
        ]}, format="json")

        body = r.json()
        self.assertEqual(body["created"], 4, r.content)
        self.assertEqual([e["index"] for e in body["errors"]], [1, 2])
        for error in body["errors"]:
            self.assertEqual(error["message"], f"price: {self.MESSAGE}")
        self.assertFalse(Product.objects.filter(title__in=["No price", "Empty price"]).exists())
        self.assertEqual(
            {v.sku: str(v.price) for v in ProductVariant.objects.all()},
            {"IMP-1": "9.50", "IMP-4": "0.00", "IMP-5": "0.00", "IMP-6": "0.00"},
        )

        # A sheet that is only re-uploaded to update stock does not need a price
        r = self.client.post(f"{URL}bulk/", {"products": [
            {"title": "Priced", "status": "active", "sku": "IMP-1", "quantity": 3},
        ]}, format="json")
        self.assertEqual(r.json()["errors"], [], r.content)


class CatalogNavigationTests(APITestCase):
    """Collections, categories, menus, redirects and the header: they follow the catalog as it changes."""

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "navigation@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        self.slug = self.store.store_slug
        self.channels = {c["type"]: c["id"] for c in self.client.get("/api/v1/catalog/sales-channels/").json()}
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()
        self.n = 0

    # ------------------------------------------------------------------ helpers
    def product(self, title, **extra):
        self.n += 1
        body = {"title": title, "status": "active", "price": "10", "sku": f"NAV-{self.n}",
                "location": self.location["id"], "quantity": 5, **extra}
        r = self.client.post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def collection(self, name, products=(), **extra):
        r = self.client.post("/api/v1/catalog/collections/", {"name": name, "products": list(products), **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def edit_collection(self, collection, **body):
        r = self.client.patch(f"/api/v1/catalog/collections/{collection['id']}/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def get(self, path):
        return self.client.get(f"/s/{self.slug}/{path}")

    def header(self):
        import re

        return re.search(r"<header.*?</header>", self.get("").content.decode(), re.S).group(0)

    def make_menu(self, items, name="Main menu"):
        r = self.client.post("/api/v1/themes/menus/", {"name": name, "items": items}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def redirect(self, path, target, store=None):
        from apps.themes.models import UrlRedirect

        return UrlRedirect.objects.create(store=store or self.store, path=path, target=target)

    # ------------------------------------------------------------------ problem 3: a collection's handle
    def test_an_edit_that_sends_the_handle_empty_keeps_the_address(self):
        summer = self.collection("Summer sale")
        self.assertEqual(summer["handle"], "summer-sale")  # a new collection's handle comes from its title

        edited = self.edit_collection(summer, name="Winter sale", handle="")

        self.assertEqual((edited["name"], edited["handle"]), ("Winter sale", "summer-sale"))
        self.assertEqual(self.collection("Autumn")["handle"], "autumn")  # and so does the next new one

    def test_changing_a_handle_adds_a_redirect_from_the_old_address(self):
        from apps.themes.models import UrlRedirect

        summer = self.collection("Summer sale")
        self.edit_collection(summer, name="Summer clearance")  # a new title alone: same address, nothing to redirect
        self.assertEqual(UrlRedirect.objects.count(), 0)

        self.edit_collection(summer, handle="winter")
        row = UrlRedirect.objects.get()
        self.assertEqual((row.store_id, row.path, row.target), (self.store.pk, "/collections/summer-sale", "/collections/winter"))
        listed = self.client.get("/api/v1/themes/redirects/").json()
        self.assertEqual([(r["path"], r["target"]) for r in listed], [("/collections/summer-sale", "/collections/winter")])

        self.edit_collection(summer, handle="summer-sale")  # back again: the old address is live, the new one redirects
        self.assertEqual(
            sorted(UrlRedirect.objects.values_list("path", "target")), [("/collections/winter", "/collections/summer-sale")]
        )

    def test_an_existing_redirect_for_the_old_address_is_kept(self):
        from apps.themes.models import UrlRedirect

        autumn = self.collection("Autumn")
        self.redirect("/collections/autumn", "/collections/somewhere-else")

        self.edit_collection(autumn, handle="fall")

        self.assertEqual(
            list(UrlRedirect.objects.values_list("path", "target")), [("/collections/autumn", "/collections/somewhere-else")]
        )

    # ------------------------------------------------------------------ problem 1: redirects on the storefront
    def test_a_renamed_collection_address_sends_shoppers_to_the_new_one(self):
        summer = self.collection("Summer sale")
        self.edit_collection(summer, handle="winter")

        r = self.get("collections/summer-sale?page=2")

        self.assertEqual(r.status_code, 301)
        self.assertEqual(r["Location"], f"/s/{self.slug}/collections/winter?page=2")
        self.assertEqual(self.get("collections/winter").status_code, 200)  # the new address itself is not redirected

    def test_redirects_work_whichever_way_the_path_was_saved_and_follow_a_chain(self):
        self.redirect("/old-page", "/collections/all")
        self.redirect(f"/s/{self.slug}/other-old-page/", f"/s/{self.slug}/collections/all")
        self.redirect("/first", "/second")
        self.redirect("/second", "/third")
        self.redirect("/third", "/collections/all")

        for path in ("old-page", "old-page/", "other-old-page", "first"):
            r = self.get(path)
            self.assertEqual((r.status_code, r["Location"]), (301, f"/s/{self.slug}/collections/all"), path)

    def test_a_missing_page_without_a_redirect_is_still_not_found(self):
        self.assertEqual(self.get("no-such-page").status_code, 404)
        self.assertEqual(self.get("collections/no-such-collection").status_code, 404)
        self.assertEqual(self.get("products/11111111-1111-1111-1111-111111111111").status_code, 404)

    def test_a_removed_product_can_be_sent_to_a_collection(self):
        product = self.product("Gone soon", location=None, quantity=None)
        self.client.delete(f"{URL}{product['id']}/")
        self.redirect(f"/products/{product['id']}", "/collections/all")

        r = self.get(f"products/{product['id']}")

        self.assertEqual((r.status_code, r["Location"]), (301, f"/s/{self.slug}/collections/all"))

    def test_a_redirect_never_replaces_a_page_that_exists(self):
        winter = self.collection("Winter")
        self.redirect(f"/collections/{winter['handle']}", "/collections/all")
        self.assertEqual(self.get(f"collections/{winter['handle']}").status_code, 200)

    def test_only_this_stores_redirects_are_used(self):
        from apps.tenancy.models import Store

        other_client = type(self.client)()
        token, other_id = signup(other_client, "other-nav@example.com")
        other = Store.objects.get(pk=other_id)
        self.redirect("/collections/summer-sale", "/collections/theirs", store=other)

        self.assertEqual(self.get("collections/summer-sale").status_code, 404)

    def test_loops_long_chains_and_unsafe_targets_are_not_followed(self):
        self.redirect("/ping", "/pong")
        self.redirect("/pong", "/ping")
        for number in range(6):  # six redirects in a row: one more than is followed
            self.redirect(f"/hop-{number}", f"/hop-{number + 1}" if number < 5 else "/collections/all")
        self.redirect("/protocol-relative", "//evil.example/steal")
        self.redirect("/script", "javascript:alert(1)")
        self.redirect("/split", "/collections/all\nSet-Cookie: x=1")

        for path in ("ping", "pong", "hop-0", "protocol-relative", "script", "split"):
            self.assertEqual(self.get(path).status_code, 404, path)
        self.assertEqual(self.get("hop-1").status_code, 301)  # five in a row are followed

    def test_a_redirect_can_leave_the_store_for_an_http_address(self):
        self.redirect("/moved", "https://example.org/new-home")
        r = self.get("moved")
        self.assertEqual((r.status_code, r["Location"]), (301, "https://example.org/new-home"))

    # ------------------------------------------------------------------ problem 2: menus and dead links
    def dead_and_live(self):
        """A menu with live links and every kind of dead one."""
        from .models import Product, ProductSalesChannel

        live = self.product("Live product")
        draft = self.product("Draft product", status="draft")
        archived = self.product("Archived product")
        Product.objects.filter(pk=archived["id"]).update(status="archived")
        offline = self.product("Offline product")
        ProductSalesChannel.objects.filter(product_id=offline["id"]).delete()
        gone = self.product("Gone product", location=None, quantity=None)  # no stock, so it can be deleted
        self.client.delete(f"{URL}{gone['id']}/")
        live_shelf = self.collection("Live shelf", [live["id"]])
        draft_shelf = self.collection("Draft shelf", [live["id"]], status="draft")
        offline_shelf = self.collection("Offline shelf", [live["id"]])
        from .models import CollectionSalesChannel

        CollectionSalesChannel.objects.filter(collection_id=offline_shelf["id"]).delete()
        links = {
            "Live product": f"/products/{live['id']}",
            "Draft product": f"/products/{draft['id']}",
            "Archived product": f"/products/{archived['id']}",
            "Offline product": f"/products/{offline['id']}",
            "Gone product": f"/products/{gone['id']}",
            "No such product": "/products/not-an-id",
            "Prefixed draft": f"/s/{self.slug}/products/{draft['id']}",
            "Live shelf": "/collections/live-shelf",
            "Shelf by id": f"/collections/{live_shelf['id']}/?sort=newest",
            "Draft shelf": "/collections/draft-shelf",
            "Offline shelf": "/collections/offline-shelf",
            "Missing shelf": "/collections/never-made",
            "All products": "/collections/all",
            "Blog": "/blog",
            "Elsewhere": "https://example.org",
        }
        return links, draft

    def test_the_storefront_hides_menu_links_to_things_shoppers_cannot_open(self):
        links, draft = self.dead_and_live()
        self.make_menu([{"title": title, "url": url} for title, url in links.items()])
        live = {"Live product", "Live shelf", "Shelf by id", "All products", "Blog", "Elsewhere"}

        header = self.header()

        for title in links:
            self.assertEqual(f">{title}</a>" in header, title in live, title)

        # Publishing the draft product brings its link back: nothing was removed from the menu
        self.client.patch(f"{URL}{draft['id']}/", {"status": "active"}, format="json")
        header = self.header()
        self.assertIn(">Draft product</a>", header)
        self.assertIn(">Prefixed draft</a>", header)

    def test_the_menu_text_lines_in_the_theme_settings_hide_dead_links_too(self):
        from apps.themes.rendering import _menu_lines, dead_menu_urls, layout_data

        settings = {
            "header_menu": "Missing shelf | /collections/never-made\nAll products | /collections/all",
            "footer_menu": "Gone | /products/11111111-1111-1111-1111-111111111111\nBlog | /blog",
            "header_collections": 0,
        }
        urls = [u for _, u in _menu_lines(settings["header_menu"])] + [u for _, u in _menu_lines(settings["footer_menu"])]

        data = layout_data(settings, "/s/x", [], [], dead=dead_menu_urls(self.store, urls))

        self.assertEqual([l["label"] for l in data["header_links"]], ["All products"])
        self.assertEqual([l["label"] for l in data["footer_links"]], ["Blog"])

    def test_the_admin_menu_api_marks_broken_links_and_keeps_every_item(self):
        from apps.themes.models import MenuItem

        links, draft = self.dead_and_live()
        items = [{"title": title, "url": url} for title, url in links.items()]
        items[-1]["children"] = [{"title": "Dead child", "url": links["Draft product"]}, {"title": "Fine child", "url": "/blog"}]
        created = self.make_menu(items)
        broken = {"Draft product", "Archived product", "Offline product", "Gone product", "No such product",
                  "Prefixed draft", "Draft shelf", "Offline shelf", "Missing shelf"}

        listed = self.client.get("/api/v1/themes/menus/").json()[0]
        fetched = self.client.get(f"/api/v1/themes/menus/{created['id']}/").json()

        for menu in (created, listed, fetched):
            by_title = {item["title"]: item["broken"] for item in menu["items"]}
            self.assertEqual({t for t, flag in by_title.items() if flag}, broken)
            self.assertEqual(len(menu["items"]), len(links))  # none removed
            children = {c["title"]: c["broken"] for c in menu["items"][-1]["children"]}
            self.assertEqual(children, {"Dead child": True, "Fine child": False})
        self.assertEqual(MenuItem.objects.count(), len(links) + 2)  # what was saved is untouched

        self.client.patch(f"{URL}{draft['id']}/", {"status": "active"}, format="json")
        again = {i["title"]: i["broken"] for i in self.client.get("/api/v1/themes/menus/").json()[0]["items"]}
        self.assertFalse(again["Draft product"])
        self.assertTrue(again["Missing shelf"])

    def test_checking_a_menu_costs_the_same_queries_for_any_number_of_links(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.themes.rendering import dead_menu_urls

        links, _ = self.dead_and_live()
        live_only = ["/blog", f"/products/{self.product('One')['id']}", "/collections/live-shelf"]
        few = live_only + ["/collections/never-made"]  # one dead link
        many = list(links.values())  # many dead links

        counts = []
        for urls in (few, many):
            with CaptureQueriesContext(connection) as queries:
                dead_menu_urls(self.store, urls)
            counts.append(len(queries))
        self.assertEqual(counts[0], counts[1])
        self.assertLessEqual(counts[0], 3)  # products, collections, and the redirects of the dead ones
        with CaptureQueriesContext(connection) as queries:
            dead_menu_urls(self.store, live_only)
        self.assertLessEqual(len(queries), 2)  # nothing dead: no redirects to look up
        with CaptureQueriesContext(connection) as queries:
            dead_menu_urls(self.store, ["/blog", "https://example.org"])
        self.assertEqual(len(queries), 0)  # nothing to look up

    # ------------------------------------------------------------------ problem 4: category links
    def test_a_category_filter_by_name_still_works_and_its_links_use_the_id(self):
        from unittest import mock

        from apps.themes import schemas

        category = self.client.post("/api/v1/catalog/categories/", {"name": "Shoes"}, format="json").json()
        self.product("Boot A", category_id=category["id"])
        self.product("Boot B", category_id=category["id"])
        self.product("A hat")

        # One product to a page, so the page links show up
        with mock.patch.dict(schemas.SECTIONS["main-collection"]["settings"]["per_page"], {"default": 1}):
            by_name = self.get("collections/all?category=Shoes").content.decode()
            self.assertIn("Boot", by_name)
            self.assertNotIn("A hat", by_name)  # the old, name based link still filters
            self.assertIn(f"category={category['id']}", by_name)  # the links on the page now carry the id...
            self.assertNotIn("category=Shoes", by_name)  # ...not the name

            self.client.patch(f"/api/v1/catalog/categories/{category['id']}/", {"name": "Boots"}, format="json")

            by_id = self.get(f"collections/all?category={category['id']}").content.decode()
            self.assertIn("Boot", by_id)  # a renamed category keeps working by id
            self.assertNotIn("A hat", by_id)
            renamed = self.get("collections/all?category=Boots").content.decode()
            self.assertIn("Boot", renamed)
            self.assertIn(f"category={category['id']}", renamed)
            self.assertNotIn("Boot A", self.get("collections/all?category=Shoes").content.decode())  # the old name is gone

    # ------------------------------------------------------------------ problem 5: the automatic header collections
    def test_the_automatic_header_list_skips_collections_with_nothing_to_buy(self):
        from .models import Product, ProductSalesChannel

        live = self.product("In stock thing")
        draft = self.product("Hidden thing", status="draft")
        offline = self.product("Pos only thing")
        ProductSalesChannel.objects.filter(product_id=offline["id"]).delete()
        self.collection("Full shelf", [live["id"]])
        self.collection("Empty shelf")
        self.collection("Draft products shelf", [draft["id"]])
        self.collection("Offline products shelf", [offline["id"]])

        header = self.header()

        self.assertIn(">Full shelf</a>", header)
        for name in ("Empty shelf", "Draft products shelf", "Offline products shelf"):
            self.assertNotIn(f">{name}</a>", header, name)

        # A menu the owner built is theirs: it keeps its link to the empty collection
        self.make_menu([{"title": "Empty shelf", "url": "/collections/empty-shelf"}])
        self.assertIn(">Empty shelf</a>", self.header())

        # the product comes back (made Active): its collection joins the automatic list when there is no owner menu
        self.client.delete(f"/api/v1/themes/menus/{self.client.get('/api/v1/themes/menus/').json()[0]['id']}/")
        self.client.patch(f"{URL}{draft['id']}/", {"status": "active"}, format="json")
        self.assertIn(">Draft products shelf</a>", self.header())
        self.assertEqual(Product.objects.filter(status="active").count(), 3)

    def test_the_header_collections_are_read_in_one_query(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.themes.rendering import _nav

        counts = []
        for shelves in (1, 5):
            for number in range(shelves):
                self.collection(f"Shelf {shelves}-{number}", [self.product(f"Thing {shelves}-{number}")["id"]])
            with CaptureQueriesContext(connection) as queries:
                nav = _nav(self.store, "/s/x")
            counts.append(len(queries))
        self.assertEqual(counts, [1, 1])
        self.assertTrue(all(entry["has_products"] for entry in nav))


class ProductPhotoRulesTests(APITestCase):
    """Photo upload rate limit, and which image addresses the product form and imports accept."""

    PNG = make_image("PNG")
    UUID = "0b9e6a52-6d0c-4f0e-9a52-7a2d0c1f8e11"

    def setUp(self):
        token, self.store_id = signup(self.client, "photos@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.n = 0

    def upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(f"{URL}images/", {"image": SimpleUploadedFile("a.png", self.PNG)}, format="multipart")

    def create(self, images):
        self.n += 1
        return self.client.post(
            URL, {"title": f"P{self.n}", "sku": f"PH-{self.n}", "images": images}, format="json"
        )

    def test_photo_uploads_are_rate_limited(self):
        from rest_framework.throttling import ScopedRateThrottle

        cache.clear()
        old = ScopedRateThrottle.THROTTLE_RATES
        ScopedRateThrottle.THROTTLE_RATES = {**old, "product_image_upload": "2/min"}
        try:
            codes = [self.upload().status_code for _ in range(3)]
        finally:
            ScopedRateThrottle.THROTTLE_RATES = old
        self.assertEqual(codes, [201, 201, 429])

    def test_form_accepts_only_our_own_image_urls(self):
        uploaded = self.upload().json()["url"]
        real = uploaded.rsplit("/", 1)[-1]  # a picture this store really uploaded
        for url in (uploaded, f"/s/_img/{real}", f"http://testserver/s/_img/{real}",
                    f"https://testserver/s/_img/{real}", "/media/uploads/a.png"):
            r = self.create([url])
            self.assertEqual(r.status_code, 201, (url, r.content))

    def test_form_refuses_other_sites_and_odd_addresses(self):
        for url in (
            "https://cdn.example.com/photo.png",
            "http://cdn.example.com/photo.png",
            f"https://evil.example/s/_img/{self.UUID}",  # right path, wrong site
            f"https://testserver@evil.example/s/_img/{self.UUID}",  # our name used as a login
            f"https://evil.example/media/a.png",
            "/s/_img/not-an-id",
            "/media/../etc/passwd",
            f"/s/_img/{self.UUID} onerror=alert(1)",
            "javascript:alert(1)",
            "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
            "//evil.example/s/_img/" + self.UUID,
        ):
            r = self.create([url])
            self.assertEqual(r.status_code, 400, url)
            self.assertEqual(r.json(), {"images": ["Each image must be an uploaded image URL."]}, url)

    def test_form_accepts_our_bucket_address(self):
        from django.test import override_settings

        with override_settings(MEDIA_S3_BUCKET="tss-media", MEDIA_S3_PUBLIC_URL="https://cdn.example.com"):
            self.assertEqual(self.create([f"https://cdn.example.com/uploads/{self.store_id}/a.png"]).status_code, 201)
            self.assertEqual(self.create(["https://cdn.example.com/other/a.png"]).status_code, 400)
        # without a bucket the same address is just somebody else's server
        self.assertEqual(self.create([f"https://cdn.example.com/uploads/{self.store_id}/a.png"]).status_code, 400)

    def import_rows(self, *images):
        rows = [{"title": f"I{i}", "sku": f"IM-{i}", "images": [url]} for i, url in enumerate(images)]
        return self.client.post(f"{URL}bulk/", {"products": rows}, format="json").json()

    def test_imports_may_use_https_pictures_from_other_sites(self):
        real = self.upload().json()["url"].rsplit("/", 1)[-1]
        body = self.import_rows("https://cdn.example.com/photo.png", f"/s/_img/{real}")
        self.assertEqual((body["created"], body["errors"]), (2, []))

    def test_imports_refuse_http_and_other_schemes(self):
        body = self.import_rows(
            "http://cdn.example.com/photo.png", "ftp://cdn.example.com/photo.png", "javascript:alert(1)",
            "https://user:pass@cdn.example.com/photo.png", "https:///photo.png",
        )
        self.assertEqual(body["created"], 0)
        messages = {e["index"]: e["message"] for e in body["errors"]}
        self.assertEqual(messages[0], "images: Each image URL must start with https://.")
        for index in (1, 2, 3, 4):
            self.assertEqual(messages[index], "images: Each image must be an uploaded image URL.")

    def test_editing_an_imported_product_keeps_its_external_picture(self):
        self.import_rows("https://cdn.example.com/photo.png")
        product = Product.objects.get(variants__sku="IM-0")
        url = "https://cdn.example.com/photo.png"
        # The form sends the picture list back unchanged
        r = self.client.patch(f"{URL}{product.pk}/", {"title": "Renamed", "images": [url]}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        # ... but a new outside address is still refused
        r = self.client.patch(f"{URL}{product.pk}/", {"images": [url, "https://other.example/b.png"]}, format="json")
        self.assertEqual(r.status_code, 400)


class JourneyHelpers:
    """The shared set-up of the storefront journey tests below: a signed-in store with a stocked location."""

    EMAIL = "journey@example.com"

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, self.EMAIL)
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        self.slug = self.store.store_slug
        self.channels = {c["type"]: c["id"] for c in self.client.get("/api/v1/catalog/sales-channels/").json()}
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()
        self.n = 0

    def product(self, title, client=None, **extra):
        self.n += 1
        body = {"title": title, "status": "active", "price": "10", "sku": f"JRN-{self.n}",
                "location": self.location["id"], "quantity": 5, **extra}
        r = (client or self.client).post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def collection(self, name, products=(), **extra):
        r = self.client.post("/api/v1/catalog/collections/", {"name": name, "products": list(products), **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def get(self, path, client=None, slug=None):
        return (client or self.client).get(f"/s/{slug or self.slug}/{path}")

    def html(self, path, **kwargs):
        return self.get(path, **kwargs).content.decode()

    def card(self, listing, title):
        start = listing.rindex('<a class="product-card', 0, listing.index(f'product-card__title">{title}<'))
        return listing[start:listing.index("</a>", start)]

    def upload_photo(self, content, name="photo.jpg", content_type="image/jpeg", client=None):
        from django.core.files.uploadedfile import SimpleUploadedFile

        r = (client or self.client).post(
            f"{URL}images/", {"image": SimpleUploadedFile(name, content, content_type=content_type)}, format="multipart"
        )
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["url"]


class ProductPhotoJourneyTests(JourneyHelpers, APITestCase):
    """Journeys 1, 2 and 10: a product's uploaded photos, from upload to the storefront and back out again."""

    EMAIL = "photos@example.com"

    def test_1_an_uploaded_photo_shows_on_the_product_page_and_card_and_is_served_without_gps(self):
        from io import BytesIO
        from urllib.parse import urlsplit

        from PIL import Image

        from apps.media.tests import make_image

        exif = Image.Exif()
        exif[0x010F] = "TestCam"
        exif[0x8825] = {1: "N", 2: (12.0, 58.0, 30.0), 3: "E", 4: (77.0, 35.0, 10.0)}  # where the photo was taken
        jpeg = make_image("JPEG", exif=exif)
        self.assertEqual(dict(Image.open(BytesIO(jpeg)).getexif().get_ifd(0x8825))[1], "N")  # the test picture has GPS

        url = self.upload_photo(jpeg)
        shirt = self.product("Photo shirt", images=[url])

        page = self.html(f"products/{shirt['id']}")
        self.assertRegex(page, r'<img src="' + re.escape(url) + r'"[^>]*data-main-image')
        self.assertIn(f'src="{url}"', self.card(self.html("collections/all"), "Photo shirt"))

        served = self.client.get(urlsplit(url).path)
        self.assertEqual((served.status_code, served["Content-Type"]), (200, "image/jpeg"))
        self.assertNotIn(b"Exif\x00\x00", served.content)  # no EXIF block at all
        picture = Image.open(BytesIO(served.content))
        self.assertEqual(dict(picture.getexif()), {})
        self.assertEqual(dict(picture.getexif().get_ifd(0x8825)), {})  # and no GPS

    def test_2_replacing_and_reordering_photos_changes_the_page_and_the_card(self):
        from apps.media.tests import make_image

        first, second, third = (self.upload_photo(make_image("PNG", size=(8 + i, 8)), f"p{i}.png", "image/png") for i in range(3))
        shirt = self.product("Photo shirt", images=[first, second, third])
        self.assertEqual([i["url"] for i in self.client.get(f"{URL}{shirt['id']}/").json()["images"]], [first, second, third])
        page = self.html(f"products/{shirt['id']}")
        self.assertRegex(page, r'<img src="' + re.escape(first) + r'"[^>]*data-main-image')
        self.assertIn(f'src="{first}"', self.card(self.html("collections/all"), "Photo shirt"))

        r = self.client.patch(f"{URL}{shirt['id']}/", {"images": [third, first]}, format="json")  # reordered, and one removed
        self.assertEqual(r.status_code, 200, r.content)

        self.assertEqual([i["url"] for i in self.client.get(f"{URL}{shirt['id']}/").json()["images"]], [third, first])
        page = self.html(f"products/{shirt['id']}")
        self.assertRegex(page, r'<img src="' + re.escape(third) + r'"[^>]*data-main-image')
        self.assertNotIn(second, page)  # the removed photo is gone from the page
        card = self.card(self.html("collections/all"), "Photo shirt")
        self.assertIn(f'src="{third}"', card)
        self.assertNotIn(first, card)
        self.assertNotIn(second, card)

    def test_10_deleting_a_product_removes_its_uploaded_picture(self):
        from apps.media.tests import make_image
        from apps.themes.models import ThemeImage

        url = self.upload_photo(make_image("PNG"), "gone.png", "image/png")
        image_id = url.rsplit("/", 1)[-1]
        product = self.product("Photo gone", images=[url], location=None, quantity=None)  # no stock, so it can be deleted
        self.assertTrue(ThemeImage.objects.filter(pk=image_id).exists())

        self.assertEqual(self.client.delete(f"{URL}{product['id']}/").status_code, 204)

        self.assertFalse(ThemeImage.objects.filter(pk=image_id).exists())

    def test_10_deleting_a_product_removes_its_photo_list(self):
        from apps.media.tests import make_image

        from .models import ProductImage

        url = self.upload_photo(make_image("PNG"), "x.png", "image/png")
        product = self.product("Photo list", images=[url], location=None, quantity=None)
        self.assertEqual(ProductImage.objects.filter(product_id=product["id"]).count(), 1)
        self.client.delete(f"{URL}{product['id']}/")
        self.assertFalse(ProductImage.objects.filter(product_id=product["id"]).exists())

    def test_10_bulk_delete_removes_the_pictures_of_every_deleted_product_but_not_of_a_kept_one(self):
        from urllib.parse import urlsplit
        from uuid import UUID

        from apps.media.tests import make_image
        from apps.themes.models import ThemeImage

        urls = [self.upload_photo(make_image("PNG", size=(8 + i, 8)), f"b{i}.png", "image/png") for i in range(3)]
        ids = [u.rsplit("/", 1)[-1] for u in urls]
        free = [self.product(f"Bulk {i}", images=[urls[i]], location=None, quantity=None)["id"] for i in range(2)]
        stocked = self.product("Bulk stocked", images=[urls[2]])  # has stock: it cannot be deleted
        for url in urls:
            self.assertEqual(self.client.get(urlsplit(url).path).status_code, 200)  # (and now they are cached)

        r = self.client.post(f"{URL}bulk-delete/", {"ids": free + [stocked["id"]]}, format="json")

        self.assertEqual((r.status_code, r.json()["deleted"], r.json()["blocked"]), (200, 2, ["Bulk stocked"]), r.content)
        self.assertEqual(set(ThemeImage.objects.filter(pk__in=ids).values_list("pk", flat=True)), {UUID(ids[2])})
        self.assertEqual(self.client.get(urlsplit(urls[0]).path).status_code, 404)  # no longer served
        self.assertEqual(self.client.get(urlsplit(urls[2]).path).status_code, 200)  # the kept product still has its photo

    def test_10_a_picture_two_products_share_stays_until_the_last_one_is_deleted(self):
        from urllib.parse import urlsplit

        from apps.media.tests import make_image
        from apps.themes.models import ThemeImage

        url = self.upload_photo(make_image("PNG"), "shared.png", "image/png")
        image_id = url.rsplit("/", 1)[-1]
        one = self.product("Shared one", images=[url], location=None, quantity=None)
        two = self.product("Shared two", images=[url], location=None, quantity=None)

        self.client.delete(f"{URL}{one['id']}/")
        self.assertTrue(ThemeImage.objects.filter(pk=image_id).exists())  # the other product still shows it
        self.assertEqual(self.client.get(urlsplit(url).path).status_code, 200)
        self.client.delete(f"{URL}{two['id']}/")
        self.assertFalse(ThemeImage.objects.filter(pk=image_id).exists())
        self.assertEqual(self.client.get(urlsplit(url).path).status_code, 404)

    def test_10_library_pictures_and_other_stores_pictures_are_never_deleted_with_a_product(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.media.models import MediaAsset
        from apps.media.tests import make_image
        from apps.themes.models import ThemeImage

        other = type(self.client)()
        token, other_id = signup(other, "delete-other@example.com")
        other.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)
        theirs = self.upload_photo(make_image("PNG", size=(9, 9)), "t.png", "image/png", client=other)
        asset = self.client.post("/api/v1/media/", {"image": SimpleUploadedFile("lib.png", make_image("PNG"))}, format="multipart").json()
        mine = self.product("Library user", images=[asset["url"]], location=None, quantity=None)

        self.client.delete(f"{URL}{mine['id']}/")

        self.assertTrue(MediaAsset.objects.filter(pk=asset["id"]).exists())  # the media library keeps its pictures
        self.assertTrue(ThemeImage.objects.filter(pk=theirs.rsplit("/", 1)[-1]).exists())  # and so does the other store

    def test_10_a_delete_that_is_refused_keeps_the_picture(self):
        from apps.media.tests import make_image
        from apps.themes.models import ThemeImage

        url = self.upload_photo(make_image("PNG"), "kept.png", "image/png")
        stocked = self.product("Stocked photo", images=[url])  # stock: used by inventory, so it cannot be deleted

        self.assertEqual(self.client.delete(f"{URL}{stocked['id']}/").status_code, 400)

        self.assertTrue(ThemeImage.objects.filter(pk=url.rsplit("/", 1)[-1]).exists())

    def test_extra_an_unknown_picture_id_is_refused_with_the_usual_message(self):
        r = self.client.post(URL, {"title": "Made up", "status": "draft", "sku": "MADE-1",
                                   "images": ["/s/_img/0b9e6a52-6d0c-4f0e-9a52-7a2d0c1f8e11"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json(), {"images": ["Each image must be an uploaded image URL."]})

    def test_extra_this_stores_own_pictures_are_accepted_in_every_form(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.media.tests import make_image

        photo = self.upload_photo(make_image("PNG"), "own.png", "image/png")
        asset = self.client.post("/api/v1/media/", {"image": SimpleUploadedFile("lib.png", make_image("PNG", size=(9, 9)))}, format="multipart").json()
        for number, url in enumerate((photo, photo.replace("http://testserver", ""), asset["url"], asset["url"].replace("http://testserver", ""))):
            r = self.client.post(URL, {"title": f"Own {number}", "status": "draft", "sku": f"OWN-{number}", "images": [url]}, format="json")
            self.assertEqual(r.status_code, 201, (url, r.content))

    def test_extra_a_picture_that_is_already_on_the_product_stays_valid_when_it_is_saved_again(self):
        from apps.media.tests import make_image
        from apps.themes.models import ThemeImage

        url = self.upload_photo(make_image("PNG"), "stays.png", "image/png")
        product = self.product("Keeps its photo", images=[url], location=None, quantity=None)
        ThemeImage.objects.filter(pk=url.rsplit("/", 1)[-1]).delete()  # the picture row is gone (an old clean-up)

        r = self.client.patch(f"{URL}{product['id']}/", {"title": "Renamed", "images": [url]}, format="json")

        self.assertEqual(r.status_code, 200, r.content)  # a kept picture is not checked again

    def test_extra_an_import_row_with_another_stores_picture_is_refused(self):
        from apps.media.tests import make_image

        other = type(self.client)()
        token, other_id = signup(other, "import-other@example.com")
        other.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)
        theirs = self.upload_photo(make_image("PNG"), "t.png", "image/png", client=other)

        body = self.client.post(f"{URL}bulk/", {"products": [
            {"title": "Borrowing", "status": "draft", "sku": "IMPB-1", "images": [theirs]},
        ]}, format="json").json()

        self.assertEqual(body["created"], 0)
        self.assertEqual(body["errors"], [{"index": 0, "message": "images: Each image must be an uploaded image URL."}])

    def test_extra_a_photo_uploaded_by_another_store_is_refused(self):
        from apps.media.tests import make_image

        other = type(self.client)()
        token, other_id = signup(other, "photos-other@example.com")
        other.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)
        theirs = self.upload_photo(make_image("PNG"), "theirs.png", "image/png", client=other)

        r = self.client.post(URL, {"title": "Borrowed", "status": "draft", "sku": "BORROW-1", "images": [theirs]}, format="json")

        self.assertEqual(r.status_code, 400, r.content)

    def test_extra_a_media_library_picture_of_another_store_is_refused(self):
        from apps.media.tests import make_image

        other = type(self.client)()
        token, other_id = signup(other, "media-other@example.com")
        other.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)
        from django.core.files.uploadedfile import SimpleUploadedFile

        asset = other.post("/api/v1/media/", {"image": SimpleUploadedFile("m.png", make_image("PNG"))}, format="multipart").json()

        r = self.client.post(
            URL, {"title": "Borrowed 2", "status": "draft", "sku": "BORROW-2", "images": [asset["url"]]}, format="json"
        )

        self.assertEqual(r.status_code, 400, r.content)


class AutomatedCollectionJourneyTests(JourneyHelpers, APITestCase):
    """Journey 3: products join and leave automated collections as their price, tags, stock or status change."""

    EMAIL = "automated@example.com"

    def auto(self, name, field, operator, value):
        return self.collection(name, type="automated", match="all", rules=[{"field": field, "operator": operator, "value": value}])

    def change(self, call):
        """One request, with the work that is queued for after the save done (as it is once it is committed)."""
        from django.db import connection

        from . import services

        with self.captureOnCommitCallbacks(execute=True):
            result = call()
        # A test never really commits, so the queued check would stay "scheduled" for good: drop it as a commit does
        connection.run_on_commit[:] = [c for c in connection.run_on_commit if c[1] is not services._flush_refresh]
        return result

    def count(self, collection):
        return self.client.get(f"/api/v1/catalog/collections/{collection['id']}/").json()["products_count"]

    def listed(self, collection, title):
        return title in self.html(f"collections/{collection['handle']}")

    def test_3_products_join_and_leave_as_they_change_and_the_collection_page_follows(self):
        tagged = self.auto("On sale", "tag", "equals", "sale")
        cheap = self.auto("Cheap", "price", "less_than", "20")
        stocked = self.auto("In stock", "inventory_stock", "greater_than", "0")
        live = self.auto("Live", "status", "equals", "active")
        for collection in (tagged, cheap, stocked, live):
            self.assertEqual(self.count(collection), 0)

        lamp = self.change(lambda: self.product("Reading lamp", tags=["sale"], price="15"))  # matches all four
        counts = lambda: [self.count(c) for c in (tagged, cheap, stocked, live)]  # noqa: E731
        self.assertEqual(counts(), [1, 1, 1, 1])
        for collection in (tagged, cheap, stocked):
            self.assertTrue(self.listed(collection, "Reading lamp"), collection["name"])
        patch = lambda body: self.change(lambda: self.client.patch(f"{URL}{lamp['id']}/", body, format="json"))  # noqa: E731

        patch({"tags": []})  # the tag is taken off
        self.assertEqual(counts(), [0, 1, 1, 1])
        self.assertFalse(self.listed(tagged, "Reading lamp"))
        patch({"tags": ["sale"]})
        self.assertEqual(counts(), [1, 1, 1, 1])
        self.assertTrue(self.listed(tagged, "Reading lamp"))

        patch({"price": "25"})  # no longer under 20
        self.assertEqual(counts(), [1, 0, 1, 1])
        self.assertFalse(self.listed(cheap, "Reading lamp"))
        patch({"price": "15"})
        self.assertEqual(counts(), [1, 1, 1, 1])
        self.assertTrue(self.listed(cheap, "Reading lamp"))

        variant = self.client.get(f"{URL}{lamp['id']}/").json()["variants"][0]["id"]

        def stock(quantity):
            body = {"variant": variant, "location": self.location["id"], "available": quantity}
            return self.change(lambda: self.client.post("/api/v1/inventory/items/set/", body, format="json"))

        stock(0)  # sold out
        self.assertEqual(counts(), [1, 1, 0, 1])
        self.assertFalse(self.listed(stocked, "Reading lamp"))
        stock(3)
        self.assertEqual(counts(), [1, 1, 1, 1])
        self.assertTrue(self.listed(stocked, "Reading lamp"))

        patch({"status": "draft"})  # taken off sale: it leaves the "active" collection, the others still hold it
        self.assertEqual(counts(), [1, 1, 1, 0])
        for collection in (tagged, cheap, stocked):
            self.assertFalse(self.listed(collection, "Reading lamp"), collection["name"])  # but shoppers do not see it
        patch({"status": "active"})
        self.assertEqual(counts(), [1, 1, 1, 1])
        for collection in (tagged, cheap, stocked):
            self.assertTrue(self.listed(collection, "Reading lamp"), collection["name"])

    def test_3_a_new_product_joins_only_the_collections_it_matches(self):
        tagged = self.auto("On sale", "tag", "equals", "sale")
        cheap = self.auto("Cheap", "price", "less_than", "20")

        self.change(lambda: self.product("Pricey plain", price="50"))
        cheap_one = self.change(lambda: self.product("Cheap plain", price="5"))

        self.assertEqual((self.count(tagged), self.count(cheap)), (0, 1))
        self.assertTrue(self.listed(cheap, "Cheap plain"))
        self.assertFalse(self.listed(cheap, "Pricey plain"))
        self.assertFalse(self.listed(tagged, "Cheap plain"))
        self.assertEqual(self.client.get(f"{URL}{cheap_one['id']}/").json()["status"], "active")


class UnavailableProductJourneyTests(JourneyHelpers, APITestCase):
    """Journeys 4 and 8: a product that is Draft, Archived or off the Online Store is hidden everywhere, and back again."""

    EMAIL = "unavailable@example.com"

    def setUp(self):
        super().setUp()
        self.gadget = self.product("Gadget zeta")
        self.shelf = self.collection("Shelf", [self.gadget["id"]])
        index = self.client.get("/api/v1/themes/settings/").json()["templates"]["index"]
        r = self.client.patch("/api/v1/themes/settings/", {"templates": {"index": {
            "order": index["order"] + ["offers"],
            "sections": {"offers": {"type": "promotions", "settings": {"view_all_collection": self.shelf["handle"]}, "blocks": []}},
        }}}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        added = self.client.post(f"/s/{self.slug}/wishlist/items/", {"action": "add", "product_id": self.gadget["id"]},
                                 HTTP_ACCEPT="application/json")
        self.assertEqual(added.json()["count"], 1)

    def promotions(self):
        found = re.search(r'<section id="section-offers".*?</section>', self.html(""), re.S)
        return found.group(0) if found else ""

    def where_it_shows(self):
        """Every place the Gadget can show: a dict of place -> True/False (the product page by its status code)."""
        title = "Gadget zeta"
        return {
            "product page": self.get(f"products/{self.gadget['id']}").status_code == 200,
            "all products": title in self.html("collections/all"),
            "its collection": title in self.html("collections/shelf"),
            "search": title in self.html("collections/all?q=Gadget"),
            "home page": title in self.html(""),
            "promotions section": title in self.promotions(),
        }

    def hide(self, how):
        from .models import Product, ProductSalesChannel

        if how == "draft":
            Product.objects.filter(pk=self.gadget["id"]).update(status="draft")
        elif how == "archived":
            Product.objects.filter(pk=self.gadget["id"]).update(status="archived")
        else:
            ProductSalesChannel.objects.filter(product_id=self.gadget["id"]).delete()

    def show(self, how):
        from .models import Product, ProductSalesChannel, SalesChannel

        if how == "off the Online Store":
            ProductSalesChannel.objects.create(
                product_id=self.gadget["id"], sales_channel=SalesChannel.objects.get(pk=self.channels["online"])
            )
        else:
            Product.objects.filter(pk=self.gadget["id"]).update(status="active")

    def test_4_a_product_that_is_not_for_sale_shows_nowhere_and_comes_back_when_it_is(self):
        self.assertEqual(set(self.where_it_shows().values()), {True})  # for sale: everywhere

        for how in ("draft", "archived", "off the Online Store"):
            self.hide(how)
            where = self.where_it_shows()
            self.assertEqual(set(where.values()), {False}, (how, where))
            self.assertNotIn("Gadget zeta", self.html("wishlist/"), how)  # and not in the shopper's wishlist

            self.show(how)
            where = self.where_it_shows()
            self.assertEqual(set(where.values()), {True}, (how, where))

    def test_4_the_wishlist_has_the_product_again_when_it_is_published_again(self):
        self.hide("draft")
        self.get("")  # the shopper browses while it is hidden
        self.show("draft")

        self.assertIn("Gadget zeta", self.html("wishlist/"))

    def test_4_the_wishlist_keeps_its_order_and_shows_only_what_is_for_sale_meanwhile(self):
        from .models import Product

        second = self.product("Gadget omega")
        self.client.post(f"/s/{self.slug}/wishlist/items/", {"action": "add", "product_id": second["id"]}, HTTP_ACCEPT="application/json")
        Product.objects.filter(pk=self.gadget["id"]).update(status="draft")

        page = self.html("wishlist/")
        self.assertIn("Gadget omega", page)
        self.assertNotIn("Gadget zeta", page)  # not shown while it is not for sale
        header = page[page.index("<header"):page.index("</header>")]
        self.assertNotIn("Gadget zeta", header)
        count = self.client.post(f"/s/{self.slug}/wishlist/items/", {"action": "add", "product_id": second["id"]}, HTTP_ACCEPT="application/json")
        self.assertEqual(count.json()["count"], 1)  # and not counted

        Product.objects.filter(pk=self.gadget["id"]).update(status="active")
        page = self.html("wishlist/")
        self.assertLess(page.index("Gadget zeta"), page.index("Gadget omega"))  # back, where it was saved
        self.assertEqual(self.client.post(f"/s/{self.slug}/wishlist/items/", {"action": "add", "product_id": second["id"]},
                                          HTTP_ACCEPT="application/json").json()["count"], 2)

    def test_8_a_hidden_product_stays_out_of_a_filtered_page(self):
        from .models import Product, ProductSalesChannel

        category = self.client.post("/api/v1/catalog/categories/", {"name": "Toys"}, format="json").json()
        shown = self.product("Toy shown", category_id=category["id"])
        draft = self.product("Toy draft", category_id=category["id"], status="draft")
        offline = self.product("Toy offline", category_id=category["id"])
        ProductSalesChannel.objects.filter(product_id=offline["id"]).delete()
        archived = self.product("Toy archived", category_id=category["id"])
        Product.objects.filter(pk=archived["id"]).update(status="archived")

        for query in (f"category={category['id']}", "category=Toys"):  # by id, and by name as older links do
            page = self.html(f"collections/all?{query}")
            self.assertIn("Toy shown", page, query)
            for hidden in ("Toy draft", "Toy offline", "Toy archived"):
                self.assertNotIn(hidden, page, (query, hidden))

        self.client.patch(f"{URL}{draft['id']}/", {"status": "active"}, format="json")
        self.assertIn("Toy draft", self.html(f"collections/all?category={category['id']}"))
        self.assertEqual(shown["status"], "active")


class MenuOldHandleJourneyTests(JourneyHelpers, APITestCase):
    """Journey 5: a menu item that points at a collection's old handle, after the handle is changed."""

    EMAIL = "oldhandle@example.com"

    def setUp(self):
        super().setUp()
        self.summer = self.collection("Summer sale", [self.product("Sun hat")["id"]])
        self.menu = self.client.post("/api/v1/themes/menus/", {"name": "Main menu", "items": [
            {"title": "Sale link", "url": "/collections/summer-sale"}, {"title": "Blog", "url": "/blog"}]}, format="json").json()
        self.client.patch(f"/api/v1/catalog/collections/{self.summer['id']}/", {"handle": "winter"}, format="json")

    def test_5_the_old_link_still_leads_to_the_collection_through_a_redirect(self):
        old = self.get("collections/summer-sale")
        self.assertEqual((old.status_code, old["Location"]), (301, f"/s/{self.slug}/collections/winter"))
        self.assertIn("Sun hat", self.html("collections/winter"))

    def test_5_the_menu_keeps_the_link_and_does_not_call_it_broken(self):
        header = re.search(r"<header.*?</header>", self.html(""), re.S).group(0)
        self.assertIn(">Sale link</a>", header)
        items = {i["title"]: i["broken"] for i in self.client.get("/api/v1/themes/menus/").json()[0]["items"]}
        self.assertFalse(items["Sale link"])


    def test_5_a_link_to_a_gone_collection_without_a_redirect_is_still_hidden_and_marked(self):
        header = lambda: re.search(r"<header.*?</header>", self.html(""), re.S).group(0)  # noqa: E731
        self.assertIn(">Sale link</a>", header())  # the old address redirects, so the link stays
        rows = self.client.get("/api/v1/themes/redirects/").json()
        self.assertEqual([r["path"] for r in rows], ["/collections/summer-sale"])

        self.client.delete(f"/api/v1/themes/redirects/{rows[0]['id']}/")  # the redirect is taken away

        self.assertNotIn(">Sale link</a>", header())  # nothing leads anywhere now
        items = {i["title"]: i["broken"] for i in self.client.get("/api/v1/themes/menus/").json()[0]["items"]}
        self.assertTrue(items["Sale link"])

    def test_5_a_redirect_saved_with_the_store_prefix_keeps_the_link_alive_too(self):
        from apps.themes.models import UrlRedirect

        self.client.post("/api/v1/themes/menus/", {"name": "Footer links", "items": [
            {"title": "Gone page", "url": "/collections/long-gone"}]}, format="json")
        items = lambda: {i["title"]: i["broken"] for m in self.client.get("/api/v1/themes/menus/").json() for i in m["items"]}  # noqa: E731
        self.assertTrue(items()["Gone page"])
        UrlRedirect.objects.create(store=self.store, path=f"/s/{self.slug}/collections/long-gone/", target="/collections/all")
        self.assertFalse(items()["Gone page"])

    def test_5_the_menus_text_lines_keep_a_link_with_a_redirect_as_well(self):
        from apps.themes.rendering import _menu_lines, dead_menu_urls, layout_data

        settings = {"header_menu": "Old shelf | /collections/summer-sale\nGone | /collections/nowhere", "footer_menu": "", "header_collections": 0}
        urls = [u for _, u in _menu_lines(settings["header_menu"])]
        data = layout_data(settings, "/s/x", [], [], dead=dead_menu_urls(self.store, urls))
        self.assertEqual([l["label"] for l in data["header_links"]], ["Old shelf"])  # the redirect keeps it, the other is dead

    def test_5_redirects_of_dead_links_are_looked_up_together_not_one_by_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from apps.themes.models import UrlRedirect
        from apps.themes.rendering import dead_menu_urls

        for number in range(6):
            UrlRedirect.objects.create(store=self.store, path=f"/collections/gone-{number}", target="/collections/all")
        counts = []
        for how_many in (1, 6):
            urls = [f"/collections/gone-{n}" for n in range(how_many)] + ["/collections/other-gone"]
            with CaptureQueriesContext(connection) as queries:
                dead = dead_menu_urls(self.store, urls)
            counts.append(len(queries))
            self.assertEqual(dead, {"/collections/other-gone"})
        self.assertEqual(counts[0], counts[1])


class ImportJourneyTests(JourneyHelpers, APITestCase):
    """Journey 6: one import with priced, unpriced and https-picture rows."""

    EMAIL = "importjourney@example.com"

    def test_6_priced_rows_are_created_with_their_pictures_and_the_unpriced_active_row_is_refused(self):
        rows = [
            {"title": "Priced with picture", "status": "active", "sku": "IMPJ-1", "price": "12.50",
             "images": ["https://cdn.example.com/priced.png"], "quantity": 3},
            {"title": "Unpriced active", "status": "active", "sku": "IMPJ-2", "images": ["https://cdn.example.com/u.png"]},
            {"title": "Unpriced draft", "status": "draft", "sku": "IMPJ-3"},
            {"title": "Priced with a plain http picture", "status": "active", "sku": "IMPJ-4", "price": "9",
             "images": ["http://cdn.example.com/insecure.png"]},
            {"title": "Priced no picture", "status": "active", "sku": "IMPJ-5", "price": "7", "quantity": 1},
        ]

        body = self.client.post(f"{URL}bulk/", {"products": rows}, format="json").json()

        self.assertEqual(body["created"], 3, body)
        messages = {e["index"]: e["message"] for e in body["errors"]}
        self.assertEqual(sorted(messages), [1, 3])
        self.assertEqual(messages[1], "price: Enter a price for every option before making this product active.")
        self.assertEqual(messages[3], "images: Each image URL must start with https://.")

        listing = self.html("collections/all")
        self.assertIn("Priced with picture", listing)
        self.assertIn('src="https://cdn.example.com/priced.png"', self.card(listing, "Priced with picture"))
        self.assertIn("Priced no picture", listing)
        for refused in ("Unpriced active", "Unpriced draft", "plain http picture"):
            self.assertNotIn(refused, listing)  # refused, or only a Draft

        from .models import Product

        made = Product.objects.get(title="Priced with picture")
        page = self.html(f"products/{made.pk}")
        self.assertRegex(page, r'<img src="https://cdn\.example\.com/priced\.png"[^>]*data-main-image')
        self.assertIn("12.50", page)
        self.assertEqual(
            set(Product.objects.filter(store_id=self.store_id).values_list("title", flat=True)),
            {"Priced with picture", "Unpriced draft", "Priced no picture"},
        )


class TwoStoresJourneyTests(JourneyHelpers, APITestCase):
    """Journey 9: two stores with the same titles, SKUs, handles, menu names and redirect paths never mix."""

    EMAIL = "twostores-a@example.com"

    def setUp(self):
        super().setUp()
        from apps.media.tests import make_image
        from apps.tenancy.models import Store

        self.other = type(self.client)()
        token, other_id = signup(self.other, "twostores-b@example.com")
        self.other.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)
        self.other_store = Store.objects.get(pk=other_id)
        self.other_slug = self.other_store.store_slug
        self.other_location = self.other.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()

        self.stores = {}
        for key, client, slug, location, price in (
            ("a", self.client, self.slug, self.location, "10"), ("b", self.other, self.other_slug, self.other_location, "20"),
        ):
            photo = self.upload_photo(make_image("PNG", size=(8, 8 if key == "a" else 9)), f"{key}.png", "image/png", client=client)
            mug = client.post(URL, {"title": "Hero mug", "status": "active", "price": price, "sku": "MUG-1",
                                    "location": location["id"], "quantity": 4, "images": [photo]}, format="json").json()
            shelf = client.post("/api/v1/catalog/collections/", {"name": "Mugs", "products": [mug["id"]]}, format="json").json()
            menu = client.post("/api/v1/themes/menus/", {"name": "Main menu", "items": [
                {"title": "All mugs", "url": "/collections/mugs"}, {"title": "The hero mug", "url": f"/products/{mug['id']}"}]},
                format="json").json()
            from apps.themes.models import UrlRedirect

            UrlRedirect.objects.create(
                store=Store.objects.get(store_slug=slug), path="/old-mugs",
                target="/collections/mugs" if key == "a" else "/collections/all",
            )
            self.stores[key] = {"client": client, "slug": slug, "mug": mug, "shelf": shelf, "menu": menu, "photo": photo}

    def page(self, key, path):
        return self.html(path, client=self.stores[key]["client"], slug=self.stores[key]["slug"])

    def test_9_each_stores_pages_show_only_its_own_things(self):
        self.assertEqual(self.stores["a"]["shelf"]["handle"], self.stores["b"]["shelf"]["handle"])  # the same address in both
        for me, other in (("a", "b"), ("b", "a")):
            mine, theirs = self.stores[me], self.stores[other]
            for path in ("", "collections/all", "collections/mugs", f"products/{mine['mug']['id']}"):
                page = self.page(me, path)
                self.assertNotIn(theirs["mug"]["id"], page, (me, path))
                self.assertNotIn(theirs["photo"].rsplit("/", 1)[-1], page, (me, path))  # their picture never appears
                self.assertNotIn(f"/s/{theirs['slug']}/", page, (me, path))
            self.assertIn(mine["mug"]["id"], self.page(me, "collections/mugs"))
            self.assertIn(mine["photo"], self.page(me, f"products/{mine['mug']['id']}"))
            # a page of the other store's under my address is not found
            self.assertEqual(self.get(f"products/{theirs['mug']['id']}", client=mine["client"], slug=mine["slug"]).status_code, 404)
            self.assertEqual(self.get(f"collections/{theirs['shelf']['id']}", client=mine["client"], slug=mine["slug"]).status_code, 404)
        a_price, b_price = self.page("a", "collections/mugs"), self.page("b", "collections/mugs")
        self.assertIn("10.00", a_price)
        self.assertNotIn("20.00", a_price)
        self.assertIn("20.00", b_price)
        self.assertNotIn("10.00", b_price)

    def test_9_each_stores_menu_links_stay_inside_its_own_store(self):
        for key in ("a", "b"):
            store = self.stores[key]
            header = re.search(r"<header.*?</header>", self.page(key, ""), re.S).group(0)
            self.assertIn(f'href="/s/{store["slug"]}/collections/mugs"', header)
            self.assertIn(f'href="/s/{store["slug"]}/products/{store["mug"]["id"]}"', header)
            self.assertNotIn("/s/" + self.stores["b" if key == "a" else "a"]["slug"], header)
            listed = store["client"].get("/api/v1/themes/menus/").json()
            self.assertEqual([m["id"] for m in listed], [store["menu"]["id"]])  # only its own menu
            self.assertEqual({i["broken"] for i in listed[0]["items"]}, {False})
        self.assertNotEqual(self.stores["a"]["menu"]["id"], self.stores["b"]["menu"]["id"])

    def test_9_the_same_redirect_path_leads_each_store_to_its_own_target(self):
        a = self.get("old-mugs", client=self.stores["a"]["client"], slug=self.stores["a"]["slug"])
        b = self.get("old-mugs", client=self.stores["b"]["client"], slug=self.stores["b"]["slug"])
        self.assertEqual((a.status_code, a["Location"]), (301, f"/s/{self.stores['a']['slug']}/collections/mugs"))
        self.assertEqual((b.status_code, b["Location"]), (301, f"/s/{self.stores['b']['slug']}/collections/all"))
        for key in ("a", "b"):
            rows = self.stores[key]["client"].get("/api/v1/themes/redirects/").json()
            self.assertEqual([r["path"] for r in rows], ["/old-mugs"])  # its own, not the other store's too


class CatalogDatabaseConstraintTests(TestCase):
    """Relationships, uniqueness and value checks enforced by the database itself, per store."""

    def setUp(self):
        self.a = Store.objects.create(store_name="Alpha", store_slug="db-alpha", status="active")
        self.b = Store.objects.create(store_name="Beta", store_slug="db-beta", status="active")
        self.product = Product.objects.create(store=self.a, title="Mug", status="active")

    def assertRejected(self, make):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            make()

    def test_variant_money_and_weight_cannot_be_negative(self):
        ProductVariant.objects.create(product=self.product, sku="M-0", price=0, compare_at_price=None)
        self.assertRejected(lambda: ProductVariant.objects.create(product=self.product, price=-1))
        self.assertRejected(lambda: ProductVariant.objects.create(product=self.product, price=1, compare_at_price=-1))
        self.assertRejected(lambda: ProductVariant.objects.create(product=self.product, price=1, weight=-1))

    def test_product_status_and_image_position_are_checked(self):
        from .models import ProductImage
        self.assertRejected(lambda: Product.objects.create(store=self.a, title="X", status="published"))
        self.assertRejected(lambda: ProductImage.objects.create(product=self.product, url="u", position=-1))
        ProductImage.objects.create(product=self.product, url="u", position=0)

    def test_catalogue_adjustment_cannot_be_negative(self):
        from .models import Catalogue
        self.assertRejected(lambda: Catalogue.objects.create(store=self.a, name="C", price_adjustment=-5))

    def test_names_are_unique_per_store_not_across_stores(self):
        from .models import Tag
        for model, extra in ((Tag, {}), (SalesChannel, {"type": "online"})):
            model.objects.create(store=self.a, name="Same", **extra)
            model.objects.create(store=self.b, name="Same", **extra)  # another store may reuse it
            self.assertRejected(lambda: model.objects.create(store=self.a, name="Same", **extra))
        Brand.objects.create(store=self.a, name="Acme")
        Brand.objects.create(store=self.b, name="Acme")
        self.assertRejected(lambda: Brand.objects.create(store=self.a, name=" acme "))  # case/space-insensitive

    def test_category_names_unique_per_parent_and_store(self):
        top = Category.objects.create(store=self.a, name="Kitchen")
        Category.objects.create(store=self.b, name="Kitchen")
        self.assertRejected(lambda: Category.objects.create(store=self.a, name="KITCHEN"))
        Category.objects.create(store=self.a, name="Mugs", parent=top)
        Category.objects.create(store=self.a, name="Mugs")  # a top-level one may share a child's name
        self.assertRejected(lambda: Category.objects.create(store=self.a, name="mugs", parent=top))

    def test_collection_handles_and_link_rows_are_unique(self):
        c = Collection.objects.create(store=self.a, name="Sale", handle="sale")
        Collection.objects.create(store=self.b, name="Sale", handle="sale")
        self.assertRejected(lambda: Collection.objects.create(store=self.a, name="Other", handle="sale"))
        ProductCollection.objects.create(product=self.product, collection=c)
        self.assertRejected(lambda: ProductCollection.objects.create(product=self.product, collection=c))

    def test_referenced_rows_are_protected(self):
        from django.db.models import ProtectedError
        category = Category.objects.create(store=self.a, name="Cups")
        brand = Brand.objects.create(store=self.a, name="Brandy")
        Product.objects.create(store=self.a, title="Cup", status="draft", category=category, brand=brand)
        ProductVariant.objects.create(product=self.product, price=5)
        for obj in (category, brand, self.product):
            with self.assertRaises(ProtectedError):
                obj.delete()

    def test_default_sales_channels_are_seeded_once(self):
        from .services import sales_channels_for
        sales_channels_for(self.a)
        sales_channels_for(self.a)
        self.assertEqual(SalesChannel.objects.filter(store=self.a).count(), 3)
        self.assertFalse(SalesChannel.objects.filter(store=self.b).exists())


class ProductOptionsTests(APITestCase):
    """Product.options (names and value order) and each variant's values in ProductVariant.metadata["options"]."""

    def setUp(self):
        token, self.store_id = signup(self.client, "opts@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.product = self.client.post(URL, {"title": "Cola", "price": "10", "sku": "C1"}, format="json").json()
        self.url = f"{URL}{self.product['id']}/"
        self.first = self.product["variants"][0]["id"]

    def size_options(self, *values):
        return [{"name": "Size", "values": list(values)}]

    def variant_ids(self):
        return {str(i) for i in ProductVariant.objects.filter(product_id=self.product["id"]).values_list("id", flat=True)}

    def add_pack(self):
        r = self.client.patch(self.url, {
            "options": self.size_options("Single", "Pack of 6"),
            "variants": [
                {"id": self.first, "options": {"Size": "Single"}},
                {"sku": "C6", "price": "30", "options": {"Size": "Pack of 6"}},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_options_are_validated(self):
        bad = [
            [{"name": n, "values": ["a"]} for n in ("A", "B", "C", "D")],  # more than 3
            [{"name": "  ", "values": ["a"]}],  # blank name
            [{"name": "Size", "values": ["Single", " "]}],  # blank value
            [{"name": "Size", "values": ["Single", "single"]}],  # duplicate value
            [{"name": "Size", "values": ["a"]}, {"name": "size", "values": ["b"]}],  # duplicate name
            [{"name": "Size", "values": []}],  # no values
        ]
        for options in bad:
            r = self.client.patch(self.url, {"options": options}, format="json")
            self.assertEqual(r.status_code, 400, options)
            self.assertIn("options", r.json())
        self.assertEqual(Product.objects.get(pk=self.product["id"]).options, [])

    def test_duplicate_combinations_and_mismatches_save_nothing(self):
        cases = [
            [{"id": self.first, "options": {"Size": "Single"}},
             {"sku": "C2", "price": "1", "options": {"Size": "Single"}}],  # same combination twice
            [{"id": self.first, "options": {"Size": "Large"}}],  # not a listed value
            [{"id": self.first, "options": {}}],  # missing an option
            [{"id": self.first, "options": {"Size": "Single", "Colour": "Red"}}],  # an unknown option
        ]
        for variants in cases:
            r = self.client.patch(
                self.url, {"options": self.size_options("Single", "Pack of 6"), "variants": variants}, format="json"
            )
            self.assertEqual(r.status_code, 400, variants)
            self.assertIn("variants", r.json())
        self.assertEqual(Product.objects.get(pk=self.product["id"]).options, [])
        self.assertEqual(self.variant_ids(), {self.first})

    def test_ids_prices_and_stock_survive_option_changes(self):
        location = self.client.post("/api/v1/inventory/locations/", {"name": "Sydney"}, format="json").json()["id"]
        r = self.client.post(
            "/api/v1/inventory/items/set/", {"variant": self.first, "location": location, "available": 7}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        data = self.add_pack()
        pack = next(v["id"] for v in data["variants"] if v["id"] != self.first)

        # Rename a value and reorder: both variants keep their ids, prices and stock
        r = self.client.patch(self.url, {
            "options": self.size_options("Pack of 6", "One can"),
            "variants": [
                {"id": self.first, "options": {"Size": "One can"}},
                {"id": pack, "options": {"Size": "Pack of 6"}},
            ],
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        variants = {v["id"]: v for v in r.json()["variants"]}
        self.assertEqual(set(variants), {self.first, pack})
        self.assertEqual(variants[self.first]["price"], "10.00")
        self.assertEqual(variants[self.first]["options"], {"Size": "One can"})
        self.assertEqual(variants[pack]["price"], "30.00")
        self.assertEqual(r.json()["options"], self.size_options("Pack of 6", "One can"))
        self.assertEqual(InventoryItem.objects.get(variant_id=self.first).available, 7)

    def test_option_values_merge_into_metadata(self):
        ProductVariant.objects.filter(pk=self.first).update(metadata={"note": "keep me"})
        self.add_pack()
        self.assertEqual(
            ProductVariant.objects.get(pk=self.first).metadata, {"note": "keep me", "options": {"Size": "Single"}}
        )
        r = self.client.patch(f"{VARIANTS}{self.first}/", {"price": "11"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["options"], {"Size": "Single"})
        self.assertEqual(ProductVariant.objects.get(pk=self.first).metadata["note"], "keep me")

    def test_variants_without_options_keep_working(self):
        r = self.client.patch(
            self.url, {"title": "Cola 375ML", "variants": [{"id": self.first, "price": "12"}]}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["options"], [])
        self.assertEqual(r.json()["variants"][0]["options"], {})
        r = self.client.post(VARIANTS, {"product": self.product["id"], "sku": "C2", "price": "5"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["options"], {})

    def test_single_variant_changes_never_remove_other_variants(self):
        self.add_pack()
        r = self.client.patch(self.url, {"options": self.size_options("Single", "Pack of 6", "Case")}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        r = self.client.post(VARIANTS, {
            "product": self.product["id"], "sku": "C24", "price": "90", "options": {"Size": "Case"},
        }, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        case = r.json()["id"]
        ids = self.variant_ids()
        self.assertEqual(len(ids), 3)

        # Inline and bulk edits go to one variant at a time
        r = self.client.patch(f"{VARIANTS}{self.first}/", {"price": "9.50", "compare_at_price": "12"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.variant_ids(), ids)

        # A duplicate combination on one variant is refused and changes nothing
        r = self.client.patch(f"{VARIANTS}{case}/", {"options": {"Size": "Single"}}, format="json")
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(ProductVariant.objects.get(pk=case).metadata["options"], {"Size": "Case"})

        # Saving the product form without a variant list leaves every variant alone
        r = self.client.patch(self.url, {"title": "Cola range"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.variant_ids(), ids)

        # Deleting one variant deletes only that one
        self.assertEqual(self.client.delete(f"{VARIANTS}{case}/").status_code, 204)
        self.assertEqual(self.variant_ids(), ids - {case})


class IncludeCollectionsTests(APITestCase):
    """"Include products from collections": a collection also shows the products of the ones ticked on it."""

    COLLECTIONS = "/api/v1/catalog/collections/"

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "include@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()
        self.n = 0

    # ------------------------------------------------------------------ helpers
    def product(self, title, **extra):
        self.n += 1
        body = {"title": title, "status": "active", "price": "10", "sku": f"INC-{self.n}",
                "location": self.location["id"], "quantity": 5, **extra}
        r = self.client.post(URL, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def collection(self, name, products=(), include=(), **extra):
        body = {"name": name, "products": [p["id"] for p in products], "include_collections": list(include), **extra}
        r = self.client.post(self.COLLECTIONS, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def save(self, collection, expect=200, **body):
        r = self.client.patch(f"{self.COLLECTIONS}{collection['id']}/", body, format="json")
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def fetch(self, collection):
        return self.client.get(f"{self.COLLECTIONS}{collection['id']}/").json()

    def page(self, collection):
        r = self.client.get(f"/s/{self.store.store_slug}/collections/{collection['handle']}")
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    # ------------------------------------------------------------------ what shoppers see
    def test_ticked_collections_products_show_with_the_collections_own_products(self):
        shiraz = self.collection("Shiraz", [self.product("Barossa Shiraz")])
        pinot = self.collection("Pinot Noir", [self.product("Otago Pinot")])
        self.collection("Rose", [self.product("Provence Rose")])
        red = self.collection("Red Wine", [self.product("House Red")], include=[shiraz["id"], pinot["id"]])

        html = self.page(red)

        for shown in ("House Red", "Barossa Shiraz", "Otago Pinot"):
            self.assertIn(shown, html)
        self.assertNotIn("Provence Rose", html)  # Rose was not ticked

    def test_a_product_in_both_collections_counts_once(self):
        both = self.product("Both")
        shiraz = self.collection("Shiraz", [both])
        red = self.collection("Red Wine", [both, self.product("Only red")], include=[shiraz["id"]])

        self.assertEqual(self.fetch(red)["products_count"], 2)

    def test_a_product_added_to_or_removed_from_a_ticked_collection_updates_this_one(self):
        first, second = self.product("First wine"), self.product("Second wine")
        shiraz = self.collection("Shiraz", [first])
        red = self.collection("Red Wine", include=[shiraz["id"]])
        self.assertIn("First wine", self.page(red))
        self.assertNotIn("Second wine", self.page(red))

        self.save(shiraz, products=[first["id"], second["id"]])
        self.assertIn("Second wine", self.page(red))
        self.assertEqual(self.fetch(red)["products_count"], 2)

        self.save(shiraz, products=[second["id"]])
        html = self.page(red)
        self.assertNotIn("First wine", html)
        self.assertIn("Second wine", html)

    def test_automated_and_manual_collections_can_both_include_others(self):
        acme = self.product("Acme wine", vendor="Acme")
        other = self.product("Other wine", vendor="Other")
        by_rule = self.collection(
            "Acme", type="automated", rules=[{"field": "vendor", "operator": "equals", "value": "Acme"}]
        )
        hand = self.collection("Hand picked", [other])

        auto = self.collection(
            "Auto", type="automated", include=[hand["id"]],
            rules=[{"field": "vendor", "operator": "equals", "value": "Acme"}],
        )
        manual = self.collection("Manual", include=[by_rule["id"]])

        self.assertEqual(self.fetch(auto)["products_count"], 2)
        html = self.page(auto)
        self.assertIn("Acme wine", html)
        self.assertIn("Other wine", html)
        self.assertIn("Acme wine", self.page(manual))
        # Included products are not copied into the collection's own products
        self.assertEqual(self.fetch(manual)["products"], [])
        self.assertEqual(self.fetch(auto)["products"], [acme["id"]])

    def test_included_products_are_never_copied_into_the_hand_picked_ones(self):
        shiraz = self.collection("Shiraz", [self.product("Barossa Shiraz")])
        red = self.collection("Red Wine", [self.product("House Red")], include=[shiraz["id"]])

        saved = self.save(red, name="Red wines")  # the admin page saves again without touching the products

        self.assertEqual(len(saved["products"]), 1)
        self.assertEqual(ProductCollection.objects.filter(collection_id=red["id"]).count(), 1)

    def test_inclusion_is_nested_through_every_level(self):
        barossa = self.collection("Barossa Shiraz", [self.product("In Barossa")])
        shiraz = self.collection("Shiraz", [self.product("In Shiraz")], include=[barossa["id"]])
        red = self.collection("Red Wine", [self.product("In Red")], include=[shiraz["id"]])

        html = self.page(red)

        for shown in ("In Red", "In Shiraz", "In Barossa"):
            self.assertIn(shown, html)
        self.assertEqual(self.fetch(red)["products_count"], 3)
        # The API still lists only what was ticked on Red Wine itself
        self.assertEqual(self.fetch(red)["include_collections"], [shiraz["id"]])

    def test_anything_added_later_at_any_level_shows_up(self):
        barossa = self.collection("Barossa Shiraz")
        shiraz = self.collection("Shiraz", include=[barossa["id"]])
        red = self.collection("Red Wine", include=[shiraz["id"]])
        late = self.product("Added late")
        self.assertNotIn("Added late", self.page(red))

        self.save(barossa, products=[late["id"]])  # two levels down
        self.assertIn("Added late", self.page(red))

        deeper = self.collection("Deeper", [self.product("Deepest")])
        self.save(barossa, include_collections=[deeper["id"]])  # a new level below
        self.assertIn("Deepest", self.page(red))
        self.assertEqual(self.fetch(red)["products_count"], 2)  # Added late, Deepest

        self.save(barossa, include_collections=[])  # and cut off again
        self.assertNotIn("Deepest", self.page(red))

    def test_a_product_reachable_in_several_ways_counts_once(self):
        both = self.product("Everywhere")
        bottom = self.collection("Bottom", [both])
        left = self.collection("Left", [both], include=[bottom["id"]])
        right = self.collection("Right", include=[bottom["id"]])
        top = self.collection("Top", [both], include=[left["id"], right["id"], bottom["id"]])

        self.assertEqual(self.fetch(top)["products_count"], 1)

    def test_a_deleted_collection_in_the_middle_cuts_off_what_is_below_it(self):
        barossa = self.collection("Barossa Shiraz", [self.product("In Barossa")])
        shiraz = self.collection("Shiraz", [self.product("In Shiraz")], include=[barossa["id"]])
        red = self.collection("Red Wine", include=[shiraz["id"]])

        self.assertEqual(self.client.delete(f"{self.COLLECTIONS}{shiraz['id']}/").status_code, 204)

        html = self.page(red)
        self.assertNotIn("In Shiraz", html)
        self.assertNotIn("In Barossa", html)  # only reachable through Shiraz
        self.assertEqual(self.fetch(red)["include_collections"], [])

    def test_nesting_stops_at_the_depth_limit(self):
        from .services import MAX_INCLUDE_DEPTH, collection_source_ids

        chain = [Collection.objects.create(store=self.store, name=f"Level {i}") for i in range(MAX_INCLUDE_DEPTH + 3)]
        for upper, lower in zip(chain, chain[1:]):
            Collection.objects.filter(pk=upper.pk).update(include_collections=[str(lower.pk)])
        top = Collection.objects.get(pk=chain[0].pk)

        sources = collection_source_ids(top)

        self.assertEqual(len(sources), MAX_INCLUDE_DEPTH + 1)  # itself and ten levels below it
        self.assertEqual([str(pk) for pk in sources], [str(c.pk) for c in chain[:MAX_INCLUDE_DEPTH + 1]])

    def test_a_loop_already_in_the_data_does_not_hang_or_repeat(self):
        from .services import collection_source_ids

        a = Collection.objects.create(store=self.store, name="A")
        b = Collection.objects.create(store=self.store, name="B")
        Collection.objects.filter(pk=a.pk).update(include_collections=[str(b.pk)])
        Collection.objects.filter(pk=b.pk).update(include_collections=[str(a.pk)])

        self.assertEqual([str(pk) for pk in collection_source_ids(Collection.objects.get(pk=a.pk))], [str(a.pk), str(b.pk)])
        self.assertEqual(self.client.get(f"{self.COLLECTIONS}{a.pk}/").status_code, 200)

    def test_the_included_collections_are_looked_up_with_one_query_however_deep(self):
        from .services import collection_source_ids

        previous = []
        for level in range(6):
            made = self.collection(f"Level {level}", include=previous)
            previous = [made["id"]]
        top = Collection.objects.get(pk=made["id"])

        with self.assertNumQueries(1):
            self.assertEqual(len(collection_source_ids(top)), 6)
            collection_source_ids(top)  # asked again on the same collection: remembered

    def test_clearing_the_list_stops_including(self):
        shiraz = self.collection("Shiraz", [self.product("Barossa Shiraz")])
        red = self.collection("Red Wine", include=[shiraz["id"]])

        saved = self.save(red, include_collections=[])

        self.assertEqual(saved["include_collections"], [])
        self.assertNotIn("Barossa Shiraz", self.page(red))

    # ------------------------------------------------------------------ the rules
    def test_a_collection_cannot_include_itself(self):
        red = self.collection("Red Wine")

        r = self.client.patch(f"{self.COLLECTIONS}{red['id']}/", {"include_collections": [red["id"]]}, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["include_collections"], ["A collection can't include itself."])
        self.assertEqual(self.fetch(red)["include_collections"], [])

    def test_two_collections_cannot_include_each_other(self):
        a = self.collection("A")
        b = self.collection("B", include=[a["id"]])

        r = self.client.patch(f"{self.COLLECTIONS}{a['id']}/", {"include_collections": [b["id"]]}, format="json")

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["include_collections"], ["A and B can't include each other."])
        self.assertEqual(self.fetch(a)["include_collections"], [])

    def test_a_longer_loop_is_refused_too(self):
        a = self.collection("A")
        b = self.collection("B", include=[a["id"]])
        c = self.collection("C", include=[b["id"]])

        r = self.client.patch(f"{self.COLLECTIONS}{a['id']}/", {"include_collections": [c["id"]]}, format="json")  # A -> C -> B -> A

        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            r.json()["include_collections"],
            ["C already includes A through other collections, so A and C can't include each other."],
        )
        self.assertEqual(self.fetch(a)["include_collections"], [])

    def test_a_list_that_is_not_a_loop_is_accepted(self):
        a, b = self.collection("A"), self.collection("B")
        c = self.collection("C", include=[a["id"]])

        saved = self.save(b, include_collections=[a["id"], c["id"]])  # B -> A and B -> C -> A: no way back to B

        self.assertEqual(sorted(saved["include_collections"]), sorted([a["id"], c["id"]]))

    def test_unknown_and_other_stores_collections_are_refused(self):
        other_client = APIClient()
        other_token, other_store_id = signup(other_client, "include-other@example.com")
        other_client.credentials(HTTP_AUTHORIZATION="Bearer " + other_token, HTTP_X_STORE_ID=other_store_id)
        theirs = other_client.post(self.COLLECTIONS, {"name": "Theirs"}, format="json").json()
        red = self.collection("Red Wine")

        for missing in (theirs["id"], "11111111-1111-1111-1111-111111111111"):
            self.save(red, expect=400, include_collections=[missing])
        self.assertEqual(self.fetch(red)["include_collections"], [])

    # ------------------------------------------------------------------ keeping the list right
    def test_a_deleted_ticked_collection_is_removed_from_the_list(self):
        shiraz = self.collection("Shiraz", [self.product("Barossa Shiraz")])
        pinot = self.collection("Pinot Noir", [self.product("Otago Pinot")])
        red = self.collection("Red Wine", include=[shiraz["id"], pinot["id"]])

        self.assertEqual(self.client.delete(f"{self.COLLECTIONS}{shiraz['id']}/").status_code, 204)

        self.assertEqual(self.fetch(red)["include_collections"], [pinot["id"]])
        self.assertEqual(Collection.objects.get(pk=red["id"]).include_collections, [pinot["id"]])  # cleaned in the database
        html = self.page(red)
        self.assertNotIn("Barossa Shiraz", html)
        self.assertIn("Otago Pinot", html)

    def test_a_stale_id_left_in_the_list_is_ignored(self):
        shiraz = self.collection("Shiraz", [self.product("Barossa Shiraz")])
        red = self.collection("Red Wine", include=[shiraz["id"]])
        ProductCollection.objects.filter(collection_id=shiraz["id"]).delete()
        Collection.objects.filter(pk=shiraz["id"]).delete()  # removed outside the API (e.g. the Django admin)

        self.assertEqual(self.fetch(red)["include_collections"], [])
        self.assertEqual(self.fetch(red)["products_count"], 0)
        self.assertNotIn("Barossa Shiraz", self.page(red))

    def test_the_header_menu_lists_a_collection_that_is_filled_only_through_an_include(self):
        barossa = self.collection("Barossa", [self.product("Barossa Shiraz")])
        shiraz = self.collection("Shiraz", include=[barossa["id"]])
        self.collection("Red Wine", include=[shiraz["id"]])  # filled two levels down
        self.collection("Empty one")

        html = self.client.get(f"/s/{self.store.store_slug}/").content.decode()

        self.assertIn("Red Wine", html)
        self.assertNotIn("Empty one", html)

    # ------------------------------------------------------------------ the item grid on the collection page
    def preview(self, **body):
        r = self.client.post(f"{self.COLLECTIONS}preview/", body, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_grid_lists_products_from_added_collections_nested_and_labelled(self):
        barossa = self.collection("Barossa Shiraz", [self.product("In Barossa")])
        shiraz = self.collection("Shiraz", [self.product("In Shiraz")], include=[barossa["id"]])
        pinot = self.collection("Pinot Noir", [self.product("In Pinot")])
        own = self.product("Own pick")

        data = self.preview(type="manual", products=[own["id"]], include_collections=[shiraz["id"], pinot["id"]])

        self.assertEqual(data["included_count"], 3)
        labels = {p["title"]: p["from_collection"]["name"] for p in data["included"]}
        # A nested product is labelled with the collection that was added, not the one it sits in
        self.assertEqual(labels, {"In Shiraz": "Shiraz", "In Barossa": "Shiraz", "In Pinot": "Pinot Noir"})
        self.assertEqual({p["from_collection"]["id"] for p in data["included"]}, {shiraz["id"], pinot["id"]})

    def test_the_grid_shows_a_product_once_and_keeps_the_collections_own_products_unlabelled(self):
        both = self.product("Both")
        a = self.collection("A", [both, self.product("Only A")])
        b = self.collection("B", [both])

        data = self.preview(type="manual", products=[both["id"]], include_collections=[a["id"], b["id"]])

        # "Both" is the collection's own pick, so only "Only A" comes from outside
        self.assertEqual([p["title"] for p in data["included"]], ["Only A"])
        self.assertEqual(data["included_count"], 1)

        # Not picked by hand: it comes once, through the first collection that was added
        data = self.preview(type="manual", products=[], include_collections=[b["id"], a["id"]])
        self.assertEqual(data["included_count"], 2)
        by_title = {p["title"]: p["from_collection"]["name"] for p in data["included"]}
        self.assertEqual(by_title, {"Both": "B", "Only A": "A"})

    def test_the_grid_for_an_automated_collection_adds_included_products_to_the_matching_ones(self):
        acme = self.product("Acme wine", vendor="Acme")
        hand = self.collection("Hand picked", [self.product("Other wine"), acme])

        data = self.preview(
            type="automated", rules=[{"field": "vendor", "operator": "equals", "value": "Acme"}],
            include_collections=[hand["id"]],
        )

        self.assertEqual([p["title"] for p in data["products"]], ["Acme wine"])
        self.assertEqual(data["count"], 1)
        # Acme wine is already matched by the rule, so only the other product is "from Hand picked"
        self.assertEqual([(p["title"], p["from_collection"]["name"]) for p in data["included"]], [("Other wine", "Hand picked")])
        self.assertEqual(data["included_count"], 1)

    def test_the_grid_follows_an_unsaved_change_and_copies_nothing(self):
        shiraz = self.collection("Shiraz", [self.product("In Shiraz")])
        red = self.collection("Red Wine")
        before = ProductCollection.objects.count()

        added = self.preview(type="manual", products=[], include_collections=[shiraz["id"]])
        removed = self.preview(type="manual", products=[], include_collections=[])

        self.assertEqual((added["included_count"], removed["included_count"]), (1, 0))
        self.assertEqual(removed["included"], [])
        self.assertEqual(ProductCollection.objects.count(), before)
        self.assertEqual(self.fetch(red)["include_collections"], [])  # nothing is saved until Save

    def test_the_grid_ignores_deleted_collections_and_refuses_bad_ids(self):
        shiraz = self.collection("Shiraz", [self.product("In Shiraz")])
        gone = "11111111-1111-1111-1111-111111111111"

        data = self.preview(type="manual", products=[], include_collections=[gone, shiraz["id"]])
        self.assertEqual([p["title"] for p in data["included"]], ["In Shiraz"])

        for body in ({"include_collections": ["nope"]}, {"include_collections": "x"}, {"products": ["nope"], "type": "manual"}):
            r = self.client.post(f"{self.COLLECTIONS}preview/", body, format="json")
            self.assertEqual(r.status_code, 400, body)

    def test_the_grid_only_sees_this_stores_collections(self):
        other_client = APIClient()
        other_token, other_store_id = signup(other_client, "preview-other@example.com")
        other_client.credentials(HTTP_AUTHORIZATION="Bearer " + other_token, HTTP_X_STORE_ID=other_store_id)
        theirs = other_client.post(self.COLLECTIONS, {"name": "Theirs"}, format="json").json()

        data = self.preview(type="manual", products=[], include_collections=[theirs["id"]])

        self.assertEqual((data["included"], data["included_count"]), ([], 0))

    def test_the_list_of_other_collections_is_the_stores_current_collections(self):
        # The admin page lists /collections/ minus the one being edited, so a new collection shows up at once
        a = self.collection("A")
        self.assertEqual([c["id"] for c in self.client.get(self.COLLECTIONS).json()], [a["id"]])
        b = self.collection("B")
        self.assertEqual({c["id"] for c in self.client.get(self.COLLECTIONS).json()}, {a["id"], b["id"]})


class CollectionChannelTests(APITestCase):
    """The Online Store toggle on a collection shows, and keeps, what is saved on the collection itself."""

    COLLECTIONS = "/api/v1/catalog/collections/"

    def setUp(self):
        from apps.tenancy.models import Store

        token, self.store_id = signup(self.client, "channels@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        self.channels = {c["type"]: c["id"] for c in self.client.get("/api/v1/catalog/sales-channels/").json()}
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()

    def product(self, title):
        r = self.client.post(URL, {"title": title, "status": "active", "price": "10", "sku": title.upper(),
                                   "location": self.location["id"], "quantity": 5}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def collection(self, name, **extra):
        r = self.client.post(self.COLLECTIONS, {"name": name, **extra}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def fetch(self, collection):
        return self.client.get(f"{self.COLLECTIONS}{collection['id']}/").json()

    def storefront(self, collection):
        return self.client.get(f"/s/{self.store.store_slug}/collections/{collection['handle']}").status_code

    def saved_types(self, collection):
        return sorted(CollectionSalesChannel.objects.filter(collection_id=collection["id"]).values_list("sales_channel__type", flat=True))

    def test_a_collection_with_no_saved_channel_shows_none_even_when_its_products_are_on_the_online_store(self):
        product = self.product("Lager")
        beer = self.collection("Beer & Cider", sales_channels=[], products=[product["id"]])
        self.assertEqual(self.saved_types(beer), [])

        shown = self.fetch(beer)

        self.assertEqual((shown["sales_channels"], shown["sales_channels_count"]), ([], 0))
        listed = next(c for c in self.client.get(self.COLLECTIONS).json() if c["id"] == beer["id"])
        self.assertEqual(listed["sales_channels"], [])
        self.assertEqual(self.storefront(beer), 404)  # what the page shows is what shoppers get

    def test_turning_the_online_store_off_stays_off_after_a_reload(self):
        beer = self.collection("Beer", products=[self.product("Lager")["id"]])
        self.assertEqual(self.saved_types(beer), ["online"])  # a new collection goes on the Online Store

        saved = self.client.patch(f"{self.COLLECTIONS}{beer['id']}/", {"sales_channels": []}, format="json").json()

        self.assertEqual(saved["sales_channels"], [])
        self.assertEqual(self.fetch(beer)["sales_channels"], [])  # not switched back on by its products
        self.assertEqual(self.saved_types(beer), [])
        self.assertEqual(self.storefront(beer), 404)

    def test_turning_the_online_store_on_really_saves_the_channel(self):
        beer = self.collection("Beer", sales_channels=[])
        self.assertEqual(self.storefront(beer), 404)

        saved = self.client.patch(
            f"{self.COLLECTIONS}{beer['id']}/", {"sales_channels": [self.channels["online"]]}, format="json"
        ).json()

        self.assertEqual([c["type"] for c in saved["sales_channels"]], ["online"])
        self.assertEqual(self.saved_types(beer), ["online"])
        self.assertEqual([c["type"] for c in self.fetch(beer)["sales_channels"]], ["online"])
        self.assertEqual(self.storefront(beer), 200)

    def test_saving_without_the_channels_leaves_them_alone_and_other_collections_are_not_touched(self):
        on = self.collection("On shelf")
        off = self.collection("Off shelf", sales_channels=[])

        self.client.patch(f"{self.COLLECTIONS}{on['id']}/", {"name": "Renamed"}, format="json")
        self.client.patch(f"{self.COLLECTIONS}{off['id']}/", {"name": "Renamed off"}, format="json")

        self.assertEqual(self.saved_types(on), ["online"])
        self.assertEqual(self.saved_types(off), [])

    def test_a_collection_can_be_on_several_channels_and_shows_them_in_order(self):
        both = self.collection("Both", sales_channels=[self.channels["pos"], self.channels["online"]])

        self.assertEqual(sorted(c["type"] for c in self.fetch(both)["sales_channels"]), ["online", "pos"])
        self.assertEqual(self.fetch(both)["sales_channels_count"], 2)


class DuplicateCollectionTitleTests(APITestCase):
    """Several collections may share a title ("Whisky"); only the handle (the address) is unique per store."""

    COLLECTIONS = "/api/v1/catalog/collections/"

    def setUp(self):
        token, self.store_id = signup(self.client, "dupes@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)

    def create(self, name, expect=201, **extra):
        r = self.client.post(self.COLLECTIONS, {"name": name, **extra}, format="json")
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def patch(self, collection, expect=200, **body):
        r = self.client.patch(f"{self.COLLECTIONS}{collection['id']}/", body, format="json")
        self.assertEqual(r.status_code, expect, r.content)
        return r.json()

    def test_the_same_title_can_be_used_again_and_gets_the_next_free_handle(self):
        made = [self.create("Whisky") for _ in range(3)]

        self.assertEqual([c["name"] for c in made], ["Whisky"] * 3)
        self.assertEqual([c["handle"] for c in made], ["whisky", "whisky-1", "whisky-2"])
        self.assertEqual(len({c["id"] for c in made}), 3)

    def test_titles_match_regardless_of_case_and_spacing(self):
        self.create("Whisky")

        self.assertEqual(self.create("  WHISKY ")["handle"], "whisky-1")

    def test_a_blank_handle_is_generated_like_a_missing_one(self):
        self.create("Whisky")

        self.assertEqual(self.create("Whisky", handle="")["handle"], "whisky-1")

    def test_a_freed_handle_is_used_again(self):
        first, second = self.create("Whisky"), self.create("Whisky")
        self.client.delete(f"{self.COLLECTIONS}{first['id']}/")

        self.assertEqual(self.create("Whisky")["handle"], "whisky")
        self.assertEqual(second["handle"], "whisky-1")

    def test_generated_handles_skip_ones_taken_by_hand(self):
        self.create("Gin", handle="whisky-1")
        self.create("Whisky")

        self.assertEqual(self.create("Whisky")["handle"], "whisky-2")

    def test_a_handle_typed_by_hand_must_still_be_unique(self):
        self.create("Whisky")

        r = self.create("Whisky", expect=400, handle="whisky")

        self.assertEqual(r["handle"], ["Another collection already uses this URL handle."])
        self.assertEqual(self.create("Whisky", handle="single-malt")["handle"], "single-malt")

    def test_the_handle_can_be_edited_but_not_onto_another_collections(self):
        first, second = self.create("Whisky"), self.create("Whisky")

        self.assertEqual(self.patch(second, handle="scotch")["handle"], "scotch")
        self.patch(second, expect=400, handle=first["handle"])
        self.assertEqual(self.patch(first, name="Whisky")["handle"], "whisky")  # saving it again keeps its own

    def test_renaming_to_a_title_in_use_keeps_the_handle(self):
        self.create("Whisky")
        gin = self.create("Gin")

        renamed = self.patch(gin, name="Whisky")

        self.assertEqual((renamed["name"], renamed["handle"]), ("Whisky", "gin"))

    def test_another_store_may_use_the_same_title_and_handle(self):
        self.create("Whisky")
        token, other_id = signup(self.client, "dupes-b@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)

        self.assertEqual(self.create("Whisky")["handle"], "whisky")

    def test_each_same_titled_collection_opens_by_its_own_handle_and_id(self):
        from apps.tenancy.models import Store

        first, second = self.create("Whisky"), self.create("Whisky")
        slug = Store.objects.get(pk=self.store_id).store_slug

        for collection in (first, second):
            r = self.client.get(f"{self.COLLECTIONS}{collection['id']}/")
            self.assertEqual(r.json()["handle"], collection["handle"])
            page = self.client.get(f"/s/{slug}/collections/{collection['handle']}")
            self.assertEqual(page.status_code, 200)
        self.assertEqual(Collection.objects.filter(store_id=self.store_id, name="Whisky").count(), 2)

    def test_the_list_gives_every_collection_its_handle(self):
        for _ in range(2):
            self.create("Whisky")

        rows = self.client.get(self.COLLECTIONS).json()
        rows = rows["results"] if isinstance(rows, dict) else rows

        self.assertEqual(sorted(r["handle"] for r in rows), ["whisky", "whisky-1"])
