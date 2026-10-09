
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="transfer",
            index=models.Index(
                fields=["store", "-created_at"],
                name="trf_store_created_idx",
            ),
        ),
    ]
