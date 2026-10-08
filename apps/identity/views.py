from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from .serializers import SignupSerializer, LoginSerializer


class SignupView(APIView):

    def post(self, request):
        serializer = SignupSerializer(data=request.data)

        if serializer.is_valid():
            return Response(
                {
                    "message": "Signup validation successful.",
                    "data": {
                        "firstName": serializer.validated_data["firstName"],
                        "lastName": serializer.validated_data["lastName"],
                        "email": serializer.validated_data["email"],
                        "mobileNumber": serializer.validated_data["mobileNumber"],
                        "agreeTerms": serializer.validated_data["agreeTerms"],
                    },
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST,
        )


class LoginView(APIView):

    def post(self, request):
        serializer = LoginSerializer(data=request.data)

        if serializer.is_valid():
            return Response(
                {
                    "message": "Login validation successful.",
                    "data": {
                        "email": serializer.validated_data["email"],
                        "rememberMe": serializer.validated_data["rememberMe"],
                    },
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST,
        )