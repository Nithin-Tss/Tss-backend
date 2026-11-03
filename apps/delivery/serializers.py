from django.db import transaction
from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin
from apps.fulfillment.models import Fulfillment

from apps.tenancy.models import ShippingRate, ShippingZone, ShippingZoneLocation, TaxRate

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


class ShippingZoneLocationSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="location_id", read_only=True)

    class Meta:
        model = ShippingZoneLocation
        fields = ["id", "country", "region"]


class ShippingZoneSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="zone_id", read_only=True)
    locations = ShippingZoneLocationSerializer(many=True, required=False)

    class Meta:
        model = ShippingZone
        fields = ["id", "name", "locations", "created_at"]
        read_only_fields = ["created_at"]

    def _others(self, model, **lookup):
        qs = model.objects.filter(store=self.context["request"].store, **lookup)
        return qs if self.instance is None else qs.exclude(**{"zone_id" if model is ShippingZoneLocation else "pk": self.instance.pk})

    def validate_name(self, value):
        if self._others(ShippingZone, name__iexact=value.strip()).exists():
            raise serializers.ValidationError("This store already has a shipping zone with that name.")
        return value.strip()

    def validate_locations(self, value):
        keys = [(loc["country"].lower(), loc.get("region", "").lower()) for loc in value]
        if len(set(keys)) != len(keys):
            raise serializers.ValidationError("A destination is listed twice.")
        # A destination belongs to one zone only
        used = {(c.strip().lower(), r.strip().lower()) for c, r in self._others(ShippingZoneLocation).values_list("country", "region")}
        clash = [loc for loc, key in zip(value, keys) if key in used]
        if clash:
            names = ", ".join(f"{loc.get('region') + ', ' if loc.get('region') else ''}{loc['country']}" for loc in clash)
            raise serializers.ValidationError(f"Already in another shipping zone: {names}.")
        return value

    def _set_locations(self, zone, locations):
        ShippingZoneLocation.objects.bulk_create(
            [ShippingZoneLocation(store=zone.store, zone=zone, **loc) for loc in locations]
        )

    @transaction.atomic
    def create(self, validated_data):
        locations = validated_data.pop("locations", [])
        zone = super().create(validated_data)
        self._set_locations(zone, locations)
        return zone

    @transaction.atomic
    def update(self, instance, validated_data):
        locations = validated_data.pop("locations", None)
        zone = super().update(instance, validated_data)
        if locations is not None:  # the list sent replaces the zone's destinations
            zone.locations.all().delete()
            self._set_locations(zone, locations)
        return zone


class ShippingRateSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    zone = serializers.PrimaryKeyRelatedField(queryset=ShippingZone.objects.all(), required=False, allow_null=True)
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0)
    min_weight = serializers.DecimalField(max_digits=10, decimal_places=3, min_value=0, required=False, default=0)
    max_weight = serializers.DecimalField(
        max_digits=10, decimal_places=3, min_value=0, required=False, allow_null=True, default=None
    )

    class Meta:
        model = ShippingRate
        fields = ["id", "name", "zone", "country", "min_weight", "max_weight", "price", "created_at"]
        read_only_fields = ["created_at"]

    id = serializers.UUIDField(source="rate_id", read_only=True)

    def validate_name(self, value):
        return value.strip()

    def validate_country(self, value):
        return value.strip()

    def validate(self, attrs):
        low = attrs.get("min_weight", getattr(self.instance, "min_weight", 0))
        high = attrs.get("max_weight", getattr(self.instance, "max_weight", None))
        if high is not None and high <= low:
            raise serializers.ValidationError({"max_weight": "Must be greater than the minimum weight."})
        zone = attrs.get("zone", getattr(self.instance, "zone", None))
        if zone is not None and attrs.get("country", getattr(self.instance, "country", "")):
            raise serializers.ValidationError({"zone": "A rate is for a zone or a country, not both."})
        return attrs


class TaxRateSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="rate_id", read_only=True)
    rate = serializers.DecimalField(max_digits=5, decimal_places=2, min_value=0, max_value=100)

    class Meta:
        model = TaxRate
        fields = ["id", "name", "country", "region", "rate", "created_at"]
        read_only_fields = ["created_at"]

    def validate_name(self, value):
        return value.strip()

    def validate_country(self, value):
        return value.strip()

    def validate_region(self, value):
        return value.strip()

    def validate(self, attrs):
        country = attrs.get("country", getattr(self.instance, "country", ""))
        region = attrs.get("region", getattr(self.instance, "region", ""))
        if region and not country:
            raise serializers.ValidationError({"country": "A region needs its country."})
        store = self.context["request"].store
        clash = TaxRate.objects.filter(store=store, country__iexact=country, region__iexact=region)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("This store already has a tax rate for that destination.")
        return attrs
