from django.contrib import admin  # noqa: F401
from .models import Wishlist, WishlistItem


# Registering models
admin.site.register(Wishlist)
admin.site.register(WishlistItem)