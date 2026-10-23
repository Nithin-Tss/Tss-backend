from pathlib import Path

from django.db import migrations

STARTER = Path(__file__).resolve().parent.parent / "starter"
PATHS = ("templates/login.liquid", "templates/register.liquid")


def reset(apps, schema_editor):
    """Every theme's login and register pages become the starter's styled ones."""
    ThemeFile = apps.get_model("themes", "ThemeFile")
    for path in PATHS:
        content = (STARTER / path).read_text(encoding="utf-8")
        ThemeFile.objects.filter(file_path=path).exclude(content=content).update(
            content=content, file_size=len(content.encode("utf-8"))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("themes", "0009_add_login_register_templates"),
    ]

    operations = [migrations.RunPython(reset, migrations.RunPython.noop)]
