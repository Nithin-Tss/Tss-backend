from django.contrib import admin  # noqa: F401
from .models import ( Discount, DiscountProduct, DiscountCode, OrderDiscount, )

# Registering models
admin.site.register(Discount)
admin.site.register(DiscountProduct) 
admin.site.register(DiscountCode) 
admin.site.register(OrderDiscount)
