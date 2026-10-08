from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from .tokens import user_from_token


class BearerTokenAuthentication(BaseAuthentication):
    """Reads `Authorization: Bearer <token>`."""

    keyword = "Bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()

        if not parts or parts[0].decode().lower() != self.keyword.lower():
            return None

        if len(parts) != 2:
            raise exceptions.AuthenticationFailed("Invalid Authorization header.")

        user = user_from_token(parts[1].decode())

        if user is None:
            raise exceptions.AuthenticationFailed("Session expired. Please sign in again.")

        return (user, None)

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when the token is missing or bad.
        return self.keyword
