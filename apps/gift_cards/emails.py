"""Gift card emails, built the same way as apps.notifications (store footer/from-header, queued send)."""
from django.conf import settings as django_settings

from apps.notifications import services as notifications

from .currencies import format_amount


def _never_or_date(card):
    return "Never expires" if not card.expires_at else f"Valid until {card.expires_at.date().isoformat()}"


def _balance_link(card):
    from .storefront_views import balance_token

    return f"{django_settings.FRONTEND_URL}/gift-card/balance?store={card.store.store_slug}&token={balance_token(card)}"


def gift_card_issued(card, raw_code, recipient_email, recipient_name=None, message=None, from_name=None):
    """The code, sent once, to whoever should receive the card (self or a recipient)."""
    store = card.store
    config = notifications.get_settings(store)
    amount = format_amount(card.currency_code, card.initial_balance)
    greeting = f"Hi {recipient_name}," if recipient_name else "Hi,"
    lines = [greeting]
    if from_name:
        lines.append(f"{from_name} sent you a {amount} gift card for {store.store_name}.")
    else:
        lines.append(f"Here is your {amount} gift card for {store.store_name}.")
    if message:
        lines.append(f'"{message}"')
    lines += [
        f"Gift card code: {raw_code}",
        _never_or_date(card),
        "Use this code at checkout to redeem it.",
        f"Check your balance any time: {_balance_link(card)}",
    ]
    return notifications.send(store, config, f"Your {store.store_name} gift card", lines, [recipient_email])


def gift_card_resent(card, raw_code, recipient_email):
    store = card.store
    config = notifications.get_settings(store)
    amount = format_amount(card.currency_code, card.current_balance)
    lines = [
        "Here is your gift card code again, as requested.",
        f"Gift card code: {raw_code}",
        f"Current balance: {amount}",
        _never_or_date(card),
        f"Check your balance any time: {_balance_link(card)}",
    ]
    return notifications.send(store, config, f"Your {store.store_name} gift card", lines, [recipient_email])
