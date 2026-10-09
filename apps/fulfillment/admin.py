from django.contrib import admin
from .models import Fulfillment, FulfillmentItem

# Registering models
admin.site.register(Fulfillment) 
admin.site.register(FulfillmentItem)