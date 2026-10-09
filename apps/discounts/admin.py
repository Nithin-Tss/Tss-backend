from django.contrib import admin  # noqa: F401
from .models import ( Discount, DiscountProduct, DiscountCode, OrderDiscount, )

admin.site.register(Discount)
admin.site.register(DiscountProduct) 
admin.site.register(DiscountCode) 
admin.site.register(OrderDiscount)
