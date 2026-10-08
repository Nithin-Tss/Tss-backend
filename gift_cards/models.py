import uuid

from django.db import models

from commerce.models import Checkout
from customers.models import Customer
from orders.models import Order
from payments.models import Payment
from tenancy.models import Store
from identity.models import User


class GiftCard(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="gift_cards",
    )
    code = models.CharField(max_length=100)
    initial_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    current_balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    currency_code = models.CharField(max_length=3)
    status = models.CharField(max_length=30)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="customer_id",
        related_name="gift_cards",
        null=True,
        blank=True,
    )
    expires_at = models.DateTimeField(
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
        db_table = '"gift_cards"."gift_cards"'
        verbose_name = "Gift Card"
        verbose_name_plural = "Gift Cards"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "code"],
                name="uq_gift_card_store_code",
            ),
            models.CheckConstraint(
                condition=models.Q(current_balance__gte=0),
                name="ck_gift_card_current_balance_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    current_balance__lte=models.F("initial_balance")
                ),
                name="ck_gift_card_balance_not_above_initial",
            ),
        ]

    def __str__(self):
        return self.code


class GiftCardTransaction(models.Model):
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    gift_card = models.ForeignKey(
        GiftCard,
        on_delete=models.PROTECT,
        db_column="gift_card_id",
        related_name="transactions",
    )
    transaction_type = models.CharField(max_length=30)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    balance_before = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    balance_after = models.DecimalField(
        max_digits=12,
        decimal_places=2,
    )
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        db_column="order_id",
        related_name="gift_card_transactions",
        null=True,
        blank=True,
    )
    checkout = models.ForeignKey(
        Checkout,
        on_delete=models.PROTECT,
        db_column="checkout_id",
        related_name="gift_card_transactions",
        null=True,
        blank=True,
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        db_column="payment_id",
        related_name="gift_card_transactions",
        null=True,
        blank=True,
    )
    created_by_user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        db_column="created_by_user_id",
        related_name="gift_card_transactions",
        null=True,
        blank=True,
    )
    reason = models.CharField(
        max_length=255,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = '"gift_cards"."gift_card_transactions"'
        verbose_name = "Gift Card Transaction"
        verbose_name_plural = "Gift Card Transactions"

    def __str__(self):
        return f"{self.gift_card} - {self.transaction_type}"