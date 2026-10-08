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

from .schemas import SECTIONS, resolve
from .services import get_store_theme, theme_files


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


def _error_box(where, error):
    return Markup(
        '<div style="margin:16px;padding:12px 16px;border:1px solid #e5484d;border-radius:8px;'
        'background:#fff5f5;color:#b42318;font:14px/1.4 monospace">'
        f"Liquid error in {escape(where)}: {escape(str(error))}</div>"
    )


def render_page(store, page, product=None, collection=None):
    """
    page: "index", "product" or "collection".
    product: a catalog Product (product page).
    collection: {"name", "obj"} where obj is a Collection, or None for "all".
    """
    store_theme = get_store_theme(store)
    files = theme_files(store_theme)
    data = resolve(store_theme.custom_data)
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
    html = []

    for section_id in template_data["order"]:
        section = dict(template_data["sections"][section_id], id=section_id)
        path = f"sections/{section['type']}.liquid"

        if path not in files:
            html.append(Markup(f"<!-- {escape(path)} is missing -->"))
            continue

        if SECTIONS[section["type"]].get("uses_products"):
            section["products"] = _products(
                store,
                base_url,
                collection=_find_collection(store, section["settings"].get("collection")),
                limit=section["settings"].get("limit"),
                exclude=product.pk if product else None,
            )

        try:
            html.append(Markup(env.get_template(path).render(section=section, **common)))
        except LiquidError as error:
            html.append(_error_box(path, error))

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
