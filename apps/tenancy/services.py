from django.db import transaction
from django.utils.text import slugify

from .models import Store, StoreMembership

ACTIVE = "active"
OWNER = "owner"
CLOSED = "closed"
REMOVED = "removed"


def stores_for_user(user):
    """Stores the user is an active member of, with their role in each."""
    memberships = (
        StoreMembership.objects.select_related("store")
        .filter(user=user, status=ACTIVE)
        .order_by("created_at")
    )

    return [
        {
            "storeId": str(m.store.store_id),
            "storeName": m.store.store_name,
            "storeSlug": m.store.store_slug,
            "status": m.store.status,
            "role": m.role,
        }
        for m in memberships
    ]


def _unique_slug(name):
    base = slugify(name)[:90] or "store"
    slug = base
    n = 2

    while Store.objects.filter(store_slug=slug).exists():
        slug = f"{base}-{n}"
        n += 1

    return slug


@transaction.atomic
def create_store(owner, store_name, store_slug=None):
    """Create a store and make `owner` its owner, as one unit."""
    store = Store.objects.create(
        store_name=store_name,
        store_slug=store_slug or _unique_slug(store_name),
        status=ACTIVE,
    )

    StoreMembership.objects.create(
        user=owner,
        store=store,
        role=OWNER,
        status=ACTIVE,
    )

    return store


@transaction.atomic
def close_store(store):
    """
    "Delete" a store. Its products, orders and so on reference it with
    PROTECT, so the rows stay; the store is marked closed and every
    membership removed, which hides it from all lists and makes every
    X-Store-Id request for it fail (IsStoreMember needs an active membership).
    """
    store.status = CLOSED
    store.save(update_fields=["status", "updated_at"])
    store.memberships.update(status=REMOVED)
