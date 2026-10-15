from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.core.viewsets import StoreScopedSerializerMixin

from .models import InventoryHistory, InventoryItem, Location, Transfer, TransferItem
from .services import MANUAL_REASONS, UNAVAILABLE_REASONS


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = ["id", "name", "created_at"]


class InventoryItemSerializer(serializers.ModelSerializer):
    product = serializers.UUIDField(source="variant.product_id", read_only=True)
    product_title = serializers.CharField(source="variant.product.title", read_only=True)
    sku = serializers.CharField(source="variant.sku", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)

    class Meta:
        model = InventoryItem
        fields = [
            "id", "variant", "product", "product_title", "sku", "location", "location_name",
            "available", "committed", "unavailable", "on_hand", "incoming", "updated_at",
        ]
        read_only_fields = fields


class StockChangeSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all())
    available = serializers.IntegerField(required=False, min_value=0)
    delta = serializers.IntegerField(required=False)
    reason = serializers.ChoiceField(choices=list(MANUAL_REASONS), required=False, default="correction")
    note = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")

    def validate_delta(self, value):
        if value == 0:
            raise serializers.ValidationError("Must not be 0.")
        return value

    def validate(self, data):
        if "available" in data and "delta" in data:
            raise serializers.ValidationError("Send either available or delta, not both.")
        return data


class UnavailableChangeSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    """Set stock aside (damaged, ...) or put it back (release=true)."""

    variant = serializers.PrimaryKeyRelatedField(queryset=ProductVariant.objects.all())
    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all())
    quantity = serializers.IntegerField(min_value=1)
    reason = serializers.ChoiceField(choices=list(UNAVAILABLE_REASONS))
    release = serializers.BooleanField(required=False, default=False)
    note = serializers.CharField(required=False, allow_blank=True, max_length=500, default="")


class InventoryHistorySerializer(serializers.ModelSerializer):
    """One line of the history: date, change, reason, who, new quantity."""

    reason_label = serializers.SerializerMethodField()
    who = serializers.SerializerMethodField()
    location_name = serializers.CharField(source="location.name", read_only=True)
    sku = serializers.CharField(source="variant.sku", read_only=True)
    product_title = serializers.CharField(source="variant.product.title", read_only=True)

    class Meta:
        model = InventoryHistory
        fields = [
            "id", "variant", "sku", "product_title", "location", "location_name", "created_at",
            "reason", "reason_label", "source", "reference_id", "note", "who",
            "before", "after", "change", "committed_change", "unavailable_change",
            "committed", "unavailable", "on_hand", "incoming",
        ]
        read_only_fields = fields

    def get_reason_label(self, row):
        return MANUAL_REASONS.get(row.reason) or row.reason.replace("_", " ").capitalize()

    def get_who(self, row):
        user = row.user
        if user is None:
            return "System"
        return f"{user.first_name} {user.last_name}".strip() or user.email


class TransferItemSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    sku = serializers.CharField(source="variant.sku", read_only=True)

    class Meta:
        model = TransferItem
        fields = ["id", "variant", "sku", "quantity"]
        extra_kwargs = {"quantity": {"min_value": 1}}


class TransferSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    items = TransferItemSerializer(many=True)

    class Meta:
        model = Transfer
        fields = [
            "id", "transfer_number", "source_location", "destination_location",
            "status", "items", "created_at", "updated_at",
        ]
        read_only_fields = ["transfer_number", "status"]

    def validate(self, data):
        if data.get("source_location") == data.get("destination_location"):
            raise serializers.ValidationError("Source and destination must be different locations.")
        if not data.get("items"):
            raise serializers.ValidationError({"items": "Add at least one item."})
        return data
