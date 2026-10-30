"""
    python manage.py test apps.webhooks --settings=config.test_settings
"""
import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.tenancy.models import Store

from . import services
from .models import FAILED, PENDING, SUCCEEDED, WebhookDelivery, WebhookEndpoint

URL = "https://hooks.example.com/tss"
BASE = "/api/v1/webhooks/endpoints/"


@override_settings(WEBHOOKS_ALLOW_PRIVATE_URLS=True)
class WebhookTestBase(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, self.store = self.sign_up("owner@example.com", "Hooks Shop")

    def endpoint(self, events=("*",), url=URL):
        return self.ok("post", BASE, {"url": url, "events": list(events)}, expect=201)

    def place_order(self):
        product = self.ok("post", "/api/v1/catalog/products/", {
            "title": "Shirt", "status": "active", "price": "100", "sku": "S-1",
        }, expect=201)
        variant = product["variants"][0]["id"]
        warehouse = self.ok("post", "/api/v1/inventory/locations/", {"name": "W"}, expect=201)
        self.ok("post", "/api/v1/inventory/items/set/", {"variant": variant, "location": warehouse["id"], "available": 5})
        with self.captureOnCommitCallbacks(execute=True):
            return self.ok("post", "/api/v1/orders/", {"items": [{"product": product["id"], "quantity": 1}]}, expect=201)


class EndpointApiTest(WebhookTestBase):
    def test_secret_only_on_create_and_rotate(self):
        created = self.endpoint(["order.created"])
        self.assertTrue(created["secret"].startswith("whsec_"))
        fetched = self.ok("get", f"{BASE}{created['id']}/")
        self.assertNotIn("secret", fetched)
        rotated = self.ok("post", f"{BASE}{created['id']}/rotate-secret/")
        self.assertNotEqual(rotated["secret"], created["secret"])

    def test_validation(self):
        self.ok("post", BASE, {"url": URL, "events": ["nope.nothing"]}, expect=400)
        self.ok("post", BASE, {"url": URL, "events": []}, expect=400)

    @override_settings(WEBHOOKS_ALLOW_PRIVATE_URLS=False)
    def test_internal_and_plain_http_urls_are_refused(self):
        for url in ("http://hooks.example.com/x", "https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest"):
            self.ok("post", BASE, {"url": url, "events": ["*"]}, expect=400)

    def test_other_stores_cannot_see_endpoints(self):
        created = self.endpoint()
        self.sign_up("other@example.com", "Other")  # switches the client to the other store
        self.ok("get", f"{BASE}{created['id']}/", expect=404)

    def test_staff_without_the_permission_is_refused(self):
        self.endpoint()
        roles = {r["key"]: r for r in self.ok("get", "/api/v1/roles/")}
        token, _ = self.sign_up("support@example.com", "Support own")
        self.use(self.token, self.store["storeId"])
        self.ok("post", "/api/v1/members/", {"email": "support@example.com", "roleIds": [roles["customer_support"]["id"]]}, expect=201)
        self.use(token, self.store["storeId"])
        self.ok("get", BASE, expect=403)


class DeliveryTest(WebhookTestBase):
    def deliveries(self):
        return WebhookDelivery.objects.order_by("created_at")

    def test_order_events_are_queued_for_subscribers_only(self):
        self.endpoint(["order.created"])
        self.endpoint(["order.cancelled"])
        order = self.place_order()
        queued = list(self.deliveries())
        self.assertEqual([d.event_type for d in queued], ["order.created"])
        self.assertEqual(queued[0].payload["data"]["object"]["order_number"], order["order_number"])
        self.assertEqual(queued[0].payload["store_id"], self.store["storeId"])

        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", f"/api/v1/orders/{order['id']}/cancel/")
        self.assertEqual(sorted(d.event_type for d in self.deliveries()), ["order.cancelled", "order.created"])

    def test_payment_events(self):
        self.endpoint(["payment.captured", "payment.refunded"])
        order = self.place_order()
        with self.captureOnCommitCallbacks(execute=True):
            payment = self.ok("post", "/api/v1/payments/", {"order": order["id"], "amount": order["total"]}, expect=201)
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", f"/api/v1/payments/{payment['id']}/refund/", {"amount": "10"})
        types = [d.event_type for d in self.deliveries()]
        self.assertEqual(types, ["payment.captured", "payment.refunded"])
        self.assertEqual(self.deliveries()[1].payload["data"]["object"]["refunded"], "10.00")

    def test_signed_request_is_sent_and_recorded(self):
        created = self.endpoint()
        self.place_order()
        sent = {}

        def fake_post(url, body, headers):
            sent.update(url=url, body=body, headers=headers)
            return 200

        with mock.patch.object(services, "post", fake_post):
            self.assertEqual(services.deliver_due(), 1)
        delivery = self.deliveries().get()
        self.assertEqual((delivery.status, delivery.attempts, delivery.last_status_code), (SUCCEEDED, 1, 200))
        stamp, sig = [p.split("=", 1)[1] for p in sent["headers"]["X-TSS-Signature"].split(",")]
        expected = hmac.new(created["secret"].encode(), f"{stamp}.".encode() + sent["body"], hashlib.sha256).hexdigest()
        self.assertTrue(hmac.compare_digest(sig, expected))
        self.assertEqual(json.loads(sent["body"])["id"], str(delivery.event_id))
        self.assertEqual(sent["headers"]["X-TSS-Event"], "order.created")
        # nothing is left to send
        self.assertEqual(services.deliver_due(), 0)

    def test_failure_is_retried_with_backoff_then_gives_up(self):
        endpoint = self.endpoint()
        self.place_order()
        with mock.patch.object(services, "post", return_value=500):
            services.deliver_due()
            delivery = self.deliveries().get()
            self.assertEqual((delivery.status, delivery.attempts, delivery.last_status_code), (PENDING, 1, 500))
            self.assertGreater(delivery.next_attempt_at, timezone.now() + timedelta(seconds=30))
            self.assertEqual(services.deliver_due(), 0)  # not due yet

            for _ in range(services.MAX_ATTEMPTS - 1):
                WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
                services.deliver_due()
        delivery = self.deliveries().get()
        self.assertEqual((delivery.status, delivery.attempts, delivery.next_attempt_at), (FAILED, services.MAX_ATTEMPTS, None))
        self.assertEqual(WebhookEndpoint.objects.get(pk=endpoint["id"]).consecutive_failures, 1)

    def test_network_error_is_a_failed_attempt(self):
        self.endpoint()
        self.place_order()
        with mock.patch.object(services, "post", side_effect=TimeoutError("timed out")):
            services.deliver_due()
        delivery = self.deliveries().get()
        self.assertEqual((delivery.status, delivery.attempts), (PENDING, 1))
        self.assertIn("timed out", delivery.last_error)

    @override_settings(WEBHOOKS_ALLOW_PRIVATE_URLS=False)
    def test_url_is_checked_again_at_send_time(self):
        endpoint = WebhookEndpoint.objects.create(store=Store.objects.get(pk=self.store["storeId"]), url="https://127.0.0.1/x", events=["*"])
        delivery = services.queue_ping(endpoint)
        with mock.patch.object(services, "post") as post:
            services.deliver_due()
        post.assert_not_called()
        delivery.refresh_from_db()
        self.assertIn("private", delivery.last_error)

    def test_endpoint_is_switched_off_after_repeated_failures(self):
        created = self.endpoint()
        endpoint = WebhookEndpoint.objects.get(pk=created["id"])
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=services.DISABLE_AFTER - 1)
        delivery = services.queue_ping(endpoint)
        WebhookDelivery.objects.filter(pk=delivery.pk).update(attempts=services.MAX_ATTEMPTS - 1)
        with mock.patch.object(services, "post", return_value=500):
            services.deliver_due()
        endpoint.refresh_from_db()
        self.assertFalse(endpoint.is_active)
        self.assertTrue(endpoint.disabled_reason)
        # turning it back on resets the counter
        self.ok("patch", f"{BASE}{created['id']}/", {"is_active": True})
        endpoint.refresh_from_db()
        self.assertEqual((endpoint.is_active, endpoint.consecutive_failures), (True, 0))

    def test_a_success_resets_the_failure_count(self):
        created = self.endpoint()
        WebhookEndpoint.objects.filter(pk=created["id"]).update(consecutive_failures=3)
        services.queue_ping(WebhookEndpoint.objects.get(pk=created["id"]))
        with mock.patch.object(services, "post", return_value=204):
            services.deliver_due()
        self.assertEqual(WebhookEndpoint.objects.get(pk=created["id"]).consecutive_failures, 0)

    def test_test_ping_and_manual_retry(self):
        created = self.endpoint(["order.created"])
        ping = self.ok("post", f"{BASE}{created['id']}/test/", expect=202)
        self.assertEqual(ping["event_type"], "ping")
        with mock.patch.object(services, "post", return_value=410):
            services.deliver_due()
            WebhookDelivery.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            for _ in range(services.MAX_ATTEMPTS):
                services.deliver_due()
                WebhookDelivery.objects.filter(status=PENDING).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.ok("post", f"/api/v1/webhooks/deliveries/{ping['id']}/retry/", expect=202)
        row = self.ok("get", f"/api/v1/webhooks/deliveries/{ping['id']}/")
        self.assertEqual((row["status"], row["attempts"]), ("pending", 0))
        self.ok("post", f"/api/v1/webhooks/deliveries/{ping['id']}/retry/", expect=400)

    def test_inactive_endpoints_get_nothing(self):
        created = self.endpoint()
        self.ok("patch", f"{BASE}{created['id']}/", {"is_active": False})
        self.place_order()
        self.assertEqual(self.deliveries().count(), 0)

    def test_a_broken_listener_never_breaks_the_order(self):
        self.endpoint()
        with mock.patch.object(services, "emit", side_effect=RuntimeError("boom")):
            order = self.place_order()
        self.assertEqual(order["status"], "open")


class MoreEventsTest(WebhookTestBase):
    def types(self):
        return sorted(WebhookDelivery.objects.values_list("event_type", flat=True))

    def test_event_list_includes_the_new_events(self):
        listed = {e["type"] for e in self.ok("get", f"{BASE}events/")}
        self.assertTrue({"customer.created", "product.created", "fulfillment.created", "shipment.created",
                         "shipment.delivered", "checkout.abandoned"} <= listed)

    def test_customer_and_product_created(self):
        self.endpoint(["customer.created", "product.created"])
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", "/api/v1/customers/", {"first_name": "Ann", "email": "ann@example.com"}, expect=201)
            self.ok("post", "/api/v1/catalog/products/", {
                "title": "Mug", "status": "active", "price": "5", "sku": "M-1",
            }, expect=201)
        self.assertEqual(self.types(), ["customer.created", "product.created"])
        payload = {d.event_type: d.payload["data"]["object"] for d in WebhookDelivery.objects.all()}
        self.assertEqual(payload["customer.created"]["email"], "ann@example.com")
        self.assertEqual(payload["product.created"]["variants"][0]["sku"], "M-1")

    def test_fulfillment_shipment_and_delivery(self):
        self.endpoint(["fulfillment.created", "shipment.created", "shipment.delivered"])
        order = self.place_order()
        gateway = self.ok("post", "/api/v1/delivery/gateways/", {"name": "Van", "code": "van"}, expect=201)
        with self.captureOnCommitCallbacks(execute=True):
            fulfillment = self.ok("post", "/api/v1/fulfillments/", {"order": order["id"]}, expect=201)
            shipment = self.ok("post", "/api/v1/delivery/shipments/", {
                "fulfillment": fulfillment["id"], "delivery_gateway": gateway["id"], "tracking_number": "T1",
            }, expect=201)
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", f"/api/v1/delivery/shipments/{shipment['id']}/events/", {"status": "delivered"})
        self.assertEqual(self.types(), ["fulfillment.created", "shipment.created", "shipment.delivered"])
        sent = {d.event_type: d.payload["data"]["object"] for d in WebhookDelivery.objects.all()}
        self.assertEqual(sent["fulfillment.created"]["items"][0]["quantity"], 1)
        self.assertEqual(sent["shipment.created"]["tracking_number"], "T1")
        self.assertEqual(sent["shipment.delivered"]["status"], "delivered")
        self.assertEqual(sent["shipment.delivered"]["order_number"], order["order_number"])

    def test_checkout_abandoned_is_sent_once(self):
        from apps.commerce import services as commerce
        from apps.commerce.models import Cart, Checkout

        self.endpoint(["checkout.abandoned"])
        store = Store.objects.get(pk=self.store["storeId"])
        cart = Cart.objects.create(store=store, status=commerce.CART_OPEN, session_id="s1", is_deleted=False)
        checkout = Checkout.objects.create(
            store=store, cart=cart, status=commerce.CHECKOUT_OPEN, currency="USD", email="a@example.com",
            subtotal=0, total_amount=0, discount_amount=0, tax_amount=0, shipping_amount=0, is_deleted=False,
        )
        Checkout.objects.filter(pk=checkout.pk).update(updated_at=timezone.now() - timedelta(hours=3))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(commerce.mark_abandoned(store, timedelta(hours=2)), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(commerce.mark_abandoned(store, timedelta(hours=2)), 0)
        delivery = WebhookDelivery.objects.get()
        self.assertEqual(delivery.payload["data"]["object"]["email"], "a@example.com")

    def test_order_payload_carries_addresses_and_line_ids(self):
        self.endpoint(["order.created"])
        order = self.place_order()
        body = WebhookDelivery.objects.get().payload["data"]["object"]
        item = body["items"][0]
        for key in ("id", "product_id", "variant_id", "sku", "unit_price", "tax_amount"):
            self.assertIn(key, item)
        self.assertEqual(item["sku"], "S-1")
        for key in ("shipping_address", "billing_address", "delivery_status", "created_at"):
            self.assertIn(key, body)
        self.assertEqual(body["order_number"], order["order_number"])


class SecretAndHousekeepingTest(WebhookTestBase):
    def test_signature_verifies_and_rejects_tampering_and_replays(self):
        body = b'{"a":1}'
        header = services.sign("whsec_x", 1000, body)
        self.assertTrue(services.verify_signature("whsec_x", header, body, now=1100))
        self.assertFalse(services.verify_signature("whsec_x", header, b'{"a":2}', now=1100))
        self.assertFalse(services.verify_signature("whsec_y", header, body, now=1100))
        self.assertFalse(services.verify_signature("whsec_x", header, body, now=1000 + 301))  # replayed later
        self.assertFalse(services.verify_signature("whsec_x", "garbage", body, now=1000))
        self.assertFalse(services.verify_signature("whsec_x", None, body, now=1000))

    def test_rotation_signs_with_both_secrets_until_the_overlap_ends(self):
        created = self.endpoint()
        old = created["secret"]
        rotated = self.ok("post", f"{BASE}{created['id']}/rotate-secret/")
        self.assertNotEqual(rotated["secret"], old)
        self.assertTrue(rotated["previous_secret_valid_until"])
        self.place_order()
        sent = {}

        def fake_post(url, body, headers):
            sent.update(body=body, header=headers["X-TSS-Signature"])
            return 200

        with mock.patch.object(services, "post", fake_post):
            services.deliver_due()
        self.assertEqual(sent["header"].count("v1="), 2)
        self.assertTrue(services.verify_signature(old, sent["header"], sent["body"]))
        self.assertTrue(services.verify_signature(rotated["secret"], sent["header"], sent["body"]))

        WebhookEndpoint.objects.filter(pk=created["id"]).update(
            previous_secret_expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertEqual(WebhookEndpoint.objects.get(pk=created["id"]).signing_secrets(), [rotated["secret"]])

    def test_prune_removes_only_old_finished_deliveries(self):
        endpoint = WebhookEndpoint.objects.get(pk=self.endpoint()["id"])
        old = timezone.now() - timedelta(days=40)
        rows = {}
        for status in (SUCCEEDED, FAILED, PENDING):
            rows[status] = services.queue_ping(endpoint)
            WebhookDelivery.objects.filter(pk=rows[status].pk).update(status=status, created_at=old)
        fresh = services.queue_ping(endpoint)
        WebhookDelivery.objects.filter(pk=fresh.pk).update(status=SUCCEEDED)
        self.assertEqual(services.prune_deliveries(30), 2)
        self.assertEqual(
            set(WebhookDelivery.objects.values_list("pk", flat=True)), {rows[PENDING].pk, fresh.pk}
        )

    def test_owners_are_emailed_when_an_endpoint_is_switched_off(self):
        from django.core import mail

        created = self.endpoint()
        endpoint = WebhookEndpoint.objects.get(pk=created["id"])
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=services.DISABLE_AFTER - 1)
        delivery = services.queue_ping(endpoint)
        WebhookDelivery.objects.filter(pk=delivery.pk).update(attempts=services.MAX_ATTEMPTS - 1)
        with mock.patch.object(services, "post", return_value=500):
            services.deliver_due()
        sent = [m for m in mail.outbox if "switched off" in m.subject]
        self.assertEqual(len(sent), 1)
        self.assertIn("owner@example.com", sent[0].to)
        self.assertIn(URL, sent[0].subject)
