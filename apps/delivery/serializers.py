from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin
from apps.fulfillment.models import Fulfillment

from .models import DeliveryGateway, Shipment, ShipmentItem, TrackingEvent


class DeliveryGatewaySerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryGateway
        fields = ["id", "name", "code", "is_active", "created_at", "updated_at"]
        extra_kwargs = {"is_active": {"default": True}}

    def validate_code(self, value):
        value = value.strip().lower()
        others = DeliveryGateway.objects.filter(
            store=self.context["request"].store, code=value, is_deleted=False
        )
        if self.instance:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError("Another delivery partner already uses this code.")
        return value


class TrackingEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrackingEvent
        fields = ["id", "status", "location", "description", "event_at"]
        extra_kwargs = {"event_at": {"required": False}}


class ShipmentItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="order_item.product_name", read_only=True)

    class Meta:
        model = ShipmentItem
        fields = ["id", "order_item", "product_name", "quantity"]
        read_only_fields = fields


class ShipmentSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    tracking_events = serializers.SerializerMethodField()
    gateway_name = serializers.CharField(source="delivery_gateway.name", read_only=True)
    order = serializers.UUIDField(source="fulfillment.order_id", read_only=True)

    class Meta:
        model = Shipment
        fields = [
            "id", "order", "fulfillment", "delivery_gateway", "gateway_name", "tracking_number",
            "status", "shipped_at", "delivered_at", "items", "tracking_events", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_items(self, shipment):
        return ShipmentItemSerializer([i for i in shipment.items.all() if not i.is_deleted], many=True).data

    def get_tracking_events(self, shipment):
        events = sorted(
            (e for e in shipment.tracking_events.all() if not e.is_deleted),
            key=lambda e: e.event_at,
        )
        return TrackingEventSerializer(events, many=True).data


class ShipSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    fulfillment = serializers.PrimaryKeyRelatedField(queryset=Fulfillment.objects.all())
    delivery_gateway = serializers.PrimaryKeyRelatedField(queryset=DeliveryGateway.objects.all())
    tracking_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
