from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from .models import Customer, CustomerAddress

SUBSCRIPTION_STATUSES = ("subscribed", "not_subscribed", "unsubscribed")


class CustomerAddressSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = CustomerAddress
        fields = [
            "id", "customer", "address_type", "first_name", "last_name", "company",
            "address_line_1", "address_line_2", "city", "state", "postal_code",
            "country", "phone", "is_default", "created_at", "updated_at",
        ]


class CustomerSerializer(serializers.ModelSerializer):
    email_subscription_status = serializers.ChoiceField(
        choices=SUBSCRIPTION_STATUSES,
        default="not_subscribed",
    )
    addresses = CustomerAddressSerializer(many=True, read_only=True)

    class Meta:
        model = Customer
        fields = [
            "id", "first_name", "last_name", "email", "email_subscription_status",
            "city", "state", "country", "addresses", "created_at", "updated_at",
        ]

    def validate_email(self, value):
        if not value:
            return value

        value = value.strip().lower()
        request = self.context["request"]
        others = Customer.objects.filter(store=request.store, email__iexact=value)

        if self.instance:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError("A customer with this email already exists.")

        return value
