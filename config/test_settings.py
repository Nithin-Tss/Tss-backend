"""
Settings for the test suite: an in-memory SQLite database, so tests never
touch the shared PostgreSQL database.

    python manage.py test --settings=config.test_settings
"""
from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "apps.core.testdb",
        "NAME": ":memory:",
    }
}

# Build tables straight from the models; the real migrations contain
# PostgreSQL-only SQL (CREATE SCHEMA).
MIGRATION_MODULES = {app.split(".")[-1]: None for app in INSTALLED_APPS}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The test client speaks plain http; don't redirect it to https.
SECURE_SSL_REDIRECT = False

# Uploads go to a throwaway folder, never the real media directory.
import tempfile  # noqa: E402

MEDIA_ROOT = tempfile.mkdtemp(prefix="tss-test-media-")

# Per-process cache, never the shared Redis from .env: parallel test workers
# call cache.clear() in setUp, which would wipe each other's throttle counters.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
