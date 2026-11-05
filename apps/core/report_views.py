"""
The built-in reports of the admin Reports page (permission area "reports").

GET /api/v1/reports/        the reports the page lists, grouped by category
GET /api/v1/reports/<id>/   one report
        ?from=YYYY-MM-DD&to=YYYY-MM-DD    store-local dates, default the last 30 days
        ?compare=previous_period|previous_year|none
        ?sort=<column>  (a leading - sorts descending)  ?page=1  (50 rows a page)
        ?product=<id>&channel=<id>&customer_type=new|returning   (where the report supports them)
        ?format=csv     every row for these dates and filters, streamed as a download

The formulas, the store timezone and the cancelled-order rule are those of /reports/analytics/
(see apps.core.reports). Notes on how each report reads the data:

* Order-level reports (by customer, discount, channel, location...) show sales before returns:
  gross sales - discounts + shipping + taxes. Refunds are recorded per payment, not per
  customer or product, so only the "over time" and "Payments by method" reports show returns.
* Product and variant reports read the order items: net sales = item total after line discounts.
  A product filter keeps the orders that contain the product.
* Orders by status is the one report that keeps cancelled orders; it is what shows them.
* Customer type: "returning" had an order before the period started, "new" had not (a customer
  who ordered twice inside the period is new).
* Session reports (Acquisition, Conversion) read analytics.storefront_sessions: a session counts on the day
  it started and is not tied to a currency or to the product / channel / customer filters. Conversion
  rate = sessions with an order / sessions. See apps.orders.services.
* Social link clicks reads store_daily_metrics.social_clicks: clicks per network on the store-local day, added up
  over every currency's row (a click has no currency).
* Inventory reports are a snapshot of stock now. Sell-through = units sold / (units sold +
  units available); days remaining = units available / average units sold per day.
  They use the period only for the sales side.
"""
import csv
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum
from django.http import StreamingHttpResponse
from django.db.models.functions import Coalesce
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response

from apps.catalog.models import Product, ProductVariant, SalesChannel
from apps.discounts.models import OrderDiscount
from apps.inventory.models import InventoryItem
from apps.orders.models import Order, OrderItem
from apps.payments.models import Payment, PaymentTransaction

from . import reports as base
from .reports import ReportsBase, ZERO, live

PAGE_SIZE = 50
MONEY_TYPES = ("money", "number")
Money = DecimalField(max_digits=16, decimal_places=2)


class _NoFormatOverride:
    URL_FORMAT_OVERRIDE = None


class QueryFormatFree(DefaultContentNegotiation):
    """DRF would turn ?format=csv into a 404; here it is an ordinary query parameter."""
    settings = _NoFormatOverride()


def col(key, label, kind="text"):
    return {"key": key, "label": label, "type": kind}


DATE = col("date", "Date", "date")
ORDERS = col("orders", "Orders", "number")
SALES_COLUMNS = [
    col("gross_sales", "Gross sales", "money"), col("discounts", "Discounts", "money"),
    col("shipping_charges", "Shipping charges", "money"), col("taxes", "Taxes", "money"),
    col("total_sales", "Total sales", "money"),
]


# --- One run of a report: the store, the dates and the filters ---------------------------

class Ctx:
    def __init__(self, store, start, end, currency, filters):
        self.store, self.start, self.end, self.currency, self.filters = store, start, end, currency, filters
        self.tz = base.store_tz(store)
        self.hourly = start == end
        self.period_start = base.datetime.combine(start, base.time.min, self.tz)
        self.period_end = base.datetime.combine(end + timedelta(days=1), base.time.min, self.tz)

    def prior_customers(self):
        """Customers (by email, else id) with an order before the period started."""
        return (Order.objects.filter(store=self.store, created_at__lt=self.period_start)
                .exclude(status="cancelled").annotate(who=base._who()).filter(who__isnull=False).values("who"))

    def narrow(self, orders):
        """Apply the product / channel / customer type filters to an Order queryset."""
        f = self.filters
        if f.get("channel"):
            orders = orders.filter(sales_channel_id=f["channel"])
        if f.get("product"):
            orders = orders.filter(
                pk__in=OrderItem.objects.filter(product_id=f["product"], is_deleted=False).values("order_id"))
        if f.get("customer_type"):
            orders = orders.annotate(ct_who=base._who())
            returning = Q(ct_who__in=self.prior_customers())
            orders = (orders.filter(returning) if f["customer_type"] == "returning"
                      else orders.filter(Q(ct_who__isnull=True) | ~returning))
        return orders

    def orders(self, cancelled=False):
        qs = Order.objects.filter(store=self.store, currency_code=self.currency,
                                  created_at__gte=self.period_start, created_at__lt=self.period_end)
        return self.narrow(qs if cancelled else qs.exclude(status="cancelled"))

    def items(self):
        items = OrderItem.objects.filter(order__in=self.orders(), is_deleted=False)
        return items.filter(product_id=self.filters["product"]) if self.filters.get("product") else items

    def sessions(self):
        return base.period_sessions(self.store, self.start, self.end)

    def series(self):
        return base.period_analytics(self.store, self.start, self.end, self.currency, narrow=self.narrow)


ORDER_SALES = dict(
    gross=Sum("subtotal"), disc=Sum("discount_amount"), ship=Sum("shipping_amount"), tax=Sum("tax_amount"), n=Count("id"))


def sales_row(r):
    gross, disc, ship, tax = (base._money(r[k]) for k in ("gross", "disc", "ship", "tax"))
    return {"orders": r["n"], "gross_sales": gross, "discounts": disc, "shipping_charges": ship, "taxes": tax,
            "total_sales": gross - disc + ship + tax}


def sum_totals(columns, rows):
    totals = {}
    for c in columns:
        if c["type"] in MONEY_TYPES:
            total = sum((r[c["key"]] or 0 for r in rows), 0)
            totals[c["key"]] = base._money(total) if c["type"] == "money" else total
    return totals


# --- "Over time" reports: one row per day (per hour for a single day) --------------------

def over_time(columns):
    def build(ctx):
        totals, series, _ = ctx.series()
        rows = [{"date": r["bucket"], **{k: v for k, v in r.items() if k != "bucket"}} for r in series]
        return [DATE] + columns, rows, totals
    return build


# --- "By X" reports ------------------------------------------------------------------------

def by_product(with_taxes):
    def build(ctx):
        columns = [col("product", "Product"), col("units", "Units sold", "number"), col("gross_sales", "Gross sales", "money"),
                   col("discounts", "Discounts", "money"), col("net_sales", "Net sales", "money")]
        if with_taxes:
            columns += [col("taxes", "Taxes", "money"), col("total_sales", "Total sales", "money")]
        rows = []
        for r in (ctx.items().values("product_id", "product_name").annotate(
                units=Sum("quantity"), gross=Sum(ExpressionWrapper(F("unit_price") * F("quantity"), output_field=Money)),
                disc=Sum("discount_amount"), net=Sum("total"), tax=Sum("tax_amount"))):
            net, tax = base._money(r["net"]), base._money(r["tax"])
            rows.append({"product": r["product_name"], "units": r["units"], "gross_sales": base._money(r["gross"]),
                         "discounts": base._money(r["disc"]), "net_sales": net, "taxes": tax, "total_sales": net + tax})
        return columns, rows, sum_totals(columns, rows)
    return build


def by_variant(ctx):
    columns = [col("product", "Product"), col("sku", "SKU"), col("units", "Units sold", "number"),
               col("net_sales", "Net sales", "money"), col("taxes", "Taxes", "money"), col("total_sales", "Total sales", "money")]
    rows = []
    for r in (ctx.items().values("variant_id", "product_name", "sku")
              .annotate(units=Sum("quantity"), net=Sum("total"), tax=Sum("tax_amount"))):
        net, tax = base._money(r["net"]), base._money(r["tax"])
        rows.append({"product": r["product_name"], "sku": r["sku"] or "", "units": r["units"],
                     "net_sales": net, "taxes": tax, "total_sales": net + tax})
    return columns, rows, sum_totals(columns, rows)


def by_customer(ctx):
    columns = [col("customer", "Customer"), col("email", "Email")] + [ORDERS] + SALES_COLUMNS
    rows = []
    for r in (ctx.orders().annotate(who=base._who()).values("who").annotate(
            first=Max("customer__first_name"), last=Max("customer__last_name"),
            acct=Max("customer__email"), guest=Max("customer_email"), **ORDER_SALES)):
        email = r["acct"] or r["guest"] or ""
        name = f"{r['first'] or ''} {r['last'] or ''}".strip()
        rows.append({"customer": name or email or "Unknown", "email": email, **sales_row(r)})
    return columns, rows, sum_totals(columns, rows)


def by_discount(ctx):
    columns = [col("discount", "Discount"), col("code", "Code"), ORDERS, col("discount_amount", "Discount amount", "money"),
               col("gross_sales", "Gross sales", "money"), col("total_sales", "Total sales", "money")]
    rows = []
    for r in (OrderDiscount.objects.filter(order__in=ctx.orders(), is_deleted=False)
              .values("discount__name", "discount_code__code")
              .annotate(n=Count("order", distinct=True), amount=Sum("discount_amount"), gross=Sum("order__subtotal"),
                        disc=Sum("order__discount_amount"), ship=Sum("order__shipping_amount"), tax=Sum("order__tax_amount"))):
        gross = base._money(r["gross"])
        rows.append({"discount": r["discount__name"], "code": r["discount_code__code"] or "", "orders": r["n"],
                     "discount_amount": base._money(r["amount"]), "gross_sales": gross,
                     "total_sales": gross - base._money(r["disc"]) + base._money(r["ship"]) + base._money(r["tax"])})
    return columns, rows, sum_totals(columns, rows)


def by_channel(ctx):
    columns = [col("channel", "Sales channel"), ORDERS] + SALES_COLUMNS
    rows = [{"channel": r["sales_channel__name"] or "No channel", **sales_row(r)}
            for r in ctx.orders().values("sales_channel__name").annotate(**ORDER_SALES)]
    return columns, rows, sum_totals(columns, rows)


def by_status(ctx):
    columns = [col("status", "Order status"), col("fulfillment_status", "Fulfillment status"), ORDERS,
               col("total_sales", "Total sales", "money")]
    rows = []
    for r in ctx.orders(cancelled=True).values("status", "fulfillment_status").annotate(**ORDER_SALES):
        rows.append({"status": r["status"], "fulfillment_status": r["fulfillment_status"],
                     "orders": r["n"], "total_sales": sales_row(r)["total_sales"]})
    return columns, rows, sum_totals(columns, rows)


def by_location(ctx):
    columns = [col("country", "Country"), col("city", "City"), col("customers", "Customers", "number"), ORDERS,
               col("total_sales", "Total sales", "money")]
    rows = []
    for r in (ctx.orders().values("customer__country", "customer__city")
              .annotate(customers=Count("customer_id", distinct=True), **ORDER_SALES)):
        rows.append({"country": r["customer__country"] or "Unknown", "city": r["customer__city"] or "Unknown",
                     "customers": r["customers"], "orders": r["n"], "total_sales": sales_row(r)["total_sales"]})
    return columns, rows, sum_totals(columns, rows)


# --- Sessions (visit tracking) -------------------------------------------------------------

SESSIONS = col("sessions", "Sessions", "number")
VISITORS = col("visitors", "Visitors", "number")
WITH_CART = col("sessions_with_cart", "Added to cart", "number")
REACHED_CHECKOUT = col("sessions_reached_checkout", "Reached checkout", "number")
COMPLETED = col("sessions_completed", "Completed", "number")
CONVERSION = col("conversion_rate", "Conversion rate", "percent")


def sessions_over_time(columns):
    def build(ctx):
        totals, buckets = base.session_analytics(ctx.store, ctx.start, ctx.end)
        rows = [{"date": key.isoformat(), **figures} for key, figures in buckets.items()]
        return [DATE] + columns, rows, totals
    return build


def _session_group(ctx, label_column, fields, label_of):
    """Rows of sessions grouped by `fields`, each with its orders and conversion rate; the totals row adds them up."""
    columns = [label_column, SESSIONS, COMPLETED, CONVERSION]
    rows = []
    for r in (ctx.sessions().values(*fields)
              .annotate(n=Count("id"), done=Count("id", filter=Q(order__isnull=False)))):
        rows.append({**label_of(r), "sessions": r["n"], "sessions_completed": r["done"],
                     "conversion_rate": base.rate(r["done"], r["n"])})
    totals = sum_totals(columns, rows)
    totals["conversion_rate"] = base.rate(totals.get("sessions_completed", 0), totals.get("sessions", 0))
    return columns, rows, totals


def sessions_by_landing_page(ctx):
    columns, rows, totals = _session_group(
        ctx, col("landing_page", "Landing page"), ("landing_path", "landing_page_type"),
        lambda r: {"landing_page": r["landing_path"] or "/", "page_type": r["landing_page_type"]})
    columns.insert(1, col("page_type", "Page type"))
    return columns, rows, totals


def sessions_by_referrer(ctx):
    """One row per referrer name and type ("facebook · social"): hosts like l.facebook.com and utm_source=facebook merge."""
    columns = [col("referrer", "Referrer"), col("referrer_name", "Name"), col("referrer_type", "Type"),
               SESSIONS, COMPLETED, CONVERSION]
    from apps.orders.services import referrer_name  # imported here: the orders services import this app's reports

    merged = {}
    for r in (ctx.sessions().values("referrer_host", "referrer_type", "utm_source")
              .annotate(n=Count("id"), done=Count("id", filter=Q(order__isnull=False)))):
        name = referrer_name(r["referrer_host"], r["utm_source"])
        row = merged.setdefault((name, r["referrer_type"]), {"n": 0, "done": 0})
        row["n"] += r["n"]
        row["done"] += r["done"]
    rows = [{"referrer": f"{name} · {kind}", "referrer_name": name, "referrer_type": kind, "sessions": m["n"],
             "sessions_completed": m["done"], "conversion_rate": base.rate(m["done"], m["n"])}
            for (name, kind), m in sorted(merged.items(), key=lambda i: (-i[1]["n"], i[0]))]
    totals = sum_totals(columns, rows)
    totals["conversion_rate"] = base.rate(totals.get("sessions_completed", 0), totals.get("sessions", 0))
    return columns, rows, totals


def sessions_by_device_type(ctx):
    return _session_group(ctx, col("device_type", "Device type"), ("device_type",),
                          lambda r: {"device_type": r["device_type"]})


def sessions_by_location(ctx):
    return _session_group(ctx, col("country", "Country"), ("country",),
                          lambda r: {"country": r["country"] or "Unknown"})


def social_link_clicks(ctx):
    """Clicks on the storefront's social links per network (store_daily_metrics.social_clicks); not tied to a currency."""
    columns = [col("network", "Network"), col("clicks", "Clicks", "number")]
    rows = [{"network": network, "clicks": n}
            for network, n in sorted(base.social_click_counts(ctx.store, ctx.start, ctx.end).items(), key=lambda i: (-i[1], i[0]))]
    return columns, rows, sum_totals(columns, rows)


def conversion_breakdown(ctx):
    figures, _ = base.session_analytics(ctx.store, ctx.start, ctx.end)
    columns = [col("step", "Step"), SESSIONS, col("share", "Share of sessions", "percent")]
    rows = [{"step": step["label"], "sessions": step["value"], "share": step["rate"]} for step in base.funnel(figures)]
    return columns, rows, {"sessions": figures["sessions"], "share": base.rate(figures["sessions_completed"], figures["sessions"])}


# --- Customers: new vs returning ---------------------------------------------------------

def _customer_history(ctx, orders):
    """(rows of (bucket, who), first order ever per customer) for the orders, in store time."""
    trunc = base.TruncHour if ctx.hourly else base.TruncDate
    pairs = list(orders.annotate(b=trunc("created_at", tzinfo=ctx.tz), who=base._who()).filter(who__isnull=False)
                 .values("b", "who").distinct())
    people = sorted({p["who"] for p in pairs})
    first = base.first_orders(ctx.store, people)
    return pairs, first


def new_vs_returning(ctx):
    columns = [DATE, col("new_customers", "New customers", "number"), col("returning_customers", "Returning customers", "number"),
               col("customers", "Customers", "number"), col("returning_customer_rate", "Returning customer rate", "percent")]
    pairs, first = _customer_history(ctx, ctx.orders())
    trunc_key = (lambda d: d.astimezone(ctx.tz).replace(tzinfo=None, minute=0, second=0, microsecond=0)) if ctx.hourly \
        else (lambda d: d.astimezone(ctx.tz).date())
    per = {}
    for p in pairs:
        key = base._bucket_key(p["b"], ctx.hourly)
        m = per.setdefault(key, [0, 0])
        m[0 if trunc_key(first[p["who"]]) == key else 1] += 1  # new: their first order ever is in this bucket
    rows = []
    for key in base._bucket_starts(ctx.start, ctx.end, ctx.tz, ctx.hourly):
        new, ret = per.get(key, [0, 0])
        rows.append({"date": key.isoformat(), "new_customers": new, "returning_customers": ret, "customers": new + ret,
                     "returning_customer_rate": round(ret / (new + ret) * 100, 1) if new + ret else 0.0})
    people = {p["who"] for p in pairs}
    ret = sum(first[w] < ctx.period_start for w in people)
    totals = {"new_customers": len(people) - ret, "returning_customers": ret, "customers": len(people),
              "returning_customer_rate": round(ret / len(people) * 100, 1) if people else 0.0}
    return columns, rows, totals


def first_time_vs_returning_sales(ctx):
    columns = [col("customer_type", "Customer type"), col("customers", "Customers", "number"), ORDERS] + SALES_COLUMNS
    orders = ctx.orders().annotate(who=base._who()).filter(who__isnull=False)
    first = base.first_orders(ctx.store, sorted({r["who"] for r in orders.values("who").distinct()}))
    groups = {"First-time": {"who": set(), "n": 0, "gross": ZERO, "disc": ZERO, "ship": ZERO, "tax": ZERO},
              "Returning": {"who": set(), "n": 0, "gross": ZERO, "disc": ZERO, "ship": ZERO, "tax": ZERO}}
    for r in orders.values("who").annotate(**ORDER_SALES):
        g = groups["Returning" if first[r["who"]] < ctx.period_start else "First-time"]
        g["who"].add(r["who"])
        for k in ("n", "gross", "disc", "ship", "tax"):
            g[k] += r[k]
    rows = [{"customer_type": name, "customers": len(g["who"]), **sales_row(g)} for name, g in groups.items()]
    return columns, rows, sum_totals(columns, rows)


def cohorts(ctx):
    months = 6
    columns = [col("cohort", "First order month"), col("customers", "Customers", "number")] + [
        col(f"month_{i}", "Month 0" if i == 0 else f"Month {i}", "percent") for i in range(months)]
    people = sorted({r["who"] for r in ctx.orders().annotate(who=base._who()).filter(who__isnull=False).values("who").distinct()})
    first = base.first_orders(ctx.store, people)
    new = [w for w in people if first[w] >= ctx.period_start]
    month = lambda d: (lambda x: x.year * 12 + x.month - 1)(d.astimezone(ctx.tz))
    cohort_of = {w: month(first[w]) for w in new}
    active = {}  # (cohort month, offset) -> customers who ordered that many months later
    for i in range(0, len(new), 500):
        for r in (Order.objects.filter(store=ctx.store).exclude(status="cancelled")
                  .annotate(who=base._who()).filter(who__in=new[i:i + 500]).values("who", "created_at")):
            offset = month(r["created_at"]) - cohort_of[r["who"]]
            if 0 <= offset < months:
                active.setdefault((cohort_of[r["who"]], offset), set()).add(r["who"])
    now_month = month(base.timezone.now())
    sizes = {}
    for w, c in cohort_of.items():
        sizes[c] = sizes.get(c, 0) + 1
    rows = []
    for c in sorted(sizes):
        row = {"cohort": f"{c // 12}-{c % 12 + 1:02d}", "customers": sizes[c]}
        for i in range(months):
            row[f"month_{i}"] = round(len(active.get((c, i), ())) / sizes[c] * 100, 1) if c + i <= now_month else None
        rows.append(row)
    return columns, rows, {"customers": len(new)}


# --- Inventory (a snapshot of stock now; sales from the period) ---------------------------

def _stock(ctx):
    """{variant id: (product, sku, available, on_hand, committed, incoming)}"""
    variants = ProductVariant.objects.filter(product__store=ctx.store).select_related("product")
    if ctx.filters.get("product"):
        variants = variants.filter(product_id=ctx.filters["product"])
    variants = variants.annotate(avail=Coalesce(Sum("inventory_items__available"), 0),
                                 hand=Coalesce(Sum("inventory_items__on_hand"), 0),
                                 commit=Coalesce(Sum("inventory_items__committed"), 0),
                                 incoming=Coalesce(Sum("inventory_items__incoming"), 0))
    return [v for v in variants if not getattr(v.product, "is_deleted", False)]


def inventory_remaining(ctx):
    columns = [col("product", "Product"), col("sku", "SKU"), col("available", "Available", "number"),
               col("on_hand", "On hand", "number"), col("committed", "Committed", "number"), col("incoming", "Incoming", "number")]
    rows = [{"product": v.product.title, "sku": v.sku or "", "available": v.avail, "on_hand": v.hand,
             "committed": v.commit, "incoming": v.incoming} for v in _stock(ctx)]
    return columns, rows, sum_totals(columns, rows)


def _stock_by_product(ctx):
    sold = {r["product_id"]: r["units"] for r in ctx.items().values("product_id").annotate(units=Sum("quantity"))}
    per = {}
    for v in _stock(ctx):
        p = per.setdefault(v.product_id, {"product": v.product.title, "available": 0})
        p["available"] += v.avail
    for pid, p in per.items():
        p["units"] = sold.get(pid, 0)
    return list(per.values())


def sell_through(ctx):
    columns = [col("product", "Product"), col("units", "Units sold", "number"), col("available", "Available", "number"),
               col("sell_through_rate", "Sell-through rate", "percent")]
    rows = []
    for p in _stock_by_product(ctx):
        denominator = p["units"] + max(p["available"], 0)
        rows.append({**{k: p[k] for k in ("product", "units", "available")},
                     "sell_through_rate": round(p["units"] / denominator * 100, 1) if denominator else 0.0})
    totals = sum_totals(columns, rows)
    denominator = totals["units"] + max(totals["available"], 0)
    totals["sell_through_rate"] = round(totals["units"] / denominator * 100, 1) if denominator else 0.0
    return columns, rows, totals


def days_remaining(ctx):
    columns = [col("product", "Product"), col("units", "Units sold", "number"), col("daily_sales", "Units sold per day", "number"),
               col("available", "Available", "number"), col("days_remaining", "Days of inventory remaining", "number")]
    days = (ctx.end - ctx.start).days + 1
    rows = []
    for p in _stock_by_product(ctx):
        daily = round(p["units"] / days, 2)
        rows.append({**{k: p[k] for k in ("product", "units", "available")}, "daily_sales": daily,
                     "days_remaining": round(max(p["available"], 0) / daily, 1) if daily else None})
    return columns, rows, {"units": sum(r["units"] for r in rows), "available": sum(r["available"] for r in rows)}


# --- Finances ------------------------------------------------------------------------------

def payments_by_method(ctx):
    columns = [col("method", "Payment method"), col("payments", "Payments", "number"),
               col("captured", "Amount captured", "money"), col("refunds", "Refunds", "money"), col("net", "Net", "money")]
    window = dict(created_at__gte=ctx.period_start, created_at__lt=ctx.period_end)
    scope = dict(store=ctx.store, currency=ctx.currency, is_deleted=False)
    per = {}
    for r in (Payment.objects.filter(**scope, **window).exclude(status__in=("pending", "failed"))
              .values("provider").annotate(n=Count("id"), s=Sum("amount"))):
        per[r["provider"]] = {"method": r["provider"], "payments": r["n"], "captured": base._money(r["s"]), "refunds": ZERO}
    for r in (PaymentTransaction.objects.filter(transaction_type="refund", status="success", **window)
              .filter(payment__store=ctx.store, payment__currency=ctx.currency, payment__is_deleted=False)
              .values("payment__provider").annotate(s=Sum("amount"))):
        row = per.setdefault(r["payment__provider"], {"method": r["payment__provider"], "payments": 0, "captured": ZERO})
        row["refunds"] = base._money(r["s"])
    rows = [{**p, "net": p["captured"] - p["refunds"]} for p in per.values()]
    return columns, rows, sum_totals(columns, rows)


# --- Registry --------------------------------------------------------------------------------

ORDER_FILTERS = ("product", "channel", "customer_type")
OT = {  # over-time columns
    "sales": [ORDERS, col("gross_sales", "Gross sales", "money"), col("discounts", "Discounts", "money"),
              col("returns", "Returns", "money"), col("net_sales", "Net sales", "money"),
              col("shipping_charges", "Shipping charges", "money"), col("taxes", "Taxes", "money"),
              col("total_sales", "Total sales", "money")],
    "aov": [ORDERS, col("average_order_value", "Average order value", "money")],
    "orders": [ORDERS],
    "fulfilled": [ORDERS, col("orders_fulfilled", "Orders fulfilled", "number")],
    "taxes": [ORDERS, col("taxes", "Taxes", "money")],
    "discounts": [ORDERS, col("gross_sales", "Gross sales", "money"), col("discounts", "Discounts", "money")],
    "returns": [col("returns", "Returns", "money")],
}


def report(id, name, category, description, build, kind="bar", chart="total_sales", label=None, sort=None,
           filters=ORDER_FILTERS, dated=True, aliases=()):
    return {"id": id, "name": name, "category": category, "description": description, "build": build, "kind": kind,
            "chart": chart, "label": label, "sort": sort, "filters": filters, "dated": dated, "aliases": aliases}


REPORTS = [
    # Sales
    report("total-sales-over-time", "Total sales over time", "Sales", "Gross sales, discounts, returns, net sales, shipping, taxes and total sales per day.",
           over_time(OT["sales"]), "line", "total_sales", "date", "date", aliases=("sales-over-time",)),
    report("total-sales-by-product", "Total sales by product", "Sales", "Units, net sales, taxes and total sales for each product.",
           by_product(True), "bar", "total_sales", "product", "-total_sales", aliases=("top-products",)),
    report("total-sales-by-variant", "Total sales by variant/SKU", "Sales", "Units and sales for each variant and SKU.",
           by_variant, "bar", "total_sales", "sku", "-total_sales"),
    report("total-sales-by-customer", "Total sales by customer", "Sales", "Orders and sales for each customer, guests matched by email.",
           by_customer, "bar", "total_sales", "customer", "-total_sales"),
    report("total-sales-by-discount", "Total sales by discount", "Sales", "Orders and sales that used each discount.",
           by_discount, "bar", "total_sales", "discount", "-total_sales", filters=("channel",)),
    report("total-sales-by-sales-channel", "Total sales by sales channel", "Sales", "Orders and sales for each sales channel.",
           by_channel, "bar", "total_sales", "channel", "-total_sales", filters=("product", "customer_type")),
    report("average-order-value-over-time", "Average order value over time", "Sales", "Average order value per day, before returns.",
           over_time(OT["aov"]), "line", "average_order_value", "date", "date", aliases=("average-order-value",)),
    report("net-sales-by-product", "Net sales by product", "Sales", "Gross sales, discounts and net sales for each product.",
           by_product(False), "bar", "net_sales", "product", "-net_sales"),
    # Orders
    report("orders-over-time", "Orders over time", "Orders", "How many orders came in each day.",
           over_time(OT["orders"]), "line", "orders", "date", "date", ),
    report("orders-fulfilled-over-time", "Orders fulfilled over time", "Orders", "Orders and how many of them are fulfilled, per day.",
           over_time(OT["fulfilled"]), "line", "orders_fulfilled", "date", "date"),
    report("orders-by-status", "Orders by status", "Orders", "Orders by order and fulfillment status, cancelled orders included.",
           by_status, "bar", "orders", "status", "-orders", filters=("channel",)),
    # Acquisition and conversion (storefront visits)
    report("sessions-over-time", "Sessions over time", "Acquisition", "Visits to the storefront per day, with the visitors and page views in them.",
           sessions_over_time([SESSIONS, VISITORS, col("page_views", "Page views", "number")]), "line", "sessions", "date", "date", filters=()),
    report("sessions-by-landing-page", "Sessions by landing page", "Acquisition", "The page each visit started on, with its orders and conversion rate.",
           sessions_by_landing_page, "bar", "sessions", "landing_page", "-sessions", filters=()),
    report("sessions-by-referrer", "Sessions by referrer", "Acquisition", "Where visits came from, by name and type (facebook · social, google · search, direct ...).",
           sessions_by_referrer, "bar", "sessions", "referrer", "-sessions", filters=()),
    report("sessions-by-device-type", "Sessions by device type", "Acquisition", "Visits from desktop, mobile and tablet.",
           sessions_by_device_type, "bar", "sessions", "device_type", "-sessions", filters=()),
    report("sessions-by-location", "Sessions by location", "Acquisition", "Visits by country, when the store's host passes it on (CF-IPCountry).",
           sessions_by_location, "bar", "sessions", "country", "-sessions", filters=()),
    report("visitors-over-time", "Visitors over time", "Acquisition", "Distinct visitors per day, and their sessions.",
           sessions_over_time([VISITORS, SESSIONS]), "line", "visitors", "date", "date", filters=()),
    report("social-link-clicks", "Social link clicks", "Acquisition", "Clicks on the store's social links (facebook, instagram, whatsapp ...), by network.",
           social_link_clicks, "bar", "clicks", "network", "-clicks", filters=()),
    report("conversion-rate-over-time", "Conversion rate over time", "Conversion", "Sessions that ended in an order, as a share of sessions, per day.",
           sessions_over_time([SESSIONS, WITH_CART, REACHED_CHECKOUT, COMPLETED, CONVERSION]), "line", "conversion_rate", "date", "date", filters=()),
    report("conversion-rate-breakdown", "Conversion rate breakdown", "Conversion", "Sessions, added to cart, reached checkout and completed.",
           conversion_breakdown, "bar", "sessions", "step", "-sessions", filters=()),
    # Customers
    report("new-vs-returning-customers-over-time", "New vs returning customers over time", "Customers",
           "New and returning customers per day; the totals row counts each customer once.",
           new_vs_returning, "line", "customers", "date", "date", filters=("channel",), aliases=("new-customers",)),
    report("customers-by-location", "Customers by location", "Customers", "Customers, orders and sales by country and city.",
           by_location, "bar", "total_sales", "country", "-total_sales"),
    report("first-time-vs-returning-customer-sales", "First-time vs returning customer sales", "Customers",
           "Sales from customers new in the period and from customers who ordered before it.",
           first_time_vs_returning_sales, "bar", "total_sales", "customer_type", "-total_sales", filters=("product", "channel")),
    report("customer-cohort-analysis", "Customer cohort analysis", "Customers",
           "Share of each month's new customers that ordered again in the months after.",
           cohorts, "bar", "customers", "cohort", "cohort", filters=("channel",)),
    # Inventory
    report("inventory-remaining", "Inventory remaining per product/variant", "Inventory", "Units available now for each variant.",
           inventory_remaining, "bar", "available", "product", "-available", filters=("product",), dated=False),
    report("products-by-sell-through-rate", "Products by sell-through rate", "Inventory",
           "Units sold in the period as a share of units sold plus units still available.",
           sell_through, "bar", "sell_through_rate", "product", "-sell_through_rate", filters=("product",)),
    report("days-of-inventory-remaining", "Days of inventory remaining", "Inventory",
           "How long the units available last at the period's average daily sales.",
           days_remaining, "bar", "days_remaining", "product", "days_remaining", filters=("product",)),
    # Finances
    report("taxes-over-time", "Taxes over time", "Finances", "Taxes charged per day.",
           over_time(OT["taxes"]), "line", "taxes", "date", "date"),
    report("discounts-over-time", "Discounts over time", "Finances", "Discounts given per day.",
           over_time(OT["discounts"]), "line", "discounts", "date", "date"),
    report("returns-over-time", "Returns over time", "Finances", "Refunds made per day, on the day of the refund.",
           over_time(OT["returns"]), "line", "returns", "date", "date", filters=("channel",), aliases=()),
    report("payments-by-method", "Payments by method", "Finances", "Payments captured and refunded for each payment method.",
           payments_by_method, "bar", "captured", "method", "-captured", filters=()),
]
BY_ID = {r["id"]: r for r in REPORTS}
BY_ID.update({alias: r for r in REPORTS for alias in r["aliases"]})
FILTER_NAMES = ("product", "channel", "customer_type")


# --- Views -------------------------------------------------------------------------------------

class ReportListView(ReportsBase):
    def get(self, request):
        return Response([
            {"id": r["id"], "name": r["name"], "category": r["category"], "description": r["description"],
             "created_by": "System", "last_viewed": None, "filters": list(r["filters"]), "dated": r["dated"]}
            for r in REPORTS])


def _sort_key(value):
    return (value is None, value if value is not None else 0)


def _cell(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value  # a spreadsheet would run it as a formula
    return value


class Echo:
    def write(self, value):
        return value


class ReportDetailView(ReportsBase):
    content_negotiation_class = QueryFormatFree

    def filters(self, request, meta):
        out = {}
        for name in FILTER_NAMES:
            value = request.query_params.get(name)
            if not value:
                continue
            if name not in meta["filters"]:
                raise ValidationError({name: "This report can't be filtered that way."})
            if name == "customer_type" and value not in ("new", "returning"):
                raise ValidationError({name: "Use new or returning."})
            if name != "customer_type":
                import uuid
                try:
                    uuid.UUID(value)
                except ValueError:
                    raise ValidationError({name: "Not a valid id."})
            out[name] = value
        return out

    def run(self, meta, store, start, end, currency, filters):
        columns, rows, totals = meta["build"](Ctx(store, start, end, currency, filters))
        return columns, rows, totals

    def get(self, request, report_id):
        meta = BY_ID.get(report_id)
        if meta is None:
            raise NotFound("Unknown report.")
        store = request.store
        end = base._parse_day(request, "to") or base.local_today(store)
        start = base._parse_day(request, "from") or end - timedelta(days=29)
        if start > end:
            raise ValidationError({"from": "The start date can't be after the end date."})
        if (end - start).days + 1 > base.MAX_ANALYTICS_DAYS:
            raise ValidationError({"from": f"Pick a range of at most {base.MAX_ANALYTICS_DAYS} days."})
        mode = request.query_params.get("compare", "previous_period")
        if mode not in base.COMPARE_MODES:
            raise ValidationError({"compare": "Use previous_period, previous_year or none."})
        currency = (request.query_params.get("currency") or store.currency or "INR").upper()
        filters = self.filters(request, meta)

        columns, rows, totals = self.run(meta, store, start, end, currency, filters)
        keys = [c["key"] for c in columns]
        chart_rows = rows  # in the report's own order: time for line charts

        sort = request.query_params.get("sort") or meta["sort"] or keys[0]
        key, descending = sort.lstrip("-"), sort.startswith("-")
        if key not in keys:
            raise ValidationError({"sort": f"Sort by one of: {', '.join(keys)}."})
        ordered = sorted(rows, key=lambda r: _sort_key(r[key]), reverse=descending)
        if descending:  # keep empty values last whichever way we sort
            ordered = [r for r in ordered if r[key] is not None] + [r for r in ordered if r[key] is None]

        if request.query_params.get("format") == "csv":
            return self.csv(meta, columns, ordered, start, end)

        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except ValueError:
            raise ValidationError({"page": "Not a number."})

        other = base.comparison_range(start, end, mode) if meta["dated"] else None
        previous = None
        if other:
            _, previous_rows, previous_totals = self.run(meta, store, other[0], other[1], currency, filters)
            previous = (previous_rows, previous_totals)

        chart_key = meta["chart"]
        if meta["kind"] == "line":
            points = [{"label": r["date"], "value": r.get(chart_key)} for r in chart_rows]
            comparison = [{"label": r["date"], "value": r.get(chart_key)} for r in previous[0]] if previous else None
        else:
            top = sorted(rows, key=lambda r: _sort_key(r[chart_key]), reverse=True)
            top = [r for r in top if r[chart_key]][:15]
            points = [{"label": r[meta["label"]], "value": r[chart_key]} for r in top]
            comparison = None

        count = len(ordered)
        return Response({
            "id": meta["id"], "name": meta["name"], "category": meta["category"], "description": meta["description"],
            "kind": meta["kind"], "dated": meta["dated"], "interval": "hour" if start == end else "day",
            "from": start, "to": end, "compare": mode if meta["dated"] else "none",
            "compare_from": other[0] if other else None, "compare_to": other[1] if other else None,
            "timezone": str(base.store_tz(store)), "currency_code": currency,
            "filters": filters, "supported_filters": list(meta["filters"]), "filter_options": self.options(store, meta),
            "columns": columns,
            "rows": ordered[(page - 1) * PAGE_SIZE: page * PAGE_SIZE],
            "totals": totals, "previous_totals": previous[1] if previous else None,
            "row_count": count, "page": page, "page_size": PAGE_SIZE,
            "sort": sort,
            "chart": {"type": meta["kind"], "value_key": chart_key,
                      "value_label": next(c["label"] for c in columns if c["key"] == chart_key),
                      "value_type": next(c["type"] for c in columns if c["key"] == chart_key),
                      "points": points, "comparison": comparison},
            "last_viewed": None,
        })

    def options(self, store, meta):
        out = {}
        if "channel" in meta["filters"]:
            out["channels"] = [{"id": c.pk, "name": c.name} for c in SalesChannel.objects.filter(store=store).order_by("name")]
        if "product" in meta["filters"]:
            out["products"] = [{"id": p.pk, "title": p.title}
                               for p in live(Product.objects.filter(store=store)).order_by("title")[:300]]
        return out

    def csv(self, meta, columns, rows, start, end):
        writer = csv.writer(Echo())

        def lines():
            yield "﻿" + writer.writerow([c["label"] for c in columns])
            for r in rows:
                yield writer.writerow([_cell(r[c["key"]]) for c in columns])

        response = StreamingHttpResponse(lines(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{meta["id"]}_{start}_{end}.csv"'
        return response
