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
    role = serializers.SerializerMethodField()

    class Meta:
        model = Store
        fields = ["storeId", "storeName", "storeSlug", "status", "createdAt", "role"]
        read_only_fields = ["status"]

    def get_role(self, store):
        """The signed-in user's role in this store ("owner", "staff", ...)."""
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return None
        membership = store.memberships.filter(user=request.user).first()
        return membership.role if membership else None

    def validate_storeName(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("Store name is required.")

        return value

    def validate_storeSlug(self, value):
        value = value.lower()

        taken = Store.objects.filter(store_slug=value)
        if self.instance is not None:
            taken = taken.exclude(pk=self.instance.pk)

        if taken.exists():
            raise serializers.ValidationError("This store URL is already taken.")

        return value
