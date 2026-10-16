"""
Inventory API tests.

    python manage.py test apps.inventory --settings=config.test_settings
"""
import uuid
import copy
from unittest import mock

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.inventory.models import Transfer
from apps.inventory.views import TransferViewSet

ITEMS = "/api/v1/inventory/items/"


class InventoryTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.sign_up("stock@example.com", "Stock Store")
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Tee", "status": "active", "price": "500", "sku": "TEE-1",
        }, expect=201)
        self.product = product["id"]
        self.variant = product["variants"][0]["id"]
        self.warehouse = self.ok("post", "/api/v1/inventory/locations/", {"name": "Warehouse"}, expect=201)["id"]

    def set_stock(self, available, location=None):
        return self.ok("post", f"{ITEMS}set/", {
            "variant": self.variant, "location": location or self.warehouse, "available": available,
        })

    def test_set_creates_and_updates_stock(self):
        row = self.set_stock(10)
        self.assertEqual((row["available"], row["on_hand"], row["committed"]), (10, 10, 0))
        self.assertEqual(row["sku"], "TEE-1")
        self.assertEqual(row["product"], self.product)

        row = self.set_stock(4)
        self.assertEqual((row["available"], row["on_hand"]), (4, 4))
        self.assertEqual(len(self.ok("get", ITEMS)), 1)  # updated in place, not duplicated

    def test_list_filters_and_retrieve(self):
        row = self.set_stock(3)
        other = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]
        self.set_stock(2, location=other)

        self.assertEqual(len(self.ok("get", f"{ITEMS}?variant={self.variant}")), 2)
        self.assertEqual(len(self.ok("get", f"{ITEMS}?location={other}")), 1)
        self.assertEqual(len(self.ok("get", f"{ITEMS}?product={self.product}")), 2)
        self.assertEqual(self.ok("get", f"{ITEMS}{row['id']}/")["available"], 3)

    def test_adjust_adds_and_removes(self):
        self.set_stock(5)
        row = self.ok("post", f"{ITEMS}adjust/", {
            "variant": self.variant, "location": self.warehouse, "delta": 7,
        })
        self.assertEqual((row["available"], row["on_hand"]), (12, 12))
        row = self.ok("post", f"{ITEMS}adjust/", {
            "variant": self.variant, "location": self.warehouse, "delta": -2,
        })
        self.assertEqual(row["available"], 10)

    def test_invalid_quantities_are_rejected(self):
        self.set_stock(5)
        base = {"variant": self.variant, "location": self.warehouse}
        self.ok("post", f"{ITEMS}set/", {**base, "available": -1}, expect=400)
        self.ok("post", f"{ITEMS}set/", {**base, "available": "lots"}, expect=400)
        self.ok("post", f"{ITEMS}adjust/", {**base, "delta": -6}, expect=400)  # more than available
        self.ok("post", f"{ITEMS}adjust/", {**base, "delta": 0}, expect=400)
        self.ok("post", f"{ITEMS}adjust/", {**base, "available": 5, "delta": 1}, expect=400)
        self.ok("post", f"{ITEMS}set/", base, expect=400)  # quantity missing
        self.assertEqual(self.ok("get", ITEMS)[0]["available"], 5)  # nothing changed

    def test_missing_variant_or_location_is_rejected(self):
        ghost = str(uuid.uuid4())
        self.ok("post", f"{ITEMS}set/", {
            "variant": ghost, "location": self.warehouse, "available": 1,
        }, expect=400)
        self.ok("post", f"{ITEMS}set/", {
            "variant": self.variant, "location": ghost, "available": 1,
        }, expect=400)
        self.ok("post", f"{ITEMS}set/", {"location": self.warehouse, "available": 1}, expect=400)
        self.ok("get", f"{ITEMS}{ghost}/", expect=404)

    def test_stock_cannot_be_edited_directly(self):
        row = self.set_stock(5)
        self.ok("post", ITEMS, {"variant": self.variant, "location": self.warehouse}, expect=400)
        self.ok("put", f"{ITEMS}{row['id']}/", {"available": 99}, expect=405)
        self.ok("delete", f"{ITEMS}{row['id']}/", expect=405)

    def test_other_stores_cannot_see_or_use_this_stock(self):
        row = self.set_stock(5)
        self.sign_up("rival@example.com", "Rival Store")
        self.assertEqual(self.ok("get", ITEMS), [])
        self.ok("get", f"{ITEMS}{row['id']}/", expect=404)
        self.ok("post", f"{ITEMS}set/", {
            "variant": self.variant, "location": self.warehouse, "available": 1,
        }, expect=400)

    def test_transfer_moves_stock_and_checks_availability(self):
        self.set_stock(5)
        shop = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]

        def transfer(quantity, source=self.warehouse, destination=shop, expect=201):
            return self.ok("post", "/api/v1/inventory/transfers/", {
                "source_location": source, "destination_location": destination,
                "items": [{"variant": self.variant, "quantity": quantity}],
            }, expect=expect)

        transfer(1, destination=self.warehouse, expect=400)  # same location
        transfer(0, expect=400)

        too_many = transfer(6)  # creating is allowed; completing is not
        self.ok("post", f"/api/v1/inventory/transfers/{too_many['id']}/complete/", expect=400)
        self.assertEqual(self.ok("get", ITEMS)[0]["available"], 5)

        good = transfer(2)
        done = self.ok("post", f"/api/v1/inventory/transfers/{good['id']}/complete/")
        self.assertEqual(done["status"], "completed")
        self.ok("post", f"/api/v1/inventory/transfers/{good['id']}/complete/", expect=400)  # only once
        self.ok("post", f"/api/v1/inventory/transfers/{good['id']}/cancel/", expect=400)

        by_location = {r["location"]: r["available"] for r in self.ok("get", ITEMS)}
        self.assertEqual(by_location, {self.warehouse: 3, shop: 2})

    def test_two_requests_that_both_read_the_old_status_move_stock_once(self):
        self.set_stock(20)
        shop = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]
        made = self.ok("post", "/api/v1/inventory/transfers/", {
            "source_location": self.warehouse, "destination_location": shop,
            "items": [{"variant": self.variant, "quantity": 10}],
        }, expect=201)
        url = f"/api/v1/inventory/transfers/{made['id']}/complete/"
        stale = Transfer.objects.get(pk=made["id"])  # each request gets its own copy of this old row; no request sees the other's change in memory
        #: status "pending"

        with mock.patch.object(TransferViewSet, "get_object", lambda view: copy.copy(stale)):
            self.ok("post", url)
            self.ok("post", url, expect=400)  # still sees the old status, but the locked row says completed

        by_location = {r["location"]: r["available"] for r in self.ok("get", ITEMS)}
        self.assertEqual(by_location, {self.warehouse: 10, shop: 10})
