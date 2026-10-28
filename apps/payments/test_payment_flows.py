"""
End-to-end payment flows beyond the Stripe happy paths: cash on delivery, partial and repeated
refunds, replayed webhooks, and money that arrives in odd states. Stripe itself is mocked.

    python manage.py test apps.payments.test_payment_flows --settings=config.test_settings
"""
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings
from rest_framework.exceptions import ValidationError

from apps.orders.models import Order

from apps.commerce import tests as base_tests
from apps.commerce.models import Checkout

from . import services, stripe_service
from .models import Payment
from .test_stripe import ACCOUNT, intent


@override_settings(ROOT_URLCONF="apps.payments.test_stripe", STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class PaymentFlowTests(base_tests.CommerceTestBase):
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

    def post_event(self, event):
        return mock.patch("apps.payments.stripe_views.stripe.Webhook.construct_event", return_value=event)

    def webhook(self):
        return self.client.generic("POST", "/api/v1/payments/stripe/webhook/", b"{}", content_type="application/json")

    def paid_order(self):
        checkout = self.checkout()
        order = stripe_service.finalize(intent(checkout), ACCOUNT)
        return order, Payment.objects.get(order=order)

    def event(self, kind, obj, account=ACCOUNT):
        event = {"type": kind, "data": {"object": obj}}
        if account:
            event["account"] = account
        return event

    # --------------------------------------------------------- cash on delivery

    def test_cod_order_waits_for_cash_then_captures_and_refunds_without_stripe(self):
        checkout = self.checkout()
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/complete/", {"payment_method": "cod"}, format="json", **self.h)
        self.assertEqual(r.status_code, 201, r.data)
        order = Order.objects.get()
        payment = Payment.objects.get()
        self.assertEqual((payment.provider, payment.status), ("cod", "pending"))
        self.assertEqual(services.paid_total(order), Decimal("0.00"))

        services.capture_payment(payment)
        self.assertEqual(services.paid_total(order), Decimal("998.00"))
        with self.assertRaises(ValidationError):
            services.capture_payment(payment)

        services.refund(payment, Decimal("998.00"))
        self.assertEqual(services.paid_total(order), Decimal("0.00"))
        self.client_mock.v1.refunds.create.assert_not_called()

    def test_cancelling_an_order_voids_its_pending_cod_payment(self):
        checkout = self.checkout()
        self.client.post(f"{self.base}/checkout/{checkout.pk}/complete/", {"payment_method": "cod"}, format="json", **self.h)
        order = Order.objects.get()
        services.cancel_pending(order)
        self.assertEqual(Payment.objects.get().status, "cancelled")

    # ------------------------------------------------------------------ refunds

    def test_partial_refunds_add_up_and_cannot_exceed_the_payment(self):
        order, payment = self.paid_order()
        self.client_mock.v1.refunds.create.side_effect = [SimpleNamespace(id="re_1"), SimpleNamespace(id="re_2"), SimpleNamespace(id="re_3")]

        services.refund(payment, Decimal("300.00"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, "partially_refunded")
        services.refund(payment, Decimal("300.00"))
        self.assertEqual(services.paid_total(order), Decimal("398.00"))

        with self.assertRaises(ValidationError):
            services.refund(payment, Decimal("398.01"))
        with self.assertRaises(ValidationError):
            services.refund(payment, Decimal("0"))

        services.refund(payment, Decimal("398.00"))
        payment.refresh_from_db()
        self.assertEqual(payment.status, "refunded")
        self.assertEqual(services.paid_total(order), Decimal("0.00"))
        with self.assertRaises(ValidationError):
            services.refund(payment, Decimal("1.00"))

    def test_each_partial_refund_gets_its_own_idempotency_key(self):
        _, payment = self.paid_order()
        self.client_mock.v1.refunds.create.side_effect = [SimpleNamespace(id="re_1"), SimpleNamespace(id="re_2")]
        services.refund(payment, Decimal("100.00"))
        services.refund(payment, Decimal("100.00"))
        keys = [c.kwargs["options"]["idempotency_key"] for c in self.client_mock.v1.refunds.create.call_args_list]
        self.assertEqual(len(set(keys)), 2)

    def test_refund_fails_cleanly_once_the_store_disconnected_stripe(self):
        _, payment = self.paid_order()
        self.store.stripe_account_id = ""
        self.store.save()
        payment = Payment.objects.get(pk=payment.pk)
        with self.assertRaises(ValidationError):
            services.refund(payment, Decimal("10.00"))
        self.assertFalse(payment.transactions.filter(transaction_type="refund").exists())

    def test_full_refund_from_the_stripe_dashboard_marks_the_payment_refunded(self):
        order, payment = self.paid_order()
        charge = {"payment_intent": "pi_1", "amount_refunded": 99800, "currency": "inr"}
        with self.post_event(self.event("charge.refunded", charge)):
            self.assertEqual(self.webhook().status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, "refunded")
        self.assertEqual(services.paid_total(order), Decimal("0.00"))

    # ----------------------------------------------------------------- webhooks

    def test_replayed_succeeded_webhook_never_duplicates_the_order_or_payment(self):
        checkout = self.checkout()
        event = self.event("payment_intent.succeeded", intent(checkout))
        for _ in range(3):
            with self.post_event(event):
                self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        self.client_mock.v1.refunds.create.assert_not_called()

    def test_shopper_return_after_the_webhook_gets_the_same_order(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        with self.post_event(self.event("payment_intent.succeeded", intent(checkout))):
            self.webhook()
        placed = Order.objects.get()
        intents.retrieve.return_value = intent(checkout)
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/confirm/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(str(r.data["id"]), str(placed.pk))
        self.assertEqual(Order.objects.count(), 1)

    def test_succeeded_event_without_an_account_is_acknowledged_and_ignored(self):
        checkout = self.checkout()
        with self.post_event(self.event("payment_intent.succeeded", intent(checkout), account=None)):
            self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(Order.objects.count(), 0)

    def test_unknown_event_types_are_acknowledged(self):
        with self.post_event(self.event("customer.created", {})):
            self.assertEqual(self.webhook().status_code, 200)

    @override_settings(STRIPE_WEBHOOK_SECRET="")
    def test_webhook_is_unavailable_until_a_signing_secret_is_configured(self):
        self.assertEqual(self.webhook().status_code, 503)

    def test_failed_refund_during_a_webhook_surfaces_so_stripe_retries(self):
        checkout = self.checkout()
        self.client_mock.v1.refunds.create.side_effect = RuntimeError("stripe down")
        event = self.event("payment_intent.succeeded", intent(checkout, amount=1))
        self.client.raise_request_exception = False
        with self.post_event(event), self.assertLogs("apps.payments.stripe_service", "ERROR"):
            self.assertEqual(self.webhook().status_code, 500)
        self.assertEqual(Order.objects.count(), 0)

    # ----------------------------------------------------------- odd conditions

    def test_wrong_currency_is_refunded_not_ordered(self):
        checkout = self.checkout()
        self.assertIsNone(stripe_service.finalize(intent(checkout, currency="usd"), ACCOUNT))
        self.assertEqual(Order.objects.count(), 0)
        self.client_mock.v1.refunds.create.assert_called_once()

    def test_a_second_payment_for_a_completed_checkout_is_refunded(self):
        checkout = self.checkout()
        first = stripe_service.finalize(intent(checkout), ACCOUNT)
        self.assertIsNotNone(first)
        self.assertIsNone(stripe_service.finalize(intent(checkout, id="pi_2"), ACCOUNT))
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Payment.objects.count(), 1)
        params, = self.client_mock.v1.refunds.create.call_args.args
        self.assertEqual(params["payment_intent"], "pi_2")

    def test_cannot_start_a_card_payment_for_an_already_paid_checkout(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        intents.retrieve.return_value = intent(checkout)
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 400)

    def test_a_canceled_intent_is_replaced_with_a_new_one(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        intents.retrieve.return_value = intent(checkout, status="canceled")
        intents.create.return_value = intent(checkout, status="requires_payment_method", id="pi_2")
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(intents.create.call_count, 2)
        checkout.refresh_from_db()
        self.assertEqual(checkout.metadata["stripe_payment_intent"], "pi_2")

    def test_confirm_without_a_started_payment_is_rejected(self):
        checkout = self.checkout()
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/confirm/", **self.h)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
