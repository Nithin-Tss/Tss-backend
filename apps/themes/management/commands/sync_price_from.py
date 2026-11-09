import difflib
import json
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.themes import section_sync as sync
from apps.themes.models import Theme

# Prices that vary by variant show the cheapest price alone: "$15.00", not "From $15.00".
OLD = "From {{ product.price | money }}"
NEW = "{{ product.price | money }}"
PATHS = ("snippets/product-card.liquid", "sections/main-product.liquid")


class Command(BaseCommand):
    help = (
        "Remove the word \"From\" before the cheapest price in ONE theme's product card and product page. "
        "Dry run unless --apply. The replaced files are saved to a JSON backup (these files have no section history)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--theme", required=True, help="the theme id to change (one theme at a time)")
        parser.add_argument("--apply", action="store_true", help="write the changes (default: only show them)")
        parser.add_argument("--backup-dir", default="backups/price_from", help="where the replaced files are saved")

    def handle(self, *args, **options):
        try:
            theme = Theme.objects.get(pk=options["theme"])
        except (Theme.DoesNotExist, ValueError):
            raise CommandError(f"no theme {options['theme']}")
        backup = {"theme": str(theme.pk), "name": theme.name, "saved_at": time.time(), "files": {}}
        changes = []
        for path in PATHS:
            file = theme.files.filter(file_path=path).first()
            if file is None or OLD not in file.content:
                self.stdout.write(f"{path}: nothing to change")
                continue
            new = file.content.replace(OLD, NEW)
            diff = [d for d in difflib.unified_diff(file.content.splitlines(), new.splitlines(), "current", "new", lineterm="", n=0)
                    if d[:1] in "+-" and d[:3] not in ("+++", "---")]
            self.stdout.write(f"{path}: {file.content.count(OLD)} place(s), +{len(diff) // 2} -{len(diff) // 2} lines")
            backup["files"][path] = file.content
            changes.append((file, new))
        if not options["apply"]:
            self.stdout.write(f"{theme.name} ({theme.pk}): dry run, pass --apply to write")
            return
        if not changes:
            return
        out = Path(options["backup_dir"])
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{theme.pk}-{int(backup['saved_at'])}.json"
        target.write_text(json.dumps(backup), encoding="utf-8")
        for file, new in changes:
            section = sync.PATH_TO_SECTION.get(file.file_path)
            if section:  # a synced section keeps the replaced file in its History as well
                sync.record_version(theme, section, file.content, "upgrade")
                sync.invalidate(theme.pk, file.file_path)
            file.content = new
            file.file_size = len(new.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
        self.stdout.write(f"Updated {len(changes)} file(s) in {theme.name} ({theme.pk}); old files saved to {target}")
