from rest_framework import serializers

from apps.catalog.models import EXTRA_FIELDS

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
    fieldLabels = serializers.JSONField(source="field_labels", required=False)
    role = serializers.SerializerMethodField()

    class Meta:
        model = Store
        fields = ["storeId", "storeName", "storeSlug", "status", "createdAt", "fieldLabels", "role"]
        read_only_fields = ["status"]

    def get_role(self, store):
        """The signed-in user's role in this store ("owner", "staff", ...)."""
        request = self.context.get("request")
        if request is None or not request.user.is_authenticated:
            return None
        membership = store.memberships.filter(user=request.user).first()
        return membership.role if membership else None

    def validate_fieldLabels(self, value):
        """Only the generic "columnN" fields can be renamed; blank resets to the default."""
        if not isinstance(value, dict):
            raise serializers.ValidationError("Field labels must be an object.")

        allowed = [f for f in EXTRA_FIELDS if f.startswith("column")]
        cleaned = {}
        for key, label in value.items():
            if key not in allowed:
                raise serializers.ValidationError(f"{key} can't be renamed.")
            if not isinstance(label, str):
                raise serializers.ValidationError("Each label must be text.")
            label = " ".join(label.split())
            if len(label) > 60:
                raise serializers.ValidationError("A field name can be at most 60 characters.")
            if label:
                cleaned[key] = label

        if len({v.lower() for v in cleaned.values()}) != len(cleaned):
            raise serializers.ValidationError("Each field needs a different name.")

        return cleaned

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
