from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework import serializers

from .models import NotificationSettings

MAX_RECIPIENTS = 10


class NotificationSettingsSerializer(serializers.ModelSerializer):
    staff_recipients = serializers.ListField(child=serializers.CharField(max_length=254), required=False, max_length=MAX_RECIPIENTS)

    class Meta:
        model = NotificationSettings
        fields = [
            "sender_name", "reply_to_email", "email_footer",
            "customer_order_confirmation", "customer_order_cancelled", "customer_order_refunded",
            "customer_shipment_delivered",
            "staff_new_order", "staff_order_cancelled", "staff_recipients",
            "updated_at",
        ]
        read_only_fields = ["updated_at"]

    def validate_sender_name(self, value):
        # The name goes into a From header: no line breaks or address punctuation
        value = value.strip()
        if any(c in value for c in '\r\n<>"@'):
            raise serializers.ValidationError("Use letters, numbers and spaces only.")
        return value

    def validate_email_footer(self, value):
        return " ".join(value.split())

    def validate_staff_recipients(self, value):
        cleaned = []
        for raw in value:
            email = raw.strip().lower()
            try:
                validate_email(email)
            except DjangoValidationError:
                raise serializers.ValidationError(f"{raw!r} is not a valid email address.")
            if email not in cleaned:
                cleaned.append(email)
        return cleaned
