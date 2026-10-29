"""Turns the order and payment announcements into queued webhook deliveries."""
import logging

from django.dispatch import receiver

from apps.orders.services import order_cancelled, order_placed
from apps.payments.services import payment_captured, payment_refunded

from . import events, services

logger = logging.getLogger(__name__)


def _emit(store, event_type, build):
    # Runs after commit; a webhook problem must never undo or break the order or the payment
    try:
        services.emit(store, event_type, build())
    except Exception:
        logger.exception("Could not queue the %s webhook", event_type)


@receiver(order_placed)
def on_order_placed(sender, order, **kwargs):
    _emit(order.store, events.ORDER_CREATED, lambda: events.describe_order(order))


@receiver(order_cancelled)
def on_order_cancelled(sender, order, **kwargs):
    _emit(order.store, events.ORDER_CANCELLED, lambda: events.describe_order(order))


@receiver(payment_captured)
def on_payment_captured(sender, payment, **kwargs):
    _emit(payment.store, events.PAYMENT_CAPTURED, lambda: events.describe_payment(payment))


@receiver(payment_refunded)
def on_payment_refunded(sender, payment, refunded, **kwargs):
    _emit(payment.store, events.PAYMENT_REFUNDED, lambda: events.describe_payment(payment, refunded))
