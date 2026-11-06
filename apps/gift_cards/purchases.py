"""
A customer buying a gift card for themselves or someone else. This is a standalone digital-good
purchase -- it never touches the Cart/Checkout/fulfillment path used for physical products, so it
can't be used to buy another gift card and never waits on shipping.

Flow: create_purchase_intent() opens a Stripe PaymentIntent on the store's own connected account
(reusing apps.payments.stripe_service, same as a normal checkout). finalize_purchase() is called
from the Stripe webhook once the intent succeeds; it is safe to call more than once (webhook
retries): the same GiftCard comes back every time.
"""
import logging

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.payments import stripe_service
from apps.payments.models import Payment

from . import emails, services
from .currencies import quantize
from .models import PAID, PENDING, GiftCard, GiftCardPurchase, GiftCardTransaction

logger = logging.getLogger(__name__)


def create_purchase_intent(
    store, currency_code, amount, buyer_email,
    recipient_email=None, recipient_name=None, message=None, send_on=None, buyer_customer=None,
):
    if not services.amount_is_offered(store, currency_code, amount):
        raise ValidationError({"amount": "Choose one of the gift card amounts this store offers."})
    stripe_service._require_connected(store)

    amount = quantize(currency_code, amount)
    purchase = GiftCardPurchase.objects.create(
        store=store,
        currency_code=currency_code,
        amount=amount,
        status=PENDING,
        buyer_customer=buyer_customer,
        buyer_email=buyer_email,
        recipient_email=recipient_email or buyer_email,
        recipient_name=recipient_name or "",
        message=message or "",
        send_on=send_on,
    )

    minor = stripe_service.to_minor_units(amount, currency_code)
    intent = stripe_service.client(store).v1.payment_intents.create(
        {
            "amount": minor,
            "currency": currency_code.lower(),
            "automatic_payment_methods": {"enabled": True},
            "receipt_email": buyer_email,
            "metadata": {"gift_card_purchase_id": str(purchase.pk), "store_id": str(store.pk)},
        },
        options={"idempotency_key": f"gift-card-purchase-{purchase.pk}"},
    )
    purchase.stripe_payment_intent_id = intent.id
    purchase.save(update_fields=["stripe_payment_intent_id", "updated_at"])

    return {
        "purchase_id": str(purchase.pk),
        "client_secret": intent.client_secret,
        "publishable_key": store.stripe_publishable_key,
        "amount": str(amount),
        "currency": currency_code,
    }


@transaction.atomic
def finalize_purchase(intent, account_id):
    """payment_intent.succeeded for a gift card purchase: issue the card. Idempotent."""
    if intent.status != stripe_service.SUCCEEDED:
        return None
    purchase_id = stripe_service.plain(intent.metadata).get("gift_card_purchase_id")
    purchase = (
        GiftCardPurchase.objects.select_for_update()
        .filter(pk=purchase_id, store__stripe_account_id=account_id)
        .select_related("store")
        .first()
        if purchase_id
        else None
    )
    if purchase is None:
        logger.error("Stripe gift card payment %s matches no purchase on account %s", intent.id, account_id)
        return None
    if purchase.status == PAID:
        return purchase.gift_card  # already handled by an earlier delivery of this event

    minor = stripe_service.to_minor_units(purchase.amount, purchase.currency_code)
    if intent.amount != minor or intent.currency != purchase.currency_code.lower():
        logger.error("Stripe gift card payment %s amount/currency does not match purchase %s", intent.id, purchase.pk)
        return None

    code = services.generate_code(purchase.store)
    card = GiftCard.objects.create(
        store=purchase.store,
        code_hash=code["code_hash"],
        code_enc=code["code_enc"],
        code_last4=code["code_last4"],
        initial_balance=purchase.amount,
        current_balance=purchase.amount,
        currency_code=purchase.currency_code,
        status=services.ACTIVE,
        customer=purchase.buyer_customer,
        expires_at=None,  # a purchased card never expires
        is_deleted=False,
    )
    # Not apps.payments.services.record_payment: that requires an order (it reads order.customer).
    # A gift card purchase has no order, so the Payment is created directly here.
    payment = Payment.objects.create(
        store=purchase.store,
        order=None,
        checkout=None,
        customer=purchase.buyer_customer,
        currency=purchase.currency_code,
        amount=purchase.amount,
        status="captured",
        provider="stripe",
        provider_payment_id=intent.id,
        metadata={"gift_card_purchase_id": str(purchase.pk)},
        is_deleted=False,
    )
    GiftCardTransaction.objects.create(
        gift_card=card,
        transaction_type="credit",
        amount=purchase.amount,
        balance_before=0,
        balance_after=purchase.amount,
        payment=payment,
        reason="Purchased",
    )

    purchase.status = PAID
    purchase.gift_card = card
    purchase.payment = payment
    purchase.sent_at = timezone.now()
    purchase.save(update_fields=["status", "gift_card", "payment", "sent_at", "updated_at"])

    raw_code = services.reveal_code(card)
    emails.gift_card_issued(
        card, raw_code, purchase.recipient_email,
        recipient_name=purchase.recipient_name or None,
        message=purchase.message or None,
        from_name=None if purchase.recipient_email == purchase.buyer_email else purchase.buyer_email,
    )
    return card


@transaction.atomic
def refund_purchase(purchase, reason=None):
    """
    Refund the money a customer paid for a gift card, and deactivate the card so it can't still
    be spent after the money was returned. Refused if any of it has already been redeemed --
    the store would be out that amount with no way to claw it back automatically.
    """
    from apps.payments import services as payments

    purchase = GiftCardPurchase.objects.select_for_update().get(pk=purchase.pk)
    if purchase.status != PAID or purchase.payment_id is None:
        raise ValidationError("This purchase hasn't been paid, so there's nothing to refund.")
    card = GiftCard.objects.select_for_update().get(pk=purchase.gift_card_id)
    if card.current_balance != card.initial_balance:
        raise ValidationError(
            "Some of this gift card has already been redeemed. Contact the customer before refunding it."
        )
    payments.refund(purchase.payment, purchase.payment.amount, reason=reason or "Gift card purchase refunded")
    services.deactivate(card, reason="Purchase refunded")
    purchase.status = "refunded"
    purchase.save(update_fields=["status", "updated_at"])
    return purchase
