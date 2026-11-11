"""
Test settings on a real PostgreSQL database (Django creates and drops its own "test_<name>" database), with the real
migrations. Use it for what SQLite cannot do: full-text search, the GIN index, schemas and data migrations.

    python manage.py test apps.helpdesk --settings=config.test_settings_pg

The connection comes from the same DB_* / DATABASE_URL variables as the app (see .env.example).
"""
from .settings import DATABASES  # noqa: F401  (the PostgreSQL connection of the app)
from .test_settings import *  # noqa: F401,F403
from .test_settings import DATABASES as _unused  # noqa: F401

from .settings import DATABASES  # noqa: F811,E402  (test_settings replaced it with SQLite: put PostgreSQL back)

MIGRATION_MODULES = {}
