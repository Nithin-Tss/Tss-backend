from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import ProtectedError
from rest_framework.exceptions import ValidationError

from apps.inventory.services import set_available

from .models import (
    Category,
    Collection,
    Product,
    ProductCollection,
    ProductImage,
    ProductSalesChannel,
    ProductTag,
    ProductVariant,
    SalesChannel,
    Tag,
)

ACTIVE = "active"
DRAFT = "draft"
ARCHIVED = "archived"
STATUSES = (ACTIVE, DRAFT, ARCHIVED)

# Product page templates a theme can render (Product.theme_template)
THEME_TEMPLATES = ("default", "featured", "minimal")

# Every store sells through these unless it adds its own
DEFAULT_CHANNELS = (("Online Store", "online"), ("Point of Sale", "pos"))


def sales_channels_for(store):
    """The store's sales channels, creating the default ones the first time."""
    channels = SalesChannel.objects.filter(store=store)
    if not channels.exists():
        SalesChannel.objects.bulk_create(
            [SalesChannel(store=store, name=name, type=kind) for name, kind in DEFAULT_CHANNELS]
        )
    return SalesChannel.objects.filter(store=store).order_by("created_at")


class ProductInUse(Exception):
    """The product, or one of its variants, is referenced by orders or inventory."""


def _price(value):
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _named(model, store, name, field="name"):
    obj, _ = model.objects.get_or_create(store=store, **{field: name})
    return obj


def published_products(store):
    """Products a shopper may see on the storefront."""
    return (
        Product.objects.filter(store=store, status=ACTIVE)
        .prefetch_related("images", "variants")
        .order_by("-created_at")
    )


@transaction.atomic
def save_product(store, data, product=None):
    """
    Create or update a product from the admin form, including its
    category, collections, tags and variants, as one unit.
    """
    product = product or Product(store=store)

    for field in ("title", "description", "product_type", "vendor", "seo_title", "seo_description", "theme_template"):
        if field in data:
            setattr(product, field, data[field])

    product.status = data.get("status", product.status or DRAFT)

    # category_id: picked from the store's categories; category: a name (older clients)
    if "category_id" in data:
        product.category = data["category_id"]
    elif "category" in data:
        name = (data["category"] or "").strip()
        product.category = _named(Category, store, name) if name else None

    product.save()

    if "collections" in data:
        ProductCollection.objects.filter(product=product).delete()
        for name in {n.strip() for n in data["collections"] if n.strip()}:
            ProductCollection.objects.create(
                product=product,
                collection=_named(Collection, store, name),
            )

    if "tags" in data:
        ProductTag.objects.filter(product=product).delete()
        for name in {n.strip() for n in data["tags"] if n.strip()}:
            ProductTag.objects.create(product=product, tag=_named(Tag, store, name))

    if "images" in data:
        # The list order is the display order; the first image is the main one
        product.images.all().delete()
        ProductImage.objects.bulk_create(
            [ProductImage(product=product, url=url, position=i) for i, url in enumerate(data["images"])]
        )

    if "sales_channels" in data:
        ProductSalesChannel.objects.filter(product=product).delete()
        ProductSalesChannel.objects.bulk_create(
            [ProductSalesChannel(product=product, sales_channel=c) for c in dict.fromkeys(data["sales_channels"])]
        )

    if "variants" in data or "price" in data or not product.variants.exists():
        try:
            _replace_variants(product, data)
        except ProtectedError:
            raise ProductInUse("A variant that is used by orders or inventory can't be removed.")

    # Opening stock for the chosen location, on the product's first variant
    if data.get("location") is not None and data.get("quantity") is not None:
        set_available(product.variants.order_by("created_at").first(), data["location"], data["quantity"])

    return product


@transaction.atomic
def delete_product(product):
    """Delete a product and everything hanging off it, or raise ProductInUse."""
    try:
        product.product_collections.all().delete()
        product.product_tags.all().delete()
        product.product_sales_channels.all().delete()
        product.images.all().delete()
        product.variants.all().delete()
        product.delete()
    except ProtectedError:
        raise ProductInUse("This product is used by orders or inventory. Set it to Draft instead.")


def _apply_variant(variant, row, default_price=None):
    variant.price = _price(row.get("price") or default_price)
    variant.sku = (row.get("sku") or "").strip() or None
    variant.save()
    return variant


def _replace_variants(product, data):
    """
    Sync the product's variants with the submitted rows.

    If any row carries an `id`, variants are matched by id: rows without one
    are created, and variants not listed are deleted. If no row has an id
    (a plain price/sku, or an older client), rows are matched by position.
    Either way, variants referenced by orders or inventory keep their ids.
    """
    rows = data.get("variants") or [
        {"price": data.get("price"), "sku": data.get("sku", "")}
    ]
    existing = list(product.variants.order_by("created_at"))
    default_price = data.get("price")

    if any(row.get("id") for row in rows):
        by_id = {str(v.id): v for v in existing}
        keep = set()
        for row in rows:
            key = str(row["id"]) if row.get("id") else None
            if key and key not in by_id:
                raise ValidationError({"variants": f"Variant {key} doesn't belong to this product."})
            if key in keep:
                raise ValidationError({"variants": f"Variant {key} is listed twice."})
            variant = by_id[key] if key else ProductVariant(product=product)
            _apply_variant(variant, row, default_price)
            keep.add(str(variant.id))
        for variant in existing:
            if str(variant.id) not in keep:
                variant.delete()
        return

    for i, row in enumerate(rows):
        variant = existing[i] if i < len(existing) else ProductVariant(product=product)
        _apply_variant(variant, row, default_price)

    for variant in existing[len(rows):]:
        variant.delete()


def create_variant(product, data):
    return _apply_variant(ProductVariant(product=product), data)


def update_variant(variant, data):
    row = {"price": data.get("price", variant.price), "sku": data.get("sku", variant.sku)}
    return _apply_variant(variant, row)


def delete_variant(variant):
    """Delete one variant, unless it is the product's last or is in use."""
    if variant.product.variants.count() <= 1:
        raise ProductInUse("A product needs at least one variant.")
    try:
        variant.delete()
    except ProtectedError:
        raise ProductInUse("This variant is used by orders or inventory, so it can't be deleted.")
