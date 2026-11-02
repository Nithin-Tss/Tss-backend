from django.core.management.base import BaseCommand, CommandError

from apps.webhooks import crypto
from apps.webhooks.models import WebhookEndpoint


class Command(BaseCommand):
    help = (
        "Encrypt the signing secrets of existing webhook endpoints (or re-encrypt them with the newest key) and check "
        "that each one decrypts back to the stored plaintext. Plaintext columns are never changed or removed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only report what would be done.")

    def handle(self, *args, **options):
        if not crypto.is_enabled():
            raise CommandError("Set WEBHOOK_SECRET_ENCRYPTION_KEYS first (for example v1:<at least 32 random characters>).")
        active = crypto.active_key_id()
        done = skipped = 0
        for endpoint in WebhookEndpoint.objects.order_by("created_at").iterator():
            current = (
                endpoint.secret_key_id == active
                and bool(endpoint.secret_encrypted) == bool(endpoint.secret)
                and bool(endpoint.previous_secret_encrypted) == bool(endpoint.previous_secret)
            )
            if current:
                skipped += 1
                continue
            if options["dry_run"]:
                done += 1
                continue
            fields = endpoint.refresh_encrypted()
            # Verify before saving: the encrypted copy must give back exactly the plaintext
            if crypto.decrypt(endpoint.secret_encrypted, endpoint._aad("secret")) != endpoint.secret or (
                endpoint.previous_secret
                and crypto.decrypt(endpoint.previous_secret_encrypted, endpoint._aad("previous_secret")) != endpoint.previous_secret
            ):
                raise CommandError(f"Endpoint {endpoint.pk}: decrypted value did not match; nothing was saved for it.")
            WebhookEndpoint.objects.filter(pk=endpoint.pk).update(**{f: getattr(endpoint, f) for f in fields})
            done += 1
        verb = "would encrypt" if options["dry_run"] else "encrypted"
        self.stdout.write(f"{verb} {done} endpoint(s), {skipped} already up to date (key '{active}').")
