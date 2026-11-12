"""
The help assistant's tools. Every tool works on ctx.store / ctx.user, which come from the signed-in member's request
and never from the model's arguments, so no prompt can point a tool at another merchant.

  search_help_articles(query)   read-only, published articles only
  get_store_settings()          read-only, a WHITELIST of safe fields (see store_settings_summary)
  raise_support_ticket(...)     the only tool that writes, and all it can do is create (or return) a ticket
"""
from dataclasses import dataclass, field

from apps.ai_assistant.tools import ToolError, dispatch
from apps.tenancy.models import ShippingRate, ShippingZone, StoreDomain

from . import escalation, search
from .models import Ticket

ARTICLES_SHOWN = 3
ARTICLE_CHARS = 3500   # of each article's text sent to the model


@dataclass
class HelpContext:
    store: object
    user: object
    conversation: object
    changes: list = field(default_factory=list)   # [{"action": "raised_ticket", "ticket_number": ...}]
    sources: list = field(default_factory=list)   # articles the answer was based on, for the links under the reply
    ticket: object = None                         # set when a ticket was raised (or found) in this turn


def article_link(article):
    return f"/admin/help/articles/{article.slug}"


# ------------------------------------------------------------------ search_help_articles

def _text(args, key, limit, required=True):
    value = args.get(key)
    if value is None or value == "":
        if required:
            raise ToolError(f"'{key}' is required.")
        return ""
    if not isinstance(value, str):
        raise ToolError(f"'{key}' must be text.")
    if len(value) > limit:
        raise ToolError(f"'{key}' is too long (at most {limit} characters).")
    return value.strip()


def search_help_articles(ctx, args):
    query = _text(args, "query", search.MAX_QUERY_CHARS)
    articles = search.get_backend().search(query, limit=ARTICLES_SHOWN)
    for article in articles:
        if all(s["slug"] != article.slug for s in ctx.sources):
            ctx.sources.append({"title": article.title, "slug": article.slug, "url": article_link(article)})
    if not articles:
        return {"articles": [], "note": "No help article matches. Say so honestly and offer to raise a support ticket."}
    return {"articles": [
        {"title": a.title, "category": a.category, "link": article_link(a), "content": a.body[:ARTICLE_CHARS]}
        for a in articles
    ]}


# ------------------------------------------------------------------ get_store_settings

def store_settings_summary(store):
    """
    The store's settings the assistant may talk about. Built field by field from a whitelist: a field added to Store
    later is NOT visible until someone adds it here. Never included: payment keys / webhook secrets / account ids,
    delivery credentials, customers and orders, members' emails.
    """
    zones = [
        {"name": z.name, "rates": [
            {"name": r.name, "price": str(r.price)}
            for r in ShippingRate.objects.filter(store=store, zone=z).order_by("price")[:10]
        ]}
        for z in ShippingZone.objects.filter(store=store).order_by("name")[:20]
    ]
    return {
        "general": {
            "store_name": store.store_name, "currency": store.currency, "timezone": store.timezone,
            "unit_system": store.unit_system, "weight_unit": store.weight_unit,
            "order_number_prefix": store.order_prefix, "order_number_suffix": store.order_suffix,
        },
        "taxes": {
            "default_tax_rate_percent": str(store.tax_rate), "prices_include_tax": store.prices_include_tax,
            "tax_shipping": store.tax_shipping,
        },
        "shipping": {
            "flat_rate": str(store.shipping_flat_rate),
            "free_shipping_threshold": None if store.free_shipping_threshold is None else str(store.free_shipping_threshold),
            "zones": zones,
        },
        "payments": {
            # Whether payments are connected, never the keys or ids behind it
            "stripe_connected": bool(store.stripe_account_id or store.stripe_publishable_key),
            "stripe_can_take_payments": store.stripe_charges_enabled,
            "stripe_live_mode": store.stripe_livemode,
        },
        "checkout": {
            "customers_must_sign_in": store.checkout_require_login,
            "full_name_field": store.checkout_full_name, "company_field": store.checkout_company,
            "address_line_2_field": store.checkout_address2, "phone_field": store.checkout_phone,
        },
        "returns_and_cancellations": {
            "cancellations_enabled": store.cancellations_enabled,
            "cancellation_window_minutes": store.cancellation_window_minutes,
            "returns_enabled": store.returns_enabled, "return_window_days": store.return_window_days,
            "return_shipping": store.return_shipping,
        },
        "domains": [
            {"host": d.host, "kind": d.kind} for d in StoreDomain.objects.filter(store=store).order_by("created_at")[:20]
        ],
    }


def get_store_settings(ctx, args):
    return store_settings_summary(ctx.store)


# ------------------------------------------------------------------ raise_support_ticket

def raise_support_ticket(ctx, args):
    subject = _text(args, "subject", 300)
    summary = _text(args, "summary", escalation.MAX_SUMMARY * 2)
    category = _text(args, "category", 30, required=False) or "other"
    priority = _text(args, "priority", 20, required=False) or "normal"
    if category not in escalation.services.ALL_CATEGORIES:
        raise ToolError(f"'category' must be one of: {', '.join(escalation.services.ALL_CATEGORIES)}.")
    if priority not in escalation.services.ALL_PRIORITIES:
        raise ToolError(f"'priority' must be one of: {', '.join(escalation.services.ALL_PRIORITIES)}.")
    if ctx.conversation is None:
        raise ToolError("A ticket can only be raised from a chat.", "refused")
    ticket, created = escalation.create_or_get_ticket(
        store=ctx.store, user=ctx.user, conversation=ctx.conversation, source=Ticket.AI_ESCALATION,
        subject=subject, summary=summary, category=category, priority=priority,
    )
    ctx.ticket = ticket
    ctx.changes.append({
        "action": "raised_ticket" if created else "existing_ticket",
        "ticket_number": ticket.ticket_number, "summary": f"Ticket {ticket.ticket_number}: {ticket.subject}",
    })
    return {
        "ticket_number": ticket.ticket_number, "status": ticket.get_status_display(), "already_existed": not created,
        "tell_the_merchant": "Say that you have passed this chat to our team and that one of our executives will "
                             "contact them. Do not mention ticket numbers.",
    }


TOOLS = {
    "search_help_articles": search_help_articles,
    "get_store_settings": get_store_settings,
    "raise_support_ticket": raise_support_ticket,
}


def run_tool(ctx, name, raw_arguments):
    return dispatch(TOOLS, ctx, name, raw_arguments)


def tool_definitions():
    def fn(name, description, properties=None, required=()):
        return {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
        }}

    return [
        fn("search_help_articles", "Search the help articles. Returns the best matching articles with their text and link.",
           {"query": {"type": "string", "description": "What the merchant wants to know, in a few words."}}, ["query"]),
        fn("get_store_settings", "The merchant's own store settings (currency, tax, shipping, domains, checkout, "
           "whether payments are connected). Read-only; contains no secrets."),
        fn("raise_support_ticket",
           "Hand this chat to a human executive, who will contact the merchant. Use when the answer is not in the help articles, the issue needs "
           "a human (billing, account access, bugs, data problems), or the merchant is frustrated.",
           {
               "subject": {"type": "string", "description": "Short title of the problem."},
               "summary": {"type": "string", "description": "The problem and what was already tried or said."},
               "category": {"type": "string", "enum": list(escalation.services.ALL_CATEGORIES)},
               "priority": {"type": "string", "enum": list(escalation.services.ALL_PRIORITIES)},
           }, ["subject", "summary", "category"]),
    ]
