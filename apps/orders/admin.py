from django.contrib import admin
from .models import ( Order, OrderItem, DraftOrder, DraftOrderDetail, )

# Register your models here.
admin.site.register(Order) 
admin.site.register(OrderItem) 
admin.site.register(DraftOrder) 
admin.site.register(DraftOrderDetail)
