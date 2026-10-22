from rest_framework import serializers

from apps.catalog.models import Product, ProductVariant
from apps.core.viewsets import StoreScopedSerializerMixin

from .models import DraftOrder, DraftOrderDetail, Order, OrderItem


class OrderItemSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = OrderItem
        fields = [
            "id", "product", "variant", "product_name", "sku", "quantity",
            "unit_price", "discount_amount", "tax_amount", "total", "image_url",
        ]
        read_only_fields = fields

    def get_image_url(self, item):
        if item.product_id:
            img = item.product.images.order_by("position").first()
            if img:
                return img.url
        return None


class OrderSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    customer_name = serializers.SerializerMethodField()
    channel = serializers.SerializerMethodField()
    items_count = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id", "order_number", "customer", "customer_name", "sales_channel", "channel", "currency_code",
            "subtotal", "discount_amount", "tax_amount", "shipping_amount", "total",
            "status", "payment_status", "fulfillment_status", "delivery_status", "delivery_method",
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

    def get_payment_status(self, order):
        payments = [payment for payment in order.payments.all() if not payment.is_deleted]
        if not payments:
            return "pending"

        amount_paid = sum(
            payment.amount - sum(
                transaction.amount
                for transaction in payment.transactions.all()
                if transaction.transaction_type == "refund" and transaction.status == "success"
            )
            for payment in payments
        )
        if any(payment.status in ("partially_refunded", "refunded") for payment in payments):
            return "refunded" if amount_paid == 0 else "partially_refunded"
        if amount_paid >= order.total:
            return "paid"
        return "partially_paid" if amount_paid > 0 else "pending"


class OrderLineInputSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    product = serializers.PrimaryKeyRelatedField(queryset=Product.objects.all())
    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), required=False, allow_null=True
    )
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)


class CurrencyCodeField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError("Enter a valid three-letter currency code.")
        return value


class OrderCreateSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """A manual order created by staff in the admin."""

    items = OrderLineInputSerializer(many=True)
    discount_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    tax_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    shipping_amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, default=0)
    currency_code = CurrencyCodeField(max_length=3, default="INR")

    class Meta:
        model = Order
        fields = [
            "customer", "sales_channel", "currency_code", "discount_amount",
            "tax_amount", "shipping_amount", "delivery_method", "items",
        ]


class DraftOrderDetailSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    product = serializers.PrimaryKeyRelatedField(
        queryset=Product.objects.all(), required=False, allow_null=True
    )
    variant = serializers.PrimaryKeyRelatedField(
        queryset=ProductVariant.objects.all(), required=False, allow_null=True
    )
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False
    )
    discount_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=0, required=False, allow_null=True
    )

    class Meta:
        model = DraftOrderDetail
        fields = ["id", "product", "variant", "title", "quantity", "unit_price", "discount_amount", "total"]
        read_only_fields = ["total"]
        extra_kwargs = {
            "quantity": {"min_value": 1},
            "title": {"required": False},
        }

    def validate(self, attrs):
        product = attrs.get("product")
        variant = attrs.get("variant")
        if variant is not None and product is None:
            raise serializers.ValidationError(
                {"product": "A product is required when a variant is selected."}
            )
        if variant is not None and variant.product_id != product.pk:
            raise serializers.ValidationError(
                {"variant": "The variant doesn't belong to the selected product."}
            )
        if product is None and not attrs.get("title", "").strip():
            raise serializers.ValidationError(
                {"title": "Custom items need a title."}
            )

        unit_price = attrs.get("unit_price")
        discount = attrs.get("discount_amount") or 0
        if unit_price is not None and discount > unit_price * attrs.get("quantity", 1):
            raise serializers.ValidationError(
                {"discount_amount": "The discount can't exceed the item subtotal."}
            )
        return attrs


class DraftOrderSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    details = DraftOrderDetailSerializer(many=True, required=False)
    created_by = serializers.CharField(source="created_by_user.email", read_only=True)
    customer_name = serializers.SerializerMethodField()
    currency_code = CurrencyCodeField(max_length=3, default="INR")

    class Meta:
        model = DraftOrder
        fields = [
            "id", "draft_number", "customer", "customer_name", "po_number", "status", "total", "currency_code",
            "order", "created_by", "details", "created_at", "updated_at",
        ]
        read_only_fields = ["draft_number", "status", "total", "order", "created_by"]

    def get_customer_name(self, draft):
        c = draft.customer
        if c is None:
            return ""
        return " ".join(filter(None, [c.first_name, c.last_name])) or c.email
