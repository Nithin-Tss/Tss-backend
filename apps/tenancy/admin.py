from django.contrib import admin
from .models import Store, StoreMembership

# Register your models here.
admin.site.register(Store)
admin.site.register(StoreMembership)