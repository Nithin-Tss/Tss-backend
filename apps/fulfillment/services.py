"""
Fulfilling (packing) order items. Each fulfillment takes the items out of
stock (inventory app) and updates the order's fulfillment status (orders app),
all in one transaction.
"""
from django.db import transaction
from rest_framework.exceptions import ValidationError

from apps.inventory import services as inventory
from apps.orders.models import Order
from apps.orders.services import CANCELLED, FULFILLED, PARTIAL

from .models import Fulfillment, FulfillmentItem

DONE = "fulfilled"


def fulfilled_quantities(order):
    """{order_item_id: quantity already fulfilled}"""
    totals = {}
    rows = FulfillmentItem.objects.filter(fulfillment__order=order).exclude(fulfillment__status="cancelled")
    for row in rows:
        totals[row.order_item_id] = totals.get(row.order_item_id, 0) + row.quantity
    return totals


def remaining(order):
    """{order_item: quantity still to fulfill}"""
    done = fulfilled_quantities(order)
    return {
        item: item.quantity - done.get(item.pk, 0)
        for item in order.items.filter(is_deleted=False)
        if item.quantity - done.get(item.pk, 0) > 0
    }


@transaction.atomic
def fulfill(order, lines=None, delivery_method=None):
    """lines: [{"order_item", "quantity"}], or None for everything left."""
    order = Order.objects.select_for_update().get(pk=order.pk)

    if order.status == CANCELLED:
        raise ValidationError("This order is cancelled.")

    left = remaining(order)
    if not left:
        raise ValidationError("Everything in this order is already fulfilled.")

    if lines is None:
        lines = [{"order_item": item, "quantity": qty} for item, qty in left.items()]

    by_id = {item.pk: qty for item, qty in left.items()}
    for line in lines:
        item = line["order_item"]
        if item.order_id != order.pk:
            raise ValidationError({"items": f"{item.product_name} is not in this order."})
        if line["quantity"] > by_id.get(item.pk, 0):
            raise ValidationError(
                {"items": f"Only {by_id.get(item.pk, 0)} of {item.product_name} still need fulfilling."}
            )

    fulfillment = Fulfillment.objects.create(
        order=order, status=DONE, delivery_method=delivery_method or order.delivery_method
    )
    for line in lines:
        FulfillmentItem.objects.create(
            fulfillment=fulfillment, order_item=line["order_item"], quantity=line["quantity"]
        )
        inventory.ship(line["order_item"].variant, line["quantity"])

    order.fulfillment_status = FULFILLED if not remaining(order) else PARTIAL
    order.save(update_fields=["fulfillment_status", "updated_at"])
    return fulfillment
