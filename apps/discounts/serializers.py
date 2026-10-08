from rest_framework import serializers

from apps.catalog.models import Product
from apps.core.viewsets import StoreScopedSerializerMixin

from .models import Discount, DiscountCode
from .services import PERCENTAGE, TYPES, times_used


class DiscountSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    discount_type = serializers.ChoiceField(choices=TYPES)
    codes = serializers.ListField(
        child=serializers.CharField(max_length=50), required=False, write_only=True,
        help_text="Codes customers type at checkout, e.g. SUMMER10",
    )
    products = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(), many=True, required=False, write_only=True,
        help_text="Limit the discount to these products (empty = whole order)",
    )
    times_used = serializers.SerializerMethodField()

    class Meta:
        model = Discount
        fields = [
            "id", "name", "discount_type", "value", "minimum_order_amount", "maximum_discount_amount",
            "starts_at", "ends_at", "usage_limit", "per_customer_limit", "is_active",
            "codes", "products", "times_used", "created_at", "updated_at",
        ]
        extra_kwargs = {"value": {"min_value": 0}, "is_active": {"default": True}}

    def get_times_used(self, discount):
        return times_used(discount)

    def to_representation(self, discount):
        # Codes and products live in their own tables (DiscountCode, DiscountProduct).
        data = super().to_representation(discount)
        data["codes"] = [c.code for c in discount.codes.filter(is_deleted=False).order_by("code")]
        data["products"] = [str(p.product_id) for p in discount.products.filter(is_deleted=False)]
        return data

    def validate_codes(self, codes):
        cleaned = sorted({c.strip().upper() for c in codes if c.strip()})
        store = self.context["request"].store
        taken = DiscountCode.objects.filter(
            discount__store=store, code__in=cleaned, is_deleted=False
        )
        if self.instance:
            taken = taken.exclude(discount=self.instance)
        if taken.exists():
            raise serializers.ValidationError(f"Code {taken.first().code} is already used by another discount.")
        return cleaned

    def validate(self, data):
        kind = data.get("discount_type", getattr(self.instance, "discount_type", None))
        value = data.get("value", getattr(self.instance, "value", 0))
        if kind == PERCENTAGE and value > 100:
            raise serializers.ValidationError({"value": "A percentage can't be more than 100."})

        starts = data.get("starts_at", getattr(self.instance, "starts_at", None))
        ends = data.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts and ends and ends <= starts:
            raise serializers.ValidationError({"ends_at": "Must be after the start date."})
        return data


class CodeCheckSerializer(serializers.Serializer):
    code = serializers.CharField()
    subtotal = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
