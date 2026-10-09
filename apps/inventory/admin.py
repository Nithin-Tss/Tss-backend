from django.contrib import admin
from .models import ( Location, InventoryItem, Transfer, TransferItem, )

# Registering models
admin.site.register(Location) 
admin.site.register(InventoryItem)
admin.site.register(Transfer) 
admin.site.register(TransferItem)