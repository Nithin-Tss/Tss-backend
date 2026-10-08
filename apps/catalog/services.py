from decimal import Decimal, InvalidOperation

from django.db import transaction

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
        _replace_variants(product, data)

    return product


def _replace_variants(product, data):
    rows = data.get("variants") or [
        {"price": data.get("price"), "sku": data.get("sku", "")}
    ]
    existing = list(product.variants.order_by("created_at"))

    # Update in place where possible, so variants referenced by
    # orders or inventory keep their ids.
    for i, row in enumerate(rows):
        variant = existing[i] if i < len(existing) else ProductVariant(product=product)
        variant.price = _price(row.get("price") or data.get("price"))
        variant.sku = (row.get("sku") or "").strip() or None
        variant.save()

    for variant in existing[len(rows):]:
        variant.delete()
