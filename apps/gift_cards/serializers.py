from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from . import currencies
from .models import GiftCard, GiftCardCurrencySetting, GiftCardTransaction
from .services import ACTIVE


def _money_in_currency(data, code, fields):
    """Money is stored with 3 places (enough for any currency) but shown with the currency's own."""
    for field in fields:
        if data.get(field) is not None and code:
            data[field] = str(currencies.quantize(code, data[field]))
    return data


class GiftCardTransactionSerializer(serializers.ModelSerializer):
    created_by = serializers.CharField(source="created_by_user.email", default=None, read_only=True)
    order_number = serializers.SerializerMethodField()

    def get_order_number(self, tx):
        return tx.order.order_number if tx.order_id else None

    def to_representation(self, instance):
        return _money_in_currency(super().to_representation(instance), instance.gift_card.currency_code,
                                  ("amount", "balance_before", "balance_after"))

    class Meta:
        model = GiftCardTransaction
        fields = [
            "id", "transaction_type", "amount", "balance_before", "balance_after",
            "order", "order_number", "checkout", "payment", "created_by", "reason", "created_at",
        ]
        read_only_fields = fields


class GiftCardSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """
    The code is never exposed here: `code_last4` is all list/retrieve/export ever show. The full
    code is only returned once, by GiftCardViewSet.create(), straight after it is generated.

    Only the expiry date and the customer can be edited. The balance moves only through orders and
    refunds, and a card is deactivated through /deactivate/ (permanent), never by writing `status`.
    """

    transactions = GiftCardTransactionSerializer(many=True, read_only=True)
    note = serializers.SerializerMethodField()
    is_expired = serializers.SerializerMethodField()
    customer_email = serializers.CharField(source="customer.email", default=None, read_only=True)

    class Meta:
        model = GiftCard
        fields = [
            "id", "code_last4", "initial_balance", "current_balance", "currency_code", "status",
            "customer", "customer_email", "expires_at", "note", "is_expired", "transactions",
            "created_at", "updated_at",
        ]
        read_only_fields = ["code_last4", "initial_balance", "current_balance", "currency_code", "status"]

    def get_note(self, card):
        """The note typed when staff issued the card (kept on its first history line)."""
        first = min(card.transactions.all(), key=lambda t: t.created_at, default=None)
        if first is None or first.order_id or first.created_by_user_id is None:
            return None
        return first.reason

    def get_is_expired(self, card):
        return bool(card.expires_at and card.expires_at <= timezone.now())

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["transactions"] = sorted(data["transactions"], key=lambda t: t["created_at"], reverse=True)
        return _money_in_currency(data, instance.currency_code, ("initial_balance", "current_balance"))

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("The expiry date must be in the future.")
        return value

    def validate(self, data):
        if self.instance and self.instance.status != ACTIVE:
            raise serializers.ValidationError("A deactivated gift card can't be edited.")
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


class BalanceCheckSerializer(serializers.Serializer):
    token = serializers.CharField()
