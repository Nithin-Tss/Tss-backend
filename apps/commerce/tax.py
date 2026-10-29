"""
Tax rules, shared by checkout, manual orders and drafts.

The rate comes from the most specific TaxRate for the destination (country +
region, then country, then anywhere) and falls back to Store.tax_rate. Tax is
worked out on each line after its share of the order discount, so a line can be
tax-exempt (Product.is_taxable) without disturbing the others, and the cents
always add up: the total is rounded once and then handed out to the lines.

Store.prices_include_tax says whether listed prices already contain the tax. If
so the tax is the part of the price that is tax, and it is not added again.
"""
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
CENT = Decimal("0.01")
HUNDRED = Decimal(100)


@dataclass
class TaxResult:
    rate: Decimal = ZERO
    total: Decimal = ZERO
    line_taxes: list = field(default_factory=list)
    shipping_tax: Decimal = ZERO


def _norm(value):
    return (value or "").strip().lower()


def rate_for(store, country=None, region=None):
    """The tax percentage for a destination."""
    country, region = _norm(country), _norm(region)
    best = None
    for rule in store.tax_rates.all():
        rule_country, rule_region = _norm(rule.country), _norm(rule.region)
        if rule_country and rule_country != country:
            continue
        if rule_region and (not rule_country or rule_region != region):
            continue
        key = (bool(rule_country), bool(rule_region))
        if best is None or key > best[0]:
            best = (key, rule)
    return Decimal(best[1].rate if best else store.tax_rate or 0)


def allocate(amount, weights):
    """
    Split a money amount across weights in whole cents (largest remainder), so
    the parts add up exactly to the amount. All-zero weights get nothing.
    """
    total_weight = sum(weights, ZERO)
    if amount <= ZERO or total_weight <= ZERO:
        return [ZERO] * len(weights)
    return _spread(amount, [Decimal(w) * amount / total_weight for w in weights])


def _spread(total, raw):
    """Round `raw` shares to cents so that they sum to `total` (already a cent amount)."""
    floors = [(r * 100).to_integral_value(ROUND_FLOOR) for r in raw]
    leftover = int(total * 100 - sum(floors))
    order = sorted(range(len(raw)), key=lambda i: raw[i] * 100 - floors[i], reverse=True)
    for i in order[:max(leftover, 0)]:
        floors[i] += 1
    return [(f / 100).quantize(CENT) for f in floors]


def calculate(store, lines, discount=ZERO, shipping=ZERO, country=None, region=None, exempt=False):
    """
    lines: [{"amount": line total after any line discount, "taxable": bool}]
    discount: order-level discount, shared out over the lines by their amounts.
    Returns a TaxResult: the order total and the tax of each line (and shipping).
    """
    rate = ZERO if exempt else rate_for(store, country, region)
    amounts = [max(Decimal(l["amount"]), ZERO) for l in lines]
    shares = allocate(min(max(Decimal(discount or 0), ZERO), sum(amounts, ZERO)), amounts)
    bases = [a - s if l.get("taxable", True) else ZERO for a, s, l in zip(amounts, shares, lines)]
    bases.append(max(Decimal(shipping or 0), ZERO) if store.tax_shipping else ZERO)

    if rate <= ZERO:
        return TaxResult(rate=rate, line_taxes=[ZERO] * len(lines))

    factor = rate / (HUNDRED + rate) if store.prices_include_tax else rate / HUNDRED
    raw = [b * factor for b in bases]
    total = sum(raw, ZERO).quantize(CENT, ROUND_HALF_UP)
    parts = _spread(total, raw)
    return TaxResult(rate=rate, total=total, line_taxes=parts[:-1], shipping_tax=parts[-1])


def grand_total(store, subtotal, discount, tax, shipping):
    """What the shopper pays. Tax is already inside the prices when the store lists them tax-inclusive."""
    added_tax = ZERO if store.prices_include_tax else tax
    return max(subtotal - discount + added_tax + shipping, ZERO)
