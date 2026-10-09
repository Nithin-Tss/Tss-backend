from django.contrib import admin  # noqa: F401
from .models import GiftCard, GiftCardTransaction

# Registering models
admin.site.register(GiftCard) 
admin.site.register(GiftCardTransaction)
