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
