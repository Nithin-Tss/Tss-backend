"""
Shipments. Creating a shipment, adding tracking events and marking it
delivered also update the order's delivery status (orders app).
"""
from django.db import transaction
from django.dispatch import Signal
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from . import providers
from .models import LOCAL, Shipment, ShipmentItem, TrackingEvent

shipment_delivered = Signal()  # sender=Shipment, shipment=...

IN_TRANSIT, DELIVERED = "in_transit", "delivered"
ORDER_SHIPPED, ORDER_DELIVERED = "shipped", "delivered"


def _set_order_status(shipment, status):
    order = shipment.fulfillment.order
    order.delivery_status = status
    order.save(update_fields=["delivery_status", "updated_at"])


def ship(store, fulfillment, gateway, tracking_number=None, driver=None, parcel=None):
    """
    Hand a fulfillment to a delivery partner. Local gateways need a driver; courier APIs book the parcel
    first (outside our transaction, so a slow courier never holds database locks) and give back the tracking number.
    """
    if fulfillment.status == "cancelled":
        raise ValidationError({"fulfillment": "This fulfillment is cancelled."})
    if Shipment.objects.filter(fulfillment=fulfillment).exists():
        raise ValidationError({"fulfillment": "This fulfillment already has a shipment."})
    if not gateway.is_active:
        raise ValidationError({"delivery_gateway": "This delivery partner is turned off."})

    provider = providers.get_provider(gateway.provider)
    metadata, booked = {}, {}
    if gateway.kind == LOCAL:
        driver = driver or {}
        if not driver.get("name"):
            raise ValidationError({"driver_name": "Name the driver for a local delivery."})
        metadata["driver"] = {k: driver[k] for k in ("name", "phone") if driver.get(k)}
        if driver.get("eta"):
            metadata["eta"] = driver["eta"]
    elif provider.books_parcels:
        booked = provider.create_shipment(gateway, fulfillment, fulfillment.order, parcel or {})
        tracking_number = booked.get("tracking_number") or tracking_number
    return _create_shipment(store, fulfillment, gateway, provider, tracking_number, metadata, booked)


@transaction.atomic
def _create_shipment(store, fulfillment, gateway, provider, tracking_number, metadata, booked):
    now = timezone.now()
    shipment = Shipment.objects.create(
        store=store,
        fulfillment=fulfillment,
        delivery_gateway=gateway,
        tracking_number=tracking_number,
        provider_shipment_id=booked.get("provider_shipment_id", ""),
        tracking_url=booked.get("tracking_url") or provider.tracking_url(gateway, tracking_number or ""),
        label_url=booked.get("label_url", ""),
        metadata=metadata,
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


def apply_carrier_events(gateway, events):
    """Apply status updates a courier pushed to us. Unknown parcels and repeats are skipped; returns how many were applied."""
    applied = 0
    for e in events:
        shipment = Shipment.objects.filter(delivery_gateway=gateway, tracking_number=e["tracking_number"]).first()
        if shipment is None or shipment.status == DELIVERED:
            continue
        when = e["event_at"]
        if when and TrackingEvent.objects.filter(shipment=shipment, status=e["status"], event_at=when).exists():
            continue
        add_event(shipment, e["status"], e["location"], e["description"], when)
        applied += 1
    return applied


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
        transaction.on_commit(lambda: shipment_delivered.send(sender=Shipment, shipment=shipment))
    elif status != shipment.status:
        shipment.status = status
        shipment.save(update_fields=["status", "updated_at"])

    return event
