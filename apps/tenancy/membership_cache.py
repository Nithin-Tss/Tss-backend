"""
Short-lived cache of "this user's active membership of this store" (with the store and the
permissions it grants). Every staff API request needs it, and without the cache it costs two
database queries each time. Anything that changes a membership, its roles or the store bumps a
version, so the next request reads fresh data; the TTL covers bulk updates that skip signals.
"""
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save

from .models import MembershipRole, Store, StoreMembership, StoreRole

TTL = 30
_VERSION_KEY = "membership-cache:version"


def _version():
    return cache.get(_VERSION_KEY, 0)


def _key(user_id, store_id):
    return f"membership:{_version()}:{user_id}:{store_id}"


def get(user_id, store_id):
    return cache.get(_key(user_id, store_id))


def put(user_id, store_id, membership):
    cache.set(_key(user_id, store_id), membership, TTL)


def invalidate(**kwargs):
    try:
        cache.incr(_VERSION_KEY)
    except ValueError:
        cache.set(_VERSION_KEY, _version() + 1, None)


for _model in (StoreMembership, MembershipRole, StoreRole, Store):
    post_save.connect(invalidate, sender=_model, weak=False, dispatch_uid=f"membership-cache-save-{_model.__name__}")
    post_delete.connect(invalidate, sender=_model, weak=False, dispatch_uid=f"membership-cache-del-{_model.__name__}")
