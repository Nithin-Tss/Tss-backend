from rest_framework import serializers

from apps.catalog.models import Product, ProductVariant
from apps.core.viewsets import StoreScopedSerializerMixin

from .models import DraftOrder, DraftOrderDetail, Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = [
            "id", "product", "variant", "product_name", "sku", "quantity",
            "unit_price", "discount_amount", "tax_amount", "total",
        ]
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    channel = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "customer", "customer_name", "sales_channel", "channel", "currency_code",
            "subtotal", "discount_amount", "tax_amount", "shipping_amount", "total",
            "status", "fulfillment_status", "delivery_status", "delivery_method",
            "items", "items_count", "tags", "created_at", "updated_at",
        ]
        # Totals and statuses are calculated by the system, never typed in.
        read_only_fields = [f for f in fields if f != "delivery_method"]

    def get_items(self, order):
        return OrderItemSerializer(
            [i for i in order.items.all() if not i.is_deleted], many=True
        ).data

    def get_customer_name(self, order):
        c = order.customer
        if c is None:
            return ""
        return " ".join(filter(None, [c.first_name, c.last_name])) or c.email

    def get_channel(self, order):
        return order.sales_channel.name if order.sales_channel else "Online Store"

    def get_items_count(self, order):
        return sum(i.quantity for i in order.items.all() if not i.is_deleted)

    def get_tags(self, order):
        return getattr(order, "tags", []) if hasattr(order, "tags") else []


class OrderLineInputSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), required=False, allow_null=True
    )
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)


class OrderCreateSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """A manual order created by staff in the admin."""

    items = OrderLineInputSerializer(many=True)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    shipping_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    currency_code = serializers.CharField(max_length=3, default="INR")

    class Meta:
        model = Order
        fields = [
            "customer", "sales_channel", "currency_code", "discount_amount",
            "tax_amount", "shipping_amount", "delivery_method", "items",
        ]


class DraftOrderDetailSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = DraftOrderDetail
        fields = ["id", "product", "variant", "title", "quantity", "unit_price", "discount_amount", "total"]
        read_only_fields = ["total"]
        extra_kwargs = {
            "quantity": {"min_value": 1},
            "title": {"required": False},
            "unit_price": {"required": False},
        }


class DraftOrderSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    details = DraftOrderDetailSerializer(many=True, required=False)
    created_by = serializers.CharField(source="created_by_user.email", read_only=True)
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = DraftOrder
        fields = [
            "id", "draft_number", "customer", "customer_name", "po_number", "status", "total", "currency_code",
            "order", "created_by", "details", "created_at", "updated_at",
        ]
        read_only_fields = ["draft_number", "status", "total", "order", "created_by"]
        extra_kwargs = {"currency_code": {"default": "INR"}}

    def get_customer_name(self, draft):
        c = draft.customer
        return " ".join(filter(None, [c.first_name, c.last_name])) or (c.email if c else "")
