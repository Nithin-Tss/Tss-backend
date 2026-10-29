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

from apps.inventory.services import sellable
from apps.orders import services as orders_services
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


@override_settings(ROOT_URLCONF="apps.payments.test_stripe", STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class CancelPaidOrderTests(base_tests.CommerceTestBase):
    """Staff cancelling a paid order: the money goes back first, and only then is the order cancelled."""

    base = base_tests.PlaceOrderTests.base
    h = base_tests.PlaceOrderTests.h
    address = base_tests.PlaceOrderTests.address
    MESSAGE = "The order was not cancelled because the refund failed."

    def setUp(self):
        super().setUp()
        self.store.stripe_account_id, self.store.stripe_charges_enabled = ACCOUNT, True
        self.store.save()
        self.client_mock = mock.MagicMock()
        self.client_mock.v1.refunds.create.return_value = SimpleNamespace(id="re_1")
        patcher = mock.patch.object(stripe_service, "client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def checkout(self, quantity=2):
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity}, format="json", **self.h)
        r = self.client.post(
            f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": self.address}, format="json", **self.h
        )
        return Checkout.objects.get(pk=r.data["id"])

    def stripe_order(self):
        order = stripe_service.finalize(intent(self.checkout()), ACCOUNT)
        return order, Payment.objects.get(order=order)

    def cod_order(self):
        checkout = self.checkout()
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/complete/", {"payment_method": "cod"}, format="json", **self.h)
        self.assertEqual(r.status_code, 201, r.data)
        return Order.objects.get(), Payment.objects.get()

    def refunds(self, payment):
        return list(payment.transactions.filter(transaction_type="refund"))

    def test_a_paid_manual_order_is_refunded_then_cancelled_and_stock_goes_back(self):
        order, payment = self.cod_order()
        total = order.total
        services.capture_payment(payment)
        self.assertEqual(sellable(self.variant), 8)

        orders_services.cancel_order(order)

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, order.total), ("cancelled", total))
        self.assertEqual(payment.status, "refunded")
        refund, = self.refunds(payment)
        self.assertEqual((refund.amount, refund.reason), (Decimal("998.00"), "Order cancelled"))
        self.assertEqual(services.paid_total(order), Decimal("0.00"))
        self.assertEqual(sellable(self.variant), 10)
        self.client_mock.v1.refunds.create.assert_not_called()  # a manual payment never goes to Stripe

    def test_only_what_is_still_unrefunded_is_refunded(self):
        order, payment = self.stripe_order()
        services.refund(payment, Decimal("300.00"))
        self.client_mock.v1.refunds.create.reset_mock()

        orders_services.cancel_order(order)

        params, = self.client_mock.v1.refunds.create.call_args.args
        self.assertEqual((params["payment_intent"], params["amount"]), ("pi_1", 69800))
        payment.refresh_from_db()
        self.assertEqual(payment.status, "refunded")
        self.assertEqual(sum(r.amount for r in self.refunds(payment)), Decimal("998.00"))

    def test_a_stripe_order_is_refunded_at_stripe_once_and_then_cancelled(self):
        order, payment = self.stripe_order()

        orders_services.cancel_order(order)

        self.client_mock.v1.refunds.create.assert_called_once()
        params, = self.client_mock.v1.refunds.create.call_args.args
        self.assertEqual((params["payment_intent"], params["amount"]), ("pi_1", 99800))
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("cancelled", "refunded"))
        self.assertEqual(self.refunds(payment)[0].provider_refund_id, "re_1")
        self.assertEqual(sellable(self.variant), 10)

    def test_a_failed_refund_leaves_the_order_open_and_the_stock_committed(self):
        import stripe

        order, payment = self.stripe_order()
        self.client_mock.v1.refunds.create.side_effect = stripe.StripeError("boom")

        with self.assertRaises(ValidationError) as raised:
            orders_services.cancel_order(order)

        detail = raised.exception.detail
        self.assertIn("Stripe refused the refund", str(detail["payment"]))  # the existing text, unchanged
        self.assertEqual(str(detail["order"]), self.MESSAGE)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("open", "captured"))
        self.assertEqual(self.refunds(payment), [])
        self.assertEqual(sellable(self.variant), 8)  # still committed

    def test_a_store_that_left_stripe_cannot_cancel_a_stripe_paid_order(self):
        order, payment = self.stripe_order()
        self.store.stripe_account_id = ""
        self.store.save()

        with self.assertRaises(ValidationError) as raised:
            orders_services.cancel_order(order)

        self.assertIn("This store is no longer connected to Stripe.", str(raised.exception.detail))
        self.assertEqual(str(raised.exception.detail["order"]), self.MESSAGE)
        order.refresh_from_db()
        self.assertEqual(order.status, "open")

    def test_cancelling_twice_refunds_and_releases_once(self):
        order, payment = self.stripe_order()
        orders_services.cancel_order(order)

        with self.assertRaises(ValidationError) as raised:
            orders_services.cancel_order(order)

        self.assertIn("This order is already cancelled.", str(raised.exception))
        self.assertEqual(self.client_mock.v1.refunds.create.call_count, 1)
        self.assertEqual(len(self.refunds(payment)), 1)
        self.assertEqual(sellable(self.variant), 10)  # not 12

    def test_refund_is_taken_before_stock_is_released(self):
        order, payment = self.stripe_order()
        calls = []
        real_refund, real_release = services.refund, orders_services.inventory.release
        with mock.patch.object(services, "refund", side_effect=lambda *a, **k: (calls.append("refund"), real_refund(*a, **k))[1]), \
             mock.patch.object(orders_services.inventory, "release", side_effect=lambda *a, **k: (calls.append("release"), real_release(*a, **k))[1]):
            orders_services.cancel_order(order)
        self.assertEqual(calls, ["refund", "release"])

    def test_if_the_cancel_fails_after_the_refund_a_retry_asks_stripe_with_the_same_key(self):
        order, payment = self.stripe_order()
        with mock.patch.object(orders_services.inventory, "release", side_effect=ValidationError("stock problem")):
            with self.assertRaises(ValidationError):
                orders_services.cancel_order(order)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("open", "captured"))  # rolled back together
        self.assertEqual(self.refunds(payment), [])

        orders_services.cancel_order(order)

        keys = [c.kwargs["options"]["idempotency_key"] for c in self.client_mock.v1.refunds.create.call_args_list]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])  # Stripe returns the first refund instead of making a second one
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_if_the_refund_was_already_recorded_by_the_webhook_a_retry_does_not_ask_stripe_again(self):
        order, payment = self.stripe_order()
        with mock.patch.object(orders_services.inventory, "release", side_effect=ValidationError("stock problem")):
            with self.assertRaises(ValidationError):
                orders_services.cancel_order(order)
        self.assertEqual(self.client_mock.v1.refunds.create.call_count, 1)
        services.record_external_refund(payment, Decimal("998.00"))  # Stripe's charge.refunded event

        # The full refund already cancelled the order and put its stock back, so a retry has nothing left to do
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(sellable(self.variant), 10)
        with self.assertRaises(ValidationError) as raised:
            orders_services.cancel_order(order)
        self.assertIn("This order is already cancelled.", str(raised.exception))
        self.assertEqual(self.client_mock.v1.refunds.create.call_count, 1)  # Stripe was not asked again
        self.assertEqual(sum(r.amount for r in self.refunds(payment)), Decimal("998.00"))
        self.assertEqual(sellable(self.variant), 10)  # and the stock was not put back twice

    def test_an_order_with_shipped_items_is_still_refused_and_nothing_is_refunded(self):
        order, payment = self.stripe_order()
        Order.objects.filter(pk=order.pk).update(fulfillment_status="partial")

        with self.assertRaises(ValidationError) as raised:
            orders_services.cancel_order(order)

        self.assertIn("Orders with shipped items can't be cancelled.", str(raised.exception))
        self.client_mock.v1.refunds.create.assert_not_called()
        payment.refresh_from_db()
        self.assertEqual(payment.status, "captured")

    def test_the_shopper_cancel_of_a_paid_order_is_still_refused_without_a_refund(self):
        order, payment = self.cod_order()
        services.capture_payment(payment)

        r = self.client.post(f"{self.base}/orders/{order.pk}/cancel/", **self.h)

        self.assertEqual(r.status_code, 400)
        self.assertIn("This order has been paid. Please contact the store to cancel it.", str(r.data))
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("open", "captured"))
        self.assertEqual(self.refunds(payment), [])
        self.assertEqual(sellable(self.variant), 8)

    def test_an_unpaid_cod_order_is_cancelled_as_before_with_nothing_refunded(self):
        order, payment = self.cod_order()

        orders_services.cancel_order(order)

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("cancelled", "cancelled"))  # the pending payment is voided
        self.assertEqual(self.refunds(payment), [])
        self.assertEqual(sellable(self.variant), 10)

    def test_every_payment_on_the_order_is_refunded_and_pending_ones_are_voided(self):
        order, pending = self.cod_order()
        first = services.record_payment(self.store, order, Decimal("300.00"))
        second = services.record_payment(self.store, order, Decimal("200.00"))

        orders_services.cancel_order(order)

        for payment in (first, second):
            payment.refresh_from_db()
            self.assertEqual(payment.status, "refunded")
        pending.refresh_from_db()
        self.assertEqual(pending.status, "cancelled")
        self.assertEqual(services.paid_total(order), Decimal("0.00"))
        self.assertEqual(sellable(self.variant), 10)


@override_settings(ROOT_URLCONF="apps.payments.test_stripe", STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class RefundRestockTests(base_tests.CommerceTestBase):
    """A refund that returns all the money puts the order's unshipped stock back, once, and cancels an unshipped order."""

    base = base_tests.PlaceOrderTests.base
    h = base_tests.PlaceOrderTests.h
    address = base_tests.PlaceOrderTests.address

    def setUp(self):
        super().setUp()
        self.store.stripe_account_id, self.store.stripe_charges_enabled = ACCOUNT, True
        self.store.save()
        self.client_mock = mock.MagicMock()
        self.client_mock.v1.refunds.create.return_value = SimpleNamespace(id="re_1")
        patcher = mock.patch.object(stripe_service, "client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def checkout(self, quantity=2):
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity}, format="json", **self.h)
        r = self.client.post(
            f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": self.address}, format="json", **self.h
        )
        return Checkout.objects.get(pk=r.data["id"])

    def stripe_order(self):
        order = stripe_service.finalize(intent(self.checkout()), ACCOUNT)
        return order, Payment.objects.get(order=order)

    def levels(self):
        from apps.inventory.models import InventoryItem

        item = InventoryItem.objects.get(variant=self.variant)
        return item.available, item.committed

    def released_rows(self):
        from apps.inventory.models import InventoryHistory

        return InventoryHistory.objects.filter(reason="order_cancelled").count()

    def test_a_full_refund_of_an_unshipped_order_cancels_it_and_puts_the_stock_back_once(self):
        order, payment = self.stripe_order()
        self.assertEqual(self.levels(), (8, 2))

        services.refund(payment, Decimal("998.00"))

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertIn("stock_released", order.metadata)
        self.assertEqual(self.levels(), (10, 0))
        self.assertEqual(self.released_rows(), 1)
        with self.assertRaises(ValidationError):
            services.refund(payment, Decimal("1.00"))  # nothing left to refund
        with self.assertRaises(ValidationError) as raised:
            orders_services.cancel_order(order)
        self.assertIn("This order is already cancelled.", str(raised.exception))
        self.assertEqual(self.levels(), (10, 0))
        self.assertEqual(self.released_rows(), 1)

    def test_a_partial_refund_changes_no_stock_and_does_not_cancel(self):
        order, payment = self.stripe_order()

        services.refund(payment, Decimal("300.00"))

        order.refresh_from_db()
        self.assertEqual(order.status, "open")
        self.assertNotIn("stock_released", order.metadata)
        self.assertEqual(self.levels(), (8, 2))

    def test_refunding_the_rest_after_a_partial_refund_settles_the_order_once(self):
        order, payment = self.stripe_order()
        services.refund(payment, Decimal("300.00"))

        services.refund(payment, Decimal("698.00"))

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.levels(), (10, 0))
        self.assertEqual(self.released_rows(), 1)

    def test_a_refund_recorded_from_the_stripe_webhook_settles_the_order_too(self):
        order, payment = self.stripe_order()

        stripe_service.record_charge_refunds(
            {"payment_intent": "pi_1", "amount_refunded": 99800, "currency": "inr"}, ACCOUNT
        )
        stripe_service.record_charge_refunds(  # Stripe repeats the event
            {"payment_intent": "pi_1", "amount_refunded": 99800, "currency": "inr"}, ACCOUNT
        )

        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(self.levels(), (10, 0))
        self.assertEqual(self.released_rows(), 1)

    def test_a_partial_webhook_refund_changes_no_stock(self):
        order, payment = self.stripe_order()
        stripe_service.record_charge_refunds(
            {"payment_intent": "pi_1", "amount_refunded": 30000, "currency": "inr"}, ACCOUNT
        )
        order.refresh_from_db()
        self.assertEqual((order.status, self.levels()), ("open", (8, 2)))

    def test_a_paid_cash_on_delivery_order_refunded_in_full_is_settled(self):
        checkout = self.checkout()
        self.client.post(f"{self.base}/checkout/{checkout.pk}/complete/", {"payment_method": "cod"}, format="json", **self.h)
        order, payment = Order.objects.get(), Payment.objects.get()
        services.capture_payment(payment)

        services.refund(payment, Decimal("998.00"))

        order.refresh_from_db()
        self.assertEqual((order.status, self.levels()), ("cancelled", (10, 0)))

    def test_a_partly_shipped_order_gets_only_its_unshipped_units_back_and_stays_open(self):
        from apps.fulfillment import services as fulfillment

        order, payment = self.stripe_order()
        item = order.items.get()
        fulfillment.fulfill(order, lines=[{"order_item": item, "quantity": 1}])
        self.assertEqual(self.levels(), (8, 1))  # one unit shipped, one still committed

        services.refund(payment, Decimal("998.00"))

        order.refresh_from_db()
        self.assertEqual(order.status, "open")
        self.assertIn("stock_released", order.metadata)
        self.assertEqual(self.levels(), (9, 0))  # only the unshipped unit is back; the shipped one is with the customer
        with self.assertRaises(ValidationError) as raised:
            fulfillment.fulfill(order, lines=[{"order_item": item, "quantity": 1}])
        self.assertIn("can't be fulfilled", str(raised.exception))

    def test_a_fully_shipped_order_gets_no_stock_back_from_a_refund(self):
        from apps.fulfillment import services as fulfillment

        order, payment = self.stripe_order()
        fulfillment.fulfill(order)
        self.assertEqual(self.levels(), (8, 0))

        services.refund(payment, Decimal("998.00"))

        order.refresh_from_db()
        self.assertEqual(order.status, "open")
        self.assertEqual(self.levels(), (8, 0))
        self.assertEqual(self.released_rows(), 0)

    def test_the_order_row_is_locked_before_the_payment_row(self):
        from .models import Payment as PaymentModel

        order, payment = self.stripe_order()
        calls = []
        real_lock_order = services._lock_order_of
        real_select = PaymentModel.objects.select_for_update

        def lock_order(p):
            calls.append("order")
            return real_lock_order(p)

        def select(*args, **kwargs):
            calls.append("payment")
            return real_select(*args, **kwargs)

        with mock.patch.object(services, "_lock_order_of", side_effect=lock_order), \
             mock.patch.object(PaymentModel.objects, "select_for_update", side_effect=select):
            services.refund(payment, Decimal("100.00"))
            self.assertEqual(calls[:2], ["order", "payment"])
            calls.clear()
            services.record_external_refund(payment, Decimal("150.00"))
            self.assertEqual(calls[:2], ["order", "payment"])


@override_settings(ROOT_URLCONF="apps.payments.test_stripe", STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class AutoRefundRecordTests(base_tests.CommerceTestBase):
    """Money taken for an order that could not be placed is refunded AND recorded on the checkout, so a failure is seen."""

    base = base_tests.PlaceOrderTests.base
    h = base_tests.PlaceOrderTests.h
    address = base_tests.PlaceOrderTests.address

    def setUp(self):
        super().setUp()
        self.store.stripe_account_id, self.store.stripe_charges_enabled = ACCOUNT, True
        self.store.save()
        self.client_mock = mock.MagicMock()
        self.client_mock.v1.refunds.create.return_value = SimpleNamespace(id="re_auto")
        patcher = mock.patch.object(stripe_service, "client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def checkout(self, quantity=2):
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity}, format="json", **self.h)
        r = self.client.post(
            f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": self.address}, format="json", **self.h
        )
        return Checkout.objects.get(pk=r.data["id"])

    def sell_out(self):
        from apps.inventory.services import set_available

        set_available(self.variant, self.location, 0)

    def recorded(self, checkout):
        checkout.refresh_from_db()
        return (checkout.metadata or {}).get("auto_refunds", [])

    def test_a_paid_but_out_of_stock_checkout_records_the_refund(self):
        checkout = self.checkout()
        self.sell_out()

        self.assertIsNone(stripe_service.finalize(intent(checkout), ACCOUNT))

        entry, = self.recorded(checkout)
        self.assertEqual((entry["payment_intent"], entry["amount"], entry["currency"]), ("pi_1", "998.00", "INR"))
        self.assertEqual((entry["status"], entry["refund_id"], entry["error"], entry["attempts"]), ("refunded", "re_auto", None, 1))
        self.assertTrue("left" in entry["reason"] or "sold out" in entry["reason"], entry["reason"])
        self.assertTrue(entry["at"])
        self.assertEqual(Order.objects.count(), 0)

    def test_a_failed_refund_is_recorded_as_failed_and_the_retry_updates_the_same_entry(self):
        checkout = self.checkout()
        self.sell_out()
        self.client_mock.v1.refunds.create.side_effect = RuntimeError("stripe is down")

        with self.assertRaises(RuntimeError):
            stripe_service.finalize(intent(checkout), ACCOUNT)  # raised on, so Stripe retries the webhook

        entry, = self.recorded(checkout)
        self.assertEqual((entry["status"], entry["error"], entry["attempts"]), ("failed", "stripe is down", 1))
        self.assertIsNone(entry["refund_id"])

        self.client_mock.v1.refunds.create.side_effect = None
        self.assertIsNone(stripe_service.finalize(intent(checkout), ACCOUNT))

        entry, = self.recorded(checkout)
        self.assertEqual((entry["status"], entry["refund_id"], entry["attempts"]), ("refunded", "re_auto", 2))
        self.assertIsNone(entry["error"])

    def test_a_total_that_moved_is_recorded_too(self):
        checkout = self.checkout()
        self.assertIsNone(stripe_service.finalize(intent(checkout, amount=100), ACCOUNT))
        entry, = self.recorded(checkout)
        self.assertEqual(entry["status"], "refunded")
        self.assertIn("doesn't match the order total", entry["reason"])

    def test_money_paid_after_the_checkout_was_already_paid_is_recorded_as_its_own_entry(self):
        checkout = self.checkout()
        order = stripe_service.finalize(intent(checkout), ACCOUNT)
        self.assertIsNotNone(order)
        self.assertEqual(self.recorded(checkout), [])  # a placed order records nothing

        self.assertIsNone(stripe_service.finalize(intent(checkout, id="pi_2"), ACCOUNT))

        entry, = self.recorded(checkout)
        self.assertEqual((entry["payment_intent"], entry["status"]), ("pi_2", "refunded"))
        self.assertEqual(Order.objects.count(), 1)

    def test_the_other_things_kept_on_the_checkout_are_not_lost(self):
        checkout = self.checkout()
        Checkout.objects.filter(pk=checkout.pk).update(metadata={"discount_code": "SAVE10", "notes": "leave at door"})
        self.sell_out()

        stripe_service.finalize(intent(checkout), ACCOUNT)

        checkout.refresh_from_db()
        self.assertEqual((checkout.metadata["discount_code"], checkout.metadata["notes"]), ("SAVE10", "leave at door"))
        self.assertEqual(len(checkout.metadata["auto_refunds"]), 1)

    def test_staff_see_it_and_shoppers_do_not(self):
        from apps.commerce.serializers import CheckoutSerializer, StaffCheckoutSerializer

        checkout = self.checkout()
        self.sell_out()
        stripe_service.finalize(intent(checkout), ACCOUNT)
        checkout.refresh_from_db()

        self.assertEqual(StaffCheckoutSerializer(checkout).data["auto_refunds"][0]["status"], "refunded")
        self.assertNotIn("auto_refunds", CheckoutSerializer(checkout).data)  # the shopper's own checkout shows no payment details
        shopper = self.client.get(f"{self.base}/checkout/{checkout.pk}/", **self.h)
        self.assertNotIn("auto_refunds", shopper.data)
        self.assertNotIn("pi_1", str(shopper.data))


@override_settings(ROOT_URLCONF="apps.payments.test_stripe", STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class DisputeFlagTests(base_tests.CommerceTestBase):
    """A chargeback flags the order so staff see it. Nothing else changes by itself."""

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

    def stripe_order(self):
        order = stripe_service.finalize(intent(self.checkout()), ACCOUNT)
        return order, Payment.objects.get(order=order)

    def dispute(self, status="needs_response", id="dp_1", intent_id="pi_1"):
        return {"id": id, "payment_intent": intent_id, "status": status, "reason": "fraudulent",
                "amount": 99800, "currency": "inr"}

    def test_a_new_dispute_flags_the_order_and_shows_in_its_timeline(self):
        from apps.orders.serializers import OrderSerializer

        order, payment = self.stripe_order()

        stripe_service.record_dispute(self.dispute(), ACCOUNT)

        order.refresh_from_db()
        entry, = order.metadata["disputes"]
        self.assertEqual((entry["id"], entry["status"], entry["reason"]), ("dp_1", "needs_response", "fraudulent"))
        self.assertEqual((entry["amount"], entry["currency"], entry["payment"]), ("998.00", "INR", str(payment.pk)))
        data = OrderSerializer(order).data
        self.assertEqual(data["dispute"]["status"], "needs_response")
        self.assertEqual(len(data["disputes"]), 1)
        self.assertIn("payment_dispute", [event["type"] for event in data["timeline"]])
        # the audit row and the payment note from before are still written
        self.assertEqual(payment.transactions.filter(transaction_type="dispute").count(), 1)
        payment.refresh_from_db()
        self.assertEqual(payment.metadata["dispute"], {"id": "dp_1", "status": "needs_response"})

    def test_nothing_else_changes_by_itself(self):
        from apps.inventory.models import InventoryItem

        order, payment = self.stripe_order()
        stripe_service.record_dispute(self.dispute(), ACCOUNT)
        stripe_service.record_dispute(self.dispute(status="lost"), ACCOUNT)

        order.refresh_from_db()
        payment.refresh_from_db()
        item = InventoryItem.objects.get(variant=self.variant)
        self.assertEqual((order.status, order.fulfillment_status), ("open", "unfulfilled"))
        self.assertEqual(payment.status, "captured")
        self.assertEqual((item.available, item.committed), (8, 2))
        self.client_mock.v1.refunds.create.assert_not_called()

    def test_the_dispute_moves_through_its_states_in_one_entry(self):
        order, payment = self.stripe_order()
        stripe_service.record_dispute(self.dispute("needs_response"), ACCOUNT)
        order.refresh_from_db()
        opened = order.metadata["disputes"][0]["opened_at"]

        stripe_service.record_dispute(self.dispute("won"), ACCOUNT)

        order.refresh_from_db()
        entry, = order.metadata["disputes"]
        self.assertEqual((entry["status"], entry["opened_at"]), ("won", opened))
        self.assertEqual(payment.transactions.filter(transaction_type="dispute").count(), 2)

    def test_the_same_event_twice_changes_nothing(self):
        order, _ = self.stripe_order()
        stripe_service.record_dispute(self.dispute(), ACCOUNT)
        order.refresh_from_db()
        first = dict(order.metadata)

        stripe_service.record_dispute(self.dispute(), ACCOUNT)

        order.refresh_from_db()
        self.assertEqual(order.metadata, first)

    def test_a_second_dispute_is_listed_and_the_latest_is_the_one_shown(self):
        from apps.orders.serializers import OrderSerializer

        order, _ = self.stripe_order()
        stripe_service.record_dispute(self.dispute(id="dp_1", status="won"), ACCOUNT)
        stripe_service.record_dispute(self.dispute(id="dp_2", status="needs_response"), ACCOUNT)

        order.refresh_from_db()
        data = OrderSerializer(order).data
        self.assertEqual([d["id"] for d in data["disputes"]], ["dp_2", "dp_1"])
        self.assertEqual(data["dispute"]["id"], "dp_2")

    def test_an_order_without_a_dispute_shows_none(self):
        from apps.orders.serializers import OrderSerializer

        order, _ = self.stripe_order()
        data = OrderSerializer(order).data
        self.assertEqual((data["dispute"], data["disputes"]), (None, []))

    def test_a_dispute_for_an_unknown_payment_is_ignored(self):
        order, _ = self.stripe_order()
        self.assertIsNone(stripe_service.record_dispute(self.dispute(intent_id="pi_unknown"), ACCOUNT))
        order.refresh_from_db()
        self.assertNotIn("disputes", order.metadata)

    def test_the_webhook_flags_the_order(self):
        order, _ = self.stripe_order()
        event = {"type": "charge.dispute.created", "account": ACCOUNT, "data": {"object": self.dispute()}}
        with mock.patch("apps.payments.stripe_views.stripe.Webhook.construct_event", return_value=event):
            r = self.client.generic("POST", "/api/v1/payments/stripe/webhook/", b"{}", content_type="application/json")
        self.assertEqual(r.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.metadata["disputes"][0]["status"], "needs_response")

    def test_shoppers_do_not_see_the_dispute(self):
        from apps.orders.serializers import ShopperOrderSerializer

        order, _ = self.stripe_order()
        stripe_service.record_dispute(self.dispute(), ACCOUNT)
        order.refresh_from_db()

        data = ShopperOrderSerializer(order).data

        for hidden in ("dispute", "disputes", "stock_released_at"):
            self.assertNotIn(hidden, data)
        self.assertNotIn("payment_dispute", [event["type"] for event in data["timeline"]])
        self.assertNotIn("dispute_dp_1", str(data["timeline"]))
        self.assertIn("customer_cancel_until", data)

    def test_shoppers_do_not_see_the_dispute_rows_of_their_payment_but_staff_do(self):
        from apps.orders.serializers import OrderSerializer

        order, payment = self.stripe_order()
        stripe_service.record_dispute(self.dispute(), ACCOUNT)
        self.assertEqual(payment.transactions.filter(transaction_type="dispute").count(), 1)

        detail = self.client.get(f"{self.base}/orders/{order.pk}/", **self.h)
        listed = self.client.get(f"{self.base}/orders/", **self.h)

        self.assertEqual((detail.status_code, listed.status_code), (200, 200), detail.data)
        for shown in (detail.data, listed.data[0]):
            kinds = [t["transaction_type"] for p in shown["payments"] for t in p["transactions"]]
            self.assertEqual(kinds, ["capture"])  # the money that came in is still shown, the chargeback is not
            self.assertNotIn("dp_1", str(shown["payments"]))
            self.assertNotIn("fraudulent", str(shown["payments"]))
        order.refresh_from_db()
        staff = OrderSerializer(order).data
        self.assertEqual(
            sorted(t["transaction_type"] for p in staff["payments"] for t in p["transactions"]), ["capture", "dispute"]
        )

    def test_the_order_row_is_locked_before_the_payment_row(self):
        from .models import Payment as PaymentModel

        order, _ = self.stripe_order()
        calls = []
        real_lock_order, real_select = services._lock_order_of, PaymentModel.objects.select_for_update

        def lock_order(p):
            calls.append("order")
            return real_lock_order(p)

        def select(*args, **kwargs):
            calls.append("payment")
            return real_select(*args, **kwargs)

        with mock.patch.object(services, "_lock_order_of", side_effect=lock_order), \
             mock.patch.object(PaymentModel.objects, "select_for_update", side_effect=select):
            stripe_service.record_dispute(self.dispute(), ACCOUNT)
        self.assertEqual(calls[:2], ["order", "payment"])


@override_settings(ROOT_URLCONF="apps.payments.test_stripe", STRIPE_SECRET_KEY="sk_test", STRIPE_PUBLISHABLE_KEY="pk_test", STRIPE_WEBHOOK_SECRET="whsec")
class StripeStockJourneyTests(base_tests.CommerceTestBase):
    """
    Whole card-payment journeys (Stripe mocked). After EVERY step the stock rules hold (on_hand = available +
    committed + unavailable, nothing negative, a history row for every change) and the payment and order agree.
    """

    base = base_tests.PlaceOrderTests.base
    h = base_tests.PlaceOrderTests.h
    address = base_tests.PlaceOrderTests.address

    def setUp(self):
        super().setUp()
        self.store.stripe_account_id, self.store.stripe_charges_enabled = ACCOUNT, True
        self.store.save()
        self.client_mock = mock.MagicMock()
        self.client_mock.v1.refunds.create.return_value = SimpleNamespace(id="re_1")
        patcher = mock.patch.object(stripe_service, "client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def checkout(self, quantity=2, session=None):
        h = {"HTTP_X_CART_SESSION": session} if session else self.h
        self.client.post(f"{self.base}/cart/items/", {"product": str(self.product.pk), "quantity": quantity}, format="json", **h)
        r = self.client.post(
            f"{self.base}/checkout/", {"email": "a@b.com", "shipping_address": self.address}, format="json", **h
        )
        return Checkout.objects.get(pk=r.data["id"])

    def stripe_order(self, intent_id="pi_1"):
        order = stripe_service.finalize(intent(self.checkout(), id=intent_id), ACCOUNT)
        return order, Payment.objects.get(order=order)

    def levels(self):
        from apps.inventory.models import InventoryItem

        item = InventoryItem.objects.get(variant=self.variant)
        return item.available, item.committed

    def consistent(self, store=None):
        return base_tests.assert_stock_consistent(self, store or self.store)

    def event(self, kind, obj):
        return {"type": kind, "account": ACCOUNT, "data": {"object": obj}}

    def webhook(self, event):
        with mock.patch("apps.payments.stripe_views.stripe.Webhook.construct_event", return_value=event):
            return self.client.generic("POST", "/api/v1/payments/stripe/webhook/", b"{}", content_type="application/json")

    def test_4_paid_once_even_when_the_webhook_the_return_and_a_replay_all_arrive(self):
        checkout = self.checkout()
        intents = self.client_mock.v1.payment_intents
        intents.create.return_value = intent(checkout, status="requires_payment_method")
        self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/", **self.h)
        self.assertEqual(self.levels(), (10, 0))  # starting a payment holds nothing
        self.consistent()

        paid = intent(checkout)
        self.assertEqual(self.webhook(self.event("payment_intent.succeeded", paid)).status_code, 200)
        self.assertEqual(self.levels(), (8, 2))
        self.consistent()

        intents.retrieve.return_value = paid
        r = self.client.post(f"{self.base}/checkout/{checkout.pk}/stripe/confirm/", **self.h)  # the shopper comes back
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.webhook(self.event("payment_intent.succeeded", paid)).status_code, 200)  # Stripe repeats it

        self.assertEqual((Order.objects.count(), Payment.objects.count()), (1, 1))
        self.assertEqual(self.levels(), (8, 2))  # committed once
        history = self.consistent()
        self.assertEqual(history["order_placed"], 1)
        self.client_mock.v1.refunds.create.assert_not_called()
        self.assertEqual(Payment.objects.get().status, "captured")

    def test_5_paid_but_the_stock_ran_out_is_refunded_with_no_order_and_stock_untouched(self):
        from apps.inventory.services import set_available

        for left in (1, 0):  # fewer than the 2 wanted, and none at all
            with self.subTest(left=left):
                self.client_mock.v1.refunds.create.reset_mock()
                set_available(self.variant, self.location, 10)
                checkout = self.checkout(quantity=2, session=f"sess-left-{left}-aaaaaaaaaa")
                set_available(self.variant, self.location, left)
                before = self.consistent()

                self.assertIsNone(stripe_service.finalize(intent(checkout, id=f"pi_left_{left}"), ACCOUNT))

                self.assertEqual(self.levels(), (left, 0))  # nothing committed, nothing lost
                self.assertEqual(self.consistent(), before)  # and no history row was written
                self.assertEqual((Order.objects.count(), Payment.objects.count()), (0, 0))
                self.client_mock.v1.refunds.create.assert_called_once()
                checkout.refresh_from_db()
                entry, = checkout.metadata["auto_refunds"]
                self.assertEqual((entry["status"], entry["payment_intent"]), ("refunded", f"pi_left_{left}"))

    def test_6_a_payment_that_failed_or_was_cancelled_leaves_stock_alone(self):
        checkout = self.checkout()
        before = self.consistent()

        for status in ("processing", "requires_payment_method", "requires_action", "canceled"):
            self.assertIsNone(stripe_service.finalize(intent(checkout, status=status), ACCOUNT))
        for kind in ("payment_intent.payment_failed", "payment_intent.canceled"):
            r = self.webhook(self.event(kind, {"id": "pi_1", "status": "canceled", "last_payment_error": {"message": "declined"}}))
            self.assertEqual(r.status_code, 200)

        self.assertEqual(self.levels(), (10, 0))
        self.assertEqual(self.consistent(), before)
        self.assertEqual((Order.objects.count(), Payment.objects.count()), (0, 0))
        self.client_mock.v1.refunds.create.assert_not_called()

    def test_7_staff_cancel_of_a_paid_order_refunds_first_and_a_failed_refund_changes_nothing(self):
        import stripe

        order, payment = self.stripe_order()
        self.assertEqual(self.levels(), (8, 2))
        before = self.consistent()
        self.client_mock.v1.refunds.create.side_effect = stripe.StripeError("boom")

        with self.assertRaises(ValidationError):
            orders_services.cancel_order(order)

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("open", "captured"))
        self.assertEqual(self.levels(), (8, 2))
        self.assertEqual(self.consistent(), before)  # not one new history row
        self.assertEqual(payment.transactions.filter(transaction_type="refund").count(), 0)

        self.client_mock.v1.refunds.create.side_effect = None
        orders_services.cancel_order(order)

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("cancelled", "refunded"))
        self.assertEqual(self.levels(), (10, 0))
        history = self.consistent()
        self.assertEqual((history["order_placed"], history["order_cancelled"]), (1, 1))

    def test_8_refunds_partial_then_full_and_a_partly_shipped_order(self):
        from apps.fulfillment import services as fulfillment

        order, payment = self.stripe_order()
        services.refund(payment, Decimal("300.00"))  # partial: no stock change
        order.refresh_from_db()
        self.assertEqual((order.status, self.levels()), ("open", (8, 2)))
        self.assertEqual(self.consistent()["order_cancelled"], 0)

        services.refund(payment, Decimal("698.00"))  # the rest: back once, cancelled
        order.refresh_from_db()
        self.assertEqual((order.status, self.levels()), ("cancelled", (10, 0)))
        self.assertEqual(self.consistent()["order_cancelled"], 1)

        second, second_payment = self.stripe_order(intent_id="pi_2")
        fulfillment.fulfill(second, lines=[{"order_item": second.items.get(), "quantity": 1}])
        self.assertEqual(self.levels(), (8, 1))
        self.consistent()
        services.refund(second_payment, Decimal("998.00"))  # the shipped unit is with the customer
        second.refresh_from_db()
        self.assertEqual((second.status, self.levels()), ("open", (9, 0)))  # only the unshipped unit is back
        history = self.consistent()
        self.assertEqual((history["order_fulfilled"], history["order_cancelled"]), (1, 2))

    def test_11_a_card_journey_in_one_store_never_touches_another_stores_stock_or_payments(self):
        from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
        from apps.catalog.services import online_channels
        from apps.inventory.models import InventoryHistory, InventoryItem, Location
        from apps.inventory.services import set_available
        from apps.tenancy.models import Store

        other = Store.objects.create(store_name="Other", store_slug="other-shop", status="active")
        product = Product.objects.create(store=other, title="Tee", status="active")
        variant = ProductVariant.objects.create(product=product, sku="TEE-1", price=Decimal("499.00"))  # the same SKU
        for channel in online_channels(other):
            ProductSalesChannel.objects.create(product=product, sales_channel=channel)
        set_available(variant, Location.objects.create(store=other, name="Main warehouse"), 10)

        def snapshot():
            rows = sorted(InventoryItem.objects.filter(variant__product__store=other).values_list(
                "available", "committed", "unavailable", "on_hand"))
            return (rows, InventoryHistory.objects.filter(store=other).count(),
                    Payment.objects.filter(store=other).count(), Order.objects.filter(store=other).count())

        before = snapshot()
        order, payment = self.stripe_order()
        services.refund(payment, Decimal("300.00"))
        services.refund(payment, Decimal("698.00"))
        second, _ = self.stripe_order(intent_id="pi_2")
        orders_services.cancel_order(second)

        self.assertEqual(snapshot(), before)
        self.assertEqual(self.levels(), (10, 0))
        self.consistent()
        self.consistent(other)

    def test_9_a_shipped_card_order_that_is_returned_is_flagged_and_its_stock_comes_back_once(self):
        from apps.fulfillment import services as fulfillment

        order, payment = self.stripe_order()
        item = order.items.get()
        fulfillment.fulfill(order)
        self.assertEqual(self.levels(), (8, 0))
        self.consistent()

        orders_services.return_items(order, [{"order_item": item, "quantity": 2, "location": None}])

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual((order.status, payment.status), ("open", "captured"))  # money received: staff refund it
        self.assertIsNotNone(order.metadata["returned"]["at"])
        self.assertEqual(self.levels(), (10, 0))
        history = self.consistent()
        self.assertEqual((history["order_fulfilled"], history["refund_restock"]), (1, 1))
        with self.assertRaises(ValidationError):
            orders_services.return_items(order, [{"order_item": item, "quantity": 1, "location": None}])
        self.assertEqual(self.consistent()["refund_restock"], 1)
        self.client_mock.v1.refunds.create.assert_not_called()
