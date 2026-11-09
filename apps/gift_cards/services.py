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


def normalize_code(raw_code):
    """Case-insensitive, spaces ignored; a 16-character code typed without its dashes still matches."""
    code = "".join((raw_code or "").split()).upper()
    if code and "-" not in code and len(code) == 16:
        code = "-".join(code[i:i + 4] for i in range(0, 16, 4))
    return code


def get_by_code(store, raw_code):
    """Look up a gift card of this store by the code a customer typed in, or None."""
    raw_code = normalize_code(raw_code)
    if not raw_code:
        return None
    return GiftCard.objects.filter(
        store=store, code_hash=_lookup_hash(store.pk, raw_code), is_deleted=False
    ).first()


def problem_with_card(card, currency):
    """Why this card can't pay an order in `currency` right now (a message for the shopper), or None."""
    if card.status != ACTIVE:
        return "This gift card has been deactivated."
    if card.expires_at and card.expires_at <= timezone.now():
        return "This gift card has expired."
    if card.currency_code != currency:
        return f"This gift card is in {card.currency_code}; this order is in {currency}."
    if card.current_balance <= 0:
        return "This gift card has no balance left."
    return None


# -- Wrong-code rate limit -----------------------------------------------------------------------
# Counted per store and client address in the shared cache (Redis in production), so guessing codes
# is slow whichever endpoint the guesses come through. A correct code resets nothing on purpose:
# only time does, so a guesser can't interleave one known good code to keep going.

MAX_WRONG_CODES = 10
WRONG_CODE_WINDOW = 15 * 60  # seconds


def _attempts_key(store, ident):
    return f"gift-card-wrong-codes:{store.pk}:{ident}"


def check_code_attempts(store, ident):
    from django.core.cache import cache
    from rest_framework.exceptions import Throttled

    if ident and (cache.get(_attempts_key(store, ident)) or 0) >= MAX_WRONG_CODES:
        raise Throttled(detail="Too many incorrect codes. Please wait a few minutes and try again.")


def record_wrong_code(store, ident):
    from django.core.cache import cache

    if not ident:
        return
    key = _attempts_key(store, ident)
    cache.add(key, 0, WRONG_CODE_WINDOW)
    try:
        cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, WRONG_CODE_WINDOW)


class code_attempts:
    """
    `with code_attempts(store, ident):` around applying a code. Refuses at once when this client
    has already got too many wrong ones, and counts a new wrong one when the block raises a
    validation error about the code (as opposed to, say, an empty cart).
    """

    def __init__(self, store, ident):
        self.store, self.ident = store, ident

    def __enter__(self):
        check_code_attempts(self.store, self.ident)

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and isinstance(exc, ValidationError):
            detail = exc.detail
            if isinstance(detail, dict) and ({"code", "gift_card_code"} & set(detail)):
                record_wrong_code(self.store, self.ident)
        return False


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


# -- The storefront "Gift Card" product -----------------------------------------------------
#
# The cart and checkout always run in the store's own currency (apps.commerce.services pins
# checkout.currency to store.currency), so the product customers actually buy from only ever
# offers amounts in that one currency, even if other currencies are enabled for admin-issued
# cards. GIFT_CARD_PRODUCT_TYPE marks it so checkout/order code can special-case it (no cash on
# delivery, no gift card may pay for it, its order items get a GiftCard issued once paid).

GIFT_CARD_PRODUCT_TYPE = "gift_card"


def sync_storefront_product(store):
    """
    Create or refresh the store's "Gift Card" product: one variant per fixed amount in the
    store's own currency. Safe to call repeatedly (e.g. whenever the amounts change).
    """
    from apps.catalog.models import Product, ProductSalesChannel, ProductVariant
    from apps.catalog.services import online_channels

    from .currencies import DEFAULT_AMOUNTS

    setting = GiftCardCurrencySetting.objects.filter(
        store=store, currency_code=store.currency, is_enabled=True
    ).first()
    if setting is None:
        setting = GiftCardCurrencySetting.objects.create(
            store=store, currency_code=store.currency, is_enabled=True,
            amounts=[str(a) for a in DEFAULT_AMOUNTS],
        )
    amounts = sorted({Decimal(str(a)) for a in (setting.amounts or [])})
    if not amounts:
        return None

    product, created = Product.objects.get_or_create(
        store=store,
        product_type=GIFT_CARD_PRODUCT_TYPE,
        defaults={"title": "Gift Card", "status": "active", "is_taxable": False},
    )
    if created:
        for channel in online_channels(store):
            ProductSalesChannel.objects.get_or_create(product=product, sales_channel=channel)

    existing = {v.sku: v for v in product.variants.all()}
    for amount in amounts:
        sku = f"GIFTCARD-{amount.normalize()}"
        variant = existing.pop(sku, None)
        if variant is None:
            ProductVariant.objects.create(
                product=product, sku=sku, price=amount, continue_selling=True,
            )
        elif variant.price != amount or not variant.continue_selling:
            variant.price = amount
            variant.continue_selling = True
            variant.save(update_fields=["price", "continue_selling", "updated_at"])
    # Amounts removed from the currency setting stop being sellable, but existing orders/variants
    # referencing them are left alone (variants are never deleted once a product can reference them).
    for stale in existing.values():
        if stale.continue_selling:
            stale.continue_selling = False
            stale.save(update_fields=["continue_selling", "updated_at"])
    return product


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


def refund_payment_amount(payment, amount, user=None):
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
    # Checked before anything moves, so a card that can't take the money stops the whole refund
    for debit in debits:
        check_can_receive_refund(debit.gift_card)
    amount = Decimal(str(amount))
    remaining = amount
    for i, debit in enumerate(debits):
        share = amount if i == len(debits) - 1 else (-debit.amount / total_debited * amount).quantize(Decimal("0.001"))
        share = min(share, remaining, -debit.amount)
        if share > 0:
            change_balance(
                debit.gift_card, share, kind="credit", payment=payment,
                order=debit.order, reason="Refunded", user=user,
            )
            remaining -= share


def check_can_receive_refund(card):
    """A deactivated card never takes money back; an expired one needs a new expiry date first."""
    if card.status != ACTIVE:
        raise ValidationError(
            {"gift_card": f"Gift card ••••{card.code_last4} is deactivated and can't receive a refund. "
                          "Refund to another destination, or choose \"later\"."}
        )
    if card.expires_at and card.expires_at <= timezone.now():
        raise ValidationError(
            {"gift_card": f"Gift card ••••{card.code_last4} has expired. Set a new expiry date on the card "
                          "before refunding to it."}
        )


def card_for_payment(payment):
    """The gift card a gift-card Payment was spent from (one Payment per card), or None."""
    debit = (
        GiftCardTransaction.objects.filter(payment=payment, transaction_type="debit")
        .select_related("gift_card").order_by("created_at").first()
    )
    return debit.gift_card if debit else None
