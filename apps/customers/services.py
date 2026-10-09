"""Customers and their addresses."""
from django.db import transaction

from .models import CustomerAddress


@transaction.atomic
def save_address(serializer):
    """Save an address; a customer has at most one default address per type."""
    address = serializer.save()
    if address.is_default:
        CustomerAddress.objects.filter(
            customer=address.customer, address_type=address.address_type
        ).exclude(pk=address.pk).update(is_default=False)
    return address
