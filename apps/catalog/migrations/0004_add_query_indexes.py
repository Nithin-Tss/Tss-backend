
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0003_productreview_order_item"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                fields=["store", "-created_at"],
                name="prd_store_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="product",
            index=models.Index(
                fields=["store", "status", "-created_at"],
                name="prd_store_stat_date_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="productvariant",
            index=models.Index(
                fields=["product", "created_at"],
                name="pvar_prod_created_idx",
            ),
        ),
    ]
