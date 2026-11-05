from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import HasStorePermission, IsStoreMember

from . import services
from .serializers import NotificationSettingsSerializer


class _Base(APIView):
    permission_classes = [IsAuthenticated, IsStoreMember, HasStorePermission]
    permission_resource = "notifications"
    # The area only has "manage"; reading the recipients list is as restricted as editing it
    action_permissions = {"GET": "notifications.manage"}


class NotificationSettingsView(_Base):
    """GET / PATCH /api/v1/notifications/settings/ - the current store's notification settings."""

    def get(self, request):
        return Response(NotificationSettingsSerializer(services.get_settings(request.store)).data)

    def patch(self, request):
        serializer = NotificationSettingsSerializer(services.get_settings(request.store), data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    put = patch


class NotificationTestView(_Base):
    """POST /api/v1/notifications/settings/test/  {"email": "..."} - sends a sample email to check the sender settings."""

    def post(self, request):
        email = request.data.get("email") or request.user.email or ""
        if not isinstance(email, str):
            raise ValidationError({"email": "Enter an email address to send the test to."})
        email = email.strip()
        if not email or "@" not in email:
            raise ValidationError({"email": "Enter an email address to send the test to."})
        sent = services.send_test(request.store, email)
        return Response({"sent": sent, "to": email}, status=202 if sent else 502)
