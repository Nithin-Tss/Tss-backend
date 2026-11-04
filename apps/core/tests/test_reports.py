"""
Reports and overview endpoints, and storefront visit tracking with the session reports.

    python manage.py test apps.core.tests.test_reports --settings=config.test_settings
"""
import contextlib
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo
from unittest import mock

from celery.exceptions import Retry

from django.core.cache import cache
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.core import reports, tasks
from apps.customers.models import Customer
from apps.orders import services as order_services
from apps.orders.models import BackgroundJob, Order, OrderItem, StoreDailyMetric, StorefrontSession
from apps.payments.models import Payment, PaymentTransaction
from apps.tenancy.models import Store
from apps.themes.rendering import render_page

from .test_rbac import RbacTestBase


class ReportsTest(RbacTestBase):
    def test_reports_list_has_built_in_reports(self):
        reports = self.ok("get", "/api/v1/reports/")
        self.assertTrue(reports)
        self.assertTrue(all(r["name"] and r["category"] for r in reports))

    def test_overview_counts_the_stores_orders_and_customers(self):
        store = Store.objects.get(pk=self.store_id)
        Customer.objects.create(store=store, email_subscription_status="subscribed")
        for number, status, total in (("1", "open", "30.00"), ("2", "open", "10.00"), ("3", "cancelled", "99.00")):
            Order.objects.create(
                store=store, order_number=number, currency_code="INR", subtotal=total, discount_amount=0,
                tax_amount=0, shipping_amount=0, total=total, status=status, fulfillment_status="unfulfilled")

        data = self.ok("get", "/api/v1/reports/overview/?days=30")

        self.assertEqual(data["totals"]["orders"], 2)  # cancelled orders don't count
        self.assertEqual(float(data["totals"]["sales"]), 40.0)
        self.assertEqual(float(data["totals"]["average_order_value"]), 20.0)
        self.assertEqual(data["totals"]["customers"], 1)
        self.assertEqual(sum(day["orders"] for day in data["series"]), 2)
        self.assertTrue(data["activity"])

    def test_old_report_ids_still_work(self):
        for old, new in (("sales-over-time", "total-sales-over-time"), ("top-products", "total-sales-by-product"),
                         ("new-customers", "new-vs-returning-customers-over-time"),
                         ("average-order-value", "average-order-value-over-time")):
            self.assertEqual(self.ok("get", f"/api/v1/reports/{old}/")["id"], new)
        self.assertEqual(self.client.get("/api/v1/reports/nope/").status_code, 404)

    def test_staff_needs_the_reports_permission(self):
        self.staff("support@example.com", "customer_support")  # has no reports.view
        self.assertEqual(self.client.get("/api/v1/reports/overview/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/reports/").status_code, 403)

        self.staff("admin@example.com", "administrator")  # has every permission
        self.assertEqual(self.client.get("/api/v1/reports/overview/").status_code, 200)


class ShippingPageFlowTest(RbacTestBase):
    """The calls the admin Shipping page makes for delivery partners."""

    def test_add_deactivate_and_remove_a_delivery_partner(self):
        created = self.ok("post", "/api/v1/delivery/gateways/",
                          {"name": "Blue Dart", "code": "blue-dart", "is_active": True}, expect=201)
        url = f"/api/v1/delivery/gateways/{created['id']}/"

        self.assertEqual(self.ok("patch", url, {"is_active": False})["is_active"], False)
        self.assertEqual([g["code"] for g in self.ok("get", "/api/v1/delivery/gateways/")], ["blue-dart"])

        self.ok("delete", url, expect=204)
        self.assertEqual(self.ok("get", "/api/v1/delivery/gateways/"), [])


class CatalogueApiTest(RbacTestBase):
    def product(self):
        return self.ok("post", "/api/v1/catalog/products/", {"title": "Hat", "price": "10", "sku": "HAT-1"}, expect=201)["id"]

    def test_catalogue_crud_and_product_counts(self):
        hat = self.product()
        made = self.ok("post", "/api/v1/catalog/catalogues/", {
            "name": "Wholesale", "status": "active", "price_adjustment": "15", "adjustment_direction": "decrease",
            "currency_code": "usd", "markets": ["India"], "auto_include_new_products": False, "products": [hat],
        }, expect=201)
        self.assertEqual(made["currency_code"], "USD")
        self.assertEqual(made["products_count"], 1)
        url = f"/api/v1/catalog/catalogues/{made['id']}/"

        updated = self.ok("patch", url, {"auto_include_new_products": True, "products": [], "excluded_products": [hat]})
        self.assertEqual(updated["products_count"], 0)
        self.assertEqual(len(self.ok("get", "/api/v1/catalog/catalogues/")), 1)

        self.ok("delete", url, expect=204)
        self.assertEqual(self.ok("get", "/api/v1/catalog/catalogues/"), [])

    def test_rejects_bad_input(self):
        hat = self.product()
        for body in (
            {"name": "  "},
            {"name": "X", "price_adjustment": "150"},
            {"name": "X", "products": ["11111111-1111-1111-1111-111111111111"]},
            {"name": "X", "products": [hat], "excluded_products": [hat]},
        ):
            self.assertEqual(self.client.post("/api/v1/catalog/catalogues/", body, format="json").status_code, 400, body)

    def test_staff_without_product_access_is_refused(self):
        self.staff("support@example.com", "online_store_editor")
        self.assertEqual(self.client.get("/api/v1/catalog/catalogues/").status_code, 403)


class DailyMetricsTest(RbacTestBase):
    """analytics.store_daily_metrics, the aggregation task and /reports/daily-metrics/."""

    def setUp(self):
        super().setUp()
        Store.objects.filter(pk=self.store_id).update(timezone="UTC")
        self.shop = Store.objects.get(pk=self.store_id)
        self.product_id = self.ok("post", "/api/v1/catalog/products/",
                                  {"title": "Hat", "price": "10", "sku": "HAT-1"}, expect=201)["id"]
        self.numbers = 0

    def order(self, total, at, status="open", currency="INR", units=1, store=None):
        self.numbers += 1
        order = Order.objects.create(
            store=store or self.shop, order_number=str(self.numbers), currency_code=currency, subtotal=total,
            discount_amount=0, tax_amount=0, shipping_amount=0, total=total, status=status,
            fulfillment_status="unfulfilled")
        Order.objects.filter(pk=order.pk).update(created_at=at)
        if store is None:
            OrderItem.objects.create(order=order, product_id=self.product_id, product_name="Hat", quantity=units,
                                     unit_price=total, discount_amount=0, tax_amount=0, total=total, is_deleted=False)
        return order

    def refund(self, order, amount, status="success"):
        payment = Payment.objects.create(store=order.store, order=order, currency=order.currency_code,
                                         amount=order.total, status="captured", provider="manual", is_deleted=False)
        PaymentTransaction.objects.create(payment=payment, transaction_type="refund", amount=amount, status=status)

    def run_days(self, start, end):
        return tasks.aggregate_daily_metrics.delay(store_id=str(self.shop.pk), start=start, end=end).get()

    def metric(self, day, currency="INR"):
        return StoreDailyMetric.objects.get(store=self.shop, metric_date=day, currency_code=currency)

    def test_table_is_created_in_the_analytics_schema(self):
        self.assertEqual(StoreDailyMetric._meta.db_table, '"analytics"."store_daily_metrics"')
        self.assertIn("analytics__store_daily_metrics", connection.introspection.table_names())

    def test_orders_sales_refunds_net_items_and_average(self):
        day = datetime(2026, 10, 1, 10, tzinfo=dt_timezone.utc)
        first = self.order("30.00", day, units=2)
        self.order("10.00", day, units=3)
        self.refund(first, "5.00")
        self.refund(first, "7.00", status="failed")  # only successful refunds count

        self.run_days("2026-10-01", "2026-10-01")

        m = self.metric(date(2026, 10, 1))
        self.assertEqual((m.total_orders, m.items_sold), (2, 5))
        self.assertEqual((m.gross_sales, m.total_refunds, m.net_sales, m.average_order_value),
                         (Decimal("40.00"), Decimal("5.00"), Decimal("35.00"), Decimal("20.00")))

    def test_cancelled_orders_and_their_refunds_are_left_out(self):
        day = datetime(2026, 10, 1, 10, tzinfo=dt_timezone.utc)
        self.order("30.00", day)
        cancelled = self.order("99.00", day, status="cancelled")
        self.refund(cancelled, "99.00")

        self.run_days("2026-10-01", "2026-10-01")

        m = self.metric(date(2026, 10, 1))
        self.assertEqual((m.total_orders, m.gross_sales, m.total_refunds), (1, Decimal("30.00"), Decimal("0.00")))

    def test_currencies_get_separate_rows(self):
        day = datetime(2026, 10, 1, 10, tzinfo=dt_timezone.utc)
        self.order("30.00", day)
        self.order("5.00", day, currency="USD")

        self.run_days("2026-10-01", "2026-10-01")

        self.assertEqual(self.metric(date(2026, 10, 1)).gross_sales, Decimal("30.00"))
        self.assertEqual(self.metric(date(2026, 10, 1), "USD").gross_sales, Decimal("5.00"))

    def test_days_follow_the_store_timezone(self):
        Store.objects.filter(pk=self.store_id).update(timezone="Asia/Kolkata")
        self.shop.refresh_from_db()
        self.order("30.00", datetime(2026, 10, 1, 20, tzinfo=dt_timezone.utc))  # 01:30 on 2 Oct in India

        self.run_days("2026-10-01", "2026-10-02")

        self.assertEqual(self.metric(date(2026, 10, 2)).total_orders, 1)
        self.assertFalse(StoreDailyMetric.objects.filter(store=self.shop, metric_date=date(2026, 10, 1)).exists())

    def test_running_twice_does_not_double_count_and_later_cancellations_zero_the_day(self):
        order = self.order("30.00", datetime(2026, 10, 1, 10, tzinfo=dt_timezone.utc))
        self.run_days("2026-10-01", "2026-10-01")
        self.run_days("2026-10-01", "2026-10-01")
        self.assertEqual(StoreDailyMetric.objects.filter(store=self.shop).count(), 1)
        self.assertEqual(self.metric(date(2026, 10, 1)).gross_sales, Decimal("30.00"))

        Order.objects.filter(pk=order.pk).update(status="cancelled")
        self.run_days("2026-10-01", "2026-10-01")
        m = self.metric(date(2026, 10, 1))
        self.assertEqual((m.total_orders, m.gross_sales, m.items_sold), (0, Decimal("0.00"), 0))

    def test_stores_are_kept_apart(self):
        _, other = self.sign_up("other@example.com", "Other shop")
        other_store = Store.objects.get(pk=other["storeId"])
        Store.objects.filter(pk__in=[self.shop.pk, other_store.pk]).update(timezone="UTC", status="active")
        self.order("30.00", timezone.now())
        self.order("500.00", timezone.now(), store=other_store)

        tasks.aggregate_daily_metrics.delay(lookback_days=2).get()  # every active store

        mine = StoreDailyMetric.objects.get(store=self.shop)
        theirs = StoreDailyMetric.objects.get(store=other_store)
        self.assertEqual((mine.gross_sales, theirs.gross_sales), (Decimal("30.00"), Decimal("500.00")))
        self.as_owner()
        data = self.ok("get", "/api/v1/reports/daily-metrics/?days=7")
        self.assertEqual([float(r["gross_sales"]) for r in data["rows"]], [30.0])

    def test_backfill_covers_a_historical_range(self):
        for day in (1, 5, 20):
            self.order("10.00", datetime(2026, 9, day, 12, tzinfo=dt_timezone.utc))

        written = self.run_days("2026-09-01", "2026-09-10")

        self.assertEqual(written, 2)
        self.assertEqual(sorted(StoreDailyMetric.objects.values_list("metric_date", flat=True)),
                         [date(2026, 9, 1), date(2026, 9, 5)])

    def test_task_retries_after_a_database_error(self):
        self.order("30.00", datetime(2026, 10, 1, 10, tzinfo=dt_timezone.utc))
        real = reports.aggregate_daily_metrics
        calls = []

        def flaky(*args):
            calls.append(args)
            if len(calls) == 1:
                raise OperationalError("connection lost")
            return real(*args)

        # Eager mode raises Retry where a worker would re-queue the task; run it again as the worker would.
        with mock.patch.object(reports, "aggregate_daily_metrics", side_effect=flaky):
            with self.assertRaises(Retry):
                self.run_days("2026-10-01", "2026-10-01")
            self.assertFalse(StoreDailyMetric.objects.exists())
            self.run_days("2026-10-01", "2026-10-01")

        self.assertEqual(len(calls), 2)
        self.assertEqual(tasks.aggregate_daily_metrics.max_retries, 5)
        self.assertEqual(self.metric(date(2026, 10, 1)).total_orders, 1)

    def test_database_constraints(self):
        values = {"store": self.shop, "metric_date": date(2026, 10, 1), "currency_code": "INR"}
        StoreDailyMetric.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StoreDailyMetric.objects.create(**values)  # one row per store, day and currency
        with self.assertRaises(IntegrityError), transaction.atomic():
            StoreDailyMetric.objects.create(**{**values, "currency_code": "USD"},
                                            gross_sales=10, total_refunds=2, net_sales=9)  # net must be gross - refunds
        with self.assertRaises(IntegrityError), transaction.atomic():
            StoreDailyMetric.objects.create(**{**values, "currency_code": "EUR"}, gross_sales=-1, net_sales=-1)

    def test_metrics_match_the_live_reports(self):
        now = timezone.now()
        self.order("30.00", now)
        self.order("10.00", now, units=4)
        self.order("99.00", now, status="cancelled")
        tasks.aggregate_daily_metrics.delay(store_id=str(self.shop.pk), lookback_days=30).get()

        live = self.ok("get", "/api/v1/reports/overview/?days=30")["totals"]
        stored = self.ok("get", "/api/v1/reports/daily-metrics/?days=30")["totals"]["INR"]
        today = now.date()
        top = self.ok("get", f"/api/v1/reports/top-products/?from={today - timedelta(days=29)}&to={today}"
                             "&compare=none")["totals"]["units"]

        self.assertEqual(stored["total_orders"], live["orders"])
        self.assertEqual(Decimal(str(stored["gross_sales"])), Decimal(str(live["sales"])))
        self.assertEqual(Decimal(str(stored["average_order_value"])), Decimal(str(live["average_order_value"])))
        self.assertEqual(stored["items_sold"], top)

    def test_daily_metrics_endpoint_needs_the_reports_permission(self):
        self.staff("support@example.com", "customer_support")
        self.assertEqual(self.client.get("/api/v1/reports/daily-metrics/").status_code, 403)

    def test_each_run_is_tracked_as_a_job(self):
        self.order("30.00", datetime(2026, 10, 1, 10, tzinfo=dt_timezone.utc))
        self.run_days("2026-10-01", "2026-10-01")

        job = BackgroundJob.objects.get()
        self.assertEqual((job.job_type, job.task_name), ("daily_metrics", "apps.core.tasks.aggregate_daily_metrics"))
        self.assertEqual((job.status, job.progress, job.retries, job.error), (BackgroundJob.SUCCEEDED, 100, 0, ""))
        self.assertEqual(job.store_id, self.shop.pk)
        self.assertEqual(job.params["start"], "2026-10-01")
        self.assertEqual(job.result, {"rows_written": 1, "stores": 1})
        self.assertIsNotNone(job.celery_task_id)
        self.assertLessEqual(job.started_at, job.finished_at)

    def test_a_failed_run_records_its_error(self):
        with mock.patch.object(reports, "aggregate_daily_metrics", side_effect=ValueError("bad day")):
            with self.assertRaises(ValueError):
                self.run_days("2026-10-01", "2026-10-01")
        job = BackgroundJob.objects.get()
        self.assertEqual((job.status, job.error), (BackgroundJob.FAILED, "ValueError: bad day"))
        self.assertIsNotNone(job.finished_at)

    def test_a_retry_updates_the_same_job(self):
        job = BackgroundJob.objects.create(job_type="daily_metrics", task_name="t", celery_task_id="abc",
                                           status=BackgroundJob.FAILED, error="boom")
        task = mock.Mock(request=mock.Mock(id="abc", retries=1))
        task.name = "t"
        again = tasks.job_start("daily_metrics", task, store_id=self.shop.pk)
        self.assertEqual(again.pk, job.pk)
        self.assertEqual((again.status, again.retries, again.error), (BackgroundJob.RUNNING, 1, ""))
        self.assertEqual(BackgroundJob.objects.count(), 1)

    def test_job_database_constraints(self):
        for bad in ({"status": "lost"}, {"progress": 101},
                    {"started_at": timezone.now(), "finished_at": timezone.now() - timedelta(minutes=1)}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                BackgroundJob.objects.create(job_type="x", task_name="x", **bad)
        BackgroundJob.objects.create(job_type="x", task_name="x", celery_task_id="same")
        with self.assertRaises(IntegrityError), transaction.atomic():
            BackgroundJob.objects.create(job_type="x", task_name="x", celery_task_id="same")


class AnalyticsBase(RbacTestBase):
    """Orders placed at store (Asia/Kolkata) times, refunds and a GET helper."""

    URL = "/api/v1/reports/analytics/"

    def setUp(self):
        super().setUp()
        Store.objects.filter(pk=self.store_id).update(timezone="Asia/Kolkata", currency="INR")
        self.shop = Store.objects.get(pk=self.store_id)
        self.numbers = 0

    def order(self, local, subtotal="100.00", discount="10.00", shipping="5.00", tax="9.00", email=None,
              status="open", fulfillment="unfulfilled", currency="INR"):
        """An order placed at `local`, a store-time (Asia/Kolkata) datetime string like 2026-10-02 23:30."""
        self.numbers += 1
        total = Decimal(subtotal) - Decimal(discount) + Decimal(shipping) + Decimal(tax)
        order = Order.objects.create(
            store=self.shop, order_number=str(self.numbers), currency_code=currency, subtotal=subtotal,
            discount_amount=discount, tax_amount=tax, shipping_amount=shipping, total=total, status=status,
            fulfillment_status=fulfillment, customer_email=email)
        at = datetime.fromisoformat(local).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        Order.objects.filter(pk=order.pk).update(created_at=at)
        order.created_at = at
        return order

    def refund(self, order, amount, status="success", at=None):
        """A refund made at `at` (store time); on the order's own day when not given."""
        payment = Payment.objects.create(store=order.store, order=order, currency=order.currency_code,
                                         amount=order.total, status="captured", provider="manual", is_deleted=False)
        when = datetime.fromisoformat(at).replace(tzinfo=ZoneInfo("Asia/Kolkata")) if at else order.created_at
        with mock.patch("django.utils.timezone.now", return_value=when):  # transactions can't be edited afterwards
            PaymentTransaction.objects.create(payment=payment, transaction_type="refund", amount=amount, status=status)

    def get(self, **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return self.ok("get", f"{self.URL}?{query}")

    def day(self, start, end=None, **extra):
        return self.get(**{"from": start, "to": end or start, "compare": "none", **extra})


class AnalyticsTest(AnalyticsBase):
    """GET /reports/analytics/: formulas, store timezone, returning customers and the comparison period."""

    def test_formulas(self):
        first = self.order("2026-10-02 10:00", fulfillment="fulfilled")
        self.order("2026-10-02 11:00", subtotal="50.00", discount="0", shipping="0", tax="0")
        self.order("2026-10-02 12:00", subtotal="999.00", status="cancelled")  # left out
        self.refund(first, "20.00")
        self.refund(first, "7.00", status="failed")  # only successful refunds count

        m = self.day("2026-10-02")["metrics"]

        self.assertEqual(Decimal(m["gross_sales"]["value"]), Decimal("150.00"))
        self.assertEqual(Decimal(m["discounts"]["value"]), Decimal("10.00"))
        self.assertEqual(Decimal(m["returns"]["value"]), Decimal("20.00"))
        self.assertEqual(Decimal(m["net_sales"]["value"]), Decimal("120.00"))  # 150 - 10 - 20
        self.assertEqual(Decimal(m["shipping_charges"]["value"]), Decimal("5.00"))
        self.assertEqual(Decimal(m["taxes"]["value"]), Decimal("9.00"))
        self.assertEqual(Decimal(m["total_sales"]["value"]), Decimal("134.00"))  # 120 + 5 + 9
        self.assertEqual((m["orders"]["value"], m["orders_fulfilled"]["value"]), (2, 1))
        self.assertEqual(Decimal(m["average_order_value"]["value"]), Decimal("77.00"))  # (150 - 10 + 5 + 9) / 2
        self.assertIsNone(m["orders"]["previous"])

    def test_an_order_at_2330_store_time_lands_on_that_local_day(self):
        self.order("2026-10-02 23:30")  # 18:00 UTC, still 2 Oct locally
        self.order("2026-10-03 00:30")  # 19:00 UTC on the 2nd, but 3 Oct locally

        data = self.day("2026-10-02", "2026-10-03")

        self.assertEqual(data["timezone"], "Asia/Kolkata")
        self.assertEqual({row["bucket"]: row["orders"] for row in data["series"]}, {"2026-10-02": 1, "2026-10-03": 1})
        self.assertEqual(self.day("2026-10-02")["metrics"]["orders"]["value"], 1)

    def test_one_day_is_hourly_in_store_time(self):
        self.order("2026-10-02 23:30")
        data = self.day("2026-10-02")
        self.assertEqual(data["interval"], "hour")
        self.assertEqual(len(data["series"]), 24)
        self.assertEqual(data["series"][23]["bucket"], "2026-10-02T23:00:00")
        self.assertEqual(data["series"][23]["orders"], 1)

    def test_returning_customer_rate_matches_guests_by_email(self):
        self.order("2026-09-01 10:00", email="Ann@example.com")  # before the period
        self.order("2026-10-02 10:00", email="ann@example.com")  # returning: a guest matched by email
        self.order("2026-10-02 11:00", email="bob@example.com")  # new
        self.order("2026-10-03 10:00", email="cy@example.com")   # new: first order ever is in the period...
        self.order("2026-10-04 10:00", email="cy@example.com")   # ...even though cy ordered twice in it

        m = self.day("2026-10-01", "2026-10-05")["metrics"]

        self.assertEqual(m["returning_customer_rate"]["value"], 33.3)  # only ann, of ann, bob, cy

    def test_a_refund_counts_on_the_day_it_was_made_in_store_time(self):
        order = self.order("2026-10-02 10:00")
        self.refund(order, "20.00", at="2026-10-04 00:30")  # 19:00 UTC on the 3rd, but the 4th in store time
        self.refund(order, "5.00", at="2026-10-02 12:00", status="failed")  # failed refunds never count

        two = self.day("2026-10-02", "2026-10-03")["metrics"]
        self.assertEqual(Decimal(two["returns"]["value"]), Decimal("0.00"))
        self.assertEqual(Decimal(two["net_sales"]["value"]), Decimal("90.00"))  # 100 - 10

        data = self.day("2026-10-02", "2026-10-04")
        by_day = {row["bucket"]: row for row in data["series"]}
        self.assertEqual(Decimal(by_day["2026-10-04"]["returns"]), Decimal("20.00"))
        self.assertEqual(Decimal(by_day["2026-10-04"]["total_sales"]), Decimal("-20.00"))  # a day with only a refund
        self.assertEqual(Decimal(by_day["2026-10-02"]["total_sales"]), Decimal("104.00"))  # 90 + 5 + 9
        self.assertEqual(Decimal(data["metrics"]["net_sales"]["value"]), Decimal("70.00"))

    def test_compare_to_previous_period_and_year(self):
        flat = {"discount": "0", "shipping": "0", "tax": "0"}
        self.order("2026-10-02 10:00", subtotal="200.00", **flat)
        self.order("2026-10-01 10:00", subtotal="100.00", **flat)  # the day before
        self.order("2025-10-02 10:00", subtotal="400.00", **flat)  # a year before

        data = self.get(**{"from": "2026-10-02", "to": "2026-10-02", "compare": "previous_period"})
        self.assertEqual((data["compare_from"], data["compare_to"]), ("2026-10-01", "2026-10-01"))
        self.assertEqual(Decimal(data["metrics"]["gross_sales"]["previous"]), Decimal("100.00"))
        self.assertEqual(data["metrics"]["gross_sales"]["change"], 100.0)
        self.assertEqual(len(data["comparison_series"]), len(data["series"]))

        data = self.get(**{"from": "2026-10-02", "to": "2026-10-02", "compare": "previous_year"})
        self.assertEqual(data["compare_from"], "2025-10-02")
        self.assertEqual(data["metrics"]["gross_sales"]["change"], -50.0)

        data = self.day("2026-10-02")
        self.assertIsNone(data["comparison_series"])
        self.assertIsNone(data["metrics"]["gross_sales"]["change"])

    def test_change_is_null_when_the_comparison_is_zero(self):
        self.order("2026-10-02 10:00")
        data = self.get(**{"from": "2026-10-02", "to": "2026-10-02"})
        self.assertIsNone(data["metrics"]["gross_sales"]["change"])

    def test_top_products_and_sales_channels(self):
        product = self.ok("post", "/api/v1/catalog/products/",
                          {"title": "Hat", "price": "10", "sku": "HAT-1"}, expect=201)["id"]
        order = self.order("2026-10-02 10:00")
        OrderItem.objects.create(order=order, product_id=product, product_name="Hat", quantity=3, unit_price="30.00",
                                 discount_amount=0, tax_amount=0, total="90.00", is_deleted=False)

        data = self.day("2026-10-02")

        self.assertEqual([(p["name"], p["units"], Decimal(p["sales"])) for p in data["top_products"]],
                         [("Hat", 3, Decimal("90.00"))])
        self.assertEqual([(c["channel"], Decimal(c["sales"])) for c in data["sales_by_channel"]],
                         [("No channel", Decimal("104.00"))])  # 100 - 10 + 5 + 9

    def test_one_currency_at_a_time_and_empty_range_has_no_data(self):
        self.order("2026-10-02 10:00", currency="USD")
        self.assertFalse(self.day("2026-10-02")["has_data"])
        self.assertTrue(self.day("2026-10-02", currency="usd")["has_data"])

    def test_bad_input_and_permission(self):
        for query in ("from=nope", "from=2026-10-05&to=2026-10-01", "compare=sideways", "from=2020-01-01&to=2026-01-01"):
            self.assertEqual(self.client.get(f"{self.URL}?{query}").status_code, 400, query)
        self.staff("support@example.com", "customer_support")  # has no reports.view
        self.assertEqual(self.client.get(self.URL).status_code, 403)

    def test_overview_follows_the_store_timezone(self):
        self.order("2026-10-03 00:30")  # 2 Oct in UTC, 3 Oct in store time
        data = self.ok("get", "/api/v1/reports/overview/?days=3650")
        self.assertEqual([str(day["date"]) for day in data["series"]], ["2026-10-03"])


class ReportPagesTest(AnalyticsBase):
    """GET /reports/ and /reports/<id>/: every report, totals, sorting, paging, CSV, filters, timezone, permission."""

    def setUp(self):
        super().setUp()
        self.product = self.ok("post", "/api/v1/catalog/products/",
                               {"title": "Hat", "price": "10", "sku": "HAT-1"}, expect=201)["id"]

    def item(self, order, total="90.00", units=3):
        OrderItem.objects.create(order=order, product_id=self.product, product_name="Hat", quantity=units,
                                 unit_price="30.00", discount_amount=0, tax_amount=0, total=total, is_deleted=False)

    def report(self, id, start="2026-10-02", end=None, **extra):
        query = "&".join(f"{k}={v}" for k, v in {"from": start, "to": end or start, "compare": "none", **extra}.items())
        return self.ok("get", f"/api/v1/reports/{id}/?{query}")

    def test_list_has_the_shopify_reports_by_category(self):
        listed = self.ok("get", "/api/v1/reports/")
        names = {r["category"]: [] for r in listed}
        for r in listed:
            names[r["category"]].append(r["name"])
        self.assertEqual(sorted(names), ["Acquisition", "Conversion", "Customers", "Finances", "Inventory", "Orders", "Sales"])
        self.assertEqual(len(names["Acquisition"]), 6)
        self.assertEqual(len(names["Conversion"]), 2)
        self.assertEqual(len(names["Sales"]), 8)
        self.assertEqual(len(names["Orders"]), 3)
        self.assertEqual(len(names["Customers"]), 4)
        self.assertEqual(len(names["Inventory"]), 3)
        self.assertEqual(len(names["Finances"]), 4)
        self.assertTrue(all(r["last_viewed"] is None for r in listed))

    def test_every_report_returns_columns_rows_totals_and_a_chart(self):
        self.order("2026-09-01 10:00", email="ann@example.com")
        first = self.order("2026-10-02 10:00", email="ann@example.com", fulfillment="fulfilled")
        self.order("2026-10-02 11:00", email="bob@example.com")
        self.item(first)
        self.refund(first, "10.00", at="2026-10-02 12:00")

        for r in self.ok("get", "/api/v1/reports/"):
            data = self.report(r["id"], "2026-10-01", "2026-10-03", compare="previous_period")
            self.assertEqual(data["id"], r["id"])
            self.assertTrue(data["columns"], r["id"])
            self.assertIsInstance(data["rows"], list, r["id"])
            self.assertIsInstance(data["totals"], dict, r["id"])
            self.assertEqual(data["row_count"], len(data["rows"]), r["id"])
            self.assertIn("points", data["chart"], r["id"])
            for row in data["rows"]:
                self.assertEqual(set(c["key"] for c in data["columns"]) - set(row), set(), r["id"])

    def test_sales_over_time_rows_totals_and_store_timezone(self):
        self.order("2026-10-02 23:30")  # 2 Oct in store time (18:00 UTC)
        self.order("2026-10-03 00:30")  # 3 Oct in store time, still the 2nd in UTC
        self.order("2026-10-03 09:00", status="cancelled")

        data = self.report("total-sales-over-time", "2026-10-02", "2026-10-03")

        self.assertEqual([(r["date"], r["orders"]) for r in data["rows"]], [("2026-10-02", 1), ("2026-10-03", 1)])
        self.assertEqual(data["totals"]["orders"], 2)
        self.assertEqual(Decimal(data["totals"]["total_sales"]), Decimal("208.00"))  # 2 x (100 - 10 + 5 + 9)
        self.assertEqual(data["timezone"], "Asia/Kolkata")
        self.assertEqual(data["kind"], "line")
        self.assertEqual(len(data["chart"]["points"]), 2)

    def test_compare_adds_previous_totals_and_a_dashed_series(self):
        self.order("2026-10-01 10:00")
        self.order("2026-10-02 10:00")
        data = self.report("total-sales-over-time", compare="previous_period")
        self.assertEqual(data["previous_totals"]["orders"], 1)
        self.assertEqual(len(data["chart"]["comparison"]), len(data["chart"]["points"]))
        self.assertIsNone(self.report("total-sales-over-time")["previous_totals"])

    def test_by_product_by_channel_and_by_customer_totals_match_their_rows(self):
        order = self.order("2026-10-02 10:00", email="ann@example.com")
        self.item(order)
        self.order("2026-10-02 11:00", email="bob@example.com")

        product = self.report("total-sales-by-product")
        self.assertEqual([(r["product"], r["units"], Decimal(r["net_sales"])) for r in product["rows"]],
                         [("Hat", 3, Decimal("90.00"))])
        self.assertEqual(Decimal(product["totals"]["net_sales"]), Decimal("90.00"))
        self.assertEqual(product["kind"], "bar")

        customers = self.report("total-sales-by-customer")
        self.assertEqual(sorted(r["email"] for r in customers["rows"]), ["ann@example.com", "bob@example.com"])
        self.assertEqual(Decimal(customers["totals"]["total_sales"]),
                         sum(Decimal(r["total_sales"]) for r in customers["rows"]))

        channel = self.report("total-sales-by-sales-channel")
        self.assertEqual([(r["channel"], r["orders"]) for r in channel["rows"]], [("No channel", 2)])

    def test_orders_by_status_keeps_cancelled_orders(self):
        self.order("2026-10-02 10:00")
        self.order("2026-10-02 11:00", status="cancelled")
        rows = self.report("orders-by-status")["rows"]
        self.assertEqual(sorted((r["status"], r["orders"]) for r in rows), [("cancelled", 1), ("open", 1)])

    def test_returns_over_time_uses_the_day_of_the_refund(self):
        order = self.order("2026-10-02 10:00")
        self.refund(order, "20.00", at="2026-10-04 00:30")  # the 4th in store time
        data = self.report("returns-over-time", "2026-10-02", "2026-10-04")
        self.assertEqual([(r["date"], Decimal(r["returns"])) for r in data["rows"]],
                         [("2026-10-02", Decimal("0.00")), ("2026-10-03", Decimal("0.00")), ("2026-10-04", Decimal("20.00"))])
        self.assertEqual(Decimal(data["totals"]["returns"]), Decimal("20.00"))

    def test_new_vs_returning_customers_over_time(self):
        self.order("2026-09-01 10:00", email="ann@example.com")
        self.order("2026-10-02 10:00", email="ann@example.com")  # returning
        self.order("2026-10-02 11:00", email="bob@example.com")  # new
        self.order("2026-10-03 11:00", email="bob@example.com")  # bob orders again: no longer new that day, still new overall
        data = self.report("new-vs-returning-customers-over-time", "2026-10-02", "2026-10-03")
        self.assertEqual([(r["new_customers"], r["returning_customers"]) for r in data["rows"]], [(1, 1), (0, 1)])
        self.assertEqual((data["totals"]["new_customers"], data["totals"]["returning_customers"]), (1, 1))

    def test_first_time_vs_returning_sales_and_the_customer_type_filter(self):
        self.order("2026-09-01 10:00", email="ann@example.com")
        self.order("2026-10-02 10:00", email="ann@example.com")
        self.order("2026-10-02 11:00", email="bob@example.com")
        self.order("2026-10-02 12:00", email="bob@example.com")  # two orders inside the period: still new

        rows = {r["customer_type"]: r for r in self.report("first-time-vs-returning-customer-sales")["rows"]}
        self.assertEqual((rows["First-time"]["customers"], rows["First-time"]["orders"]), (1, 2))
        self.assertEqual((rows["Returning"]["customers"], rows["Returning"]["orders"]), (1, 1))

        self.assertEqual(self.report("total-sales-over-time", customer_type="returning")["totals"]["orders"], 1)
        self.assertEqual(self.report("total-sales-over-time", customer_type="new")["totals"]["orders"], 2)

    def test_product_filter_keeps_orders_with_that_product(self):
        self.item(self.order("2026-10-02 10:00"))
        self.order("2026-10-02 11:00")
        self.assertEqual(self.report("total-sales-over-time", product=self.product)["totals"]["orders"], 1)
        self.assertEqual(self.report("total-sales-over-time")["totals"]["orders"], 2)

    def test_cohorts_group_new_customers_by_first_order_month(self):
        self.order("2026-09-10 10:00", email="ann@example.com")
        self.order("2026-10-05 10:00", email="ann@example.com")
        self.order("2026-09-12 10:00", email="bob@example.com")
        rows = self.report("customer-cohort-analysis", "2026-09-01", "2026-10-31")["rows"]
        self.assertEqual([(r["cohort"], r["customers"], r["month_0"], r["month_1"]) for r in rows],
                         [("2026-09", 2, 100.0, 50.0)])

    def test_inventory_reports_list_the_variants(self):
        data = self.report("inventory-remaining")
        self.assertEqual([r["product"] for r in data["rows"]], ["Hat"])
        self.assertEqual(self.report("days-of-inventory-remaining")["rows"][0]["days_remaining"], None)  # nothing sold
        self.item(self.order("2026-10-02 10:00"))
        self.assertEqual(self.report("products-by-sell-through-rate")["rows"][0]["units"], 3)

    def test_payments_by_method(self):
        order = self.order("2026-10-02 10:00")
        Payment.objects.create(store=self.shop, order=order, currency="INR", amount="104.00", status="captured",
                               provider="stripe", is_deleted=False)
        Payment.objects.update(created_at=order.created_at)
        rows = self.report("payments-by-method")["rows"]
        self.assertEqual([(r["method"], r["payments"], Decimal(r["captured"])) for r in rows],
                         [("stripe", 1, Decimal("104.00"))])

    def test_sort_and_pagination(self):
        data = self.report("total-sales-over-time", "2026-08-01", "2026-09-29")  # 60 days
        self.assertEqual((data["row_count"], len(data["rows"]), data["page"], data["page_size"]), (60, 50, 1, 50))
        self.assertEqual(len(self.report("total-sales-over-time", "2026-08-01", "2026-09-29", page=2)["rows"]), 10)
        desc = self.report("total-sales-over-time", "2026-08-01", "2026-09-29", sort="-date")
        self.assertEqual(desc["rows"][0]["date"], "2026-09-29")

        self.order("2026-10-02 10:00", email="ann@example.com")
        self.order("2026-10-02 11:00", email="bob@example.com", subtotal="500.00")
        by_sales = self.report("total-sales-by-customer", sort="-total_sales")["rows"]
        self.assertEqual(by_sales[0]["email"], "bob@example.com")

    def test_csv_export_has_a_header_and_every_row_not_just_the_page(self):
        response = self.client.get("/api/v1/reports/total-sales-over-time/"
                                   "?from=2026-08-01&to=2026-09-29&compare=none&format=csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", response["Content-Disposition"])
        lines = b"".join(response.streaming_content).decode("utf-8-sig").splitlines()
        self.assertEqual(len(lines), 61)  # header + all 60 days, though a page holds 50
        self.assertTrue(lines[0].startswith("Date,Orders,Gross sales"))
        self.assertTrue(lines[1].startswith("2026-08-01,"))

    def test_csv_respects_filters_and_neutralises_formulas(self):
        order = self.order("2026-10-02 10:00")
        OrderItem.objects.create(order=order, product_id=self.product, product_name="=SUM(1)", quantity=1,
                                 unit_price="5.00", discount_amount=0, tax_amount=0, total="5.00", is_deleted=False)
        response = self.client.get("/api/v1/reports/total-sales-by-product/?from=2026-10-02&to=2026-10-02&format=csv")
        lines = b"".join(response.streaming_content).decode("utf-8-sig").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("'=SUM(1),"))

    def test_bad_input_and_permission(self):
        for query in ("sort=nope", "product=nope", "customer_type=sideways", "page=x", "from=2026-10-05&to=2026-10-01"):
            self.assertEqual(self.client.get(f"/api/v1/reports/total-sales-over-time/?{query}").status_code, 400, query)
        self.assertEqual(self.client.get("/api/v1/reports/payments-by-method/?channel=" + "1" * 32).status_code, 400)
        self.staff("support@example.com", "customer_support")  # has no reports.view
        self.assertEqual(self.client.get("/api/v1/reports/total-sales-over-time/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/reports/total-sales-over-time/?format=csv").status_code, 403)


# --- Storefront visit tracking and the session reports -----------------------------------------

DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
TABLET = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
IST = ZoneInfo("Asia/Kolkata")


class TrackingBase(RbacTestBase):
    """A store on Asia/Kolkata time, an anonymous shopper client and a helper that sends beacons at a store time."""

    def setUp(self):
        super().setUp()
        cache.clear()
        Store.objects.filter(pk=self.store_id).update(timezone="Asia/Kolkata", currency="INR")
        self.shop = Store.objects.get(pk=self.store_id)
        self.shopper = Client()
        self.orders = 0

    def at(self, local):
        return datetime.fromisoformat(local).replace(tzinfo=IST)

    def beacon(self, local, visitor="visitor-000001", path="/", store=None, ua=DESKTOP, origin="null", **extra):
        """POST the storefront beacon as the page's script does, with the clock at `local` (store time; None: now)."""
        store = store or self.shop
        body = {"visitor_id": visitor, "path": f"/s/{store.store_slug}{path}", **{k: v for k, v in extra.items()
                                                                             if not k.startswith("HTTP_")}}
        headers = {k: v for k, v in extra.items() if k.startswith("HTTP_")}
        with (mock.patch("django.utils.timezone.now", return_value=self.at(local)) if local else contextlib.nullcontext()):
            return self.shopper.post(
                f"/s/{store.store_slug}/_track", data=json.dumps(body), content_type="text/plain;charset=UTF-8",
                HTTP_USER_AGENT=ua, HTTP_ORIGIN=origin, **headers)

    def other_store(self):
        _, store = self.sign_up("other@example.com", "Other shop")
        self.as_owner()
        Store.objects.filter(pk=store["storeId"]).update(timezone="Asia/Kolkata")
        return Store.objects.get(pk=store["storeId"])

    def sessions(self, store=None):
        return StorefrontSession.objects.filter(store=store or self.shop).order_by("started_at")

    def order(self, store=None):
        self.orders += 1
        return Order.objects.create(
            store=store or self.shop, order_number=str(self.orders), currency_code="INR", subtotal="100.00",
            discount_amount=0, tax_amount=0, shipping_amount=0, total="100.00", status="open",
            fulfillment_status="unfulfilled")

    def visit(self, local, visitor, cart=None, added=False, checkout=False, ordered=False, **kwargs):
        """One tracked session for `visitor` that got as far as the funnel flags say."""
        cart = cart or hashlib.md5(visitor.encode()).hexdigest()
        self.assertEqual(self.beacon(local, visitor=visitor, cart_session=cart, **kwargs).status_code, 204)
        now = self.at(local)
        if added or checkout or ordered:
            order_services.mark_cart_event(self.shop, cart, added_to_cart=added, reached_checkout=checkout, now=now)
        if ordered:
            order_services.mark_cart_event(self.shop, cart, order=self.order(), now=now)

    def report(self, id, start, end=None, **extra):
        query = "&".join(f"{k}={v}" for k, v in {"from": start, "to": end or start, "compare": "none", **extra}.items())
        return self.ok("get", f"/api/v1/reports/{id}/?{query}")

    def analytics(self, start, end=None, **extra):
        query = "&".join(f"{k}={v}" for k, v in {"from": start, "to": end or start, "compare": "none", **extra}.items())
        return self.ok("get", f"/api/v1/reports/analytics/?{query}")


class CollectingTest(TrackingBase):
    def test_a_beacon_starts_a_session_with_where_it_came_from(self):
        response = self.beacon(
            "2026-10-02 10:00", path="/products/3f2b8c1e-0000-4000-8000-000000000001", ua=MOBILE,
            referrer="https://www.google.com/search?q=hats", utm_source="newsletter", utm_medium="email",
            utm_campaign="autumn", HTTP_CF_IPCOUNTRY="in")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(response["X-Visit-Tracking"], "counted")
        self.assertEqual(self.beacon("2026-10-02 10:01", ua="curl/8").headers["X-Visit-Tracking"], "ignored")

        s = self.sessions().get()
        self.assertEqual((s.landing_path, s.landing_page_type), ("/products/3f2b8c1e-0000-4000-8000-000000000001", "product"))
        self.assertEqual((s.referrer_host, s.referrer_type), ("google.com", "email"))  # the campaign's medium wins
        self.assertEqual((s.utm_source, s.utm_medium, s.utm_campaign), ("newsletter", "email", "autumn"))
        self.assertEqual((s.device_type, s.country, s.page_views), ("mobile", "IN", 1))
        self.assertEqual((s.started_at, s.last_seen_at), (self.at("2026-10-02 10:00"),) * 2)
        self.assertFalse(s.added_to_cart or s.reached_checkout or s.order_id)
        self.assertFalse({f.name for f in StorefrontSession._meta.fields if "ip" in f.name.split("_")})  # no addresses

    def test_page_types_referrer_types_and_devices(self):
        for path, kind in (("/", "home"), ("", "home"), ("/products/x", "product"), ("/collections/all", "collection"),
                           ("/cart", "cart"), ("/checkout/order/1/", "checkout"), ("/pages/about", "other")):
            self.assertEqual(order_services.page_type(path), kind, path)
        for host, medium, kind in (("", "", "direct"), ("google.co.in", "", "search"), ("bing.com", "", "search"),
                                   ("l.facebook.com", "", "social"), ("t.co", "", "social"), ("instagram.com", "", "social"),
                                   ("mail.google.com", "", "email"), ("blog.example.org", "", "other"),
                                   ("", "email", "email"), ("", "cpc", "search"), ("", "social", "social")):
            self.assertEqual(order_services.referrer_type(host, medium), kind, (host, medium))
        self.assertEqual(order_services.referrer_host("https://www.Google.com/x", own_host="shop.example.com"), "google.com")
        self.assertEqual(order_services.referrer_host("https://shop.example.com/products", own_host="shop.example.com:8000"), "")
        self.assertEqual((order_services.device_type(DESKTOP), order_services.device_type(MOBILE), order_services.device_type(TABLET)),
                         ("desktop", "mobile", "tablet"))

    def test_a_visit_goes_on_for_30_minutes_after_the_last_page(self):
        self.beacon("2026-10-02 10:00")
        self.beacon("2026-10-02 10:29")  # joins the visit
        self.beacon("2026-10-02 10:59")  # exactly 30 minutes after the last page: still the same visit
        s = self.sessions().get()
        self.assertEqual(s.page_views, 3)
        self.assertEqual(s.last_seen_at, self.at("2026-10-02 10:59"))
        self.assertEqual(s.started_at, self.at("2026-10-02 10:00"))

        self.beacon("2026-10-02 11:30")  # 31 minutes of silence: a new visit
        first, second = self.sessions()
        self.assertEqual((first.page_views, second.page_views), (3, 1))
        self.assertEqual(second.started_at, self.at("2026-10-02 11:30"))

    def test_a_new_day_in_the_store_timezone_starts_a_new_visit(self):
        # 23:50 and 00:10 IST are 18:20 and 18:40 UTC: the same UTC day, but two days in the store
        self.beacon("2026-10-02 23:50")
        self.beacon("2026-10-03 00:10")
        self.assertEqual(self.sessions().count(), 2)

        self.beacon("2026-10-03 00:25", visitor="visitor-000002")  # a different visitor, same day: untouched
        self.beacon("2026-10-03 00:40", visitor="visitor-000002")
        self.assertEqual(self.sessions().filter(visitor_id="visitor-000002").get().page_views, 2)

    def test_visitors_have_their_own_visits(self):
        self.beacon("2026-10-02 10:00", visitor="visitor-000001")
        self.beacon("2026-10-02 10:01", visitor="visitor-000002")
        self.beacon("2026-10-02 10:02", visitor="visitor-000001")
        self.assertEqual(sorted(self.sessions().values_list("page_views", flat=True)), [1, 2])

    def test_bots_and_requests_without_a_user_agent_are_ignored(self):
        for ua in ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "curl/8.4.0",
                   "Mozilla/5.0 HeadlessChrome/120.0", "facebookexternalhit/1.1", "python-requests/2.31", "",
                   "Mozilla/5.0 (compatible; bingbot/2.0)", "UptimeRobot/2.0"):
            response = self.beacon("2026-10-02 10:00", ua=ua)
            self.assertEqual(response.status_code, 204, ua)
        self.assertEqual(self.sessions().count(), 0)

    def test_the_editor_and_store_staff_previews_do_not_load_the_script(self):
        request = RequestFactory().get("/")
        request.session = {}
        self.assertIn("/_track", render_page(self.shop, "index", request=request))
        self.assertNotIn("/_track", render_page(self.shop, "index", request=request, editor=True))  # the customizer
        staff = RequestFactory().get("/")
        staff.session = {}
        staff._no_track = True  # a store member who opened the store from the admin (View store, Open theme)
        self.assertNotIn("/_track", render_page(self.shop, "index", request=staff))

        page = self.shopper.get(f"/s/{self.shop.store_slug}/").content.decode()
        self.assertIn(f'sendBeacon("/s/{self.shop.store_slug}/_track"', page)
        self.assertRegex(page, r'data-cart-session="[0-9a-f]{32}"')

    def remove_the_cart_from_the_store(self):
        """The owner takes the cart icon out of the header and the product cards: no page has the cart any more."""
        values = self.ok("get", "/api/v1/themes/sections/")["header"]["values"]
        values["blocks"] = [b for b in values["blocks"] if b["type"] != "cart"]
        self.ok("patch", "/api/v1/themes/settings/", {"sections": {"header": values}, "client": "test"})
        index = self.ok("get", "/api/v1/themes/settings/")["templates"]["index"]
        for section in index["sections"].values():
            for block in section.get("blocks", []):
                if block["type"] == "cart_icon":
                    block["hidden"] = True
        self.ok("patch", "/api/v1/themes/settings/",
                {"templates": {"index": {"order": index["order"], "sections": index["sections"]}}})

    def test_a_page_with_the_cart_gets_the_cookie_and_is_tracked_with_its_cart(self):
        cookie = f"tss_cart_{self.shop.store_slug}"
        reply = Client().get(f"/s/{self.shop.store_slug}/")
        page = reply.content.decode()
        cart = reply.cookies[cookie].value
        self.assertRegex(cart, r"^[0-9a-f]{32}$")
        self.assertIn(f'data-cart-session="{cart}"', page)
        self.assertIn(f'data-visit-cart="{cart}"', page)  # the tracking script is given the same id
        self.assertIn(f'sendBeacon("/s/{self.shop.store_slug}/_track"', page)

        self.assertEqual(self.beacon("2026-10-02 10:00", cart_session=cart).headers["X-Visit-Tracking"], "counted")
        self.assertEqual(self.sessions().get().cart_session, cart)

    def test_a_page_without_the_cart_gets_no_cookie_and_no_throwaway_id(self):
        self.remove_the_cart_from_the_store()
        cookie = f"tss_cart_{self.shop.store_slug}"
        home = f"/s/{self.shop.store_slug}/"

        for _ in range(2):  # a new id on every load would be a new visitor on every load
            reply = Client().get(home)
            page = reply.content.decode()
            self.assertNotIn(cookie, reply.cookies)
            self.assertNotIn("data-cart-session=", page)
            self.assertIn('data-visit-cart=""', page)  # the script still counts the visit by its own visitor id
            script = re.search(r"<script data-visit-beacon.*?</script>", page, re.S).group(0)
            self.assertNotRegex(script, r"[0-9a-f]{32}")
            self.assertIn(f'sendBeacon("/s/{self.shop.store_slug}/_track"', page)

        # a browser that already has a cart keeps it: the cookie is not replaced and the script is given that id
        returning = Client()
        returning.cookies[cookie] = "a" * 32
        reply = returning.get(home)
        self.assertNotIn(cookie, reply.cookies)
        self.assertIn(f'data-visit-cart="{"a" * 32}"', reply.content.decode())

        # a visit with no cart id and no visitor id (storage blocked) cannot be told from any other: it is not counted
        self.assertEqual(self.beacon("2026-10-02 10:00", visitor="").headers["X-Visit-Tracking"], "ignored")
        self.assertEqual(self.sessions().count(), 0)
        self.assertEqual(self.beacon("2026-10-02 10:00").headers["X-Visit-Tracking"], "counted")  # one with a visitor id is

    def test_other_sites_and_junk_are_refused_or_dropped_without_an_error(self):
        self.assertEqual(self.beacon("2026-10-02 10:00", origin="https://evil.example").status_code, 403)
        self.assertEqual(self.beacon("2026-10-02 10:00", origin=f"http://testserver").status_code, 204)  # the store's own site
        self.assertEqual(self.sessions().count(), 1)

        url = f"/s/{self.shop.store_slug}/_track"
        for body in ("not json", "[1, 2]", json.dumps({"path": "/"}), json.dumps({"visitor_id": "x", "path": "/"}),
                     json.dumps({"visitor_id": "bad id with spaces!!", "path": "/"}), "x" * 5000):
            response = self.shopper.post(url, data=body, content_type="text/plain", HTTP_USER_AGENT=DESKTOP, HTTP_ORIGIN="null")
            self.assertEqual(response.status_code, 204, body[:20])
        self.assertEqual(self.sessions().count(), 1)
        self.assertEqual(self.shopper.get(url).status_code, 405)
        self.assertEqual(self.shopper.options(url, HTTP_ORIGIN="null").status_code, 204)
        self.assertEqual(self.shopper.post("/s/no-such-store/_track", data="{}", content_type="text/plain",
                                           HTTP_USER_AGENT=DESKTOP, HTTP_ORIGIN="null").status_code, 204)

    def test_the_storefront_rate_limit_applies(self):
        responses = [self.beacon("2026-10-02 10:00").status_code for _ in range(125)]
        self.assertEqual(responses[:120], [204] * 120)
        self.assertEqual(responses[120:], [429] * 5)

    def test_the_whole_shopper_journey_marks_the_visit(self):
        self.location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Main"}, expect=201)["id"]
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Lamp", "sku": "LAMP-1", "status": "active", "price": "49.99", "location": self.location,
            "quantity": 10}, expect=201)
        base = f"/s/{self.shop.store_slug}"
        session_id = re.search(r'data-cart-session="([0-9a-f]{32})"', self.shopper.get(f"{base}/").content.decode()).group(1)

        self.beacon(None, cart_session=session_id)
        s = self.sessions().get()
        self.assertEqual((s.cart_session, s.added_to_cart, s.reached_checkout, s.order_id), (session_id, False, False, None))

        self.shopper.get(f"{base}/checkout?cart={session_id}")  # an empty cart is not "reaching checkout"
        s.refresh_from_db()
        self.assertFalse(s.reached_checkout)

        add = self.shopper.post(f"{base}/cart/add", data=json.dumps({"product": product["id"], "quantity": 1}),
                                content_type="application/json", HTTP_X_CART_SESSION=session_id)
        self.assertEqual(add.status_code, 200)
        s.refresh_from_db()
        self.assertEqual((s.added_to_cart, s.reached_checkout), (True, False))

        self.shopper.get(f"{base}/checkout?cart={session_id}")
        s.refresh_from_db()
        self.assertEqual((s.added_to_cart, s.reached_checkout, s.order_id), (True, True, None))

        placed = self.shopper.post(
            f"{base}/checkout/", content_type="application/json", HTTP_ACCEPT="application/json",
            HTTP_X_CART_SESSION=session_id, data=json.dumps({
                "email": "eleanor@example.com", "first_name": "Eleanor", "last_name": "Vance", "address1": "42 Hill Lane",
                "city": "Boston", "postal_code": "02101", "country": "India", "payment_method": "cod"}))
        self.assertEqual(placed.status_code, 200, placed.content[:300])
        s.refresh_from_db()
        self.assertEqual(str(s.order_id), placed.json()["order_id"])

    def test_cart_events_need_a_tracked_visit_and_the_right_store(self):
        cart = "c" * 32  # a cart id is 32 hex characters
        order_services.mark_cart_event(self.shop, cart, added_to_cart=True)  # nothing tracked: nothing happens
        self.assertEqual(self.sessions().count(), 0)

        other = self.other_store()
        self.beacon("2026-10-02 10:00", cart_session=cart, store=other)
        self.beacon("2026-10-02 10:00", cart_session=cart)
        order_services.mark_cart_event(self.shop, cart, added_to_cart=True, now=self.at("2026-10-02 10:05"))
        self.assertEqual(self.sessions().get().added_to_cart, True)
        self.assertEqual(self.sessions(other).get().added_to_cart, False)  # same cart id, other store

        # an order implies the steps before it; it belongs to the visit once
        order = self.order()
        order_services.mark_cart_event(self.shop, cart, order=order, now=self.at("2026-10-02 10:06"))
        order_services.mark_cart_event(self.shop, cart, order=self.order(), now=self.at("2026-10-02 10:07"))
        s = self.sessions().get()
        self.assertEqual((s.added_to_cart, s.reached_checkout, s.order_id), (True, True, order.pk))


class SessionReportsTest(TrackingBase):
    def seed(self):
        """2 Oct: 5 sessions (4 visitors), one added to cart, one reached checkout, one ordered."""
        self.visit("2026-10-02 09:00", "visitor-000001", referrer="https://www.google.com/", path="/products/p1")
        self.visit("2026-10-02 10:00", "visitor-000002", referrer="https://facebook.com/", added=True, path="/products/p1")
        self.visit("2026-10-02 11:00", "visitor-000003", checkout=True, ua=MOBILE, HTTP_CF_IPCOUNTRY="IN")
        self.visit("2026-10-02 12:00", "visitor-000004", ordered=True, ua=MOBILE, HTTP_CF_IPCOUNTRY="US", path="/collections/all")
        self.visit("2026-10-02 15:00", "visitor-000001", ua=TABLET, path="/products/p1")  # visitor 1 again, after the 30 minutes: 5th session

    def test_funnel_counts_never_grow_down_the_steps(self):
        self.seed()
        data = self.analytics("2026-10-02")
        steps = {step["key"]: step for step in data["funnel"]}
        self.assertEqual([s["label"] for s in data["funnel"]], ["Sessions", "Added to cart", "Reached checkout", "Completed"])
        self.assertEqual([s["value"] for s in data["funnel"]], [5, 3, 2, 1])  # checkout and order imply the cart
        self.assertEqual(steps["sessions_completed"]["rate"], 20.0)
        self.assertEqual(data["metrics"]["sessions"]["value"], 5)
        self.assertEqual(data["metrics"]["visitors"]["value"], 4)
        self.assertTrue(data["has_sessions"])

        rows = self.report("conversion-rate-breakdown", "2026-10-02")["rows"]
        self.assertEqual([(r["step"], r["sessions"], r["share"]) for r in rows],
                         [("Sessions", 5, 100.0), ("Added to cart", 3, 60.0), ("Reached checkout", 2, 40.0), ("Completed", 1, 20.0)])

    def test_conversion_rate_is_sessions_with_an_order_over_sessions(self):
        self.seed()
        data = self.analytics("2026-10-02")
        self.assertEqual(data["metrics"]["conversion_rate"]["value"], 20.0)
        hours = {r["bucket"][11:13]: r["conversion_rate"] for r in data["series"]}  # one day = hourly buckets
        self.assertEqual((hours["12"], hours["09"], hours["03"]), (100.0, 0.0, 0.0))

        report = self.report("conversion-rate-over-time", "2026-10-02", "2026-10-03")
        self.assertEqual([r["conversion_rate"] for r in report["rows"]], [20.0, 0.0])  # no sessions on 3 Oct: 0, not an error
        self.assertEqual(report["totals"]["conversion_rate"], 20.0)  # 1 order in 5 sessions over the two days
        self.assertEqual(report["chart"]["value_type"], "percent")

    def test_compare_period_has_its_own_funnel_and_change(self):
        self.seed()
        self.visit("2026-10-01 10:00", "visitor-000009")
        data = self.analytics("2026-10-02", compare="previous_period")
        self.assertEqual(data["funnel"][0]["previous"], 1)
        self.assertEqual(data["funnel"][0]["change"], 400.0)
        self.assertEqual(data["metrics"]["sessions"]["previous"], 1)
        self.assertEqual(data["comparison_series"][10]["sessions"], 1)

    def test_sessions_and_visitors_over_time(self):
        self.seed()
        self.visit("2026-10-03 10:00", "visitor-000001")
        sessions = self.report("sessions-over-time", "2026-10-02", "2026-10-03")
        self.assertEqual([(r["date"], r["sessions"], r["visitors"], r["page_views"]) for r in sessions["rows"]],
                         [("2026-10-02", 5, 4, 5), ("2026-10-03", 1, 1, 1)])
        self.assertEqual((sessions["totals"]["sessions"], sessions["totals"]["visitors"]), (6, 4))  # visitor 1 counted once
        visitors = self.report("visitors-over-time", "2026-10-02", "2026-10-03")
        self.assertEqual([r["visitors"] for r in visitors["rows"]], [4, 1])
        self.assertEqual(visitors["totals"]["visitors"], 4)

    def test_sessions_by_landing_page_referrer_device_and_location(self):
        self.seed()
        landing = self.report("sessions-by-landing-page", "2026-10-02")
        pages = {r["landing_page"]: r for r in landing["rows"]}
        self.assertEqual(landing["rows"][0]["landing_page"], "/products/p1")  # sorted by sessions
        self.assertEqual((pages["/products/p1"]["page_type"], pages["/products/p1"]["sessions"]), ("product", 3))
        self.assertEqual((pages["/"]["page_type"], pages["/"]["sessions"]), ("home", 1))
        self.assertEqual(landing["totals"]["sessions"], 5)
        self.assertEqual(pages["/collections/all"]["conversion_rate"], 100.0)  # the one landing on it ordered
        self.assertEqual(pages["/products/p1"]["conversion_rate"], 0.0)

        referrers = {r["referrer"]: r for r in self.report("sessions-by-referrer", "2026-10-02")["rows"]}
        self.assertEqual((referrers["google.com"]["referrer_type"], referrers["google.com"]["sessions"]), ("search", 1))
        self.assertEqual(referrers["facebook.com"]["referrer_type"], "social")
        self.assertEqual((referrers["Direct"]["referrer_type"], referrers["Direct"]["sessions"]), ("direct", 3))

        devices = {r["device_type"]: r["sessions"] for r in self.report("sessions-by-device-type", "2026-10-02")["rows"]}
        self.assertEqual(devices, {"desktop": 2, "mobile": 2, "tablet": 1})

        places = {r["country"]: r["sessions"] for r in self.report("sessions-by-location", "2026-10-02")["rows"]}
        self.assertEqual(places, {"Unknown": 3, "IN": 1, "US": 1})  # no header, no country

    def test_days_follow_the_store_timezone(self):
        self.visit("2026-10-03 00:30", "visitor-000001")  # 19:00 UTC on 2 Oct, but 3 Oct in the store
        self.assertEqual(self.analytics("2026-10-02")["metrics"]["sessions"]["value"], 0)
        self.assertEqual(self.analytics("2026-10-03")["metrics"]["sessions"]["value"], 1)
        rows = self.report("sessions-over-time", "2026-10-02", "2026-10-03")["rows"]
        self.assertEqual([r["sessions"] for r in rows], [0, 1])

    def test_one_store_never_sees_another_stores_sessions(self):
        self.seed()
        other = self.other_store()
        self.beacon("2026-10-02 10:00", store=other, visitor="visitor-000077", referrer="https://bing.com/")
        self.beacon("2026-10-02 10:00", store=other, visitor="visitor-000078")
        order_services.mark_cart_event(other, "z" * 32, order=self.order(other))  # no such cart in the other store

        mine = self.analytics("2026-10-02")
        self.assertEqual(mine["metrics"]["sessions"]["value"], 5)
        self.assertEqual("bing.com" in {r["referrer"] for r in mine["sessions_by_referrer"]}, False)
        self.assertEqual(sum(r["sessions"] for r in self.report("sessions-by-referrer", "2026-10-02")["rows"]), 5)

    def test_landing_pages_and_referrers_on_the_dashboard_and_nothing_to_show(self):
        self.assertFalse(self.analytics("2026-10-02")["has_sessions"])
        self.seed()
        data = self.analytics("2026-10-02")
        self.assertEqual(data["sessions_by_landing_page"][0], {"page": "/products/p1", "page_type": "product", "sessions": 3})
        self.assertEqual(data["sessions_by_referrer"][0], {"referrer": "Direct", "type": "direct", "sessions": 3})

    def test_session_reports_are_listed_and_need_the_reports_permission(self):
        listed = {r["id"]: r["category"] for r in self.ok("get", "/api/v1/reports/")}
        for id in ("sessions-over-time", "sessions-by-landing-page", "sessions-by-referrer", "sessions-by-device-type",
                   "sessions-by-location", "visitors-over-time", "conversion-rate-over-time", "conversion-rate-breakdown"):
            self.assertIn(id, listed)
        self.staff("support@example.com", "customer_support")
        self.assertEqual(self.client.get("/api/v1/reports/sessions-over-time/").status_code, 403)

    def test_database_constraints(self):
        from django.db import IntegrityError, transaction
        self.beacon("2026-10-02 10:00")
        s = self.sessions().get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StorefrontSession.objects.filter(pk=s.pk).update(page_views=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            StorefrontSession.objects.filter(pk=s.pk).update(last_seen_at=self.at("2026-10-02 09:00"))


class LiveViewTest(TrackingBase):
    """GET /reports/live/: the 5 and 10 minute windows, since-midnight in the store timezone, isolation, permission."""

    NOW = "2026-10-02 12:00"

    def live(self, now=None):
        with mock.patch("django.utils.timezone.now", return_value=self.at(now or self.NOW)):
            return self.ok("get", "/api/v1/reports/live/")

    def placed(self, local, email=None, total="100.00", store=None):
        order = self.order(store)
        Order.objects.filter(pk=order.pk).update(created_at=self.at(local), customer_email=email, total=total)
        return order

    def test_visitors_right_now_are_distinct_visitors_seen_in_the_last_5_minutes(self):
        self.visit("2026-10-02 11:56", "visitor-000001")
        self.visit("2026-10-02 11:57", "visitor-000002")
        self.visit("2026-10-02 11:50", "visitor-000003")  # last seen 10 minutes ago
        StorefrontSession.objects.filter(store=self.shop, visitor_id="visitor-000001").update(
            started_at=self.at("2026-10-02 11:20"))
        StorefrontSession.objects.create(  # a second session of visitor 1: still one visitor
            store=self.shop, visitor_id="visitor-000001", started_at=self.at("2026-10-02 11:58"),
            last_seen_at=self.at("2026-10-02 11:59"))
        data = self.live()
        self.assertEqual(data["visitors_now"], 2)
        self.assertEqual(data["total_sessions"], 4)

    def test_behaviour_counts_sessions_seen_in_the_last_10_minutes(self):
        self.visit("2026-10-02 11:55", "visitor-000001", added=True)
        self.visit("2026-10-02 11:52", "visitor-000002", checkout=True)
        self.visit("2026-10-02 11:51", "visitor-000003", ordered=True)
        self.visit("2026-10-02 11:40", "visitor-000004", ordered=True)  # too old
        b = self.live()["behaviour"]
        self.assertEqual((b["active_carts"], b["checking_out"], b["purchased"]), (3, 2, 1))

    def test_today_starts_at_midnight_in_the_store_timezone(self):
        self.placed("2026-10-01 23:59", email="a@example.com")  # yesterday, store time
        self.placed("2026-10-02 00:01", email="a@example.com")
        self.placed("2026-10-02 09:00", email="b@example.com", total="50.00")
        cancelled = self.placed("2026-10-02 10:00", email="c@example.com")
        Order.objects.filter(pk=cancelled.pk).update(status="cancelled")
        self.visit("2026-10-01 23:50", "visitor-000001")  # session started yesterday
        self.visit("2026-10-02 00:10", "visitor-000002", HTTP_CF_IPCOUNTRY="IN")
        self.visit("2026-10-02 11:55", "visitor-000003", HTTP_CF_IPCOUNTRY="IN")
        self.visit("2026-10-02 11:56", "visitor-000004")

        data = self.live()
        self.assertEqual(data["timezone"], "Asia/Kolkata")
        self.assertEqual((data["total_orders"], data["total_sessions"]), (2, 3))
        self.assertEqual(Decimal(data["total_sales"]), Decimal("200.00"))  # 2 orders, 100 each, cancelled one left out
        self.assertEqual(data["top_locations"], [{"country": "IN", "sessions": 2}, {"country": "Unknown", "sessions": 1}])
        self.assertEqual(data["customers"], {"first_time": 1, "returning": 1})  # a@ ordered yesterday
        self.assertEqual(len(data["top_products"]), 0)  # orders here have no items

    def test_a_store_never_sees_another_stores_figures(self):
        self.visit("2026-10-02 11:58", "visitor-000001", added=True)
        self.placed("2026-10-02 11:00", email="a@example.com")
        other = self.other_store()
        self.beacon("2026-10-02 11:59", store=other, visitor="visitor-000077", cart_session="z" * 32, HTTP_CF_IPCOUNTRY="US")
        self.placed("2026-10-02 11:30", email="x@example.com", store=other)
        data = self.live()
        self.assertEqual((data["visitors_now"], data["total_sessions"], data["total_orders"]), (1, 1, 1))
        self.assertEqual(data["top_locations"], [{"country": "Unknown", "sessions": 1}])
        self.assertEqual(data["behaviour"]["active_carts"], 1)

    def test_needs_the_reports_permission(self):
        self.staff("support@example.com", "customer_support")
        self.assertEqual(self.client.get("/api/v1/reports/live/").status_code, 403)
