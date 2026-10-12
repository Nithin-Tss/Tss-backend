from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import rbac
from apps.core.permissions import HasStorePermission, IsStoreMember

from . import access
from .models import AccessLog, Invitation, StoreMembership
from .services import ACTIVE, OWNER


def role_json(role):
    return {
        "id": str(role.role_id),
        "key": role.key,
        "kind": role.kind,
        "name": role.name,
        "description": role.description or "",
        "permissions": sorted(access.role_permissions(role)),
        "memberCount": role.membership_assignments.filter(membership__status=ACTIVE).count(),
    }


def member_json(m):
    user = m.user
    return {
        "membershipId": str(m.membership_id),
        "userId": str(user.pk),
        "email": user.email,
        "firstName": user.first_name,
        "lastName": user.last_name,
        "isOwner": m.role == OWNER,
        "roles": [{"id": str(r.role_id), "name": r.name} for r in access.member_roles(m)],
        "permissions": sorted(access.membership_permissions(m)),
        "createdAt": m.created_at,
    }


def invitation_json(i):
    roles = {str(r.role_id): r.name for r in i.store.roles.filter(role_id__in=access._valid_uuids(i.role_ids))}
    return {
        "invitationId": str(i.id),
        "invited": True,
        "email": i.email,
        "isOwner": i.is_owner,
        "roles": [{"id": rid, "name": roles[rid]} for rid in map(str, i.role_ids) if rid in roles],
        "createdAt": i.created_at,
    }


def log_json(entry):
    return {
        "id": str(entry.id),
        "action": entry.action,
        "subject": entry.subject,
        "detail": entry.detail,
        "actor": entry.actor.email if entry.actor_id else None,
        "createdAt": entry.created_at,
    }


class TeamView(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "members"

    def get_role(self, role_id):
        role = self.request.store.roles.filter(pk=role_id).first()
        if role is None:
            raise NotFound("Role not found.")
        return role

    def get_member(self, membership_id):
        m = (
            StoreMembership.objects.select_related("user", "store")
            .filter(pk=membership_id, store=self.request.store, status=ACTIVE)
            .first()
        )
        if m is None:
            raise NotFound("Member not found.")
        return m


class PermissionCatalogView(TeamView):
    """GET /api/v1/roles/permissions/  every permission, grouped by area (any member)"""

    action_permissions = {"GET": None}

    def get(self, request):
        return Response([
            {
                "area": area,
                "label": title,
                "permissions": [
                    {"key": key, "label": info["label"]}
                    for key, info in rbac.PERMISSIONS.items() if info["area"] == area
                ],
            }
            for area, (title, _) in rbac.AREAS.items()
        ])


class RoleListView(TeamView):
    """
    GET  /api/v1/roles/  the store's roles (any member)
    POST /api/v1/roles/  {name, description, permissions}  (roles.manage)
    """

    action_permissions = {"GET": None, "POST": "roles.manage"}

    def get(self, request):
        access.ensure_built_in_roles(request.store)
        roles = request.store.roles.order_by("kind", "name")
        order = {rbac.SYSTEM: 0, rbac.PREDEFINED: 1, rbac.CUSTOM: 2}
        return Response([role_json(r) for r in sorted(roles, key=lambda r: (order[r.kind], r.name))])

    def post(self, request):
        role = access.create_role(
            request.store, request.membership,
            request.data.get("name"), request.data.get("description"),
            request.data.get("permissions", []),
        )
        return Response(role_json(role), status=status.HTTP_201_CREATED)


class RoleDetailView(TeamView):
    """PATCH / DELETE /api/v1/roles/<id>/  (roles.manage)"""

    action_permissions = {"GET": None, "PATCH": "roles.manage", "DELETE": "roles.manage"}

    def get(self, request, role_id):
        return Response(role_json(self.get_role(role_id)))

    def patch(self, request, role_id):
        role = access.update_role(
            self.get_role(role_id), request.membership,
            name=request.data.get("name"),
            description=request.data.get("description"),
            permissions=request.data.get("permissions"),
        )
        return Response(role_json(role))

    def delete(self, request, role_id):
        access.delete_role(self.get_role(role_id), request.membership)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MemberListView(TeamView):
    """
    GET  /api/v1/members/  the store's people (members.view)
    POST /api/v1/members/  {email, roleIds, isOwner}  (members.manage)
    """

    def get(self, request):
        members = (
            StoreMembership.objects.select_related("user")
            .filter(store=request.store, status=ACTIVE)
            .order_by("created_at")
        )
        return Response([member_json(m) for m in members])

    def post(self, request):
        result = access.add_member(
            request.store, request.membership,
            request.data.get("email"), request.data.get("roleIds", []),
            make_owner=bool(request.data.get("isOwner")),
        )
        if isinstance(result, Invitation):
            return Response(invitation_json(result), status=status.HTTP_201_CREATED)
        return Response(member_json(result), status=status.HTTP_201_CREATED)


class MemberDetailView(TeamView):
    """
    PATCH  /api/v1/members/<id>/  {roleIds, isOwner}
    DELETE /api/v1/members/<id>/  take away their access
    """

    def get(self, request, membership_id):
        return Response(member_json(self.get_member(membership_id)))

    def patch(self, request, membership_id):
        is_owner = request.data.get("isOwner")
        m = access.update_member(
            self.get_member(membership_id), request.membership,
            role_ids=request.data.get("roleIds"),
            make_owner=None if is_owner is None else bool(is_owner),
        )
        return Response(member_json(m))

    def delete(self, request, membership_id):
        access.remove_member(self.get_member(membership_id), request.membership)
        return Response(status=status.HTTP_204_NO_CONTENT)


class InvitationListView(TeamView):
    """GET /api/v1/members/invitations/  invitations still waiting to be claimed (members.view)"""

    def get(self, request):
        return Response([invitation_json(i) for i in access.pending_invitations(request.store)])


class InvitationDetailView(TeamView):
    """DELETE /api/v1/members/invitations/<id>/  withdraw an invitation (members.manage)"""

    def delete(self, request, invitation_id):
        invitation = access.pending_invitations(request.store).filter(pk=invitation_id).first()
        if invitation is None:
            raise NotFound("Invitation not found.")
        access.revoke_invitation(invitation, request.membership)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ActivityView(TeamView):
    """GET /api/v1/members/activity/  recent changes to roles and access (members.manage)"""

    action_permissions = {"GET": "members.manage"}

    def get(self, request):
        entries = AccessLog.objects.filter(store=request.store).select_related("actor")[:100]
        return Response([log_json(e) for e in entries])
