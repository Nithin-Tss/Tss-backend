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
