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
    """Mark a pending payment (e.g. cash on delivery) as received."""
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status != PENDING:
        raise ValidationError("Only a pending payment can be captured.")

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


@transaction.atomic
def refund(payment, amount, reason=None, settle_order=True):
    """
    settle_order: after a refund that returns everything paid, the order is settled (see
    orders.services.settle_after_refund: cancelled and its stock back, if nothing shipped).
    cancel_order passes False because it does that itself.
    """
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
    if payment.provider == "stripe":
        from . import stripe_service

        refund_id = stripe_service.refund_payment(payment, amount, refunded_total(payment))

    PaymentTransaction.objects.create(
        payment=payment,
        transaction_type=REFUND,
        amount=amount,
        status=SUCCESS,
        reason=reason,
        provider_refund_id=refund_id,
    )
    payment.status = REFUNDED if amount == left else PARTIALLY_REFUNDED
    payment.save(update_fields=["status", "updated_at"])
    transaction.on_commit(lambda: payment_refunded.send(sender=Payment, payment=payment, refunded=refunded_total(payment)))
    if settle_order and order is not None:
        from apps.orders import services as orders

        orders.settle_after_refund(order)
    return payment


@transaction.atomic
def refund_unrefunded(order, reason="Order cancelled"):
    """
    Refund whatever is still unrefunded on the order's captured payments, one payment at a time
    (Stripe ones are refunded at Stripe first, see refund()). Manual and cash-on-delivery payments
    only get the refund recorded: staff hand the money back themselves. Nothing to refund does nothing.
    If any refund fails nothing is recorded for it and the error is raised. Returns the amount refunded now.
    """
    total = Decimal("0.00")
    ids = Payment.objects.filter(
        order=order, is_deleted=False, status__in=(CAPTURED, PARTIALLY_REFUNDED)
    ).order_by("created_at", "pk").values_list("pk", flat=True)
    for pk in list(ids):
        payment = Payment.objects.select_for_update().get(pk=pk)
        if payment.status not in (CAPTURED, PARTIALLY_REFUNDED):
            continue
        left = payment.amount - refunded_total(payment)
        if left > 0:
            refund(payment, left, reason, settle_order=False)
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
