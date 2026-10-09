from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import ProtectedError
from rest_framework.exceptions import ValidationError

from .models import (
    Category,
    Collection,
    Product,
    ProductCollection,
    ProductTag,
    ProductVariant,
    Tag,
)

ACTIVE = "active"
DRAFT = "draft"
STATUSES = (ACTIVE, DRAFT)


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

    for field in ("title", "description", "product_type", "vendor", "seo_title", "seo_description"):
        if field in data:
            setattr(product, field, data[field])

    product.status = data.get("status", product.status or DRAFT)

    if "category" in data:
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

    if "variants" in data or "price" in data:
        try:
            _replace_variants(product, data)
        except ProtectedError:
            raise ProductInUse("A variant that is used by orders or inventory can't be removed.")

    return product


@transaction.atomic
def delete_product(product):
    """Delete a product and everything hanging off it, or raise ProductInUse."""
    try:
        product.product_collections.all().delete()
        product.product_tags.all().delete()
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
