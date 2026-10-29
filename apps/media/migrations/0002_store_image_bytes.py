import apps.media.models
from django.db import migrations, models


def drop_file_unique(apps, schema_editor):
    """
    A picture kept in the database has no file on disk, and several of them would share the blank value, so the
    unique constraint on `file` (created by 0001) goes. Django's AlterField leaves it behind on PostgreSQL.
    """
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute('ALTER TABLE "media"."media_assets" DROP CONSTRAINT IF EXISTS "media_assets_file_key"')


class Migration(migrations.Migration):

    dependencies = [
        ('media', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='mediaasset',
            name='data',
            field=models.BinaryField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='mediaasset',
            name='file',
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=apps.media.models.media_upload_path),
        ),
        migrations.RunPython(drop_file_unique, migrations.RunPython.noop),
    ]
