"""
Every database read the assistant makes lives here, and nowhere else.

Rules (tests/test_queries_static.py checks them):
  - only the read-only alias: every queryset starts with .using(ALIAS)
  - always filtered by store_id (the store comes from the request, never from the model)
  - never save / update / delete / create
  - no subqueries across aliases: ids are fetched first and passed back as plain Python lists
"""
from django.conf import settings
from django.db import connections
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce, Length

from apps.catalog.models import ProductVariant
from apps.orders.models import Order, OrderItem
from apps.payments.models import PaymentTransaction
from apps.themes.models import StoreTheme, ThemeFile

from .apps import RO_ALIAS

ALIAS = RO_ALIAS


class ReadOnlyNotEnforced(Exception):
    """The database session is not read-only, so nothing is read."""


def alias_configured():
    return ALIAS in settings.DATABASES


def ensure_read_only():
    """Ask the database itself whether this session is read-only. Refuse to go on when it is not."""
    with connections[ALIAS].cursor() as cursor:
        cursor.execute("SHOW transaction_read_only")
        value = cursor.fetchone()[0]
    if str(value).lower() != "on":
        raise ReadOnlyNotEnforced("The assistant's database session is not read-only.")


def _orders(store_id):
    return Order.objects.using(ALIAS).filter(store_id=store_id)


def _counted(store_id, start, end):
    """Orders placed in [start, end) that were not cancelled (what Reports calls sales)."""
    return _orders(store_id).exclude(status="cancelled").filter(created_at__gte=start, created_at__lt=end)


def sales_totals(store_id, start, end):
    """Gross, refunds on those same orders (counted on the ORDER's date), order count and currencies."""
    orders = _counted(store_id, start, end)
    agg = orders.aggregate(gross=Sum("total"), count=Count("id"))
    refunds = (
        PaymentTransaction.objects.using(ALIAS)
        .filter(
            transaction_type="refund",
            status="success",
            payment__is_deleted=False,
            payment__store_id=store_id,
            payment__order__store_id=store_id,
            payment__order__created_at__gte=start,
            payment__order__created_at__lt=end,
        )
        .exclude(payment__order__status="cancelled")
        .aggregate(total=Sum("amount"))["total"]
    )
    currencies = sorted(set(orders.values_list("currency_code", flat=True).distinct()))
    return {
        "gross": agg["gross"],
        "orders": agg["count"] or 0,
        "refunds": refunds,
        "currencies": currencies,
    }


def low_stock_rows(store_id, threshold, limit):
    """
    Active products' variants with stock (summed over locations) at or under `threshold`, out of stock
    (<= 0) first, then the lowest. A variant with no inventory record at all is not listed.
    Returns (number of matching variants, the first limit rows). A row says if its product has several variants.
    """
    variants = (
        ProductVariant.objects.using(ALIAS)
        .filter(product__store_id=store_id, product__status="active")
        .annotate(stock=Sum("inventory_items__available"))
        .filter(stock__isnull=False, stock__lte=threshold)
    )
    total = variants.count()
    rows = variants.order_by("stock", "product__title", "sku").values_list("product_id", "product__title", "sku", "stock")[:limit]
    multi = set(
        ProductVariant.objects.using(ALIAS)
        .filter(product__store_id=store_id, product__status="active")
        .values("product_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("product_id", flat=True)
    )
    return total, [{"title": t, "sku": s, "stock": int(q), "multi": p in multi} for p, t, s, q in rows]


def orders_page(store_id, status, fulfillment_status, limit):
    """The newest orders (optionally filtered) as plain dicts, plus how many orders match."""
    orders = _orders(store_id)
    if status:
        orders = orders.filter(status=status)
    if fulfillment_status:
        orders = orders.filter(fulfillment_status=fulfillment_status)
    total = orders.count()
    page = list(
        orders.select_related("customer")
        .order_by("-created_at", "-id")[:limit]
    )
    ids = [o.id for o in page]  # a plain list: never a queryset from another alias

    items = dict(
        OrderItem.objects.using(ALIAS)
        .filter(order_id__in=ids, is_deleted=False)
        .values_list("order_id")
        .annotate(n=Coalesce(Sum("quantity"), 0))
    )
    refunded = dict(
        PaymentTransaction.objects.using(ALIAS)
        .filter(
            transaction_type="refund",
            status="success",
            payment__is_deleted=False,
            payment__store_id=store_id,
            payment__order_id__in=ids,
        )
        .values_list("payment__order_id")
        .annotate(total=Sum("amount"))
    )
    result = []
    for o in page:
        c = o.customer
        result.append({
            "order_number": o.order_number,
            "created_at": o.created_at,
            "status": o.status,
            "fulfillment_status": o.fulfillment_status,
            "total": o.total,
            "refunded": refunded.get(o.id),
            "items": int(items.get(o.id, 0)),
            "first_name": c.first_name if c else None,
            "email": (c.email if c else None) or o.customer_email,
        })
    return total, result


# ---------------------------------------------------------------- theme code (read-only)
#
# These read ThemeFile rows directly. They never call apps.themes.services.get_store_theme(): that runs
# _prepare(), which adds and upgrades files, i.e. writes. A theme is read only when it is one of this
# store's own copies (theme_data.store_id), so a shared theme is never returned.

def _own_themes(store_id):
    return StoreTheme.objects.using(ALIAS).filter(
        store_id=store_id, theme__theme_data__store_id=str(store_id)
    ).select_related("theme")


def theme_for(store_id, theme_id):
    """
    The theme to read: `theme_id` (the one the admin is editing) when it is a private theme of this store,
    otherwise the published one. None when the store has no theme of its own yet.
    """
    themes = _own_themes(store_id)
    row = themes.filter(theme_id=theme_id).first() if theme_id else None
    if row is None:
        row = themes.filter(is_active=True).first()
    if row is None:
        return None
    return {"theme_id": str(row.theme_id), "name": row.theme.name, "published": bool(row.is_active)}


def theme_file_list(store_id, theme_id):
    """[(file_path, size in characters)] of the theme's files, without their text."""
    rows = (
        ThemeFile.objects.using(ALIAS)
        .filter(theme_id=theme_id, theme__theme_data__store_id=str(store_id))
        .annotate(size=Length("content"))
        .order_by("file_path")
        .values_list("file_path", "size")
    )
    return [(path, int(size or 0)) for path, size in rows]


def theme_file_text(store_id, theme_id, path):
    """One theme file's text, or None."""
    return (
        ThemeFile.objects.using(ALIAS)
        .filter(theme_id=theme_id, theme__theme_data__store_id=str(store_id), file_path=path)
        .values_list("content", flat=True)
        .first()
    )
