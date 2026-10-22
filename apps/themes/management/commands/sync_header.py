from django.core.management.base import BaseCommand

from apps.themes import section_sync as sync
from apps.themes.models import Theme
from apps.themes.services import starter_files

PATH = "sections/header.liquid"


class Command(BaseCommand):
    help = "Replace every theme's sections/header.liquid with the starter's (the old one is kept in the section's history)."

    def handle(self, *args, **options):
        new = starter_files()[PATH]
        changed = 0
        for theme in Theme.objects.all():
            file = theme.files.filter(file_path=PATH).first()
            if file is None or file.content == new:
                continue
            sync.record_version(theme, sync.PATH_TO_SECTION[PATH], file.content, "upgrade")
            sync.invalidate(theme.pk, PATH)
            file.content = new
            file.file_size = len(new.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
            changed += 1
        self.stdout.write(f"Updated {changed} theme header(s).")
