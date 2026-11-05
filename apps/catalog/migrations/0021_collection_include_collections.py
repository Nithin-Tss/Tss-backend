from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0020_product_options'),
    ]

    operations = [
        migrations.AddField(
            model_name='collection',
            name='include_collections',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
