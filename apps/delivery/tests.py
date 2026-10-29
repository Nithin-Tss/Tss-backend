"""
Shipping API access and permissions.

    python manage.py test apps.delivery --settings=config.test_settings
"""
from rest_framework.test import APIClient, APITestCase

from apps.core.tests.test_rbac import RbacTestBase

GATEWAYS = "/api/v1/delivery/gateways/"
SHIPMENTS = "/api/v1/delivery/shipments/"
ZERO = "00000000-0000-0000-0000-000000000000"


class ShippingAccessTest(RbacTestBase):
    def gateway(self, code="vandelivery"):
        self.as_owner()
        return self.ok("post", GATEWAYS, {"name": "Van", "code": code}, expect=201)

    def test_anonymous_is_refused(self):
        anonymous = APIClient()
        for url in (GATEWAYS, SHIPMENTS):
            self.assertEqual(anonymous.get(url, HTTP_X_STORE_ID=self.store_id).status_code, 401)

    def test_store_header_is_required(self):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.owner_token}")
        self.assertEqual(self.client.get(GATEWAYS).status_code, 400)

    def test_staff_without_roles_is_refused(self):
        self.staff("nobody@example.com")
        self.ok("get", GATEWAYS, expect=403)
        self.ok("get", SHIPMENTS, expect=403)

    def test_unrelated_role_is_refused(self):
        self.staff("merch@example.com", "merchandiser")
        self.ok("get", GATEWAYS, expect=403)
        self.ok("get", SHIPMENTS, expect=403)

    def test_shipping_coordinator_manages_partners_and_shipments(self):
        self.staff("ship@example.com", "shipping_coordinator")
        gw = self.ok("post", GATEWAYS, {"name": "Van", "code": "Van"}, expect=201)
        self.assertEqual(gw["code"], "van")
        self.ok("patch", f"{GATEWAYS}{gw['id']}/", {"is_active": False})
        self.assertEqual(len(self.ok("get", GATEWAYS)), 1)
        self.ok("get", SHIPMENTS)

    def test_partner_view_permission_is_read_only(self):
        self.as_owner()
        role = self.ok("post", "/api/v1/roles/", {
            "name": "Partner viewer", "permissions": ["shipping.view"],
        }, expect=201)
        self.roles["Partner viewer"] = role
        self.staff("viewer@example.com", "Partner viewer")
        self.ok("get", GATEWAYS)
        self.ok("post", GATEWAYS, {"name": "Van", "code": "van"}, expect=403)
        self.ok("get", SHIPMENTS, expect=403)  # needs fulfillment.view

    def test_shipments_need_fulfillment_permissions(self):
        self.as_owner()
        role = self.ok("post", "/api/v1/roles/", {
            "name": "Shipment viewer", "permissions": ["fulfillment.view"],
        }, expect=201)
        self.roles["Shipment viewer"] = role
        self.staff("viewer@example.com", "Shipment viewer")
        self.ok("get", SHIPMENTS)
        self.ok("post", SHIPMENTS, {}, expect=403)
        self.ok("post", f"{SHIPMENTS}{ZERO}/events/", {"status": "delivered"}, expect=403)
        self.ok("get", GATEWAYS, expect=403)

    def test_partners_are_scoped_to_their_store(self):
        gw = self.gateway()
        other = APIClient()
        r = other.post("/api/v1/auth/signup/", {
            "firstName": "Other", "lastName": "User", "email": "other@example.com",
            "password": "Sturdy-pass-123", "confirmPassword": "Sturdy-pass-123",
            "mobileNumber": "9876543210", "agreeTerms": True,
        }, format="json")
        token = r.json()["access"]
        other.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        store = other.post("/api/v1/stores/", {"storeName": "Other"}, format="json").json()
        other.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=store["storeId"])
        self.assertEqual(other.get(GATEWAYS).json(), [])
        self.assertEqual(other.get(f"{GATEWAYS}{gw['id']}/").status_code, 404)
        # and cannot reach this store by naming it
        other.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=self.store_id)
        self.assertEqual(other.get(GATEWAYS).status_code, 403)

    def test_duplicate_partner_code_is_rejected(self):
        self.gateway("van")
        self.ok("post", GATEWAYS, {"name": "Van 2", "code": "VAN"}, expect=400)


class ShippingCoordinatorFlowTest(RbacTestBase):
    """A Shipping coordinator, on their own, can take a paid order out the door."""

    def test_fulfil_ship_track_and_deliver(self):
        self.as_owner()
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Shirt", "status": "active", "price": "100", "sku": "S-1",
        }, expect=201)
        warehouse = self.ok("post", "/api/v1/inventory/locations/", {"name": "Warehouse"}, expect=201)
        self.ok("post", "/api/v1/inventory/items/set/", {
            "variant": product["variants"][0]["id"], "location": warehouse["id"], "available": 5,
        })
        order = self.ok("post", "/api/v1/orders/", {
            "items": [{"product": product["id"], "quantity": 2}],
        }, expect=201)

        self.staff("ship@example.com", "shipping_coordinator")
        fulfillment = self.ok("post", "/api/v1/fulfillments/", {"order": order["id"]}, expect=201)
        gateway = self.ok("post", GATEWAYS, {"name": "Van", "code": "van"}, expect=201)
        shipment = self.ok("post", SHIPMENTS, {
            "fulfillment": fulfillment["id"], "delivery_gateway": gateway["id"], "tracking_number": "T1",
        }, expect=201)
        self.assertEqual(shipment["status"], "in_transit")
        self.assertEqual(len(self.ok("get", f"{SHIPMENTS}?order={order['id']}")), 1)

        shipment = self.ok("post", f"{SHIPMENTS}{shipment['id']}/events/", {"status": "delivered"})
        self.assertEqual(shipment["status"], "delivered")

        # The role can see orders but not change them or touch other areas
        self.ok("get", f"/api/v1/orders/{order['id']}/")
        self.ok("post", f"/api/v1/orders/{order['id']}/cancel/", expect=403)
        self.ok("get", "/api/v1/catalog/products/", expect=403)


from decimal import Decimal  # noqa: E402

from apps.commerce.test_integration import ADDRESS, CommerceIntegrationBase, sign_up_with_store  # noqa: E402
from apps.delivery.models import Shipment  # noqa: E402
from apps.inventory.services import sellable  # noqa: E402


class ShippingFullFlowTest(CommerceIntegrationBase):
    """Shopper checks out with a shipping fee -> staff fulfil, ship, track -> delivered."""

    def place_order(self, quantity=2):
        cid = self.checkout(quantity=quantity, email="a@b.com", shipping_address=ADDRESS)
        r = self.place(cid)
        self.assertEqual(r.status_code, 201, r.data)
        return r.data

    def setUp(self):
        super().setUp()
        self.store.shipping_flat_rate = Decimal("50.00")
        self.store.free_shipping_threshold = Decimal("1500.00")
        self.store.save()
        self.gateway = self.staff.post(GATEWAYS, {"name": "Van", "code": "van"}, format="json").data

    def ship(self, fulfillment, gateway=None, **extra):
        return self.staff.post(SHIPMENTS, {
            "fulfillment": fulfillment, "delivery_gateway": (gateway or self.gateway)["id"], **extra,
        }, format="json")

    def test_shipping_fee_then_ship_track_deliver(self):
        order = self.place_order()
        self.assertEqual(order["shipping_amount"], "50.00")
        self.assertEqual(order["total"], "1048.00")
        self.assertEqual(sellable(self.variant), 8)

        f = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json")
        self.assertEqual(f.status_code, 201, f.data)

        r = self.ship(f.data["id"], tracking_number="TRK1")
        self.assertEqual(r.status_code, 201, r.data)
        shipment = r.data
        self.assertEqual(shipment["status"], "in_transit")
        self.assertEqual(shipment["items"][0]["quantity"], 2)
        self.assertEqual(len(shipment["tracking_events"]), 1)
        self.assertEqual(self.staff.get(f"/api/v1/orders/{order['id']}/").data["delivery_status"], "shipped")

        # shopper sees the shipped status on their own order
        self.assertEqual(self.shopper.get(f"{self.base}/orders/{order['id']}/").data["delivery_status"], "shipped")

        r = self.staff.post(f"{SHIPMENTS}{shipment['id']}/events/",
                            {"status": "out_for_delivery", "location": "Madurai"}, format="json")
        self.assertEqual(r.data["status"], "out_for_delivery")
        r = self.staff.post(f"{SHIPMENTS}{shipment['id']}/events/", {"status": "delivered"}, format="json")
        self.assertEqual(r.data["status"], "delivered")
        self.assertIsNotNone(r.data["delivered_at"])
        self.assertEqual([e["status"] for e in r.data["tracking_events"]],
                         ["in_transit", "out_for_delivery", "delivered"])
        self.assertEqual(self.shopper.get(f"{self.base}/orders/{order['id']}/").data["delivery_status"], "delivered")

        # delivered is final
        r = self.staff.post(f"{SHIPMENTS}{shipment['id']}/events/", {"status": "in_transit"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_free_shipping_over_threshold(self):
        order = self.place_order(quantity=4)  # 1996 >= 1500
        self.assertEqual(order["shipping_amount"], "0.00")

    def test_partial_fulfillments_ship_separately(self):
        order = self.place_order()
        line = order["items"][0]["id"]
        f1 = self.staff.post("/api/v1/fulfillments/", {
            "order": order["id"], "items": [{"order_item": line, "quantity": 1}]}, format="json").data
        f2 = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json").data
        self.assertEqual(self.ship(f1["id"]).status_code, 201)
        self.assertEqual(self.staff.get(f"/api/v1/orders/{order['id']}/").data["delivery_status"], "shipped")
        self.assertEqual(self.ship(f2["id"]).status_code, 201)
        self.assertEqual(len(self.staff.get(f"{SHIPMENTS}?order={order['id']}").json()), 2)

    def test_cannot_ship_twice_or_with_a_turned_off_partner(self):
        order = self.place_order()
        f = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json").data
        off = self.staff.post(GATEWAYS, {"name": "Old", "code": "old", "is_active": False}, format="json").data
        self.assertEqual(self.ship(f["id"], gateway=off).status_code, 400)
        self.assertEqual(self.ship(f["id"]).status_code, 201)
        self.assertEqual(self.ship(f["id"]).status_code, 400)
        self.assertEqual(Shipment.objects.count(), 1)

    def test_cannot_ship_an_unfulfilled_or_unknown_fulfillment(self):
        self.assertEqual(self.ship(ZERO).status_code, 400)
        self.assertEqual(self.staff.post(SHIPMENTS, {}, format="json").status_code, 400)

    def test_cannot_use_another_stores_records(self):
        order = self.place_order()
        f = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json").data
        other, _ = sign_up_with_store("other@example.com")
        theirs = other.post(GATEWAYS, {"name": "Theirs", "code": "t"}, format="json").data
        # their gateway on my fulfillment, and my fulfillment from their store
        self.assertEqual(self.ship(f["id"], gateway=theirs).status_code, 400)
        r = other.post(SHIPMENTS, {"fulfillment": f["id"], "delivery_gateway": theirs["id"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Shipment.objects.count(), 0)

    def test_cancelled_order_cannot_be_fulfilled_or_shipped(self):
        order = self.place_order()
        self.assertEqual(self.shopper.post(f"{self.base}/orders/{order['id']}/cancel/").status_code, 200)
        r = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_tracking_event_needs_a_status(self):
        order = self.place_order()
        f = self.staff.post("/api/v1/fulfillments/", {"order": order["id"]}, format="json").data
        s = self.ship(f["id"]).data
        r = self.staff.post(f"{SHIPMENTS}{s['id']}/events/", {}, format="json")
        self.assertEqual(r.status_code, 400)


RATES = "/api/v1/delivery/rates/"


class ShippingRateApiTest(RbacTestBase):
    def test_coordinator_manages_rates(self):
        self.staff("ship@example.com", "shipping_coordinator")
        rate = self.ok("post", RATES, {"country": " India ", "min_weight": "1", "max_weight": "5", "price": "60"}, expect=201)
        self.assertEqual((rate["country"], rate["price"], rate["max_weight"]), ("India", "60.00", "5.000"))
        self.ok("patch", f"{RATES}{rate['id']}/", {"price": "75"})
        self.assertEqual(len(self.ok("get", RATES)), 1)
        self.ok("delete", f"{RATES}{rate['id']}/", expect=204)
        self.assertEqual(self.ok("get", RATES), [])

    def test_open_ended_band_and_validation(self):
        self.as_owner()
        rate = self.ok("post", RATES, {"price": "10", "min_weight": "2"}, expect=201)
        self.assertIsNone(rate["max_weight"])
        self.ok("post", RATES, {"price": "10", "min_weight": "5", "max_weight": "5"}, expect=400)
        self.ok("post", RATES, {"price": "-1"}, expect=400)
        self.ok("patch", f"{RATES}{rate['id']}/", {"max_weight": "1"}, expect=400)

    def test_view_permission_is_read_only(self):
        self.as_owner()
        role = self.ok("post", "/api/v1/roles/", {"name": "Rate viewer", "permissions": ["shipping.view"]}, expect=201)
        self.roles["Rate viewer"] = role
        self.staff("viewer@example.com", "Rate viewer")
        self.ok("get", RATES)
        self.ok("post", RATES, {"price": "10"}, expect=403)

    def test_unrelated_role_is_refused(self):
        self.staff("merch@example.com", "merchandiser")
        self.ok("get", RATES, expect=403)


class ShippingRateIsolationTest(RbacTestBase):
    def test_another_stores_rates_are_invisible_and_immutable(self):
        self.as_owner()
        rate = self.ok("post", RATES, {"price": "10"}, expect=201)
        # a second merchant with their own store
        token, other = self.sign_up("other-ship@example.com", "Other shipping shop")
        self.use(token, other["storeId"])
        self.assertEqual(self.ok("get", RATES), [])
        self.ok("patch", f"{RATES}{rate['id']}/", {"price": "0"}, expect=404)
        self.ok("delete", f"{RATES}{rate['id']}/", expect=404)
        # a submitted store in the body is ignored: the rate lands in the caller's store
        mine = self.ok("post", RATES, {"price": "5", "store": self.store_id}, expect=201)
        self.as_owner()
        self.assertEqual([r["id"] for r in self.ok("get", RATES)], [rate["id"]])
        self.assertNotEqual(mine["id"], rate["id"])
        self.assertEqual(self.ok("get", f"{RATES}{rate['id']}/")["price"], "10.00")

    def test_anonymous_cannot_touch_rates(self):
        anonymous = APIClient()
        for method in ("get", "post"):
            r = getattr(anonymous, method)(RATES, {"price": "1"}, format="json", HTTP_X_STORE_ID=self.store_id)
            self.assertEqual(r.status_code, 401, method)


class OrderKeepsItsTaxAfterSettingsChangeTest(CommerceIntegrationBase):
    def configure(self, **settings):
        r = self.staff.patch(f"/api/v1/stores/{self.store.pk}/", settings, format="json")
        self.assertEqual(r.status_code, 200, r.content)

    def test_placed_order_is_not_rewritten_by_new_tax_or_shipping(self):
        self.configure(taxRate="10", shippingFlatRate="50")
        order = self.place(self.checkout(quantity=2, email="a@b.com", shipping_address=ADDRESS)).data
        before = (order["tax_amount"], order["shipping_amount"], order["total"])
        self.assertEqual(before, ("99.80", "50.00", "1147.80"))

        self.configure(taxRate="25", shippingFlatRate="500")
        again = self.staff.get(f"/api/v1/orders/{order['id']}/").json()
        self.assertEqual((again["tax_amount"], again["shipping_amount"], again["total"]), before)

    def test_tax_boundaries_and_rounding(self):
        for rate, ok in (("0", True), ("100", True), ("100.01", False), ("-0.01", False)):
            r = self.staff.patch(f"/api/v1/stores/{self.store.pk}/", {"taxRate": rate}, format="json")
            self.assertEqual(r.status_code == 200, ok, rate)
        self.configure(taxRate="8.25", shippingFlatRate="0")
        cid = self.checkout(quantity=1, email="a@b.com", shipping_address=ADDRESS)
        detail = self.shopper.get(f"{self.base}/checkout/{cid}/").data
        self.assertEqual(detail["tax_amount"], "41.17")  # 499.00 * 8.25% = 41.1675 -> half up
        self.assertEqual(detail["total_amount"], "540.17")
