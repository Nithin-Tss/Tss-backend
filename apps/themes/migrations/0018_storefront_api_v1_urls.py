"""
The storefront theme's JSON endpoints moved from /s/<store_slug>/... to the versioned API,
/api/v1/storefront/<store_slug>/... ({{ shop.api_url }}). Saved theme files that still call the old
addresses are rewritten (apps/themes/api_urls_rewrite.py).

A file still equal to a starter version listed in STARTER_UPGRADES is skipped: it was never edited, and
the starter upgrade (STARTER_REVISION 64) replaces it with the current starter file, which already uses
the new addresses. Rewriting it here would change its hash and stop that upgrade from recognising it.
"""
import hashlib

from django.db import migrations
from django.utils import timezone


def forwards(apps, schema_editor):
    from apps.themes.api_urls_rewrite import REWRITES, rewrite
    from apps.themes.services import STARTER_UPGRADES

    ThemeFile = apps.get_model("themes", "ThemeFile")
    olds = [old for old, _ in REWRITES]
    candidates = ThemeFile.objects.filter(file_path__endswith=".liquid").only("id", "file_path", "content")
    for file in candidates.iterator():
        if not any(old in file.content for old in olds):
            continue
        digest = hashlib.sha256(file.content.encode("utf-8")).hexdigest()
        if digest in STARTER_UPGRADES.get(file.file_path, ()):
            continue
        content = rewrite(file.content)
        # updated_at too: the renderer's file cache (services._files_state) notices the change
        ThemeFile.objects.filter(pk=file.pk).update(
            content=content, file_size=len(content.encode("utf-8")), updated_at=timezone.now(),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("themes", "0017_menuitem_badge_menuitem_is_mega_menuitem_promo_image_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
