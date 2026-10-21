from django.db import transaction
from django.utils.text import slugify

from .models import Store, StoreMembership

ACTIVE = "active"
OWNER = "owner"
CLOSED = "closed"
REMOVED = "removed"
# Memberships that were active when the store was closed: reopening brings them back
STORE_CLOSED = "store_closed"


def platform_membership(user, store):
    """
    An unsaved owner membership for a platform admin (User.is_owner) in a store
    they don't belong to, so they can open and manage any store.
    """
    return StoreMembership(user=user, store=store, role=OWNER, status=ACTIVE)


def stores_for_user(user):
    """Stores the user is an active member of, with their role and access in each.
    A platform admin gets every store that isn't closed, as owner."""
    from .access import member_roles, membership_permissions

    memberships = list(
        StoreMembership.objects.select_related("store")
        .filter(user=user, status=ACTIVE)
        .order_by("created_at")
    )

    if user.is_owner:
        held = {m.store_id for m in memberships}
        memberships += [
            platform_membership(user, store)
            for store in Store.objects.exclude(status=CLOSED).exclude(pk__in=held).order_by("created_at")
        ]

    return [
        {
            "storeId": str(m.store.store_id),
            "storeName": m.store.store_name,
            "storeSlug": m.store.store_slug,
            "status": m.store.status,
            "role": m.role,
            "roles": [r.name for r in member_roles(m)],
            "permissions": sorted(membership_permissions(m)),
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

    from .access import ensure_built_in_roles
    ensure_built_in_roles(store)

    return store


@transaction.atomic
def close_store(store):
    """
    "Delete" a store. Its products, orders and so on reference it with
    PROTECT, so the rows stay; the store is marked closed and every active
    membership set aside (reopen_store restores them), which hides it from all lists and makes every
    X-Store-Id request for it fail (IsStoreMember needs an active membership).
    """
    store.status = CLOSED
    store.save(update_fields=["status", "updated_at"])
    store.memberships.filter(status=ACTIVE).update(status=STORE_CLOSED)


@transaction.atomic
def reopen_store(store):
    """Undo close_store: the store is active again and its members get their access back."""
    store.status = ACTIVE
    store.save(update_fields=["status", "updated_at"])
    store.memberships.filter(status=STORE_CLOSED).update(status=ACTIVE)


def _purge(obj, seen):
    """Delete obj, first deleting whatever still protects it (PROTECT/RESTRICT), depth first."""
    from django.db.models import ProtectedError, RestrictedError

    while True:
        try:
            obj.delete()
            return
        except (ProtectedError, RestrictedError) as error:
            blockers = error.protected_objects if isinstance(error, ProtectedError) else error.restricted_objects
            fresh = [b for b in blockers if (b._meta.label, b.pk) not in seen]
            if not fresh:
                raise
        # Delete the blockers outside the except block: an error from them must not carry this
        # handled error along, whose objects are deleted (pk None) by then and can't even be printed.
        for blocker in fresh:
            seen.add((blocker._meta.label, blocker.pk))
            _purge(blocker, seen)


@transaction.atomic
def purge_store(store):
    """
    Permanently delete a store together with everything that belongs to it (members, products, orders,
    themes, ...). Used by Django admin, where deleting a store must not leave its data behind. One
    transaction: if anything fails, nothing is deleted.
    """
    from apps.themes.services import remove_store_themes

    remove_store_themes(store)
    _purge(store, {(store._meta.label, store.pk)})


@transaction.atomic
def delete_store(store):
    """
    Permanently delete a store (close_store is the reversible "delete"). Its theme assignments, and its
    own themes nobody else uses, are removed first (themes.services.remove_store_themes). Everything
    else that references the store with PROTECT (members, products, orders, ...) still protects it:
    then nothing at all is deleted, and the error names what is in the way.
    """
    from django.db.models import ProtectedError
    from rest_framework.exceptions import ValidationError

    from apps.themes.services import remove_store_themes

    try:
        remove_store_themes(store)
        store.delete()
    except ProtectedError as error:
        blockers = sorted({str(obj._meta.verbose_name_plural) for obj in error.protected_objects})
        raise ValidationError(
            {"detail": f"This store can't be deleted permanently while it still has: {', '.join(blockers)}."}
        )
