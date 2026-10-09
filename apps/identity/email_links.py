"""
Links sent by email: password reset and email verification.

Both links point at the frontend (settings.FRONTEND_URL), which reads the
values from the URL and posts them back to the API.

- Password reset:  /auth/reset-password?uid=<uid>&token=<token>
  Django's PasswordResetTokenGenerator: the token is built from the current
  password hash, so it stops working once the password is changed (single
  use), and after settings.PASSWORD_RESET_TIMEOUT.
- Email verification:  /auth/verify-email?token=<token>
  A signed (not stored) token naming the user and the email address; it
  stops working if the email changes, and after EMAIL_VERIFICATION_TIMEOUT.

No email provider is connected yet: messages go to the backend configured in
settings.MAILERS (the console, for now).
"""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

logger = logging.getLogger(__name__)

VERIFY_EMAIL_SALT = "identity.verify-email"


def _frontend_link(path, **params):
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}?{urlencode(params)}"


def _send(user, subject, write_body):
    # Writing or sending the email must never break the request: sign-up has
    # already saved the account, and a reset request must not reveal (by
    # erroring) that an account exists. Failures are logged instead.
    try:
        send_mail(subject, write_body(), None, [user.email])
    except Exception:
        logger.exception("Could not send %r to user %s", subject, user.pk)


# ------------------------------------------------------------ password reset

def password_reset_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return _frontend_link("/auth/reset-password", uid=uid, token=token)


def user_for_password_reset(uid, token):
    """The user a reset link was made for, or None if it is invalid, used or expired."""
    try:
        pk = force_str(urlsafe_base64_decode(uid))
        user = get_user_model().objects.filter(pk=pk).first()
    except (ValueError, TypeError, OverflowError, ValidationError):  # bad base64 / not a UUID
        return None

    if user is None or not default_token_generator.check_token(user, token):
        return None

    return user


def send_password_reset_email(user):
    def write_body():
        minutes = settings.PASSWORD_RESET_TIMEOUT // 60
        return (
            f"Hi {user.first_name},\n\n"
            f"Someone asked to reset the password for {user.email}. "
            f"To choose a new password, open this link (valid for {minutes} minutes, once):\n\n"
            f"{password_reset_link(user)}\n\n"
            "If it wasn't you, ignore this email: your password stays the same.\n"
        )

    _send(user, "Reset your password", write_body)


# -------------------------------------------------------- email verification

def email_verification_link(user):
    token = signing.dumps({"user": str(user.pk), "email": user.email}, salt=VERIFY_EMAIL_SALT)
    return _frontend_link("/auth/verify-email", token=token)


def user_for_email_verification(token):
    """The user a verification link was made for, or None if invalid, expired or outdated."""
    try:
        data = signing.loads(
            token,
            salt=VERIFY_EMAIL_SALT,
            max_age=settings.EMAIL_VERIFICATION_TIMEOUT,
        )
    except signing.BadSignature:  # also covers SignatureExpired
        return None

    user = get_user_model().objects.filter(pk=data.get("user")).first()

    # A link sent to an old address doesn't verify the new one.
    if user is None or user.email != data.get("email"):
        return None

    return user


def send_verification_email(user):
    def write_body():
        hours = settings.EMAIL_VERIFICATION_TIMEOUT // 3600
        return (
            f"Hi {user.first_name},\n\n"
            f"Please confirm that {user.email} is your email address by opening this link "
            f"(valid for {hours} hours):\n\n"
            f"{email_verification_link(user)}\n"
        )

    _send(user, "Verify your email address", write_body)
