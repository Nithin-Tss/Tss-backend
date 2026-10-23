from pathlib import Path

from django.db import migrations

PATH = "templates/login.liquid"
STARTER = Path(__file__).resolve().parent.parent / "starter" / PATH


def reset_login(apps, schema_editor):
    """Every theme's templates/login.liquid becomes the starter's again (undoes per-theme edits)."""
    content = STARTER.read_text(encoding="utf-8")
    size = len(content.encode("utf-8"))
    ThemeFile = apps.get_model("themes", "ThemeFile")
    ThemeFile.objects.filter(file_path=PATH).exclude(content=content).update(content=content, file_size=size)


class Migration(migrations.Migration):

    dependencies = [
        ("themes", "0006_storepage_template"),
    ]

    operations = [migrations.RunPython(reset_login, migrations.RunPython.noop)]
