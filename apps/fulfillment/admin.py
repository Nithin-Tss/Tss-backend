from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Fulfillment, FulfillmentItem


@admin.register(Fulfillment)
class FulfillmentAdmin(StoreGuardAdmin):
    store_path = "order__store"
    parent_fields = ("order",)
    store_links = {"order": "store"}


@admin.register(FulfillmentItem)
class FulfillmentItemAdmin(StoreGuardAdmin):
    store_path = "fulfillment__order__store"
    parent_fields = ("fulfillment",)
    store_links = {"fulfillment": "order.store", "order_item": "order.store"}
