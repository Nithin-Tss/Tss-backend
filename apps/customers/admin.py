from django.contrib import admin
from .models import Customer, CustomerAddress

# Registering models
admin.site.register(Customer) 
admin.site.register(CustomerAddress)