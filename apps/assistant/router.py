from .apps import RO_ALIAS


class AssistantRouter:
    """Never migrates the read-only alias (it is the same database as "default"). Routes nothing else."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == RO_ALIAS:
            return False
        return None
