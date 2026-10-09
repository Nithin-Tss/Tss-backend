from django.contrib import admin
from .models import Theme, ThemeFile, StoreTheme

# Register your models here.
admin.site.register(Theme)
admin.site.register(ThemeFile)
admin.site.register(StoreTheme)