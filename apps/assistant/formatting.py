"""Money and privacy formatting for what the model is shown."""
from decimal import ROUND_HALF_UP, Decimal

RUPEE = "₹"


def format_money(amount):
    """Indian grouping with the rupee sign and no currency code: 2400 -> "₹2,400", 1234.5 -> "₹1,234.50"."""
    value = Decimal(str(amount if amount is not None else 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    whole, _, cents = f"{abs(value):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return f"{sign}{RUPEE}{whole}" + ("" if cents == "00" else f".{cents}")


def percent_change(current, previous):
    """"+12.5%" / "-3.0%" / "0.0%", or "n/a" when there is nothing to compare with. Computed here, never by the model."""
    previous = Decimal(str(previous or 0))
    if previous == 0:
        return "n/a (nothing in the previous period)"
    change = ((Decimal(str(current or 0)) - previous) / previous * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{'+' if change > 0 else ''}{change}%"


def mask_name(name):
    """"Rita" -> "R***a", "Al" -> "A***l", "R" -> "R***"."""
    name = (name or "").strip()
    if not name:
        return ""
    if len(name) == 1:
        return f"{name}***"
    return f"{name[0]}***{name[-1]}"


def mask_email(email):
    """"rita@gmail.com" -> "r***@gmail.com"."""
    email = (email or "").strip()
    local, at, domain = email.partition("@")
    if not at or not local:
        return ""
    return f"{local[0]}***@{domain}"


def mask_customer(first_name, email):
    """What the model may know about a customer: a masked first name, else a masked email, else "Guest"."""
    return mask_name(first_name) or mask_email(email) or "Guest"
