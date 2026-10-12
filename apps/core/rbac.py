"""
Role-based access control for the store admin.

A person's access to a store is their membership: "owner" (everything, no role
check) or "staff" (the union of the permissions of the roles assigned to them).
Permissions are plain keys such as "orders.view"; they live in code, so new
ones need no table change, and keys that no longer exist grant nothing.
"""

VIEW = "view"
MANAGE = "manage"
DELETE = "delete"
REFUND = "refund"

# area -> [(action, label)]. A key is "<area>.<action>".
AREAS = {
    "orders": ("Orders", [
        (VIEW, "View orders, payments and abandoned checkouts"),
        (MANAGE, "Create, edit, cancel and record payments for orders"),
        (REFUND, "Refund payments"),
    ]),
    "draft_orders": ("Draft orders", [
        (VIEW, "View draft orders"),
        (MANAGE, "Create, edit and complete draft orders"),
    ]),
    "products": ("Products and catalogs", [
        (VIEW, "View products, collections and catalogs"),
        (MANAGE, "Create and edit products, collections and catalogs"),
        (DELETE, "Delete products, collections and catalogs"),
    ]),
    "gift_cards": ("Gift cards", [
        (VIEW, "View gift cards"),
        (MANAGE, "Issue and adjust gift cards"),
    ]),
    "inventory": ("Inventory", [
        (VIEW, "View inventory, locations and transfers"),
        (MANAGE, "Adjust inventory, manage locations and transfers"),
    ]),
    "purchasing": ("Purchase orders", [
        (VIEW, "View purchase orders"),
        (MANAGE, "Create, send and receive purchase orders"),
    ]),
    "customers": ("Customers", [
        (VIEW, "View customers and wishlists"),
        (MANAGE, "Create and edit customers"),
        (DELETE, "Delete customers"),
    ]),
    "discounts": ("Marketing and discounts", [
        (VIEW, "View discounts and check codes"),
        (MANAGE, "Create, edit and delete discounts"),
    ]),
    "fulfillment": ("Fulfillment", [
        (VIEW, "View fulfillments and shipments"),
        (MANAGE, "Fulfill orders and track shipments"),
    ]),
    "shipping": ("Shipping and delivery", [
        (VIEW, "View delivery partners"),
        (MANAGE, "Manage delivery partners"),
    ]),
    "themes": ("Online store", [
        (VIEW, "View themes and preview them"),
        (MANAGE, "Edit themes, theme code and the customizer"),
    ]),
    "reports": ("Reports and analytics", [
        (VIEW, "View overview, reports and analytics"),
    ]),
    "members": ("Users and roles", [
        (VIEW, "View users and their roles"),
        (MANAGE, "Add and remove users, assign roles"),
        ("roles", "Create, edit and delete roles"),
    ]),
}

# "members.roles" is spelled "roles.manage" in the public key list.
_KEY_OVERRIDES = {"members.roles": "roles.manage"}


def _key(area, action):
    return _KEY_OVERRIDES.get(f"{area}.{action}", f"{area}.{action}")


# key -> {"area": area key, "label": ..., "group": area title}
PERMISSIONS = {
    _key(area, action): {"area": area, "group": title, "label": label}
    for area, (title, actions) in AREAS.items()
    for action, label in actions
}
ALL_PERMISSIONS = frozenset(PERMISSIONS)

# Held by owners only; no role can grant it.
STORE_MANAGE = "store.manage"

SYSTEM = "system"
PREDEFINED = "predefined"
CUSTOM = "custom"


def has_delete(area):
    return _key(area, DELETE) in ALL_PERMISSIONS


def _view_and_manage(*areas):
    return [_key(a, act) for a in areas for act in (VIEW, MANAGE)]


# key -> (kind, name, description, permissions)
BUILT_IN_ROLES = {
    "administrator": (
        SYSTEM,
        "Administrator",
        "High-trust role with every permission, including ones added later.",
        sorted(ALL_PERMISSIONS),
    ),
    "store_user_administrator": (
        SYSTEM,
        "Store user administrator",
        "Manages the team without touching store data.",
        ["members.view", "members.manage", "roles.manage"],
    ),
    "online_store_editor": (
        PREDEFINED,
        "Online store editor",
        "Designs the storefront.",
        ["themes.view", "themes.manage"],
    ),
    "customer_support": (
        PREDEFINED,
        "Customer support",
        "Handles orders and can look up customers and products to take an order.",
        [
            "orders.view", "orders.manage",
            "draft_orders.view", "draft_orders.manage",
            "customers.view", "products.view",
        ],
    ),
    "merchandiser": (
        PREDEFINED,
        "Merchandiser",
        "Looks after the catalog and stock.",
        _view_and_manage("products", "inventory") + ["products.delete"],
    ),
    "marketer": (
        PREDEFINED,
        "Marketer",
        "Runs promotions and works with customers.",
        ["discounts.view", "discounts.manage", "customers.view", "customers.manage"],
    ),
}

SYSTEM_KEYS = frozenset(k for k, v in BUILT_IN_ROLES.items() if v[0] == SYSTEM)


def clean_permissions(keys):
    """Drop unknown keys and duplicates, keeping a stable order."""
    return sorted({k for k in keys if k in ALL_PERMISSIONS})


def permission_for(area, method, action=None, overrides=None):
    """
    The permission key a request needs.

    `overrides` maps an action name (or an upper-case HTTP method) to a key,
    or to None for "any member of the store".
    """
    overrides = overrides or {}
    for name in (action, method):
        if name in overrides:
            return overrides[name]

    if method in ("GET", "HEAD", "OPTIONS"):
        return _key(area, VIEW)
    if method == "DELETE" and has_delete(area):
        return _key(area, DELETE)
    return _key(area, MANAGE)
