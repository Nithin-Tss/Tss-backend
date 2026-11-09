from decimal import Decimal

from rest_framework import serializers

from .models import Payment, PaymentTransaction
from .services import PROVIDERS


class PaymentTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTransaction
        fields = [
            "id", "transaction_type", "amount", "status", "provider_transaction_id",
            "provider_refund_id", "failure_reason", "reason", "metadata", "created_at",
        ]
        read_only_fields = fields


def gift_card_summary(payment):
    """For a gift card payment: which card paid ({id, last4, status, expires_at, is_expired}); else None."""
    if payment.provider != "gift_card":
        return None
    from django.utils import timezone

    from apps.gift_cards.services import card_for_payment

    card = card_for_payment(payment)
    if card is None:
        return None
    return {
        "id": str(card.pk),
        "last4": card.code_last4,
        "status": card.status,
        "expires_at": card.expires_at,
        "is_expired": bool(card.expires_at and card.expires_at <= timezone.now()),
    }


class PaymentSerializer(serializers.ModelSerializer):
    transactions = PaymentTransactionSerializer(many=True, read_only=True)
    gift_card = serializers.SerializerMethodField()

    def get_gift_card(self, payment):
        return gift_card_summary(payment)

    class Meta:
        model = Payment
        fields = [
            "id", "order_id", "checkout", "customer", "currency", "amount", "status",
            "provider", "provider_payment_id", "gift_card", "transactions", "created_at", "updated_at",
        ]
        read_only_fields = fields


class RecordPaymentSerializer(serializers.Serializer):
    order = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    provider = serializers.ChoiceField(choices=PROVIDERS, default="manual")
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True)


class RefundSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    # Omitted: a gift card payment goes back to its card, anything else to its original method
    destination = serializers.ChoiceField(choices=("gift_card", "original"), required=False)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)
