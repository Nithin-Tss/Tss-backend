from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from apps.inventory.models import Location

from .models import Category, Collection, Product, ProductVariant, SalesChannel
from .services import STATUSES, THEME_TEMPLATES


class VariantInputSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    sku = serializers.CharField(required=False, allow_blank=True, max_length=100)
    price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
    )

    def to_internal_value(self, data):
        # The admin form sends "" for an empty price
        if isinstance(data, dict) and data.get("price") == "":
            data = {**data, "price": None}
        return super().to_internal_value(data)


class ProductInputSerializer(StoreScopedSerializerMixin, serializers.Serializer):
    """
    Accepts the payload of the admin "Add product" form. Categories,
    channels and locations must belong to the active store (X-Store-Id).
    """

    title = serializers.CharField(max_length=255)
    status = serializers.ChoiceField(choices=STATUSES, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    category = serializers.CharField(required=False, allow_blank=True, max_length=255)
    product_type = serializers.CharField(required=False, allow_blank=True, max_length=100)
    vendor = serializers.CharField(required=False, allow_blank=True, max_length=255)
    seo_title = serializers.CharField(required=False, allow_blank=True, max_length=255)
    seo_description = serializers.CharField(required=False, allow_blank=True)
    price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
    )
    sku = serializers.CharField(required=False, allow_blank=True, max_length=100)
    collections = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
    )
    tags = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
    )
    variants = VariantInputSerializer(many=True, required=False)
    category_id = serializers.PrimaryKeyRelatedField(
        queryset=Category.objects.all(), required=False, allow_null=True
    )
    theme_template = serializers.ChoiceField(choices=THEME_TEMPLATES, required=False)
    images = serializers.ListField(
        child=serializers.CharField(max_length=2000),
        required=False,
        max_length=50,
    )
    sales_channels = serializers.PrimaryKeyRelatedField(
        queryset=SalesChannel.objects.all(), many=True, required=False
    )
    location = serializers.PrimaryKeyRelatedField(
        queryset=Location.objects.all(), required=False, allow_null=True
    )
    quantity = serializers.IntegerField(min_value=0, required=False, allow_null=True)

    def to_internal_value(self, data):
        if hasattr(data, "get"):
            blanks = {k: None for k in ("price", "quantity", "location", "category_id") if data.get(k) == ""}
            if blanks:
                data = {**data, **blanks}
        return super().to_internal_value(data)

    def validate_images(self, value):
        for url in value:
            if not url.startswith(("http://", "https://", "/media/")):
                raise serializers.ValidationError("Each image must be an uploaded image URL.")
        return value

    def validate(self, data):
        if data.get("quantity") is not None and data.get("location") is None:
            raise serializers.ValidationError({"location": "Choose a location for this stock."})
        return data

    def validate_title(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("Title is required.")

        return value


class ProductSerializer(serializers.ModelSerializer):
    """What the admin product list and detail views receive."""

    category = serializers.CharField(source="category.name", default="", read_only=True)
    category_id = serializers.UUIDField(read_only=True, allow_null=True)
    price = serializers.SerializerMethodField()
    sku = serializers.SerializerMethodField()
    collections = serializers.SerializerMethodField()
    tags = serializers.SerializerMethodField()
    variants = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    sales_channels = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "title", "status", "description", "category", "category_id", "product_type",
            "vendor", "seo_title", "seo_description", "theme_template", "price", "sku",
            "collections", "tags", "variants", "images", "sales_channels", "createdAt", "updatedAt",
        ]

    def _variants(self, product):
        return sorted(product.variants.all(), key=lambda v: v.created_at)

    def get_price(self, product):
        variants = self._variants(product)
        return str(min(v.price for v in variants)) if variants else None

    def get_sku(self, product):
        variants = self._variants(product)
        return variants[0].sku or "" if variants else ""

    def get_collections(self, product):
        return [pc.collection.name for pc in product.product_collections.all()]

    def get_tags(self, product):
        return [pt.tag.name for pt in product.product_tags.all()]

    def get_variants(self, product):
        return [
            {"id": str(v.id), "sku": v.sku or "", "price": str(v.price)}
            for v in self._variants(product)
        ]

    def get_images(self, product):
        return [
            {"url": i.url, "position": i.position}
            for i in sorted(product.images.all(), key=lambda i: i.position)
        ]

    def get_sales_channels(self, product):
        return [str(pc.sales_channel_id) for pc in product.product_sales_channels.all()]


class CategorySerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """Categories, with an optional parent for subcategories."""

    name = serializers.CharField(max_length=255)

    class Meta:
        model = Category
        fields = ["id", "name", "parent"]

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Name is required.")
        return value


class SalesChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = SalesChannel
        fields = ["id", "name", "type"]


class CollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = ["id", "name"]


class VariantSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """A variant on its own: /catalog/variants/."""

    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    sku = serializers.CharField(required=False, allow_blank=True, allow_null=True, max_length=100)

    class Meta:
        model = ProductVariant
        fields = ["id", "product", "sku", "price", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_product(self, value):
        if self.instance and value.pk != self.instance.product_id:
            raise serializers.ValidationError("A variant can't be moved to another product.")
        return value
