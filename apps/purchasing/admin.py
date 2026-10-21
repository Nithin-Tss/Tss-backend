from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import PurchaseOrder, PurchaseOrderItem


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(StoreGuardAdmin):
    pass


@admin.register(PurchaseOrderItem)
class PurchaseOrderItemAdmin(StoreGuardAdmin):
    store_path = "purchase_order__store"
    parent_fields = ("purchase_order",)
    store_links = {"purchase_order": "store", "variant": "product.store"}
