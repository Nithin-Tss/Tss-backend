"""
The help assistant API, mounted at /api/v1/help-assistant/ (see urls.py). Any signed-in member of the store (no API
key) while the help_assistant feature flag is on for it (403 while off).

    POST conversations/<id>/escalate/   {message?}  "Talk to a human": makes a ticket WITHOUT relying on the model.
                                                    <id> may be "new" before the first message was sent.
    POST chat/                          {message, conversation_id?} -> {reply, conversation_id, sources, ticket, escalated}
    GET  conversations/<id>/            the chat so far and its ticket, to restore the widget
    GET  articles/<slug>/               one published help article
Rate limit: per store (HELP_ASSISTANT_THROTTLE_RATE). These requests are not audited (the chat can hold private text).
"""
import uuid

from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import exceptions
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from apps.ai_assistant import llm
from apps.ai_assistant.models import Conversation, Message
from apps.ai_assistant.views import NoApiKeys
from apps.core.permissions import IsStoreMember
from apps.platform_admin.permissions import feature_required

from . import escalation, help_agent, help_tools, services
from .models import HelpArticle, Ticket
from .views import NewTicketThrottle

FLAG = "help_assistant"
MAX_MESSAGE_CHARS = 2000
HISTORY_MESSAGES = 12
ESCALATED_NOTICE = "One of our executives will contact you."


class StoreThrottle(SimpleRateThrottle):
    """Per merchant (store): every chat message costs a paid model call."""

    scope = "help_assistant"

    def get_cache_key(self, request, view):
        store = getattr(request, "store", None)
        return self.cache_format % {"scope": self.scope, "ident": store.pk if store else self.get_ident(request)}


class HelpView(APIView):
    permission_classes = [IsAuthenticated, NoApiKeys, IsStoreMember, feature_required(FLAG)]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        request._request.audit = None


def own_conversation(request, raw_id):
    """The member's own help chat in this store; anyone else's looks like it doesn't exist."""
    try:
        wanted = uuid.UUID(str(raw_id))
    except ValueError:
        raise exceptions.ValidationError({"conversation_id": "Not a valid conversation id."}) from None
    conversation = Conversation.objects.filter(
        pk=wanted, store=request.store, user=request.user, assistant_type=Conversation.HELP,
    ).first()
    if conversation is None:
        raise exceptions.NotFound("That conversation doesn't exist.")
    return conversation


def ticket_summary(ticket):
    if ticket is None:
        return None
    return {
        "id": str(ticket.id), "ticketNumber": ticket.ticket_number, "subject": ticket.subject,
        "status": ticket.status,
    }


def _history(conversation):
    rows = list(conversation.messages.order_by("-created_at")[:HISTORY_MESSAGES])[::-1]
    return [{"role": row.role, "content": row.content} for row in rows]


def _clean_message(data):
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        raise exceptions.ValidationError({"message": "Type a message."})
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise exceptions.ValidationError({"message": f"Keep it under {MAX_MESSAGE_CHARS} characters."})
    return message


class ChatView(HelpView):
    throttle_classes = [StoreThrottle]

    def post(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        message = _clean_message(data)
        if data.get("conversation_id"):
            conversation = own_conversation(request, data["conversation_id"])
        else:
            conversation = Conversation.objects.create(
                store=request.store, user=request.user, assistant_type=Conversation.HELP,
            )

        if conversation.escalated:
            # A ticket exists: the AI stays quiet. Nothing is sent to the model or stored in the chat.
            return Response({
                "reply": ESCALATED_NOTICE, "conversation_id": str(conversation.pk), "sources": [],
                "ticket": ticket_summary(escalation.active_ticket(conversation) or conversation.tickets.first()),
                "escalated": True,
            })

        ctx = help_tools.HelpContext(store=request.store, user=request.user, conversation=conversation)
        try:
            reply = help_agent.run(ctx, _history(conversation), message)
        except llm.LLMError as error:
            return Response({"detail": str(error)}, status=503)

        if ctx.ticket and ESCALATED_NOTICE not in reply and "executive" not in reply.lower():
            # The merchant must always be told someone will contact them, whatever the model wrote
            reply = f"{reply}\n\n{ESCALATED_NOTICE}".strip()

        Message.objects.create(conversation=conversation, role=Message.USER, content=message)
        Message.objects.create(conversation=conversation, role=Message.ASSISTANT, content=reply, changes=ctx.changes)
        conversation.save(update_fields=["updated_at"])
        return Response({
            "reply": reply, "conversation_id": str(conversation.pk), "sources": ctx.sources,
            "ticket": ticket_summary(ctx.ticket), "escalated": ctx.ticket is not None,
        })


class EscalateView(HelpView):
    """The "Talk to a human" button."""

    throttle_classes = [NewTicketThrottle]

    def post(self, request, conversation_id):
        data = request.data if isinstance(request.data, dict) else {}
        note = data.get("message") if isinstance(data.get("message"), str) else ""
        note = note.strip()[:MAX_MESSAGE_CHARS]
        if conversation_id == "new":
            conversation = Conversation.objects.create(
                store=request.store, user=request.user, assistant_type=Conversation.HELP,
            )
        else:
            conversation = own_conversation(request, conversation_id)
        ticket, created = escalation.escalate_conversation(
            store=request.store, user=request.user, conversation=conversation, note=note,
        )
        return Response(
            {
                "conversation_id": str(conversation.pk), "created": created, "ticket": ticket_summary(ticket),
                "reply": ESCALATED_NOTICE, "escalated": True,
            },
            status=201 if created else 200,
        )


class ConversationListView(HelpView):
    """The member's earlier chats in this store, newest first: [{id, title, updatedAt, escalated}]."""

    def get(self, request):
        rows = Conversation.objects.filter(
            store=request.store, user=request.user, assistant_type=Conversation.HELP,
        ).order_by("-updated_at")[:50]
        items = []
        for c in rows:
            first = c.messages.filter(role=Message.USER).order_by("created_at").first()
            if first is None:
                continue  # an empty chat has nothing to show
            items.append({
                "id": str(c.pk), "title": " ".join(first.content.split())[:80],
                "updatedAt": c.updated_at, "escalated": c.escalated,
            })
        return Response({"conversations": items})


class ConversationView(HelpView):
    def get(self, request, conversation_id):
        conversation = own_conversation(request, conversation_id)
        ticket = conversation.tickets.order_by("-created_at").first()
        return Response({
            "conversation_id": str(conversation.pk), "escalated": conversation.escalated,
            "ticket": ticket_summary(ticket),
            "messages": [
                {"role": m.role, "content": m.content, "createdAt": m.created_at}
                for m in conversation.messages.order_by("created_at")[:200]
            ],
        })


class ArticleView(HelpView):
    def get(self, request, slug):
        article = get_object_or_404(HelpArticle, slug=slug, is_published=True)
        return Response({
            "slug": article.slug, "title": article.title, "category": article.category, "body": article.body,
            "updatedAt": article.updated_at,
        })
