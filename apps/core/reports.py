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

GET /api/v1/reports/analytics/  the Analytics dashboard, with the visit funnel (?from=YYYY-MM-DD&to=YYYY-MM-DD,
                                ?compare=previous_period|previous_year|none, ?currency=INR)

aggregate_daily_metrics() fills that table (Celery task in apps.core.tasks).

Analytics formulas. Cancelled orders are left out, dates and buckets are the store's local
time (Store.timezone), and every figure is for one currency (the store's unless ?currency=
says otherwise). Orders count on the day they were placed and refunds on the day the refund
was made (unlike the daily metrics above, which put a refund on its order's day).

    gross sales      sum(Order.subtotal)           item prices x quantity, before discounts
    discounts        sum(Order.discount_amount)
    returns          successful refund transactions made in the period (cancelled orders excluded)
    net sales        gross sales - discounts - returns
    shipping         sum(Order.shipping_amount)
    taxes            sum(Order.tax_amount)
    total sales      net sales + shipping + taxes
    orders           count of orders; orders fulfilled = fulfillment_status "fulfilled"
    average order    (gross sales - discounts + shipping + taxes) / orders   (before returns)
    returning rate   returning customers / customers who ordered in the period. A customer is
                     returning when they also had an order before the period started; a customer
                     whose first order ever is inside the period is new, even with two orders.
                     Customers are matched by email (account or guest), else by id.
    % change         (current - comparison) / comparison x 100; null when comparison is 0

Visit figures come from analytics.storefront_sessions (apps.orders.services). A session counts on the
local day it started, in every currency.

    sessions         count of sessions (visits); a visit ends after 30 minutes idle or at local midnight
    visitors         distinct visitor ids with a session in the period
    conversion rate  sessions with an order / sessions x 100
    funnel           sessions -> added to cart -> reached checkout -> completed (has an order); a later
                     step implies the earlier ones, so each step is at most the one before it
"""
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from django.db.models import CharField, Count, Min, Q, Sum
from django.db.models.functions import Cast, Coalesce, Lower, TruncDate, TruncHour
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from apps.customers.models import Customer
from apps.orders.models import Order, OrderItem, StoreDailyMetric, StorefrontSession
from apps.payments.models import PaymentTransaction

from .permissions import HasStorePermission, IsStoreMember
from .viewsets import has_field

def live(qs):
    """Hide soft-deleted rows for the models that have them."""
    return qs.filter(is_deleted=False) if has_field(qs.model, "is_deleted") else qs


class ReportsBase(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "reports"


def parse_days(request):
    try:
        return min(max(int(request.query_params.get("days", 30)), 1), 365)
    except ValueError:
        return 30


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
            recent.annotate(day=TruncDate("created_at", tzinfo=store_tz(store)))
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


def social_click_counts(store, start, end):
    """{network: clicks} for the store's local dates start..end, added up over every currency's row."""
    counts = {}
    for clicks in (StoreDailyMetric.objects.filter(store=store, metric_date__range=(start, end))
                   .exclude(social_clicks={}).values_list("social_clicks", flat=True)):
        for network, n in clicks.items():
            counts[network] = counts.get(network, 0) + n
    return counts


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


# --- Analytics dashboard (formulas in the module docstring) ----------------------------

MAX_ANALYTICS_DAYS = 400
CENT = Decimal("0.01")
COMPARE_MODES = ("previous_period", "previous_year", "none")


def _parse_day(request, name):
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValidationError({name: "Use the format YYYY-MM-DD."})


def _year_before(day):
    try:
        return day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year - 1, day=28)


def comparison_range(start, end, mode):
    if mode == "previous_period":
        days = (end - start).days + 1
        return start - timedelta(days=days), start - timedelta(days=1)
    if mode == "previous_year":
        return _year_before(start), _year_before(end)
    return None


def _money(value):
    return (value or ZERO).quantize(CENT)


def percent_change(current, previous):
    if not previous:
        return None
    return round(float((Decimal(current) - Decimal(previous)) / Decimal(previous) * 100), 1)


def _who():
    """Who placed the order: account or guest email, else the customer id."""
    return Coalesce(
        Lower("customer__email"), Lower("customer_email"), Cast("customer_id", CharField()),
        output_field=CharField())


def _bucket_starts(start, end, tz, hourly):
    if hourly:
        first = datetime.combine(start, time.min, tz).astimezone(dt_timezone.utc)
        return [(first + timedelta(hours=h)).astimezone(tz).replace(tzinfo=None) for h in range(24)]
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _bucket_key(value, hourly):
    return value.replace(tzinfo=None) if hourly else value


def _blank():
    return {"gross": ZERO, "discounts": ZERO, "shipping": ZERO, "taxes": ZERO, "refunds": ZERO,
            "orders": 0, "fulfilled": 0, "customers": 0, "returning": 0}


def _derive(m):
    net = m["gross"] - m["discounts"] - m["refunds"]
    before_returns = m["gross"] - m["discounts"] + m["shipping"] + m["taxes"]
    return {
        "gross_sales": _money(m["gross"]), "discounts": _money(m["discounts"]), "returns": _money(m["refunds"]),
        "net_sales": _money(net), "shipping_charges": _money(m["shipping"]), "taxes": _money(m["taxes"]),
        "total_sales": _money(net + m["shipping"] + m["taxes"]),
        "orders": m["orders"], "orders_fulfilled": m["fulfilled"],
        "average_order_value": _money(before_returns / m["orders"]) if m["orders"] else ZERO,
        "returning_customer_rate": round(m["returning"] / m["customers"] * 100, 1) if m["customers"] else 0.0,
    }


def first_orders(store, people):
    """{customer: time of their first order ever} for the given customers (see _who)."""
    first = {}
    for i in range(0, len(people), 500):
        for r in (Order.objects.filter(store=store).exclude(status="cancelled")
                  .annotate(who=_who()).filter(who__in=people[i:i + 500])
                  .values("who").annotate(first=Min("created_at"))):
            first[r["who"]] = r["first"]
    return first


def period_analytics(store, start, end, currency, narrow=None):
    """(figures, per-bucket series, orders queryset) for the store's local dates start..end."""
    tz = store_tz(store)
    hourly = start == end
    orders = Order.objects.filter(
        store=store, currency_code=currency,
        created_at__gte=datetime.combine(start, time.min, tz),
        created_at__lt=datetime.combine(end + timedelta(days=1), time.min, tz),
    ).exclude(status="cancelled")
    if narrow:
        orders = narrow(orders)
    trunc = TruncHour if hourly else TruncDate
    per_bucket = {}

    for r in (orders.annotate(b=trunc("created_at", tzinfo=tz)).values("b").annotate(
            gross=Sum("subtotal"), discounts=Sum("discount_amount"), shipping=Sum("shipping_amount"),
            taxes=Sum("tax_amount"), n=Count("id"), fulfilled=Count("id", filter=Q(fulfillment_status="fulfilled")))):
        per_bucket[_bucket_key(r["b"], hourly)] = {
            **_blank(), "gross": r["gross"] or ZERO, "discounts": r["discounts"] or ZERO,
            "shipping": r["shipping"] or ZERO, "taxes": r["taxes"] or ZERO, "orders": r["n"],
            "fulfilled": r["fulfilled"]}

    period_start = datetime.combine(start, time.min, tz)
    period_end = datetime.combine(end + timedelta(days=1), time.min, tz)
    refunds = PaymentTransaction.objects.filter(
        transaction_type="refund", status="success", created_at__gte=period_start, created_at__lt=period_end,
        payment__store=store, payment__order__currency_code=currency, payment__is_deleted=False,
    ).exclude(payment__order__status="cancelled")
    if narrow:
        refunds = refunds.filter(payment__order__in=narrow(Order.objects.filter(store=store)).values("pk"))
    for r in (refunds
              .annotate(b=trunc("created_at", tzinfo=tz)).values("b").annotate(s=Sum("amount"))):
        per_bucket.setdefault(_bucket_key(r["b"], hourly), _blank())["refunds"] = r["s"] or ZERO

    # Returning customers: they also ordered before the period started.
    pairs = list(orders.annotate(b=trunc("created_at", tzinfo=tz), who=_who()).filter(who__isnull=False)
                 .values("b", "who").distinct())
    people = sorted({r["who"] for r in pairs})
    first_ever = first_orders(store, people)
    in_period = set()
    for r in pairs:
        m = per_bucket.setdefault(_bucket_key(r["b"], hourly), _blank())
        m["customers"] += 1
        m["returning"] += first_ever[r["who"]] < period_start
        in_period.add(r["who"])

    totals = _blank()
    for m in per_bucket.values():
        for k in ("gross", "discounts", "shipping", "taxes", "refunds", "orders", "fulfilled"):
            totals[k] += m[k]
    totals["customers"] = len(in_period)
    totals["returning"] = sum(first_ever[who] < period_start for who in in_period)

    series = [{"bucket": key.isoformat(), **_derive(per_bucket.get(key, _blank()))}
              for key in _bucket_starts(start, end, tz, hourly)]
    return _derive(totals), series, orders


SESSION_KEYS = ("sessions", "visitors", "page_views", "sessions_with_cart", "sessions_reached_checkout",
                "sessions_completed")
SESSION_COUNTS = dict(
    sessions=Count("id"), visitors=Count("visitor_id", distinct=True), page_views=Sum("page_views"),
    sessions_with_cart=Count("id", filter=Q(added_to_cart=True)),
    sessions_reached_checkout=Count("id", filter=Q(reached_checkout=True)),
    sessions_completed=Count("id", filter=Q(order__isnull=False)))


def rate(part, whole):
    """part as a percentage of whole, one decimal; 0.0 when there is no whole."""
    return round(part / whole * 100, 1) if whole else 0.0


def _session_figures(counts):
    figures = {key: counts.get(key) or 0 for key in SESSION_KEYS}
    figures["conversion_rate"] = rate(figures["sessions_completed"], figures["sessions"])
    return figures


def period_sessions(store, start, end):
    """The store's sessions that started on its local dates start..end."""
    tz = store_tz(store)
    return StorefrontSession.objects.filter(
        store=store, started_at__gte=datetime.combine(start, time.min, tz),
        started_at__lt=datetime.combine(end + timedelta(days=1), time.min, tz))


def session_analytics(store, start, end):
    """(figures for the whole period, {bucket: figures}) for the store's visits, buckets as in period_analytics."""
    tz = store_tz(store)
    hourly = start == end
    sessions = period_sessions(store, start, end)
    trunc = TruncHour if hourly else TruncDate
    per_bucket = {
        _bucket_key(r["b"], hourly): _session_figures(r)
        for r in sessions.annotate(b=trunc("started_at", tzinfo=tz)).values("b").annotate(**SESSION_COUNTS)}
    blank = _session_figures({})
    return (_session_figures(sessions.aggregate(**SESSION_COUNTS)),
            {key: per_bucket.get(key, blank) for key in _bucket_starts(start, end, tz, hourly)})


def funnel(figures, previous=None):
    steps = (("sessions", "Sessions"), ("sessions_with_cart", "Added to cart"),
             ("sessions_reached_checkout", "Reached checkout"), ("sessions_completed", "Completed"))
    return [{"key": key, "label": label, "value": figures[key], "rate": rate(figures[key], figures["sessions"]),
             "previous": previous[key] if previous else None,
             "change": percent_change(figures[key], previous[key]) if previous else None}
            for key, label in steps]


def sessions_by_landing_page(sessions, limit=10):
    return [{"page": r["landing_path"] or "/", "page_type": r["landing_page_type"], "sessions": r["n"]}
            for r in sessions.values("landing_path", "landing_page_type").annotate(n=Count("id"))
            .order_by("-n", "landing_path")[:limit]]


def sessions_by_referrer(sessions, limit=10):
    from apps.orders.services import referrer_name  # imported here: the orders services import this module

    merged = {}
    for r in sessions.values("referrer_host", "referrer_type", "utm_source").annotate(n=Count("id")):
        key = (referrer_name(r["referrer_host"], r["utm_source"]), r["referrer_type"])
        merged[key] = merged.get(key, 0) + r["n"]
    return [{"referrer": name.title() if name == "direct" else name, "type": kind, "sessions": n}
            for (name, kind), n in sorted(merged.items(), key=lambda i: (-i[1], i[0]))[:limit]]


def top_products(orders, limit=10):
    return [{"product_id": r["product"], "name": r["product_name"], "units": r["units"], "sales": _money(r["sales"])}
            for r in OrderItem.objects.filter(order__in=orders, is_deleted=False)
            .values("product", "product_name").annotate(units=Sum("quantity"), sales=Sum("total"))
            .order_by("-sales", "product_name")[:limit]]


def sales_by_channel(orders):
    rows = (orders.values("sales_channel__name")
            .annotate(sales=Sum("subtotal") - Sum("discount_amount") + Sum("shipping_amount") + Sum("tax_amount"),
                      n=Count("id")).order_by("-sales"))
    return [{"channel": r["sales_channel__name"] or "No channel", "orders": r["n"], "sales": _money(r["sales"])}
            for r in rows]


class AnalyticsView(ReportsBase):
    def get(self, request):
        store = request.store
        end = _parse_day(request, "to") or local_today(store)
        start = _parse_day(request, "from") or end - timedelta(days=29)
        if start > end:
            raise ValidationError({"from": "The start date can't be after the end date."})
        if (end - start).days + 1 > MAX_ANALYTICS_DAYS:
            raise ValidationError({"from": f"Pick a range of at most {MAX_ANALYTICS_DAYS} days."})
        mode = request.query_params.get("compare", "previous_period")
        if mode not in COMPARE_MODES:
            raise ValidationError({"compare": "Use previous_period, previous_year or none."})
        currency = (request.query_params.get("currency") or store.currency or "INR").upper()

        current, series, orders = period_analytics(store, start, end, currency)
        visits, visit_buckets = session_analytics(store, start, end)
        current.update(visits)
        for row, figures in zip(series, visit_buckets.values()):
            row.update(figures)
        other = comparison_range(start, end, mode)
        previous, previous_series, previous_visits = (None, None, None)
        if other:
            previous, previous_series, _ = period_analytics(store, other[0], other[1], currency)
            previous_visits, previous_buckets = session_analytics(store, other[0], other[1])
            previous.update(previous_visits)
            for row, figures in zip(previous_series, previous_buckets.values()):
                row.update(figures)
        sessions = period_sessions(store, start, end)

        currencies = sorted(set(Order.objects.filter(store=store).values_list("currency_code", flat=True)))
        return Response({
            "from": start, "to": end, "compare": mode,
            "compare_from": other[0] if other else None, "compare_to": other[1] if other else None,
            "timezone": str(store_tz(store)), "currency_code": currency,
            "currencies": currencies or [currency],
            "interval": "hour" if start == end else "day",
            "has_data": current["orders"] > 0,
            "has_sessions": visits["sessions"] > 0,
            "metrics": {key: {"value": value,
                              "previous": previous[key] if previous else None,
                              "change": percent_change(value, previous[key]) if previous else None}
                        for key, value in current.items()},
            "series": series,
            "comparison_series": previous_series,
            "top_products": top_products(orders),
            "sales_by_channel": sales_by_channel(orders),
            "funnel": funnel(visits, previous_visits),
            "sessions_by_landing_page": sessions_by_landing_page(sessions),
            "sessions_by_referrer": sessions_by_referrer(sessions),
            "refreshed_at": timezone.now(),
        })


# --- Live view (GET /api/v1/reports/live/) -----------------------------------------------
#
# Today is since local midnight in the store's timezone, with the formulas above (total sales,
# orders without cancelled ones, returning customers). Visitors "right now" are distinct visitor
# ids with a session seen in the last 5 minutes; customer behaviour counts sessions seen in the
# last 10 minutes. Locations are countries only (the sessions carry no city).

LIVE_VISITOR_MINUTES = 5
LIVE_BEHAVIOUR_MINUTES = 10


def live_customers(store, orders, midnight):
    """(first-time, returning) customers who ordered today; returning ordered before midnight."""
    people = sorted({r["who"] for r in orders.annotate(who=_who()).filter(who__isnull=False).values("who").distinct()})
    first = first_orders(store, people)
    returning = sum(first[who] < midnight for who in people)
    return {"first_time": len(people) - returning, "returning": returning}


class LiveView(ReportsBase):
    def get(self, request):
        store = request.store
        now = timezone.now()
        tz = store_tz(store)
        today = now.astimezone(tz).date()
        midnight = datetime.combine(today, time.min, tz)
        currency = (store.currency or "INR").upper()

        totals, _, orders = period_analytics(store, today, today, currency)
        sessions = StorefrontSession.objects.filter(store=store)
        today_sessions = period_sessions(store, today, today)
        recent = sessions.filter(last_seen_at__gte=now - timedelta(minutes=LIVE_BEHAVIOUR_MINUTES))

        return Response({
            "timezone": str(tz), "currency_code": currency, "since": midnight,
            "visitors_now": sessions.filter(last_seen_at__gte=now - timedelta(minutes=LIVE_VISITOR_MINUTES))
                            .values("visitor_id").distinct().count(),
            "total_sales": totals["total_sales"],
            "total_sessions": today_sessions.count(),
            "total_orders": totals["orders"],
            "top_locations": [{"country": r["country"] or "Unknown", "sessions": r["n"]}
                              for r in today_sessions.values("country").annotate(n=Count("id")).order_by("-n", "country")[:10]],
            "top_products": top_products(orders, 5),
            "customers": live_customers(store, orders, midnight),
            "behaviour": recent.aggregate(
                active_carts=Count("id", filter=Q(added_to_cart=True)),
                checking_out=Count("id", filter=Q(reached_checkout=True)),
                purchased=Count("id", filter=Q(order__isnull=False))),
            "refreshed_at": now,
        })
