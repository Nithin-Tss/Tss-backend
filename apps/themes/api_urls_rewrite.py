"""
Theme Liquid that called the storefront's JSON endpoints under /s/<store_slug>/... now calls the versioned
API (/api/v1/storefront/<store_slug>/..., via {{ shop.api_url }}). These are the exact rewrites, used by the
starter's files and by migration 0018 for the copies already saved in every store's theme.
"""

REWRITES = (
    # Visit beacon (layout/theme.liquid)
    ('"{{ shop.url }}/_track"', '"{{ shop.api_url }}/track/"'),
    # Cart drawer, cart page, product form and product card "add" buttons: the script appends /add, /change ...
    ('data-cart-base="{{ shop.url }}/cart"', 'data-cart-base="{{ shop.api_url }}/cart"'),
    # ... and the checkout link no longer derives the store's address from that base
    (
        'var base = ctx && ctx.base ? ctx.base.replace(/\\/cart\\/?$/, "") : "";',
        'var base = "{{ shop.url }}";',
    ),
    # Wishlist hearts and the newsletter form
    ('action="{{ shop.url }}/wishlist/items/"', 'action="{{ shop.api_url }}/wishlist/items/"'),
    ('action="{{ shop.url }}/newsletter"', 'action="{{ shop.api_url }}/newsletter/"'),
)


def rewrite(content):
    """`content` with the old endpoint addresses replaced; unchanged when it has none."""
    for old, new in REWRITES:
        content = content.replace(old, new)
    return content
