from django.conf import settings
from django.core.management.base import BaseCommand

from apps.tenancy.models import Store
from apps.themes.default_provisioning import MARK, cutoff, is_eligible, missing_files, provision_default_files
from apps.themes.models import StoreTheme
from apps.themes.services import is_private


class Command(BaseCommand):
    help = (
        "Add the default theme files to the published theme of every store created at or after "
        "DEFAULT_TREE_CUTOFF that doesn't have them yet (only while DEFAULT_TREE_ENABLED is on). "
        "Existing files are never changed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only list the stores and files that would change.")

    def handle(self, *args, dry_run=False, **options):
        if not getattr(settings, "DEFAULT_TREE_ENABLED", False):
            self.stdout.write("DEFAULT_TREE_ENABLED is off: nothing to do.")
            return

        rows = StoreTheme.objects.select_related("store", "theme").filter(
            is_active=True, store__in=Store.objects.filter(created_at__gte=cutoff()),
        )
        changed = 0
        for row in rows:
            if not is_private(row.store, row.theme) or not is_eligible(row.store):
                continue
            if (row.theme.theme_data or {}).get(MARK):  # already has the tree: files it deleted stay deleted
                continue
            paths = sorted(missing_files(row)) if dry_run else provision_default_files(row)
            if paths:
                changed += 1
                self.stdout.write(f"{row.store.store_name} ({row.store.store_id}): {len(paths)} file(s)")
                for path in paths:
                    self.stdout.write(f"  {path}")

        verb = "would change" if dry_run else "changed"
        self.stdout.write(f"{changed} store(s) {verb}.")
