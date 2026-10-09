from django.contrib import admin
from .models import Fulfillment, FulfillmentItem

admin.site.register(Fulfillment) 
admin.site.register(FulfillmentItem)