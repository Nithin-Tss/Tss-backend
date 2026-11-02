from django.apps import AppConfig

RO_ALIAS = "assistant_ro"
# Run on every new connection of the read-only alias. This repeats what the startup options ask for, so
# the session is read-only even when a connection pooler ignores or rejects those options.
READ_ONLY_SQL = (
    "SET default_transaction_read_only = on",
    "SET statement_timeout = 15000",
)


def make_read_only(sender=None, connection=None, **kwargs):
    """connection_created receiver: only the assistant's own alias is touched."""
    if connection is None or connection.alias != RO_ALIAS:
        return
    with connection.cursor() as cursor:
        for sql in READ_ONLY_SQL:
            cursor.execute(sql)


class AssistantConfig(AppConfig):
    name = "apps.assistant"

    def ready(self):
        from django.db.backends.signals import connection_created

        connection_created.connect(make_read_only, dispatch_uid="assistant_make_read_only")
