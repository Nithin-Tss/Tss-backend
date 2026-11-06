"""
Gift card balances. Every balance change writes a GiftCardTransaction with
the balance before and after, so the history always adds up.
"""
import base64
import hashlib
import logging
import secrets
from decimal import Decimal

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import GiftCard, GiftCardCurrencySetting, GiftCardTransaction

logger = logging.getLogger(__name__)

ACTIVE, DISABLED = "active", "disabled"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I lookalikes


# -- Code encryption (same approach as apps.payments.crypto for Stripe keys, with its own ------
# -- domain-separated key so the two secrets never share key material) -------------------------

def _fernet():
    digest = hashlib.sha256(f"gift-card-codes:{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value):
    return _fernet().encrypt(value.encode()).decode() if value else ""


def _decrypt(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""


def _lookup_hash(store_id, code):
    """A deterministic, non-reversible fingerprint for exact-match lookup, never logged or displayed."""
    return hashlib.sha256(f"{settings.SECRET_KEY}:{store_id}:{code.upper()}".encode()).hexdigest()


def _raw_code():
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def generate_code(store):
    """
    A fresh, unused gift card code for this store. Returns the raw code (shown once to the caller)
    plus the fields that are actually persisted: a lookup hash, an encrypted copy, and the last 4
    characters for display. The raw code itself is never written to the database or logged.
    """
    while True:
        raw = _raw_code()
        code_hash = _lookup_hash(store.pk, raw)
        if not GiftCard.objects.filter(store=store, code_hash=code_hash).exists():
            return {
                "raw": raw,
                "code_hash": code_hash,
                "code_enc": _encrypt(raw),
                "code_last4": raw[-4:],
            }


def get_by_code(store, raw_code):
    """Look up an active gift card by the code a customer typed in, or None."""
    raw_code = (raw_code or "").strip().upper()
    if not raw_code:
        return None
    return GiftCard.objects.filter(
        store=store, code_hash=_lookup_hash(store.pk, raw_code), is_deleted=False
    ).first()


def reveal_code(card):
    """The one-time plaintext code, for the creation response and the purchase/resend email only."""
    return _decrypt(card.code_enc)


# -- Currencies and amounts ---------------------------------------------------------------------

def enabled_currencies(store):
    return GiftCardCurrencySetting.objects.filter(store=store, is_enabled=True).order_by("currency_code")


def seed_default_currency(store):
    """A store with no gift card currency configured yet sells AUD 25/50/100/200 by default."""
    from .currencies import DEFAULT_AMOUNTS, DEFAULT_CURRENCY

    setting, _ = GiftCardCurrencySetting.objects.get_or_create(
        store=store,
        currency_code=DEFAULT_CURRENCY,
        defaults={"is_enabled": True, "amounts": [str(a) for a in DEFAULT_AMOUNTS]},
    )
    return setting


def amount_is_offered(store, currency_code, amount):
    setting = GiftCardCurrencySetting.objects.filter(
        store=store, currency_code=currency_code, is_enabled=True
    ).first()
    if setting is None:
        return False
    offered = {Decimal(str(a)) for a in (setting.amounts or [])}
    return Decimal(str(amount)) in offered


# -- Balance changes -----------------------------------------------------------------------------

@transaction.atomic
def change_balance(gift_card, amount, kind, user=None, reason=None, order=None, checkout=None, payment=None):
    """
    amount > 0 adds money back (up to the original value), amount < 0 spends it.
    kind: "debit", "credit" or "adjustment".
    """
    from .currencies import quantize

    card = GiftCard.objects.select_for_update().get(pk=gift_card.pk)
    amount = quantize(card.currency_code, amount)

    if card.status != ACTIVE:
        raise ValidationError("This gift card is disabled.")
    if amount < 0 and card.expires_at and card.expires_at <= timezone.now():
        raise ValidationError("This gift card has expired.")

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


def deactivate(card, user=None, reason=None):
    if card.status == DISABLED:
        return card
    card.status = DISABLED
    card.save(update_fields=["status", "updated_at"])
    GiftCardTransaction.objects.create(
        gift_card=card,
        transaction_type="adjustment",
        amount=Decimal("0"),
        balance_before=card.current_balance,
        balance_after=card.current_balance,
        created_by_user=user,
        reason=reason or "Deactivated",
    )
    return card


def refund_payment_amount(payment, amount):
    """
    A gift-card-provider Payment was refunded by `amount`: credit that much back to the card(s)
    it paid, split across them in proportion to what each one covered (the common case is one
    card paying the whole amount). Called from apps.payments.services.refund().
    """
    debits = list(
        GiftCardTransaction.objects.filter(payment=payment, transaction_type="debit").select_related("gift_card")
    )
    total_debited = sum((-d.amount for d in debits), Decimal("0"))
    if total_debited <= 0:
        logger.warning("Gift card payment %s refunded but no debit is on record to credit back", payment.pk)
        return
    amount = Decimal(str(amount))
    remaining = amount
    for i, debit in enumerate(debits):
        share = amount if i == len(debits) - 1 else (-debit.amount / total_debited * amount).quantize(Decimal("0.001"))
        share = min(share, remaining, -debit.amount)
        if share > 0:
            change_balance(debit.gift_card, share, kind="credit", payment=payment, reason="Refunded")
            remaining -= share
