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


class PurchaseOrderPermissionTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, store = self.sign_up("po-permissions@example.com", "PO permissions")
        self.store_id = store["storeId"]
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Tee", "status": "active", "price": "500", "sku": "TEE-PERM-1",
        }, expect=201)
        self.variant = product["variants"][0]["id"]
        self.location = self.ok("post", "/api/v1/inventory/locations/", {"name": "Warehouse"}, expect=201)["id"]
        self.po_body = {"supplier_name": "Acme", "items": [{"variant": self.variant, "quantity": 2}]}
        po = self.ok("post", "/api/v1/purchase-orders/", self.po_body, expect=201)
        self.po_id = po["id"]

    def test_purchase_order_endpoints_require_auth_and_purchasing_permission(self):
        actions = (
            ("get", "/api/v1/purchase-orders/", None),
            ("post", "/api/v1/purchase-orders/", self.po_body),
            ("post", f"/api/v1/purchase-orders/{self.po_id}/order/", {}),
            ("post", f"/api/v1/purchase-orders/{self.po_id}/cancel/", {}),
            ("post", f"/api/v1/purchase-orders/{self.po_id}/receive/", {"location": self.location}),
        )

        role = self.ok("post", "/api/v1/roles/", {
            "name": "No purchasing permissions", "permissions": [],
        }, expect=201)
        member_token, _ = self.sign_up("po-no-permissions@example.com", "PO member shop")
        self.use(self.token, self.store_id)
        self.ok("post", "/api/v1/members/", {
            "email": "po-no-permissions@example.com", "roleIds": [role["id"]],
        }, expect=201)
        self.use(member_token, self.store_id)
        for method, url, body in actions:
            self.ok(method, url, body, expect=403)

        self.client.credentials()
        self.client.cookies.clear()
        for method, url, body in actions:
            response = getattr(self.client, method)(url, body, format="json")
            self.assertEqual(response.status_code, 401, f"{method.upper()} {url}: {response.content[:300]}")
