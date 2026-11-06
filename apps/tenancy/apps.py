from django.apps import AppConfig


class TenancyConfig(AppConfig):
    name = 'apps.tenancy'

    def ready(self):
        from . import membership_cache  # noqa: F401  (registers the cache-invalidation signals)
