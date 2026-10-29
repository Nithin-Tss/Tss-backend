"""
Reports and overview endpoints.

    python manage.py test apps.core.tests.test_reports --settings=config.test_settings
"""
from apps.customers.models import Customer
from apps.orders.models import Order
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
