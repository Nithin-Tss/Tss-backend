import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive only: new nullable or defaulted columns on existing tables, two new tables. Nothing is dropped or rewritten."""

    dependencies = [
        ("tenancy", "0011_store_ck_store_tax_rate_range"),
        ("webhooks", "0002_secret_overlap"),
    ]

    operations = [
        # Encrypted secrets (the plaintext columns stay)
        migrations.AddField(
            model_name="webhookendpoint", name="secret_encrypted", field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="webhookendpoint", name="previous_secret_encrypted", field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="webhookendpoint", name="secret_key_id", field=models.CharField(blank=True, default="", max_length=40),
        ),
        # Outbox
        migrations.CreateModel(
            name="WebhookEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=60)),
                ("dedupe_key", models.CharField(max_length=200)),
                ("payload", models.JSONField(blank=True, null=True)),
                ("resource_model", models.CharField(blank=True, default="", max_length=100)),
                ("resource_id", models.CharField(blank=True, default="", max_length=64)),
                ("status", models.CharField(default="pending", max_length=20)),
                ("dispatch_attempts", models.PositiveIntegerField(default=0)),
                ("last_error", models.CharField(blank=True, default="", max_length=500)),
                ("dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "store",
                    models.ForeignKey(
                        db_column="store_id", on_delete=django.db.models.deletion.CASCADE,
                        related_name="webhook_events", to="tenancy.store",
                    ),
                ),
            ],
            options={"db_table": '"webhooks"."webhook_events"', "ordering": ["created_at"]},
        ),
        migrations.AddIndex(
            model_name="webhookevent", index=models.Index(fields=["status", "created_at"], name="webhook_event_due_idx"),
        ),
        migrations.AddConstraint(
            model_name="webhookevent",
            constraint=models.UniqueConstraint(fields=("store", "dedupe_key"), name="webhook_event_dedupe_uniq"),
        ),
        migrations.AddConstraint(
            model_name="webhookevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("dedupe_key", ""), _negated=True), name="webhook_event_dedupe_not_empty",
            ),
        ),
        migrations.AddConstraint(
            model_name="webhookevent",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["pending", "dispatched", "failed"])), name="webhook_event_status_valid",
            ),
        ),
        # Destination snapshot, lease and fencing on deliveries
        migrations.AddField(
            model_name="webhookdelivery", name="destination_url", field=models.URLField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(model_name="webhookdelivery", name="lease_token", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="webhookdelivery", name="locked_until", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name="webhookdelivery",
            name="outbox_event",
            field=models.ForeignKey(
                blank=True, db_column="outbox_event_id", null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="deliveries", to="webhooks.webhookevent",
            ),
        ),
        migrations.AddConstraint(
            model_name="webhookdelivery",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("lease_token__isnull", True), ("locked_until__isnull", True)),
                    models.Q(("lease_token__isnull", False), ("locked_until__isnull", False)),
                    _connector="OR",
                ),
                name="webhook_delivery_lease_pair",
            ),
        ),
        # Incoming receipts
        migrations.CreateModel(
            name="IncomingWebhookReceipt",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("provider", models.CharField(max_length=30)),
                ("external_event_id", models.CharField(max_length=255)),
                ("event_type", models.CharField(blank=True, default="", max_length=100)),
                ("payload", models.JSONField()),
                ("status", models.CharField(default="received", max_length=20)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("last_error", models.CharField(blank=True, default="", max_length=500)),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("locked_until", models.DateTimeField(blank=True, null=True)),
                ("received_at", models.DateTimeField(auto_now_add=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"db_table": '"webhooks"."incoming_webhook_receipts"', "ordering": ["received_at"]},
        ),
        migrations.AddIndex(
            model_name="incomingwebhookreceipt",
            index=models.Index(fields=["status", "next_attempt_at"], name="webhook_receipt_due_idx"),
        ),
        migrations.AddConstraint(
            model_name="incomingwebhookreceipt",
            constraint=models.UniqueConstraint(fields=("provider", "external_event_id"), name="webhook_receipt_event_uniq"),
        ),
        migrations.AddConstraint(
            model_name="incomingwebhookreceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["received", "processed", "failed"])), name="webhook_receipt_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="incomingwebhookreceipt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("lease_token__isnull", True), ("locked_until__isnull", True)),
                    models.Q(("lease_token__isnull", False), ("locked_until__isnull", False)),
                    _connector="OR",
                ),
                name="webhook_receipt_lease_pair",
            ),
        ),
    ]
