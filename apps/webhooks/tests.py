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
