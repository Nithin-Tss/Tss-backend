"""
adjust_inventory(): the one function behind every stock change.

    python manage.py test apps.inventory.test_adjust --settings=config.test_settings
"""
from unittest import mock

from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.catalog.models import ProductVariant
from apps.catalog.tests import signup
from apps.inventory import services
from apps.inventory.models import InventoryHistory, InventoryItem, Location
from apps.inventory.services import adjust_inventory
from apps.tenancy.models import Store

PRODUCTS = "/api/v1/catalog/products/"
ITEMS = "/api/v1/inventory/items/"


class StockBase(APITestCase):
    def setUp(self):
        token, self.store_id = signup(self.client, f"{self._testMethodName}@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=self.store_id)
        self.store = Store.objects.get(pk=self.store_id)
        self.slug = self.store.store_slug
        self.location = self.client.post("/api/v1/inventory/locations/", {"name": "Main"}, format="json").json()["id"]

    def product(self, title="Wine", sku="W-1", quantity=None):
        body = {"title": title, "status": "active", "price": "10", "sku": sku}
        if quantity is not None:
            body.update(location=self.location, quantity=quantity)
        r = self.client.post(PRODUCTS, body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()

    def variant_with_stock(self, available, sku="W-1"):
        """A product whose stock row is created directly, so no history row exists yet."""
        product = self.product(sku=sku)
        variant = ProductVariant.objects.get(pk=product["variants"][0]["id"])
        InventoryItem.objects.create(
            variant=variant, location_id=self.location,
            available=available, committed=0, unavailable=0, on_hand=available, incoming=0,
        )
        return product, variant

    def adjust(self, variant, reason="correction", **kw):
        return adjust_inventory(self.store_id, variant.pk, self.location, reason, **kw)

    def row(self, variant):
        return InventoryItem.objects.get(variant=variant, location_id=self.location)

    def html(self, path):
        return self.client.get(f"/s/{self.slug}/{path}").content.decode()


class AdjustInventoryTests(StockBase):
    def test_add_then_remove_keeps_two_history_rows(self):
        _, variant = self.variant_with_stock(5)

        self.assertEqual(self.adjust(variant, delta=10).available, 15)
        self.assertEqual(self.adjust(variant, delta=-3).available, 12)

        row = self.row(variant)
        self.assertEqual((row.available, row.on_hand), (12, 12))
        history = list(InventoryHistory.objects.filter(variant=variant).order_by("created_at", "id"))
        self.assertEqual([(h.before, h.after, h.change) for h in history], [(5, 15, 10), (15, 12, -3)])
        self.assertEqual(len(history), 2)

    def test_going_below_zero_is_blocked_and_nothing_changes(self):
        _, variant = self.variant_with_stock(2)

        with self.assertRaises(ValidationError) as caught:
            self.adjust(variant, delta=-3)

        self.assertIn("Only 2 available", str(caught.exception.detail))
        self.assertEqual(self.row(variant).available, 2)
        self.assertEqual(InventoryHistory.objects.count(), 0)

    def test_continue_selling_allows_going_below_zero(self):
        _, variant = self.variant_with_stock(1)
        ProductVariant.objects.filter(pk=variant.pk).update(continue_selling=True)

        self.assertEqual(self.adjust(variant, delta=-3).available, -2)

    def test_only_whole_numbers(self):
        _, variant = self.variant_with_stock(5)
        for bad in (1.5, "2", True, None):
            with self.assertRaises(ValidationError):
                self.adjust(variant, delta=bad)
        with self.assertRaises(ValidationError):
            self.adjust(variant)  # neither a change nor a new total
        with self.assertRaises(ValidationError):
            self.adjust(variant, delta=1, new_quantity=3)  # both
        self.assertEqual(self.row(variant).available, 5)

    def test_new_quantity_sets_the_total(self):
        _, variant = self.variant_with_stock(5)
        self.adjust(variant, new_quantity=9, reason="cycle_count", user_id=None, note="Counted")
        history = InventoryHistory.objects.get()
        self.assertEqual((history.before, history.after, history.change), (5, 9, 4))
        self.assertEqual((history.reason, history.note, history.source), ("cycle_count", "Counted", "admin"))

    def test_history_cannot_be_edited_or_deleted(self):
        _, variant = self.variant_with_stock(5)
        self.adjust(variant, delta=1)
        entry = InventoryHistory.objects.get()
        entry.note = "changed"
        with self.assertRaises(Exception):
            entry.save()
        with self.assertRaises(Exception):
            entry.delete()
        with self.assertRaises(Exception):
            InventoryHistory.objects.all().update(note="x")
        with self.assertRaises(Exception):
            InventoryHistory.objects.all().delete()
        self.assertEqual(InventoryHistory.objects.get().note, "")

    def test_two_changes_at_the_same_moment_are_both_counted(self):
        """
        Stock 1, two -1 changes at once: only one succeeds. The first takes the last unit. The
        second had read the row before that (available 1), so its write finds the row changed
        and is refused instead of overwriting it. (On PostgreSQL the row lock makes the second
        one wait and then see 0; this guards the write itself.)
        """
        _, variant = self.variant_with_stock(1)
        stale = self.row(variant)  # what the second change read: available 1

        self.assertEqual(self.adjust(variant, delta=-1).available, 0)  # the first change wins

        with mock.patch.object(services, "_locked_item", return_value=stale):
            with self.assertRaises(ValidationError) as caught:
                self.adjust(variant, delta=-1)

        self.assertIn("just changed", str(caught.exception.detail))
        self.assertEqual(self.row(variant).available, 0)  # not -1, and not overwritten back to 1
        self.assertEqual(InventoryHistory.objects.count(), 1)

        # and without the stale read, the second change is simply blocked as out of stock
        with self.assertRaises(ValidationError) as caught:
            self.adjust(variant, delta=-1)
        self.assertIn("Only 0 available", str(caught.exception.detail))

    def test_another_store_cannot_touch_this_stock(self):
        _, variant = self.variant_with_stock(5)
        other = signup(self.client, "other-store@example.com")[1]

        with self.assertRaises(ValidationError):
            adjust_inventory(other, variant.pk, self.location, "correction", delta=-1)
        self.assertEqual(self.row(variant).available, 5)

        # and over the API: the other store's token cannot see or move it
        token, other_id = signup(self.client, "third-store@example.com")
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_STORE_ID=other_id)
        self.assertEqual(self.client.get(ITEMS).json(), [])
        r = self.client.post(f"{ITEMS}adjust/", {"variant": str(variant.pk), "location": self.location, "delta": -1},
                             format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.row(variant).available, 5)
        self.assertEqual(self.client.get(f"{ITEMS}history/").json(), [])


class StockRulesTests(StockBase):
    def numbers(self, variant):
        r = self.row(variant)
        return r.available, r.committed, r.unavailable, r.on_hand

    def test_order_placed_cancelled_fulfilled(self):
        product, variant = self.variant_with_stock(10)

        order = self.client.post("/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 4}],
        }, format="json")
        self.assertEqual(order.status_code, 201, order.content)
        self.assertEqual(self.numbers(variant), (6, 4, 0, 10))  # available down, committed up, on hand same

        r = self.client.post(f"/api/v1/orders/{order.json()['id']}/cancel/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.numbers(variant), (10, 0, 0, 10))  # back to before

        order = self.client.post("/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 4}],
        }, format="json").json()
        r = self.client.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(self.numbers(variant), (6, 0, 0, 6))  # committed and on hand down

        reasons = list(InventoryHistory.objects.order_by("created_at", "id").values_list("reason", flat=True))
        self.assertEqual(
            reasons,
            ["order_placed", "order_cancelled", "order_placed", "order_fulfilled"],
        )
        self.assertTrue(all(InventoryHistory.objects.exclude(reference_id=None).values_list("reference_id")))

    def test_mark_unavailable_damaged(self):
        _, variant = self.variant_with_stock(10)
        services.mark_unavailable(variant, Location.objects.get(pk=self.location), 2, "damaged")
        self.assertEqual(self.numbers(variant), (8, 0, 2, 10))  # available -2, unavailable +2, on hand same

        with self.assertRaises(ValidationError):  # can't set aside more than is available
            services.mark_unavailable(variant, Location.objects.get(pk=self.location), 9, "damaged")
        services.release_unavailable(variant, Location.objects.get(pk=self.location), 2, "damaged")
        self.assertEqual(self.numbers(variant), (10, 0, 0, 10))

    def test_refund_restock_and_incoming(self):
        _, variant = self.variant_with_stock(5)
        InventoryItem.objects.filter(variant=variant).update(incoming=4)
        self.adjust(variant, "refund_restock", delta=2)
        self.assertEqual(self.numbers(variant), (7, 0, 0, 7))  # available up, on hand up

        self.adjust(variant, "incoming_received", delta=3)
        self.assertEqual(self.numbers(variant), (10, 0, 0, 10))
        self.assertEqual(self.row(variant).incoming, 1)  # incoming down

    def test_cannot_commit_more_than_available(self):
        product, variant = self.variant_with_stock(2)
        r = self.client.post("/api/v1/orders/", {"items": [{"product": product["id"], "quantity": 3}]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.numbers(variant), (2, 0, 0, 2))

    def test_csv_import_goes_through_the_history(self):
        r = self.client.post(f"{PRODUCTS}bulk/", {"products": [
            {"title": "Imported", "sku": "IMP-1", "status": "active", "price": "5", "quantity": 7,
             "location": self.location},
        ]}, format="json")
        self.assertEqual(r.json()["created"], 1, r.content)
        entry = InventoryHistory.objects.get()
        self.assertEqual((entry.source, entry.reason, entry.after), ("import", "import", 7))
        self.assertIsNotNone(entry.user_id)


class OnePlaceTests(StockBase):
    """Product page, Inventory page and storefront all show the same stock."""

    def available_on_inventory_page(self, product_id):
        rows = self.client.get(f"{ITEMS}?product={product_id}").json()
        return sum(r["available"] for r in rows)

    def test_edits_show_everywhere(self):
        product = self.product(title="ZONZO RORO", sku="ZR-1", quantity=3)
        variant = product["variants"][0]["id"]
        self.assertEqual(self.available_on_inventory_page(product["id"]), 3)

        # Product page edit 3 -> 8: the Inventory page shows 8
        r = self.client.patch(f"{PRODUCTS}{product['id']}/", {"location": self.location, "quantity": 8}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.available_on_inventory_page(product["id"]), 8)
        self.assertEqual(r.json()["quantity"], 8)

        # Inventory page adjust 8 -> 5: the product page shows 5 and the store shows "Add to cart"
        r = self.client.post(f"{ITEMS}set/", {"variant": variant, "location": self.location, "available": 5,
                                              "reason": "cycle_count", "note": "Counted"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(self.client.get(f"{PRODUCTS}{product['id']}/").json()["quantity"], 5)
        page = self.html(f"products/{product['id']}")
        self.assertRegex(page, r"data-add[^>]*>\s*Add to cart")
        self.assertNotRegex(page, r"data-add[^>]*\sdisabled")

        # Set to 0 -> "Sold out" (disabled); set to 5 -> "Add to cart" again
        self.client.post(f"{ITEMS}set/", {"variant": variant, "location": self.location, "available": 0}, format="json")
        page = self.html(f"products/{product['id']}")
        self.assertRegex(page, r"data-add[^>]*\sdisabled>\s*Sold out")
        self.client.post(f"{ITEMS}set/", {"variant": variant, "location": self.location, "available": 5}, format="json")
        page = self.html(f"products/{product['id']}")
        self.assertRegex(page, r"data-add[^>]*>\s*Add to cart")

    def test_continue_selling_keeps_the_button_on(self):
        product = self.product(title="Backorder", sku="BO-1", quantity=0)
        self.assertRegex(self.html(f"products/{product['id']}"), r"data-add[^>]*\sdisabled>\s*Sold out")

        r = self.client.patch(f"{PRODUCTS}{product['id']}/", {"continue_selling": True}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        page = self.html(f"products/{product['id']}")
        self.assertRegex(page, r"data-add[^>]*>\s*Add to cart")
        self.assertNotRegex(page, r"data-add[^>]*\sdisabled")


class InventoryApiTests(StockBase):
    def test_history_unavailable_and_breakdown(self):
        product, variant = self.variant_with_stock(10)
        item = self.row(variant)

        r = self.client.post(f"{ITEMS}unavailable/", {
            "variant": str(variant.pk), "location": self.location, "quantity": 2, "reason": "damaged",
            "note": "Dropped",
        }, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["available"], r.json()["unavailable"], r.json()["on_hand"]), (8, 2, 10))

        order = self.client.post("/api/v1/orders/", {"items": [{"product": product["id"], "quantity": 3}]},
                                 format="json").json()

        breakdown = self.client.get(f"{ITEMS}{item.pk}/breakdown/").json()
        self.assertEqual(breakdown["unavailable"], [{"reason": "damaged", "quantity": 2}])
        self.assertEqual(breakdown["committed"], [{"order": order["id"], "order_number": order["order_number"],
                                                   "quantity": 3}])

        history = self.client.get(f"{ITEMS}history/?variant={variant.pk}").json()
        self.assertEqual([h["reason"] for h in history], ["order_placed", "unavailable_damaged"])  # newest first
        self.assertEqual(history[1]["note"], "Dropped")
        self.assertNotEqual(history[1]["who"], "System")
        self.assertEqual((history[0]["change"], history[0]["after"]), (-3, 5))

        # once the order is fulfilled it no longer counts as committed
        self.client.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json")
        self.assertEqual(self.client.get(f"{ITEMS}{item.pk}/breakdown/").json()["committed"], [])

    def test_whole_numbers_and_reasons_are_checked_by_the_api(self):
        _, variant = self.variant_with_stock(5)
        body = {"variant": str(variant.pk), "location": self.location}
        self.assertEqual(self.client.post(f"{ITEMS}adjust/", {**body, "delta": 1.5}, format="json").status_code, 400)
        self.assertEqual(self.client.post(f"{ITEMS}adjust/", {**body, "delta": 1, "reason": "nope"},
                                          format="json").status_code, 400)
        r = self.client.post(f"{ITEMS}adjust/", {**body, "delta": -9}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.row(variant).available, 5)
