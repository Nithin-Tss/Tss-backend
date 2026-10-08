from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from .models import Wishlist, WishlistItem


class WishlistItemSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    product_title = serializers.CharField(source="product.title", read_only=True)

    class Meta:
        model = WishlistItem
        fields = ["id", "wishlist", "product", "product_title", "variant", "created_at"]

    def validate(self, data):
        variant = data.get("variant")
        if variant and variant.product_id != data["product"].pk:
            raise serializers.ValidationError({"variant": "This variant belongs to a different product."})
        return data


class WishlistSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    items = WishlistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Wishlist
        fields = ["id", "customer", "name", "items", "created_at", "updated_at"]
