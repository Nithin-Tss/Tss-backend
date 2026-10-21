from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import ( DeliveryGateway, Shipment, ShipmentItem, TrackingEvent, )


@admin.register(DeliveryGateway)
class DeliveryGatewayAdmin(StoreGuardAdmin):
    pass


@admin.register(Shipment)
class ShipmentAdmin(StoreGuardAdmin):
    store_links = {"fulfillment": "order.store", "delivery_gateway": "store"}


@admin.register(ShipmentItem)
class ShipmentItemAdmin(StoreGuardAdmin):
    store_path = "shipment__store"
    parent_fields = ("shipment",)
    store_links = {"shipment": "store", "order_item": "order.store"}


@admin.register(TrackingEvent)
class TrackingEventAdmin(StoreGuardAdmin):
    store_path = "shipment__store"
    parent_fields = ("shipment",)
    store_links = {"shipment": "store"}
