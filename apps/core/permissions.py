import uuid

from rest_framework import exceptions
from rest_framework.permissions import BasePermission

from apps.tenancy.models import StoreMembership

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

        if membership is None:
            # Same answer whether the store exists or not, so ids can't be probed.
            raise exceptions.PermissionDenied("You don't have access to this store.")

        request.store = membership.store
        request.store_role = membership.role
        return True
