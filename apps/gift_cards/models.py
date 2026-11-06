import uuid

from django.db import models

from apps.commerce.models import Checkout
from apps.customers.models import Customer
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.tenancy.models import Store
from apps.identity.models import User
from django.core.exceptions import ValidationError


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
    # The code itself is never stored in plain text: code_hash is used for lookup, code_enc only to
    # show it again once (creation response, resend email), code_last4 is all admin lists/exports show.
    code_hash = models.CharField(max_length=64, null=True, blank=True)
    code_enc = models.TextField(null=True, blank=True)
    code_last4 = models.CharField(max_length=4, null=True, blank=True)
    # decimal_places=3 covers every ISO 4217 currency's own precision (0, 2 or 3): the business
    # logic always quantizes to the currency's own places (see apps.gift_cards.currencies), this
    # just avoids truncating a 3-decimal currency like KWD.
    initial_balance = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )
    current_balance = models.DecimalField(
        max_digits=14,
        decimal_places=3,
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
                fields=["store", "code_hash"],
                name="uq_gift_card_store_code_hash",
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
        return f"****{self.code_last4}" if self.code_last4 else str(self.pk)
class GiftCardTransactionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError(
            "Gift card transactions cannot be edited."
        )

    def delete(self):
        raise ValidationError(
            "Gift card transactions cannot be deleted."
        )

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
        max_digits=14,
        decimal_places=3,
    )
    balance_before = models.DecimalField(
        max_digits=14,
        decimal_places=3,
    )
    balance_after = models.DecimalField(
        max_digits=14,
        decimal_places=3,
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
    objects = GiftCardTransactionQuerySet.as_manager()

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(
                "Gift card transactions cannot be edited."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Gift card transactions cannot be deleted."
        )

    class Meta:
        db_table = '"gift_cards"."gift_card_transactions"'
        verbose_name = "Gift Card Transaction"
        verbose_name_plural = "Gift Card Transactions"

    def __str__(self):
        return f"{self.gift_card} - {self.transaction_type}"


class GiftCardCurrencySetting(models.Model):
    """Which currencies a store sells gift cards in, and the fixed amounts offered for each."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store,
        on_delete=models.CASCADE,
        db_column="store_id",
        related_name="gift_card_currencies",
    )
    currency_code = models.CharField(max_length=3)
    is_enabled = models.BooleanField(default=True)
    # Fixed amounts a customer may buy, as strings (exact, no float rounding): ["25", "50", "100", "200"]
    amounts = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"gift_cards"."gift_card_currency_settings"'
        verbose_name = "Gift Card Currency Setting"
        verbose_name_plural = "Gift Card Currency Settings"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "currency_code"],
                name="uq_gift_card_currency_store_code",
            ),
        ]

    def __str__(self):
        return f"{self.store_id} - {self.currency_code}"


PENDING, PAID, FAILED, REFUNDED = "pending", "paid", "failed", "refunded"


class GiftCardPurchase(models.Model):
    """
    A customer buying a gift card for themselves or a recipient. Holds the recipient details and
    the Stripe payment until it is confirmed; the GiftCard itself is only created once paid.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        db_column="store_id",
        related_name="gift_card_purchases",
    )
    currency_code = models.CharField(max_length=3)
    amount = models.DecimalField(max_digits=14, decimal_places=3)
    status = models.CharField(
        max_length=20,
        choices=[(PENDING, PENDING), (PAID, PAID), (FAILED, FAILED), (REFUNDED, REFUNDED)],
        default=PENDING,
    )
    buyer_customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        db_column="buyer_customer_id",
        related_name="gift_card_purchases",
        null=True,
        blank=True,
    )
    buyer_email = models.EmailField()
    recipient_name = models.CharField(max_length=255, null=True, blank=True)
    recipient_email = models.EmailField(null=True, blank=True)
    message = models.TextField(null=True, blank=True)
    send_on = models.DateField(null=True, blank=True)
    gift_card = models.ForeignKey(
        GiftCard,
        on_delete=models.PROTECT,
        db_column="gift_card_id",
        related_name="purchase",
        null=True,
        blank=True,
    )
    payment = models.ForeignKey(
        Payment,
        on_delete=models.PROTECT,
        db_column="payment_id",
        related_name="gift_card_purchases",
        null=True,
        blank=True,
    )
    stripe_payment_intent_id = models.CharField(max_length=255, null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = '"gift_cards"."gift_card_purchases"'
        verbose_name = "Gift Card Purchase"
        verbose_name_plural = "Gift Card Purchases"
        constraints = [
            models.UniqueConstraint(
                fields=["store", "stripe_payment_intent_id"],
                name="uq_gift_card_purchase_store_intent",
                condition=models.Q(stripe_payment_intent_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f"{self.store_id} - {self.amount} {self.currency_code} ({self.status})"