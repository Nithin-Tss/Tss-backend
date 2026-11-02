"""
Settings for running the AI assistant on this computer only.

    python manage.py runserver --settings=config.assistant_local_settings

This is the ONLY thing that turns the assistant on. It:
  - sets ASSISTANT_REAL_MODE = True (settings.py keeps the literal False, so production never has it),
  - adds a second database alias, "assistant_ro": the same database and credentials as "default", but
    every session is read-only and statements stop after 15 seconds,
  - installs apps.assistant (its connection_created hook repeats the read-only settings after connecting),
  - adds a router that never migrates that alias.

Nothing here is read by production: deploy with config.settings as before.
"""
import copy
import os

from .settings import *  # noqa: F401,F403

ASSISTANT_REAL_MODE = True

# Set ASSISTANT_RO_STARTUP_OPTIONS=0 if a connection pooler refuses "options" at connect time;
# the hook in apps/assistant/apps.py still makes the session read-only.
ASSISTANT_RO_STARTUP_OPTIONS = os.getenv("ASSISTANT_RO_STARTUP_OPTIONS", "1") != "0"

INSTALLED_APPS = [*INSTALLED_APPS, "apps.assistant"]  # noqa: F405

_ro = copy.deepcopy(DATABASES["default"])  # noqa: F405
_ro["CONN_MAX_AGE"] = 0  # a fresh session per request, so the read-only settings always apply
_ro["OPTIONS"] = dict(_ro.get("OPTIONS", {}))
if ASSISTANT_RO_STARTUP_OPTIONS:
    _ro["OPTIONS"]["options"] = "-c default_transaction_read_only=on -c statement_timeout=15000"
_ro["TEST"] = {"MIRROR": "default"}

DATABASES = {**DATABASES, "assistant_ro": _ro}  # noqa: F405
DATABASE_ROUTERS = [*globals().get("DATABASE_ROUTERS", []), "apps.assistant.router.AssistantRouter"]
