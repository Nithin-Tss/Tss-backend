"""Sends the store's notification emails when an order is placed or cancelled, a payment is refunded or a shipment arrives."""
import logging

from django.dispatch import receiver

from apps.delivery.services import shipment_delivered
from apps.orders.services import order_cancelled, order_placed
from apps.payments.services import payment_refunded

from . import services

logger = logging.getLogger(__name__)


def _safely(name, fn, *args):
    try:
        fn(*args)
    except Exception:
        logger.exception("Could not send the %s notification", name)


@receiver(order_placed)
def on_order_placed(sender, order, **kwargs):
    _safely("order placed", services.order_placed, order)


@receiver(order_cancelled)
def on_order_cancelled(sender, order, **kwargs):
    _safely("order cancelled", services.order_cancelled, order)


@receiver(payment_refunded)
def on_payment_refunded(sender, payment, refunded, **kwargs):
    _safely("refund", services.payment_refunded, payment, refunded)


@receiver(shipment_delivered)
def on_shipment_delivered(sender, shipment, **kwargs):
    _safely("delivered", services.shipment_delivered, shipment)
