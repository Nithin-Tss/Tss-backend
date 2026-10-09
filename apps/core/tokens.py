"""
JWT login tokens (djangorestframework-simplejwt).

After sign-in the client gets two tokens:
- access  (15 min):  sent as "Authorization: Bearer <access>" on every request
- refresh (7 days, 30 with "remember me"): only sent to /auth/refresh/ to get
  a new access token when the old one expires

Both carry a short fingerprint of the user's password hash ("pwd"), so
changing the password signs the user out everywhere.
"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils.crypto import constant_time_compare
from rest_framework import exceptions
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

PASSWORD_CLAIM = "pwd"
REMEMBER_CLAIM = "remember"


def password_fingerprint(user):
    # HMAC of the password hash keyed with SECRET_KEY: reveals nothing about
    # the password, but changes whenever the password changes.
    return user.get_session_auth_hash()[:16]


def fingerprint_matches(user, token):
    return constant_time_compare(token.get(PASSWORD_CLAIM, ""), password_fingerprint(user))


def issue_tokens(user, remember_me=False):
    """{"access": ..., "refresh": ...} for a user who just signed in."""
    refresh = RefreshToken.for_user(user)
    refresh[PASSWORD_CLAIM] = password_fingerprint(user)

    if remember_me:
        refresh[REMEMBER_CLAIM] = True
        refresh.set_exp(lifetime=settings.JWT_REMEMBER_ME_REFRESH_LIFETIME)

    # The access token copies the custom claims of the refresh token.
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def refresh_tokens(raw_refresh):
    """
    Exchange a refresh token for a new access + refresh pair (rotation).
    Raises AuthenticationFailed if it is invalid, expired, an access token,
    or the user changed their password since.
    """
    try:
        old = RefreshToken(raw_refresh)
    except TokenError:
        raise exceptions.AuthenticationFailed("Session expired. Please sign in again.")

    user = get_user_model().objects.filter(pk=old.get("user_id")).first()

    if user is None or not fingerprint_matches(user, old):
        raise exceptions.AuthenticationFailed("Session expired. Please sign in again.")

    return issue_tokens(user, remember_me=bool(old.get(REMEMBER_CLAIM)))
