from django.apps import AppConfig


class WebhooksConfig(AppConfig):
    name = "apps.webhooks"

    def ready(self):
        from . import receivers  # noqa: F401  (connects the order and payment listeners)
