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
