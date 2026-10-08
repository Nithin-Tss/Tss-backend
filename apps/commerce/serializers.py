from rest_framework import serializers

from .models import Cart, CartItem, Checkout, CheckoutItem


class CartItemSerializer(serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title", read_only=True)

    class Meta:
        model = CartItem
        fields = ["id", "product", "product_title", "variant", "quantity", "unit_price", "created_at"]
        read_only_fields = fields


class CartSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = Cart
        fields = ["id", "customer", "session_id", "currency", "status", "items", "created_at", "updated_at"]
        read_only_fields = fields

    def get_items(self, cart):
        return CartItemSerializer([i for i in cart.items.all() if not i.is_deleted], many=True).data


class CheckoutItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = CheckoutItem
        fields = ["id", "product", "variant", "product_name", "sku", "quantity", "unit_price", "total_price"]
        read_only_fields = fields


class CheckoutSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()

    class Meta:
        model = Checkout
        fields = [
            "id", "cart", "customer", "email", "currency", "subtotal", "discount_amount",
            "tax_amount", "shipping_amount", "total_amount", "status", "shipping_address",
            "billing_address", "abandoned_at", "recovery_status", "recovered_at",
            "items", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_items(self, checkout):
        return CheckoutItemSerializer([i for i in checkout.items.all() if not i.is_deleted], many=True).data
