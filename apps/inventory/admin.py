from django.contrib import admin
from .models import ( Location, InventoryItem, Transfer, TransferItem, InventoryHistory )

# Registering models
admin.site.register(Location) 
admin.site.register(InventoryItem)
admin.site.register(Transfer) 
admin.site.register(TransferItem)
admin.site.register(InventoryHistory)