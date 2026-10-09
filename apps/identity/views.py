from django.contrib.auth import SESSION_KEY, login, logout, update_session_auth_hash
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.tokens import (
    REMEMBER_CLAIM, SessionStore, SESSION_CLAIM, end_token_session, issue_tokens, refresh_tokens,
)
from apps.tenancy.services import stores_for_user

from .serializers import ChangePasswordSerializer, LoginSerializer, SignupSerializer


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


def start_session(request, user, remember_me=False):
    """
    Sign the user in to a Django session too (cookie "sessionid"), for
    browser clients and /admin/. Also records last_login.
    """
    login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
    # 0 = ends when the browser closes; None = SESSION_COOKIE_AGE (30 days)
    request.session.set_expiry(None if remember_me else 0)


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
        start_session(request, user)

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
        remember_me = serializer.validated_data["rememberMe"]
        start_session(request, user, remember_me=remember_me)
        tokens = issue_tokens(user, remember_me=remember_me)

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


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True)


class LogoutView(APIView):
    """
    POST /api/v1/auth/logout/  {refresh}  -> 204

    Ends this sign-in: its access and refresh tokens stop working at once,
    and the session cookie (if any) is cleared. Works even with an expired
    access token, so the client can always sign out.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if serializer.validated_data.get("refresh"):
            end_token_session(serializer.validated_data["refresh"])

        logout(request._request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChangePasswordView(APIView):
    """
    POST /api/v1/auth/password/  {currentPassword, newPassword, confirmPassword}
    -> {access, refresh}

    Signs out every other device (their tokens and sessions stop working);
    this one gets fresh tokens and keeps its session.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data["newPassword"])
        user.save(update_fields=["password", "updated_at"])

        # Cookie clients: keep this browser's session signed in.
        if request.session.get(SESSION_KEY) == str(user.pk):
            update_session_auth_hash(request._request, user)

        # JWT clients: replace this device's sign-in with a fresh one, keeping
        # "remember me" if it had it.
        remember_me = False

        if request.auth is not None and hasattr(request.auth, "get"):
            remember_me = bool(request.auth.get(REMEMBER_CLAIM))
            SessionStore(session_key=request.auth.get(SESSION_CLAIM)).delete()

        return Response({
            "message": "Password changed.",
            **issue_tokens(user, remember_me=remember_me),
        })


class MeView(APIView):
    """GET /api/v1/auth/me/  the signed-in user and their stores"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(session_payload(request.user))
