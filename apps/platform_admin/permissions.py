from rest_framework.permissions import BasePermission

from . import flags
from .models import ApiKey


class IsPlatformAdmin(BasePermission):
    """Platform administrators only (User.is_owner), signed in as themselves."""

    message = "Only a platform administrator can do this."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_owner and not isinstance(request.auth, ApiKey))


def feature_required(key):
    """
    A permission that refuses the request while feature flag `key` is off for the
    request's store. List it after IsStoreMember, which resolves the store.
    """

    class FeatureEnabled(BasePermission):
        message = "This feature isn't available for your store."

        def has_permission(self, request, view):
            return flags.is_enabled(key, getattr(request, "store", None))

    FeatureEnabled.__name__ = f"FeatureEnabled_{key}"
    return FeatureEnabled
