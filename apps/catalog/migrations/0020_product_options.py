from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0019_catalog_integrity'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='options',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
