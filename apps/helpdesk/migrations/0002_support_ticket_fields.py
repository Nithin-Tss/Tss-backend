import uuid

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models

import apps.helpdesk.models


class Migration(migrations.Migration):
    dependencies = [
        ("helpdesk", "0001_initial"),
        ("ai_assistant", "0002_conversation_assistant_type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketCounter",
            fields=[
                ("id", models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
                ("last_number", models.PositiveIntegerField(default=0)),
            ],
            options={"db_table": '"helpdesk"."ticket_counter"'},
        ),
        # The old status constraint goes first: it only allows open/pending/closed
        migrations.RemoveConstraint(model_name="ticket", name="ck_ticket_status"),
        migrations.AlterField(
            model_name="ticket", name="status",
            field=models.CharField(
                choices=[
                    ("open", "Open"), ("in_progress", "In progress"), ("waiting_on_customer", "Waiting on customer"),
                    ("resolved", "Resolved"), ("closed", "Closed"),
                ],
                default="open", max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="ticket", name="ticket_number",
            field=models.CharField(editable=False, max_length=20, null=True),
        ),
        migrations.AddField(model_name="ticket", name="summary", field=models.TextField(blank=True, default="")),
        migrations.AddField(
            model_name="ticket", name="conversation",
            field=models.ForeignKey(
                blank=True, db_column="conversation_id", null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="tickets", to="ai_assistant.conversation",
            ),
        ),
        migrations.AddField(
            model_name="ticket", name="source",
            field=models.CharField(
                choices=[
                    ("ai_escalation", "AI escalation"), ("customer_request", "Customer request"),
                    ("help_center", "Help Center form"),
                ],
                default="help_center", max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="ticket", name="priority",
            field=models.CharField(
                choices=[("low", "Low"), ("normal", "Normal"), ("high", "High"), ("urgent", "Urgent")],
                default="normal", max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="ticket", name="category",
            field=models.CharField(
                choices=[
                    ("payments", "Payments"), ("shipping", "Shipping"), ("domains", "Domains"),
                    ("themes", "Themes"), ("account", "Account"), ("other", "Other"),
                ],
                default="other", max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="ticket", name="assigned_to",
            field=models.ForeignKey(
                blank=True, db_column="assigned_to_id", null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_tickets", to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(model_name="ticket", name="resolved_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="ticketmessage", name="is_internal", field=models.BooleanField(default=False)),
    ]
