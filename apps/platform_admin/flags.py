"""
Feature flags.

    is_enabled("api_keys", store)       one answer
    flags_for_store(store)              every flag at once, as {key: bool}

A store's override wins; otherwise the flag must be switched on and the store
must fall inside the rollout percentage. The share is worked out from the flag
key and store id, so a store keeps its answer while the percentage only grows.
Flags without a row use the default below, so shipping code that asks about a
new flag never needs a data migration.
"""
import hashlib

from .models import FeatureFlag, FeatureFlagOverride

# key -> (name, description, on by default)
REGISTRY = {
    "api_keys": ("API keys", "Let the store create API keys for its own integrations.", True),
    "ai_page_builder": (
        "AI page builder",
        "Let the store's staff ask the AI assistant to build draft pages (About, FAQ, Contact...).",
        False,
    ),
}


def bucket(key, store_id):
    """A stable number 0-99 for this flag and store."""
    digest = hashlib.sha256(f"{key}:{store_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def _answer(key, store_id, flag, overrides):
    if key in overrides:
        return overrides[key]
    if flag is None:
        return REGISTRY[key][2] if key in REGISTRY else False
    if not flag.enabled:
        return False
    if flag.rollout_percent >= 100:
        return True
    return store_id is not None and bucket(key, store_id) < flag.rollout_percent


def _overrides(store_id):
    if store_id is None:
        return {}
    rows = FeatureFlagOverride.objects.filter(store_id=store_id).select_related("flag")
    return {row.flag.key: row.enabled for row in rows}


def is_enabled(key, store=None):
    store_id = getattr(store, "pk", store)
    flag = FeatureFlag.objects.filter(key=key).first()
    return _answer(key, store_id, flag, _overrides(store_id))


def flags_for_store(store=None):
    store_id = getattr(store, "pk", store)
    flags = {f.key: f for f in FeatureFlag.objects.all()}
    overrides = _overrides(store_id)
    return {key: _answer(key, store_id, flags.get(key), overrides) for key in sorted({*REGISTRY, *flags})}
