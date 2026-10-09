"""Wishlists."""
from rest_framework.exceptions import ValidationError

from .models import WishlistItem


def add_item(serializer):
    """Add a product (or one variant of it) to a wishlist, once."""
    data = serializer.validated_data
    if WishlistItem.objects.filter(
        wishlist=data["wishlist"], product=data["product"], variant=data.get("variant")
    ).exists():
        raise ValidationError("This is already on the wishlist.")
    return serializer.save()
