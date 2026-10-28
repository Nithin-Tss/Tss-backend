"""
Inventory API tests.

    python manage.py test apps.inventory --settings=config.test_settings
"""
import uuid
import copy
from unittest import mock

from rest_framework.test import APITestCase

from apps.catalog.models import ProductVariant
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

    def availability(self, expect=200, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.ok("get", f"{ITEMS}availability/?{query}", expect=expect)

    def test_availability_status_boundaries(self):
        for quantity, status, in_stock in (
            (0, "OUT_OF_STOCK", False), (1, "LOW_STOCK", True), (5, "LOW_STOCK", True),
            (6, "IN_STOCK", True), (50, "IN_STOCK", True),
        ):
            self.set_stock(quantity)
            body = self.availability(variant=self.variant)
            self.assertEqual((body["quantity"], body["status"], body["inStock"]), (quantity, status, in_stock))
            self.assertEqual(
                body["variants"],
                [{
                    "variantId": self.variant, "tracked": True, "inStock": in_stock,
                    "quantity": quantity, "status": status, "continueSelling": False,
                }],
            )

    def test_availability_sums_locations_and_filters_by_location(self):
        shop = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]
        self.set_stock(2)
        self.set_stock(3, location=shop)

        body = self.availability(variant=self.variant)
        self.assertEqual((body["quantity"], body["status"]), (5, "LOW_STOCK"))
        self.assertEqual(len(body["variants"]), 1)

        body = self.availability(variant=self.variant, location=shop)
        self.assertEqual((body["quantity"], body["status"]), (3, "LOW_STOCK"))

    def test_availability_by_product_lists_each_variant(self):
        self.set_stock(4)
        body = self.availability(product=self.product)
        self.assertEqual(body["quantity"], 4)
        self.assertEqual([v["variantId"] for v in body["variants"]], [self.variant])

    def test_availability_reflects_adjustments(self):
        self.set_stock(10)
        self.assertEqual(self.availability(variant=self.variant)["status"], "IN_STOCK")
        self.ok("post", f"{ITEMS}adjust/", {"variant": self.variant, "location": self.warehouse, "delta": -7})
        body = self.availability(variant=self.variant)
        self.assertEqual((body["quantity"], body["status"]), (3, "LOW_STOCK"))
        self.ok("post", f"{ITEMS}adjust/", {"variant": self.variant, "location": self.warehouse, "delta": -3})
        body = self.availability(variant=self.variant)
        self.assertEqual((body["quantity"], body["status"], body["inStock"]), (0, "OUT_OF_STOCK", False))

    def test_availability_bad_requests(self):
        self.set_stock(5)
        self.availability(expect=400)  # neither variant nor product
        self.availability(location=self.warehouse, expect=400)
        self.availability(variant="not-a-uuid", expect=400)
        self.availability(product="nope", expect=400)
        self.availability(variant=self.variant, location="nope", expect=400)
        body = self.availability(variant=str(uuid.uuid4()), expect=404)
        self.assertEqual(body["detail"], "No inventory found.")

    def test_availability_untracked_variant_is_in_stock(self):
        # no stock rows at all: orders never block it, so it is not "not found"
        body = self.availability(variant=self.variant)
        self.assertEqual((body["tracked"], body["inStock"], body["quantity"], body["status"]),
                         (False, True, None, "IN_STOCK"))
        self.assertEqual(body["variants"][0]["tracked"], False)
        self.assertEqual(body["variants"][0]["quantity"], None)

    def test_availability_continue_selling_keeps_real_quantity(self):
        ProductVariant.objects.filter(pk=self.variant).update(continue_selling=True)
        self.set_stock(0)
        body = self.availability(variant=self.variant)
        self.assertEqual((body["quantity"], body["status"], body["inStock"]), (0, "OUT_OF_STOCK", True))
        self.assertEqual(
            (body["variants"][0]["tracked"], body["variants"][0]["continueSelling"]), (True, True),
        )

        self.set_stock(3)
        body = self.availability(variant=self.variant)
        self.assertEqual((body["quantity"], body["status"], body["inStock"]), (3, "LOW_STOCK", True))

    def test_availability_quantity_is_clamped_when_sold_past_zero(self):
        ProductVariant.objects.filter(pk=self.variant).update(continue_selling=True)
        self.set_stock(0)
        self.ok("post", f"{ITEMS}adjust/", {"variant": self.variant, "location": self.warehouse, "delta": -2})
        body = self.availability(variant=self.variant)
        self.assertEqual((body["quantity"], body["inStock"]), (0, True))

    def test_availability_location_filter_on_a_tracked_variant_without_a_row_there(self):
        shop = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]
        self.set_stock(8)
        body = self.availability(variant=self.variant, location=shop)
        self.assertEqual((body["tracked"], body["quantity"], body["status"], body["inStock"]),
                         (True, 0, "OUT_OF_STOCK", False))

    def test_availability_by_product_covers_every_variant(self):
        extra = ProductVariant.objects.create(product_id=self.product, sku="TEE-2", price="500").pk
        self.set_stock(0)  # TEE-1 tracked and sold out, TEE-2 not tracked
        body = self.availability(product=self.product)
        by_id = {v["variantId"]: v for v in body["variants"]}
        self.assertEqual(set(by_id), {self.variant, str(extra)})
        self.assertEqual((by_id[self.variant]["inStock"], by_id[self.variant]["status"]), (False, "OUT_OF_STOCK"))
        self.assertEqual((by_id[str(extra)]["tracked"], by_id[str(extra)]["inStock"]), (False, True))
        self.assertTrue(body["inStock"])  # any variant that can be sold
        self.assertEqual((body["quantity"], body["status"]), (0, "IN_STOCK"))

        self.set_stock(0, location=self.warehouse)
        ProductVariant.objects.filter(pk=extra).delete()
        body = self.availability(product=self.product)
        self.assertEqual((body["inStock"], body["status"]), (False, "OUT_OF_STOCK"))

    def test_availability_is_hidden_from_other_stores(self):
        self.set_stock(5)
        self.sign_up("rival@example.com", "Rival Store")
        self.availability(variant=self.variant, expect=404)
        self.availability(product=self.product, expect=404)

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


class InventoryResourceApiTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, store = self.sign_up("inventory-resources@example.com", "Inventory resources")
        self.store_id = store["storeId"]
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Mug", "sku": "MUG-1", "price": "5",
        }, expect=201)
        self.product = product["id"]
        self.variant = product["variants"][0]["id"]
        self.location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]

    def test_locations_list_retrieve_update_delete_and_validation(self):
        loc = self.ok("post", "/api/v1/inventory/locations/", {"name": "  Back room  "}, expect=201)
        self.assertEqual(loc["name"], "Back room")
        self.assertIn(loc["id"], [x["id"] for x in self.ok("get", "/api/v1/inventory/locations/")])
        self.assertEqual(self.ok("get", f"/api/v1/inventory/locations/{loc['id']}/")["name"], "Back room")
        self.assertEqual(self.ok("patch", f"/api/v1/inventory/locations/{loc['id']}/", {"name": "Stock room"})["name"], "Stock room")
        self.ok("post", "/api/v1/inventory/locations/", {"name": "   "}, expect=400)
        self.ok("delete", f"/api/v1/inventory/locations/{loc['id']}/", expect=204)

    def test_history_breakdown_zero_stock_and_rejects_below_zero(self):
        row = self.ok("post", f"{ITEMS}set/", {
            "variant": self.variant, "location": self.location, "available": 2,
        })
        self.assertEqual((row["available"], row["on_hand"]), (2, 2))
        below_zero = self.client.post(f"{ITEMS}adjust/", {
            "variant": self.variant, "location": self.location, "delta": -3,
        }, format="json")
        self.assertEqual(below_zero.status_code, 400)
        self.assertIn("available", str(below_zero.json()).lower())
        zeroed = self.ok("post", f"{ITEMS}adjust/", {
            "variant": self.variant, "location": self.location, "delta": -2,
        })
        self.assertEqual((zeroed["available"], zeroed["on_hand"]), (0, 0))
        current = self.ok("get", f"{ITEMS}{row['id']}/")
        self.assertEqual((current["available"], current["on_hand"]), (0, 0))
        history = self.ok("get", f"{ITEMS}history/?variant={self.variant}&limit=1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["after"], 0)
        breakdown = self.ok("get", f"{ITEMS}{row['id']}/breakdown/")
        self.assertEqual(breakdown["committed_total"], 0)

    def test_transfer_list_retrieve_cancel_and_reject_invalid_input(self):
        dest = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]
        payload = {"source_location": self.location, "destination_location": dest,
                   "items": [{"variant": self.variant, "quantity": 1}]}
        same = self.ok("post", "/api/v1/inventory/transfers/", {
            **payload, "destination_location": self.location,
        }, expect=400)
        self.assertTrue(same)
        self.ok("post", "/api/v1/inventory/transfers/", {
            **payload, "items": [{"variant": self.variant, "quantity": 1.5}],
        }, expect=400)
        made = self.ok("post", "/api/v1/inventory/transfers/", payload, expect=201)
        self.assertEqual(made["status"], "pending")
        self.assertIn(made["id"], [x["id"] for x in self.ok("get", "/api/v1/inventory/transfers/")])
        self.assertEqual(self.ok("get", f"/api/v1/inventory/transfers/{made['id']}/")["id"], made["id"])
        cancelled = self.ok("post", f"/api/v1/inventory/transfers/{made['id']}/cancel/")
        self.assertEqual(cancelled["status"], "cancelled")
        self.ok("post", f"/api/v1/inventory/transfers/{made['id']}/cancel/", expect=400)
        self.ok("post", f"/api/v1/inventory/transfers/{made['id']}/complete/", expect=400)

    def test_inventory_permissions_authentication_and_store_isolation(self):
        row = self.ok("post", f"{ITEMS}set/", {
            "variant": self.variant, "location": self.location, "available": 4,
        })
        role = self.ok("post", "/api/v1/roles/", {"name": "No inventory", "permissions": []}, expect=201)
        member_token, _ = self.sign_up("inventory-member@example.com", "Member shop")
        self.use(self.token, self.store_id)
        self.ok("post", "/api/v1/members/", {"email": "inventory-member@example.com", "roleIds": [role["id"]]}, expect=201)
        self.use(member_token, self.store_id)
        self.ok("get", "/api/v1/inventory/locations/", expect=403)
        self.ok("post", f"{ITEMS}adjust/", {
            "variant": self.variant, "location": self.location, "delta": 1,
        }, expect=403)
        self.ok("get", "/api/v1/inventory/transfers/", expect=403)
        self.use(self.token, self.store_id)
        other_token, other_store = self.sign_up("inventory-other@example.com", "Other shop")
        self.use(other_token, other_store["storeId"])
        self.assertEqual(self.ok("get", "/api/v1/inventory/locations/"), [])
        self.assertEqual(self.ok("get", ITEMS), [])
        self.ok("get", f"{ITEMS}{row['id']}/", expect=404)
        self.ok("post", f"{ITEMS}set/", {
            "variant": self.variant, "location": self.location, "available": 99,
        }, expect=400)
        self.client.credentials()
        self.client.cookies.clear()
        for url in ("/api/v1/inventory/locations/", ITEMS, "/api/v1/inventory/transfers/"):
            self.assertIn(self.client.get(url).status_code, (401, 403))

    def test_transfer_isolation_and_unknown_ids(self):
        dest = self.ok("post", "/api/v1/inventory/locations/", {"name": "Shop"}, expect=201)["id"]
        self.ok("post", "/api/v1/inventory/transfers/", {
            "source_location": self.location, "destination_location": dest,
            "items": [{"variant": self.variant, "quantity": 1}],
        }, expect=201)
        transfer = self.ok("get", "/api/v1/inventory/transfers/")[0]
        other_token, other = self.sign_up("inventory-transfer-other@example.com", "Other transfer store")
        self.use(other_token, other["storeId"])
        self.assertEqual(self.ok("get", "/api/v1/inventory/transfers/"), [])
        self.ok("get", f"/api/v1/inventory/transfers/{transfer['id']}/", expect=404)
        self.ok("post", f"/api/v1/inventory/transfers/{transfer['id']}/complete/", expect=404)
        self.ok("post", "/api/v1/inventory/transfers/", {
            "source_location": self.location, "destination_location": dest,
            "items": [{"variant": self.variant, "quantity": 1}],
        }, expect=400)
