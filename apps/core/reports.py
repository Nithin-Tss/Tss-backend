"""
Reports and analytics for the store admin (permission area "reports").

GET /api/v1/reports/            the built-in reports the Reports page lists
GET /api/v1/reports/overview/   totals, a daily sales series and recent activity
                                (?days=30, between 1 and 365)
GET /api/v1/reports/<id>/       one built-in report (?days=30), where <id> is
                                an id from the list above
GET /api/v1/reports/daily-metrics/
                                pre-calculated per-day metrics from
                                analytics.store_daily_metrics (?days=30, ?currency=INR)

aggregate_daily_metrics() fills that table (Celery task in apps.core.tasks).
"""
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from apps.customers.models import Customer
from apps.orders.models import Order, OrderItem, StoreDailyMetric
from apps.payments.models import PaymentTransaction

from .permissions import HasStorePermission, IsStoreMember
from .viewsets import has_field

BUILT_IN_REPORTS = [
    ("sales-over-time", "Sales over time", "Sales", "Total sales and orders per day."),
    ("orders-over-time", "Orders over time", "Sales", "How many orders came in each day."),
    ("average-order-value", "Average order value", "Sales", "Total sales divided by orders."),
    ("new-customers", "New customers over time", "Customers", "Customers added each day."),
    ("top-products", "Top products by units sold", "Sales", "Best sellers by quantity."),
]


def live(qs):
    """Hide soft-deleted rows for the models that have them."""
    return qs.filter(is_deleted=False) if has_field(qs.model, "is_deleted") else qs


class ReportsBase(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "reports"


class ReportListView(ReportsBase):
    def get(self, request):
        return Response([
            {"id": key, "name": name, "category": category, "description": description,
             "created_by": "System", "last_viewed": None}
            for key, name, category, description in BUILT_IN_REPORTS
        ])


def parse_days(request):
    try:
        return min(max(int(request.query_params.get("days", 30)), 1), 365)
    except ValueError:
        return 30


class ReportDetailView(ReportsBase):
    def get(self, request, report_id):
        meta = next((r for r in BUILT_IN_REPORTS if r[0] == report_id), None)
        if meta is None:
            raise NotFound("Unknown report.")

        days = parse_days(request)
        store = request.store
        since = timezone.now() - timedelta(days=days)
        orders = live(Order.objects.filter(store=store)).exclude(status="cancelled").filter(created_at__gte=since)
        per_day = orders.annotate(day=TruncDate("created_at")).values("day").order_by("day")
        currency = orders.values_list("currency_code", flat=True).first()
        zero = Decimal("0.00")

        if report_id == "sales-over-time":
            rows = [{"date": r["day"], "sales": r["sales"], "orders": r["orders"]}
                    for r in per_day.annotate(sales=Sum("total"), orders=Count("id"))]
        elif report_id == "orders-over-time":
            rows = [{"date": r["day"], "orders": r["orders"]} for r in per_day.annotate(orders=Count("id"))]
        elif report_id == "average-order-value":
            rows = [{"date": r["day"], "average_order_value": (r["sales"] / r["orders"]).quantize(Decimal("0.01"))}
                    for r in per_day.annotate(sales=Sum("total"), orders=Count("id"))]
        elif report_id == "new-customers":
            customers = live(Customer.objects.filter(store=store, created_at__gte=since))
            rows = [{"date": r["day"], "customers": r["customers"]}
                    for r in customers.annotate(day=TruncDate("created_at")).values("day")
                    .annotate(customers=Count("id")).order_by("day")]
        else:  # top-products
            items = OrderItem.objects.filter(order__in=orders, is_deleted=False)
            rows = [{"product_id": r["product"], "name": r["product_name"],
                     "units": r["units"], "sales": r["sales"]}
                    for r in items.values("product", "product_name")
                    .annotate(units=Sum("quantity"), sales=Sum("total")).order_by("-units")[:20]]

        return Response({
            "id": report_id, "name": meta[1], "category": meta[2], "description": meta[3],
            "days": days, "currency_code": currency or None,
            "total": {
                "sales-over-time": lambda: sum((r["sales"] for r in rows), zero),
                "orders-over-time": lambda: sum(r["orders"] for r in rows),
                "average-order-value": lambda: (
                    (orders.aggregate(s=Sum("total"))["s"] / orders.count()).quantize(Decimal("0.01"))
                    if rows else zero),
                "new-customers": lambda: sum(r["customers"] for r in rows),
                "top-products": lambda: sum(r["units"] for r in rows),
            }[report_id](),
            "rows": rows,
        })


class OverviewView(ReportsBase):
    def get(self, request):
        days = parse_days(request)

        store = request.store
        since = timezone.now() - timedelta(days=days)
        orders = live(Order.objects.filter(store=store)).exclude(status="cancelled")
        recent = orders.filter(created_at__gte=since)

        totals = recent.aggregate(sales=Sum("total"), count=Count("id"))
        sales = totals["sales"] or Decimal("0.00")
        count = totals["count"] or 0

        series = (
            recent.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(sales=Sum("total"), orders=Count("id"))
            .order_by("day")
        )

        activity = [
            {"type": "order", "title": f"Order {o.order_number}", "amount": o.total,
             "currency_code": o.currency_code, "at": o.created_at}
            for o in live(Order.objects.filter(store=store)).order_by("-created_at")[:8]
        ]
        activity += [
            {"type": "customer", "title": f"New customer {c.first_name} {c.last_name}".strip(),
             "amount": None, "currency_code": None, "at": c.created_at}
            for c in live(Customer.objects.filter(store=store)).order_by("-created_at")[:5]
        ]
        activity.sort(key=lambda a: a["at"], reverse=True)

        currency = orders.values_list("currency_code", flat=True).first()

        return Response({
            "days": days,
            "currency_code": currency,
            "totals": {
                "sales": sales,
                "orders": count,
                "average_order_value": (sales / count).quantize(Decimal("0.01")) if count else Decimal("0.00"),
                "customers": live(Customer.objects.filter(store=store)).count(),
                "products": live(Product.objects.filter(store=store)).count(),
            },
            "series": [{"date": row["day"], "sales": row["sales"], "orders": row["orders"]} for row in series],
            "activity": activity[:10],
        })


# --- Daily metrics (analytics.store_daily_metrics) -------------------------------------
#
# Same definitions as the live reports above: an order counts unless it is cancelled, gross
# sales = sum of Order.total, average order value = gross sales / orders. Days are the
# store's local dates and every currency gets its own row. Refunds (successful refund
# transactions) are counted on the day the refunded order was placed, so net sales is
# gross - refunds for that day's orders. Orders, payments and refunds are only read.

ZERO = Decimal("0.00")
METRIC_FIELDS = ("total_orders", "gross_sales", "total_refunds", "net_sales", "items_sold", "average_order_value")


def store_tz(store):
    try:
        return ZoneInfo(store.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_today(store):
    return datetime.now(store_tz(store)).date()


def compute_daily_metrics(store, start, end):
    """{(day, currency): metrics} for the store's orders placed start..end (local dates, inclusive)."""
    tz = store_tz(store)
    orders = Order.objects.filter(
        store=store,
        created_at__gte=datetime.combine(start, time.min, tz),
        created_at__lt=datetime.combine(end + timedelta(days=1), time.min, tz),
    ).exclude(status="cancelled")
    out = {}

    def row(day, currency):
        return out.setdefault((day, currency), {"total_orders": 0, "gross_sales": ZERO, "total_refunds": ZERO, "items_sold": 0})

    for r in (orders.annotate(day=TruncDate("created_at", tzinfo=tz)).values("day", "currency_code")
              .annotate(n=Count("id"), s=Sum("total"))):
        m = row(r["day"], r["currency_code"])
        m["total_orders"], m["gross_sales"] = r["n"], r["s"] or ZERO

    for r in (OrderItem.objects.filter(order__in=orders, is_deleted=False)
              .annotate(day=TruncDate("order__created_at", tzinfo=tz)).values("day", "order__currency_code")
              .annotate(q=Sum("quantity"))):
        row(r["day"], r["order__currency_code"])["items_sold"] = r["q"] or 0

    for r in (PaymentTransaction.objects.filter(transaction_type="refund", status="success",
                                                payment__order__in=orders, payment__is_deleted=False)
              .annotate(day=TruncDate("payment__order__created_at", tzinfo=tz))
              .values("day", "payment__order__currency_code").annotate(s=Sum("amount"))):
        row(r["day"], r["payment__order__currency_code"])["total_refunds"] = r["s"] or ZERO

    for m in out.values():
        m["net_sales"] = m["gross_sales"] - m["total_refunds"]
        m["average_order_value"] = (
            (m["gross_sales"] / m["total_orders"]).quantize(Decimal("0.01")) if m["total_orders"] else ZERO)
    return out


def aggregate_daily_metrics(store, start, end):
    """
    Rebuild the store's metric rows for start..end; returns how many rows were written.
    Idempotent: each (store, day, currency) row is upserted, so reruns and retries never
    double count. A day whose orders have all been cancelled since is zeroed, not deleted.
    """
    if start > end:
        raise ValueError("start must not be after end")
    fresh = compute_daily_metrics(store, start, end)
    with transaction.atomic():
        for metric in StoreDailyMetric.objects.filter(store=store, metric_date__range=(start, end)):
            fresh.setdefault((metric.metric_date, metric.currency_code),
                             {field: 0 if field in ("total_orders", "items_sold") else ZERO for field in METRIC_FIELDS})
        for (day, currency), values in fresh.items():
            StoreDailyMetric.objects.update_or_create(
                store=store, metric_date=day, currency_code=currency, defaults=values)
    return len(fresh)


class DailyMetricsView(ReportsBase):
    def get(self, request):
        days = parse_days(request)
        store = request.store
        rows = StoreDailyMetric.objects.filter(
            store=store, metric_date__gt=local_today(store) - timedelta(days=days))
        currency = request.query_params.get("currency")
        if currency:
            rows = rows.filter(currency_code=currency.upper())

        totals = {}
        for r in rows:
            t = totals.setdefault(r.currency_code, {field: 0 for field in METRIC_FIELDS})
            for field in METRIC_FIELDS[:-1]:
                t[field] += getattr(r, field)
        for t in totals.values():
            t["average_order_value"] = (
                (Decimal(t["gross_sales"]) / t["total_orders"]).quantize(Decimal("0.01")) if t["total_orders"] else ZERO)

        return Response({
            "days": days,
            "timezone": str(store_tz(store)),
            "totals": totals,  # per currency; currencies are never added together
            "rows": [{"date": r.metric_date, "currency_code": r.currency_code,
                      **{field: getattr(r, field) for field in METRIC_FIELDS}} for r in rows],
        })
