from django.apps import AppConfig


class OrdersConfig(AppConfig):
    name = 'apps.orders'

    def ready(self):
        from . import receivers  # noqa: F401  (order emails now live in apps.notifications)
