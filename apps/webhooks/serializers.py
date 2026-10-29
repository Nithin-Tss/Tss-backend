from rest_framework import serializers

from . import events
from .models import WebhookDelivery, WebhookEndpoint
from .security import UnsafeURL, check_url


class WebhookEndpointSerializer(serializers.ModelSerializer):
    events = serializers.ListField(child=serializers.CharField(), allow_empty=False)

    class Meta:
        model = WebhookEndpoint
        fields = [
            "id", "url", "description", "events", "is_active",
            "consecutive_failures", "disabled_reason", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "consecutive_failures", "disabled_reason", "created_at", "updated_at"]

    def validate_url(self, value):
        try:
            check_url(value)
        except UnsafeURL as exc:
            raise serializers.ValidationError(str(exc))
        return value

    def validate_events(self, value):
        unknown = sorted(set(value) - set(events.EVENTS) - {"*"})
        if unknown:
            raise serializers.ValidationError(f"Unknown events: {', '.join(unknown)}.")
        return sorted(set(value))

    def update(self, instance, validated_data):
        # Turning an endpoint back on is a fresh start
        if validated_data.get("is_active") and not instance.is_active:
            validated_data.update(consecutive_failures=0, disabled_reason="")
        return super().update(instance, validated_data)


class WebhookDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDelivery
        fields = [
            "id", "endpoint", "event_id", "event_type", "payload", "status", "attempts", "next_attempt_at",
            "last_status_code", "last_error", "delivered_at", "created_at",
        ]
        read_only_fields = fields
