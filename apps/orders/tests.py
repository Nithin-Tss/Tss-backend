from unittest.mock import patch

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.orders import services
from apps.orders.models import DraftOrder, DraftOrderDetail, Order, OrderItem
from apps.tenancy.models import Store
from decimal import Decimal

from apps.payments.services import record_payment, record_pending_payment, refund


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

    def test_full_repayment_after_refund_is_paid(self):
        order_data = self.create_order(customer=self.customer["id"])
        order = Order.objects.get(pk=order_data["id"])

        original_payment = record_payment(
            store=self.store,
            order=order,
            amount=Decimal("50.00"),
        )

        refund(original_payment, Decimal("50.00"))

        record_payment(
            store=self.store,
            order=order,
            amount=Decimal("50.00"),
        )

        retrieved = self.ok(
            "get",
            f"/api/v1/orders/{order.pk}/",
        )

        self.assertEqual(retrieved["payment_status"], "paid")

    def test_pending_payment_does_not_mark_order_paid(self):
        order_data = self.create_order()
        order = Order.objects.get(pk=order_data["id"])

        record_pending_payment(
            store=self.store,
            order=order,
            checkout=None,
            amount=Decimal("50.00"),
            provider="cod",
            customer=order.customer,
        )

        retrieved = self.ok(
            "get",
            f"/api/v1/orders/{order.pk}/",
        )

        self.assertEqual(retrieved["payment_status"], "pending")

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
            with self.assertLogs("django.request", level="ERROR"), \
                    self.assertRaisesRegex(RuntimeError, "detail failure"):
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


class OrderAccessTests(StoreClientMixin, APITestCase):
    """Store isolation and role permissions for /api/v1/orders/."""

    def setUp(self):
        self.token_a, store_a = self.sign_up("owner-a@example.com", "Store A")
        self.store_a = store_a["storeId"]
        self.product_a = self.make_product("Shirt A")
        self.customer_a = self.make_customer("alex@example.com")

        self.token_b, store_b = self.sign_up("owner-b@example.com", "Store B")
        self.store_b = store_b["storeId"]
        self.product_b = self.make_product("Shirt B")
        self.customer_b = self.make_customer("sam@example.com")
        self.order_b = self.ok("post", "/api/v1/orders/", {
            "customer": self.customer_b["id"],
            "items": [{"product": self.product_b["id"], "quantity": 1}],
        }, expect=201)

        self.use(self.token_a, self.store_a)

    def make_product(self, title):
        return self.ok("post", "/api/v1/catalog/products/", {
            "title": title, "sku": f"SKU-{title.upper().replace(' ', '-')}", "price": "25",
        }, expect=201)

    def make_customer(self, email):
        return self.ok("post", "/api/v1/customers/", {"first_name": "Test", "email": email}, expect=201)

    def order_payload(self, **extra):
        return {"items": [{"product": self.product_a["id"], "quantity": 2}], **extra}

    def staff(self, email, *role_names):
        """A second account that is staff in Store A with the given roles (by key or name)."""
        roles = {r["key"] or r["name"]: r for r in self.ok("get", "/api/v1/roles/")}
        token, _ = self.sign_up(email, f"{email} own store")
        self.use(self.token_a, self.store_a)
        self.ok("post", "/api/v1/members/", {
            "email": email, "roleIds": [roles[name]["id"] for name in role_names],
        }, expect=201)
        self.use(token, self.store_a)
        return token

    # 1. A Store A user cannot view, update, or cancel a Store B order

    def test_store_a_cannot_see_store_b_order(self):
        self.assertEqual(self.ok("get", "/api/v1/orders/"), [])
        self.ok("get", f"/api/v1/orders/{self.order_b['id']}/", expect=404)

    def test_store_a_cannot_update_store_b_order(self):
        self.ok("patch", f"/api/v1/orders/{self.order_b['id']}/", {"delivery_method": "pickup"}, expect=404)
        self.assertIsNone(Order.objects.get(pk=self.order_b["id"]).delivery_method)

    def test_store_a_cannot_cancel_store_b_order(self):
        self.ok("post", f"/api/v1/orders/{self.order_b['id']}/cancel/", expect=404)
        self.assertEqual(Order.objects.get(pk=self.order_b["id"]).status, "open")

    def test_store_a_user_cannot_switch_to_store_b_header(self):
        self.use(self.token_a, self.store_b)
        self.ok("get", f"/api/v1/orders/{self.order_b['id']}/", expect=403)
        self.ok("patch", f"/api/v1/orders/{self.order_b['id']}/", {"delivery_method": "pickup"}, expect=403)
        self.ok("post", f"/api/v1/orders/{self.order_b['id']}/cancel/", expect=403)
        order = Order.objects.get(pk=self.order_b["id"])
        self.assertEqual((order.status, order.delivery_method), ("open", None))

    # 2. Staff without the required permission cannot create an order

    def test_staff_without_roles_cannot_create_order(self):
        self.staff("nobody@example.com")
        self.ok("post", "/api/v1/orders/", self.order_payload(), expect=403)
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())

    def test_merchandiser_cannot_create_order(self):
        self.staff("merch@example.com", "merchandiser")
        self.ok("post", "/api/v1/orders/", self.order_payload(), expect=403)
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())

    def test_view_only_staff_can_list_but_not_create_update_or_cancel(self):
        self.ok("post", "/api/v1/roles/", {
            "name": "Order viewer", "permissions": ["orders.view", "products.view", "customers.view"],
        }, expect=201)
        order = self.ok("post", "/api/v1/orders/", self.order_payload(), expect=201)

        self.staff("viewer@example.com", "Order viewer")
        self.assertEqual(len(self.ok("get", "/api/v1/orders/")), 1)
        self.ok("post", "/api/v1/orders/", self.order_payload(), expect=403)
        self.ok("patch", f"/api/v1/orders/{order['id']}/", {"delivery_method": "pickup"}, expect=403)
        self.ok("post", f"/api/v1/orders/{order['id']}/cancel/", expect=403)
        self.assertEqual(Order.objects.filter(store_id=self.store_a).count(), 1)
        self.assertEqual(Order.objects.get(pk=order["id"]).status, "open")

    def test_customer_support_staff_can_create_order(self):
        self.staff("support@example.com", "customer_support")
        order = self.ok("post", "/api/v1/orders/", self.order_payload(customer=self.customer_a["id"]), expect=201)
        self.assertEqual(str(Order.objects.get(pk=order["id"]).store_id), self.store_a)

    # 3. A Store A order cannot be linked to a Store B customer

    def test_create_order_with_store_b_customer_is_rejected(self):
        response = self.client.post(
            "/api/v1/orders/", self.order_payload(customer=self.customer_b["id"]), format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("customer", response.json())
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())

    def test_patch_cannot_relink_order_to_store_b_customer(self):
        order = self.ok("post", "/api/v1/orders/", self.order_payload(customer=self.customer_a["id"]), expect=201)
        self.ok("patch", f"/api/v1/orders/{order['id']}/", {"customer": self.customer_b["id"]})
        self.assertEqual(str(Order.objects.get(pk=order["id"]).customer_id), self.customer_a["id"])

    def test_service_rejects_store_b_customer(self):
        from rest_framework.exceptions import ValidationError

        from apps.catalog.models import Product
        from apps.customers.models import Customer

        store_a = Store.objects.get(pk=self.store_a)
        with self.assertRaises(ValidationError):
            services.create_order(
                store_a,
                [{"product": Product.objects.get(pk=self.product_a["id"]), "quantity": 1}],
                customer=Customer.objects.get(pk=self.customer_b["id"]),
            )
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())

    def test_draft_with_store_b_customer_is_rejected(self):
        response = self.client.post("/api/v1/orders/drafts/", {
            "customer": self.customer_b["id"],
            "details": [{"product": self.product_a["id"], "quantity": 1, "unit_price": "25"}],
        }, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("customer", response.json())
        self.assertFalse(DraftOrder.objects.filter(store_id=self.store_a).exists())

    # 4. Valid order operations within the same store keep working

    def test_same_store_create_list_retrieve_update_cancel(self):
        order = self.ok("post", "/api/v1/orders/", self.order_payload(customer=self.customer_a["id"]), expect=201)
        self.assertEqual(order["customer"], self.customer_a["id"])
        self.assertEqual(order["total"], "50.00")
        self.assertEqual(order["status"], "open")

        self.assertEqual([o["id"] for o in self.ok("get", "/api/v1/orders/")], [order["id"]])
        self.assertEqual(
            [o["id"] for o in self.ok("get", f"/api/v1/orders/?customer={self.customer_a['id']}")], [order["id"]],
        )
        self.assertEqual(self.ok("get", f"/api/v1/orders/{order['id']}/")["id"], order["id"])

        updated = self.ok("patch", f"/api/v1/orders/{order['id']}/", {"delivery_method": "pickup", "total": "1.00"})
        self.assertEqual(updated["delivery_method"], "pickup")
        self.assertEqual(updated["total"], "50.00")  # read-only, ignored

        cancelled = self.ok("post", f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual(cancelled["status"], "cancelled")
        self.ok("post", f"/api/v1/orders/{order['id']}/cancel/", expect=400)

    def test_orders_cannot_be_deleted(self):
        order = self.ok("post", "/api/v1/orders/", self.order_payload(), expect=201)
        self.ok("delete", f"/api/v1/orders/{order['id']}/", expect=405)
        self.assertTrue(Order.objects.filter(pk=order["id"]).exists())

    # Sales channels and draft orders across stores

    def store_b_channel(self):
        self.use(self.token_b, self.store_b)
        channels = self.ok("get", "/api/v1/catalog/sales-channels/")
        self.use(self.token_a, self.store_a)
        return channels[0]["id"]

    def store_b_draft(self):
        self.use(self.token_b, self.store_b)
        draft = self.ok("post", "/api/v1/orders/drafts/", {
            "customer": self.customer_b["id"],
            "details": [{"product": self.product_b["id"], "quantity": 1, "unit_price": "25"}],
        }, expect=201)
        self.use(self.token_a, self.store_a)
        return draft

    def post_draft(self, *details, customer=None):
        return self.client.post("/api/v1/orders/drafts/", {
            "customer": customer or self.customer_a["id"], "details": list(details),
        }, format="json")

    def test_create_order_with_store_b_sales_channel_is_rejected(self):
        response = self.client.post(
            "/api/v1/orders/", self.order_payload(sales_channel=self.store_b_channel()), format="json",
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("sales_channel", response.json())
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())

        own_channel = self.ok("get", "/api/v1/catalog/sales-channels/")[0]["id"]
        order = self.ok("post", "/api/v1/orders/", self.order_payload(sales_channel=own_channel), expect=201)
        self.assertEqual(order["sales_channel"], own_channel)

    def test_draft_with_store_b_product_or_variant_is_rejected(self):
        variant_b = self.product_b["variants"][0]["id"]
        for detail in (
            {"product": self.product_b["id"], "quantity": 1, "unit_price": "25"},
            {"product": self.product_a["id"], "variant": variant_b, "quantity": 1, "unit_price": "25"},
        ):
            with self.subTest(detail=detail):
                response = self.post_draft(detail)
                self.assertEqual(response.status_code, 400, response.content)
        self.assertFalse(DraftOrder.objects.filter(store_id=self.store_a).exists())

    def test_store_a_cannot_update_delete_or_complete_store_b_draft(self):
        draft = self.store_b_draft()
        url = f"/api/v1/orders/drafts/{draft['id']}/"

        self.ok("get", url, expect=404)
        self.ok("patch", url, {"po_number": "HIJACK"}, expect=404)
        self.ok("delete", url, expect=404)
        self.ok("post", f"{url}complete/", expect=404)

        saved = DraftOrder.objects.get(pk=draft["id"])
        self.assertEqual((saved.po_number, saved.status, saved.order_id), (None, "open", None))
        self.assertEqual(saved.details.count(), 1)
        self.assertEqual(Order.objects.count(), 1)  # only Store B's order from setUp

    # Invalid input

    def test_invalid_customer_ids_return_400(self):
        for bad in ("not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(customer=bad):
                response = self.client.post("/api/v1/orders/", self.order_payload(customer=bad), format="json")
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("customer", response.json())

                response = self.post_draft(
                    {"product": self.product_a["id"], "quantity": 1, "unit_price": "25"}, customer=bad,
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn("customer", response.json())
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())
        self.assertFalse(DraftOrder.objects.filter(store_id=self.store_a).exists())

    def test_invalid_customer_filter_returns_400(self):
        for url in ("/api/v1/orders/", "/api/v1/orders/drafts/"):
            with self.subTest(url=url):
                self.ok("get", f"{url}?customer=not-a-uuid", expect=400)
                self.assertEqual(self.ok("get", f"{url}?customer=00000000-0000-0000-0000-000000000000"), [])

    def test_negative_quantities_and_amounts_are_rejected(self):
        product = self.product_a["id"]
        cases = [
            {"items": [{"product": product, "quantity": -1}]},
            {"items": [{"product": product, "quantity": 1, "unit_price": "-1"}]},
            {"items": [{"product": product, "quantity": 1, "discount_amount": "-1"}]},
            {"items": []},
            self.order_payload(discount_amount="-1"),
            self.order_payload(tax_amount="-1"),
            self.order_payload(shipping_amount="-1"),
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/v1/orders/", payload, format="json")
                self.assertEqual(response.status_code, 400, response.content)

        for detail in (
            {"product": product, "quantity": 0, "unit_price": "25"},
            {"product": product, "quantity": -1, "unit_price": "25"},
            {"product": product, "quantity": 1, "unit_price": "-1"},
            {"product": product, "quantity": 1, "unit_price": "25", "discount_amount": "-1"},
        ):
            with self.subTest(detail=detail):
                response = self.post_draft(detail)
                self.assertEqual(response.status_code, 400, response.content)

        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())
        self.assertFalse(DraftOrder.objects.filter(store_id=self.store_a).exists())

    def test_excessive_order_discounts_are_rejected(self):
        line_discount = {"items": [{"product": self.product_a["id"], "quantity": 1, "unit_price": "10", "discount_amount": "11"}]}
        response = self.client.post("/api/v1/orders/", line_discount, format="json")
        self.assertEqual(response.status_code, 400, response.content)

        # Subtotal is 2 x 25 = 50: a discount above it is refused, the whole subtotal is allowed.
        response = self.client.post("/api/v1/orders/", self.order_payload(discount_amount="50.01"), format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("discount_amount", response.json())
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())

        order = self.ok("post", "/api/v1/orders/", self.order_payload(discount_amount="50.00"), expect=201)
        self.assertEqual(order["total"], "0.00")

    # Business rules for finished orders and drafts

    def test_fulfilled_or_partly_fulfilled_order_cannot_be_cancelled(self):
        order = self.ok("post", "/api/v1/orders/", self.order_payload(), expect=201)
        for fulfillment_status in ("partial", "fulfilled"):
            with self.subTest(fulfillment_status=fulfillment_status):
                Order.objects.filter(pk=order["id"]).update(fulfillment_status=fulfillment_status)
                self.ok("post", f"/api/v1/orders/{order['id']}/cancel/", expect=400)
                self.assertEqual(Order.objects.get(pk=order["id"]).status, "open")

    def test_completed_draft_cannot_be_updated(self):
        draft = self.ok("post", "/api/v1/orders/drafts/", {
            "customer": self.customer_a["id"], "po_number": "PO-1",
            "details": [{"product": self.product_a["id"], "quantity": 1, "unit_price": "25"}],
        }, expect=201)
        url = f"/api/v1/orders/drafts/{draft['id']}/"
        order = self.ok("post", f"{url}complete/", expect=201)

        self.ok("patch", url, {"po_number": "PO-2"}, expect=400)
        self.ok("patch", url, {"details": [{"product": self.product_a["id"], "quantity": 5, "unit_price": "1"}]}, expect=400)

        saved = DraftOrder.objects.get(pk=draft["id"])
        self.assertEqual((saved.po_number, saved.status, str(saved.order_id)), ("PO-1", "completed", order["id"]))
        self.assertEqual(saved.details.get().quantity, 1)
        self.assertEqual(saved.total, 25)

    def test_draft_with_custom_item_saves_but_cannot_be_completed(self):
        draft = self.ok("post", "/api/v1/orders/drafts/", {
            "customer": self.customer_a["id"],
            "details": [{"title": "Gift wrap", "quantity": 1, "unit_price": "5"}],
        }, expect=201)
        self.ok("post", f"/api/v1/orders/drafts/{draft['id']}/complete/", expect=400)

        saved = DraftOrder.objects.get(pk=draft["id"])
        self.assertEqual((saved.status, saved.order_id), ("open", None))
        self.assertFalse(Order.objects.filter(store_id=self.store_a).exists())
