from rest_framework import exceptions
from rest_framework_simplejwt import authentication

from .tokens import fingerprint_matches, token_session


class JWTAuthentication(authentication.JWTAuthentication):
    """
    Reads "Authorization: Bearer <access token>".

    simplejwt already rejects bad signatures, expired tokens and refresh
    tokens used as access tokens; this also rejects tokens issued before
    the user's last password change, or whose sign-in has been signed out.
    """

    def get_user(self, validated_token):
        user = super().get_user(validated_token)

        if not fingerprint_matches(user, validated_token) or token_session(user, validated_token) is None:
            raise exceptions.AuthenticationFailed("Session expired. Please sign in again.")

        return user
