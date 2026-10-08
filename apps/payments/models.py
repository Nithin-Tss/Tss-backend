import uuid

from django.db import models

from apps.commerce.models import Checkout
from apps.customers.models import Customer
from apps.tenancy.models import Store


class Payment(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="payments",
    )

    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.PROTECT,
        db_column="checkout_id",
        related_name="payments",
        null=True,
        blank=True,
    )

    # Deferred until orders.Order exists.
    # The final database design requires this to become:
    # order_id UUID FK -> orders.orders.id, NULL
    order_id = models.UUIDField(
        null=True,
        blank=True,
        db_column="order_id",
    )

    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="payments",
        null=True,
        blank=True,
    )

    currency = models.CharField(max_length=3)

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    status = models.CharField(max_length=30)

    provider = models.CharField(max_length=50)

    provider_payment_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    metadata = models.JSONField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    is_deleted = models.BooleanField()

    deleted_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        db_table = '"payments"."payments"'
        verbose_name = "Payment"
        verbose_name_plural = "Payments"

    def __str__(self):
        return str(self.id)


class PaymentTransaction(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )

    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        db_column="payment_id",
        related_name="transactions",
    )

    transaction_type = models.CharField(max_length=30)

    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )

    status = models.CharField(max_length=30)

    provider_transaction_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    provider_refund_id = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    failure_reason = models.TextField(
        null=True,
        blank=True,
    )

    reason = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )

    metadata = models.JSONField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"payments"."payment_transactions"'
        verbose_name = "Payment Transaction"
        verbose_name_plural = "Payment Transactions"

    def __str__(self):
        return f"{self.payment} - {self.transaction_type}"