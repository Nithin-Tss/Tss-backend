"""
One-off: picture addresses saved while developing point at http://localhost:8000/s/_img/<id>, which a deployed
storefront cannot load (the browser blocks public pages from reaching the loopback address). This finds them in
every text and JSON column and, with --apply, puts them on the backend's public address instead.

    python manage.py fix_localhost_image_urls                 # report only, changes nothing
    python manage.py fix_localhost_image_urls --apply         # rewrite to PUBLIC_API_URL
    python manage.py fix_localhost_image_urls --apply --base https://api.example.com

Only the host part before /s/_img/ is changed; the picture ids, the pictures and other URLs are left alone.
Safe to repeat: rewritten addresses no longer match.
"""
import re

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django.db.models.functions import Cast

LOCAL_IMAGE = re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?=/s/_img/)")
TEXT_FIELDS = (models.CharField, models.TextField, models.JSONField)
SKIPPED_APPS = {"audit"}  # the audit log is history: it keeps the address as it was


def _rewrite(value, base):
    """The value with every local picture host replaced, and how many were replaced."""
    if isinstance(value, str):
        return LOCAL_IMAGE.subn(base, value)
    if isinstance(value, list):
        out, total = [], 0
        for item in value:
            new, n = _rewrite(item, base)
            out.append(new)
            total += n
        return out, total
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            new, n = _rewrite(item, base)
            out[key] = new
            total += n
        return out, total
    return value, 0


class Command(BaseCommand):
    help = "Report (or, with --apply, rewrite) picture URLs saved as http://localhost:8000/s/_img/<id>."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes. Without it nothing is changed.")
        parser.add_argument("--base", default="", help="New address, e.g. https://api.example.com (default: PUBLIC_API_URL).")

    def handle(self, *args, apply=False, base="", **options):
        base = (base or settings.PUBLIC_API_URL).strip().rstrip("/")
        if apply and not re.match(r"^https?://[^/\s]+$", base):
            raise CommandError("Set PUBLIC_API_URL (or pass --base https://api.example.com) before --apply.")
        if apply and LOCAL_IMAGE.match(base + "/s/_img/"):
            raise CommandError(f"{base} is itself a local address; nothing would be fixed.")

        rows = urls = 0
        for model in apps.get_models():
            if model._meta.proxy or not model._meta.managed or model._meta.app_label in SKIPPED_APPS:
                continue
            for field in model._meta.concrete_fields:
                if not isinstance(field, TEXT_FIELDS):
                    continue
                column = field.attname
                found = (
                    model._default_manager.annotate(_as_text=Cast(column, models.TextField()))
                    .filter(_as_text__regex=r"(localhost|127\.0\.0\.1)(:[0-9]+)?/s/_img/")
                    .only("pk", column)
                )
                for obj in found.iterator():
                    new, n = _rewrite(getattr(obj, column), base or "<PUBLIC_API_URL>")
                    if not n:
                        continue
                    rows += 1
                    urls += n
                    self.stdout.write(f"{model._meta.label}.{column} pk={obj.pk}: {n} URL(s)")
                    if apply:
                        with transaction.atomic():
                            model._default_manager.filter(pk=obj.pk).update(**{column: new})

        verb = "rewrote" if apply else "would rewrite"
        target = base or "<PUBLIC_API_URL not set>"
        self.stdout.write(self.style.SUCCESS(f"{verb} {urls} URL(s) in {rows} row(s) to {target}."))
        if not apply:
            self.stdout.write("Dry run: nothing was changed. Re-run with --apply to write.")
