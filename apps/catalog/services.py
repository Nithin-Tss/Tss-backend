import threading
from decimal import Decimal, InvalidOperation

from django.db import connection, transaction
from django.db.models import Exists, OuterRef, PROTECT, ProtectedError, Q, Sum
from django.db.models.functions import Coalesce
from rest_framework.exceptions import ValidationError

from apps.inventory.services import default_location, set_available

from .models import (
    EXTRA_FIELDS,
    Category,
    Collection,
    CollectionSalesChannel,
    Product,
    ProductCollection,
    ProductImage,
    ProductSalesChannel,
    ProductTag,
    ProductVariant,
    SalesChannel,
    Tag,
    unique_handle,
)

ACTIVE = "active"
DRAFT = "draft"
ARCHIVED = "archived"
STATUSES = (ACTIVE, DRAFT, ARCHIVED)

# Product page templates a theme can render (Product.theme_template)
THEME_TEMPLATES = ("default", "featured", "minimal")

# Every store sells through these unless it adds its own
ONLINE = "online"
DEFAULT_CHANNELS = (("Online Store", ONLINE), ("Point of Sale", "pos"))


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


def check_skus(store, skus, exclude_product=None, exclude_variant=None, allowed_skus=None):
    """Raise a ValidationError naming any SKU that repeats in `skus` or is already used in the store."""
    skus = [s for s in skus if s]
    repeated = sorted({s for s in skus if skus.count(s) > 1})
    if repeated:
        raise ValidationError({"variants": f"SKU {', '.join(repeated)} is used more than once."})

    skus_to_check = skus
    if allowed_skus:
        skus_to_check = [s for s in skus if s not in allowed_skus]
    elif exclude_variant is not None and getattr(exclude_variant, "sku", None):
        skus_to_check = [s for s in skus if s != exclude_variant.sku]

    if not skus_to_check:
        return

    taken = ProductVariant.objects.filter(product__store=store, sku__in=skus_to_check)
    if exclude_product is not None:
        taken = taken.exclude(product=exclude_product)
    if exclude_variant is not None:
        taken = taken.exclude(pk=exclude_variant.pk)
    used = sorted(set(taken.values_list("sku", flat=True)))
    if used:
        raise ValidationError({"sku": f"SKU {', '.join(used)} is already used by another variant."})


def _named(model, store, name, field="name"):
    # Collection names may repeat (their handles are unique): use the first one
    obj = model.objects.filter(store=store, **{field: name}).order_by("pk").first()
    if obj is None:
        obj = model.objects.create(store=store, **{field: name})
        if model is Collection:
            _put_on_online_store([obj])
    return obj


def _put_on_online_store(collections):
    """New collections made from the product form or an import show on the store, like products do."""
    online = online_channels(collections[0].store) if collections else []
    CollectionSalesChannel.objects.bulk_create(
        [CollectionSalesChannel(collection=c, sales_channel=ch) for c in collections for ch in online],
        ignore_conflicts=True,
    )


# ---------------------------------------------------------------------------
# Collections. A manual collection holds the products picked for it; an
# automated one holds every product matching its rules, refreshed whenever a
# product, variant, tag or stock level changes (see signals.py). Both keep
# their products in collection_products (ProductCollection), so the store and
# the product form read them the same way.
# ---------------------------------------------------------------------------

# field: (kind, label). Text rules ignore upper/lower case.
RULE_FIELDS = {
    "product_category": ("text", "Product category"),
    "tag": ("text", "Tag"),
    "vendor": ("text", "Vendor"),
    "product_type": ("text", "Product type"),
    "price": ("number", "Price"),
    "inventory_stock": ("number", "Inventory stock"),
    "status": ("text", "Status"),
}
RULE_OPERATORS = {
    "text": ("equals", "not_equals", "contains"),
    "number": ("equals", "not_equals", "greater_than", "less_than"),
}
_TEXT_PATHS = {
    "product_category": "category__name",
    "tag": "product_tags__tag__name",
    "vendor": "vendor",
    "product_type": "product_type",
    "status": "status",
}
_NUMBER_LOOKUPS = {"equals": "exact", "greater_than": "gt", "less_than": "lt"}


def _rule_q(store, field, operator, value):
    """Q for one rule. Each rule is its own subquery, so "tag = A and tag = B" means both tags."""
    base = Product.objects.filter(store=store)
    negate = operator == "not_equals"
    if negate:
        operator = "equals"

    if field in _TEXT_PATHS:
        lookup = "icontains" if operator == "contains" else "iexact"
        matching = base.filter(**{f"{_TEXT_PATHS[field]}__{lookup}": value.strip()})
    else:
        number = Decimal(str(value))
        lookup = _NUMBER_LOOKUPS[operator]
        if field == "price":
            matching = base.filter(**{f"variants__price__{lookup}": number})
        else:  # inventory_stock: available over every variant and location
            matching = base.annotate(
                stock=Coalesce(Sum("variants__inventory_items__available"), 0)
            ).filter(**{f"stock__{lookup}": number})

    q = Q(pk__in=matching.values("pk"))
    return ~q if negate else q


def matching_products(store, rules, match="all", excluded=()):
    """The store's products matching `rules` ([{field, operator, value}]); none when there are no rules."""
    if not rules:
        return Product.objects.none()
    combined = None
    for rule in rules:
        q = _rule_q(store, rule["field"], rule["operator"], rule["value"])
        combined = q if combined is None else (combined | q if match == "any" else combined & q)
    return Product.objects.filter(store=store).filter(combined).exclude(pk__in=list(excluded))


def _rules_of(collection):
    return [{"field": r.field, "operator": r.operator, "value": r.value} for r in collection.rules.all()]


def sync_collection(collection, product_ids=None):
    """Make an automated collection's products match its rules (only `product_ids`, when given)."""
    if collection.type != Collection.AUTOMATED:
        return
    wanted = matching_products(collection.store_id, _rules_of(collection), collection.match, collection.excluded_products)
    current = ProductCollection.objects.filter(collection=collection)
    if product_ids is not None:
        wanted = wanted.filter(pk__in=product_ids)
        current = current.filter(product_id__in=product_ids)
    wanted = set(wanted.values_list("pk", flat=True))
    have = set(current.values_list("product_id", flat=True))

    if have - wanted:
        ProductCollection.objects.filter(collection=collection, product_id__in=have - wanted).delete()
    ProductCollection.objects.bulk_create(
        [ProductCollection(collection=collection, product_id=pk) for pk in wanted - have], ignore_conflicts=True,
    )


def refresh_automated_collections(store, product_ids=None):
    """Re-check the store's automated collections, for `product_ids` only when given."""
    for collection in Collection.objects.filter(store=store, type=Collection.AUTOMATED).prefetch_related("rules"):
        sync_collection(collection, product_ids)


def set_manual_products(collection, product_ids):
    """A manual collection's products: exactly `product_ids` (the store's own products only)."""
    keep = set(
        Product.objects.filter(store=collection.store_id, pk__in=product_ids).values_list("pk", flat=True)
    )
    ProductCollection.objects.filter(collection=collection).exclude(product_id__in=keep).delete()
    ProductCollection.objects.bulk_create(
        [ProductCollection(collection=collection, product_id=pk) for pk in keep], ignore_conflicts=True,
    )


def published_collections(store):
    """Collections a shopper may see: Active and on the Online Store. Drafts stay hidden."""
    on_online_store = CollectionSalesChannel.objects.filter(collection=OuterRef("pk"), sales_channel__type=ONLINE)
    return (
        Collection.objects.filter(store=store, status=Collection.ACTIVE)
        .filter(Exists(on_online_store))
        .order_by("name")
    )


def published_products(store):
    """
    Products a shopper may see on the storefront: the store's Active products
    with the Online Store channel ticked. Draft and Archived stay hidden.
    """
    on_online_store = ProductSalesChannel.objects.filter(product=OuterRef("pk"), sales_channel__type=ONLINE)
    return (
        Product.objects.filter(store=store, status=ACTIVE)
        .filter(Exists(on_online_store))
        .prefetch_related("images", "variants__inventory_items")
        .order_by("-created_at")
    )


def online_channels(store):
    return [c for c in sales_channels_for(store) if c.type == ONLINE]


@transaction.atomic
def link_active_products_to_online_store(store=None):
    """
    One-time fix for products imported before imports set a channel: Active
    products with no sales channel at all are put on the Online Store.
    Products with any channel ticked are left as the owner set them.
    Returns how many products were linked.
    """
    unlisted = Product.objects.filter(status=ACTIVE, product_sales_channels__isnull=True)
    if store is not None:
        unlisted = unlisted.filter(store=store)
    links, channels = [], {}
    for product in unlisted.select_related("store"):
        if product.store_id not in channels:
            channels[product.store_id] = online_channels(product.store)
        links += [ProductSalesChannel(product=product, sales_channel=c) for c in channels[product.store_id]]
    ProductSalesChannel.objects.bulk_create(links)
    return len({link.product_id for link in links})


@transaction.atomic
def save_product(store, data, product=None):
    """
    Create or update a product from the admin form, including its
    category, collections, tags and variants, as one unit.
    """
    creating = product is None
    product = product or Product(store=store)

    for field in ("title", "description", "product_type", "vendor", "seo_title", "seo_description", "theme_template", *EXTRA_FIELDS):
        if field in data:
            value = data[field]
            # Extra fields are free text: blank means "not set"
            if field in EXTRA_FIELDS:
                value = (value or "").strip() or None
            setattr(product, field, value)

    product.status = data.get("status", product.status or DRAFT)

    # category_id: picked from the store's categories; category: a name (older clients)
    if "category_id" in data:
        product.category = data["category_id"]
    elif "category" in data:
        name = (data["category"] or "").strip()
        product.category = _named(Category, store, name) if name else None

    product.save()

    if "collections" in data:
        # Only manual collections are picked here; automated ones follow their rules
        ProductCollection.objects.filter(product=product, collection__type=Collection.MANUAL).delete()
        for name in {n.strip() for n in data["collections"] if n.strip()}:
            collection = _named(Collection, store, name)
            if collection.type == Collection.MANUAL:
                ProductCollection.objects.get_or_create(product=product, collection=collection)

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
    elif creating:
        # Like the Add product page: new products go on the Online Store unless channels are given
        ProductSalesChannel.objects.bulk_create(
            [ProductSalesChannel(product=product, sales_channel=c) for c in online_channels(store)]
        )

    # An Active product shows in the store: with no sales channel at all (e.g. imported as Draft,
    # then made Active), it goes on the Online Store. To hide a product, set it to Draft.
    if product.status == ACTIVE and not ProductSalesChannel.objects.filter(product=product).exists():
        ProductSalesChannel.objects.bulk_create(
            [ProductSalesChannel(product=product, sales_channel=c) for c in online_channels(store)]
        )

    if "variants" in data or "price" in data or not product.variants.exists():
        original_skus = set(product.variants.values_list("sku", flat=True)) if product.pk else set()
        try:
            _replace_variants(product, data)
        except ProtectedError:
            raise ProductInUse("A variant that is used by orders or inventory can't be removed.")
        skus = list(product.variants.values_list("sku", flat=True))
        if not all(skus):
            raise ValidationError({"sku": "Every product needs a SKU."})
        check_skus(store, skus, exclude_product=product, allowed_skus=original_skus)

    # Opening stock for the chosen location, on the product's first variant
    if data.get("location") is not None and data.get("quantity") is not None:
        set_available(product.variants.order_by("created_at").first(), data["location"], data["quantity"])

    return product


def delete_product(product):
    """Delete a product and everything hanging off it, or raise ProductInUse (nothing is removed then)."""
    try:
        with transaction.atomic():
            product.product_collections.all().delete()
            product.product_tags.all().delete()
            product.product_sales_channels.all().delete()
            product.images.all().delete()
            product.variants.all().delete()
            product.delete()
    except ProtectedError:
        raise ProductInUse("This product is used by orders or inventory. Set it to Draft instead.")


def _protected_product_ids(ids):
    """Ids among `ids` whose product, or one of its variants, a PROTECT foreign key still points at."""
    children = (ProductCollection, ProductTag, ProductSalesChannel, ProductImage, ProductVariant)
    blocked = set()
    for rel in Product._meta.related_objects:
        if rel.on_delete is PROTECT and rel.related_model not in children:
            blocked.update(rel.related_model.objects.filter(**{f"{rel.field.name}__in": ids})
                           .values_list(rel.field.name, flat=True))
    for rel in ProductVariant._meta.related_objects:
        if rel.on_delete is PROTECT:
            blocked.update(rel.related_model.objects.filter(**{f"{rel.field.name}__product__in": ids})
                           .values_list(f"{rel.field.name}__product_id", flat=True))
    return blocked


def delete_products(store, ids):
    """
    Delete the store's products with these ids in a handful of queries.
    Products used by orders or inventory are left alone.
    Returns (deleted count, titles of those kept).
    """
    products = {p.pk: p.title for p in Product.objects.filter(store=store, pk__in=ids)}
    blocked = _protected_product_ids(list(products))
    free = [pk for pk in products if pk not in blocked]
    if free:
        with transaction.atomic():
            for model in (ProductCollection, ProductTag, ProductSalesChannel, ProductImage, ProductVariant):
                model.objects.filter(product__in=free).delete()
            Product.objects.filter(pk__in=free).delete()
    return len(free), sorted(products[pk] for pk in blocked)


def _apply_variant(variant, row, default_price=None):
    variant.price = _price(row.get("price") or default_price)
    if "sku" in row:
        variant.sku = (row["sku"] or "").strip() or None
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
        {"price": data.get("price"), **({"sku": data["sku"]} if "sku" in data else {})}
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


def _name_map(model, store, names):
    """Existing and newly created rows for `names`, in two queries."""
    found = {o.name: o for o in model.objects.filter(store=store, name__in=names).order_by("-pk")}
    missing = [model(store=store, name=n) for n in names if n not in found]
    if model is Collection:
        # bulk_create skips Collection.save(), which gives a handle
        taken = set()
        for c in missing:
            c.handle = unique_handle(store, c.name, taken=taken)
            taken.add(c.handle)
    model.objects.bulk_create(missing)
    if model is Collection:
        _put_on_online_store(missing)
    found.update({o.name: o for o in missing})
    return found


@transaction.atomic
def import_products(store, rows):
    """
    Create many products at once from validated rows (see ProductInputSerializer).

    Rows whose SKU, or title when there is no SKU, already exist in the store
    (or earlier in `rows`) are skipped, so importing the same file twice is safe.
    Every product gets a price (0.00 when the file has none) and a stock record
    at the row's location or the store's first one (quantity 0 when the file has
    none). Active products are put on the Online Store channel, as in Shopify.
    Returns (created, skipped).
    """
    skus = {(r.get("sku") or "").strip() for r in rows} - {""}
    titles = {r["title"] for r in rows}
    seen_skus = set(
        ProductVariant.objects.filter(product__store=store, sku__in=skus).values_list("sku", flat=True)
    )
    seen_titles = set(
        Product.objects.filter(store=store, title__in=titles).values_list("title", flat=True)
    )

    fresh = []
    for row in rows:
        sku = (row.get("sku") or "").strip()
        if sku and sku in seen_skus:
            continue
        if not sku and row["title"] in seen_titles:
            continue
        if sku:
            seen_skus.add(sku)
        else:
            seen_titles.add(row["title"])
        fresh.append((row, sku))

    if not fresh:
        return 0, len(rows)

    def names(key):
        return {n.strip() for row, _ in fresh for n in row.get(key) or [] if n.strip()}

    categories = _name_map(
        Category, store, {(row.get("category") or "").strip() for row, _ in fresh} - {""}
    )
    collections = _name_map(Collection, store, names("collections"))
    tags = _name_map(Tag, store, names("tags"))

    products, variants, product_collections, product_tags = [], [], [], []
    for row, sku in fresh:
        product = Product(
            store=store,
            title=row["title"],
            status=row.get("status") or DRAFT,
            description=row.get("description", ""),
            product_type=row.get("product_type", ""),
            vendor=row.get("vendor", ""),
            seo_title=row.get("seo_title", ""),
            seo_description=row.get("seo_description", ""),
            category=categories.get((row.get("category") or "").strip()),
            **{f: (row.get(f) or "").strip() or None for f in EXTRA_FIELDS},
        )
        products.append(product)
        variants.append(
            ProductVariant(product=product, sku=sku or None, price=_price(row.get("price")))
        )
        for name in {n.strip() for n in row.get("collections") or [] if n.strip()}:
            if collections[name].type == Collection.MANUAL:
                product_collections.append(ProductCollection(product=product, collection=collections[name]))
        for name in {n.strip() for n in row.get("tags") or [] if n.strip()}:
            product_tags.append(ProductTag(product=product, tag=tags[name]))

    Product.objects.bulk_create(products)
    ProductVariant.objects.bulk_create(variants)
    ProductCollection.objects.bulk_create(product_collections)
    ProductTag.objects.bulk_create(product_tags)

    # Every imported product is on the Online Store (as in Shopify); its status decides whether
    # shoppers see it, so a Draft import shows up as soon as it is made Active
    online = online_channels(store)
    ProductSalesChannel.objects.bulk_create(
        [ProductSalesChannel(product=p, sales_channel=c) for p in products for c in online]
    )

    fallback = default_location(store)
    for variant, (row, _) in zip(variants, fresh):
        set_available(variant, row.get("location") or fallback, row.get("quantity") or 0)

    # bulk_create sends no signals: put the new products in the automated collections they match
    refresh_automated_collections(store, [p.pk for p in products])

    return len(products), len(rows) - len(products)


# ---------------------------------------------------------------------------
# Automated collections follow their rules: when a product, its variants
# (price), tags or stock change, the products touched are re-checked once the
# change is committed. Changes inside one transaction (a product form save) are
# checked together, once. Connected in CatalogConfig.ready().
# bulk_create sends no signals: import_products refreshes on its own.
# ---------------------------------------------------------------------------

_pending_refresh = threading.local()


def _flush_refresh():
    pending, _pending_refresh.ids = getattr(_pending_refresh, "ids", set()), set()
    by_store = {}
    for store_id, pk in Product.objects.filter(pk__in=pending).values_list("store_id", "pk"):
        by_store.setdefault(store_id, []).append(pk)
    for store_id, ids in by_store.items():
        refresh_automated_collections(store_id, ids)


def queue_collection_refresh(product_id):
    if product_id is None:
        return
    # One check per transaction (scheduled again when a rollback dropped the earlier one)
    if not any(item[1] is _flush_refresh for item in connection.run_on_commit):
        _pending_refresh.ids = {product_id}
        transaction.on_commit(_flush_refresh)
    else:
        _pending_refresh.ids.add(product_id)


def _product_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        queue_collection_refresh(instance.pk)


def _product_part_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        queue_collection_refresh(instance.product_id)


def _stock_changed(sender, instance, raw=False, **kwargs):
    if not raw:
        queue_collection_refresh(
            ProductVariant.objects.filter(pk=instance.variant_id).values_list("product_id", flat=True).first()
        )


def connect_collection_signals():
    from django.db.models.signals import post_delete, post_save

    from apps.inventory.models import InventoryItem

    post_save.connect(_product_saved, sender=Product, dispatch_uid="collections-product")
    for model in (ProductVariant, ProductTag):
        post_save.connect(_product_part_changed, sender=model, dispatch_uid=f"collections-{model.__name__}-save")
        post_delete.connect(_product_part_changed, sender=model, dispatch_uid=f"collections-{model.__name__}-delete")
    post_save.connect(_stock_changed, sender=InventoryItem, dispatch_uid="collections-stock")
