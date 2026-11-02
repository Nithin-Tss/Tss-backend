"""
Outbound webhooks: an event is first written to the outbox (WebhookEvent, `emit`), in the same transaction as the
change that caused it; `dispatch_event` then fans it out into one WebhookDelivery per subscribed endpoint, and the
`deliver_webhooks` command (`deliver_due`) sends them, so a slow or dead endpoint never slows an order down.

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

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .events import PING
from .models import (
    DISPATCHED, FAILED, PENDING, SUCCEEDED, WebhookDelivery, WebhookEndpoint, WebhookEvent, new_secret,
)
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
# An outbox event that can't be turned into deliveries this many times is parked as failed
MAX_DISPATCH_ATTEMPTS = 10
# How long the old secret keeps signing after rotate-secret
SECRET_OVERLAP = timedelta(hours=24)
# How far a signature's timestamp may be from the receiver's clock
SIGNATURE_TOLERANCE_SECONDS = 300

# event type -> (model label, describe function) for events whose payload is built at dispatch time from the
# saved row, once the lines saved right after it exist. Filled in by receivers.py.
LAZY_RESOURCES = {}


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
    endpoint.previous_secret = endpoint.get_secret()
    endpoint.previous_secret_expires_at = timezone.now() + overlap
    endpoint.secret = new_secret()
    endpoint.save(update_fields=["secret", "previous_secret", "previous_secret_expires_at", "updated_at"])
    return endpoint


def _envelope(event, data):
    return {
        "id": str(event.pk),
        "type": event.event_type,
        "created": int(event.created_at.timestamp()),
        "store_id": str(event.store_id),
        "data": {"object": data},
    }


def emit(store, event_type, data=None, dedupe_key=None, resource=None):
    """
    Record `event_type` in the outbox for `store`; returns the WebhookEvent (None if no endpoint wants it).
    Call it inside the transaction of the change it announces: the row commits or rolls back with it.
    `dedupe_key` identifies the business operation ("order.created:<order id>"): emitting it again returns the
    first event instead of making a second. `resource=(model label, pk)` defers building the payload to dispatch.
    """
    if not any(e.wants(event_type) for e in WebhookEndpoint.objects.filter(store=store, is_active=True)):
        return None
    event, created = WebhookEvent.objects.get_or_create(
        store=store,
        dedupe_key=dedupe_key or f"auto:{uuid.uuid4()}",
        defaults={
            "event_type": event_type,
            # The envelope's id is the event's own id, filled in at dispatch
            "payload": None if resource else {
                "type": event_type, "created": int(time.time()), "store_id": str(store.pk), "data": {"object": data},
            },
            "resource_model": resource[0] if resource else "",
            "resource_id": str(resource[1]) if resource else "",
        },
    )
    if created:
        transaction.on_commit(lambda: _kick(event.pk))
    return event


def _kick(event_id):
    """Ask Celery to dispatch now. If the broker is down the event stays pending and the sweeper picks it up."""
    from .tasks import dispatch_event_task

    try:
        dispatch_event_task.delay(str(event_id))
    except Exception:
        logger.exception("Could not queue the dispatch of webhook event %s; the sweeper will retry it", event_id)


def _build_lazy_payload(event):
    _, describe = LAZY_RESOURCES[event.event_type]
    row = apps.get_model(event.resource_model).objects.get(pk=event.resource_id)
    return _envelope(event, describe(row))


def dispatch_event(event_id):
    """Fan one pending outbox event out into deliveries. Safe to call twice or from two workers. Returns deliveries made."""
    try:
        with transaction.atomic():
            event = (
                WebhookEvent.objects.select_for_update(skip_locked=True).select_related("store")
                .filter(pk=event_id, status=PENDING).first()
            )
            if event is None:
                return 0
            payload = dict(event.payload) if event.payload else _build_lazy_payload(event)
            payload["id"] = str(event.pk)
            payload.setdefault("created", int(event.created_at.timestamp()))
            endpoints = [
                e for e in WebhookEndpoint.objects.filter(store=event.store, is_active=True) if e.wants(event.event_type)
            ]
            now = timezone.now()
            WebhookDelivery.objects.bulk_create(
                [
                    WebhookDelivery(
                        endpoint=e, event_id=event.pk, event_type=event.event_type, payload=payload,
                        next_attempt_at=now, destination_url=e.url, outbox_event=event,
                    )
                    for e in endpoints
                ],
                ignore_conflicts=True,  # (endpoint, event_id) is unique: a repeated dispatch adds nothing
            )
            event.payload, event.status, event.dispatched_at, event.last_error = payload, DISPATCHED, now, ""
            event.save(update_fields=["payload", "status", "dispatched_at", "last_error"])
            return len(endpoints)
    except Exception as exc:
        logger.exception("Webhook event %s could not be dispatched", event_id)
        pending = WebhookEvent.objects.filter(pk=event_id, status=PENDING)
        pending.update(dispatch_attempts=F("dispatch_attempts") + 1, last_error=f"{type(exc).__name__}: {exc}"[:500])
        pending.filter(dispatch_attempts__gte=MAX_DISPATCH_ATTEMPTS).update(status=FAILED)
        return 0


def dispatch_pending(limit=100):
    """The sweeper: dispatch outbox events still pending (Celery was down, a worker died, ...). Returns how many it saw."""
    ids = list(WebhookEvent.objects.filter(status=PENDING).order_by("created_at").values_list("pk", flat=True)[:limit])
    for event_id in ids:
        dispatch_event(event_id)
    return len(ids)


def queue_ping(endpoint):
    """A test delivery to one endpoint, whether or not it subscribed to anything."""
    event_id = uuid.uuid4()
    payload = {
        "id": str(event_id), "type": PING, "created": int(time.time()),
        "store_id": str(endpoint.store_id), "data": {"object": {"message": "This is a test webhook."}},
    }
    return WebhookDelivery.objects.create(
        endpoint=endpoint, event_id=event_id, event_type=PING, payload=payload, next_attempt_at=timezone.now(),
        destination_url=endpoint.url,
    )


def retry(delivery):
    """Queue a finished delivery again, as if new."""
    delivery.status, delivery.attempts, delivery.next_attempt_at = PENDING, 0, timezone.now()
    delivery.last_error, delivery.delivered_at = "", None
    delivery.lease_token, delivery.locked_until = None, None
    delivery.save(update_fields=[
        "status", "attempts", "next_attempt_at", "last_error", "delivered_at", "lease_token", "locked_until",
    ])


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
    # The destination frozen when the delivery was created; older rows fall back to the endpoint's current URL
    url = delivery.destination_url or endpoint.url
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
        check_url(url)
        status_code = post(url, body, headers)
        if not 200 <= status_code < 300:
            error = f"The endpoint answered {status_code}."
    except UnsafeURL as exc:
        error = str(exc)
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"[:500]

    attempts = delivery.attempts + 1
    fields = {"attempts": attempts, "last_status_code": status_code, "last_error": error[:500]}
    if not error:
        fields.update(status=SUCCEEDED, delivered_at=timezone.now(), next_attempt_at=None)
    elif attempts >= MAX_ATTEMPTS:
        fields.update(status=FAILED, next_attempt_at=None)
    else:
        fields.update(next_attempt_at=timezone.now() + RETRY_DELAYS[attempts - 1])
    # Fencing: the result is written only while this worker still owns the lease it claimed with. If the lease ran out
    # and another worker took over, this matches nothing and the newer owner's outcome stands.
    written = WebhookDelivery.objects.filter(pk=delivery.pk, lease_token=delivery.lease_token).update(
        **fields, lease_token=None, locked_until=None,
    )
    if not written:
        logger.warning("Webhook delivery %s: its lease was lost, the result of this attempt is discarded", delivery.pk)
        return delivery
    for name, value in fields.items():
        setattr(delivery, name, value)
    delivery.lease_token = delivery.locked_until = None
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
    Pick up deliveries that are due and not leased. Each claim stamps a fresh lease_token and locked_until, so two
    workers never send the same one, a crashed worker's batch is claimable again once locked_until passes, and a
    worker that outlives its lease can no longer record a result (see `attempt`).
    """
    now = timezone.now()
    token = uuid.uuid4()
    with transaction.atomic():
        ids = list(
            WebhookDelivery.objects.select_for_update(skip_locked=True)
            .filter(status=PENDING, next_attempt_at__lte=now, endpoint__is_active=True)
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .order_by("next_attempt_at")
            .values_list("pk", flat=True)[:limit]
        )
        # next_attempt_at also moves forward, as before, so a worker still on the old code can't take them either
        WebhookDelivery.objects.filter(pk__in=ids).update(
            next_attempt_at=now + LEASE, lease_token=token, locked_until=now + LEASE,
        )
    return list(WebhookDelivery.objects.filter(pk__in=ids).select_related("endpoint").order_by("next_attempt_at"))


def deliver_due(limit=50):
    """Send everything that is due. Returns how many were attempted."""
    dispatch_pending()  # outbox events whose immediate dispatch never happened
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
