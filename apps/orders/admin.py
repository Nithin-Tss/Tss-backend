from django.contrib import admin
from .models import ( Order, OrderItem, DraftOrder, DraftOrderDetail, )

# Registering models
admin.site.register(Order) 
admin.site.register(OrderItem) 
admin.site.register(DraftOrder) 
admin.site.register(DraftOrderDetail)
