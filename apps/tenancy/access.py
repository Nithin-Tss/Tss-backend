"""
Roles and team members of a store: what each person may do, and the rules
for changing it.

The no-escalation rule: someone who isn't an owner can only hand out, edit
or take away access they hold themselves.
"""
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core import rbac

from .models import AccessLog, Invitation, MembershipRole, Store, StoreMembership, StoreRole
from .services import ACTIVE, OWNER, REMOVED

logger = logging.getLogger(__name__)

STAFF = "staff"
PENDING = "pending"
ACCEPTED = "accepted"
REVOKED = "revoked"
INVITATION_DAYS = 14


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


def record(store, actor, action, subject="", **detail):
    """Add a line to the store's access log. `actor` is a membership (or None)."""
    AccessLog.objects.create(
        store=store,
        actor=actor.user if actor is not None else None,
        action=action,
        subject=subject,
        detail=detail,
    )


# ---------------------------------------------------------------- roles

@transaction.atomic
def ensure_built_in_roles(store):
    """
    Create the built-in roles once per store. The store row is locked while
    doing it, so two first visits can't both insert; and `roles_seeded` makes
    a role the store later deletes stay deleted.
    """
    if store.roles_seeded:
        return

    locked = Store.objects.select_for_update().get(pk=store.pk)
    if locked.roles_seeded:
        store.roles_seeded = True
        return

    existing = set(locked.roles.exclude(key__isnull=True).values_list("key", flat=True))
    for key, (kind, name, description, permissions) in rbac.BUILT_IN_ROLES.items():
        if key in existing:
            continue
        # A custom role may already use the name; the built-in one gets a suffix
        if locked.roles.filter(name=name).exists():
            name = f"{name} (built-in)"
        StoreRole.objects.create(
            store=locked, key=key, kind=kind, name=name,
            description=description, permissions=permissions,
        )

    Store.objects.filter(pk=store.pk).update(roles_seeded=True)
    store.roles_seeded = True


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
    role = StoreRole.objects.create(
        store=store, kind=rbac.CUSTOM, name=name,
        description=(description or "").strip() or None, permissions=permissions,
    )
    record(store, caller, "role.created", name, permissions=permissions)
    return role


@transaction.atomic
def update_role(role, caller, name=None, description=None, permissions=None):
    if role.kind == rbac.SYSTEM:
        raise PermissionDenied("System roles can't be changed.")

    before = sorted(role_permissions(role))
    new_name, new_perms = _clean_role_input(
        role.store,
        role.name if name is None else name,
        role.permissions if permissions is None else permissions,
        instance=role,
    )
    # What the role grants now and what it would grant must both be within reach
    _require_covers(caller, set(before) | set(new_perms))

    old_name = role.name
    role.name = new_name
    role.permissions = new_perms
    if description is not None:
        role.description = description.strip() or None
    role.save()
    record(
        role.store, caller, "role.updated", new_name, previousName=old_name,
        added=sorted(set(new_perms) - set(before)), removed=sorted(set(before) - set(new_perms)),
    )
    return role


@transaction.atomic
def delete_role(role, caller):
    if role.kind == rbac.SYSTEM:
        raise PermissionDenied("System roles can't be deleted.")
    _require_covers(caller, role_permissions(role))
    record(role.store, caller, "role.deleted", role.name, permissions=sorted(role_permissions(role)))
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


def _lock_owners(store):
    """
    Lock the store's owner rows until the transaction ends, so two requests
    can't each see "another owner exists" and then both remove one.
    """
    return list(store.memberships.select_for_update().filter(role=OWNER, status=ACTIVE))


def _other_owners(store, membership):
    return any(o.pk != membership.pk for o in _lock_owners(store))


def _set_roles(membership, roles):
    membership.role_assignments.all().delete()
    MembershipRole.objects.bulk_create(
        [MembershipRole(membership=membership, role=r) for r in roles]
    )
    membership._permissions = None


def _grant(store, user, role_ids, make_owner):
    """Make `user` an active member with the given access (creating or reviving the membership)."""
    roles = [] if make_owner else _roles_for(store, role_ids)

    membership = StoreMembership.objects.filter(user=user, store=store).first()
    if membership is not None and membership.status == ACTIVE:
        raise ValidationError({"email": "This person already has access to the store."})
    if membership is None:
        membership = StoreMembership(user=user, store=store)

    membership.role = OWNER if make_owner else STAFF
    membership.status = ACTIVE
    membership.save()
    _set_roles(membership, roles)
    return membership, roles


@transaction.atomic
def add_member(store, caller, email, role_ids, make_owner=False):
    """
    Give an existing account access and return the membership. When nobody
    has that email yet, an invitation is saved and emailed instead, and that
    is returned (it is claimed once the address is verified).
    """
    if make_owner and not _is_owner(caller):
        raise PermissionDenied("Only an owner can do this.")

    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValidationError({"email": "Enter a valid email address."})

    ensure_built_in_roles(store)
    roles = [] if make_owner else _roles_for(store, role_ids)
    for r in roles:
        _require_covers(caller, role_permissions(r))

    user = get_user_model().objects.filter(email__iexact=email).first()

    if user is None:
        if Invitation.objects.filter(store=store, email__iexact=email, status=PENDING).exists():
            raise ValidationError({"email": "This person has already been invited."})
        invitation = Invitation.objects.create(
            store=store, email=email.lower(), role_ids=[str(r.role_id) for r in roles],
            is_owner=make_owner, invited_by=caller.user,
        )
        record(store, caller, "member.invited", email, owner=make_owner, roles=[r.name for r in roles])
        _send_invitation(invitation)
        return invitation

    membership, roles = _grant(store, user, role_ids, make_owner)
    record(store, caller, "member.added", user.email, owner=make_owner, roles=[r.name for r in roles])
    return membership


@transaction.atomic
def update_member(membership, caller, role_ids=None, make_owner=None):
    if (_is_owner(membership) or make_owner is not None) and not _is_owner(caller):
        raise PermissionDenied("Only an owner can do this.")

    store = membership.store
    changes = {}

    if make_owner is not None:
        if not make_owner and _is_owner(membership) and not _other_owners(store, membership):
            raise ValidationError("A store needs at least one owner.")
        membership.role = OWNER if make_owner else STAFF
        membership.save(update_fields=["role", "updated_at"])
        if make_owner:
            _set_roles(membership, [])
        changes["owner"] = bool(make_owner)

    if role_ids is not None:
        if _is_owner(membership):
            raise ValidationError({"roleIds": "Owners have every permission, so they don't take roles."})
        new_roles = _roles_for(store, role_ids)
        old_roles = member_roles(membership)
        # Giving a role, or taking one away, needs every permission in it
        for r in new_roles + old_roles:
            _require_covers(caller, role_permissions(r))
        _set_roles(membership, new_roles)
        changes["roles"] = [r.name for r in new_roles]

    membership._permissions = None
    if changes:
        record(store, caller, "member.updated", membership.user.email, **changes)
    return membership


@transaction.atomic
def remove_member(membership, caller):
    store = membership.store
    if _is_owner(membership):
        if not _is_owner(caller):
            raise PermissionDenied("Only an owner can do this.")
        if not _other_owners(store, membership):
            raise ValidationError("A store needs at least one owner.")
    else:
        for r in member_roles(membership):
            _require_covers(caller, role_permissions(r))

    membership.status = REMOVED
    membership.save(update_fields=["status", "updated_at"])
    record(store, caller, "member.removed", membership.user.email)


# ---------------------------------------------------------------- invitations

def pending_invitations(store):
    cutoff = timezone.now() - timedelta(days=INVITATION_DAYS)
    return store.invitations.filter(status=PENDING, created_at__gte=cutoff).order_by("-created_at")


@transaction.atomic
def revoke_invitation(invitation, caller):
    roles = invitation.store.roles.filter(role_id__in=_valid_uuids(invitation.role_ids))
    if invitation.is_owner and not _is_owner(caller):
        raise PermissionDenied("Only an owner can do this.")
    for r in roles:
        _require_covers(caller, role_permissions(r))
    invitation.status = REVOKED
    invitation.save(update_fields=["status"])
    record(invitation.store, caller, "invitation.revoked", invitation.email)


def _valid_uuids(values):
    out = []
    for v in values or []:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError:
            pass
    return out


def accept_invitations(user):
    """
    Turn the pending invitations for a verified email into memberships.
    Called when an address is verified; does nothing for an unverified one.
    """
    if user.email_verified_at is None:
        return []

    cutoff = timezone.now() - timedelta(days=INVITATION_DAYS)
    accepted = []

    for invitation in Invitation.objects.filter(
        email__iexact=user.email, status=PENDING, created_at__gte=cutoff,
    ).select_related("store", "invited_by"):
        with transaction.atomic():
            inviter = None
            if invitation.invited_by_id:
                inviter = StoreMembership.objects.filter(
                    user_id=invitation.invited_by_id, store=invitation.store, status=ACTIVE,
                ).first()

            # An invitation lapses if the person who sent it has lost their own access
            if inviter is None or invitation.store.status != ACTIVE:
                invitation.status = REVOKED
                invitation.save(update_fields=["status"])
                continue

            make_owner = invitation.is_owner and _is_owner(inviter)
            role_ids = [] if make_owner else [
                r.role_id for r in invitation.store.roles.filter(role_id__in=_valid_uuids(invitation.role_ids))
                if _is_owner(inviter) or role_permissions(r) <= membership_permissions(inviter)
            ]

            try:
                membership, roles = _grant(invitation.store, user, role_ids, make_owner)
            except ValidationError:  # already a member
                invitation.status = REVOKED
                invitation.save(update_fields=["status"])
                continue

            invitation.status = ACCEPTED
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["status", "accepted_at"])
            record(invitation.store, membership, "invitation.accepted", user.email, roles=[r.name for r in roles])
            accepted.append(membership)

    return accepted


def _send_invitation(invitation):
    store = invitation.store
    link = f"{settings.FRONTEND_URL.rstrip('/')}/auth/signup"
    try:
        send_mail(
            f"You've been invited to {store.store_name}",
            f"You've been invited to help run {store.store_name}.\n\n"
            f"Create an account with this email address ({invitation.email}) and verify it, "
            f"and you'll get access automatically:\n\n{link}\n\n"
            f"The invitation is valid for {INVITATION_DAYS} days.\n",
            None, [invitation.email],
        )
    except Exception:
        logger.exception("Could not send an invitation to %s", invitation.email)
