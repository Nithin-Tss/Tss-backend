from django.contrib import admin  # noqa: F401
from .models import Wishlist, WishlistItem

admin.site.register(Wishlist)
admin.site.register(WishlistItem)