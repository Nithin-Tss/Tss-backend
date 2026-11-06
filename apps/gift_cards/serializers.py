from decimal import Decimal

from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from . import currencies
from .models import GiftCard, GiftCardCurrencySetting, GiftCardTransaction
from .services import ACTIVE, DISABLED


def _money_in_currency(data, code, fields):
    """Money is stored with 3 places (enough for any currency) but shown with the currency's own."""
    for field in fields:
        if data.get(field) is not None and code:
            data[field] = str(currencies.quantize(code, data[field]))
    return data


class GiftCardTransactionSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by_user.email", default=None, read_only=True)

    def to_representation(self, instance):
        return _money_in_currency(super().to_representation(instance), instance.gift_card.currency_code,
                                  ("amount", "balance_before", "balance_after"))

    class Meta:
        model = GiftCardTransaction
        fields = [
            "id", "transaction_type", "amount", "balance_before", "balance_after",
            "order", "checkout", "payment", "created_by", "reason", "created_at",
        ]
        read_only_fields = fields


class GiftCardSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """
    The code is never exposed here: `code_last4` is all list/retrieve/export ever show. The full
    code is only returned once, by GiftCardViewSet.create(), straight after it is generated.
    """

    status = serializers.ChoiceField(choices=(ACTIVE, DISABLED), default=ACTIVE)
    transactions = GiftCardTransactionSerializer(many=True, read_only=True)

    class Meta:
        model = GiftCard
        fields = [
            "id", "code_last4", "initial_balance", "current_balance", "currency_code", "status",
            "customer", "expires_at", "transactions", "created_at", "updated_at",
        ]
        read_only_fields = ["code_last4", "current_balance"]
        extra_kwargs = {
            "initial_balance": {"min_value": 1},
            "currency_code": {"required": False},
        }

    def to_representation(self, instance):
        return _money_in_currency(super().to_representation(instance), instance.currency_code,
                                  ("initial_balance", "current_balance"))

    def validate_currency_code(self, value):
        return currencies.validate_code(value)

    def validate(self, data):
        if not self.instance:
            data.setdefault("currency_code", currencies.DEFAULT_CURRENCY)
        # The value of an issued card can't be rewritten; use /adjust/ instead.
        if self.instance and "initial_balance" in data and data["initial_balance"] != self.instance.initial_balance:
            raise serializers.ValidationError({"initial_balance": "Can't be changed after the card is issued."})
        return data


class GiftCardCurrencySettingSerializer(serializers.ModelSerializer):
    class Meta:
        model = GiftCardCurrencySetting
        fields = ["id", "currency_code", "is_enabled", "amounts"]

    def validate_currency_code(self, value):
        return currencies.validate_code(value)

    def validate_amounts(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError("Give at least one fixed amount.")
        cleaned = []
        for raw in value:
            try:
                amount = Decimal(str(raw))
            except Exception:
                raise serializers.ValidationError(f"{raw!r} is not a number.")
            if amount <= 0:
                raise serializers.ValidationError("Amounts must be greater than 0.")
            cleaned.append(str(amount))
        return cleaned


class AdjustSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=3)
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError("Must not be 0.")
        return value


class CreateGiftCardSerializer(serializers.Serializer):
    """A free gift card, issued by staff directly (not bought by a customer)."""

    initial_balance = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.01"))
    currency_code = serializers.CharField(required=False, max_length=3)
    customer = serializers.UUIDField(required=False, allow_null=True)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)
    note = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate_currency_code(self, value):
        return currencies.validate_code(value)


class ResendSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False)


class PurchaseInputSerializer(serializers.Serializer):
    currency_code = serializers.CharField(max_length=3)
    amount = serializers.DecimalField(max_digits=14, decimal_places=3)
    buyer_email = serializers.EmailField()
    recipient_email = serializers.EmailField(required=False, allow_blank=True)
    recipient_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    message = serializers.CharField(required=False, allow_blank=True, max_length=500)
    send_on = serializers.DateField(required=False, allow_null=True)

    def validate_currency_code(self, value):
        return currencies.validate_code(value)


class BalanceCheckSerializer(serializers.Serializer):
    token = serializers.CharField()
