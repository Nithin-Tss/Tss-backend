"""
Shipments. Creating a shipment, adding tracking events and marking it
delivered also update the order's delivery status (orders app).
"""
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Shipment, ShipmentItem, TrackingEvent

IN_TRANSIT, DELIVERED = "in_transit", "delivered"
ORDER_SHIPPED, ORDER_DELIVERED = "shipped", "delivered"


def _set_order_status(shipment, status):
    order = shipment.fulfillment.order
    order.delivery_status = status
    order.save(update_fields=["delivery_status", "updated_at"])


@transaction.atomic
def ship(store, fulfillment, gateway, tracking_number=None):
    if fulfillment.status == "cancelled":
        raise ValidationError({"fulfillment": "This fulfillment is cancelled."})
    if Shipment.objects.filter(fulfillment=fulfillment).exists():
        raise ValidationError({"fulfillment": "This fulfillment already has a shipment."})
    if not gateway.is_active:
        raise ValidationError({"delivery_gateway": "This delivery partner is turned off."})

    now = timezone.now()
    shipment = Shipment.objects.create(
        store=store,
        fulfillment=fulfillment,
        delivery_gateway=gateway,
        tracking_number=tracking_number,
        status=IN_TRANSIT,
        shipped_at=now,
    )
    for item in fulfillment.items.all():
        ShipmentItem.objects.create(
            shipment=shipment, order_item=item.order_item, quantity=item.quantity, is_deleted=False
        )
    add_event(shipment, IN_TRANSIT, description="Shipped", event_at=now)
    _set_order_status(shipment, ORDER_SHIPPED)
    return shipment


@transaction.atomic
def add_event(shipment, status, location=None, description=None, event_at=None):
    if shipment.status == DELIVERED:
        raise ValidationError("This shipment is already delivered.")

    event = TrackingEvent.objects.create(
        shipment=shipment,
        status=status,
        location=location,
        description=description,
        event_at=event_at or timezone.now(),
        is_deleted=False,
    )

    if status == DELIVERED:
        shipment.status = DELIVERED
        shipment.delivered_at = event.event_at
        shipment.save(update_fields=["status", "delivered_at", "updated_at"])
        _set_order_status(shipment, ORDER_DELIVERED)
    elif status != shipment.status:
        shipment.status = status
        shipment.save(update_fields=["status", "updated_at"])

    return event
