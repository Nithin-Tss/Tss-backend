"""Mounted at /api/v1/ (see config/api_v1.py): /api-keys/, /feature-flags/ and /platform/..."""
from django.urls import path
from rest_framework.routers import SimpleRouter

from . import platform_views as p
from .views import ApiKeyViewSet, StoreFeatureFlagsView

router = SimpleRouter()
router.register("api-keys", ApiKeyViewSet, basename="api-key")

urlpatterns = [
    path("feature-flags/", StoreFeatureFlagsView.as_view(), name="store_feature_flags"),
    path("platform/overview/", p.OverviewView.as_view(), name="platform_overview"),
    path("platform/stores/", p.StoreListView.as_view(), name="platform_stores"),
    path("platform/stores/<uuid:store_id>/", p.StoreDetailView.as_view(), name="platform_store"),
    path("platform/stores/<uuid:store_id>/suspend/", p.StoreSuspendView.as_view(), name="platform_store_suspend"),
    path("platform/stores/<uuid:store_id>/unsuspend/", p.StoreUnsuspendView.as_view(), name="platform_store_unsuspend"),
    path("platform/users/", p.UserListView.as_view(), name="platform_users"),
    path("platform/users/<uuid:user_id>/", p.UserDetailView.as_view(), name="platform_user"),
    path("platform/users/<uuid:user_id>/support-agent/", p.UserSupportAgentView.as_view(), name="platform_user_support_agent"),
    path("platform/users/<uuid:user_id>/platform-admin/", p.UserPlatformAdminView.as_view(), name="platform_user_admin"),
    path("platform/api-keys/", p.ApiKeyListView.as_view(), name="platform_api_keys"),
    path("platform/api-keys/<uuid:key_id>/revoke/", p.ApiKeyRevokeView.as_view(), name="platform_api_key_revoke"),
    path("platform/feature-flags/", p.FeatureFlagListView.as_view(), name="platform_flags"),
    path("platform/feature-flags/<slug:key>/", p.FeatureFlagDetailView.as_view(), name="platform_flag"),
    path("platform/feature-flags/<slug:key>/overrides/", p.FeatureFlagOverridesView.as_view(), name="platform_flag_overrides"),
    path(
        "platform/feature-flags/<slug:key>/overrides/<uuid:store_id>/",
        p.FeatureFlagOverrideDetailView.as_view(), name="platform_flag_override",
    ),
    path("platform/audit/", p.PlatformAuditView.as_view(), name="platform_audit"),
] + router.urls
