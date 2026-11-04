"""
    python manage.py test apps.webhooks --settings=config.test_settings
"""
import hashlib
import hmac
import io
import json
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.tenancy.models import Store

from . import crypto, incoming, services
from .models import (
    DISPATCHED, FAILED, PENDING, PROCESSED, RECEIVED, SUCCEEDED,
    IncomingWebhookReceipt, WebhookDelivery, WebhookEndpoint, WebhookEvent,
)

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
        created = self.endpoint(["order.created"])
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
        endpoint = self.endpoint(["order.created"])
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
        self.endpoint(["order.created"])
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


# ---- outbox, secret encryption, leases/fencing, incoming receipts ----
# The suite runs on SQLite, so row locks (skip_locked) are not exercised; claims and leases are tested by calling
# the workers one after another and by moving the lease clock.

KEYS = "v1:" + "a" * 40
KEYS_V2 = "v2:" + "b" * 40 + ",v1:" + "a" * 40


class OutboxTest(WebhookTestBase):
    def setUp(self):
        super().setUp()
        self.row = Store.objects.get(pk=self.store["storeId"])
        self.endpoint_id = self.endpoint(["order.created"])["id"]

    def test_rollback_removes_the_event(self):
        try:
            with transaction.atomic():
                services.emit(self.row, "order.created", {"x": 1}, dedupe_key="order.created:1")
                raise RuntimeError("business operation failed")
        except RuntimeError:
            pass
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_committed_event_is_dispatched_into_deliveries(self):
        with self.captureOnCommitCallbacks(execute=True):
            event = services.emit(self.row, "order.created", {"x": 1}, dedupe_key="order.created:1")
        event.refresh_from_db()
        self.assertEqual(event.status, DISPATCHED)
        delivery = WebhookDelivery.objects.get()
        self.assertEqual((delivery.event_id, delivery.outbox_event_id), (event.pk, event.pk))
        self.assertEqual(delivery.payload["id"], str(event.pk))
        self.assertEqual(delivery.payload["data"]["object"], {"x": 1})

    def test_unsubscribed_event_is_not_recorded(self):
        self.assertIsNone(services.emit(self.row, "product.created", {}, dedupe_key="product.created:1"))
        self.assertEqual(WebhookEvent.objects.count(), 0)

    def test_same_business_operation_makes_one_event_and_one_delivery(self):
        with self.captureOnCommitCallbacks(execute=True):
            first = services.emit(self.row, "order.created", {"x": 1}, dedupe_key="order.created:7")
            second = services.emit(self.row, "order.created", {"x": 1}, dedupe_key="order.created:7")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual((WebhookEvent.objects.count(), WebhookDelivery.objects.count()), (1, 1))

    def test_database_refuses_a_second_event_with_the_same_key(self):
        services.emit(self.row, "order.created", {}, dedupe_key="order.created:9")
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookEvent.objects.create(store=self.row, event_type="order.created", dedupe_key="order.created:9", payload={})
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookEvent.objects.create(store=self.row, event_type="order.created", dedupe_key="", payload={})

    def test_losing_a_creation_race_returns_the_winner(self):
        winner = WebhookEvent.objects.create(store=self.row, event_type="order.created", dedupe_key="k", payload={"type": "x"})
        real = WebhookEvent.objects.filter
        calls = {"n": 0}

        def blind_first_lookup(*args, **kwargs):
            calls["n"] += 1
            return WebhookEvent.objects.none() if calls["n"] == 1 else real(*args, **kwargs)

        with mock.patch.object(WebhookEvent.objects, "filter", side_effect=blind_first_lookup), \
                mock.patch.object(WebhookEvent.objects, "get", side_effect=lambda **kw: real(**kw).get()):
            event = services.emit(self.row, "order.created", {}, dedupe_key="k")
        self.assertEqual(event.pk, winner.pk)
        self.assertEqual(WebhookEvent.objects.count(), 1)

    def test_broker_outage_loses_nothing(self):
        from . import tasks

        with mock.patch.object(tasks.dispatch_event_task, "delay", side_effect=ConnectionError("redis down")):
            with self.captureOnCommitCallbacks(execute=True):
                event = services.emit(self.row, "order.created", {"x": 1}, dedupe_key="order.created:2")
        event.refresh_from_db()
        self.assertEqual((event.status, WebhookDelivery.objects.count()), (PENDING, 0))
        # Redis is back: the sweeper (also run by deliver_due) finishes the job
        with mock.patch.object(services, "post", return_value=200):
            self.assertEqual(services.deliver_due(), 1)
        event.refresh_from_db()
        self.assertEqual((event.status, WebhookDelivery.objects.get().status), (DISPATCHED, SUCCEEDED))

    def test_dispatching_twice_adds_nothing(self):
        event = services.emit(self.row, "order.created", {}, dedupe_key="order.created:3")
        self.assertEqual(services.dispatch_event(event.pk), 1)
        self.assertEqual(services.dispatch_event(event.pk), 0)
        self.assertEqual(WebhookDelivery.objects.count(), 1)

    def test_unbuildable_event_is_parked_not_lost(self):
        event = WebhookEvent.objects.create(
            store=self.row, event_type="order.created", dedupe_key="lazy", payload=None, resource_model="nope.Nothing", resource_id="1",
        )
        for _ in range(services.MAX_DISPATCH_ATTEMPTS):
            services.dispatch_event(event.pk)
        event.refresh_from_db()
        self.assertEqual((event.status, event.dispatch_attempts), (FAILED, services.MAX_DISPATCH_ATTEMPTS))

    def test_event_is_only_delivered_inside_its_own_store(self):
        self.sign_up("other@example.com", "Other Shop")
        other = WebhookEndpoint.objects.exclude(pk=self.endpoint_id).first()
        self.assertIsNone(other)  # the second owner has no endpoint yet
        other_store = Store.objects.exclude(pk=self.row.pk).get()
        WebhookEndpoint.objects.create(store=other_store, url=URL, events=["*"])
        event = services.emit(self.row, "order.created", {}, dedupe_key="order.created:5")
        services.dispatch_event(event.pk)
        self.assertEqual(list(WebhookDelivery.objects.values_list("endpoint_id", flat=True)), [uuid.UUID(self.endpoint_id)])

    def test_customer_creation_records_event_in_the_same_transaction(self):
        self.endpoint(["customer.created"])
        with self.captureOnCommitCallbacks(execute=True):
            self.ok("post", "/api/v1/customers/", {"email": "buyer@example.com", "first_name": "A", "last_name": "B"}, expect=201)
        event = WebhookEvent.objects.get(event_type="customer.created")
        self.assertTrue(event.dedupe_key.startswith("customer.created:"))
        self.assertEqual(event.status, DISPATCHED)
        self.assertEqual(event.payload["data"]["object"]["email"], "buyer@example.com")


class SecretEncryptionTest(WebhookTestBase):
    @override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS)
    def test_round_trip_and_tamper_detection(self):
        sealed = crypto.encrypt("whsec_abc", "row:secret")
        self.assertNotIn("whsec_abc", sealed)
        self.assertEqual(crypto.decrypt(sealed, "row:secret"), "whsec_abc")
        self.assertNotEqual(sealed, crypto.encrypt("whsec_abc", "row:secret"))  # fresh nonce
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt(sealed, "other-row:secret")
        head, key_id, blob = sealed.split(":")
        flipped = blob[:-4] + ("AAAA" if not blob.endswith("AAAA") else "BBBB")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt(f"{head}:{key_id}:{flipped}", "row:secret")

    def test_new_endpoint_gets_an_encrypted_copy_and_signing_still_works(self):
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS):
            created = self.endpoint(["*"])
            endpoint = WebhookEndpoint.objects.get(pk=created["id"])
            self.assertTrue(endpoint.secret_encrypted.startswith("enc1:v1:"))
            self.assertEqual(endpoint.get_secret(), created["secret"])
            self.assertEqual(endpoint.signing_secrets(), [created["secret"]])
            self.assertNotIn(created["secret"], endpoint.secret_encrypted)

    def test_api_never_returns_secret_material(self):
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS):
            created = self.endpoint(["*"])
            detail = self.ok("get", f"{BASE}{created['id']}/")
            listing = json.dumps(self.ok("get", BASE))
        for text in (json.dumps(detail), listing):
            self.assertNotIn(created["secret"], text)
            self.assertNotIn("enc1:", text)
            self.assertNotIn("secret_encrypted", text)

    def test_legacy_endpoint_without_encrypted_copy_keeps_working(self):
        endpoint = WebhookEndpoint.objects.create(store=Store.objects.get(pk=self.store["storeId"]), url=URL, events=["*"], secret="whsec_legacy")
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(secret_encrypted="", secret_key_id="")
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS):
            endpoint.refresh_from_db()
            self.assertEqual(endpoint.get_secret(), "whsec_legacy")
            self.assertEqual(endpoint.signing_secrets(), ["whsec_legacy"])

    def test_backfill_encrypts_verifies_and_leaves_plaintext_alone(self):
        store = Store.objects.get(pk=self.store["storeId"])
        endpoint = WebhookEndpoint.objects.create(store=store, url=URL, events=["*"], secret="whsec_old", previous_secret="whsec_older")
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(secret_encrypted="", previous_secret_encrypted="", secret_key_id="")
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS):
            call_command("encrypt_webhook_secrets", "--dry-run", stdout=io.StringIO())
            endpoint.refresh_from_db()
            self.assertEqual(endpoint.secret_encrypted, "")
            call_command("encrypt_webhook_secrets", stdout=io.StringIO())
            endpoint.refresh_from_db()
            self.assertEqual((endpoint.secret, endpoint.previous_secret), ("whsec_old", "whsec_older"))
            self.assertEqual(crypto.decrypt(endpoint.secret_encrypted, endpoint._aad("secret")), "whsec_old")
            self.assertEqual(crypto.decrypt(endpoint.previous_secret_encrypted, endpoint._aad("previous_secret")), "whsec_older")
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS_V2):  # key rotation
            call_command("encrypt_webhook_secrets", stdout=io.StringIO())
            endpoint.refresh_from_db()
            self.assertEqual(endpoint.secret_key_id, "v2")
            self.assertEqual(endpoint.get_secret(), "whsec_old")

    def test_backfill_refuses_without_a_key(self):
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS="", DEBUG=False), self.assertRaises(CommandError):
            call_command("encrypt_webhook_secrets", stdout=io.StringIO())

    def test_rotation_keeps_both_secrets_encrypted_and_signing(self):
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS):
            created = self.endpoint(["*"])
            rotated = self.ok("post", f"{BASE}{created['id']}/rotate-secret/")
            endpoint = WebhookEndpoint.objects.get(pk=created["id"])
            self.assertEqual(endpoint.signing_secrets(), [rotated["secret"], created["secret"]])
            self.assertTrue(endpoint.previous_secret_encrypted.startswith("enc1:"))

    def test_undecryptable_value_falls_back_to_plaintext_without_logging_it(self):
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS=KEYS):
            created = self.endpoint(["*"])
        endpoint = WebhookEndpoint.objects.get(pk=created["id"])
        with override_settings(WEBHOOK_SECRET_ENCRYPTION_KEYS="v9:" + "z" * 40):
            with self.assertLogs("apps.webhooks.models", level="ERROR") as logs:
                self.assertEqual(endpoint.get_secret(), created["secret"])
        self.assertNotIn(created["secret"], "\n".join(logs.output))


class DeliveryLeaseTest(WebhookTestBase):
    def setUp(self):
        super().setUp()
        self.row = Store.objects.get(pk=self.store["storeId"])
        self.ep = WebhookEndpoint.objects.create(store=self.row, url=URL, events=["*"])

    def due(self, **kw):
        return services.queue_ping(self.ep)

    def test_destination_is_frozen_when_the_delivery_is_created(self):
        delivery = self.due()
        self.assertEqual(delivery.destination_url, URL)
        WebhookEndpoint.objects.filter(pk=self.ep.pk).update(url="https://changed.example.com/new")
        sent = {}
        with mock.patch.object(services, "post", lambda url, body, headers: sent.update(url=url) or 200):
            services.deliver_due()
        self.assertEqual(sent["url"], URL)

    def test_legacy_delivery_without_snapshot_uses_the_endpoint_url(self):
        delivery = self.due()
        WebhookDelivery.objects.filter(pk=delivery.pk).update(destination_url="")
        sent = {}
        with mock.patch.object(services, "post", lambda url, body, headers: sent.update(url=url) or 200):
            services.deliver_due()
        self.assertEqual(sent["url"], URL)
        self.assertEqual(WebhookDelivery.objects.get().destination_url, "")  # history is not rewritten

    def test_two_workers_never_claim_the_same_delivery(self):
        self.due()
        first, second = services.claim_due(), services.claim_due()
        self.assertEqual((len(first), len(second)), (1, 0))
        self.assertIsNotNone(first[0].lease_token)
        self.assertGreater(first[0].locked_until, timezone.now())

    def test_expired_lease_is_recovered(self):
        self.due()
        services.claim_due()
        WebhookDelivery.objects.update(locked_until=timezone.now() - timedelta(seconds=1), next_attempt_at=timezone.now() - timedelta(seconds=1))
        again = services.claim_due()
        self.assertEqual(len(again), 1)

    def test_stale_worker_cannot_overwrite_the_new_owner(self):
        self.due()
        (stale,) = services.claim_due()
        WebhookDelivery.objects.update(locked_until=timezone.now() - timedelta(seconds=1), next_attempt_at=timezone.now() - timedelta(seconds=1))
        (current,) = services.claim_due()
        self.assertNotEqual(stale.lease_token, current.lease_token)
        with mock.patch.object(services, "post", return_value=200):
            services.attempt(current)
        with mock.patch.object(services, "post", return_value=500):
            services.attempt(stale)  # slow worker finishing late
        row = WebhookDelivery.objects.get()
        self.assertEqual((row.status, row.attempts, row.last_status_code), (SUCCEEDED, 1, 200))
        self.assertIsNone(row.lease_token)

    def test_stale_worker_cannot_write_after_a_retry_reset_the_row(self):
        self.due()
        (stale,) = services.claim_due()
        services.retry(WebhookDelivery.objects.get())
        with mock.patch.object(services, "post", return_value=200):
            services.attempt(stale)
        self.assertEqual(WebhookDelivery.objects.get().status, PENDING)

    def test_result_clears_the_lease(self):
        self.due()
        with mock.patch.object(services, "post", return_value=500):
            services.deliver_due()
        row = WebhookDelivery.objects.get()
        self.assertEqual((row.status, row.attempts, row.lease_token, row.locked_until), (PENDING, 1, None, None))

    def test_database_refuses_half_a_lease_and_duplicate_event_rows(self):
        delivery = self.due()
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookDelivery.objects.filter(pk=delivery.pk).update(lease_token="11111111-1111-1111-1111-111111111111")
        with self.assertRaises(IntegrityError), transaction.atomic():
            WebhookDelivery.objects.create(endpoint=self.ep, event_id=delivery.event_id, event_type="ping", payload={})

    def test_delivery_api_shows_the_destination_but_no_lease(self):
        self.due()
        data = self.ok("get", "/api/v1/webhooks/deliveries/")
        row = data["results"][0] if isinstance(data, dict) else data[0]
        self.assertEqual(row["destination_url"], URL)
        self.assertNotIn("lease_token", row)


STRIPE_SECRET = "whsec_test_incoming"


@override_settings(STRIPE_WEBHOOK_SECRET=STRIPE_SECRET, STRIPE_SECRET_KEY="sk_test")
class IncomingReceiptTest(APITestCase):
    URL = "/api/v1/payments/stripe/webhook/"

    def body(self, event_id="evt_100", type="account.updated"):
        return json.dumps({"id": event_id, "object": "event", "type": type, "data": {"object": {"id": "acct_1"}}})

    def post(self, payload, secret=STRIPE_SECRET, stamp=None):
        stamp = int(time.time()) if stamp is None else stamp
        digest = hmac.new(secret.encode(), f"{stamp}.{payload}".encode(), hashlib.sha256).hexdigest()
        return APIClient().generic("POST", self.URL, payload.encode(), content_type="application/json",
                                   HTTP_STRIPE_SIGNATURE=f"t={stamp},v1={digest}")

    def test_bad_or_stale_signature_stores_nothing(self):
        payload = self.body()
        self.assertEqual(self.post(payload, secret="whsec_wrong").status_code, 400)
        self.assertEqual(self.post(payload, stamp=1000).status_code, 400)
        self.assertEqual(APIClient().generic("POST", self.URL, payload.encode(), content_type="application/json").status_code, 400)
        self.assertEqual(IncomingWebhookReceipt.objects.count(), 0)

    def test_verified_event_is_stored_then_processed(self):
        handled = []
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": handled.append}):
            self.assertEqual(self.post(self.body()).status_code, 200)
        receipt = IncomingWebhookReceipt.objects.get()
        self.assertEqual((receipt.provider, receipt.external_event_id, receipt.status), ("stripe", "evt_100", PROCESSED))
        self.assertEqual(handled[0]["id"], "evt_100")
        self.assertIsNotNone(receipt.processed_at)

    def test_duplicate_event_is_stored_and_processed_once(self):
        handled = []
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": handled.append}):
            self.assertEqual(self.post(self.body()).status_code, 200)
            self.assertEqual(self.post(self.body()).status_code, 200)
        self.assertEqual((IncomingWebhookReceipt.objects.count(), len(handled)), (1, 1))

    def test_database_refuses_a_second_receipt_for_the_same_event(self):
        incoming.record_receipt("stripe", {"id": "evt_1", "type": "x"})
        with self.assertRaises(IntegrityError), transaction.atomic():
            IncomingWebhookReceipt.objects.create(provider="stripe", external_event_id="evt_1", payload={})
        incoming.record_receipt("other", {"id": "evt_1", "type": "x"})  # same id, other provider
        self.assertEqual(IncomingWebhookReceipt.objects.count(), 2)

    def test_losing_a_creation_race_returns_the_stored_receipt(self):
        first, created = incoming.record_receipt("stripe", {"id": "evt_2", "type": "x"})
        again, created_again = incoming.record_receipt("stripe", {"id": "evt_2", "type": "x"})
        self.assertEqual((created, created_again, first.pk), (True, False, again.pk))

    def test_event_survives_a_broker_outage_and_is_processed_by_the_sweeper(self):
        from . import tasks

        handled = []
        receipt, _ = incoming.record_receipt("stripe", {"id": "evt_3", "type": "x"})
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False), \
                mock.patch.object(tasks.process_receipt_task, "delay", side_effect=ConnectionError("redis down")):
            incoming.enqueue(receipt)  # must not raise: the receipt is safe in PostgreSQL
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, RECEIVED)
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": handled.append}):
            self.assertEqual(incoming.process_pending(), 1)
        receipt.refresh_from_db()
        self.assertEqual((receipt.status, len(handled)), (PROCESSED, 1))

    def test_failure_is_recorded_retried_later_and_finally_parked(self):
        receipt, _ = incoming.record_receipt("stripe", {"id": "evt_4", "type": "x"})
        boom = mock.Mock(side_effect=ConnectionError("db hiccup"))
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": boom}):
            with self.assertRaises(ConnectionError):
                incoming.process_receipt(receipt.pk)
            receipt.refresh_from_db()
            self.assertEqual((receipt.status, receipt.attempts, receipt.lease_token), (RECEIVED, 1, None))
            self.assertGreater(receipt.next_attempt_at, timezone.now())
            self.assertEqual(incoming.process_pending(), 0)  # back-off not over
            IncomingWebhookReceipt.objects.update(attempts=incoming.MAX_ATTEMPTS - 1)
            with self.assertRaises(ConnectionError):
                incoming.process_receipt(receipt.pk)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, FAILED)

    def test_permanent_error_is_parked_at_once(self):
        receipt, _ = incoming.record_receipt("stripe", {"id": "evt_5", "type": "x"})
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": mock.Mock(side_effect=KeyError("bad"))}), \
                self.assertLogs("apps.webhooks.incoming", level="CRITICAL"):
            with self.assertRaises(KeyError):
                incoming.process_receipt(receipt.pk)
        self.assertEqual(IncomingWebhookReceipt.objects.get().status, FAILED)

    def test_validation_error_is_parked_at_once(self):
        from rest_framework.exceptions import ValidationError

        receipt, _ = incoming.record_receipt("stripe", {"id": "evt_7", "type": "x"})
        boom = mock.Mock(side_effect=ValidationError("amount doesn't match"))
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": boom}), \
                self.assertLogs("apps.webhooks.incoming", level="CRITICAL"):
            with self.assertRaises(ValidationError):
                incoming.process_receipt(receipt.pk)
        receipt.refresh_from_db()
        self.assertEqual((receipt.status, receipt.attempts), (FAILED, 1))
        self.assertIn("ValidationError", receipt.last_error)

    def test_retry_back_off_doubles_and_is_capped(self):
        boom = mock.Mock(side_effect=ConnectionError("db hiccup"))
        expected = {0: 30, 1: 60, 2: 120, 5: 900, 7: 900}  # seconds after attempt number attempts+1
        for before, seconds in expected.items():
            with self.subTest(attempts_before=before):
                IncomingWebhookReceipt.objects.all().delete()
                receipt, _ = incoming.record_receipt("stripe", {"id": f"evt_b{before}", "type": "x"})
                IncomingWebhookReceipt.objects.update(attempts=before)
                started = timezone.now()
                with mock.patch.object(incoming, "_handlers", return_value={"stripe": boom}):
                    with self.assertRaises(ConnectionError):
                        incoming.process_receipt(receipt.pk)
                receipt.refresh_from_db()
                wait = (receipt.next_attempt_at - started).total_seconds()
                self.assertAlmostEqual(wait, seconds, delta=5)
                self.assertEqual(receipt.status, RECEIVED)

    def test_the_attempt_before_the_limit_still_retries_and_the_limit_parks_it(self):
        boom = mock.Mock(side_effect=ConnectionError("db hiccup"))
        for before, status in ((incoming.MAX_ATTEMPTS - 2, RECEIVED), (incoming.MAX_ATTEMPTS - 1, FAILED)):
            with self.subTest(attempts_before=before):
                IncomingWebhookReceipt.objects.all().delete()
                receipt, _ = incoming.record_receipt("stripe", {"id": f"evt_l{before}", "type": "x"})
                IncomingWebhookReceipt.objects.update(attempts=before)
                with mock.patch.object(incoming, "_handlers", return_value={"stripe": boom}):
                    with self.assertRaises(ConnectionError):
                        incoming.process_receipt(receipt.pk)
                self.assertEqual(IncomingWebhookReceipt.objects.get().status, status)

    def test_a_retry_that_succeeds_clears_the_error_and_is_not_run_again(self):
        receipt, _ = incoming.record_receipt("stripe", {"id": "evt_8", "type": "x"})
        flaky = mock.Mock(side_effect=[ConnectionError("db hiccup"), None])
        with mock.patch.object(incoming, "_handlers", return_value={"stripe": flaky}):
            with self.assertRaises(ConnectionError):
                incoming.process_receipt(receipt.pk)
            IncomingWebhookReceipt.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(incoming.process_pending(), 1)
            self.assertEqual(incoming.process_pending(), 0)
        receipt.refresh_from_db()
        self.assertEqual((receipt.status, receipt.attempts, receipt.last_error), (PROCESSED, 2, ""))
        self.assertEqual(flaky.call_count, 2)

    def test_a_parked_receipt_is_never_picked_up_by_the_sweeper(self):
        incoming.record_receipt("stripe", {"id": "evt_9", "type": "x"})
        IncomingWebhookReceipt.objects.update(status=FAILED)
        self.assertEqual(incoming.process_pending(), 0)

    def test_leased_receipt_is_not_taken_twice_and_stale_worker_is_fenced(self):
        receipt, _ = incoming.record_receipt("stripe", {"id": "evt_6", "type": "x"})
        claimed = incoming.claim_receipt(receipt.pk)
        self.assertIsNotNone(claimed)
        self.assertIsNone(incoming.claim_receipt(receipt.pk))
        IncomingWebhookReceipt.objects.update(locked_until=timezone.now() - timedelta(seconds=1))
        reclaimed = incoming.claim_receipt(receipt.pk)
        self.assertNotEqual(claimed.lease_token, reclaimed.lease_token)
        # the old owner finishing late changes nothing
        self.assertEqual(IncomingWebhookReceipt.objects.filter(pk=receipt.pk, lease_token=claimed.lease_token).update(status=PROCESSED), 0)
        self.assertEqual(IncomingWebhookReceipt.objects.get().status, RECEIVED)
