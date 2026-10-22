"""
Payments. A Payment is money received for an order; each capture or refund
is a PaymentTransaction. Refunds can never exceed what was captured.

Only the "manual" provider (cash, bank transfer, cash on delivery) exists
for now. An online provider (Razorpay, Stripe, ...) plugs in here later.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework.exceptions import ValidationError

from .models import Payment, PaymentTransaction

PENDING = "pending"
CAPTURED, PARTIALLY_REFUNDED, REFUNDED = "captured", "partially_refunded", "refunded"
CAPTURE, REFUND, SUCCESS = "capture", "refund", "success"
PROVIDERS = ("manual",)


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
    return payment


@transaction.atomic
def refund(payment, amount, reason=None):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.status not in (CAPTURED, PARTIALLY_REFUNDED):
        raise ValidationError("Only a captured payment can be refunded.")
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    left = payment.amount - refunded_total(payment)

    if amount <= 0:
        raise ValidationError({"amount": "Must be more than 0."})
    if amount > left:
        raise ValidationError({"amount": f"Only {left} can still be refunded."})

    PaymentTransaction.objects.create(
        payment=payment,
        transaction_type=REFUND,
        amount=amount,
        status=SUCCESS,
        reason=reason,
    )
    payment.status = REFUNDED if amount == left else PARTIALLY_REFUNDED
    payment.save(update_fields=["status", "updated_at"])
    return payment


def paid_total(order):
    """Captured minus refunded, across all received payments for this order (pending ones don't count)."""
    total = Decimal("0.00")
    received = (CAPTURED, PARTIALLY_REFUNDED, REFUNDED)
    for payment in Payment.objects.filter(order=order, is_deleted=False, status__in=received):
        total += payment.amount - refunded_total(payment)
    return total
