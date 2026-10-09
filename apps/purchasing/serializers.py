from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin
from apps.inventory.models import Location

from .models import PurchaseOrder, PurchaseOrderItem


class PurchaseOrderItemSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    sku = serializers.CharField(source="variant.sku", read_only=True)
    product_title = serializers.CharField(source="variant.product.title", read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = ["id", "variant", "sku", "product_title", "quantity"]
        extra_kwargs = {"quantity": {"min_value": 1}}


class PurchaseOrderSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True)

    class Meta:
        model = PurchaseOrder
        fields = ["id", "po_number", "supplier_name", "status", "items", "created_at", "updated_at"]
        read_only_fields = ["po_number", "status"]

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Add at least one item.")
        return items


class ReceiveSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    location = serializers.PrimaryKeyRelatedField(queryset=Location.objects.all())
