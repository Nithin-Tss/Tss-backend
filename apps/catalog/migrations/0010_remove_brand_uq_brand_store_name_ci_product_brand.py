
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0009_brand'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='brand',
            field=models.ForeignKey(
                blank=True,
                db_column='brand_id',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='products',
                to='catalog.brand',
            ),
        ),
    ]
