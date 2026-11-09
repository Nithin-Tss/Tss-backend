"""
Payments. A Payment is money received for an order; each capture or refund
is a PaymentTransaction. Refunds can never exceed what was captured.

Providers: "manual" (cash, bank transfer, cash on delivery) and "stripe"
(see stripe_service.py), whose refunds are sent to Stripe before they are recorded.
"""
from decimal import Decimal

from django.db import transaction
from django.dispatch import Signal
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Payment, PaymentTransaction

PENDING = "pending"
CAPTURED, PARTIALLY_REFUNDED, REFUNDED = "captured", "partially_refunded", "refunded"
CAPTURE, REFUND, SUCCESS = "capture", "refund", "success"
PROVIDERS = ("manual",)
CANCELLED = "cancelled"

# Announcements other parts of the system can listen to (webhooks); sent after the transaction commits.
payment_captured = Signal()   # sender=Payment, payment=...
payment_refunded = Signal()   # sender=Payment, payment=..., refunded=<total refunded so far>


def refunded_total(payment):
    return payment.transactions.filter(transaction_type=REFUND, status=SUCCESS).aggregate(
        total=Sum("amount")
    )["total"] or Decimal("0.00")


@transaction.atomic
def record_payment(store, order, amount, provider="manual", currency=None, reference=None):
    payment = Payment.objects.create(
        store=store,
        order=order,
        customer=order.customer,
        currency=currency or order.currency_code,
        amount=amount,
        status=CAPTURED,
        provider=provider,
        provider_payment_id=reference,
        metadata={},
        is_deleted=False,
    )
    PaymentTransaction.objects.create(
        payment=payment,
        transaction_type=CAPTURE,
        amount=amount,
        status=SUCCESS,
        provider_transaction_id=reference,
    )
    transaction.on_commit(lambda: payment_captured.send(sender=Payment, payment=payment))
    return payment


@transaction.atomic
def record_pending_payment(store, order, checkout, amount, provider="cod", customer=None):
    """A payment promised but not yet received (e.g. cash on delivery)."""
    return Payment.objects.create(
        store=store,
        order=order,
        checkout=checkout,
        customer=customer or order.customer,
        currency=order.currency_code,
        amount=amount,
        status=PENDING,
        provider=provider,
        metadata={},
        is_deleted=False,
    )


@transaction.atomic
def capture_payment(payment, reference=None):
    """
    Mark a pending payment (e.g. cash on delivery) as received.
    Locks the order before the payment, like cancel_order and refund, so a capture and a cancellation
    of the same order run one after the other. Under that lock the order must still be open and the
    capture must not take the order past its total.
    """
    order = _lock_order_of(payment)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != PENDING:
        raise ValidationError("Only a pending payment can be captured.")
    if order is not None:
        if order.status == CANCELLED:
            raise ValidationError({"order": "This order is cancelled, so its payment can't be captured."})
        due = order.total - paid_total(order)
        if payment.amount > due:
            raise ValidationError({"amount": f"Only {max(due, Decimal('0.00'))} is still due on this order."})

    PaymentTransaction.objects.create(
        payment=payment,
        transaction_type=CAPTURE,
        amount=payment.amount,
        status=SUCCESS,
        provider_transaction_id=reference,
    )
    payment.status = CAPTURED
    if reference:
        payment.provider_payment_id = reference
    payment.save(update_fields=["status", "provider_payment_id", "updated_at"])
    transaction.on_commit(lambda: payment_captured.send(sender=Payment, payment=payment))
    return payment


def _lock_order_of(payment):
    """Lock the payment's order row. Always done BEFORE the payment row, the same order cancel_order uses."""
    if not payment.order_id:
        return None
    from apps.orders.models import Order

    return Order.objects.select_for_update().filter(pk=payment.order_id).first()


def refunded_in_full(order):
    """True when money was paid for this order and everything refunded adds up to at least its total."""
    payments = Payment.objects.filter(
        order=order, is_deleted=False, status__in=(CAPTURED, PARTIALLY_REFUNDED, REFUNDED)
    )
    refunded = sum((refunded_total(p) for p in payments), Decimal("0.00"))
    return refunded > 0 and refunded >= order.total


GIFT_CARD, ORIGINAL, LATER = "gift_card", "original", "later"
REFUND_DESTINATIONS = (GIFT_CARD, ORIGINAL, LATER)


def default_destination(payment):
    """Where a payment's refund goes unless staff choose otherwise: a gift card payment back to its card."""
    return GIFT_CARD if payment.provider == "gift_card" else ORIGINAL


def _check_destination(payment, destination):
    if destination == LATER:
        return
    if payment.provider == "gift_card" and destination != GIFT_CARD:
        raise ValidationError({"destination": "The part paid by gift card can only be refunded to the gift card (or later)."})
    if payment.provider != "gift_card" and destination == GIFT_CARD:
        raise ValidationError({"destination": "Only the part paid by gift card can be refunded to a gift card."})


@transaction.atomic
def refund(payment, amount, reason=None, settle_order=True, destination=None, user=None):
    """
    settle_order: after a refund that returns everything paid, the order is settled (see
    orders.services.settle_after_refund: cancelled and its stock back, if nothing shipped).
    cancel_order passes False because it does that itself.
    destination: "gift_card" (a gift card payment's only real one: the balance goes back on the
    card) or "original" (Stripe/manual/cash); None picks the right one for the payment. "Later"
    means no refund is recorded now, so it is the caller's job not to call this at all.
    """
    destination = destination or default_destination(payment)
    if destination == LATER:
        raise ValidationError({"destination": "\"Later\" records nothing; just don't refund yet."})
    _check_destination(payment, destination)
    order = _lock_order_of(payment)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status not in (CAPTURED, PARTIALLY_REFUNDED):
        raise ValidationError({
            "payment": "Only captured or partially refunded payments can be refunded."
        })
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    left = payment.amount - refunded_total(payment)

    if amount <= 0:
        raise ValidationError({"amount": "Must be more than 0."})
    if amount > left:
        raise ValidationError({"amount": f"Only {left} can still be refunded."})

    refund_id = None
    tx_metadata = {"destination": destination}
    if payment.provider == "stripe":
        from . import stripe_service

        refund_id = stripe_service.refund_payment(payment, amount, refunded_total(payment))
    elif payment.provider == "gift_card":
        from apps.gift_cards import services as gift_cards

        gift_cards.refund_payment_amount(payment, amount, user=user)
        card = gift_cards.card_for_payment(payment)
        if card is not None:
            tx_metadata["gift_card_last4"] = card.code_last4

    PaymentTransaction.objects.create(
        payment=payment,
        transaction_type=REFUND,
        amount=amount,
        status=SUCCESS,
        reason=reason,
        provider_refund_id=refund_id,
        metadata=tx_metadata,
    )
    payment.status = REFUNDED if amount == left else PARTIALLY_REFUNDED
    payment.save(update_fields=["status", "updated_at"])
    transaction.on_commit(lambda: payment_refunded.send(sender=Payment, payment=payment, refunded=refunded_total(payment)))
    if settle_order and order is not None:
        from apps.orders import services as orders

        orders.settle_after_refund(order)
    return payment


@transaction.atomic
def refund_unrefunded(order, reason="Order cancelled", destinations=None, user=None):
    """
    Refund whatever is still unrefunded on the order's captured payments, one payment at a time
    (Stripe ones are refunded at Stripe first, see refund()). Manual and cash-on-delivery payments
    only get the refund recorded: staff hand the money back themselves. Nothing to refund does nothing.
    If any refund fails nothing is recorded for it and the error is raised. Returns the amount refunded now.

    destinations: {payment id: "gift_card" | "original" | "later"}. A payment not in it goes to its
    default (gift card payments back to their card first). "later" leaves that payment untouched:
    no balance changes and no refund is recorded; staff refund it from the payment when ready.
    """
    destinations = {str(k): v for k, v in (destinations or {}).items()}
    for key, value in destinations.items():
        if value not in REFUND_DESTINATIONS:
            raise ValidationError({"refund_destinations": f"Unknown refund destination {value!r}."})
    total = Decimal("0.00")
    # Gift card payments first: that money goes back on the card before the rest is refunded
    payments = sorted(
        Payment.objects.filter(order=order, is_deleted=False, status__in=(CAPTURED, PARTIALLY_REFUNDED)),
        key=lambda p: (p.provider != "gift_card", p.created_at, str(p.pk)),
    )
    for p in payments:
        payment = Payment.objects.select_for_update().get(pk=p.pk)
        if payment.status not in (CAPTURED, PARTIALLY_REFUNDED):
            continue
        destination = destinations.get(str(payment.pk)) or default_destination(payment)
        if destination == LATER:
            continue
        left = payment.amount - refunded_total(payment)
        if left > 0:
            refund(payment, left, reason, settle_order=False, destination=destination, user=user)
            total += left
    return total


@transaction.atomic
def record_external_refund(payment, total_refunded, reason="refunded_at_provider"):
    """
    The provider says `total_refunded` of this payment has been refunded in all (e.g. from the
    Stripe dashboard). Record whatever we don't know about yet; safe to repeat.
    Returns the new refund amount, or None if there was nothing new.
    """
    order = _lock_order_of(payment)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status not in (CAPTURED, PARTIALLY_REFUNDED, REFUNDED):
        return None
    already = refunded_total(payment)
    new = min(Decimal(total_refunded), payment.amount) - already
    if new <= 0:
        return None
    PaymentTransaction.objects.create(
        payment=payment,
        transaction_type=REFUND,
        amount=new,
        status=SUCCESS,
        reason=reason,
    )
    payment.status = REFUNDED if already + new >= payment.amount else PARTIALLY_REFUNDED
    payment.save(update_fields=["status", "updated_at"])
    transaction.on_commit(lambda: payment_refunded.send(sender=Payment, payment=payment, refunded=refunded_total(payment)))
    if order is not None:
        from apps.orders import services as orders

        orders.settle_after_refund(order)
    return new


def cancel_pending(order):
    """The order is cancelled: promised-but-unreceived payments (e.g. cash on delivery) are void."""
    Payment.objects.filter(order=order, is_deleted=False, status=PENDING).update(
        status=CANCELLED, updated_at=timezone.now()
    )


def paid_total(order):
    """Return captured payments minus successful refunds."""
    total = Decimal("0.00")
    payments = Payment.objects.filter(
        order=order,
        is_deleted=False,
        status__in=(CAPTURED, PARTIALLY_REFUNDED, REFUNDED),
    )
    for payment in payments:
        total += payment.amount - refunded_total(payment)

    return total
