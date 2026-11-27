"""
Per-account sign-in lockout, on top of the per-IP "auth" throttle: that one
can't stop password guessing spread over many addresses.

After LOGIN_MAX_FAILURES wrong passwords for one email within
LOGIN_LOCKOUT_SECONDS, sign-in for that email is refused for a full
LOGIN_LOCKOUT_SECONDS from the last of those failures, even with the right
password. A successful sign-in resets the count.
Unknown emails are counted too, so the answer never reveals which exist.
"""
import hashlib

from django.conf import settings
from django.core.cache import cache


def _key(email):
    return "login-fail:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def is_locked(email):
    return cache.get(_key(email), 0) >= settings.LOGIN_MAX_FAILURES


def record_failure(email):
    key = _key(email)
    cache.add(key, 0, settings.LOGIN_LOCKOUT_SECONDS)
    try:
        count = cache.incr(key)
    except ValueError:
        count = 1
        cache.set(key, count, settings.LOGIN_LOCKOUT_SECONDS)
    if count >= settings.LOGIN_MAX_FAILURES:
        # The lock lasts the full period from this failure, not from the first one
        cache.set(key, count, settings.LOGIN_LOCKOUT_SECONDS)


def locked_message():
    minutes = max(1, round(settings.LOGIN_LOCKOUT_SECONDS / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return (
        f"Too many failed sign-in attempts. Your account is locked for {minutes} {unit}. "
        "Try again later or reset your password."
    )


def clear(email):
    cache.delete(_key(email))
