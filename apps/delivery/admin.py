from django.contrib import admin
from .models import ( DeliveryGateway, Shipment, ShipmentItem, TrackingEvent, )

# Register your models here.
admin.site.register(DeliveryGateway) 
admin.site.register(Shipment) 
admin.site.register(ShipmentItem) 
admin.site.register(TrackingEvent)