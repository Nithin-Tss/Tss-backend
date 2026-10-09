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
        self.ok("post", "/api/v1/catalog/products/", {"title": "Only in first", "price": "5"}, expect=201)
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

    def test_cannot_touch_someone_elses_store(self):
        _, store_a = self.sign_up("a@example.com", "Store A")
        self.sign_up("b@example.com", "Store B")
        url = f"/api/v1/stores/{store_a['storeId']}/"

        self.ok("get", url, expect=404)
        self.ok("patch", url, {"storeName": "Mine now"}, expect=404)
        self.ok("delete", url, expect=404)
