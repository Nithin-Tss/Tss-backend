from django.contrib import admin
from .models import Customer, CustomerAddress

# Register your models here.
admin.site.register(Customer) 
admin.site.register(CustomerAddress)