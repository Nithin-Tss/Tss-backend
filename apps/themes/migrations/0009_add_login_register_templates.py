from pathlib import Path

from django.db import migrations

STARTER = Path(__file__).resolve().parent.parent / "starter"
PATHS = ("templates/login.liquid", "templates/register.liquid")


def add_missing(apps, schema_editor):
    """Every theme gets the starter's login and register pages (those lacking them are given them)."""
    Theme = apps.get_model("themes", "Theme")
    ThemeFile = apps.get_model("themes", "ThemeFile")
    for path in PATHS:
        content = (STARTER / path).read_text(encoding="utf-8")
        size = len(content.encode("utf-8"))
        ThemeFile.objects.filter(file_path=path).exclude(content=content).update(content=content, file_size=size)
        have = set(ThemeFile.objects.filter(file_path=path).values_list("theme_id", flat=True))
        name = path.rpartition("/")[2]
        ThemeFile.objects.bulk_create([
            ThemeFile(
                theme=theme, file_path=path, file_name=name, directory="templates", file_type="liquid",
                content=content, is_required=True, file_size=size,
            )
            for theme in Theme.objects.exclude(pk__in=have)
        ])


class Migration(migrations.Migration):

    dependencies = [
        ("themes", "0008_reset_login_register_templates"),
    ]

    operations = [migrations.RunPython(add_missing, migrations.RunPython.noop)]
