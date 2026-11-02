from celery import shared_task


@shared_task
def dispatch_event_task(event_id):
    from . import services

    return services.dispatch_event(event_id)


@shared_task(bind=True)
def process_receipt_task(self, receipt_id):
    from . import incoming

    try:
        return incoming.process_receipt(receipt_id)
    except Exception:
        # The failure is already recorded on the receipt and the sweeper retries it. With no broker the error goes
        # back to the caller instead, so the provider (Stripe) gets a non-2xx and retries, as it always did.
        if self.request.is_eager:
            raise
        return False


@shared_task
def sweep_webhooks():
    """Safety net, every few seconds: outbox events and incoming receipts that were committed but never handled."""
    from . import incoming, services

    return {"events": services.dispatch_pending(), "receipts": incoming.process_pending()}
