import uuid

from rest_framework import exceptions
from rest_framework.permissions import BasePermission

from apps.tenancy import membership_cache
from apps.tenancy.models import StoreMembership
from apps.tenancy.access import membership_permissions
from apps.tenancy.services import OWNER, SUSPENDED

from apps.audit import services as audit

from .rbac import permission_for

STORE_HEADER = "X-Store-Id"
ACTIVE = "active"
# The role name of a request made with an API key (it holds the key's scopes, nothing else)
API_KEY_ROLE = "api_key"


def _api_key(request):
    """The ApiKey this request was authenticated with, or None."""
    from apps.platform_admin.models import ApiKey

    return request.auth if isinstance(request.auth, ApiKey) else None


class IsStoreMember(BasePermission):
    """
    Resolves the store from the X-Store-Id header and checks that the
    signed-in user is an active member of it.

    On success the view can use `request.store` and `request.store_role`.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False

        api_key = _api_key(request)
        if api_key is not None:
            membership = self._api_key_membership(request, api_key)
        else:
            membership = self._user_membership(request)

        if membership.store.status == SUSPENDED and (api_key is not None or not request.user.is_owner):
            raise exceptions.PermissionDenied("This store is suspended. Contact support.")

        request.store = membership.store
        request.store_role = membership.role
        request.membership = membership
        audit.begin(request)
        return True

    def _user_membership(self, request):
        raw = request.headers.get(STORE_HEADER)

        if not raw:
            raise exceptions.ParseError(f"{STORE_HEADER} header is required.")

        try:
            store_id = uuid.UUID(raw)
        except ValueError:
            raise exceptions.ParseError(f"{STORE_HEADER} is not a valid store id.")

        membership = membership_cache.get(request.user.pk, store_id)
        if membership is None:
            membership = (
                StoreMembership.objects.select_related("store")
                .filter(user=request.user, store_id=store_id, status=ACTIVE)
                .first()
            )
            if membership is not None:
                membership_permissions(membership)  # computed once, cached with it
                membership_cache.put(request.user.pk, store_id, membership)

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

        return membership

    def _api_key_membership(self, request, key):
        """A request made with an API key acts as a staff member of the key's store holding just its scopes."""
        from apps.platform_admin.scopes import grantable

        raw = request.headers.get(STORE_HEADER)
        if raw and raw.lower() != str(key.store_id):
            raise exceptions.PermissionDenied("This API key belongs to a different store.")

        membership = StoreMembership(user=request.user, store=key.store, role=API_KEY_ROLE, status=ACTIVE)
        membership._permissions = frozenset(grantable(key.scopes))
        return membership


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

        # A store's owner (or a platform admin, who gets an owner membership) has full
        # access to that store; everyone else only what their roles grant.
        area = getattr(view, "permission_resource", None)
        audit.bind(request, membership, area, view)

        if membership.role == OWNER:
            return True

        if not area:
            return False

        needed = permission_for(
            area,
            request.method,
            getattr(view, "action", None),
            getattr(view, "action_permissions", None),
        )

        allowed = needed is None or needed in membership_permissions(membership)
        if not allowed:
            audit.deny(request, needed)
        return allowed
