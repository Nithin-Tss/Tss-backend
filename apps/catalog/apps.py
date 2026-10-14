from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = 'apps.catalog'

    def ready(self):
        # Automated collections follow product changes
        from .services import connect_collection_signals
        connect_collection_signals()
