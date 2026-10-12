from django.urls import path

from .team_views import (
    MemberDetailView, MemberListView, PermissionCatalogView, RoleDetailView, RoleListView,
)

members_urlpatterns = [
    path("", MemberListView.as_view(), name="member_list"),
    path("<uuid:membership_id>/", MemberDetailView.as_view(), name="member_detail"),
]

roles_urlpatterns = [
    path("", RoleListView.as_view(), name="role_list"),
    path("permissions/", PermissionCatalogView.as_view(), name="role_permissions"),
    path("<uuid:role_id>/", RoleDetailView.as_view(), name="role_detail"),
]
