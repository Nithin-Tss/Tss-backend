from django.contrib import admin
from .models import Store, StoreMembership


# Registering models
admin.site.register(Store)
admin.site.register(StoreMembership)