"""
Reports and overview endpoints.

    python manage.py test apps.core.tests.test_reports --settings=config.test_settings
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from celery.exceptions import Retry

from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

from apps.core import reports, tasks
from apps.customers.models import Customer
from apps.orders.models import BackgroundJob, Order, OrderItem, StoreDailyMetric
from apps.payments.models import Payment, PaymentTransaction
from apps.tenancy.models import Store

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

    def test_every_built_in_report_has_a_detail_endpoint(self):
        store = Store.objects.get(pk=self.store_id)
        Order.objects.create(
            store=store, order_number="1", currency_code="INR", subtotal="30.00", discount_amount=0,
            tax_amount=0, shipping_amount=0, total="30.00", status="open", fulfillment_status="unfulfilled")
        for report in self.ok("get", "/api/v1/reports/"):
            data = self.ok("get", f"/api/v1/reports/{report['id']}/?days=7")
            self.assertEqual(data["id"], report["id"])
            self.assertIn("rows", data)
        self.assertEqual(float(self.ok("get", "/api/v1/reports/sales-over-time/")["total"]), 30.0)
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
        top = self.ok("get", "/api/v1/reports/top-products/?days=30")["total"]

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
