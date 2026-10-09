from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin
from apps.orders.models import Order, OrderItem

from .models import Fulfillment, FulfillmentItem


class FulfillmentItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="order_item.product_name", read_only=True)

    class Meta:
        model = FulfillmentItem
        fields = ["id", "order_item", "product_name", "quantity"]
        read_only_fields = fields


class FulfillmentSerializer(serializers.ModelSerializer):
    items = FulfillmentItemSerializer(many=True, read_only=True)
    order_number = serializers.CharField(source="order.order_number", read_only=True)

    class Meta:
        model = Fulfillment
        fields = ["id", "order", "order_number", "status", "delivery_method", "items", "created_at", "updated_at"]
        read_only_fields = fields


class FulfillLineSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    order_item = serializers.PrimaryKeyRelatedField(queryset=OrderItem.objects.all())
    quantity = serializers.IntegerField(min_value=1)


class FulfillSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    order = serializers.PrimaryKeyRelatedField(queryset=Order.objects.all())
    items = FulfillLineSerializer(many=True, required=False, help_text="Leave out to fulfill everything left")
    delivery_method = serializers.CharField(max_length=50, required=False, allow_blank=True)
