from decimal import Decimal

from django.core.exceptions import ValidationError
from rest_framework.test import APITestCase

from apps.core.tests.test_flow import StoreClientMixin
from apps.gift_cards.models import GiftCard, GiftCardTransaction
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

        self.card = GiftCard.objects.create(
            store=self.store,
            code="TEST-GC-001",
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