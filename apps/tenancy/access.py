"""
Roles and team members of a store: what each person may do, and the rules
for changing it.

The no-escalation rule: someone who isn't an owner can only hand out, edit
or take away access they hold themselves.
"""
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core import rbac

from .models import MembershipRole, StoreMembership, StoreRole
from .services import ACTIVE, OWNER, REMOVED

STAFF = "staff"


def role_permissions(role):
    """System roles always take their permissions from code; others from the row."""
    if role.kind == rbac.SYSTEM and role.key in rbac.BUILT_IN_ROLES:
        return set(rbac.BUILT_IN_ROLES[role.key][3])
    return set(rbac.clean_permissions(role.permissions or []))


def membership_permissions(membership):
    """Every permission the member holds in this store (cached on the instance)."""
    cached = getattr(membership, "_permissions", None)
    if cached is not None:
        return cached

    if membership.role == OWNER:
        perms = set(rbac.ALL_PERMISSIONS) | {rbac.STORE_MANAGE}
    else:
        perms = set()
        for assignment in membership.role_assignments.select_related("role"):
            perms |= role_permissions(assignment.role)

    membership._permissions = perms
    return perms


def member_roles(membership):
    if membership.role == OWNER:
        return []
    return [a.role for a in membership.role_assignments.select_related("role")]


# ---------------------------------------------------------------- roles

def ensure_built_in_roles(store):
    """Create the built-in roles for a store the first time they are needed."""
    existing = set(store.roles.exclude(key__isnull=True).values_list("key", flat=True))

    for key, (kind, name, description, permissions) in rbac.BUILT_IN_ROLES.items():
        if key in existing:
            continue
        # A custom role may already use the name; the built-in one gets a suffix
        if store.roles.filter(name=name).exists():
            name = f"{name} (built-in)"
        StoreRole.objects.create(
            store=store, key=key, kind=kind, name=name,
            description=description, permissions=permissions,
        )


def _is_owner(membership):
    return membership.role == OWNER


def _require_covers(caller, permissions):
    if _is_owner(caller):
        return
    if set(permissions) - membership_permissions(caller):
        raise PermissionDenied("You can only give access that you have yourself.")


def _clean_role_input(store, name, permissions, instance=None):
    name = " ".join((name or "").split())
    if not name:
        raise ValidationError({"name": "A role needs a name."})
    if len(name) > 100:
        raise ValidationError({"name": "A role name can be at most 100 characters."})

    taken = store.roles.filter(name__iexact=name)
    if instance is not None:
        taken = taken.exclude(pk=instance.pk)
    if taken.exists():
        raise ValidationError({"name": "A role with this name already exists."})

    if not isinstance(permissions, list):
        raise ValidationError({"permissions": "Permissions must be a list."})
    unknown = [p for p in permissions if p not in rbac.ALL_PERMISSIONS]
    if unknown:
        raise ValidationError({"permissions": f"Unknown permissions: {', '.join(map(str, unknown))}."})

    return name, rbac.clean_permissions(permissions)


@transaction.atomic
def create_role(store, caller, name, description, permissions):
    name, permissions = _clean_role_input(store, name, permissions)
    _require_covers(caller, permissions)
    return StoreRole.objects.create(
        store=store, kind=rbac.CUSTOM, name=name,
        description=(description or "").strip() or None, permissions=permissions,
    )


@transaction.atomic
def update_role(role, caller, name=None, description=None, permissions=None):
    if role.kind == rbac.SYSTEM:
        raise PermissionDenied("System roles can't be changed.")

    new_name, new_perms = _clean_role_input(
        role.store,
        role.name if name is None else name,
        role.permissions if permissions is None else permissions,
        instance=role,
    )
    # What the role grants now and what it would grant must both be within reach
    _require_covers(caller, role_permissions(role) | set(new_perms))

    role.name = new_name
    role.permissions = new_perms
    if description is not None:
        role.description = description.strip() or None
    role.save()
    return role


@transaction.atomic
def delete_role(role, caller):
    if role.kind == rbac.SYSTEM:
        raise PermissionDenied("System roles can't be deleted.")
    _require_covers(caller, role_permissions(role))
    role.delete()


# ---------------------------------------------------------------- members

def _roles_for(store, role_ids):
    try:
        ids = list(dict.fromkeys(uuid.UUID(str(r)) for r in role_ids or []))
    except ValueError:
        raise ValidationError({"roleIds": "Invalid role id."})
    roles = list(store.roles.filter(role_id__in=ids))
    if len(roles) != len(ids):
        raise ValidationError({"roleIds": "One or more roles don't exist in this store."})
    return roles


def _other_owners(store, membership):
    return store.memberships.filter(role=OWNER, status=ACTIVE).exclude(pk=membership.pk).exists()


def _set_roles(membership, roles):
    membership.role_assignments.all().delete()
    MembershipRole.objects.bulk_create(
        [MembershipRole(membership=membership, role=r) for r in roles]
    )
    membership._permissions = None


@transaction.atomic
def add_member(store, caller, email, role_ids, make_owner=False):
    if make_owner and not _is_owner(caller):
        raise PermissionDenied("Only an owner can do this.")

    user = get_user_model().objects.filter(email__iexact=(email or "").strip()).first()
    if user is None:
        raise ValidationError({"email": "No account with this email. Ask them to sign up first."})

    roles = [] if make_owner else _roles_for(store, role_ids)
    for r in roles:
        _require_covers(caller, role_permissions(r))

    membership = StoreMembership.objects.filter(user=user, store=store).first()
    if membership is not None and membership.status == ACTIVE:
        raise ValidationError({"email": "This person already has access to the store."})
    if membership is None:
        membership = StoreMembership(user=user, store=store)

    membership.role = OWNER if make_owner else STAFF
    membership.status = ACTIVE
    membership.save()
    _set_roles(membership, roles)
    return membership


@transaction.atomic
def update_member(membership, caller, role_ids=None, make_owner=None):
    if (_is_owner(membership) or make_owner is not None) and not _is_owner(caller):
        raise PermissionDenied("Only an owner can do this.")

    if make_owner is not None:
        if not make_owner and _is_owner(membership) and not _other_owners(membership.store, membership):
            raise ValidationError("A store needs at least one owner.")
        membership.role = OWNER if make_owner else STAFF
        membership.save(update_fields=["role", "updated_at"])
        if make_owner:
            _set_roles(membership, [])

    if role_ids is not None and not _is_owner(membership):
        new_roles = _roles_for(membership.store, role_ids)
        # Giving a role, or taking one away, needs every permission in it
        for r in new_roles + member_roles(membership):
            _require_covers(caller, role_permissions(r))
        _set_roles(membership, new_roles)

    membership._permissions = None
    return membership


@transaction.atomic
def remove_member(membership, caller):
    if _is_owner(membership):
        if not _is_owner(caller):
            raise PermissionDenied("Only an owner can do this.")
        if not _other_owners(membership.store, membership):
            raise ValidationError("A store needs at least one owner.")
    else:
        for r in member_roles(membership):
            _require_covers(caller, role_permissions(r))

    membership.status = REMOVED
    membership.save(update_fields=["status", "updated_at"])
