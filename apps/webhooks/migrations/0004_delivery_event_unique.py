from django.db import migrations, models
from django.db.models import Count


def refuse_if_duplicates(apps, schema_editor):
    """Read-only check. Never deletes: if one event was queued twice for an endpoint, a person decides which row stays."""
    Delivery = apps.get_model("webhooks", "WebhookDelivery")
    count = Delivery.objects.values("endpoint_id", "event_id").annotate(n=Count("id")).filter(n__gt=1).count()
    if count:
        raise RuntimeError(
            f"{count} (endpoint, event_id) pair(s) in webhooks.webhook_deliveries have more than one delivery. "
            "The unique constraint was NOT added and no rows were changed. Review them, then migrate again."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("webhooks", "0003_webhook_hardening"),
    ]

    operations = [
        migrations.RunPython(refuse_if_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="webhookdelivery",
            constraint=models.UniqueConstraint(fields=("endpoint", "event_id"), name="webhook_delivery_event_uniq"),
        ),
    ]
