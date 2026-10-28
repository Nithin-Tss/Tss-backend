"""
Stripe Connect payments, with Stripe itself mocked.

    python manage.py test apps.payments.test_stripe --settings=config.test_settings
"""
import json
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
        self.assertEqual(r.data["stripe_account"], ACCOUNT)
        params, = intents.create.call_args.args
        self.assertEqual((params["amount"], params["currency"]), (99800, "inr"))
        self.assertEqual(intents.create.call_args.kwargs["options"]["stripe_account"], ACCOUNT)

        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(intents.create.call_count, 1)

    def test_store_without_stripe_cannot_start_a_payment(self):
        self.store.stripe_charges_enabled = False
        self.store.save()
        checkout = self.checkout()
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 400)

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
