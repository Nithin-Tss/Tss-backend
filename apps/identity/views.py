from django.contrib.auth.models import update_last_login
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.tokens import issue_tokens, refresh_tokens
from apps.tenancy.services import stores_for_user

from .serializers import LoginSerializer, SignupSerializer


def session_payload(user, tokens=None):
    payload = {
        "user": {
            "id": str(user.pk),
            "email": user.email,
            "firstName": user.first_name,
            "lastName": user.last_name,
            "mobileNumber": user.phone,
        },
        "stores": stores_for_user(user),
    }

    if tokens:
        payload.update(tokens)  # "access" and "refresh"

    return payload


class PublicAuthView(APIView):
    """Sign-up, sign-in and refresh: no login needed, but rate-limited."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def get_authenticate_header(self, request):
        # A rejected refresh token answers 401 (sign in again), not 403.
        return 'Bearer realm="api"'


class SignupView(PublicAuthView):
    """POST /api/v1/auth/signup/  -> {user, stores, access, refresh}"""

    def post(self, request):
        serializer = SignupSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = serializer.save()
        update_last_login(None, user)

        return Response(
            {
                "message": "Account created.",
                **session_payload(user, issue_tokens(user)),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(PublicAuthView):
    """POST /api/v1/auth/login/  {email, password, rememberMe} -> {user, stores, access, refresh}"""

    def post(self, request):
        serializer = LoginSerializer(
            data=request.data,
            context={"request": request},
        )

        if not serializer.is_valid():
            return Response(
                serializer.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = serializer.validated_data["user"]
        update_last_login(None, user)
        tokens = issue_tokens(
            user,
            remember_me=serializer.validated_data["rememberMe"],
        )

        return Response(
            {
                "message": "Signed in.",
                **session_payload(user, tokens),
            },
            status=status.HTTP_200_OK,
        )


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class RefreshView(PublicAuthView):
    """POST /api/v1/auth/refresh/  {refresh} -> {access, refresh}"""

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(refresh_tokens(serializer.validated_data["refresh"]))


class MeView(APIView):
    """GET /api/v1/auth/me/  the signed-in user and their stores"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(session_payload(request.user))
