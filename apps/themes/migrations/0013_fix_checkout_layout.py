from pathlib import Path

from django.db import migrations

STARTER = Path(__file__).resolve().parent.parent / "starter"
PATHS = ("templates/checkout.liquid", "sections/main-checkout.liquid")


def reset(apps, schema_editor):
    """Fix the checkout layout (grid overflow, mobile spacing) in every theme."""
    ThemeFile = apps.get_model("themes", "ThemeFile")
    for path in PATHS:
        content = (STARTER / path).read_text(encoding="utf-8")
        ThemeFile.objects.filter(file_path=path).exclude(content=content).update(
            content=content, file_size=len(content.encode("utf-8"))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("themes", "0012_lock_checkout_design"),
    ]

    operations = [migrations.RunPython(reset, migrations.RunPython.noop)]
