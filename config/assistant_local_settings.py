"""
Settings for running the AI assistant on this computer.

    python manage.py runserver --settings=config.assistant_local_settings

config/settings.py now turns the assistant on from the environment (ASSISTANT_ENABLED=True), which is what
production uses. This module is a shortcut that sets that variable and then loads the normal settings.
"""
import os

os.environ["ASSISTANT_ENABLED"] = "True"

from .settings import *  # noqa: E402,F401,F403
