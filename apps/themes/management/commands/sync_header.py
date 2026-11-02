from django.core.management.base import BaseCommand

from apps.themes import section_sync as sync
from apps.themes.models import Theme
from apps.themes.services import starter_files

PATHS = ("sections/header.liquid", "snippets/header.liquid")


class Command(BaseCommand):
    help = "Replace every theme's sections/header.liquid and snippets/header.liquid with the starter's (the old one is kept in the section's history)."

    def handle(self, *args, **options):
        starter = starter_files()
        changed = 0
        for path in PATHS:
            new = starter.get(path)
            if not new:
                continue
            for theme in Theme.objects.all():
                file = theme.files.filter(file_path=path).first()
                if file is None or file.content == new:
                    continue
                section = sync.PATH_TO_SECTION.get(path)
                if section:
                    sync.record_version(theme, section, file.content, "upgrade")
                    sync.invalidate(theme.pk, path)
                file.content = new
                file.file_size = len(new.encode("utf-8"))
                file.save(update_fields=["content", "file_size", "updated_at"])
                changed += 1
        self.stdout.write(f"Updated {changed} theme header file(s).")
