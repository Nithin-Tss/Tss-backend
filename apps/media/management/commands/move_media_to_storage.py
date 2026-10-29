"""
One-off: copy media library pictures stored in the database into the configured bucket (MEDIA_S3_BUCKET).

    python manage.py move_media_to_storage --dry-run
    python manage.py move_media_to_storage

Safe to repeat: pictures already in the bucket are skipped. Their /s/_img/<id> addresses keep working (they redirect).
"""
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from apps.media import storage
from apps.media.models import MediaAsset
from apps.media.services import IMAGE_EXTENSIONS


class Command(BaseCommand):
    help = "Copy media pictures from the database into the S3-compatible bucket."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would move, change nothing.")

    def handle(self, *args, dry_run=False, **options):
        if not storage.is_configured():
            raise CommandError("Set MEDIA_S3_BUCKET (and the other MEDIA_S3_* settings) first.")
        moved = failed = 0
        for asset in MediaAsset.objects.filter(storage_key__isnull=True, data__isnull=False).iterator():
            key = f"uploads/{asset.store_id}/{asset.id.hex}{IMAGE_EXTENSIONS.get(asset.mime_type, '')}"
            if not dry_run:
                try:
                    storage.put(key, bytes(asset.data), asset.mime_type)
                except Exception as exc:
                    failed += 1
                    self.stderr.write(f"skip {asset.id}: {exc}")
                    continue
                MediaAsset.objects.filter(pk=asset.pk).update(storage_key=key, data=None)
                cache.delete(f"theme-image:{asset.id}")
            moved += 1
        verb = "would move" if dry_run else "moved"
        self.stdout.write(self.style.SUCCESS(f"{verb} {moved} picture(s); {failed} failed."))
