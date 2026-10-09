"""
JWT login tokens (djangorestframework-simplejwt), backed by Django sessions.

After sign-in the client gets two tokens:
- access  (15 min):  sent as "Authorization: Bearer <access>" on every request
- refresh (7 days, 30 with "remember me"): only sent to /auth/refresh/ to get
  a new access token when the old one expires

Every sign-in also gets a row in the django_session table, and both tokens
carry its key ("sid"). That row is what makes tokens revocable:
- sign-out deletes it, so the tokens stop working at once
- it remembers the latest refresh token, so each refresh token works only
  once (a stolen, already-used one is refused)
The row holds no Django login data, so its key is useless as a session cookie.

Both tokens also carry a short fingerprint of the user's password hash
("pwd"), so changing the password signs the user out everywhere.
"""
from importlib import import_module

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils.crypto import constant_time_compare
from rest_framework import exceptions
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

PASSWORD_CLAIM = "pwd"
REMEMBER_CLAIM = "remember"
SESSION_CLAIM = "sid"

# Keys inside the session row
SESSION_USER = "jwt_user_id"
SESSION_REFRESH_JTI = "jwt_refresh_jti"

SessionStore = import_module(settings.SESSION_ENGINE).SessionStore


def password_fingerprint(user):
    # HMAC of the password hash keyed with SECRET_KEY: reveals nothing about
    # the password, but changes whenever the password changes.
    return user.get_session_auth_hash()[:16]


def fingerprint_matches(user, token):
    return constant_time_compare(token.get(PASSWORD_CLAIM, ""), password_fingerprint(user))


def token_session(user, token):
    """The live session row behind `token`, or None if signed out / expired."""
    session_key = token.get(SESSION_CLAIM)

    if not session_key:
        return None

    session = SessionStore(session_key=session_key)

    if session.get(SESSION_USER) != str(user.pk):
        return None

    return session


def issue_tokens(user, remember_me=False, session=None):
    """
    {"access": ..., "refresh": ...} for a user who just signed in (new
    session row), or for an existing `session` when refreshing.
    """
    lifetime = (
        settings.JWT_REMEMBER_ME_REFRESH_LIFETIME if remember_me
        else settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
    )

    if session is None:
        session = SessionStore()
        session[SESSION_USER] = str(user.pk)
        session.save()  # creates the row and its key

    refresh = RefreshToken.for_user(user)
    refresh[PASSWORD_CLAIM] = password_fingerprint(user)
    refresh[SESSION_CLAIM] = session.session_key
    refresh.set_exp(lifetime=lifetime)

    if remember_me:
        refresh[REMEMBER_CLAIM] = True

    # Only this refresh token may be used next; the row lives as long as it.
    session[SESSION_REFRESH_JTI] = refresh["jti"]
    session.set_expiry(int(lifetime.total_seconds()))
    session.save()

    # The access token copies the custom claims of the refresh token.
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def refresh_tokens(raw_refresh):
    """
    Exchange a refresh token for a new access + refresh pair (rotation).
    Raises AuthenticationFailed if it is invalid, expired, an access token,
    already used, signed out, or the user changed their password since.
    """
    expired = exceptions.AuthenticationFailed("Session expired. Please sign in again.")

    try:
        old = RefreshToken(raw_refresh)
    except TokenError:
        raise expired

    user = get_user_model().objects.filter(pk=old.get("user_id")).first()

    if user is None or not user.is_active or not fingerprint_matches(user, old):
        raise expired

    with transaction.atomic():
        # Lock this sign-in's row until the new token is saved, so two
        # requests with the same refresh token can't both succeed.
        list(
            SessionStore.get_model_class().objects.select_for_update()
            .filter(session_key=old.get(SESSION_CLAIM)).values_list("pk", flat=True)
        )
        session = token_session(user, old)

        if session is None or not constant_time_compare(session.get(SESSION_REFRESH_JTI, ""), old["jti"]):
            raise expired

        return issue_tokens(user, remember_me=bool(old.get(REMEMBER_CLAIM)), session=session)


def end_token_session(raw_token):
    """Sign-out: the tokens of this sign-in stop working. Bad tokens are ignored."""
    try:
        token = RefreshToken(raw_token)
    except TokenError:
        return

    session_key = token.get(SESSION_CLAIM)

    if session_key:
        SessionStore(session_key=session_key).delete()
