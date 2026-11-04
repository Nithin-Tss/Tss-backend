"""
Per-account sign-in lockout, on top of the per-IP "auth" throttle: that one
can't stop password guessing spread over many addresses.

After LOGIN_MAX_FAILURES wrong passwords for one email within
LOGIN_LOCKOUT_SECONDS, sign-in for that email answers 429 until the window
ends, even with the right password. A successful sign-in resets the count.
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
        cache.incr(key)
    except ValueError:
        cache.set(key, 1, settings.LOGIN_LOCKOUT_SECONDS)


def clear(email):
    cache.delete(_key(email))
