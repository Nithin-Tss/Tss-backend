from datetime import timedelta
from decimal import Decimal
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.gift_cards import services
from apps.gift_cards.models import GiftCard, GiftCardCurrencySetting, GiftCardTransaction
from apps.gift_cards.serializers import GiftCardSerializer
from apps.gift_cards.services import change_balance
from apps.tenancy.models import Store
from django.db import DatabaseError, connection, transaction

class GiftCardTransactionProtectionTests(StoreClientMixin, APITestCase):
    def setUp(self):
        self.token, store_data = self.sign_up(
            "gift-card-protection@example.com",
            "Gift Card Test Store",
        )
        self.store = Store.objects.get(pk=store_data["storeId"])

        code = services.generate_code(self.store)
        self.card = GiftCard.objects.create(
            store=self.store,
            code_hash=code["code_hash"],
            code_enc=code["code_enc"],
            code_last4=code["code_last4"],
            initial_balance=Decimal("100.00"),
            current_balance=Decimal("100.00"),
            currency_code="INR",
            status="active",
            is_deleted=False,
        )

        self.transaction_record = GiftCardTransaction.objects.create(
            gift_card=self.card,
            transaction_type="debit",
            amount=Decimal("-10.00"),
            balance_before=Decimal("100.00"),
            balance_after=Decimal("90.00"),
        )
    @skipUnless(
        connection.vendor == "postgresql",
        "The append-only trigger is created by a PostgreSQL-only migration; "
        "config.test_settings uses SQLite without migrations.",
    )
    def test_database_trigger_blocks_gift_card_transaction_updates_and_deletes(self):
        record = self.transaction_record

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'UPDATE "gift_cards"."gift_card_transactions" '
                        'SET reason = %s WHERE id = %s',
                        ["tampered", str(record.pk)],
                    )

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        'DELETE FROM "gift_cards"."gift_card_transactions" '
                        'WHERE id = %s',
                        [str(record.pk)],
                    )

        self.assertTrue(
            GiftCardTransaction.objects.filter(pk=record.pk).exists()
        )

    def test_new_transaction_can_be_created_by_service(self):
        card = change_balance(
            self.card,
            Decimal("-10.00"),
            "debit",
        )

        self.assertEqual(card.current_balance, Decimal("90.00"))
        self.assertEqual(
            GiftCardTransaction.objects.filter(
                gift_card=self.card
            ).count(),
            2,
        )

    def test_transaction_instance_cannot_be_updated_or_deleted(self):
        self.transaction_record.reason = "Modified reason"

        with self.assertRaises(ValidationError):
            self.transaction_record.save()

        self.transaction_record.refresh_from_db()
        self.assertIsNone(self.transaction_record.reason)

        with self.assertRaises(ValidationError):
            self.transaction_record.delete()

        self.assertTrue(
            GiftCardTransaction.objects.filter(
                pk=self.transaction_record.pk
            ).exists()
        )

    def test_queryset_cannot_update_or_delete_transactions(self):
        with self.assertRaises(ValidationError):
            GiftCardTransaction.objects.filter(
                pk=self.transaction_record.pk
            ).update(reason="Modified reason")

        with self.assertRaises(ValidationError):
            GiftCardTransaction.objects.filter(
                pk=self.transaction_record.pk
            ).delete()

        self.assertTrue(
            GiftCardTransaction.objects.filter(
                pk=self.transaction_record.pk
            ).exists()
        )

class GiftCardRulesTests(StoreClientMixin, APITestCase):
    def setUp(self):
        token, store_data = self.sign_up("gift-card-rules@example.com", "Gift Card Rules Store")
        self.store = Store.objects.get(pk=store_data["storeId"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=str(self.store.pk))

    def test_staff_can_issue_a_free_card_in_any_valid_currency(self):
        r = self.client.post(
            "/api/v1/gift-cards/", {"initial_balance": "50.00", "currency_code": "USD"}, format="json"
        )
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["currency_code"], "USD")
        self.assertEqual(len(r.data["code"]), 19)  # the raw code, returned once by the view
        self.assertEqual(len(r.data["code_last4"]), 4)
        card = GiftCard.objects.get(pk=r.data["id"])
        self.assertEqual(GiftCardSerializer(card).data.get("code"), None)  # never in the serializer itself

    def test_card_in_an_invalid_currency_is_refused(self):
        r = self.client.post(
            "/api/v1/gift-cards/", {"initial_balance": "50.00", "currency_code": "ZZZ"}, format="json"
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("currency_code", r.data)

    def test_full_code_is_never_stored_in_plain_text(self):
        r = self.client.post("/api/v1/gift-cards/", {"initial_balance": "50.00"}, format="json")
        raw_code = r.json()["code"]
        card = GiftCard.objects.get(pk=r.data["id"])
        self.assertNotEqual(card.code_enc, raw_code)
        self.assertNotIn(raw_code, str(card.__dict__))
        self.assertEqual(card.code_last4, raw_code[-4:])
        # Resolvable by the hash, not by scanning for the raw value
        self.assertEqual(services.get_by_code(self.store, raw_code).pk, card.pk)

    def test_expired_card_cannot_be_spent_but_can_be_credited(self):
        code = services.generate_code(self.store)
        card = GiftCard.objects.create(
            store=self.store, code_hash=code["code_hash"], code_enc=code["code_enc"], code_last4=code["code_last4"],
            initial_balance=Decimal("100.00"), current_balance=Decimal("50.00"),
            currency_code=self.store.currency, status="active", is_deleted=False,
            expires_at=timezone.now() - timedelta(days=1),
        )
        with self.assertRaises(DRFValidationError):
            change_balance(card, Decimal("-10.00"), "debit")
        self.assertEqual(change_balance(card, Decimal("10.00"), "credit").current_balance, Decimal("60.00"))

    def test_deactivated_card_cannot_be_spent(self):
        code = services.generate_code(self.store)
        card = GiftCard.objects.create(
            store=self.store, code_hash=code["code_hash"], code_enc=code["code_enc"], code_last4=code["code_last4"],
            initial_balance=Decimal("50.00"), current_balance=Decimal("50.00"),
            currency_code=self.store.currency, status="active", is_deleted=False,
        )
        services.deactivate(card)
        card.refresh_from_db()
        self.assertEqual(card.status, "disabled")
        with self.assertRaises(DRFValidationError):
            change_balance(card, Decimal("-10.00"), "debit")

    def test_currency_settings_default_to_aud_25_50_100_200(self):
        r = self.client.get("/api/v1/gift-cards/currencies/")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(r.data[0]["currency_code"], "AUD")
        self.assertEqual(set(r.data[0]["amounts"]), {"25", "50", "100", "200"})

    def test_report_groups_by_currency_and_never_mixes_them(self):
        self.client.post("/api/v1/gift-cards/", {"initial_balance": "50.00", "currency_code": "USD"}, format="json")
        self.client.post("/api/v1/gift-cards/", {"initial_balance": "30.00", "currency_code": "AUD"}, format="json")
        r = self.client.get("/api/v1/gift-cards/report/")
        self.assertEqual(r.status_code, 200, r.data)
        by_currency = {row["currency_code"]: row for row in r.data}
        self.assertEqual(Decimal(by_currency["USD"]["issued"]), Decimal("50"))
        self.assertEqual(Decimal(by_currency["AUD"]["issued"]), Decimal("30"))

    def test_export_masks_the_code(self):
        self.client.post("/api/v1/gift-cards/", {"initial_balance": "50.00"}, format="json")
        r = self.client.get("/api/v1/gift-cards/export/")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("****", body)
        self.assertNotIn("-", body.split("\n")[1].split(",")[0])  # no raw XXXX-XXXX-... code made it in


class GiftCardRedemptionTests(StoreClientMixin, APITestCase):
    """Redeeming a gift card at checkout: currency match, partial spend, shortfall, double-spend."""

    def setUp(self):
        token, store_data = self.sign_up("gift-card-redeem@example.com", "Gift Card Redeem Store")
        self.store = Store.objects.get(pk=store_data["storeId"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_STORE_ID=str(self.store.pk))

    def _card(self, balance="100.00", currency=None):
        currency = currency or self.store.currency
        code = services.generate_code(self.store)
        card = GiftCard.objects.create(
            store=self.store, code_hash=code["code_hash"], code_enc=code["code_enc"], code_last4=code["code_last4"],
            initial_balance=Decimal(balance), current_balance=Decimal(balance),
            currency_code=currency, status="active", is_deleted=False,
        )
        return card, code["raw"]

    def test_currency_mismatch_is_refused_at_checkout(self):
        from apps.commerce import services as commerce

        other_currency = "JPY" if self.store.currency != "JPY" else "USD"
        card, raw_code = self._card(currency=other_currency)
        checkout = self._checkout_with_item()
        with self.assertRaises(DRFValidationError) as exc:
            commerce.update_checkout(checkout, gift_card_code=raw_code)
        self.assertIn("gift_card_code", exc.exception.detail)

    def test_gift_card_covers_part_of_the_total_the_rest_is_due(self):
        from apps.commerce import services as commerce

        card, raw_code = self._card(balance="5.00")
        checkout = self._checkout_with_item()
        checkout = commerce.update_checkout(checkout, gift_card_code=raw_code)
        self.assertEqual(checkout.gift_card_amount, Decimal("5.00"))
        self.assertGreater(commerce.amount_due(checkout), Decimal("0.00"))

    def test_double_spending_the_same_gift_card_is_refused(self):
        card, raw_code = self._card(balance="5.00")
        change_balance(card, Decimal("-5.00"), "debit")
        with self.assertRaises(DRFValidationError):
            change_balance(card, Decimal("-1.00"), "debit")

    def test_refunding_a_gift_card_payment_credits_the_balance_back(self):
        from apps.payments import services as payments
        from apps.payments.models import Payment

        card, _ = self._card(balance="20.00")
        order = self._placed_order_paid_by_gift_card(card, Decimal("20.00"))
        payment = Payment.objects.get(order=order, provider="gift_card")
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("0.00"))
        payments.refund(payment, Decimal("20.00"), reason="test refund")
        card.refresh_from_db()
        self.assertEqual(card.current_balance, Decimal("20.00"))

    # -- helpers ----------------------------------------------------------------------------

    def _checkout_with_item(self):
        from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
        from apps.catalog.services import online_channels
        from apps.commerce import services as commerce
        from apps.inventory.models import Location
        from apps.inventory.services import set_available

        product = Product.objects.create(store=self.store, title="Test widget", status="active")
        variant = ProductVariant.objects.create(product=product, sku="W-1", price=Decimal("20.00"))
        for channel in online_channels(self.store):
            ProductSalesChannel.objects.create(product=product, sales_channel=channel)
        location = Location.objects.create(store=self.store, name="Main warehouse")
        set_available(variant, location, 10)

        cart = commerce.get_or_create_cart(self.store, session_id="s" * 24)
        commerce.add_item(cart, product, 1)
        return commerce.start_checkout(cart, email="buyer@example.com")

    def _placed_order_paid_by_gift_card(self, card, amount):
        from apps.commerce import services as commerce

        checkout = self._checkout_with_item()
        checkout = commerce.update_checkout(
            checkout,
            shipping_address={
                "first_name": "A", "last_name": "B", "address_line_1": "1 Test St", "city": "Sydney",
                "state": "NSW", "postal_code": "2000", "country": "Australia",
            },
            gift_card_code=services.reveal_code(card),
        )
        return commerce._place_order(checkout, "gift_card")
