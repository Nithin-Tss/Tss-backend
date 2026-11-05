from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"

    def ready(self):
        from . import receivers  # noqa: F401  (connects the order, payment and delivery listeners)
