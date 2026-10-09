from django.contrib import admin
from .models import Payment, PaymentTransaction

# Registering models

admin.site.register(Payment)
admin.site.register(PaymentTransaction)