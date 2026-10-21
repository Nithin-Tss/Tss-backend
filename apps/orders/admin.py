from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import ( Order, OrderItem, DraftOrder, DraftOrderDetail, )


@admin.register(Order)
class OrderAdmin(StoreGuardAdmin):
    store_links = {"customer": "store", "sales_channel": "store"}


@admin.register(OrderItem)
class OrderItemAdmin(StoreGuardAdmin):
    store_path = "order__store"
    parent_fields = ("order",)
    store_links = {"order": "store", "product": "store", "variant": "product.store"}


@admin.register(DraftOrder)
class DraftOrderAdmin(StoreGuardAdmin):
    store_links = {"customer": "store", "order": "store"}


@admin.register(DraftOrderDetail)
class DraftOrderDetailAdmin(StoreGuardAdmin):
    store_path = "draft_order__store"
    parent_fields = ("draft_order",)
    store_links = {"draft_order": "store", "product": "store", "variant": "product.store"}
