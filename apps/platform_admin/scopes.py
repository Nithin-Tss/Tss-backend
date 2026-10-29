"""
What an API key may be given.

A scope is an ordinary role permission ("products.view", "orders.manage"), so a
key is checked by the same rules as a team member. Keys never get the
permissions that change who can get in or how the store is billed and paid:
those stay with people.
"""
from apps.core import rbac

# Permission areas that no API key can hold
EXCLUDED_AREAS = frozenset({
    "members", "api_keys", "audit", "billing", "domains", "payments", "settings", "checkout",
})

SCOPES = frozenset(
    key for key, info in rbac.PERMISSIONS.items() if info["area"] not in EXCLUDED_AREAS
)


def grantable(keys):
    """`keys` cleaned for use as a key's scopes: unknown or excluded ones dropped, implied "view"s added."""
    return sorted(k for k in rbac.with_implied(k for k in keys if k in SCOPES) if k in SCOPES)


def catalog():
    """Every scope, grouped by area, for the "create key" screen."""
    return [
        {"key": key, "group": rbac.PERMISSIONS[key]["group"], "label": rbac.PERMISSIONS[key]["label"]}
        for key in sorted(SCOPES, key=lambda k: (rbac.PERMISSIONS[k]["group"], k))
    ]
