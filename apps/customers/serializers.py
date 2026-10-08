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
    name = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    orders_count = serializers.SerializerMethodField()
    amount_spent = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id", "first_name", "last_name", "name", "email", "email_subscription_status",
            "city", "state", "country", "location", "orders_count", "amount_spent",
            "addresses", "created_at", "updated_at",
        ]

    def get_name(self, obj):
        name = " ".join(filter(None, [obj.first_name, obj.last_name])).strip()
        return name or obj.email or "—"

    def get_location(self, obj):
        default_addr = next((a for a in obj.addresses.all() if a.is_default), None) or obj.addresses.first()
        if default_addr:
            parts = [default_addr.address_line_1, default_addr.city, default_addr.state, default_addr.country]
            loc = ", ".join(filter(None, parts)).strip()
            if loc:
                return loc
        parts = [obj.city, obj.state, obj.country]
        loc = ", ".join(filter(None, parts)).strip()
        return loc or "—"

    def get_orders_count(self, obj):
        return obj.orders.count() if hasattr(obj, "orders") else 0

    def get_amount_spent(self, obj):
        from django.db.models import Sum
        if hasattr(obj, "orders"):
            total = obj.orders.aggregate(total_sum=Sum("total"))["total_sum"]
            return f"{total:.2f}" if total is not None else "0.00"
        return "0.00"

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
