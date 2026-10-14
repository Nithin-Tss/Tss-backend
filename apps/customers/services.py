"""Customers and their addresses."""
from django.db import transaction

from .models import CustomerAddress


@transaction.atomic
def save_address(serializer, **kwargs):
    """Save an address; a customer has at most one default address per type."""
    address = serializer.save(**kwargs)

    # If this is the customer's only address for this type, make it default automatically
    count_for_type = CustomerAddress.objects.filter(
        customer=address.customer, address_type=address.address_type
    ).count()

    if count_for_type == 1 and not address.is_default:
        address.is_default = True
        address.save(update_fields=["is_default", "updated_at"])
    elif address.is_default:
        CustomerAddress.objects.filter(
            customer=address.customer, address_type=address.address_type
        ).exclude(pk=address.pk).update(is_default=False)

    return address


@transaction.atomic
def set_default_address(address):
    """Set an address as the default for its customer and address type."""
    address.is_default = True
    address.save(update_fields=["is_default", "updated_at"])
    CustomerAddress.objects.filter(
        customer=address.customer, address_type=address.address_type
    ).exclude(pk=address.pk).update(is_default=False)
    return address


@transaction.atomic
def delete_address(address):
    """Delete an address and promote the next remaining address to default if needed."""
    customer = address.customer
    address_type = address.address_type
    was_default = address.is_default
    address_pk = address.pk

    address.delete()

    if was_default:
        remaining = (
            CustomerAddress.objects.filter(customer=customer, address_type=address_type)
            .order_by("-created_at")
            .first()
        )
        if remaining:
            remaining.is_default = True
            remaining.save(update_fields=["is_default", "updated_at"])

