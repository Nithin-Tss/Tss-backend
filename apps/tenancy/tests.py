"""
Multi-store tests.

    python manage.py test apps.tenancy --settings=config.test_settings
"""
from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin

from .models import Store, StoreMembership


class MultiStoreTest(StoreClientMixin, APITestCase):
    def test_one_user_many_stores(self):
        token, first = self.sign_up("owner@example.com", "First store")
        second = self.ok("post", "/api/v1/stores/", {"storeName": "Second store"}, expect=201)

        listed = self.ok("get", "/api/v1/stores/")
        self.assertEqual([s["storeName"] for s in listed], ["First store", "Second store"])
        self.assertEqual({s["role"] for s in listed}, {"owner"})

        # Each store only sees its own products
        self.ok("post", "/api/v1/catalog/products/", {"title": "Only in first", "sku": "SKU-ONLY-IN-FIRST", "price": "5"}, expect=201)
        self.use(token, second["storeId"])
        self.assertEqual(self.ok("get", "/api/v1/catalog/products/"), [])
        self.use(token, first["storeId"])
        self.assertEqual(len(self.ok("get", "/api/v1/catalog/products/")), 1)

    def test_rename_and_close(self):
        token, store = self.sign_up("owner@example.com", "Old name")
        url = f"/api/v1/stores/{store['storeId']}/"

        renamed = self.ok("patch", url, {"storeName": "  New name  "})
        self.assertEqual(renamed["storeName"], "New name")
        self.ok("patch", url, {"storeName": " "}, expect=400)

        self.ok("delete", url, expect=204)
        self.assertEqual(self.ok("get", "/api/v1/stores/"), [])
        self.assertEqual(Store.objects.get(pk=store["storeId"]).status, "closed")

        # The closed store's data can't be reached any more
        self.ok("get", "/api/v1/catalog/products/", expect=403)

    def test_only_owner_can_rename_or_close(self):
        _, store = self.sign_up("owner@example.com", "Owned")
        url = f"/api/v1/stores/{store['storeId']}/"

        staff_token, _ = self.sign_up("staff@example.com", "Staff's own")
        StoreMembership.objects.create(
            user=get_user_model().objects.get(email="staff@example.com"),
            store_id=store["storeId"], role="staff", status="active",
        )
        self.use(staff_token, store["storeId"])

        self.ok("patch", url, {"storeName": "Hijacked"}, expect=403)
        self.ok("delete", url, expect=403)
        self.assertEqual(Store.objects.get(pk=store["storeId"]).store_name, "Owned")

    def make_admin(self, email):
        get_user_model().objects.filter(email=email).update(is_owner=True)

    def test_new_store_has_default_settings(self):
        _, store = self.sign_up("owner@example.com", "Defaults")
        self.assertEqual(store["currency"], "INR")
        self.assertEqual(store["unitSystem"], "metric")
        self.assertEqual(store["weightUnit"], "kg")
        self.assertEqual(store["orderPrefix"], "#")
        self.assertEqual(store["autoFulfill"], "gift_cards")
        self.assertTrue(store["autoArchive"])
        self.assertEqual(store["contactEmail"], "")

    def test_update_settings(self):
        _, store = self.sign_up("owner@example.com", "Settings")
        url = f"/api/v1/stores/{store['storeId']}/"

        saved = self.ok("patch", url, {
            "contactEmail": "hello@shop.test", "phone": "+91 98765 43210",
            "address": "  1  Main   Road ", "currency": "aud", "timezone": "Australia/Melbourne",
            "unitSystem": "imperial", "weightUnit": "lb", "backupRegion": "Australia",
            "orderPrefix": "SO-", "orderSuffix": "-X", "autoFulfill": "none", "autoArchive": False,
        })
        self.assertEqual(saved["currency"], "AUD")
        self.assertEqual(saved["address"], "1 Main Road")
        self.assertEqual(self.ok("get", url)["timezone"], "Australia/Melbourne")
        self.assertFalse(self.ok("get", url)["autoArchive"])

    def test_settings_are_validated(self):
        _, store = self.sign_up("owner@example.com", "Validation")
        url = f"/api/v1/stores/{store['storeId']}/"

        for bad in (
            {"contactEmail": "nope"}, {"phone": "abc"}, {"currency": "RUPEES"},
            {"timezone": "Mars/Base"}, {"unitSystem": "cubits"}, {"weightUnit": "stone"},
            {"autoFulfill": "sometimes"}, {"orderPrefix": "A1"}, {"orderSuffix": "9"},
        ):
            self.ok("patch", url, bad, expect=400)

    def test_order_numbers_follow_prefix_and_suffix(self):
        _, store = self.sign_up("owner@example.com", "Numbers")
        self.ok("patch", f"/api/v1/stores/{store['storeId']}/", {"orderPrefix": "SO-", "orderSuffix": "-X"})

        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Tee", "status": "active", "price": "100", "sku": "TEE-1",
        }, expect=201)
        numbers = [
            self.ok("post", "/api/v1/orders/", {
                "items": [{"product": product["id"], "quantity": 1}],
            }, expect=201)["order_number"]
            for _ in range(2)
        ]
        self.assertEqual(numbers, ["SO-1001-X", "SO-1002-X"])

    def test_platform_admin_manages_any_store(self):
        _, store = self.sign_up("owner@example.com", "Someone's store")
        admin_token, _ = self.sign_up("admin@example.com", "Admin's own")
        self.make_admin("admin@example.com")
        self.use(admin_token, store["storeId"])
        url = f"/api/v1/stores/{store['storeId']}/"

        self.assertEqual(self.ok("patch", url, {"currency": "usd"})["currency"], "USD")
        self.ok("delete", url, expect=204)

    def test_only_platform_admin_reopens_a_store(self):
        owner_token, store = self.sign_up("owner@example.com", "Closed shop")
        url = f"/api/v1/stores/{store['storeId']}/"
        self.ok("delete", url, expect=204)

        # The owner of a closed store can't reopen it
        self.ok("post", url + "reopen/", expect=403)

        admin_token, _ = self.sign_up("admin@example.com", "Admin's own")
        self.make_admin("admin@example.com")
        reopened = self.ok("post", url + "reopen/")
        self.assertEqual(reopened["status"], "active")

        # ...and the owner has their access back
        self.use(owner_token, store["storeId"])
        self.assertEqual(self.ok("get", url)["storeName"], "Closed shop")
        self.ok("patch", url, {"phone": "12345"})

    def test_staff_cannot_change_settings_whatever_their_role(self):
        _, store = self.sign_up("owner@example.com", "Owned")
        url = f"/api/v1/stores/{store['storeId']}/"
        staff_token, _ = self.sign_up("staff@example.com", "Staff's own")
        StoreMembership.objects.create(
            user=get_user_model().objects.get(email="staff@example.com"),
            store_id=store["storeId"], role="staff", status="active",
        )
        self.use(staff_token, store["storeId"])

        self.assertEqual(self.ok("get", url)["storeName"], "Owned")  # can look
        self.ok("patch", url, {"currency": "USD"}, expect=403)
        self.ok("delete", url, expect=403)
        self.assertEqual(Store.objects.get(pk=store["storeId"]).currency, "INR")

    def test_cannot_touch_someone_elses_store(self):
        _, store_a = self.sign_up("a@example.com", "Store A")
        self.sign_up("b@example.com", "Store B")
        url = f"/api/v1/stores/{store_a['storeId']}/"

        self.ok("get", url, expect=404)
        self.ok("patch", url, {"storeName": "Mine now"}, expect=404)
        self.ok("patch", url, {"currency": "USD"}, expect=404)
        self.ok("delete", url, expect=404)
        self.ok("post", url + "reopen/", expect=403)
