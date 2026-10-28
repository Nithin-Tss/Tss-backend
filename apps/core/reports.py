"""
Reports and analytics for the store admin (permission area "reports").

GET /api/v1/reports/            the built-in reports the Reports page lists
GET /api/v1/reports/overview/   totals, a daily sales series and recent activity
                                (?days=30, between 1 and 365)
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.models import Product
from apps.customers.models import Customer
from apps.orders.models import Order

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


class OverviewView(ReportsBase):
    def get(self, request):
        try:
            days = min(max(int(request.query_params.get("days", 30)), 1), 365)
        except ValueError:
            days = 30

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
