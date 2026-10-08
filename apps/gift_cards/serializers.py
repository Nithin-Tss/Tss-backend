from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from .models import GiftCard, GiftCardTransaction
from .services import ACTIVE, DISABLED


class GiftCardTransactionSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by_user.email", default=None, read_only=True)

    class Meta:
        model = GiftCardTransaction
        fields = [
            "id", "transaction_type", "amount", "balance_before", "balance_after",
            "order", "checkout", "payment", "created_by", "reason", "created_at",
        ]
        read_only_fields = fields


class GiftCardSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    status = serializers.ChoiceField(choices=(ACTIVE, DISABLED), default=ACTIVE)
    transactions = GiftCardTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = GiftCard
        fields = [
            "id", "code", "initial_balance", "current_balance", "currency_code", "status",
            "customer", "expires_at", "transactions", "created_at", "updated_at",
        ]
        read_only_fields = ["current_balance"]
        extra_kwargs = {
            "code": {"required": False, "allow_blank": True},
            "initial_balance": {"min_value": 1},
            "currency_code": {"default": "INR"},
        }

    def validate_code(self, value):
        value = (value or "").strip().upper()
        if not value:
            return value

        others = GiftCard.objects.filter(store=self.context["request"].store, code=value)
        if self.instance:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError("A gift card with this code already exists.")
        return value

    def validate(self, data):
        # The value of an issued card can't be rewritten; use /adjust/ instead.
        if self.instance and "initial_balance" in data and data["initial_balance"] != self.instance.initial_balance:
            raise serializers.ValidationError({"initial_balance": "Can't be changed after the card is issued."})
        return data


class AdjustSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError("Must not be 0.")
        return value
