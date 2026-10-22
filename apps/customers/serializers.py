from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from .models import Customer, CustomerAddress

SUBSCRIPTION_STATUSES = ("subscribed", "not_subscribed", "unsubscribed")


ADDRESS_TYPES = ("shipping", "billing")


class CustomerAddressSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    customer = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.all(),
        required=False,
        allow_null=True,
    )
    address_type = serializers.ChoiceField(
        choices=ADDRESS_TYPES,
        default="shipping",
        required=False,
    )
    first_name = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=100,
        error_messages={
            "required": "First name is required.",
            "blank": "First name is required.",
        },
    )
    last_name = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=100,
        error_messages={
            "required": "Last name is required.",
            "blank": "Last name is required.",
        },
    )
    address_line_1 = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=255,
        error_messages={
            "required": "Address line 1 is required.",
            "blank": "Address line 1 is required.",
        },
    )
    city = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=100,
        error_messages={
            "required": "City is required.",
            "blank": "City is required.",
        },
    )
    state = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=100,
        error_messages={
            "required": "State is required.",
            "blank": "State is required.",
        },
    )
    postal_code = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=20,
        error_messages={
            "required": "Postal code is required.",
            "blank": "Postal code is required.",
        },
    )
    country = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=100,
        error_messages={
            "required": "Country is required.",
            "blank": "Country is required.",
        },
    )
    company = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=255,
    )
    address_line_2 = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=255,
    )
    phone = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        max_length=30,
    )
    is_default = serializers.BooleanField(required=False, default=False)

    class Meta:
        model = CustomerAddress
        fields = [
            "id", "customer", "address_type", "first_name", "last_name", "company",
            "address_line_1", "address_line_2", "city", "state", "postal_code",
            "country", "phone", "is_default", "created_at", "updated_at",
        ]

    def validate(self, attrs):
        # Trim string fields
        str_fields = [
            "first_name", "last_name", "company", "address_line_1",
            "address_line_2", "city", "state", "postal_code", "country", "phone",
        ]
        for field in str_fields:
            if field in attrs and isinstance(attrs[field], str):
                attrs[field] = attrs[field].strip()
        return attrs


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
    phone = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "id", "first_name", "last_name", "name", "email", "email_subscription_status",
            "city", "state", "country", "location", "orders_count", "amount_spent",
            "phone", "addresses", "created_at", "updated_at",
        ]

    def _get_default_or_first_address(self, obj):
        addrs = list(obj.addresses.all())
        return next((a for a in addrs if a.is_default), None) or (addrs[0] if addrs else None)

    def get_phone(self, obj):
        default_addr = self._get_default_or_first_address(obj)
        return default_addr.phone if default_addr else None

    def get_name(self, obj):
        name = " ".join(filter(None, [obj.first_name, obj.last_name])).strip()
        return name or obj.email or "—"

    def get_location(self, obj):
        default_addr = self._get_default_or_first_address(obj)
        if default_addr:
            parts = [default_addr.address_line_1, default_addr.city, default_addr.state, default_addr.country]
            loc = ", ".join(filter(None, parts)).strip()
            if loc:
                return loc
        parts = [obj.city, obj.state, obj.country]
        loc = ", ".join(filter(None, parts)).strip()
        return loc or "—"

    def get_orders_count(self, obj):
        if hasattr(obj, "annotated_orders_count"):
            return obj.annotated_orders_count
        if hasattr(obj, "orders"):
            return obj.orders.filter(status__in=["open", "closed"]).exclude(status="cancelled").count()
        return 0

    def get_amount_spent(self, obj):
        if hasattr(obj, "annotated_amount_spent"):
            return f"{obj.annotated_amount_spent:.2f}"
        from django.db.models import Sum
        if hasattr(obj, "orders"):
            total = obj.orders.filter(status__in=["open", "closed"]).exclude(status="cancelled").aggregate(total_sum=Sum("total"))["total_sum"]
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
