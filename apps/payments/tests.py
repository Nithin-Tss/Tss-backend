from django.test import TestCase

# Create your tests here.
from decimal import Decimal
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.orders.models import Order
from apps.payments.services import (
    paid_total,
    record_payment,
    record_pending_payment,
    refund,
)
from apps.tenancy.models import Store


class PaidTotalTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, store = self.sign_up(
            "payments@example.com",
            "Payments Store",
        )
        self.store = Store.objects.get(pk=store["storeId"])

        self.product = self.ok(
            "post",
            "/api/v1/catalog/products/",
            {
                "title": "Shirt",
                "sku": "SKU-SHIRT",
                "price": "25",
            },
            expect=201,
        )

        self.customer = self.ok(
            "post",
            "/api/v1/customers/",
            {
                "first_name": "Alex",
                "email": "alex@example.com",
            },
            expect=201,
        )

        order_data = self.ok(
            "post",
            "/api/v1/orders/",
            {
                "customer": self.customer["id"],
                "items": [
                    {
                        "product": self.product["id"],
                        "quantity": 2,
                    }
                ],
            },
            expect=201,
        )

        self.order = Order.objects.get(pk=order_data["id"])

    def test_pending_payment_cannot_be_refunded(self):
        payment = record_pending_payment(
            store=self.store,
            order=self.order,
            checkout=None,
            amount=Decimal("50.00"),
            provider="cod",
            customer=self.order.customer,
        )

        with self.assertRaises(ValidationError):
            refund(payment, Decimal("10.00"))

        payment.refresh_from_db()
        self.assertEqual(payment.status, "pending")
        self.assertFalse(
            payment.transactions.filter(
                transaction_type="refund",
                status="success",
            ).exists()
        )

    def test_pending_payment_is_excluded(self):
        record_pending_payment(
            store=self.store,
            order=self.order,
            checkout=None,
            amount=Decimal("50.00"),
            provider="cod",
            customer=self.order.customer,
        )

        self.assertEqual(
            paid_total(self.order),
            Decimal("0.00"),
        )

    def test_captured_payment_is_included(self):
        record_payment(
            store=self.store,
            order=self.order,
            amount=Decimal("50.00"),
        )

        self.assertEqual(
            paid_total(self.order),
            Decimal("50.00"),
        )

    def test_successful_refund_is_subtracted(self):
        payment = record_payment(
            store=self.store,
            order=self.order,
            amount=Decimal("50.00"),
        )

        refund(payment, Decimal("10.00"))

        self.assertEqual(
            paid_total(self.order),
            Decimal("40.00"),
        )

    def test_deleted_payment_is_excluded(self):
        payment = record_payment(
            store=self.store,
            order=self.order,
            amount=Decimal("50.00"),
        )

        payment.is_deleted = True
        payment.save(update_fields=["is_deleted"])

        self.assertEqual(
            paid_total(self.order),
            Decimal("0.00"),
        )