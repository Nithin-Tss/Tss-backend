"""
Outbound webhooks: events are queued as WebhookDelivery rows (`emit`) and sent by the
`deliver_webhooks` command (`deliver_due`), so a slow or dead endpoint never slows an order down.

Each request is a JSON POST signed like Stripe's: the header X-TSS-Signature is
"t=<unix time>,v1=<hex>", where v1 is HMAC-SHA256 of "<t>.<raw body>" with the endpoint's secret.
For a while after a secret is rotated the header carries a second ",v1=<hex>" made with the old secret:
a receiver accepts the request if any v1 matches. `verify_signature` below is a reference receiver:

    from apps.webhooks.services import verify_signature
    if not verify_signature(secret, request.headers["X-TSS-Signature"], request.body):
        return HttpResponse(status=400)

It also rejects a timestamp more than 5 minutes old, so a captured request can't be replayed later.
"""
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .events import PING
from .models import FAILED, PENDING, SUCCEEDED, WebhookDelivery, WebhookEndpoint, new_secret
from .security import UnsafeURL, check_url

logger = logging.getLogger(__name__)

# Wait before attempt 2, 3, ...; after the last wait one more failure is final.
RETRY_DELAYS = [timedelta(minutes=m) for m in (1, 5, 30, 120, 360, 720)]
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
TIMEOUT_SECONDS = 10
# An endpoint that fails this many deliveries for good in a row is switched off
DISABLE_AFTER = 15
# How long a worker owns a delivery it picked up; if it dies, the delivery becomes due again after this
LEASE = timedelta(minutes=5)
# How long the old secret keeps signing after rotate-secret
SECRET_OVERLAP = timedelta(hours=24)
# How far a signature's timestamp may be from the receiver's clock
SIGNATURE_TOLERANCE_SECONDS = 300


def sign(secret, timestamp, body):
    """The header value; `secret` may be one secret or a list (current first, then the one being retired)."""
    secrets_ = [secret] if isinstance(secret, str) else secret
    macs = [hmac.new(s.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest() for s in secrets_]
    return f"t={timestamp}," + ",".join(f"v1={m}" for m in macs)


def verify_signature(secret, header, body, tolerance=SIGNATURE_TOLERANCE_SECONDS, now=None):
    """What a receiver does: True if `header` holds a v1 signature of `body` made with `secret` and is recent."""
    try:
        parts = [p.split("=", 1) for p in header.split(",")]
        timestamp = int([v for k, v in parts if k == "t"][0])
    except (AttributeError, IndexError, ValueError):
        return False
    if abs((now if now is not None else time.time()) - timestamp) > tolerance:
        return False
    expected = sign(secret, timestamp, body).split("v1=")[1]
    return any(hmac.compare_digest(expected, v) for k, v in parts if k == "v1")


def rotate_secret(endpoint, overlap=SECRET_OVERLAP):
    """New secret now; the old one keeps signing for `overlap` so the receiver can be updated without dropping events."""
    endpoint.previous_secret = endpoint.secret
    endpoint.previous_secret_expires_at = timezone.now() + overlap
    endpoint.secret = new_secret()
    endpoint.save(update_fields=["secret", "previous_secret", "previous_secret_expires_at", "updated_at"])
    return endpoint


def emit(store, event_type, data):
    """Queue `event_type` for every active endpoint of `store` that subscribed to it."""
    endpoints = [e for e in WebhookEndpoint.objects.filter(store=store, is_active=True) if e.wants(event_type)]
    if not endpoints:
        return []
    event_id = uuid.uuid4()
    payload = {
        "id": str(event_id),
        "type": event_type,
        "created": int(time.time()),
        "store_id": str(store.pk),
        "data": {"object": data},
    }
    now = timezone.now()
    return WebhookDelivery.objects.bulk_create(
        WebhookDelivery(endpoint=e, event_id=event_id, event_type=event_type, payload=payload, next_attempt_at=now)
        for e in endpoints
    )


def queue_ping(endpoint):
    """A test delivery to one endpoint, whether or not it subscribed to anything."""
    event_id = uuid.uuid4()
    payload = {
        "id": str(event_id), "type": PING, "created": int(time.time()),
        "store_id": str(endpoint.store_id), "data": {"object": {"message": "This is a test webhook."}},
    }
    return WebhookDelivery.objects.create(
        endpoint=endpoint, event_id=event_id, event_type=PING, payload=payload, next_attempt_at=timezone.now(),
    )


def retry(delivery):
    """Queue a finished delivery again, as if new."""
    delivery.status, delivery.attempts, delivery.next_attempt_at = PENDING, 0, timezone.now()
    delivery.last_error, delivery.delivered_at = "", None
    delivery.save(update_fields=["status", "attempts", "next_attempt_at", "last_error", "delivered_at"])


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect could lead to an internal address the URL check never saw, so it counts as a failure."""

    def redirect_request(self, *args, **kwargs):
        return None


def post(url, body, headers):
    """Returns the HTTP status code. Raises OSError (URLError, timeouts) when nothing came back."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def attempt(delivery):
    """Send once and record the outcome; schedules the retry or gives up."""
    endpoint = delivery.endpoint
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "TSS-Webhooks/1",
        "X-TSS-Event": delivery.event_type,
        "X-TSS-Delivery": str(delivery.pk),
        "X-TSS-Signature": sign(endpoint.signing_secrets(), int(time.time()), body),
    }
    status_code, error = None, ""
    try:
        check_url(endpoint.url)
        status_code = post(endpoint.url, body, headers)
        if not 200 <= status_code < 300:
            error = f"The endpoint answered {status_code}."
    except UnsafeURL as exc:
        error = str(exc)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]

    delivery.attempts += 1
    delivery.last_status_code = status_code
    delivery.last_error = error[:500]
    if not error:
        delivery.status, delivery.delivered_at, delivery.next_attempt_at = SUCCEEDED, timezone.now(), None
    elif delivery.attempts >= MAX_ATTEMPTS:
        delivery.status, delivery.next_attempt_at = FAILED, None
    else:
        delivery.next_attempt_at = timezone.now() + RETRY_DELAYS[delivery.attempts - 1]
    delivery.save()
    if delivery.status != PENDING:
        _track_health(endpoint, ok=delivery.status == SUCCEEDED)
    return delivery


def _track_health(endpoint, ok):
    if ok:
        if endpoint.consecutive_failures:
            WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=0)
        return
    endpoint = WebhookEndpoint.objects.get(pk=endpoint.pk)
    endpoint.consecutive_failures += 1
    if endpoint.consecutive_failures >= DISABLE_AFTER and endpoint.is_active:
        endpoint.is_active = False
        endpoint.disabled_reason = f"Switched off after {DISABLE_AFTER} failed deliveries in a row."
        disabled = True
    else:
        disabled = False
    endpoint.save(update_fields=["consecutive_failures", "is_active", "disabled_reason", "updated_at"])
    if disabled:
        _notify_disabled(endpoint)


def _notify_disabled(endpoint):
    """Email the store's owners: nothing else tells them their endpoint stopped receiving events."""
    from apps.core.tasks import send_email_async
    from apps.tenancy.models import StoreMembership
    from apps.tenancy.services import ACTIVE, OWNER

    try:
        owners = StoreMembership.objects.filter(store_id=endpoint.store_id, role=OWNER, status=ACTIVE).select_related("user")
        recipients = sorted({m.user.email for m in owners if m.user.email})
        if recipients:
            send_email_async(
                f"Webhook endpoint switched off: {endpoint.url}",
                f"{endpoint.disabled_reason}\n\nEndpoint: {endpoint.url}\n"
                "Fix the receiving server, then turn the endpoint back on in the webhook settings. "
                "Failed deliveries can be retried from the deliveries list.",
                recipients,
            )
    except Exception:
        logger.exception("Could not tell the owners of store %s that %s was switched off", endpoint.store_id, endpoint.pk)


def claim_due(limit=50):
    """
    Pick up deliveries that are due. Their next_attempt_at moves forward by LEASE, so two workers
    never send the same one and a crashed worker's batch comes back by itself.
    """
    now = timezone.now()
    with transaction.atomic():
        ids = list(
            WebhookDelivery.objects.select_for_update(skip_locked=True)
            .filter(status=PENDING, next_attempt_at__lte=now, endpoint__is_active=True)
            .order_by("next_attempt_at")
            .values_list("pk", flat=True)[:limit]
        )
        WebhookDelivery.objects.filter(pk__in=ids).update(next_attempt_at=now + LEASE)
    return list(WebhookDelivery.objects.filter(pk__in=ids).select_related("endpoint").order_by("next_attempt_at"))


def deliver_due(limit=50):
    """Send everything that is due. Returns how many were attempted."""
    deliveries = claim_due(limit)
    for delivery in deliveries:
        try:
            attempt(delivery)
        except Exception:
            logger.exception("Webhook delivery %s crashed", delivery.pk)
    return len(deliveries)


def prune_deliveries(older_than_days):
    """Delete finished (succeeded or failed) deliveries older than the cutoff; pending ones are never touched. Returns how many."""
    cutoff = timezone.now() - timedelta(days=older_than_days)
    return WebhookDelivery.objects.filter(status__in=(SUCCEEDED, FAILED), created_at__lt=cutoff).delete()[0]
