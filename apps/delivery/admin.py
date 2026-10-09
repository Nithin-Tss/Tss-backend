from django.contrib import admin
from .models import ( DeliveryGateway, Shipment, ShipmentItem, TrackingEvent, )

# Registering models
admin.site.register(DeliveryGateway) 
admin.site.register(Shipment) 
admin.site.register(ShipmentItem) 
admin.site.register(TrackingEvent)