from rest_framework import serializers

from .models import Store


class StoreSerializer(serializers.ModelSerializer):
    storeId = serializers.UUIDField(source="store_id", read_only=True)
    storeName = serializers.CharField(source="store_name", max_length=200)
    storeSlug = serializers.SlugField(
        source="store_slug",
        max_length=100,
        required=False,
    )
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Store
        fields = ["storeId", "storeName", "storeSlug", "status", "createdAt"]
        read_only_fields = ["status"]

    def validate_storeName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("Store name is required.")

        return value

    def validate_storeSlug(self, value):
        value = value.lower()

        if Store.objects.filter(store_slug=value).exists():
            raise serializers.ValidationError("This store URL is already taken.")

        return value
