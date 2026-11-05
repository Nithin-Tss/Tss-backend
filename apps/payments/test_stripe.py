"""
Stripe Connect payments, with Stripe itself mocked.

    python manage.py test apps.payments.test_stripe --settings=config.test_settings
"""
import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings
from rest_framework.exceptions import ValidationError

from apps.commerce import tests as base_tests
from apps.commerce.models import Checkout
from apps.inventory.services import sellable
from apps.orders.models import Order

from django.urls import include, path

from . import services, stripe_service
from .models import Payment

urlpatterns = [
    path("api/v1/payments/", include("apps.payments.urls")),
    path("api/v1/storefront/", include("apps.commerce.storefront_urls")),
]

ACCOUNT = "acct_123"


def intent(checkout, status="succeeded", amount=None, currency="inr", id="pi_1"):
    return SimpleNamespace(
        id=id, status=status, currency=currency, client_secret="secret_1",
        amount=amount if amount is not None else int(checkout.total_amount * 100),
        metadata={"checkout_id": str(checkout.pk), "store_id": str(checkout.store_id)},
    )


@override_settings(ROOT_URLCONF=__name__, STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class StripeTests(base_tests.CommerceTestBase):
    base = base_tests.PlaceOrderTests.base
    h = base_tests.PlaceOrderTests.h
    address = base_tests.PlaceOrderTests.address

    def setUp(self):
        super().setUp()
        self.store.stripe_account_id, self.store.stripe_charges_enabled = ACCOUNT, True
        self.store.save()
        self.client_mock = mock.MagicMock()
        patcher = mock.patch.object(stripe_service, "client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def checkout(self, quantity=2):
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity}, format="json", **self.h)
        r = self.client.post(
            f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": self.address}, format="json", **self.h
        )
        return Checkout.objects.get(pk=r.data["id"])

    def test_session_creates_an_intent_on_the_connected_account_and_reuses_it(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        intents.retrieve.return_value = intent(checkout, status="requires_payment_method")

        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["client_secret"], "secret_1")
        self.assertIsNone(r.data["stripe_account"])  # the store's own keys: charged directly, no connected account
        params, = intents.create.call_args.args
        self.assertEqual((params["amount"], params["currency"]), (99800, "inr"))
        self.assertNotIn("stripe_account", intents.create.call_args.kwargs["options"])

        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(intents.create.call_count, 1)

    def test_minor_units_are_rounded_not_truncated(self):
        self.assertEqual(stripe_service.to_minor_units(Decimal("19.995"), "USD"), 2000)
        self.assertEqual(stripe_service.to_minor_units(Decimal("0.29"), "USD"), 29)
        self.assertEqual(stripe_service.to_minor_units(Decimal("500.4"), "JPY"), 500)

    def test_session_refuses_while_a_payment_is_processing(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        intents.retrieve.return_value = intent(checkout, status="processing")
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 400)
        intents.update.assert_not_called()

    def test_store_without_stripe_cannot_start_a_payment(self):
        self.store.stripe_charges_enabled = False
        self.store.save()
        checkout = self.checkout()
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 400)

    def test_payment_session_rejects_insufficient_stock_without_creating_payment(self):
        checkout = self.checkout(quantity=2)
        self.client_mock.v1.payment_intents.create.return_value = intent(checkout, status="requires_payment_method")
        from apps.inventory.services import set_available
        set_available(self.variant, self.location, 1)

        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)

        self.assertEqual(r.status_code, 400)
        self.assertIn("Only 1 of Tee left", str(r.data))
        self.client_mock.v1.payment_intents.create.assert_not_called()
        self.assertEqual(Payment.objects.count(), 0)

    def test_payment_session_refuses_a_product_that_is_no_longer_for_sale(self):
        from apps.catalog.models import Product, ProductSalesChannel

        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        url = f"{self.base}/checkout/{checkout.pk}/stripe/"
        self.assertEqual(self.client.post(url, **self.h).status_code, 200)  # for sale: the session starts
        intents.create.reset_mock()
        intents.retrieve.reset_mock()

        for status in ("draft", "archived"):
            Product.objects.filter(pk=self.product.pk).update(status=status)
            r = self.client.post(url, **self.h)
            self.assertEqual(r.status_code, 400, status)
            self.assertIn("Tee isn't available.", str(r.data))
        Product.objects.filter(pk=self.product.pk).update(status="active")
        self.assertEqual(self.client.post(url, **self.h).status_code, 200)

        intents.create.reset_mock()
        ProductSalesChannel.objects.filter(product=self.product).delete()  # off the Online Store
        r = self.client.post(url, **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Tee isn't available.", str(r.data))
        intents.create.assert_not_called()  # no payment was started for it

    def test_payment_session_holds_stock_until_the_order_is_placed(self):
        from apps.inventory.models import StockReservation
        from apps.inventory.services import reserved, sellable_for
        checkout = self.checkout(quantity=2)
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(reserved(self.variant), 2)
        self.assertEqual(sellable(self.variant), 10)  # holds never touch the stock numbers
        self.assertEqual(sellable_for(self.variant), 8)
        self.assertEqual(sellable_for(self.variant, checkout.pk), 10)  # its own hold doesn't count against it

        stripe_service.finalize(intent(checkout), ACCOUNT)
        self.assertEqual(StockReservation.objects.get().status, "converted")
        self.assertEqual(reserved(self.variant), 0)
        self.assertEqual(sellable(self.variant), 8)

    def test_held_stock_blocks_another_checkout_until_released_or_expired(self):
        import uuid
        from datetime import timedelta
        from django.utils import timezone
        from apps.inventory import services as inv
        from apps.inventory.models import StockReservation
        mine = self.checkout(quantity=5)
        # Another shopper's checkout then holds 8 of the 10
        inv.reserve(uuid.uuid4(), self.store.pk, [(self.variant, 8)], timedelta(minutes=15))
        self.client_mock.v1.payment_intents.create.return_value = intent(mine, status="requires_payment_method")

        r = self.client.post(f"{self.base}/checkout/{mine.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("Only 2 of Tee left", str(r.data))
        self.client_mock.v1.payment_intents.create.assert_not_called()

        StockReservation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(inv.expire_reservations(), 1)
        r = self.client.post(f"{self.base}/checkout/{mine.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)

    def test_cancelled_intent_releases_the_hold(self):
        from apps.inventory.services import reserved
        checkout = self.checkout(quantity=2)
        self.client_mock.v1.payment_intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(reserved(self.variant), 2)
        stripe_service.note_unpaid({"id": "pi_1", "status": "canceled", "metadata": {"checkout_id": str(checkout.pk)}}, ACCOUNT)
        self.assertEqual(reserved(self.variant), 0)

    def test_canceled_intent_of_another_account_cannot_release_my_hold(self):
        from apps.inventory.services import reserved
        checkout = self.checkout(quantity=2)
        self.client_mock.v1.payment_intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        stripe_service.note_unpaid(
            {"id": "pi_evil", "status": "canceled", "metadata": {"checkout_id": str(checkout.pk)}}, "acct_other"
        )
        self.assertEqual(reserved(self.variant), 2)

    def test_database_refuses_a_second_payment_for_the_same_intent(self):
        from django.db import IntegrityError, transaction
        checkout = self.checkout()
        order = stripe_service.finalize(intent(checkout), ACCOUNT)
        with self.assertRaises(IntegrityError), transaction.atomic():
            services.record_payment(self.store, order, order.total, provider="stripe", reference="pi_1")

    def test_paid_intent_places_one_captured_order_even_if_finalized_twice(self):
        checkout = self.checkout()
        paid = intent(checkout)
        first = stripe_service.finalize(paid, ACCOUNT)
        second = stripe_service.finalize(paid, ACCOUNT)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Order.objects.count(), 1)
        payment = Payment.objects.get()
        self.assertEqual((payment.provider, payment.status, payment.provider_payment_id), ("stripe", "captured", "pi_1"))
        self.assertEqual(services.paid_total(first), Decimal("998.00"))
        self.assertEqual(sellable(self.variant), 8)

    def test_paid_order_sends_one_confirmation_email(self):
        from django.core import mail

        checkout = self.checkout()
        with self.captureOnCommitCallbacks(execute=True):
            stripe_service.finalize(intent(checkout), ACCOUNT)
            stripe_service.finalize(intent(checkout), ACCOUNT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["a@b.com"])
        self.assertIn("Total:", mail.outbox[0].body)

    def test_refunded_payment_sends_no_confirmation(self):
        from django.core import mail

        checkout = self.checkout()
        with self.captureOnCommitCallbacks(execute=True):
            stripe_service.finalize(intent(checkout, amount=1), ACCOUNT)
        self.assertEqual(mail.outbox, [])

    def test_unpaid_intent_places_nothing(self):
        checkout = self.checkout()
        self.assertIsNone(stripe_service.finalize(intent(checkout, status="processing"), ACCOUNT))
        self.assertEqual(Order.objects.count(), 0)

    def test_amount_mismatch_is_refunded_not_ordered(self):
        checkout = self.checkout()
        self.assertIsNone(stripe_service.finalize(intent(checkout, amount=100), ACCOUNT))
        self.assertEqual(Order.objects.count(), 0)
        self.client_mock.v1.refunds.create.assert_called_once()

    def test_paid_but_out_of_stock_is_refunded(self):
        checkout = self.checkout(quantity=2)
        from apps.inventory.services import set_available
        set_available(self.variant, self.location, 0)
        self.assertIsNone(stripe_service.finalize(intent(checkout), ACCOUNT))
        self.assertEqual(Order.objects.count(), 0)
        self.client_mock.v1.refunds.create.assert_called_once()

    def test_intent_of_another_stores_account_is_ignored(self):
        checkout = self.checkout()
        self.assertIsNone(stripe_service.finalize(intent(checkout), "acct_other"))
        self.assertEqual(Order.objects.count(), 0)

    def test_complete_endpoint_cannot_place_a_stripe_order_without_paying(self):
        checkout = self.checkout()
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/complete/", {"payment_method": "stripe"}, format="json", **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_refund_goes_to_stripe_and_is_recorded(self):
        checkout = self.checkout()
        order = stripe_service.finalize(intent(checkout), ACCOUNT)
        self.client_mock.v1.refunds.create.return_value = SimpleNamespace(id="re_1")
        payment = Payment.objects.get()
        services.refund(payment, Decimal("100.00"), "oops")
        params, = self.client_mock.v1.refunds.create.call_args.args
        self.assertEqual((params["payment_intent"], params["amount"]), ("pi_1", 10000))
        self.assertEqual(payment.transactions.get(transaction_type="refund").provider_refund_id, "re_1")
        self.assertEqual(services.paid_total(order), Decimal("898.00"))

    def test_failed_stripe_refund_records_nothing(self):
        checkout = self.checkout()
        stripe_service.finalize(intent(checkout), ACCOUNT)
        self.client_mock.v1.refunds.create.side_effect = ValidationError("nope")
        payment = Payment.objects.get()
        with self.assertRaises(ValidationError):
            services.refund(payment, Decimal("100.00"))
        self.assertFalse(payment.transactions.filter(transaction_type="refund").exists())

    # ------------------------------------------------------------- webhook

    def post_event(self, event, signature_ok=True):
        target = "apps.payments.stripe_views.stripe.Webhook.construct_event"
        if signature_ok:
            return mock.patch(target, return_value=event)
        import stripe
        return mock.patch(target, side_effect=stripe.SignatureVerificationError("bad", "sig"))

    def webhook(self):
        return self.client.generic("POST", "/api/v1/payments/stripe/webhook/", b"{}", content_type="application/json")

    def test_webhook_rejects_a_bad_signature(self):
        with self.post_event({}, signature_ok=False):
            r = self.webhook()
        self.assertEqual(r.status_code, 400, r.content)

    def test_webhook_payment_succeeded_places_the_order(self):
        checkout = self.checkout()
        event = {"type": "payment_intent.succeeded", "account": ACCOUNT, "data": {"object": intent(checkout)}}
        with self.post_event(event):
            r = self.webhook()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Order.objects.count(), 1)

    def test_webhook_account_updated_enables_charges(self):
        self.store.stripe_charges_enabled = False
        self.store.save()
        event = {
            "type": "account.updated", "account": ACCOUNT,
            "data": {"object": {"id": ACCOUNT, "charges_enabled": True, "details_submitted": True}},
        }
        account = SimpleNamespace(charges_enabled=True, details_submitted=True)
        with self.post_event(event), mock.patch.object(stripe_service, "sync_account") as sync:
            self.webhook()
        self.assertEqual(sync.call_args.args[0].pk, self.store.pk)
        stripe_service.sync_account(self.store, account=account)
        self.store.refresh_from_db()
        self.assertTrue(self.store.stripe_charges_enabled)

    def paid(self):
        checkout = self.checkout()
        stripe_service.finalize(intent(checkout), ACCOUNT)
        return Payment.objects.get()

    def send(self, type, obj, account=ACCOUNT):
        with self.post_event({"type": type, "account": account, "data": {"object": obj}}):
            return self.webhook()

    def test_webhook_dashboard_refund_is_recorded_once(self):
        payment = self.paid()
        charge = {"payment_intent": "pi_1", "currency": "inr", "amount_refunded": 10000}
        self.assertEqual(self.send("charge.refunded", charge).status_code, 200)
        self.send("charge.refunded", charge)
        refunds = payment.transactions.filter(transaction_type="refund")
        self.assertEqual([r.amount for r in refunds], [Decimal("100.00")])
        payment.refresh_from_db()
        self.assertEqual(payment.status, services.PARTIALLY_REFUNDED)
        self.send("charge.refunded", {**charge, "amount_refunded": int(payment.amount * 100)})
        payment.refresh_from_db()
        self.assertEqual(payment.status, services.REFUNDED)
        self.assertEqual(services.refunded_total(payment), payment.amount)

    def test_webhook_refund_made_in_the_app_is_not_counted_twice(self):
        payment = self.paid()
        self.client_mock.v1.refunds.create.return_value = SimpleNamespace(id="re_1")
        services.refund(payment, Decimal("100.00"))
        self.send("charge.refunded", {"payment_intent": "pi_1", "currency": "inr", "amount_refunded": 10000})
        self.assertEqual(payment.transactions.filter(transaction_type="refund").count(), 1)

    def test_webhook_refund_of_unknown_payment_is_ignored(self):
        self.paid()
        r = self.send("charge.refunded", {"payment_intent": "pi_other", "currency": "inr", "amount_refunded": 100})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(services.PaymentTransaction.objects.filter(transaction_type="refund").exists())

    def test_webhook_dispute_is_recorded_and_updated(self):
        payment = self.paid()
        dispute = {"id": "dp_1", "payment_intent": "pi_1", "amount": 5000, "currency": "inr",
                   "status": "needs_response", "reason": "fraudulent"}
        self.send("charge.dispute.created", dispute)
        self.send("charge.dispute.created", dispute)
        self.send("charge.dispute.closed", {**dispute, "status": "lost"})
        rows = payment.transactions.filter(transaction_type="dispute")
        self.assertEqual(sorted(r.status for r in rows), ["lost", "needs_response"])
        payment.refresh_from_db()
        self.assertEqual(payment.metadata["dispute"], {"id": "dp_1", "status": "lost"})

    def test_webhook_failed_and_canceled_payments_change_nothing(self):
        for type in ("payment_intent.payment_failed", "payment_intent.canceled"):
            r = self.send(type, {"id": "pi_1", "status": "requires_payment_method",
                                 "last_payment_error": {"message": "declined"}})
            self.assertEqual(r.status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    def active_holds(self, checkout):
        from apps.inventory.models import StockReservation

        return StockReservation.objects.filter(checkout_id=checkout.pk, status=StockReservation.ACTIVE).count()

    def started(self):
        """A checkout whose card payment has started: the stock is held while the shopper pays."""
        checkout = self.checkout()
        self.client_mock.v1.payment_intents.create.return_value = intent(checkout, status="requires_payment_method")
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertGreater(self.active_holds(checkout), 0)
        return checkout

    def unpaid(self, checkout, status, account=ACCOUNT):
        return stripe_service.note_unpaid(
            {"id": "pi_1", "status": status, "last_payment_error": {"message": "declined"},
             "metadata": {"checkout_id": str(checkout.pk)}},
            account,
        )

    def test_a_declined_attempt_keeps_the_stock_hold_so_the_shopper_can_retry(self):
        checkout = self.started()
        held = self.active_holds(checkout)
        self.unpaid(checkout, "requires_payment_method")
        self.assertEqual(self.active_holds(checkout), held)

    def test_a_retry_after_a_decline_on_the_same_intent_places_the_order_once(self):
        checkout = self.started()
        self.unpaid(checkout, "requires_payment_method")
        self.assertEqual(Order.objects.count(), 0)
        stripe_service.finalize(intent(checkout), ACCOUNT)
        stripe_service.finalize(intent(checkout), ACCOUNT)  # Stripe redelivers the success
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (1, 1))

    def test_a_cancelled_intent_gives_the_stock_hold_back(self):
        checkout = self.started()
        self.unpaid(checkout, "canceled")
        self.assertEqual(self.active_holds(checkout), 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_cancelled_intent_from_another_account_cannot_release_this_stores_stock(self):
        checkout = self.started()
        held = self.active_holds(checkout)
        with self.assertLogs("apps.payments.stripe_service", "ERROR"):
            self.unpaid(checkout, "canceled", account="acct_other")
        self.assertEqual(self.active_holds(checkout), held)

    def test_webhook_deauthorization_disconnects_the_store(self):
        r = self.send("account.application.deauthorized", {"id": "ca_1"})
        self.assertEqual(r.status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(self.store.stripe_account_id, "")
        self.assertFalse(self.store.stripe_charges_enabled)

    def test_failed_automatic_refund_surfaces_so_stripe_retries(self):
        checkout = self.checkout()
        self.client_mock.v1.refunds.create.side_effect = RuntimeError("stripe down")
        with self.assertRaises(RuntimeError), self.assertLogs("apps.payments.stripe_service", "ERROR"):
            stripe_service.finalize(intent(checkout, amount=1), ACCOUNT)

    # Real signature checking: nothing mocked, signed the way Stripe signs (t=<ts>,v1=HMAC-SHA256("<ts>.<body>")).
    # The mocked tests above hand the view plain dicts; the real library hands it Stripe objects (no .get()).

    def signed(self, payload, secret="whsec", timestamp=None):
        timestamp = int(time.time()) if timestamp is None else timestamp
        digest = hmac.new(secret.encode(), f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={digest}"

    def post_signed(self, payload, header):
        extra = {"HTTP_STRIPE_SIGNATURE": header} if header is not None else {}
        return self.client.generic(
            "POST", "/api/v1/payments/stripe/webhook/", payload.encode(), content_type="application/json", **extra
        )

    def real_event(self, type, obj, account=ACCOUNT):
        return json.dumps({"id": f"evt_{type}_{obj.get('id')}", "object": "event", "type": type, "account": account, "data": {"object": obj}})

    def paid_intent_object(self, checkout):
        paid = intent(checkout)
        return {"id": paid.id, "object": "payment_intent", "status": "succeeded", "amount": paid.amount,
                "currency": paid.currency, "metadata": paid.metadata}

    def test_webhook_accepts_a_genuinely_signed_event_and_places_the_order(self):
        payload = self.real_event("payment_intent.succeeded", self.paid_intent_object(self.checkout()))
        r = self.post_signed(payload, self.signed(payload))
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.post_signed(payload, self.signed(payload)).status_code, 200)  # Stripe retries
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (1, 1))

    def test_webhook_rejects_wrong_secret_tampered_body_missing_and_stale_signatures(self):
        payload = self.real_event("payment_intent.succeeded", self.paid_intent_object(self.checkout()))
        cases = {
            "wrong secret": (payload, self.signed(payload, secret="whsec_other")),
            "tampered body": (payload.replace("99800", "1"), self.signed(payload)),
            "no header": (payload, None),
            "garbage header": (payload, "nonsense"),
            "stale timestamp": (payload, self.signed(payload, timestamp=int(time.time()) - 3600)),
        }
        for name, (body, header) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.post_signed(body, header).status_code, 400)
        self.assertEqual(Order.objects.count(), 0)

    def test_webhook_refuses_everything_when_no_secret_is_configured(self):
        with override_settings(STRIPE_WEBHOOK_SECRET=""):
            r = self.post_signed("{}", self.signed("{}", secret=""))
        self.assertEqual(r.status_code, 503)

    def test_every_handled_event_type_survives_a_real_signed_delivery(self):
        payment = self.paid()
        events = [
            ("charge.refunded", {"id": "ch_1", "payment_intent": "pi_1", "currency": "inr", "amount_refunded": 10000}),
            ("charge.dispute.created", {"id": "dp_1", "payment_intent": "pi_1", "amount": 5000, "currency": "inr",
                                        "status": "needs_response", "reason": "fraudulent"}),
            ("charge.dispute.closed", {"id": "dp_1", "payment_intent": "pi_1", "amount": 5000, "currency": "inr",
                                       "status": "lost", "reason": "fraudulent"}),
            ("payment_intent.payment_failed", {"id": "pi_9", "status": "requires_payment_method",
                                               "last_payment_error": {"message": "declined"}}),
            ("payment_intent.canceled", {"id": "pi_9", "status": "canceled"}),
            ("account.updated", {"id": ACCOUNT, "charges_enabled": True, "details_submitted": True}),
        ]
        for type, obj in events:
            with self.subTest(type):
                payload = self.real_event(type, obj)
                r = self.post_signed(payload, self.signed(payload))
                self.assertEqual(r.status_code, 200, r.content)
        payment.refresh_from_db()
        self.assertEqual(services.refunded_total(payment), Decimal("100.00"))
        self.assertEqual(payment.metadata["dispute"], {"id": "dp_1", "status": "lost"})

        payload = self.real_event("account.application.deauthorized", {"id": "ca_1"})
        self.assertEqual(self.post_signed(payload, self.signed(payload)).status_code, 200)
        self.store.refresh_from_db()
        self.assertEqual(self.store.stripe_account_id, "")

    # ------------------------------------------------ checkout security and idempotency

    STRANGER = {"HTTP_X_CART_SESSION": "someone-elses-session-0000"}

    def test_stranger_session_cannot_start_or_confirm_my_stripe_payment(self):
        checkout = self.checkout()
        for suffix in ("stripe/", "stripe/confirm/"):
            r = self.client.post(f"{self.base}/checkout/{checkout.pk}/{suffix}", **self.STRANGER)
            self.assertEqual(r.status_code, 404, suffix)
            self.assertEqual(self.client.post(f"{self.base}/checkout/{checkout.pk}/{suffix}").status_code, 400, suffix)
        self.client_mock.v1.payment_intents.create.assert_not_called()
        self.client_mock.v1.payment_intents.retrieve.assert_not_called()

    def test_stripe_checkout_routes_accept_post_only(self):
        checkout = self.checkout()
        for suffix in ("stripe/", "stripe/confirm/"):
            for method in ("get", "patch", "put", "delete"):
                r = getattr(self.client, method)(f"{self.base}/checkout/{checkout.pk}/{suffix}", **self.h)
                self.assertEqual(r.status_code, 405, f"{method} {suffix}")

    def test_confirm_without_a_started_payment_or_before_paying_places_nothing(self):
        checkout = self.checkout()
        url = f"{self.base}/checkout/{checkout.pk}/stripe/confirm/"
        self.assertEqual(self.client.post(url, **self.h).status_code, 400)
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        intents.retrieve.return_value = intent(checkout, status="requires_payment_method")
        self.assertEqual(self.client.post(url, **self.h).status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_confirm_twice_then_webhook_still_makes_exactly_one_order(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        intents.retrieve.return_value = intent(checkout)
        url = f"{self.base}/checkout/{checkout.pk}/stripe/confirm/"
        first, second = self.client.post(url, **self.h), self.client.post(url, **self.h)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(self.send("payment_intent.succeeded", intent(checkout)).status_code, 200)
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (1, 1))
        self.assertEqual(sellable(self.variant), 8)
        self.client_mock.v1.refunds.create.assert_not_called()

    def test_money_paid_for_an_already_completed_checkout_is_refunded_not_double_ordered(self):
        checkout = self.checkout()
        self.client.post(f"{self.base}/checkout/{checkout.pk}/complete/", {"payment_method": "cod"}, format="json", **self.h)
        self.assertEqual(Order.objects.count(), 1)
        self.assertIsNone(stripe_service.finalize(intent(checkout), ACCOUNT))
        self.assertEqual(Order.objects.count(), 1)
        self.client_mock.v1.refunds.create.assert_called_once()

    def test_stripe_outage_is_a_clean_502_and_places_nothing(self):
        import stripe
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.side_effect = stripe.APIConnectionError("secret internal detail")
        with self.assertLogs("apps.payments.stripe_views", "ERROR"):
            r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("secret internal detail", r.content.decode())

        intents.create.side_effect = None
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        intents.retrieve.side_effect = stripe.APIConnectionError("down")
        with self.assertLogs("apps.payments.stripe_views", "ERROR"):
            r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/confirm/", **self.h)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(Order.objects.count(), 0)

    def test_stripe_requests_carry_stable_idempotency_keys(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertIn(str(checkout.pk), intents.create.call_args.kwargs["options"]["idempotency_key"])

        refunds = self.client_mock.v1.refunds
        refunds.create.return_value = SimpleNamespace(id="re_1")
        stripe_service.finalize(intent(checkout, amount=100), ACCOUNT)  # mismatch: refunded
        stripe_service.finalize(intent(checkout, amount=100), ACCOUNT)  # webhook retry: same refund key
        keys = [c.kwargs["options"]["idempotency_key"] for c in refunds.create.call_args_list]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_partial_refunds_use_distinct_keys_and_a_repeat_of_one_reuses_its_key(self):
        payment = self.paid()
        refunds = self.client_mock.v1.refunds
        refunds.create.return_value = SimpleNamespace(id="re_1")
        stripe_service.refund_payment(payment, Decimal("100.00"), Decimal("0.00"))
        stripe_service.refund_payment(payment, Decimal("100.00"), Decimal("0.00"))  # the same request retried
        stripe_service.refund_payment(payment, Decimal("100.00"), Decimal("100.00"))  # a second, later refund
        keys = [c.kwargs["options"]["idempotency_key"] for c in refunds.create.call_args_list]
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[1], keys[2])

    def test_confirm_endpoint_asks_stripe_and_places_the_order(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        intents.retrieve.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)

        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/confirm/", **self.h)
        self.assertEqual(r.status_code, 409)

        intents.retrieve.return_value = intent(checkout)
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/confirm/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Order.objects.count(), 1)
