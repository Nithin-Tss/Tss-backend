"""
Live updates for a ticket (Server-Sent Events). When a message is saved or the ticket changes, add_message and
staff_update call publish(); every open conversation of that ticket (the merchant's chat and the staff page) is told
at once and fetches the ticket again. The event carries no message text, only "something changed".

Same in-process bus as the theme editor sync (apps/themes/section_sync.EventBus): one worker process, many threads
(see gunicorn.conf.py). Several workers would need a shared broker such as Redis in its place.
"""
import json
import time

from django.db import connection, transaction
from django.http import StreamingHttpResponse
from rest_framework.renderers import BaseRenderer, JSONRenderer

from apps.themes.section_sync import EventBus

TICKET_EVENTS = EventBus()
STREAM_SECONDS = 55  # the client reconnects with ?after=<last id>
PING_SECONDS = 15


def publish(ticket):
    """Tell everyone watching this ticket. Sent after the database commit so a fetch sees the new data."""
    ticket_id = str(ticket.pk)
    transaction.on_commit(lambda: TICKET_EVENTS.publish(ticket_id, {"type": "ticket", "ticketId": ticket_id}))


class EventStreamRenderer(BaseRenderer):
    media_type = "text/event-stream"
    format = "event-stream"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        return json.dumps(data).encode("utf-8")


RENDERERS = [JSONRenderer, EventStreamRenderer]


def stream_response(ticket_id, after, seconds=STREAM_SECONDS):
    """The open event stream for one ticket (the caller has already checked who may watch it)."""
    ticket_id = str(ticket_id)
    if after is None:
        after = TICKET_EVENTS.last_seq
    if not connection.in_atomic_block:
        connection.close()  # the stream holds no database connection while it waits

    def stream():
        last = after
        yield f"retry: 1000\nevent: hello\ndata: {json.dumps({'seq': last})}\n\n"
        ends = time.monotonic() + seconds
        while time.monotonic() < ends:
            events, last = TICKET_EVENTS.wait(ticket_id, last, min(PING_SECONDS, max(ends - time.monotonic(), 0.1)))
            if not events:
                yield ": ping\n\n"
                continue
            yield f"id: {last}\nevent: ticket\ndata: {json.dumps(events[-1][1])}\n\n"

    response = StreamingHttpResponse(stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def after_param(request):
    try:
        return int(request.query_params.get("after"))
    except (TypeError, ValueError):
        return None
