from unittest.mock import patch

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.orders import services
from apps.orders.models import DraftOrder, DraftOrderDetail, Order, OrderItem
from apps.tenancy.models import Store


class OrdersApiTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, store = self.sign_up("orders@example.com", "Orders Store")
        self.store_id = store["storeId"]
        self.store = Store.objects.get(pk=self.store_id)
        self.product = self.create_product("Shirt")
        self.customer = self.ok(
            "post",
            "/api/v1/customers/",
            {"first_name": "Alex", "email": "alex@example.com"},
            expect=201,
        )

    def create_product(self, title):
        return self.ok(
            "post",
            "/api/v1/catalog/products/",
            {"title": title, "sku": f"SKU-{title.upper()}", "price": "25"},
            expect=201,
        )

    def create_order(self, **extra):
        return self.ok(
            "post",
            "/api/v1/orders/",
            {"items": [{"product": self.product["id"], "quantity": 2}], **extra},
            expect=201,
        )

    def create_draft(self, **extra):
        return self.ok(
            "post",
            "/api/v1/orders/drafts/",
            {
                "customer": self.customer["id"],
                "po_number": "PO-42",
                "details": [
                    {
                        "product": self.product["id"],
                        "quantity": 2,
                        "unit_price": "25",
                    }
                ],
                **extra,
            },
            expect=201,
        )

    def test_normal_order_persists_order_and_items_only(self):
        order = self.create_order(customer=self.customer["id"])

        self.assertEqual(Order.objects.filter(store=self.store).count(), 1)
        self.assertEqual(OrderItem.objects.filter(order_id=order["id"]).count(), 1)
        self.assertFalse(DraftOrder.objects.filter(store=self.store).exists())
        self.assertEqual(order["items"][0]["product"], self.product["id"])
        self.assertEqual(order["items_count"], 2)
        self.assertEqual(order["total"], "50.00")
        self.assertEqual(order["payment_status"], "pending")

    def test_draft_persists_details_only_and_listing_is_separate(self):
        draft = self.create_draft()

        self.assertEqual(DraftOrder.objects.filter(store=self.store).count(), 1)
        self.assertEqual(DraftOrderDetail.objects.filter(draft_order_id=draft["id"]).count(), 1)
        self.assertFalse(Order.objects.filter(store=self.store).exists())
        self.assertFalse(OrderItem.objects.exists())
        self.assertEqual(draft["po_number"], "PO-42")
        self.assertEqual(draft["total"], "50.00")
        self.assertEqual(len(self.ok("get", "/api/v1/orders/")), 0)
        self.assertEqual(len(self.ok("get", "/api/v1/orders/drafts/")), 1)

    def test_draft_update_replaces_details_without_creating_order(self):
        draft = self.create_draft()

        updated = self.ok(
            "patch",
            f"/api/v1/orders/drafts/{draft['id']}/",
            {
                "po_number": "PO-43",
                "details": [
                    {
                        "product": self.product["id"],
                        "quantity": 3,
                        "unit_price": "20",
                    }
                ],
            },
        )

        self.assertEqual(updated["po_number"], "PO-43")
        self.assertEqual(updated["total"], "60.00")
        self.assertEqual(updated["details"][0]["quantity"], 3)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderItem.objects.exists())

    def test_order_and_draft_validation_rejects_invalid_lines(self):
        other_product = self.create_product("Hat")
        invalid_order = self.client.post(
            "/api/v1/orders/",
            {
                "items": [
                    {
                        "product": self.product["id"],
                        "variant": other_product["variants"][0]["id"],
                        "quantity": 1,
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(invalid_order.status_code, 400, invalid_order.content)
        self.assertFalse(Order.objects.exists())

        invalid_draft = self.client.post(
            "/api/v1/orders/drafts/",
            {
                "customer": self.customer["id"],
                "details": [
                    {
                        "product": self.product["id"],
                        "quantity": 1,
                        "unit_price": "10",
                        "discount_amount": "11",
                    }
                ],
            },
            format="json",
        )
        self.assertEqual(invalid_draft.status_code, 400, invalid_draft.content)
        self.assertFalse(DraftOrder.objects.exists())

    def test_invalid_order_quantity_is_rejected(self):
        response = self.client.post(
            "/api/v1/orders/",
            {"items": [{"product": self.product["id"], "quantity": 0}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(Order.objects.exists())

    def test_order_and_customer_records_are_store_scoped(self):
        token, other_store = self.sign_up("other@example.com", "Other Store")

        self.use(token, other_store["storeId"])
        other_product = self.create_product("Other store shirt")
        other_customer = self.ok(
            "post",
            "/api/v1/customers/",
            {"first_name": "Sam", "email": "sam@example.com"},
            expect=201,
        )
        response = self.client.post(
            "/api/v1/orders/",
            {
                "customer": other_customer["id"],
                "items": [{"product": other_product["id"], "quantity": 1}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.client.get("/api/v1/orders/").json()[0]["id"], response.json()["id"])

        response = self.client.post(
            "/api/v1/orders/",
            {
                "customer": self.customer["id"],
                "items": [{"product": self.product["id"], "quantity": 1}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(Order.objects.count(), 1)

        self.use(self.token, self.store_id)
        self.assertEqual(self.client.get("/api/v1/orders/").json(), [])

    def test_conversion_is_atomic_linked_and_idempotent(self):
        draft = self.create_draft()

        first = self.ok(
            "post", f"/api/v1/orders/drafts/{draft['id']}/complete/", expect=201
        )
        repeated = self.ok(
            "post", f"/api/v1/orders/drafts/{draft['id']}/complete/", expect=200
        )
        saved_draft = DraftOrder.objects.get(pk=draft["id"])

        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(str(saved_draft.order_id), first["id"])
        self.assertEqual(saved_draft.status, "completed")
        self.assertEqual(Order.objects.filter(store=self.store).count(), 1)
        self.assertEqual(OrderItem.objects.filter(order_id=first["id"]).count(), 1)
        self.assertEqual(DraftOrder.objects.filter(store=self.store).count(), 1)

    def test_completed_draft_cannot_be_deleted(self):
        draft = self.create_draft()
        self.ok("post", f"/api/v1/orders/drafts/{draft['id']}/complete/", expect=201)

        response = self.client.delete(f"/api/v1/orders/drafts/{draft['id']}/")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertTrue(DraftOrder.objects.filter(pk=draft["id"]).exists())

    def test_payment_status_uses_recorded_payment(self):
        order = self.create_order()

        payment = self.ok(
            "post",
            "/api/v1/payments/",
            {"order": order["id"], "amount": order["total"]},
            expect=201,
        )
        self.assertEqual(payment["status"], "captured")
        retrieved = self.ok("get", f"/api/v1/orders/{order['id']}/")
        self.assertEqual(retrieved["payment_status"], "paid")

    def test_order_creation_rolls_back_if_inventory_commit_fails(self):
        from apps.catalog.models import Product

        product = Product.objects.get(pk=self.product["id"])
        with patch("apps.orders.services.inventory.commit", side_effect=RuntimeError("inventory failure")):
            with self.assertRaisesRegex(RuntimeError, "inventory failure"):
                services.create_order(
                    self.store,
                    [{"product": product, "quantity": 1}],
                )

        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderItem.objects.exists())

    def test_draft_creation_rolls_back_if_detail_save_fails(self):
        with patch(
            "apps.orders.views.DraftOrderDetail.objects.create",
            side_effect=RuntimeError("detail failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "detail failure"):
                self.client.post(
                    "/api/v1/orders/drafts/",
                    {
                        "customer": self.customer["id"],
                        "details": [
                            {
                                "product": self.product["id"],
                                "quantity": 1,
                                "unit_price": "25",
                            }
                        ],
                    },
                    format="json",
                )

        self.assertFalse(DraftOrder.objects.exists())
        self.assertFalse(DraftOrderDetail.objects.exists())


class StockValidationCases:
    """
    Overselling protection, run through both ways of placing an order. A subclass says how
    with attempt(lines): lines are (product, variant, quantity); returns (status_code, body).
    """

    def setUp(self):
        self.token, store = self.sign_up("stockorders@example.com", "Stock Orders Store")
        self.store_id = store["storeId"]
        self.customer = self.ok(
            "post", "/api/v1/customers/", {"first_name": "Alex", "email": "alex@example.com"}, expect=201,
        )
        self.product, self.variant = self.make_product("Shirt")
        self.warehouse = self.make_location("Warehouse")
        self.shop = self.make_location("Shop")

    def make_product(self, title):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": title, "status": "active", "price": "25", "sku": f"SKU-{title.upper()}",
        }, expect=201)
        return product["id"], product["variants"][0]["id"]

    def make_location(self, name):
        return self.ok("post", "/api/v1/inventory/locations/", {"name": name}, expect=201)["id"]

    def set_stock(self, available, variant=None, location=None):
        self.ok("post", "/api/v1/inventory/items/set/", {
            "variant": variant or self.variant, "location": location or self.warehouse, "available": available,
        })

    def levels(self, variant=None):
        return self.stock(variant or self.variant)

    def line(self, quantity, product=None, variant=None):
        return (product or self.product, variant or self.variant, quantity)

    def place(self, *lines):
        status_code, body = self.attempt(lines or [self.line(1)])
        self.assertEqual(status_code, 201, body)
        return body

    def test_quantity_below_stock_commits_it(self):
        self.set_stock(10)
        self.place(self.line(4))
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (6, 4))

    def test_quantity_equal_to_stock_leaves_zero(self):
        self.set_stock(5)
        self.place(self.line(5))
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (0, 5))

    def test_quantity_above_stock_is_rejected_and_nothing_changes(self):
        self.set_stock(3)
        status_code, body = self.attempt([self.line(4)])
        self.assertEqual(status_code, 400, body)
        self.assertIn("Only 3 left", str(body))
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderItem.objects.exists())
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (3, 0))

    def test_stock_in_two_locations_is_added_up(self):
        self.set_stock(3)
        self.set_stock(2, location=self.shop)
        self.place(self.line(5))
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (0, 5))

    def test_more_than_the_total_over_two_locations_is_rejected(self):
        self.set_stock(3)
        self.set_stock(2, location=self.shop)
        status_code, body = self.attempt([self.line(6)])
        self.assertEqual(status_code, 400, body)
        self.assertIn("Only 5 left", str(body))
        self.assertFalse(Order.objects.exists())
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (5, 0))

    def test_continue_selling_allows_an_order_past_stock(self):
        from apps.catalog.models import ProductVariant

        self.set_stock(0)
        ProductVariant.objects.filter(pk=self.variant).update(continue_selling=True)
        self.place(self.line(2))
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (-2, 2))

        self.set_stock(1)  # low, not empty
        self.place(self.line(3))
        self.assertEqual(Order.objects.count(), 2)

    def test_untracked_variant_is_never_blocked(self):
        from apps.inventory.models import InventoryItem

        self.place(self.line(100))  # no stock rows at all: current behaviour is to allow it
        self.assertEqual(Order.objects.count(), 1)
        self.assertFalse(InventoryItem.objects.exists())

    def test_second_line_out_of_stock_saves_nothing(self):
        from apps.inventory.models import InventoryHistory

        other_product, other_variant = self.make_product("Hat")
        self.set_stock(5)
        self.set_stock(1, variant=other_variant)
        status_code, body = self.attempt([
            self.line(2), self.line(3, product=other_product, variant=other_variant),
        ])
        self.assertEqual(status_code, 400, body)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(OrderItem.objects.exists())
        self.assertFalse(InventoryHistory.objects.filter(reason="order_placed").exists())
        first = self.levels()
        self.assertEqual((first["available"], first["committed"]), (5, 0))
        second = self.levels(other_variant)
        self.assertEqual((second["available"], second["committed"]), (1, 0))

    def test_cancelling_puts_the_stock_back_and_allows_ordering_again(self):
        self.set_stock(5)
        order = self.place(self.line(5))
        status_code, _ = self.attempt([self.line(1)])
        self.assertEqual(status_code, 400)  # sold out

        self.ok("post", f"/api/v1/orders/{order['id']}/cancel/")
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (5, 0))

        self.place(self.line(5))
        levels = self.levels()
        self.assertEqual((levels["available"], levels["committed"]), (0, 5))

    def test_another_stores_variant_cannot_be_ordered(self):
        rival_token, rival = self.sign_up("rivalorders@example.com", "Rival Orders Store")
        rival_product, rival_variant = self.make_product("Rival shirt")
        rival_location = self.make_location("Rival warehouse")
        self.set_stock(5, variant=rival_variant, location=rival_location)

        self.use(self.token, self.store_id)
        status_code, body = self.attempt([self.line(1, product=rival_product, variant=rival_variant)])
        self.assertEqual(status_code, 400, body)
        self.assertFalse(Order.objects.exists())

        self.use(rival_token, rival["storeId"])  # the rival's stock is untouched
        self.assertEqual(self.levels(rival_variant), {"available": 5, "committed": 0, "on_hand": 5})


class OrderStockTests(StockValidationCases, StoreClientMixin, APITestCase):
    """POST /api/v1/orders/"""

    def attempt(self, lines):
        r = self.client.post("/api/v1/orders/", {
            "items": [{"product": p, "variant": v, "quantity": q} for p, v, q in lines],
        }, format="json")
        return r.status_code, r.json()


class DraftCompleteStockTests(StockValidationCases, StoreClientMixin, APITestCase):
    """POST /api/v1/orders/drafts/ then /complete/ (stock is only checked on complete)"""

    def attempt(self, lines):
        r = self.client.post("/api/v1/orders/drafts/", {
            "customer": self.customer["id"],
            "details": [
                {"product": p, "variant": v, "quantity": q, "unit_price": "25"} for p, v, q in lines
            ],
        }, format="json")
        if r.status_code != 201:
            return r.status_code, r.json()
        self.draft = r.json()["id"]
        r = self.client.post(f"/api/v1/orders/drafts/{self.draft}/complete/", format="json")
        return r.status_code, r.json()

    def test_a_draft_can_be_saved_without_enough_stock_but_not_completed(self):
        self.set_stock(1)
        status_code, _ = self.attempt([self.line(5)])
        self.assertEqual(status_code, 400)
        draft = DraftOrder.objects.get(pk=self.draft)
        self.assertEqual(draft.status, "open")  # saved fine; only completing is checked
        self.assertIsNone(draft.order_id)
        self.assertFalse(Order.objects.exists())
