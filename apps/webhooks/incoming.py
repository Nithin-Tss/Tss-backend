"""
Incoming provider webhooks (Stripe). The view verifies the provider's signature, then `record_receipt` stores the
verified event in PostgreSQL; only after that does it answer 2xx. Processing starts from the stored row (Celery, or
the sweeper if Celery/Redis was down), is leased so two workers never run the same receipt, and is retried with a
back-off. The (provider, external_event_id) pair is unique, so a repeated or concurrent delivery of the same event
is stored once and processed once.
"""
import hashlib
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import FAILED, PROCESSED, RECEIVED, IncomingWebhookReceipt

logger = logging.getLogger(__name__)

STRIPE = "stripe"
LEASE = timedelta(minutes=5)
MAX_ATTEMPTS = 9
# Retrying can't fix a payload or a rule that is wrong; only outages and lock timeouts are worth another go
PERMANENT = (ValueError, KeyError, TypeError, ValidationError)


def _handlers():
    # Imported on use so this app does not depend on payments at import time
    from apps.payments.tasks import _handle

    return {STRIPE: _handle}


def record_receipt(provider, data):
    """Store a verified event (a dict). Returns (receipt, created); a repeat returns the stored one with created=False."""
    data = json.loads(json.dumps(data, default=vars))
    external_id = str(data.get("id") or "") or "sha256:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    receipt, created = IncomingWebhookReceipt.objects.get_or_create(
        provider=provider,
        external_event_id=external_id,
        defaults={"event_type": str(data.get("type") or "")[:100], "payload": data, "next_attempt_at": timezone.now()},
    )
    return receipt, created


def enqueue(receipt):
    """Start processing now. With no broker (development) it runs inline and its errors propagate, as before."""
    from .tasks import process_receipt_task

    if settings.CELERY_TASK_ALWAYS_EAGER:
        process_receipt_task.delay(str(receipt.pk))
        return
    try:
        process_receipt_task.delay(str(receipt.pk))
    except Exception:
        # The receipt is already safe in PostgreSQL; the sweeper will process it
        logger.exception("Could not queue receipt %s; the sweeper will process it", receipt.pk)


def claim_receipt(receipt_id):
    """Lease one receipt for processing; None if it is done, failed, or leased by another worker."""
    now = timezone.now()
    token = uuid.uuid4()
    with transaction.atomic():
        receipt = (
            IncomingWebhookReceipt.objects.select_for_update(skip_locked=True)
            .filter(pk=receipt_id, status=RECEIVED)
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .first()
        )
        if receipt is None:
            return None
        receipt.lease_token, receipt.locked_until = token, now + LEASE
        receipt.save(update_fields=["lease_token", "locked_until"])
    return receipt


def process_receipt(receipt_id):
    """
    Run the provider handler for one receipt. Returns True if it was processed now. On failure the outcome is
    recorded (retry later, or failed for good) and the error is re-raised for the caller to decide about.
    The writes are fenced by the lease token, so a worker whose lease ran out cannot overwrite the new owner.
    """
    receipt = claim_receipt(receipt_id)
    if receipt is None:
        return False
    mine = IncomingWebhookReceipt.objects.filter(pk=receipt.pk, lease_token=receipt.lease_token)
    try:
        _handlers()[receipt.provider](receipt.payload)
    except Exception as exc:
        attempts = receipt.attempts + 1
        final = isinstance(exc, PERMANENT) or attempts >= MAX_ATTEMPTS
        fields = {"attempts": attempts, "last_error": f"{type(exc).__name__}: {exc}"[:500]}
        if final:
            fields["status"] = FAILED
            logger.critical("%s event %s dropped, needs attention", receipt.provider, receipt.external_event_id, exc_info=exc)
        else:
            fields["next_attempt_at"] = timezone.now() + timedelta(seconds=min(2 ** attempts * 15, 900))
        mine.update(**fields, lease_token=None, locked_until=None)
        raise
    mine.update(
        status=PROCESSED, processed_at=timezone.now(), attempts=receipt.attempts + 1, last_error="",
        lease_token=None, locked_until=None,
    )
    return True


def process_pending(limit=50):
    """The sweeper: process receipts still waiting (Celery was down, a worker died, a retry came due). Returns how many it tried."""
    now = timezone.now()
    ids = list(
        IncomingWebhookReceipt.objects.filter(status=RECEIVED, next_attempt_at__lte=now)
        .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
        .order_by("received_at").values_list("pk", flat=True)[:limit]
    )
    for receipt_id in ids:
        try:
            process_receipt(receipt_id)
        except Exception:
            logger.exception("Receipt %s failed", receipt_id)
    return len(ids)
