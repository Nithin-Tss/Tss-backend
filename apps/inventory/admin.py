from django.contrib import admin
from .models import ( Location, InventoryItem, Transfer, TransferItem, )

# Register your models here.
admin.site.register(Location) 
admin.site.register(InventoryItem)
admin.site.register(Transfer) 
admin.site.register(TransferItem)