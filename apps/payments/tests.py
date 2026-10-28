from django.test import TestCase

# Create your tests here.
from decimal import Decimal
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from django.core.exceptions import ValidationError
from apps.payments.models import PaymentTransaction

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError as DRFValidationError
from unittest import skipUnless

from django.db import DatabaseError, connection, transaction

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
    def test_payment_transaction_instance_cannot_be_updated_or_deleted(self):
        payment = record_payment(
            store=self.store,
            order=self.order,
            amount=Decimal("50.00"),
        )
        transaction_record = payment.transactions.get(
            transaction_type="capture"
        )

        transaction_record.amount = Decimal("1.00")

        with self.assertRaises(ValidationError):
            transaction_record.save()

        transaction_record.refresh_from_db()
        self.assertEqual(
            transaction_record.amount,
            Decimal("50.00"),
        )

        with self.assertRaises(ValidationError):
            transaction_record.delete()

        self.assertTrue(
            PaymentTransaction.objects.filter(
                pk=transaction_record.pk
            ).exists()
        )

    @skipUnless(
        connection.vendor == "postgresql",
        "The append-only trigger is created by a PostgreSQL-only migration; "
        "config.test_settings uses SQLite without migrations.",
    )
    def test_database_trigger_blocks_payment_transaction_updates_and_deletes(self):
        payment = record_payment(
            store=self.store,
            order=self.order,
            amount=Decimal("50.00"),
        )
        record = payment.transactions.get(transaction_type="capture")

        # Direct SQL UPDATE should be rejected by PostgreSQL.
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE "payments"."payment_transactions" '
                        'SET reason = %s WHERE id = %s',
                        ["tampered", str(record.pk)],
                    )

        # Direct SQL DELETE should also be rejected.
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'DELETE FROM "payments"."payment_transactions" '
                        'WHERE id = %s',
                        [str(record.pk)],
                    )

        self.assertTrue(
            payment.transactions.filter(pk=record.pk).exists()
        )
    def test_payment_transaction_queryset_cannot_be_updated_or_deleted(self):
        payment = record_payment(
            store=self.store,
            order=self.order,
            amount=Decimal("50.00"),
        )
        transaction_record = payment.transactions.get(
            transaction_type="capture"
        )

        with self.assertRaises(ValidationError):
            PaymentTransaction.objects.filter(
                pk=transaction_record.pk
            ).update(amount=Decimal("1.00"))

        with self.assertRaises(ValidationError):
            PaymentTransaction.objects.filter(
                pk=transaction_record.pk
            ).delete()

        self.assertTrue(
            PaymentTransaction.objects.filter(
                pk=transaction_record.pk
            ).exists()
        )

    def test_pending_payment_cannot_be_refunded(self):
        payment = record_pending_payment(
            store=self.store,
            order=self.order,
            checkout=None,
            amount=Decimal("50.00"),
            provider="cod",
            customer=self.order.customer,
        )

        with self.assertRaises(DRFValidationError):
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