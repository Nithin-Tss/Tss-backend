import uuid

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db import migrations, models

import apps.helpdesk.models


class Migration(migrations.Migration):

    dependencies = [("helpdesk", "0003_backfill_ticket_numbers")]

    operations = [
        migrations.AlterField(
            model_name="ticket", name="ticket_number",
            field=models.CharField(editable=False, max_length=20, unique=True),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["open", "in_progress", "waiting_on_customer", "resolved", "closed"])),
                name="ck_ticket_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=models.Q(("priority__in", ["low", "normal", "high", "urgent"])), name="ck_ticket_priority",
            ),
        ),
        migrations.AddConstraint(
            model_name="ticket",
            constraint=models.CheckConstraint(
                condition=models.Q(("source__in", ["ai_escalation", "customer_request", "help_center"])),
                name="ck_ticket_source",
            ),
        ),
        migrations.AddIndex(
            model_name="ticket",
            index=models.Index(fields=["assigned_to", "status"], name="idx_ticket_assignee_status"),
        ),
        migrations.AddIndex(
            model_name="ticket", index=models.Index(fields=["conversation"], name="idx_ticket_conversation"),
        ),
        migrations.CreateModel(
            name="HelpArticle",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("title", models.CharField(max_length=200)),
                ("slug", models.SlugField(max_length=120, unique=True)),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("store_settings", "Store settings"), ("payments", "Payments"), ("shipping", "Shipping"),
                            ("discounts", "Discounts"), ("domains", "Domains"), ("themes", "Themes"),
                            ("other", "Other"),
                        ],
                        default="other", max_length=30,
                    ),
                ),
                ("body", models.TextField(help_text="Markdown.")),
                (
                    "keywords",
                    models.CharField(blank=True, default="", help_text="Comma-separated words merchants use.", max_length=500),
                ),
                ("is_published", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": '"helpdesk"."help_articles"', "ordering": ["category", "title"]},
        ),
        migrations.AddIndex(
            model_name="helparticle",
            index=models.Index(fields=["is_published", "category"], name="idx_help_article_pub_cat"),
        ),
        migrations.AddIndex(
            model_name="helparticle",
            index=apps.helpdesk.models.PgGinIndex(
                SearchVector("title", "keywords", "body", config="english"), name="gin_help_article_fts",
            ),
        ),
    ]
