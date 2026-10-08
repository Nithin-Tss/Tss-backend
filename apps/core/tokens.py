"""
Signed, stateless API tokens.

A token is the user id plus an expiry, signed with SECRET_KEY. It also
carries a hash of the user's password, so changing the password logs out
every existing token. No database table is needed.
"""
import time

from django.contrib.auth import get_user_model
from django.core import signing
from django.utils.crypto import constant_time_compare

SALT = "apps.core.tokens"

DEFAULT_TTL = 60 * 60 * 24 * 7  # 7 days
REMEMBER_ME_TTL = 60 * 60 * 24 * 30  # 30 days


def issue_token(user, remember_me=False):
    ttl = REMEMBER_ME_TTL if remember_me else DEFAULT_TTL

    return signing.dumps(
        {
            "uid": str(user.pk),
            "exp": int(time.time()) + ttl,
            "pwd": user.get_session_auth_hash(),
        },
        salt=SALT,
        compress=True,
    )


def user_from_token(token):
    """Return the token's user, or None if the token is invalid or expired."""
    try:
        payload = signing.loads(token, salt=SALT)
    except signing.BadSignature:
        return None

    if payload.get("exp", 0) < time.time():
        return None

    user = get_user_model().objects.filter(pk=payload.get("uid")).first()

    if user is None or not constant_time_compare(
        payload.get("pwd", ""), user.get_session_auth_hash()
    ):
        return None

    return user
