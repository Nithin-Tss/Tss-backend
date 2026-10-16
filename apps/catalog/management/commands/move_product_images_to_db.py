"""
One-off: copy product photos saved on disk (MEDIA_ROOT/products/...) into the
database and point each product image at its new /s/_img/<id> address.

    python manage.py move_product_images_to_db --dry-run
    python manage.py move_product_images_to_db

Run it where the files still exist (the server that holds media/). Safe to
repeat: images already moved no longer contain /media/.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import ProductImage
from apps.themes.models import ThemeImage

TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class Command(BaseCommand):
    help = "Copy product photos from the media folder into the database."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would move, change nothing.")

    def handle(self, *args, dry_run=False, **options):
        root = Path(settings.MEDIA_ROOT).resolve()
        moved = missing = 0

        for image in ProductImage.objects.filter(url__contains=settings.MEDIA_URL).select_related("product"):
            prefix, _, name = image.url.partition(settings.MEDIA_URL)
            path = (root / name).resolve()
            if root not in path.parents or not path.is_file() or path.suffix.lower() not in TYPES:
                missing += 1
                self.stderr.write(f"skip (file not found): {image.url}")
                continue
            if not dry_run:
                data = path.read_bytes()
                with transaction.atomic():
                    stored = ThemeImage.objects.create(
                        store_id=image.product.store_id, content_type=TYPES[path.suffix.lower()],
                        data=data, size=len(data),
                    )
                    image.url = f"{prefix}/s/_img/{stored.id}"
                    image.save(update_fields=["url"])
            moved += 1

        verb = "would move" if dry_run else "moved"
        self.stdout.write(self.style.SUCCESS(f"{verb} {moved} image(s); {missing} skipped."))
