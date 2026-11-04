"""
Two-factor sign-in (MFA) with authenticator-app codes (TOTP, RFC 6238) and
one-time recovery codes. Standard library only: no extra dependency.

Turning it on is two steps, so a mistyped scan can't lock the owner out:
`begin_setup` stores a new, unconfirmed secret; `confirm_setup` checks the
first code from the app, switches MFA on and returns the recovery codes
(shown once, stored hashed).
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from django.utils import timezone

from .models import MFADevice

DIGITS = 6
PERIOD = 30
WINDOW = 1  # accept one step either side, for clock drift
RECOVERY_CODE_COUNT = 10
ISSUER = "TSS"


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret, counter):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def totp_now(secret, at=None):
    return _hotp(secret, int((time.time() if at is None else at) // PERIOD))


def provisioning_uri(email, secret):
    label = quote(f"{ISSUER}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={PERIOD}"


def _hash_recovery(code):
    return hashlib.sha256(code.replace("-", "").strip().lower().encode()).hexdigest()


def _new_recovery_codes():
    codes = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(RECOVERY_CODE_COUNT)]
    return codes, [_hash_recovery(code) for code in codes]


def get_device(user):
    return MFADevice.objects.filter(user=user).first()


def is_enabled(user):
    device = get_device(user)
    return device is not None and device.confirmed_at is not None


def _check_totp(device, code):
    """True for a valid code. Each time step is accepted once, so a sniffed code can't be replayed."""
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == DIGITS):
        return False

    step = int(time.time() // PERIOD)
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if hmac.compare_digest(_hotp(device.secret, candidate), code):
            if candidate <= device.last_used_step:
                return False
            device.last_used_step = candidate
            device.save(update_fields=["last_used_step"])
            return True
    return False


def _check_recovery(device, code):
    digest = _hash_recovery(code or "")
    for stored in device.recovery_codes:
        if hmac.compare_digest(stored, digest):
            device.recovery_codes = [c for c in device.recovery_codes if c != stored]
            device.save(update_fields=["recovery_codes"])
            return True
    return False


def verify(user, code):
    """Check an authenticator code or an unused recovery code for a user with MFA on."""
    device = get_device(user)
    if device is None or device.confirmed_at is None:
        return False
    return _check_totp(device, code) or _check_recovery(device, code)


def begin_setup(user):
    """Store a fresh unconfirmed secret (replacing any earlier unconfirmed one)."""
    device = get_device(user)
    if device is not None and device.confirmed_at is not None:
        raise ValueError("Two-factor sign-in is already on.")

    secret = new_secret()
    MFADevice.objects.update_or_create(user=user, defaults={"secret": secret, "last_used_step": 0, "recovery_codes": []})
    return secret, provisioning_uri(user.email, secret)


def confirm_setup(user, code):
    """Switch MFA on when the first code is right. Returns the recovery codes, or None for a wrong code."""
    device = get_device(user)
    if device is None or device.confirmed_at is not None or not _check_totp(device, code):
        return None

    codes, hashed = _new_recovery_codes()
    device.recovery_codes = hashed
    device.confirmed_at = timezone.now()
    device.save(update_fields=["recovery_codes", "confirmed_at"])
    return codes


def disable(user):
    MFADevice.objects.filter(user=user).delete()
