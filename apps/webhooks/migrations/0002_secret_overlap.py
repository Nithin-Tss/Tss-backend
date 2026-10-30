from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("webhooks", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="webhookendpoint",
            name="previous_secret",
            field=models.CharField(blank=True, default="", max_length=80),
        ),
        migrations.AddField(
            model_name="webhookendpoint",
            name="previous_secret_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
