from django.apps import AppConfig


class HelpdeskConfig(AppConfig):
    name = "apps.helpdesk"

    def ready(self):
        from . import signals  # noqa: F401
