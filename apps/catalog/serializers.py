from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from apps.inventory.models import Location

from .models import EXTRA_FIELDS, Category, Collection, Product, ProductVariant, SalesChannel
from .services import STATUSES, THEME_TEMPLATES, check_skus


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

    def validate_sku(self, value):
        return value.strip()

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
    theme_template = serializers.ChoiceField(
        choices=THEME_TEMPLATES, required=False, allow_null=True, allow_blank=True
    )
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

    def get_fields(self):
        fields = super().get_fields()
        for name in EXTRA_FIELDS:
            fields[name] = serializers.CharField(required=False, allow_blank=True, allow_null=True)
        return fields

    def to_internal_value(self, data):
        if hasattr(data, "get"):
            blanks = {
                k: None
                for k in ("price", "quantity", "location", "category_id", "theme_template")
                if data.get(k) == ""
            }
            if blanks:
                data = {**data, **blanks}
        return super().to_internal_value(data)

    def validate_variants(self, rows):
        ids = [str(r["id"]) for r in rows if r.get("id")]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("A variant is listed more than once.")
        return rows

    def validate_images(self, value):
        for url in value:
            if not url.startswith(("http://", "https://", "/media/")):
                raise serializers.ValidationError("Each image must be an uploaded image URL.")
        return value

    def validate(self, data):
        # Imports may leave the location out: their stock goes to the store's first location
        if self.context.get("location_optional"):
            return data
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
            *EXTRA_FIELDS,
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
    products_count = serializers.SerializerMethodField()
    sales_channels = serializers.SerializerMethodField()
    sales_channels_count = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            "id",
            "name",
            "description",
            "image_url",
            "conditions",
            "theme_template",
            "seo_title",
            "seo_description",
            "products_count",
            "sales_channels",
            "sales_channels_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_products_count(self, obj):
        if hasattr(obj, "products_count_annotated"):
            return obj.products_count_annotated
        return obj.product_collections.count()

    def get_sales_channels_count(self, obj):
        if hasattr(obj, "sales_channels_count_annotated"):
            return obj.sales_channels_count_annotated
        return obj.collection_sales_channels.count()

    def get_sales_channels(self, obj):
        channels = [
            csc.sales_channel
            for csc in obj.collection_sales_channels.select_related("sales_channel").all()
        ]
        return [
            {"id": str(c.id), "name": c.name, "type": c.type}
            for c in channels
        ]

    def create(self, validated_data):
        sales_channels_data = self.initial_data.get("sales_channels", None)
        instance = super().create(validated_data)
        self._sync_sales_channels(instance, sales_channels_data, creating=True)
        return instance

    def update(self, instance, validated_data):
        sales_channels_data = self.initial_data.get("sales_channels", None)
        instance = super().update(instance, validated_data)
        if sales_channels_data is not None:
            self._sync_sales_channels(instance, sales_channels_data, creating=False)
        return instance

    def _sync_sales_channels(self, instance, sales_channels_data, creating=False):
        import uuid
        from apps.catalog.models import CollectionSalesChannel, SalesChannel
        from apps.catalog.services import online_channels

        if sales_channels_data is not None:
            instance.collection_sales_channels.all().delete()
            channel_ids = []
            for item in sales_channels_data:
                if isinstance(item, dict) and "id" in item:
                    channel_ids.append(item["id"])
                elif isinstance(item, (str, uuid.UUID)):
                    channel_ids.append(item)
            channels = SalesChannel.objects.filter(
                store=instance.store, id__in=channel_ids
            )
            CollectionSalesChannel.objects.bulk_create(
                [
                    CollectionSalesChannel(collection=instance, sales_channel=c)
                    for c in channels
                ]
            )
        elif creating:
            channels = online_channels(instance.store)
            CollectionSalesChannel.objects.bulk_create(
                [
                    CollectionSalesChannel(collection=instance, sales_channel=c)
                    for c in channels
                ]
            )


class VariantSerializer(StoreScopedSerializerMixin, serializers.ModelSerializer):
    """A variant on its own: /catalog/variants/."""

    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    sku = serializers.CharField(max_length=100)

    class Meta:
        model = ProductVariant
        fields = ["id", "product", "sku", "price", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate_sku(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("SKU is required.")
        request = self.context.get("request")
        store = getattr(request, "store", None)
        if value and store is not None:
            check_skus(store, [value], exclude_variant=self.instance)
        return value

    def validate_product(self, value):
        if self.instance and value.pk != self.instance.product_id:
            raise serializers.ValidationError("A variant can't be moved to another product.")
        return value
