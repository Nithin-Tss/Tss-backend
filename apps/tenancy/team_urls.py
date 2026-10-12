from django.urls import path

from .team_views import (
    ActivityView, InvitationDetailView, InvitationListView, MemberDetailView, MemberListView, PermissionCatalogView, RoleDetailView, RoleListView,
)

members_urlpatterns = [
    path("", MemberListView.as_view(), name="member_list"),
    path("invitations/", InvitationListView.as_view(), name="invitation_list"),
    path("invitations/<uuid:invitation_id>/", InvitationDetailView.as_view(), name="invitation_detail"),
    path("activity/", ActivityView.as_view(), name="access_activity"),
    path("<uuid:membership_id>/", MemberDetailView.as_view(), name="member_detail"),
]

roles_urlpatterns = [
    path("", RoleListView.as_view(), name="role_list"),
    path("permissions/", PermissionCatalogView.as_view(), name="role_permissions"),
    path("<uuid:role_id>/", RoleDetailView.as_view(), name="role_detail"),
]
