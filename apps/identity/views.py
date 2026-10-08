from django.contrib.auth.models import update_last_login
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.tokens import issue_token
from apps.tenancy.services import stores_for_user

from .serializers import LoginSerializer, SignupSerializer


def session_payload(user, token=None):
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

    if token:
        payload["token"] = token

    return payload


class SignupView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

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
                **session_payload(user, issue_token(user)),
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

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
        token = issue_token(
            user,
            remember_me=serializer.validated_data["rememberMe"],
        )

        return Response(
            {
                "message": "Signed in.",
                **session_payload(user, token),
            },
            status=status.HTTP_200_OK,
        )


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(session_payload(request.user))
