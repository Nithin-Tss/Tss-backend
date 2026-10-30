"""Storefront sitemap and robots.txt views."""
import xml.etree.ElementTree as ET

from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404

from apps.catalog.services import published_collections, published_products
from apps.tenancy.models import Store
from apps.themes.rendering import published_pages, published_posts, shop_url


def _format_lastmod(dt):
    if not dt:
        return None
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(dt)


def storefront_sitemap_view(request, store_slug):
    """
    Dynamic sitemap.xml for a store's public storefront.

    Includes:
    - Homepage
    - All Products collection
    - Active & online-published collections
    - Active & online-published products
    - Visible store pages
    - Published blog articles (not scheduled in the future)
    """
    store = get_object_or_404(Store, store_slug=store_slug, status="active")
    base_url = shop_url(store)

    urlset = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

    def add_url(loc, lastmod=None, changefreq=None, priority=None):
        url_el = ET.SubElement(urlset, "url")
        ET.SubElement(url_el, "loc").text = loc
        formatted_lastmod = _format_lastmod(lastmod)
        if formatted_lastmod:
            ET.SubElement(url_el, "lastmod").text = formatted_lastmod
        if changefreq:
            ET.SubElement(url_el, "changefreq").text = changefreq
        if priority is not None:
            ET.SubElement(url_el, "priority").text = str(priority)

    # 1. Storefront homepage
    add_url(
        request.build_absolute_uri(f"{base_url}/"),
        lastmod=store.updated_at,
        changefreq="daily",
        priority="1.0",
    )

    # 2. All Products collection
    add_url(
        request.build_absolute_uri(f"{base_url}/collections/all"),
        changefreq="daily",
        priority="0.8",
    )

    # 3. Active & online-published collections
    for handle, col_id, col_updated in published_collections(store).values_list("handle", "id", "updated_at"):
        col_slug = handle or str(col_id)
        add_url(
            request.build_absolute_uri(f"{base_url}/collections/{col_slug}"),
            lastmod=col_updated,
            changefreq="weekly",
            priority="0.8",
        )

    # 4. Active & online-published products
    for prod_id, prod_updated in published_products(store).values_list("id", "updated_at"):
        add_url(
            request.build_absolute_uri(f"{base_url}/products/{prod_id}"),
            lastmod=prod_updated,
            changefreq="daily",
            priority="0.9",
        )

    # 5. Visible custom store pages
    for page_handle, page_updated in published_pages(store).values_list("handle", "updated_at"):
        add_url(
            request.build_absolute_uri(f"{base_url}/pages/{page_handle}"),
            lastmod=page_updated,
            changefreq="weekly",
            priority="0.6",
        )

    # 6. Published blog posts
    posts = list(published_posts(store).values_list("handle", "updated_at"))
    if posts:
        # Blog listing page
        add_url(
            request.build_absolute_uri(f"{base_url}/blog"),
            changefreq="daily",
            priority="0.7",
        )
        # Individual articles
        for post_handle, post_updated in posts:
            add_url(
                request.build_absolute_uri(f"{base_url}/blog/{post_handle}"),
                lastmod=post_updated,
                changefreq="weekly",
                priority="0.7",
            )

    xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(urlset, encoding="unicode")
    return HttpResponse(xml_content, content_type="application/xml; charset=utf-8")


def storefront_robots_txt_view(request, store_slug):
    """
    Dynamic robots.txt for a store's public storefront.

    Directs search engine crawlers to the store's sitemap.xml and
    disallows internal/transactional shopper routes (cart, checkout, account, wishlist).
    """
    store = get_object_or_404(Store, store_slug=store_slug, status="active")
    base_url = shop_url(store)
    sitemap_url = request.build_absolute_uri(f"{base_url}/sitemap.xml")

    lines = [
        "User-agent: *",
        f"Disallow: {base_url}/cart/",
        f"Disallow: {base_url}/cart",
        f"Disallow: {base_url}/checkout/",
        f"Disallow: {base_url}/checkout",
        f"Disallow: {base_url}/account/",
        f"Disallow: {base_url}/account",
        f"Disallow: {base_url}/wishlist/",
        f"Disallow: {base_url}/wishlist",
        "",
        f"Sitemap: {sitemap_url}",
        "",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")

