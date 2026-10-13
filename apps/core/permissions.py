import uuid

from rest_framework import exceptions
from rest_framework.permissions import BasePermission

from apps.tenancy.models import StoreMembership
from apps.tenancy.access import membership_permissions
from apps.tenancy.services import OWNER

from .rbac import permission_for

STORE_HEADER = "X-Store-Id"
ACTIVE = "active"


class IsStoreMember(BasePermission):
    """
    Resolves the store from the X-Store-Id header and checks that the
    signed-in user is an active member of it.

    On success the view can use `request.store` and `request.store_role`.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        raw = request.headers.get(STORE_HEADER)

        if not raw:
            raise exceptions.ParseError(f"{STORE_HEADER} header is required.")

        try:
            store_id = uuid.UUID(raw)
        except ValueError:
            raise exceptions.ParseError(f"{STORE_HEADER} is not a valid store id.")

        membership = (
            StoreMembership.objects.select_related("store")
            .filter(user=request.user, store_id=store_id, status=ACTIVE)
            .first()
        )

        if membership is None and request.user.is_owner:
            # Platform admin: owner access to every store that isn't closed
            from apps.tenancy.models import Store
            from apps.tenancy.services import CLOSED, platform_membership

            store = Store.objects.filter(pk=store_id).exclude(status=CLOSED).first()
            if store is not None:
                membership = platform_membership(request.user, store)

        if membership is None:
            # Same answer whether the store exists or not, so ids can't be probed.
            raise exceptions.PermissionDenied("You don't have access to this store.")

        request.store = membership.store
        request.store_role = membership.role
        request.membership = membership
        return True


class HasStorePermission(BasePermission):
    """
    Checks the signed-in member's role permissions for the view's area.

    The view names its area with `permission_resource` ("orders", "products", ...).
    The HTTP method picks <area>.view / .manage / .delete, and
    `action_permissions` overrides that per action or method; a value of None
    means any member of the store. A view with no area is refused rather than
    allowed. Owners skip the check. Run after IsStoreMember.
    """

    message = "Your role in this store doesn't allow this."

    def has_permission(self, request, view):
        membership = getattr(request, "membership", None)
        if membership is None:
            return False

        area = getattr(view, "permission_resource", None)
        # Owners of their own stores can't edit themes (root admin only); a member
        # who was given a role with themes.manage can, like any other assigned access
        if area == "themes" and not request.user.is_owner and membership.role == OWNER:
            needed = permission_for(
                area, request.method, getattr(view, "action", None), getattr(view, "action_permissions", None),
            )
            if needed == "themes.manage":
                self.message = "Only the administrator can edit themes."
                return False

        if membership.role == OWNER:
            return True

        area = getattr(view, "permission_resource", None)
        if not area:
            return False

        needed = permission_for(
            area,
            request.method,
            getattr(view, "action", None),
            getattr(view, "action_permissions", None),
        )

        return needed is None or needed in membership_permissions(membership)
