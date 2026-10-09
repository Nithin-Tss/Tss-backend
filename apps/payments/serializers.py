from decimal import Decimal

from rest_framework import serializers

from .models import Payment, PaymentTransaction
from .services import PROVIDERS


class PaymentTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTransaction
        fields = [
            "id", "transaction_type", "amount", "status", "provider_transaction_id",
            "provider_refund_id", "failure_reason", "reason", "created_at",
        ]
        read_only_fields = fields


class PaymentSerializer(serializers.ModelSerializer):
    transactions = PaymentTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = Payment
        fields = [
            "id", "order_id", "checkout", "customer", "currency", "amount", "status",
            "provider", "provider_payment_id", "transactions", "created_at", "updated_at",
        ]
        read_only_fields = fields


class RecordPaymentSerializer(serializers.Serializer):
    order = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    provider = serializers.ChoiceField(choices=PROVIDERS, default="manual")
    reference = serializers.CharField(max_length=255, required=False, allow_blank=True)


class RefundSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)
