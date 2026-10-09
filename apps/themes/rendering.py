"""
Renders a store's theme to HTML for the public storefront.

Page = layout/theme.liquid
         └ templates/<page>.liquid
             └ sections/<type>.liquid for each section, in the store's order
"""
import uuid
from decimal import Decimal

from liquid import DictLoader, Environment
from liquid.exceptions import LiquidError
from markupsafe import Markup, escape

from apps.catalog.models import Collection
from apps.catalog.services import published_products

from .schemas import SECTIONS, resolve, sanitize_embed, sanitize_html
from .services import get_store_theme, theme_files

# Shown on the home page when every section has been removed or hidden
FALLBACK_INDEX = [
    ("fallback-hero", {"type": "hero", "settings": {"height": "small"}}),
    ("fallback-products", {"type": "product-grid", "settings": {}}),
]

# Styles for custom sections, added to the page once when one is shown
CUSTOM_CSS = Markup("""<style>
.tss-custom .tss-blocks{display:flex;flex-direction:column;gap:20px}
.tss-align-center{text-align:center}.tss-align-right{text-align:right}
.tss-align-center .tss-media{margin-left:auto;margin-right:auto}
.tss-align-right .tss-media{margin-left:auto}
.tss-block-heading{margin:0;line-height:1.2}
.tss-text>*:first-child{margin-top:0}.tss-text>*:last-child{margin-bottom:0}
.tss-text ul,.tss-text ol{padding-left:1.25em;display:inline-block;text-align:left}
.tss-image img{display:block;max-width:100%;height:auto;border-radius:12px}
.tss-button--outline{background:transparent!important;color:inherit!important;border:1px solid currentColor}
.tss-collections{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;text-align:left}
.tss-collection{display:flex;align-items:flex-end;min-height:120px;padding:16px;border-radius:12px;background:#F1F3F7;color:inherit;text-decoration:none;font-weight:600}
.tss-collection:hover{background:#E7EBF2}
.tss-embed{max-width:100%;overflow-x:auto}.tss-embed iframe{max-width:100%;border:0}.tss-embed img{max-width:100%;height:auto}
.tss-empty{padding:24px;border:1px dashed #D8DFE8;border-radius:12px;color:#53627E;text-align:center}
</style>""")


def shop_url(store):
    return f"/s/{store.store_slug}"


def _environment(files, base_url, currency):
    loader = {}
    for path, content in files.items():
        loader[path] = content
        # Snippets can be rendered by name: {% render 'product-card' %}
        if path.startswith("snippets/") and path.endswith(".liquid"):
            loader[path[len("snippets/"):-len(".liquid")]] = content

    env = Environment(loader=DictLoader(loader), autoescape=True)

    def money(value):
        if value is None or value == "":
            return ""
        return f"{currency}{Decimal(str(value)):,.2f}"

    def link(value):
        # Store-relative links ("/collections/all") point inside this store.
        value = str(value or "")
        return base_url + value if value.startswith("/") and not value.startswith("//") else value

    env.filters["money"] = money
    env.filters["link"] = link
    return env


def product_drop(product, base_url):
    images = [i.url for i in sorted(product.images.all(), key=lambda i: i.position)]
    variants = sorted(product.variants.all(), key=lambda v: v.created_at)
    prices = [v.price for v in variants]

    return {
        "id": str(product.id),
        "title": product.title,
        "url": f"{base_url}/products/{product.id}",
        "vendor": product.vendor or "",
        "product_type": product.product_type or "",
        # Written by the merchant in the admin rich-text editor.
        "description": Markup(product.description or ""),
        "price": min(prices) if prices else None,
        "price_varies": len(set(prices)) > 1,
        "images": images,
        "featured_image": images[0] if images else None,
        "variants": [
            {"id": str(v.id), "sku": v.sku or "", "price": v.price}
            for v in variants
        ],
    }


def _find_collection(store, value):
    value = (value or "").strip()
    if not value:
        return None

    try:
        return Collection.objects.filter(store=store, id=uuid.UUID(value)).first()
    except ValueError:
        return Collection.objects.filter(store=store, name__iexact=value).first()


def _products(store, base_url, collection=None, limit=None, exclude=None):
    qs = published_products(store)

    if collection is not None:
        qs = qs.filter(product_collections__collection=collection)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    if limit:
        qs = qs[:limit]

    return [product_drop(p, base_url) for p in qs]


def _chosen_products(store, base_url, ids, limit=None):
    """The products the owner picked, in the order they picked them (published ones only)."""
    by_id = {str(p.id): p for p in published_products(store).filter(pk__in=ids)}
    picked = [by_id[i] for i in ids if i in by_id]
    return [product_drop(p, base_url) for p in picked[:limit]]


def _prepare(section_type, settings):
    """Settings as the section template sees them: rich text becomes (cleaned) HTML."""
    schema = SECTIONS[section_type]["settings"]
    return {
        key: Markup(sanitize_html(value)) if schema.get(key, {}).get("type") == "richtext" else value
        for key, value in settings.items()
    }


def _render_custom(section, store, env, base_url):
    """A custom section: the owner's name as a heading, then their blocks in order."""
    s = section["settings"]
    link = env.filters["link"]
    parts = []

    for block in section.get("blocks", []):
        b = block["settings"]
        kind = block["type"]

        if kind == "heading" and b.get("text"):
            tag = {"large": "h2", "medium": "h3", "small": "h4"}.get(b.get("size"), "h3")
            parts.append(f'<{tag} class="tss-block-heading">{escape(b["text"])}</{tag}>')

        elif kind == "text" and b.get("body"):
            parts.append(f'<div class="tss-text">{sanitize_html(b["body"])}</div>')

        elif kind == "image" and b.get("image_url"):
            img = f'<img src="{escape(b["image_url"])}" alt="{escape(b.get("alt") or "")}" loading="lazy">'
            if b.get("link"):
                img = f'<a href="{escape(link(b["link"]))}">{img}</a>'
            parts.append(f'<div class="tss-image tss-media">{img}</div>')

        elif kind == "button" and b.get("label"):
            style = " tss-button--outline" if b.get("style") == "outline" else ""
            parts.append(
                f'<div><a class="button{style}" href="{escape(link(b.get("link") or ""))}">{escape(b["label"])}</a></div>'
            )

        elif kind == "products":
            products = _chosen_products(store, base_url, b.get("products") or [])
            if not products:
                parts.append('<div class="tss-empty">Choose products to show here.</div>')
                continue
            try:
                cards = env.from_string(
                    "{% for product in products %}{% render 'product-card', product: product, show_vendor: false %}{% endfor %}"
                ).render(products=products)
            except LiquidError:
                cards = "".join(f'<a href="{escape(p["url"])}">{escape(p["title"])}</a>' for p in products)
            parts.append(
                f'<div class="product-grid product-grid--{int(b.get("columns") or 4)}" style="text-align:left">{cards}</div>'
            )

        elif kind == "collections":
            ids = b.get("collections") or []
            by_id = {str(c.id): c for c in Collection.objects.filter(store=store, id__in=ids)}
            tiles = "".join(
                f'<a class="tss-collection" href="{escape(base_url)}/collections/{c.id}">{escape(c.name)}</a>'
                for c in (by_id.get(i) for i in ids) if c
            )
            parts.append(
                f'<div class="tss-collections">{tiles}</div>' if tiles else '<div class="tss-empty">Choose collections to show here.</div>'
            )

        elif kind == "html" and b.get("html"):
            parts.append(f'<div class="tss-embed">{sanitize_embed(b["html"])}</div>')

    heading = (
        f'<h2 class="section-heading">{escape(s["heading"])}</h2>' if s.get("show_heading") and s.get("heading") else ""
    )
    body = f'<div class="tss-blocks">{"".join(parts)}</div>' if parts else ""
    if not heading and not body:
        return Markup("")

    align = escape(s.get("text_alignment") or "left")
    return Markup(
        f'<section id="section-{escape(section["id"])}" class="section tss-custom tss-align-{align}">'
        f'<div class="page-width">{heading}{body}</div></section>'
    )


def _error_box(where, error):
    return Markup(
        '<div style="margin:16px;padding:12px 16px;border:1px solid #e5484d;border-radius:8px;'
        'background:#fff5f5;color:#b42318;font:14px/1.4 monospace">'
        f"Liquid error in {escape(where)}: {escape(str(error))}</div>"
    )


def render_page(store, page, product=None, collection=None, custom_data=None, editor=False):
    """
    page: "index", "product" or "collection".
    product: a catalog Product (product page).
    collection: {"name", "obj"} where obj is a Collection, or None for "all".
    custom_data: unsaved section data to render instead of the saved one (customizer preview).
    editor: wrap each section in a marker the customizer preview uses for drag and drop.
    """
    store_theme = get_store_theme(store)
    files = theme_files(store_theme)
    data = resolve(store_theme.custom_data if custom_data is None else custom_data)
    settings = data["settings"]
    base_url = shop_url(store)
    env = _environment(files, base_url, settings["currency_symbol"])

    shop = {"name": store.store_name, "url": base_url}
    nav = [
        {"name": c.name, "url": f"{base_url}/collections/{c.id}"}
        for c in Collection.objects.filter(store=store).order_by("name")[:20]
    ]
    product_drop_ = product_drop(product, base_url) if product else None
    collection_drop = None

    if page == "collection":
        collection_drop = {
            "name": collection["name"],
            "url": f"{base_url}/collections/{collection['obj'].id if collection['obj'] else 'all'}",
            "products": _products(store, base_url, collection=collection["obj"], limit=200),
        }

    common = {
        "shop": shop,
        "settings": settings,
        "product": product_drop_,
        "collection": collection_drop,
        "collections": nav,
        "template": page,
    }

    template_data = data["templates"][page]
    visible = [
        (section_id, template_data["sections"][section_id])
        for section_id in template_data["order"]
        if not template_data["sections"][section_id].get("hidden")
    ]
    if page == "index" and not visible:
        fallback = resolve({"templates": {"index": {
            "order": [i for i, _ in FALLBACK_INDEX], "sections": dict(FALLBACK_INDEX),
        }}})
        visible = list(fallback["templates"]["index"]["sections"].items())

    real_ids = set(template_data["sections"])
    html = []

    if any(raw.get("type") == "custom" for _, raw in visible):
        html.append(CUSTOM_CSS)

    for section_id, raw_section in visible:
        section = dict(raw_section, id=section_id)
        section["settings"] = _prepare(section["type"], section["settings"])
        path = f"sections/{section['type']}.liquid"
        wrap = editor and section_id in real_ids

        if wrap:
            html.append(Markup(f'<div data-tss-section="{escape(section_id)}">'))

        if section["type"] == "custom":
            html.append(_render_custom(section, store, env, base_url))

        elif path not in files:
            html.append(Markup(f"<!-- {escape(path)} is missing -->"))

        else:
            if SECTIONS[section["type"]].get("uses_products"):
                chosen = section["settings"].get("products") or []
                section["products"] = (
                    _chosen_products(store, base_url, chosen, section["settings"].get("limit"))
                    if chosen
                    else _products(
                        store,
                        base_url,
                        collection=_find_collection(store, section["settings"].get("collection")),
                        limit=section["settings"].get("limit"),
                        exclude=product.pk if product else None,
                    )
                )

            # The slideshow template has no heading of its own; show the section heading above it
            if section["type"] == "slideshow" and section["settings"].get("heading"):
                html.append(Markup(
                    '<div class="page-width" style="padding-top:32px">'
                    f'<h2 class="section-heading">{escape(section["settings"]["heading"])}</h2></div>'
                ))

            try:
                html.append(Markup(env.get_template(path).render(section=section, **common)))
            except LiquidError as error:
                html.append(_error_box(path, error))

        if wrap:
            html.append(Markup("</div>"))

    content = env.get_template(f"templates/{page}.liquid").render(
        sections=Markup("\n".join(html)),
        **common,
    )

    if product:
        title = product.seo_title or product.title
    elif collection_drop:
        title = collection_drop["name"]
    else:
        title = store.store_name

    return env.get_template("layout/theme.liquid").render(
        content_for_layout=Markup(content),
        page_title=title,
        **common,
    )
