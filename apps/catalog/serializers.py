from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils.text import slugify
from rest_framework import serializers

from apps.core.viewsets import StoreScopedSerializerMixin

from apps.inventory.models import Location

from .models import (
    EXTRA_FIELDS, RESERVED_HANDLES, Category, Collection, CollectionRule, Product, ProductVariant, SalesChannel,
    unique_handle,
)
from .services import (
    RULE_FIELDS, RULE_OPERATORS, STATUSES, THEME_TEMPLATES, check_skus, set_manual_products, sync_collection,
)


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
    continue_selling = serializers.BooleanField(required=False)

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
    continue_selling = serializers.BooleanField(required=False)

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
    quantity = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    sales_channels = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source="created_at", read_only=True)
    updatedAt = serializers.DateTimeField(source="updated_at", read_only=True)

    class Meta:
        model = Product
        fields = [
            "id", "title", "status", "description", "category", "category_id", "product_type",
            "vendor", "seo_title", "seo_description", "theme_template", "price", "sku",
            "collections", "tags", "variants", "quantity", "images", "sales_channels", "createdAt", "updatedAt",
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

    def get_quantity(self, product):
        """Available stock over every variant and location (what the store sells)."""
        return sum(i.available for v in product.variants.all() for i in v.inventory_items.all())

    def get_variants(self, product):
        return [
            {
                "id": str(v.id),
                "sku": v.sku or "",
                "price": str(v.price),
                "continue_selling": v.continue_selling,
                "quantity": sum(i.available for i in v.inventory_items.all()),
            }
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


class CollectionRuleSerializer(serializers.Serializer):
    """One condition of an automated collection: {field, operator, value}."""

    field = serializers.ChoiceField(choices=list(RULE_FIELDS))
    operator = serializers.CharField(max_length=20)
    value = serializers.CharField(max_length=255)

    def validate(self, data):
        kind, label = RULE_FIELDS[data["field"]]
        if data["operator"] not in RULE_OPERATORS[kind]:
            raise serializers.ValidationError({"operator": f"{label} can't use this comparison."})
        if kind == "number":
            try:
                number = Decimal(data["value"])
            except InvalidOperation:
                raise serializers.ValidationError({"value": f"{label} must be a number."})
            if not number.is_finite():
                raise serializers.ValidationError({"value": f"{label} must be a number."})
        return data


class CollectionSerializer(serializers.ModelSerializer):
    """
    A collection with its rules (automated) or products (manual).

    products: ids of a manual collection's products (read: the products it holds now).
    sales_channels: channel ids; a new collection without them goes on the Online Store.
    """

    name = serializers.CharField(max_length=255)
    handle = serializers.CharField(max_length=255, required=False, allow_blank=True)
    type = serializers.ChoiceField(choices=Collection.TYPES, required=False)
    status = serializers.ChoiceField(choices=Collection.STATUSES, required=False)
    match = serializers.ChoiceField(choices=("all", "any"), required=False)
    rules = CollectionRuleSerializer(many=True, required=False)
    products = serializers.ListField(child=serializers.UUIDField(), required=False, write_only=True, max_length=5000)
    excluded_products = serializers.ListField(child=serializers.UUIDField(), required=False, max_length=5000)
    products_count = serializers.SerializerMethodField()
    sales_channels = serializers.SerializerMethodField()
    sales_channels_count = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            "id",
            "name",
            "handle",
            "type",
            "status",
            "description",
            "image_url",
            "match",
            "rules",
            "products",
            "excluded_products",
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
        channels = self.get_sales_channels(obj)
        return len(channels)

    def get_sales_channels(self, obj):
        from apps.catalog.models import SalesChannel

        if hasattr(obj, "_prefetched_objects_cache") and "product_collections" in obj._prefetched_objects_cache:
            channels_dict = {}
            for pc in obj.product_collections.all():
                product = getattr(pc, "product", None)
                if (
                    product
                    and hasattr(product, "_prefetched_objects_cache")
                    and "product_sales_channels" in product._prefetched_objects_cache
                ):
                    for psc in product.product_sales_channels.all():
                        ch = getattr(psc, "sales_channel", None)
                        if ch:
                            key = (ch.name, ch.type)
                            if key not in channels_dict:
                                channels_dict[key] = ch
            if channels_dict:
                return [
                    {"id": str(c.id), "name": c.name, "type": c.type}
                    for c in sorted(channels_dict.values(), key=lambda c: c.created_at)
                ]
            if hasattr(obj, "sales_channels_count_annotated") and obj.sales_channels_count_annotated == 0:
                return []

        channels = (
            SalesChannel.objects.filter(
                product_sales_channels__product__product_collections__collection=obj
            )
            .distinct()
            .order_by("created_at")
        )
        seen = set()
        unique_channels = []
        for c in channels:
            key = (c.name, c.type)
            if key not in seen:
                seen.add(key)
                unique_channels.append(c)

        return [
            {"id": str(c.id), "name": c.name, "type": c.type}
            for c in unique_channels
        ]

    def to_representation(self, obj):
        data = super().to_representation(obj)
        data["products"] = [str(pc.product_id) for pc in obj.product_collections.all()]
        return data

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Title is required.")
        return value

    def validate_handle(self, value):
        return slugify(value)[:255].strip("-")

    def validate_excluded_products(self, value):
        return [str(pk) for pk in dict.fromkeys(value)]

    def validate(self, attrs):
        request = self.context.get("request")
        store = getattr(request, "store", None) or getattr(self.instance, "store", None)
        current = self.instance

        # Handle: typed by the owner, or made from the title; unique in the store
        if attrs.get("handle"):
            handle = attrs["handle"]
            if handle in RESERVED_HANDLES:
                raise serializers.ValidationError({"handle": f'"{handle}" is reserved. Choose another URL handle.'})
            taken = Collection.objects.filter(store=store, handle=handle)
            if current is not None:
                taken = taken.exclude(pk=current.pk)
            if taken.exists():
                raise serializers.ValidationError({"handle": "Another collection already uses this URL handle."})
        elif "handle" in attrs or current is None:
            name = attrs.get("name") or (current.name if current else "")
            attrs["handle"] = unique_handle(store, name, exclude_pk=current.pk if current else None)

        kind = attrs.get("type", current.type if current else Collection.MANUAL)
        if kind == Collection.AUTOMATED:
            has_rules = bool(attrs["rules"]) if "rules" in attrs else bool(current and current.rules.exists())
            if not has_rules:
                raise serializers.ValidationError({"rules": "Add at least one condition."})
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        rules, products = validated_data.pop("rules", None), validated_data.pop("products", None)
        sales_channels_data = self.initial_data.get("sales_channels", None)
        instance = super().create(validated_data)
        self._sync_sales_channels(instance, sales_channels_data, creating=True)
        self._save_items(instance, rules, products)
        return instance

    @transaction.atomic
    def update(self, instance, validated_data):
        rules, products = validated_data.pop("rules", None), validated_data.pop("products", None)
        sales_channels_data = self.initial_data.get("sales_channels", None)
        instance = super().update(instance, validated_data)
        if sales_channels_data is not None:
            self._sync_sales_channels(instance, sales_channels_data, creating=False)
        self._save_items(instance, rules, products)
        return instance

    def _save_items(self, instance, rules, products):
        if rules is not None:
            instance.rules.all().delete()
            CollectionRule.objects.bulk_create([
                CollectionRule(store=instance.store, collection=instance, position=i, **rule)
                for i, rule in enumerate(rules)
            ])
        # Counts and prefetched rows read before this save are out of date now
        instance._prefetched_objects_cache = {}
        for attr in ("products_count_annotated", "sales_channels_count_annotated"):
            instance.__dict__.pop(attr, None)

        if instance.type == Collection.AUTOMATED:
            sync_collection(instance)
        elif products is not None:
            set_manual_products(instance, products)

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
