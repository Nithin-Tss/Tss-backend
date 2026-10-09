from django.contrib import admin
from .models import PurchaseOrder, PurchaseOrderItem


# Registering models
admin.site.register(PurchaseOrder)
admin.site.register(PurchaseOrderItem)