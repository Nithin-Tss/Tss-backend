"""
Gift card balances. Every balance change writes a GiftCardTransaction with
the balance before and after, so the history always adds up.
"""
import secrets
from decimal import Decimal

from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import GiftCard, GiftCardTransaction

ACTIVE, DISABLED = "active", "disabled"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I lookalikes


def generate_code(store):
    while True:
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
        code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
        if not GiftCard.objects.filter(store=store, code=code).exists():
            return code


@transaction.atomic
def change_balance(gift_card, amount, kind, user=None, reason=None, order=None, checkout=None, payment=None):
    """
    amount > 0 adds money back (up to the original value), amount < 0 spends it.
    kind: "debit", "credit" or "adjustment".
    """
    card = GiftCard.objects.select_for_update().get(pk=gift_card.pk)
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))

    if card.status != ACTIVE:
        raise ValidationError("This gift card is disabled.")

    before = card.current_balance
    after = before + amount

    if after < 0:
        raise ValidationError(f"The gift card only has {before} left.")
    if after > card.initial_balance:
        raise ValidationError(f"The balance can't go above the original value of {card.initial_balance}.")

    card.current_balance = after
    card.save(update_fields=["current_balance", "updated_at"])

    GiftCardTransaction.objects.create(
        gift_card=card,
        transaction_type=kind,
        amount=amount,
        balance_before=before,
        balance_after=after,
        order=order,
        checkout=checkout,
        payment=payment,
        created_by_user=user,
        reason=reason,
    )
    return card
