
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0001_initial"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["store", "-created_at"],
                name="ord_store_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="draftorder",
            index=models.Index(
                fields=["store", "-created_at"],
                name="drf_store_created_idx",
            ),
        ),
    ]
