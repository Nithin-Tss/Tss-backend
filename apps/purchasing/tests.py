"""
Purchase order tests.

    python manage.py test apps.purchasing --settings=config.test_settings
"""
import copy
from unittest import mock

from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.purchasing.models import PurchaseOrder
from apps.purchasing.views import PurchaseOrderViewSet


class PurchaseOrderReceiveTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.sign_up("po@example.com", "PO Store")
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Tee", "status": "active", "price": "500", "sku": "TEE-1",
        }, expect=201)
        self.variant = product["variants"][0]["id"]
        self.warehouse = self.ok("post", "/api/v1/inventory/locations/", {"name": "Warehouse"}, expect=201)["id"]

    def test_two_requests_that_both_read_the_old_status_add_stock_once(self):
        po = self.ok("post", "/api/v1/purchase-orders/", {
            "supplier_name": "Acme", "items": [{"variant": self.variant, "quantity": 10}],
        }, expect=201)
        url = f"/api/v1/purchase-orders/{po['id']}/receive/"
        body = {"location": self.warehouse}
        stale = PurchaseOrder.objects.get(pk=po["id"])  # each request gets its own copy of this old row; no request sees the other's change in memory
        #: status "draft"

        with mock.patch.object(PurchaseOrderViewSet, "get_object", lambda view: copy.copy(stale)):
            self.ok("post", url, body)
            self.ok("post", url, body, expect=400)  # still sees the old status, but the locked row says received

        self.assertEqual(self.stock(self.variant)["available"], 10)
        self.assertEqual(PurchaseOrder.objects.get(pk=po["id"]).status, "received")
