"""
Gift cards as a payment method: several cards, partial and full cover, double spend, cancel and
refund destinations, and the admin rules. Run with:

    python manage.py test apps.gift_cards --settings=config.test_settings
"""
from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

from django.core.cache import cache
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.exceptions import Throttled
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.gift_cards import services
from apps.gift_cards.models import GiftCard, GiftCardTransaction
from apps.gift_cards.services import change_balance
from apps.tenancy.models import Store

ADDRESS = {
    "first_name": "A", "last_name": "B", "address_line_1": "1 Test St", "city": "Sydney",
    "state": "NSW", "postal_code": "2000", "country": "Australia",
}


class GiftCardPaymentTests(StoreClientMixin, APITestCase):
    """End to end through apps.commerce.services and apps.payments.services (the layers the storefront calls)."""

    def setUp(self):
        cache.clear()  # the wrong-code counters live in the cache
        token, store_data = self.sign_up("gift-card-pay@example.com", "Gift Card Pay Store")
        self.store = Store.objects.get(pk=store_data["storeId"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=str(self.store.pk))
        self._product = None

    # -- helpers ------------------------------------------------------------------------------

    def card(self, balance="100.00", store=None, currency=None, **extra):
        store = store or self.store
        code = services.generate_code(store)
        card = GiftCard.objects.create(
            store=store, code_hash=code["code_hash"], code_enc=code["code_enc"], code_last4=code["code_last4"],
            initial_balance=Decimal(balance), current_balance=Decimal(balance),
            currency_code=currency or store.currency, status="active", is_deleted=False, **extra,
        )
        return card, code["raw"]

    def checkout(self, session="s" * 24, address=True):
        """An open checkout for one 20.00 item (plus whatever shipping/tax the store charges)."""
        from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
        from apps.catalog.services import online_channels
        from apps.commerce import services as commerce
        from apps.inventory.models import Location
        from apps.inventory.services import set_available

        if self._product is None:
            product = Product.objects.create(store=self.store, title="Test widget", status="active")
            variant = ProductVariant.objects.create(product=product, sku="W-1", price=Decimal("20.00"))
            for channel in online_channels(self.store):
                ProductSalesChannel.objects.create(product=product, sales_channel=channel)
            location = Location.objects.create(store=self.store, name="Main warehouse")
            set_available(variant, location, 50)
            self._product = product
        cart = commerce.get_or_create_cart(self.store, session_id=session)
        commerce.add_item(cart, self._product, 1)
        checkout = commerce.start_checkout(cart, email="buyer@example.com")
        if address:
            checkout = commerce.update_checkout(checkout, shipping_address=ADDRESS)
        return checkout

    def apply(self, checkout, raw):
        from apps.commerce import services as commerce

        return commerce.update_checkout(checkout, gift_card_code=raw)

    def place(self, checkout, method="cod"):
        from apps.commerce import services as commerce

        return commerce._place_order(checkout, method)

    def discount(self, code="SAVE10", percentage="10"):
        from apps.discounts.models import Discount, DiscountCode

        discount = Discount.objects.create(
            store=self.store, name="Ten off", discount_type="percentage", value=Decimal(percentage),
            starts_at=timezone.now() - timedelta(days=1), is_active=True, is_deleted=False,
        )
        DiscountCode.objects.create(discount=discount, code=code, is_active=True, is_deleted=False)

    # -- applying a card ----------------------------------------------------------------------

    def test_code_is_case_insensitive_and_spaces_are_trimmed(self):
        from apps.commerce import services as commerce

        card, raw = self.card("500.00")
        checkout = self.apply(self.checkout(), f"  {raw.lower()}  ")
        lines = commerce.gift_card_lines(checkout)
        self.assertEqual([l["last4"] for l in lines], [card.code_last4])
        self.assertEqual(checkout.gift_card_amount, checkout.total_amount)

    def test_code_typed_without_dashes_still_matches(self):
        card, raw = self.card("500.00")
        checkout = self.apply(self.checkout(), raw.replace("-", "").lower())
        self.assertGreater(checkout.gift_card_amount, 0)

    def test_applied_card_shows_amount_used_and_remaining_balance(self):
        from apps.commerce import services as commerce

        _, raw = self.card("500.00")
        checkout = self.apply(self.checkout(), raw)
        line = commerce.gift_card_lines(checkout)[0]
        self.assertEqual(Decimal(line["amount"]), checkout.total_amount)
        self.assertEqual(Decimal(line["remaining"]), Decimal("500.00") - checkout.total_amount)

    # -- partial use and full cover -------------------------------------------------------------

    def test_partial_use_card_pays_its_balance_and_the_rest_is_due(self):
        from apps.commerce import services as commerce

        _, raw = self.card("5.00")
        checkout = self.apply(self.checkout(), raw)
        self.assertEqual(checkout.gift_card_amount, Decimal("5.00"))
        self.assertEqual(commerce.amount_due(checkout), checkout.total_amount - Decimal("5.00"))
        self.assertEqual(commerce.gift_card_lines(checkout)[0]["remaining"], "0.00")

    def test_partial_use_places_the_order_with_the_rest_on_another_payment_method(self):
        from apps.payments.models import Payment

        card, raw = self.card("5.00")
        order = self.place(self.apply(self.checkout(), raw), "cod")
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("0.00"))
        by_provider = {p.provider: p for p in Payment.objects.filter(order=order)}
        self.assertEqual(by_provider["gift_card"].amount, Decimal("5.00"))
        self.assertEqual(by_provider["cod"].amount, order.total - Decimal("5.00"))
        self.assertEqual(by_provider["cod"].status, "pending")

    def test_full_cover_places_the_order_with_nothing_left_to_pay(self):
        from apps.payments.models import Payment

        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        payments = list(Payment.objects.filter(order=order))
        self.assertEqual([p.provider for p in payments], ["gift_card"])
        self.assertEqual(payments[0].amount, order.total)
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("500.00") - order.total)

    def test_gift_card_method_is_refused_when_the_cards_do_not_cover_the_total(self):
        _, raw = self.card("5.00")
        checkout = self.apply(self.checkout(), raw)
        with self.assertRaises(DRFValidationError):
            self.place(checkout, "gift_card")

    # -- several cards, discount, shipping ------------------------------------------------------

    def test_two_cards_pay_in_the_order_they_were_added(self):
        from apps.commerce import services as commerce
        from apps.payments.models import Payment

        first, raw1 = self.card("5.00")
        second, raw2 = self.card("500.00")
        checkout = self.apply(self.apply(self.checkout(), raw1), raw2)
        lines = commerce.gift_card_lines(checkout)
        self.assertEqual([l["last4"] for l in lines], [first.code_last4, second.code_last4])
        self.assertEqual(Decimal(lines[0]["amount"]), Decimal("5.00"))
        self.assertEqual(Decimal(lines[1]["amount"]), checkout.total_amount - Decimal("5.00"))
        self.assertEqual(commerce.amount_due(checkout), Decimal("0.00"))

        order = self.place(checkout, "gift_card")
        self.assertEqual(Payment.objects.filter(order=order, provider="gift_card").count(), 2)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.current_balance, Decimal("0.00"))
        self.assertEqual(second.current_balance, Decimal("500.00") - (order.total - Decimal("5.00")))

    def test_removing_one_card_spreads_the_total_over_the_rest(self):
        from apps.commerce import services as commerce

        first, raw1 = self.card("5.00")
        second, raw2 = self.card("500.00")
        checkout = self.apply(self.apply(self.checkout(), raw1), raw2)
        checkout = commerce.update_checkout(checkout, remove_gift_card=first.pk)
        lines = commerce.gift_card_lines(checkout)
        self.assertEqual([l["last4"] for l in lines], [second.code_last4])
        self.assertEqual(Decimal(lines[0]["amount"]), checkout.total_amount)
        checkout = commerce.update_checkout(checkout, gift_card_code="")  # "" removes every card
        self.assertEqual(checkout.gift_card_amount, Decimal("0.00"))

    def test_the_same_card_cannot_be_applied_twice(self):
        _, raw = self.card("5.00")
        checkout = self.apply(self.checkout(), raw)
        with self.assertRaises(DRFValidationError) as exc:
            self.apply(checkout, raw)
        self.assertIn("already applied", str(exc.exception.detail))

    def test_a_card_added_once_the_order_is_covered_is_refused(self):
        _, raw1 = self.card("500.00")
        _, raw2 = self.card("500.00")
        checkout = self.apply(self.checkout(), raw1)
        with self.assertRaises(DRFValidationError) as exc:
            self.apply(checkout, raw2)
        self.assertIn("fully covered", str(exc.exception.detail))

    def test_discount_comes_off_first_then_the_card_pays_what_is_left(self):
        from apps.commerce import services as commerce

        _, raw = self.card("500.00")
        self.discount("SAVE10", "10")
        checkout = commerce.update_checkout(self.checkout(), discount_code="SAVE10", gift_card_code=raw)
        self.assertEqual(checkout.discount_amount, Decimal("2.00"))
        self.assertEqual(checkout.gift_card_amount, checkout.total_amount)  # the discounted total, not the full price
        order = self.place(checkout, "gift_card")
        self.assertEqual(order.discount_amount, Decimal("2.00"))

    def test_shipping_is_covered_and_a_changed_total_recalculates(self):
        from apps.commerce import services as commerce

        _, raw = self.card("500.00")
        self.store.shipping_flat_rate = Decimal("7.00")
        self.store.save(update_fields=["shipping_flat_rate"])
        checkout = self.apply(self.checkout(), raw)
        self.assertGreaterEqual(checkout.shipping_amount, Decimal("7.00"))
        self.assertEqual(checkout.gift_card_amount, checkout.total_amount)  # shipping is inside what the card pays
        old_total = checkout.total_amount

        self.store.shipping_flat_rate = Decimal("12.00")
        self.store.save(update_fields=["shipping_flat_rate"])
        checkout = commerce.update_checkout(checkout)
        self.assertEqual(checkout.total_amount, old_total + Decimal("5.00"))
        self.assertEqual(checkout.gift_card_amount, checkout.total_amount)  # re-spread over the new total

    def test_a_changed_card_share_is_shown_to_the_shopper_before_the_order_is_placed(self):
        from apps.commerce import services as commerce

        card, raw = self.card("500.00")
        checkout = self.apply(self.checkout(), raw)
        GiftCard.objects.filter(pk=card.pk).update(current_balance=Decimal("1.00"))  # spent elsewhere
        with self.assertRaises(DRFValidationError) as exc:
            commerce.place_order(checkout, "gift_card")
        self.assertIn("total", exc.exception.detail)
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("1.00"))

    # -- double spend ---------------------------------------------------------------------------

    def test_two_checkouts_cannot_both_spend_the_same_balance(self):
        from apps.orders.models import Order

        card, raw = self.card("30.00")
        first = self.apply(self.checkout("a" * 24), raw)
        second = self.apply(self.checkout("b" * 24), raw)  # both priced against the full 30.00
        self.assertEqual(first.gift_card_amount, first.total_amount)
        self.assertEqual(second.gift_card_amount, second.total_amount)
        self.assertGreater(first.total_amount + second.total_amount, Decimal("30.00"))

        self.place(first, "cod")
        before = Order.objects.filter(store=self.store).count()
        with self.assertRaises(DRFValidationError) as exc:
            self.place(second, "cod")
        self.assertIn("gift_card_code", exc.exception.detail)
        self.assertEqual(Order.objects.filter(store=self.store).count(), before)  # the second order was not created
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("30.00") - first.total_amount)  # only the first spend landed
        self.assertEqual(GiftCardTransaction.objects.filter(gift_card=card, transaction_type="debit").count(), 1)

    # -- errors ---------------------------------------------------------------------------------

    def _error(self, raw):
        from apps.commerce import services as commerce

        with self.assertRaises(DRFValidationError) as exc:
            commerce.update_checkout(self.checkout(), gift_card_code=raw)
        return str(exc.exception.detail["gift_card_code"])

    def test_each_problem_has_its_own_message(self):
        self.assertIn("couldn't find", self._error("NOPE-NOPE-NOPE-NOPE"))

        _, raw = self.card("10.00", expires_at=timezone.now() - timedelta(days=1))
        self.assertIn("expired", self._error(raw))

        deactivated, raw = self.card("10.00")
        services.deactivate(deactivated)
        self.assertIn("deactivated", self._error(raw))

        spent, raw = self.card("10.00")
        change_balance(spent, Decimal("-10.00"), "debit")
        self.assertIn("no balance", self._error(raw))

    def test_another_stores_card_is_simply_not_found(self):
        _, data = self.sign_up("gift-card-other@example.com", "Other Gift Store")
        other = Store.objects.get(pk=data["storeId"])
        _, other_raw = self.card("100.00", store=other)
        self.assertIn("couldn't find", self._error(other_raw))

    def test_wrong_code_attempts_are_rate_limited(self):
        from apps.commerce import services as commerce

        checkout = self.checkout()
        for _ in range(services.MAX_WRONG_CODES):
            with self.assertRaises(DRFValidationError):
                with services.code_attempts(self.store, "203.0.113.9"):
                    commerce.update_checkout(checkout, gift_card_code="WRONG-CODE-0000-0000")
        _, raw = self.card("10.00")
        with self.assertRaises(Throttled):
            with services.code_attempts(self.store, "203.0.113.9"):
                commerce.update_checkout(checkout, gift_card_code=raw)  # even a right code waits
        with services.code_attempts(self.store, "198.51.100.1"):  # someone else is unaffected
            self.assertGreater(commerce.update_checkout(checkout, gift_card_code=raw).gift_card_amount, 0)

    def test_a_gift_card_cannot_buy_a_gift_card(self):
        from apps.commerce import services as commerce

        _, raw = self.card("100.00")
        product = services.sync_storefront_product(self.store)
        cart = commerce.get_or_create_cart(self.store, session_id="g" * 24)
        commerce.add_item(cart, product, 1)
        checkout = commerce.start_checkout(cart, email="buyer@example.com")
        with self.assertRaises(DRFValidationError) as exc:
            commerce.update_checkout(checkout, gift_card_code=raw)
        self.assertIn("buy a gift card", str(exc.exception.detail))

    # -- cancel and refund ----------------------------------------------------------------------

    def test_cancelling_an_order_paid_by_card_puts_the_balance_back(self):
        from apps.orders import services as orders_services
        from apps.payments.models import Payment

        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("500.00") - order.total)

        orders_services.cancel_order(order)
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("500.00"))
        refund_tx = Payment.objects.get(order=order).transactions.get(transaction_type="refund")
        self.assertEqual(refund_tx.metadata["destination"], "gift_card")
        self.assertEqual(refund_tx.metadata["gift_card_last4"], card.code_last4)
        last = card.transactions.order_by("created_at").last()
        self.assertEqual((last.transaction_type, last.order_id), ("credit", order.pk))

    def test_split_payment_refunds_the_card_first_and_the_rest_to_the_original_method(self):
        from apps.orders import services as orders_services
        from apps.payments import services as payments
        from apps.payments.models import Payment

        card, raw = self.card("5.00")
        order = self.place(self.apply(self.checkout(), raw), "cod")
        payments.capture_payment(Payment.objects.get(order=order, provider="cod"))

        orders_services.cancel_order(order)  # no choices given: the defaults
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("5.00"))
        cod = Payment.objects.get(order=order, provider="cod")
        self.assertEqual(cod.transactions.get(transaction_type="refund").metadata["destination"], "original")

    def test_refund_later_leaves_the_balance_and_the_payment_alone(self):
        from apps.orders import services as orders_services
        from apps.payments import services as payments
        from apps.payments.models import Payment

        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        payment = Payment.objects.get(order=order)
        orders_services.cancel_order(order, refund_destinations={str(payment.pk): "later"})
        order.refresh_from_db()
        card.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(card.current_balance, Decimal("500.00") - order.total)  # nothing credited yet
        self.assertEqual(payment.status, "captured")  # still refundable later

        payments.refund(payment, payment.amount, "Refund after cancel")
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("500.00"))

    def test_a_gift_card_payment_cannot_be_refunded_to_the_original_method(self):
        from apps.payments import services as payments
        from apps.payments.models import Payment

        _, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        with self.assertRaises(DRFValidationError):
            payments.refund(Payment.objects.get(order=order), Decimal("1.00"), destination="original")

    def test_two_cards_are_each_refunded_to_their_own_card(self):
        from apps.orders import services as orders_services

        first, raw1 = self.card("5.00")
        second, raw2 = self.card("500.00")
        order = self.place(self.apply(self.apply(self.checkout(), raw1), raw2), "gift_card")
        orders_services.cancel_order(order)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.current_balance, second.current_balance), (Decimal("5.00"), Decimal("500.00")))

    def test_expired_card_needs_a_new_expiry_before_it_can_be_refunded_to(self):
        from apps.orders import services as orders_services

        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        GiftCard.objects.filter(pk=card.pk).update(expires_at=timezone.now() - timedelta(days=1))

        with self.assertRaises(DRFValidationError) as exc:
            orders_services.cancel_order(order)
        self.assertIn("new expiry", str(exc.exception.detail))
        order.refresh_from_db()
        self.assertNotEqual(order.status, "cancelled")  # the whole cancel rolled back

        r = self.client.patch(
            f"/api/v1/gift-cards/{card.pk}/",
            {"expires_at": (timezone.now() + timedelta(days=30)).isoformat()}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        orders_services.cancel_order(order)
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("500.00"))

    def test_deactivated_card_cannot_receive_a_refund_but_later_still_works(self):
        from apps.orders import services as orders_services
        from apps.payments.models import Payment

        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        services.deactivate(card)
        with self.assertRaises(DRFValidationError) as exc:
            orders_services.cancel_order(order)
        self.assertIn("deactivated", str(exc.exception.detail))
        payment = Payment.objects.get(order=order)
        orders_services.cancel_order(order, refund_destinations={str(payment.pk): "later"})
        order.refresh_from_db()
        self.assertEqual(order.status, "cancelled")

    def test_admin_cancel_endpoint_accepts_refund_destinations(self):
        from apps.payments.models import Payment

        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        payment = Payment.objects.get(order=order)
        r = self.client.post(
            f"/api/v1/orders/{order.pk}/cancel/", {"refund_destinations": {str(payment.pk): "later"}}, format="json"
        )
        self.assertEqual(r.status_code, 200, r.data)
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("500.00") - order.total)

    # -- admin page and order page --------------------------------------------------------------

    def test_admin_cannot_set_the_balance_reactivate_or_adjust(self):
        r = self.client.post("/api/v1/gift-cards/", {"initial_balance": "50.00", "note": "Birthday"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        card_id, code = r.data["id"], r.data["code"]
        self.assertEqual(r.data["note"], "Birthday")

        r = self.client.patch(
            f"/api/v1/gift-cards/{card_id}/",
            {"current_balance": "9999", "initial_balance": "9999", "status": "disabled"}, format="json",
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["current_balance"]), Decimal("50"))
        self.assertEqual(r.data["status"], "active")  # status isn't writable either

        adjust = self.client.post(f"/api/v1/gift-cards/{card_id}/adjust/", {"amount": "10"}, format="json")
        self.assertIn(adjust.status_code, (404, 405))

        r = self.client.post(f"/api/v1/gift-cards/{card_id}/deactivate/", {}, format="json")
        self.assertEqual(r.data["status"], "disabled")
        reactivate = self.client.patch(f"/api/v1/gift-cards/{card_id}/", {"status": "active"}, format="json")
        self.assertEqual(reactivate.status_code, 400)  # a deactivated card can't be edited at all
        shown = self.client.get(f"/api/v1/gift-cards/{card_id}/")
        self.assertEqual(shown.data["status"], "disabled")  # permanent
        self.assertNotIn(code, str(shown.data))  # last 4 only

    def test_expiry_can_be_edited_but_not_into_the_past(self):
        r = self.client.post("/api/v1/gift-cards/", {"initial_balance": "50.00"}, format="json")
        card_id = r.data["id"]
        ok = self.client.patch(
            f"/api/v1/gift-cards/{card_id}/",
            {"expires_at": (timezone.now() + timedelta(days=5)).isoformat()}, format="json",
        )
        self.assertEqual(ok.status_code, 200, ok.data)
        bad = self.client.patch(
            f"/api/v1/gift-cards/{card_id}/",
            {"expires_at": (timezone.now() - timedelta(days=5)).isoformat()}, format="json",
        )
        self.assertEqual(bad.status_code, 400)

    def test_card_timeline_lists_order_number_amount_and_balance_after(self):
        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        r = self.client.get(f"/api/v1/gift-cards/{card.pk}/")
        self.assertEqual(r.status_code, 200, r.data)
        use = next(t for t in r.data["transactions"] if t["transaction_type"] == "debit")
        self.assertEqual(use["order_number"], order.order_number)
        self.assertEqual(Decimal(use["amount"]), -order.total)
        self.assertEqual(Decimal(use["balance_after"]), Decimal("500.00") - order.total)

    def test_order_page_shows_the_gift_card_payment_and_timeline(self):
        card, raw = self.card("500.00")
        order = self.place(self.apply(self.checkout(), raw), "gift_card")
        r = self.client.get(f"/api/v1/orders/{order.pk}/")
        self.assertEqual(r.status_code, 200, r.data)
        payment = next(p for p in r.data["payments"] if p["provider"] == "gift_card")
        self.assertEqual(payment["gift_card"]["last4"], card.code_last4)
        self.assertEqual(Decimal(payment["amount"]), order.total)
        titles = " ".join(e["title"] for e in r.data["timeline"])
        self.assertIn(f"gift card ••••{card.code_last4}", titles)

    # -- storefront balance page ----------------------------------------------------------------

    def test_balance_link_shows_the_current_balance(self):
        from apps.gift_cards.storefront_views import balance_token

        card, raw = self.card("40.00")
        self.place(self.apply(self.checkout(), raw), "cod")
        card.refresh_from_db()
        url = f"/api/v1/storefront/{self.store.store_slug}/gift-cards/balance/"
        r = self.client.get(url, {"token": balance_token(card)})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Decimal(r.data["current_balance"]), card.current_balance)
        self.assertEqual(r.data["code_last4"], card.code_last4)
        self.assertEqual(self.client.get(url, {"token": "nope"}).status_code, 400)


@skipUnless(
    connection.vendor == "postgresql",
    "Needs real row locks: run against PostgreSQL (config.test_settings uses SQLite).",
)
class GiftCardConcurrentUseTests(StoreClientMixin, TransactionTestCase):
    """Two threads spend the same card at the same moment; the balance must never go negative."""

    def test_parallel_debits_never_overdraw(self):
        import threading

        _, store_data = self.sign_up("gift-card-race@example.com", "Gift Card Race Store")
        store = Store.objects.get(pk=store_data["storeId"])
        code = services.generate_code(store)
        card = GiftCard.objects.create(
            store=store, code_hash=code["code_hash"], code_enc=code["code_enc"], code_last4=code["code_last4"],
            initial_balance=Decimal("100.00"), current_balance=Decimal("100.00"),
            currency_code=store.currency, status="active", is_deleted=False,
        )
        results, barrier = [], threading.Barrier(2)

        def spend():
            try:
                barrier.wait()
                change_balance(card, Decimal("-60.00"), "debit")
                results.append("ok")
            except DRFValidationError:
                results.append("refused")
            finally:
                connection.close()

        threads = [threading.Thread(target=spend) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        card.refresh_from_db()
        self.assertEqual(sorted(results), ["ok", "refused"])
        self.assertEqual(card.current_balance, Decimal("40.00"))
