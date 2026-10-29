from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0002_store_image_bytes'),
    ]

    operations = [
        migrations.AddField(
            model_name='mediaasset',
            name='storage_key',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
    ]
