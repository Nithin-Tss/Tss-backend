"""Turns the order and payment announcements into queued webhook deliveries."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.catalog.models import Product
from apps.commerce.models import Checkout
from apps.commerce.services import checkout_abandoned
from apps.customers.models import Customer
from apps.delivery.models import Shipment
from apps.delivery.services import shipment_delivered
from apps.fulfillment.models import Fulfillment
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


# Records have no announcement of their own, so listen for the row being created. Waiting for the commit means
# the lines saved right after it (a product's variants, a fulfillment's items) are in the payload too.

def _on_created(model, event_type, describe, store_of):
    def listener(sender, instance, created, raw=False, **kwargs):
        if created and not raw:
            transaction.on_commit(lambda: _emit(store_of(instance), event_type, lambda: describe(instance)))

    post_save.connect(listener, sender=model, weak=False, dispatch_uid=f"webhooks-{event_type}")


_on_created(Customer, events.CUSTOMER_CREATED, events.describe_customer, lambda c: c.store)
_on_created(Product, events.PRODUCT_CREATED, events.describe_product, lambda p: p.store)
_on_created(Fulfillment, events.FULFILLMENT_CREATED, events.describe_fulfillment, lambda f: f.order.store)
_on_created(Shipment, events.SHIPMENT_CREATED, events.describe_shipment, lambda s: s.store)


@receiver(shipment_delivered)
def on_shipment_delivered(sender, shipment, **kwargs):
    _emit(shipment.store, events.SHIPMENT_DELIVERED, lambda: events.describe_shipment(shipment))


@receiver(checkout_abandoned)
def on_checkout_abandoned(sender, checkout, **kwargs):
    _emit(checkout.store, events.CHECKOUT_ABANDONED, lambda: events.describe_checkout(checkout))
