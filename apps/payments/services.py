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
        order_id=order.pk,
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
def refund(payment, amount, reason=None):
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
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
    """Captured minus refunded, across all payments for this order."""
    total = Decimal("0.00")
    for payment in Payment.objects.filter(order_id=order.pk, is_deleted=False):
        total += payment.amount - refunded_total(payment)
    return total
