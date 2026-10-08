from rest_framework import serializers

from apps.catalog.models import ProductVariant
from apps.core.viewsets import StoreScopedSerializerMixin

from .models import InventoryItem, Location, Transfer, TransferItem


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
