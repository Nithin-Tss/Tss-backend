"""
One chat turn with the page-builder assistant, shared by the DRF view (views.py) and the FastAPI app (fastapi_app.py).

Raises DRF's ValidationError / NotFound for bad input and llm.LLMError when the model can't be reached; each
front end turns those into its own HTTP response.
"""
import uuid

from rest_framework import exceptions

from . import agent, tools
from .models import Conversation, Message

MAX_MESSAGE_CHARS = 2000
HISTORY_MESSAGES = 12


def history(conversation):
    """The last turns as plain chat messages. Changes are noted in text so follow-ups can refer to the page ids."""
    rows = list(conversation.messages.order_by("-created_at")[:HISTORY_MESSAGES])[::-1]
    out = []
    for row in rows:
        content = row.content
        if row.changes:
            notes = "; ".join(f"{c['summary']} (page_id {c['page_id']})" for c in row.changes)
            content = f"{content}\n[Changes made: {notes}]"
        out.append({"role": row.role, "content": content})
    return out


def own_conversations(store, user):
    """Page-builder conversations of this person in this store: never another store's or another person's."""
    return Conversation.objects.filter(store=store, user=user, assistant_type=Conversation.PAGE_BUILDER)


def send_message(store, user, message, conversation_id=None):
    """Run one turn. Returns {"reply", "conversation_id", "changes"}."""
    if not isinstance(message, str) or not message.strip():
        raise exceptions.ValidationError({"message": "Type a message."})
    message = message.strip()
    if len(message) > MAX_MESSAGE_CHARS:
        raise exceptions.ValidationError({"message": f"Keep it under {MAX_MESSAGE_CHARS} characters."})

    if conversation_id:
        try:
            wanted = uuid.UUID(str(conversation_id))
        except ValueError:
            raise exceptions.ValidationError({"conversation_id": "Not a valid conversation id."})
        # Both store and user: another merchant's (or colleague's) conversation looks like it doesn't exist
        conversation = own_conversations(store, user).filter(pk=wanted).first()
        if conversation is None:
            raise exceptions.NotFound("That conversation doesn't exist.")
    else:
        conversation = Conversation.objects.create(store=store, user=user)

    ctx = tools.Context(store=store, user=user, conversation=conversation)
    reply = agent.run(ctx, history(conversation), message)  # may raise llm.LLMError

    Message.objects.create(conversation=conversation, role=Message.USER, content=message)
    Message.objects.create(conversation=conversation, role=Message.ASSISTANT, content=reply, changes=ctx.changes)
    conversation.save(update_fields=["updated_at"])
    return {"reply": reply, "conversation_id": str(conversation.pk), "changes": ctx.changes}
