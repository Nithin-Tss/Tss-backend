"""
Renders a store's theme to HTML for the public storefront.

Page = layout/theme.liquid
         └ templates/<page>.liquid
             └ sections/<type>.liquid for each section, in the store's order
"""
import hashlib
import json
import re
import uuid
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

from liquid import DictLoader, Environment
from liquid.exceptions import LiquidError
from markupsafe import Markup, escape

from django.db.models import Avg, Count, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.text import slugify

from apps.catalog.models import EXTRA_FIELDS
from apps.catalog.services import published_collections, published_products
from apps.orders.models import OrderItem

from .schemas import ALL_PRODUCTS_SETTINGS, FONTS, GENERIC_BLOCK_TYPES, SECTIONS, resolve, sanitize_embed, sanitize_html
from . import section_sync as sync
from .models import BlogPost, ThemeFile
from .services import get_store_theme, synced_section, theme_files

# Shown on the home page when every section has been removed or hidden
FALLBACK_INDEX = [
    ("fallback-hero", {"type": "hero", "settings": {"height": "small"}}),
    ("fallback-products", {"type": "product-grid", "settings": ALL_PRODUCTS_SETTINGS}),
]

# Reviews that count towards a product's rating
SHOWN_REVIEWS = ("approved", "published")

# Styles for the customizer's section options (spacing, width, colours, ...)
# and the newer section features. Added to every page once, so they also work
# for themes whose own CSS file was edited.
SECTION_CSS = Markup("""<script>document.documentElement.classList.add("tss-js")</script>
<style>
.tss-wrap{padding-top:var(--tss-pt,0);padding-bottom:var(--tss-pb,0);background:var(--tss-bg,transparent);color:var(--tss-color,inherit);border-radius:var(--tss-radius,0);overflow:clip}
.tss-wrap .section{padding-top:0;padding-bottom:0}
.tss-wrap .page-width{padding-left:var(--tss-px,20px);padding-right:var(--tss-px,20px)}
.tss-w-narrow .page-width{max-width:800px}.tss-w-full .page-width{max-width:none}
.tss-align-left{text-align:left}.tss-align-center{text-align:center}.tss-align-right{text-align:right}
.tss-div-top,.tss-div-both{border-top:1px solid var(--line,rgba(0,0,0,.12))}
.tss-div-bottom,.tss-div-both{border-bottom:1px solid var(--line,rgba(0,0,0,.12))}
.tss-hs-small .section-heading{font-size:clamp(18px,2vw,22px)}.tss-hs-small .hero :is(h1,h2.hero__title){font-size:clamp(26px,3.5vw,38px)}
.tss-hs-large .section-heading{font-size:clamp(28px,4vw,42px)}.tss-hs-large .hero :is(h1,h2.hero__title){font-size:clamp(40px,6.5vw,76px)}
.tss-hw-regular :is(.section-heading,h1,h2,h3){font-weight:400}.tss-hw-medium :is(.section-heading,h1,h2,h3){font-weight:500}
.tss-hw-semibold :is(.section-heading,h1,h2,h3){font-weight:600}.tss-hw-bold :is(.section-heading,h1,h2,h3){font-weight:700}
@media (max-width:749px){.tss-vis-desktop{display:none!important}}
@media (min-width:750px){.tss-vis-mobile{display:none!important}}
.tss-js .tss-anim-fade{opacity:0;transition:opacity .7s ease}
.tss-js .tss-anim-slide-up{opacity:0;transform:translateY(28px);transition:opacity .7s ease,transform .7s ease}
.tss-js .tss-in{opacity:1;transform:none}
@media (prefers-reduced-motion:reduce){.tss-js .tss-anim-fade,.tss-js .tss-anim-slide-up{opacity:1;transform:none;transition:none}}

/* Hero */
.hero__media{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
.hero--full{min-height:100vh}.hero--v-top{align-items:flex-start}.hero--v-bottom{align-items:flex-end}
.hero--parallax{background-size:cover;background-attachment:fixed}
.hero__inner h1,.hero__inner p{max-width:none}
.hero h2.hero__title{font-size:clamp(32px,5vw,56px);max-width:720px}
.hero__label{font-size:13px!important;text-transform:uppercase;letter-spacing:.12em;margin:0 0 12px!important;opacity:.85}
.hero__buttons{display:flex;flex-wrap:wrap;gap:12px}
.hero__buttons{gap:var(--b-gap,12px)}
.align-center .hero__buttons{justify-content:center}.align-right .hero__buttons{justify-content:flex-end}
.tss-ba-left .hero__buttons{justify-content:flex-start}.tss-ba-center .hero__buttons{justify-content:center}.tss-ba-right .hero__buttons{justify-content:flex-end}
.hero .button.button--outline{background:transparent;color:inherit;border-color:currentColor}

/* Slideshow */
.slideshow--fade .slideshow__track{display:grid;transform:none!important}
.slideshow--fade .slideshow__slide{grid-area:1/1;opacity:0;visibility:hidden;transition:opacity .6s ease,visibility .6s}
.slideshow--fade .slideshow__slide.is-active{opacity:1;visibility:visible}

/* Product lists */
.product-list__sub{color:var(--muted);margin:-12px 0 24px}
.product-list__actions{display:flex;align-items:center;gap:16px;margin-bottom:24px}
.product-list__actions .carousel__controls{margin-bottom:0}
.product-list__view-all{font-weight:600;text-decoration:none;white-space:nowrap}
.product-list__grid{display:grid;gap:var(--gap,24px);grid-template-columns:repeat(var(--cols-mobile,2),minmax(0,1fr))}
.product-list__track{gap:var(--gap,24px);grid-auto-columns:calc((100% - (var(--cols-mobile,2) - 1) * var(--gap,24px)) / var(--cols-mobile,2))}
@media (min-width:900px){
  .product-list__grid{grid-template-columns:repeat(var(--cols,4),minmax(0,1fr))}
  .product-list__track{grid-auto-columns:calc((100% - (var(--cols,4) - 1) * var(--gap,24px)) / var(--cols,4))}
}
.product-card__media--portrait{aspect-ratio:3/4}.product-card__media--landscape{aspect-ratio:4/3}.product-card__media--square{aspect-ratio:1}
.card-hover--none .product-card:hover .product-card__media img{transform:none}
.card-hover--lift .product-card{transition:transform .2s ease,box-shadow .2s ease}
.card-hover--lift .product-card:hover{transform:translateY(-4px)}.card-hover--lift .product-card:hover .product-card__media img{transform:none}
.product-list[style*="--card-bg"] .product-card,.cards--bordered .product-card{padding:10px;border-radius:var(--radius,14px)}
.product-list[style*="--card-bg"] .product-card{background:var(--card-bg)}
.cards--bordered .product-card{border:1px solid var(--line,rgba(0,0,0,.12))}
.product-card__rating{font-size:13px;color:#B7791F}.product-card__rating span{color:var(--muted)}
.product-card__media{position:relative}
.product-card__badge{position:absolute;top:10px;left:10px;z-index:1;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;background:#141b2d;color:#fff}
.product-card--sold-out .product-card__media img,.product-card--sold-out .product-card__media .placeholder{opacity:.6}
.product__stock{font-size:14px;font-weight:600;margin:-12px 0 16px}
.product__stock--few{color:#B45309}.product__stock--out{color:var(--muted)}
.product-list__more{display:flex;justify-content:center;margin-top:28px}
.pagination{display:flex;align-items:center;justify-content:center;gap:16px;margin-top:32px}
.collection-toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px}
.product-card__tags{display:flex;flex-wrap:wrap;gap:6px}
.product-card__tags span{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--soft);color:var(--muted)}
.product-card__cta{min-height:38px;padding:0 16px;font-size:14px;align-self:flex-start}

/* Rich text */
.rich-text--narrow{max-width:560px}.rich-text--medium{max-width:780px}.rich-text--wide{max-width:none}
.rich-text.align-center{margin-left:auto;margin-right:auto}.rich-text.align-right{margin-left:auto}
.rich-text__body{color:var(--muted)}
.rich-text__body p{white-space:normal;font-size:inherit;color:inherit;margin:0 0 1em}
.rich-text__body ul,.rich-text__body ol{display:inline-block;text-align:left;padding-left:1.25em}
.rich-text--size-small .rich-text__body{font-size:15px}.rich-text--size-medium .rich-text__body{font-size:18px}.rich-text--size-large .rich-text__body{font-size:21px}
.rich-text--lh-tight .rich-text__body{line-height:1.35}.rich-text--lh-normal .rich-text__body{line-height:1.6}.rich-text--lh-relaxed .rich-text__body{line-height:1.9}
.rich-text__body blockquote{margin:1em 0}
.rich-text--quote-bordered blockquote{border-left:3px solid var(--accent);padding-left:16px;text-align:left}
.rich-text--quote-highlighted blockquote{background:var(--soft);padding:16px 20px;border-radius:12px;font-style:italic}
@media (min-width:750px){.rich-text__body--2col{columns:2;column-gap:40px;text-align:left}}
.rich-text--image{display:grid;gap:32px;align-items:center;max-width:none}
.rich-text__image img{display:block;width:100%;height:auto;border-radius:var(--radius,14px);object-fit:var(--img-fit,cover);object-position:var(--img-focus,center)}
.rich-text__image--square img{aspect-ratio:1/1;height:100%}.rich-text__image--portrait img{aspect-ratio:4/5;height:100%}
.rich-text__image--landscape img{aspect-ratio:3/2;height:100%}.rich-text__image--wide img{aspect-ratio:21/9;height:100%}
.rich-text__image--contain{background:var(--soft)}
@media (min-width:750px){.rich-text--image-left,.rich-text--image-right{grid-template-columns:1fr 1fr}.rich-text--image-right .rich-text__image{order:2}}
.rich-text__button{margin-top:8px}
</style>""")

FONT_BY_KEY = {key: (stack, family, weights) for key, _, stack, family, weights in FONTS}

# Buttons and text: one set of rules driven by CSS variables. The theme
# defaults set the variables on :root; a section or block that overrides
# anything sets the full set again on its own wrapper (class tss-btn).
TYPE_CSS = Markup("""<style>
body{font-family:var(--tss-body-font);font-size:var(--tss-base-size,16px)}
h1,h2,h3,h4,h5,.section-heading,.site-header__logo{font-family:var(--tss-heading-font)}
.tss-wrap[data-hfont] :is(h1,h2,h3,h4,h5,.section-heading){font-family:var(--tss-section-hfont)}
.button,.tss-btn .button{background:var(--b-bg);color:var(--b-tx);border:var(--b-bw) solid var(--b-bd);border-radius:var(--b-r);
  min-height:var(--b-mh);padding:0 var(--b-px);text-decoration:var(--b-dec);width:var(--b-w,auto);font-family:var(--b-ff);
  font-size:var(--b-fs);font-weight:var(--b-fw);text-transform:var(--b-tt);box-shadow:var(--b-sh);
  transition:background var(--b-sp) ease,color var(--b-sp) ease,border-color var(--b-sp) ease,transform var(--b-sp) ease,filter var(--b-sp) ease}
.button:hover,.tss-btn .button:hover{background:var(--b-hbg);color:var(--b-htx);border-color:var(--b-hbd);transform:var(--b-ht);filter:var(--b-hf);opacity:1}
.button.button--outline{background:transparent;color:var(--b-bd);border-color:var(--b-bd)}
.hero--image .button.button--outline,.hero .button.button--outline{background:transparent;color:inherit;border-color:currentColor}
.tss-bi .button{gap:.5em}
.tss-bi-right .button::after,.tss-bi-left .button::before{content:"";width:1.1em;height:1.1em;flex:none;background:currentColor;
  -webkit-mask:var(--b-icon) center/contain no-repeat;mask:var(--b-icon) center/contain no-repeat}
.tss-block.tss-ba-left{text-align:left}.tss-block.tss-ba-center{text-align:center}.tss-block.tss-ba-right{text-align:right}
.tss-fs{font-size:var(--t-fsm,var(--t-fsd))}
@media (min-width:750px){.tss-fs{font-size:var(--t-fsd,var(--t-fsm))}}
.tss-fs :is(h1,h2,h3,h4,p,li,blockquote,summary,b){font-size:inherit}
.tss-txt :is(h1,h2,h3,h4,p,li,blockquote,summary,footer,small){font-family:inherit;font-weight:inherit;font-style:inherit;
  text-transform:inherit;letter-spacing:inherit;word-spacing:inherit;line-height:inherit}
.tss-txt-color :is(h1,h2,h3,h4,p,li,blockquote,summary,footer){color:inherit}
.tss-links a{color:var(--t-link,inherit)}
.tss-link-hover a{text-decoration:none}.tss-link-hover a:hover{text-decoration:underline}
</style>""")

SHADOWS = {"soft": "0 4px 12px rgba(0,0,0,.12)", "strong": "0 10px 24px rgba(0,0,0,.25)"}
TEXT_SHADOWS = {"soft": "0 1px 2px rgba(0,0,0,.25)", "strong": "0 2px 8px rgba(0,0,0,.45)"}
RADII = {"square": "0px", "slight": "6px", "rounded": "12px", "pill": "999px"}
SIZES = {"small": ("36px", "18px", "14px"), "medium": ("46px", "26px", "inherit"), "large": ("56px", "34px", "17px")}
HOVERS = {"none": ("none", "none"), "lift": ("translateY(-2px)", "none"), "darken": ("none", "brightness(.9)"), "grow": ("scale(1.04)", "none")}

# Button icons, used as a CSS mask so any button can show one
_ICON_SVG = {
    "arrow": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "cart": '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/><path d="M2 3h3l2.6 12.4a2 2 0 0 0 2 1.6h8.8a2 2 0 0 0 2-1.6L22 7H6"/>',
    "bag": '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4ZM3 6h18M16 10a4 4 0 0 1-8 0"/>',
    "heart": '<path d="M20.8 5.6a5.5 5.5 0 0 0-7.8 0L12 6.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 22l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8Z"/>',
    "star": '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9Z"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "external": '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 6-10 7L2 6"/>',
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2Z"/>',
    "download": '<path d="M12 3v12M7 10l5 5 5-5M5 21h14"/>',
}


def _icon_url(name):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" '
        f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{_ICON_SVG[name]}</svg>'
    )
    return f"url('data:image/svg+xml,{quote(svg)}')"


def _font_stack(key, fallback=None):
    return FONT_BY_KEY.get(key, (fallback, None, None))[0] if key else fallback


def _theme_button(settings):
    """The theme's button defaults, as override values the button builder understands."""
    return {
        "b_style": settings.get("button_style") or "filled",
        "b_bg": settings.get("button_bg") or "",
        "b_text": settings.get("button_text") or "",
        "b_radius": settings.get("button_radius") or "pill",
        "b_hover": settings.get("button_hover") or "darken",
    }


def _button_vars(o, theme):
    """Every button CSS variable for override values `o` on top of the theme's defaults."""
    def pick(key):
        return o.get(key) or theme.get(key) or ""

    style = pick("b_style") or "filled"
    color = pick("b_bg") or "var(--accent)"
    text = o.get("b_text") or ""
    border = o.get("b_border") or ""

    if style == "filled":
        bg, tx, bd = color, text or theme.get("b_text") or "var(--bg)", border or color
    elif style == "outline":
        bg, tx, bd = "transparent", text or color, border or color
    elif style == "soft":
        bg, tx, bd = f"color-mix(in srgb, {color} 14%, transparent)", text or color, border or "transparent"
    else:  # text only, link style
        bg, tx, bd = "transparent", text or color, border or "transparent"

    height, padding, font_size = SIZES.get(o.get("b_size") or "medium", SIZES["medium"])
    if style in ("text", "link"):
        height, padding = "auto", "0px"
    if o.get("b_height"):
        height = f"{int(o['b_height'])}px"
    if o.get("b_padding_x"):
        padding = f"{int(o['b_padding_x'])}px"
    if o.get("b_font_size"):
        font_size = f"{int(o['b_font_size'])}px"

    hover_t, hover_f = HOVERS.get(pick("b_hover") or "darken", HOVERS["darken"])
    vars_ = {
        "--b-bg": bg, "--b-tx": tx, "--b-bd": bd,
        "--b-hbg": o.get("b_hover_bg") or bg, "--b-htx": o.get("b_hover_text") or tx, "--b-hbd": o.get("b_hover_border") or bd,
        "--b-mh": height, "--b-px": padding, "--b-fs": font_size,
        "--b-r": RADII.get(pick("b_radius") or "pill", "999px"),
        "--b-bw": f"{int(o.get('b_border_width', 1) if o.get('b_border_width') is not None else 1)}px",
        "--b-dec": "underline" if style == "link" else "none",
        "--b-w": "100%" if o.get("b_full_width") else "auto",
        "--b-ff": _font_stack(o.get("b_font"), "inherit"),
        "--b-fw": o.get("b_weight") or "600",
        "--b-tt": o.get("b_transform") or "none",
        "--b-sh": SHADOWS.get(o.get("b_shadow"), "none"),
        "--b-sp": f"{int(o.get('b_speed', 150) if o.get('b_speed') is not None else 150)}ms",
        "--b-ht": hover_t, "--b-hf": hover_f,
    }
    if o.get("b_gap") is not None:
        vars_["--b-gap"] = f"{int(o['b_gap'])}px"
    if o.get("b_icon") in _ICON_SVG:
        vars_["--b-icon"] = _icon_url(o["b_icon"])
    return vars_


def _has_button_overrides(o):
    defaults = {"b_border_width": 1, "b_speed": 150, "b_gap": 12, "b_icon_position": "right", "b_shadow": "none"}
    return any(
        value not in ("", 0, False, None) and value != defaults.get(key)
        for key, value in o.items()
        if key.startswith("b_")
    )


def _style_attr(vars_):
    return ";".join(f"{k}:{v}" for k, v in vars_.items())


def theme_css(settings):
    """:root variables for the theme's fonts, base size and button defaults."""
    root = {
        "--tss-heading-font": _font_stack(settings.get("heading_font"), FONT_BY_KEY["system"][0]),
        "--tss-body-font": _font_stack(settings.get("body_font"), FONT_BY_KEY["system"][0]),
        "--tss-base-size": f"{int(settings.get('base_font_size') or 16)}px",
        **_button_vars({}, _theme_button(settings)),
    }
    # Every value comes from fixed tables or validated colours/numbers, so it can't break out of <style>
    return Markup(f"<style>:root{{{_style_attr(root)}}}</style>")


def _collect_fonts(data):
    keys = {data["settings"].get("heading_font"), data["settings"].get("body_font")}
    for page in data["templates"].values():
        for section in page["sections"].values():
            if section.get("hidden"):
                continue
            for key in ("t_heading_font", "t_body_font", "b_font"):
                keys.add(section["settings"].get(key))
            for block in section.get("blocks", []):
                if not block.get("hidden"):
                    keys.add(block["settings"].get("t_font"))
                    keys.add(block["settings"].get("b_font"))
    return keys


def google_fonts_link(keys):
    """One stylesheet for just the Google fonts in use (others are system fonts)."""
    families = []
    for key in sorted(k for k in keys if k):
        stack, family, weights = FONT_BY_KEY.get(key, (None, None, None))
        if family:
            families.append(f"family={quote(family).replace('%20', '+')}:wght@{weights}")
    if not families:
        return Markup("")
    href = "https://fonts.googleapis.com/css2?" + "&".join(families) + "&display=swap"
    return Markup(
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{escape(href)}">'
    )


def text_attrs(t):
    """(classes, inline style) for a block's text options."""
    classes, style = ["tss-txt"], []
    if t.get("t_font"):
        style.append(f"font-family:{_font_stack(t['t_font'], 'inherit')}")
    if t.get("t_size") or t.get("t_size_mobile"):
        classes.append("tss-fs")
        if t.get("t_size"):
            style.append(f"--t-fsd:{int(t['t_size'])}px")
        if t.get("t_size_mobile"):
            style.append(f"--t-fsm:{int(t['t_size_mobile'])}px")
    if t.get("t_weight"):
        style.append(f"font-weight:{t['t_weight']}")
    if t.get("t_italic"):
        style.append("font-style:italic")
    lines = [name for key, name in (("t_underline", "underline"), ("t_strike", "line-through")) if t.get(key)]
    if lines:
        style.append(f"text-decoration:{' '.join(lines)}")
    if t.get("t_transform"):
        style.append(f"text-transform:{t['t_transform']}")
    if t.get("t_color"):
        classes.append("tss-txt-color")
        style.append(f"color:{t['t_color']}")
    if t.get("t_line_height"):
        style.append(f"line-height:{t['t_line_height']}")
    if t.get("t_letter_spacing"):
        style.append(f"letter-spacing:{int(t['t_letter_spacing'])}px")
    if t.get("t_word_spacing"):
        style.append(f"word-spacing:{int(t['t_word_spacing'])}px")
    if t.get("t_align"):
        style.append(f"text-align:{t['t_align']}")
    if t.get("t_shadow") in TEXT_SHADOWS:
        style.append(f"text-shadow:{TEXT_SHADOWS[t['t_shadow']]}")
    if t.get("t_link_color"):
        classes.append("tss-links")
        style.append(f"--t-link:{t['t_link_color']}")
    if t.get("t_link_hover"):
        classes.append("tss-link-hover")
    return classes, style


def button_attrs(o, theme):
    """(classes, inline style) for button options; nothing when no button option was changed."""
    if not _has_button_overrides(o):
        return [], []
    classes = ["tss-btn"]
    if o.get("b_icon") in _ICON_SVG:
        classes += ["tss-bi", f"tss-bi-{o.get('b_icon_position') or 'right'}"]
    if o.get("b_align"):
        classes.append(f"tss-ba-{o['b_align']}")
    return classes, [_style_attr(_button_vars(o, theme))]


# Plays each section's entrance animation when it scrolls into view
ANIMATION_JS = Markup("""<script>
(function () {
  var els = document.querySelectorAll(".tss-anim-fade,.tss-anim-slide-up");
  if (!("IntersectionObserver" in window)) { els.forEach(function (el) { el.classList.add("tss-in"); }); return; }
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add("tss-in"); io.unobserve(e.target); } });
  }, { threshold: 0.12 });
  els.forEach(function (el) { io.observe(el); });
})();
</script>""")

# Countdown timers in blocks
BLOCKS_JS = Markup("""<script>
(function () {
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function tick() {
    document.querySelectorAll("[data-tss-countdown]").forEach(function (el) {
      var left = new Date(el.getAttribute("data-tss-countdown")).getTime() - Date.now();
      var out = el.querySelector(".tss-countdown__time");
      if (isNaN(left)) return;
      if (left <= 0) { out.textContent = el.getAttribute("data-expired"); el.classList.add("is-over"); return; }
      var s = Math.floor(left / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
      out.innerHTML = [[d, "days"], [h, "hours"], [m, "min"], [s % 60, "sec"]].map(function (p, i) {
        return "<span><b>" + (i ? pad(p[0]) : p[0]) + "</b><small>" + p[1] + "</small></span>";
      }).join("");
    });
  }
  tick();
  setInterval(tick, 1000);
})();
</script>""")

# Styles for blocks and custom sections
CUSTOM_CSS = Markup("""<style>
.tss-blocks{display:grid;gap:var(--tss-gap,20px);grid-template-columns:minmax(0,1fr);align-items:var(--tss-valign,start)}
@media (min-width:750px){.tss-blocks{grid-template-columns:repeat(var(--tss-cols,1),minmax(0,1fr))}}
.tss-extra-blocks{margin-top:32px}
.tss-block{padding-top:var(--tss-bpt,0);padding-bottom:var(--tss-bpb,0);min-width:0}
.tss-block.tss-bw-full{grid-column:1/-1}
.tss-block.tss-bw-narrow>*{max-width:480px}
.tss-block.tss-balign-left{text-align:left}.tss-block.tss-balign-center{text-align:center}.tss-block.tss-balign-right{text-align:right}
.tss-balign-center>*{margin-left:auto;margin-right:auto}.tss-balign-right>*{margin-left:auto}
.tss-divider{border:0;border-top:var(--tss-line,1px) var(--tss-line-style,solid) var(--tss-line-color,var(--line,rgba(0,0,0,.15)));margin:0}
.tss-icon-text{display:flex;flex-direction:column;gap:10px}
.tss-icon-text--left{flex-direction:row;align-items:flex-start;gap:14px;text-align:left}
.tss-icon-text svg{width:32px;height:32px;flex:none;color:var(--accent)}
.tss-balign-center .tss-icon-text--top{align-items:center}.tss-balign-right .tss-icon-text--top{align-items:flex-end}
.tss-icon-text h3{font-size:17px;margin:0}.tss-icon-text p{margin:4px 0 0;color:var(--muted)}
.tss-list{margin:0;padding-left:1.3em;text-align:left;display:inline-block}.tss-list li{margin:.3em 0}
.tss-list--checks{list-style:none;padding-left:0}.tss-list--checks li:before{content:"\\2713";color:var(--accent);font-weight:700;margin-right:.6em}
.tss-quote{margin:0;font-size:20px;line-height:1.5}
.tss-quote footer{margin-top:10px;font-size:14px;color:var(--muted)}
.tss-quote--bordered{border-left:3px solid var(--accent);padding-left:18px;text-align:left}
.tss-quote--highlighted{background:var(--soft);padding:22px 26px;border-radius:14px;font-style:italic}
.tss-social{display:flex;flex-wrap:wrap;gap:10px}
.tss-balign-center .tss-social{justify-content:center}.tss-balign-right .tss-social{justify-content:flex-end}
.tss-social a{padding:8px 14px;border:1px solid var(--line,rgba(0,0,0,.15));border-radius:999px;text-decoration:none;font-size:14px;font-weight:600}
.tss-social a:hover{border-color:currentColor}
.tss-countdown__title{margin:0 0 10px;font-weight:600}
.tss-countdown__time{display:flex;gap:12px;flex-wrap:wrap}
.tss-balign-center .tss-countdown__time{justify-content:center}.tss-balign-right .tss-countdown__time{justify-content:flex-end}
.tss-countdown__time span{display:flex;flex-direction:column;align-items:center;min-width:64px;padding:10px 8px;border-radius:12px;background:var(--soft)}
.tss-countdown__time b{font-size:26px;line-height:1.1}.tss-countdown__time small{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.tss-accordion{text-align:left}
.tss-accordion details{border-bottom:1px solid var(--line,rgba(0,0,0,.12))}
.tss-accordion summary{cursor:pointer;padding:14px 0;font-weight:600;list-style:none;display:flex;justify-content:space-between;gap:12px}
.tss-accordion summary::-webkit-details-marker{display:none}
.tss-accordion summary:after{content:"+";font-size:20px;line-height:1}.tss-accordion details[open] summary:after{content:"\\2212"}
.tss-accordion details p{margin:0 0 14px;color:var(--muted);white-space:pre-line}
.tss-map iframe{width:100%;height:var(--tss-map-h,360px);border:0;border-radius:12px;display:block}
.tss-video video{display:block;width:100%;max-width:960px;border-radius:12px;background:#000}
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
.tss-pinfo{margin-top:24px;font-size:var(--pi-fs,16px);text-align:left}
.tss-pinfo__heading{margin:0 0 8px;font-size:1.15em}
.tss-pinfo__item{border-top:1px solid var(--pi-line,var(--line,rgba(0,0,0,.12)))}
.tss-pinfo__item:last-child{border-bottom:1px solid var(--pi-line,var(--line,rgba(0,0,0,.12)))}
.tss-pinfo__title{margin:0;font-size:inherit}
.tss-pinfo__toggle{display:flex;width:100%;align-items:center;justify-content:space-between;gap:12px;min-height:48px;padding:12px 0;
  border:0;background:none;color:inherit;font:inherit;font-weight:600;text-align:left;cursor:pointer}
.tss-pinfo__toggle:focus-visible{outline:2px solid currentColor;outline-offset:2px}
.tss-pinfo__icon{flex:none;width:1.1em;height:1.1em;transition:transform .3s ease}
.tss-pinfo__toggle[aria-expanded="true"] .tss-pinfo__icon{transform:rotate(180deg)}
.tss-pinfo__panel{display:grid;grid-template-rows:1fr}
.tss-js .tss-pinfo__panel{grid-template-rows:0fr;visibility:hidden;transition:grid-template-rows .3s ease,visibility 0s .3s}
.tss-js .tss-pinfo__item.is-open .tss-pinfo__panel{grid-template-rows:1fr;visibility:visible;transition:grid-template-rows .3s ease,visibility 0s}
.tss-pinfo__inner{min-height:0;overflow:hidden}
.tss-pinfo__text{padding:0 0 16px;color:var(--muted,inherit);white-space:pre-line;overflow-wrap:anywhere}
@media (prefers-reduced-motion:reduce){.tss-js .tss-pinfo__panel,.tss-pinfo__icon{transition:none}}
</style>""")

# "Product information" rows: a click opens or closes a row (and, unless several
# may be open, closes the others). One listener for the page, so rows the
# customizer preview swaps in work too. Edit mode stops clicks before they get here.
PRODUCT_INFO_JS = Markup("""<script>
document.addEventListener("click", function (e) {
  var button = e.target.closest && e.target.closest(".tss-pinfo__toggle");
  if (!button) return;
  var root = button.closest("[data-product-info]");
  var open = button.getAttribute("aria-expanded") !== "true";
  function set(b, on) {
    b.setAttribute("aria-expanded", on ? "true" : "false");
    b.closest(".tss-pinfo__item").classList.toggle("is-open", on);
  }
  if (open && root.getAttribute("data-multiple") !== "true") {
    root.querySelectorAll('.tss-pinfo__toggle[aria-expanded="true"]').forEach(function (b) { set(b, false); });
  }
  set(button, open);
});
</script>""")


def shop_url(store):
    return f"/s/{store.store_slug}"


def _resolve_link(base_url, value):
    """Store-relative links ("/collections/all") point inside this store."""
    value = str(value or "")
    return base_url + value if value.startswith("/") and not value.startswith("//") else value


def _menu_links(text, base_url):
    """[{"label", "url"}] from "Label | /page" lines (already validated by the menu setting)."""
    links = []
    for line in str(text or "").splitlines():
        label, _, url = line.partition("|")
        label, url = label.strip(), url.strip()
        if label and not url:  # a bare link is its own label
            url = label
        if label and url:
            links.append({"label": label, "url": _resolve_link(base_url, url)})
    return links


def layout_data(settings, base_url, nav):
    """Header links, footer links and footer social links for layout/theme.liquid."""
    header = _menu_links(settings.get("header_menu"), base_url)
    if not header:
        header = [{"label": "Home", "url": f"{base_url}/"}, {"label": "Shop all", "url": f"{base_url}/collections/all"}]
        header += [{"label": c["name"], "url": c["url"]} for c in nav[: int(settings.get("header_collections") or 0)]]

    footer = _menu_links(settings.get("footer_menu"), base_url) or [
        {"label": "Home", "url": f"{base_url}/"},
        {"label": "Shop all", "url": f"{base_url}/collections/all"},
    ]
    social = [
        {"name": name, "url": settings[f"footer_{key}"]}
        for key, name in SOCIAL_NAMES.items()
        if settings.get(f"footer_{key}")
    ]
    return {"header_links": header, "footer_links": footer, "footer_social": social}


def _environment(files, base_url, currency, overrides=None):
    loader = {}
    for path, content in files.items():
        loader[path] = content
        # Snippets can be rendered by name: {% render 'product-card' %}
        if path.startswith("snippets/") and path.endswith(".liquid"):
            loader[path[len("snippets/"):-len(".liquid")]] = content
    # e.g. the footer snippet name pointing at the synced footer section's output
    loader.update(overrides or {})

    env = Environment(loader=DictLoader(loader), autoescape=True)

    def money(value):
        if value is None or value == "":
            return ""
        return f"{currency}{Decimal(str(value)):,.2f}"

    def link(value):
        return _resolve_link(base_url, value)

    env.filters["money"] = money
    env.filters["link"] = link
    return env


def _catalog(store):
    """Published products with what the product cards can show (tags, rating)."""
    shown = Q(reviews__status__in=SHOWN_REVIEWS)
    return published_products(store).prefetch_related("product_tags__tag").annotate(
        avg_rating=Avg("reviews__rating", filter=shown),
        review_count=Count("reviews", filter=shown, distinct=True),
    )


def _best_sellers(store, base_url, limit):
    sold = (
        OrderItem.objects.filter(product=OuterRef("pk"))
        .exclude(order__status="cancelled")
        .values("product")
        .annotate(total=Sum("quantity"))
        .values("total")
    )
    qs = _catalog(store).annotate(
        sold=Coalesce(Subquery(sold, output_field=IntegerField()), Value(0))
    ).order_by("-sold", "-created_at")
    return [product_drop(p, base_url) for p in qs[:limit]]


# Products a "Load more" grid can reveal, and products per all-products page
LOAD_MORE_MAX = 96
PAGE_SIZE = 24
SORTS = {
    "newest": None,  # the catalog's own order
    "price-asc": lambda d: (d["price"] is None, d["price"] or 0),
    "price-desc": lambda d: (d["price"] is None, -(d["price"] or 0)),
}


# "Only a few left" at this quantity or lower
FEW_LEFT = 5


def _available(variant):
    """Available stock of one variant over all locations; no stock record counts as 0."""
    return sum(max(item.available, 0) for item in variant.inventory_items.all())


def product_drop(product, base_url):
    images = [i.url for i in sorted(product.images.all(), key=lambda i: i.position)]
    variants = sorted(product.variants.all(), key=lambda v: v.created_at)
    prices = [v.price for v in variants]
    stock = {v.id: _available(v) for v in variants}
    available_total = sum(stock.values())

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
        "tags": [pt.tag.name for pt in product.product_tags.all()],
        # Stock for the templates: a number and a true/false. Quantity 0 (or no stock record) is sold out.
        "available_total": available_total,
        "is_sold_out": available_total <= 0,
        "few_left": 0 < available_total <= FEW_LEFT,
        "sold_out": available_total <= 0,  # older name, kept for theme copies that still use it
        "rating": round(float(product.avg_rating), 1) if getattr(product, "avg_rating", None) else None,
        "review_count": getattr(product, "review_count", 0) or 0,
        "variants": [
            {"id": str(v.id), "sku": v.sku or "", "price": v.price, "available_total": stock[v.id]}
            for v in variants
        ],
    }


# Shopper-facing names of the Additional details fields. "Column 1".."Column 7"
# have none: they show only once the store owner has named them.
DETAIL_LABELS = {
    "allergens": "Allergens",
    "food_recommendations": "Food recommendations",
    "storage_suggestions": "Storage suggestions",
}


def product_details(product, field_labels):
    """The product's filled Additional details, in admin order: [{"key", "label", "text"}]."""
    labels = field_labels if isinstance(field_labels, dict) else {}
    details = []
    for key in EXTRA_FIELDS:
        text = (getattr(product, key, None) or "").strip()
        label = str(labels.get(key) or DETAIL_LABELS.get(key) or "").strip()
        if text and label:
            details.append({"key": key, "label": label, "text": text})
    return details


_CHEVRON = (
    '<svg class="tss-pinfo__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="m6 9 6 6 6-6"/></svg>'
)


def _product_info_html(block, index, details, section_id, editor=False):
    """The "Product information" block: one accordion row per filled detail, or "" when there are none."""
    b = block["settings"]
    if not details:
        # The customizer still needs something to click to open the block's settings
        if editor:
            return (
                f'<div class="tss-pinfo" data-product-info data-tss-block="{int(index)}">'
                '<p class="tss-empty">Product information: this product has no Additional details filled in.</p></div>'
            )
        return ""

    style = [f"--pi-fs:{int(b.get('font_size') or 16)}px"]
    if b.get("divider_color"):
        style.append(f"--pi-line:{b['divider_color']}")
    heading = f'<h2 class="tss-pinfo__heading">{escape(b["heading"])}</h2>' if b.get("heading") else ""

    rows = []
    for i, detail in enumerate(details):
        is_open = i == 0 and bool(b.get("open_first"))
        row_id = f"pinfo-{section_id}-{detail['key']}"
        rows.append(
            f'<div class="tss-pinfo__item{" is-open" if is_open else ""}">'
            f'<h3 class="tss-pinfo__title"><button type="button" class="tss-pinfo__toggle" id="{escape(row_id)}-button" '
            f'aria-expanded="{"true" if is_open else "false"}" aria-controls="{escape(row_id)}">'
            f'<span>{escape(detail["label"])}</span>{_CHEVRON}</button></h3>'
            f'<div class="tss-pinfo__panel" id="{escape(row_id)}" role="region" aria-labelledby="{escape(row_id)}-button">'
            f'<div class="tss-pinfo__inner"><div class="tss-pinfo__text">{escape(detail["text"])}</div></div></div></div>'
        )

    return (
        f'<div class="tss-pinfo" data-product-info data-multiple="{"true" if b.get("allow_multiple") else "false"}" '
        f'data-tss-block="{int(index)}" style="{escape(";".join(style))}">{heading}{"".join(rows)}</div>'
    )


def collection_url(base_url, collection):
    return f"{base_url}/collections/{collection.handle or collection.id}"


def _find_collection(store, value):
    """A shown collection by id, handle or name (sections pick one); drafts are never found."""
    value = (value or "").strip()
    if not value:
        return None

    shown = published_collections(store)
    try:
        return shown.filter(id=uuid.UUID(value)).first()
    except ValueError:
        return shown.filter(handle=value).first() or shown.filter(name__iexact=value).first()


def _products(store, base_url, collection=None, limit=None, exclude=None):
    qs = _catalog(store)

    if collection is not None:
        qs = qs.filter(product_collections__collection=collection)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    if limit:
        qs = qs[:limit]

    return [product_drop(p, base_url) for p in qs]


def _chosen_products(store, base_url, ids, limit=None):
    """The products the owner picked, in the order they picked them (published ones only)."""
    by_id = {str(p.id): p for p in _catalog(store).filter(pk__in=ids)}
    picked = [by_id[i] for i in ids if i in by_id]
    return [product_drop(p, base_url) for p in picked[:limit]]


def _wrap_attrs(section_id, s, theme=None):
    """The wrapper every section gets: spacing, width, colours, visibility, animation, ID, classes."""
    classes = [
        "tss-wrap",
        f"tss-w-{s.get('width', 'wide')}",
        f"tss-hs-{s.get('heading_size', 'medium')}",
        f"tss-hw-{s.get('heading_weight', 'bold')}",
        f"tss-vis-{s.get('visibility', 'both')}",
        f"tss-div-{s.get('divider', 'none')}",
    ]
    if s.get("content_alignment", "default") != "default":
        classes.append(f"tss-align-{s['content_alignment']}")
    if s.get("animation", "none") != "none":
        classes.append(f"tss-anim-{s['animation']}")
    classes += (s.get("css_class") or "").split()

    style = [
        f"--tss-pt:{int(s.get('spacing_top', 48))}px",
        f"--tss-pb:{int(s.get('spacing_bottom', 48))}px",
        f"--tss-px:{int(s.get('padding_x', 20))}px",
        f"--tss-radius:{int(s.get('border_radius', 0))}px",
    ]
    if s.get("background_color"):
        style.append(f"--tss-bg:{s['background_color']}")
    if s.get("text_color"):
        style.append(f"--tss-color:{s['text_color']}")

    extra_attrs = ""
    if s.get("t_body_font"):
        style.append(f"font-family:{_font_stack(s['t_body_font'], 'inherit')}")
    if s.get("t_heading_font"):
        style.append(f"--tss-section-hfont:{_font_stack(s['t_heading_font'], 'inherit')}")
        extra_attrs += " data-hfont"
    if s.get("t_shadow") in TEXT_SHADOWS:
        style.append(f"text-shadow:{TEXT_SHADOWS[s['t_shadow']]}")
    if s.get("t_link_color"):
        classes.append("tss-links")
        style.append(f"--t-link:{s['t_link_color']}")
    if s.get("t_link_hover"):
        classes.append("tss-link-hover")
    b_classes, b_style = button_attrs(s, theme or {})
    classes += b_classes
    style += b_style

    anchor = s.get("anchor_id") or ""
    id_attr = f' id="{escape(anchor)}"' if anchor else ""
    return Markup(
        f'<div class="{escape(" ".join(classes))}" style="{escape(";".join(style))}"{id_attr}{extra_attrs}>'
    )


def _prepare(section_type, settings):
    """Settings as the section template sees them: rich text becomes (cleaned) HTML."""
    schema = SECTIONS[section_type]["settings"]
    return {
        key: Markup(sanitize_html(value)) if schema.get(key, {}).get("type") == "richtext" else value
        for key, value in settings.items()
    }


# Simple line icons for "Icon with text" blocks (24x24, stroke)
ICON_PATHS = {
    "star": '<path d="m12 3 2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9Z"/>',
    "heart": '<path d="M20.8 5.6a5.5 5.5 0 0 0-7.8 0L12 6.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 22l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8Z"/>',
    "truck": '<path d="M1 6h13v10H1zM14 10h4l3 3v3h-7"/><circle cx="5.5" cy="18.5" r="1.5"/><circle cx="17.5" cy="18.5" r="1.5"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
    "gift": '<path d="M20 12v10H4V12M2 7h20v5H2zM12 22V7"/><path d="M12 7H7.5a2.5 2.5 0 0 1 0-5C11 2 12 7 12 7ZM12 7h4.5a2.5 2.5 0 0 0 0-5C13 2 12 7 12 7Z"/>',
    "leaf": '<path d="M11 20A7 7 0 0 1 4 13c0-6 7-10 16-10 0 9-4 16-10 16"/><path d="M4 21c4-6 8-9 13-11"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "phone": '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.4 1.8.7 2.7a2 2 0 0 1-.5 2.1L8 9.8a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.7.7a2 2 0 0 1 1.7 2Z"/>',
    "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 6-10 7L2 6"/>',
    "check": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    "return": '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "sparkle": '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/>',
}

SOCIAL_NAMES = {
    "instagram": "Instagram", "facebook": "Facebook", "x": "X", "tiktok": "TikTok",
    "youtube": "YouTube", "pinterest": "Pinterest", "linkedin": "LinkedIn",
}

_VIDEO_FILE = re.compile(r"^(?:https?://|/(?!/))\S+\.(?:mp4|webm|ogg)(?:\?\S*)?$", re.IGNORECASE)


def _block_html(block, store, env, base_url):
    """One block's own markup, or "" when it has nothing to show."""
    b = block["settings"]
    kind = block["type"]
    link = env.filters["link"]

    if kind == "heading" and b.get("text"):
        tag = {"large": "h2", "medium": "h3", "small": "h4"}.get(b.get("size"), "h3")
        return f'<{tag} class="tss-block-heading">{escape(b["text"])}</{tag}>'

    if kind == "text" and b.get("body"):
        return f'<div class="tss-text">{sanitize_html(b["body"])}</div>'

    if kind == "image" and b.get("image_url"):
        img = f'<img src="{escape(b["image_url"])}" alt="{escape(b.get("alt") or "")}" loading="lazy">'
        if b.get("link"):
            img = f'<a href="{escape(link(b["link"]))}">{img}</a>'
        return f'<div class="tss-image tss-media">{img}</div>'

    if kind == "button" and b.get("label"):
        target = ' target="_blank" rel="noopener"' if b.get("new_tab") else ""
        return f'<div><a class="button" href="{escape(link(b.get("link") or ""))}"{target}>{escape(b["label"])}</a></div>'

    if kind == "video":
        url = (b.get("url") or "").strip()
        if not _VIDEO_FILE.match(url):
            return '<div class="tss-empty">Add a link to an .mp4 or .webm video file.</div>' if url else ""
        flags = " autoplay muted loop playsinline" if b.get("autoplay") else ""
        flags += " controls" if b.get("controls", True) else ""
        poster = f' poster="{escape(b["poster"])}"' if b.get("poster") else ""
        return f'<div class="tss-video tss-media"><video src="{escape(url)}"{poster}{flags} preload="metadata"></video></div>'

    if kind == "products":
        products = _chosen_products(store, base_url, b.get("products") or [])
        if not products:
            return '<div class="tss-empty">Choose products to show here.</div>'
        try:
            cards = env.from_string(
                "{% for product in products %}{% render 'product-card', product: product, show_vendor: false %}{% endfor %}"
            ).render(products=products)
        except LiquidError:
            cards = "".join(f'<a href="{escape(p["url"])}">{escape(p["title"])}</a>' for p in products)
        return f'<div class="product-grid product-grid--{int(b.get("columns") or 4)}" style="text-align:left">{cards}</div>'

    if kind == "collections":
        ids = b.get("collections") or []
        # Drafts and collections off the Online Store are left out
        by_id = {str(c.id): c for c in published_collections(store).filter(id__in=ids)}
        tiles = "".join(
            f'<a class="tss-collection" href="{escape(collection_url(base_url, c))}">{escape(c.name)}</a>'
            for c in (by_id.get(i) for i in ids) if c
        )
        return f'<div class="tss-collections">{tiles}</div>' if tiles else '<div class="tss-empty">Choose collections to show here.</div>'

    if kind == "divider":
        style = f'--tss-line:{int(b.get("thickness") or 1)}px;--tss-line-style:{escape(b.get("line_style") or "solid")}'
        if b.get("color"):
            style += f';--tss-line-color:{escape(b["color"])}'
        return f'<hr class="tss-divider" style="{style}">'

    if kind == "spacer":
        return f'<div aria-hidden="true" style="height:{int(b.get("height") or 40)}px"></div>'

    if kind == "icon_text" and (b.get("title") or b.get("text")):
        icon = ICON_PATHS.get(b.get("icon"), ICON_PATHS["star"])
        svg = f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icon}</svg>'
        title = f'<h3>{escape(b["title"])}</h3>' if b.get("title") else ""
        text = f'<p>{escape(b["text"])}</p>' if b.get("text") else ""
        position = "left" if b.get("icon_position") == "left" else "top"
        return f'<div class="tss-icon-text tss-icon-text--{position}">{svg}<div>{title}{text}</div></div>'

    if kind == "list":
        items = [line.strip() for line in (b.get("items") or "").splitlines() if line.strip()]
        if not items:
            return ""
        tag = "ol" if b.get("list_style") == "numbers" else "ul"
        lis = "".join(f"<li>{escape(item)}</li>" for item in items)
        return f'<{tag} class="tss-list tss-list--{escape(b.get("list_style") or "bullets")}">{lis}</{tag}>'

    if kind == "quote" and b.get("text"):
        author = f'<footer>&mdash; {escape(b["author"])}</footer>' if b.get("author") else ""
        return (
            f'<blockquote class="tss-quote tss-quote--{escape(b.get("quote_style") or "bordered")}">'
            f'<p style="margin:0">{escape(b["text"])}</p>{author}</blockquote>'
        )

    if kind == "social":
        links = "".join(
            f'<a href="{escape(b[key])}" target="_blank" rel="noopener">{name}</a>'
            for key, name in SOCIAL_NAMES.items() if b.get(key)
        )
        return f'<div class="tss-social">{links}</div>' if links else '<div class="tss-empty">Add your social profile links.</div>'

    if kind == "countdown":
        if not b.get("end"):
            return '<div class="tss-empty">Choose when the countdown ends.</div>'
        title = f'<p class="tss-countdown__title">{escape(b["title"])}</p>' if b.get("title") else ""
        return (
            f'<div class="tss-countdown" data-tss-countdown="{escape(b["end"])}" data-expired="{escape(b.get("expired_text") or "")}">'
            f'{title}<div class="tss-countdown__time" aria-live="polite"></div></div>'
        )

    if kind == "accordion":
        groups = [g.strip() for g in re.split(r"\n\s*\n", (b.get("items") or "").strip()) if g.strip()]
        items = []
        for i, group in enumerate(groups):
            question, _, answer = group.partition("\n")
            is_open = " open" if i == 0 and b.get("open_first") else ""
            items.append(
                f"<details{is_open}><summary>{escape(question.strip())}</summary>"
                f"<p>{escape(answer.strip())}</p></details>"
            )
        return f'<div class="tss-accordion">{"".join(items)}</div>' if items else ""

    if kind == "map":
        height = int(b.get("height") or 360)
        if b.get("embed"):
            return f'<div class="tss-map tss-embed" style="--tss-map-h:{height}px">{sanitize_embed(b["embed"])}</div>'
        if b.get("address"):
            src = f"https://www.google.com/maps?q={quote(b['address'])}&output=embed"
            return (
                f'<div class="tss-map" style="--tss-map-h:{height}px"><iframe src="{escape(src)}" title="Map" '
                'loading="lazy" referrerpolicy="no-referrer-when-downgrade"></iframe></div>'
            )
        return '<div class="tss-empty">Add an address or embed code.</div>'

    if kind == "html" and b.get("html"):
        return f'<div class="tss-embed">{sanitize_embed(b["html"])}</div>'

    return ""


def _render_blocks(blocks, store, env, base_url, theme=None):
    """Visible blocks in order, each in a wrapper with its own spacing, alignment and width."""
    parts = []
    for position, block in enumerate(blocks):
        if block.get("hidden"):
            continue
        # The block's index in its section, so the customizer preview can open it on click
        index = block.get("_index", position)
        inner = _block_html(block, store, env, base_url)
        if not inner:
            continue
        b = block["settings"]
        classes = ["tss-block", f"tss-bw-{b.get('width', 'auto')}"]
        if b.get("alignment", "default") != "default":
            classes.append(f"tss-balign-{b['alignment']}")
        style = [f"--tss-bpt:{int(b.get('spacing_top', 0))}px", f"--tss-bpb:{int(b.get('spacing_bottom', 0))}px"]
        if any(key.startswith("t_") for key in b):
            t_classes, t_style = text_attrs(b)
            classes += t_classes
            style += t_style
        if block["type"] == "button":
            b_classes, b_style = button_attrs(b, theme or {})
            classes += b_classes
            style += b_style
        parts.append(
            f'<div class="{escape(" ".join(classes))}" style="{escape(";".join(style))}" data-tss-block="{int(index)}">{inner}</div>'
        )
    return parts


def _render_custom(section, store, env, base_url, theme=None):
    """A custom section: the owner's name as a heading, then their blocks in a grid."""
    s = section["settings"]
    parts = _render_blocks(section.get("blocks", []), store, env, base_url, theme)

    heading = (
        f'<h2 class="section-heading">{escape(s["heading"])}</h2>' if s.get("show_heading") and s.get("heading") else ""
    )
    valign = {"top": "start", "center": "center", "bottom": "end", "stretch": "stretch"}.get(s.get("vertical_align"), "start")
    body = (
        f'<div class="tss-blocks" style="--tss-cols:{int(s.get("columns") or 1)};'
        f'--tss-gap:{int(s.get("block_gap", 20))}px;--tss-valign:{valign}">{"".join(parts)}</div>'
        if parts else ""
    )
    if not heading and not body:
        return Markup("")

    align = s.get("content_alignment") or "left"
    align = escape("left" if align == "default" else align)
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


# Where a synced section's HTML goes in the page: the layout's {% render 'header' %} /
# {% render 'footer' %} output this, and it is replaced with the section (see section_sync)
SECTION_PLACEHOLDER = "<!--tss-section:{}-->"


class _LazyList(Sequence):
    """A list loaded on first use: the fast preview path reads collections only if the Liquid uses them."""

    def __init__(self, load):
        self._load = load
        self._items = None

    def _all(self):
        if self._items is None:
            self._items = list(self._load())
        return self._items

    def __getitem__(self, index):
        return self._all()[index]

    def __len__(self):
        return len(self._all())


def _nav(store, base_url):
    return [{"name": c.name, "url": collection_url(base_url, c)} for c in published_collections(store)[:20]]


def _env_key(files, base_url, currency):
    """What a compiled section depends on besides its own file: the snippets it can render."""
    snippets = sorted((p, hashlib.sha256(c.encode("utf-8")).hexdigest()) for p, c in files.items() if p.startswith("snippets/"))
    return hashlib.sha256(json.dumps([base_url, currency, snippets]).encode("utf-8")).hexdigest()


def _synced_html(store, store_theme, name, section, data, env, env_key, context, editor):
    raw = (data.get("sections") or {}).get(name)
    return sync.render_section(
        name, theme_id=store_theme.theme_id, content=section["content"],
        values=raw if raw is not None else section["values"], env=env, env_key=env_key,
        context=context, editor=editor, link=lambda url: _resolve_link(shop_url(store), url),
    )


def render_synced_section_fast(store, name, custom_data):
    """
    One synced section only (the customizer's live preview while editing it):
    a single database read for its code, and its compiled template from the cache.
    Returns None when it can't be done this way (the caller renders it the full way).
    """
    path = sync.SYNCED_SECTIONS[name]["path"]
    row = (
        ThemeFile.objects.filter(file_path=path, theme__store_assignments__store=store, theme__store_assignments__is_active=True)
        .values("theme_id", "content").first()
    )
    if row is None:
        return None
    data = resolve(custom_data)
    settings = data["settings"]
    base_url = shop_url(store)
    env = _environment({}, base_url, settings["currency_symbol"])
    context = {"shop": {"name": store.store_name, "url": base_url}, "settings": settings, "template": "index"}
    if "collections" in sync.SYNCED_SECTIONS[name]["context"]:
        context["collections"] = _LazyList(lambda: _nav(store, base_url))
    try:
        return sync.render_section(
            name, theme_id=row["theme_id"], content=row["content"], values=(data.get("sections") or {}).get(name) or {},
            env=env, env_key=("fast", base_url, settings["currency_symbol"]),
            context=context, editor=True, link=lambda url: _resolve_link(base_url, url),
        )
    except Exception:  # e.g. it renders a snippet: the full way has them
        sync.invalidate(row["theme_id"], path)
        return None


# ---------------------------------------------------------------- blog
#
# Posts are written in the admin (BlogPost); the store shows the published ones.
# Until a store has published one, the customizer previews the blog pages with
# these sample posts (blog_posts).

SAMPLE_POSTS = [
    {
        "title": "Five summer drinks to make at home", "handle": "five-summer-drinks", "days_ago": 2,
        "category": "Recipes", "tags": ["Summer", "Easy"], "author": "Asha Rao",
        "bio": "Asha writes about food, drink and the people who make them.",
        "excerpt": "Bright, cold and ready in minutes: our favourite drinks for warm evenings.",
        "body": "<p>Long days call for something cold. These five drinks need no special tools.</p>"
                "<h2>1. Mint lemonade</h2><p>Crush a handful of mint with sugar, add lemon juice and top up with "
                "sparkling water.</p><blockquote>Make the syrup the night before and keep it in the fridge.</blockquote>"
                "<h2>2. Cold brew tonic</h2><p>Pour cold brew coffee over tonic and ice for a bitter, fizzy pick-me-up.</p>"
                "<ul><li>Use fresh ice</li><li>Chill the glasses</li><li>Taste before you sweeten</li></ul>",
    },
    {
        "title": "How we choose our ingredients", "handle": "how-we-choose-ingredients", "days_ago": 6,
        "category": "Stories", "tags": ["Sourcing"], "author": "Ben Carter",
        "bio": "Ben looks after the people and farms we buy from.",
        "excerpt": "A look behind the scenes at the farms and makers we work with.",
        "body": "<p>Every ingredient starts with a visit. We meet the growers, taste the harvest and ask how it was made.</p>"
                "<h2>Small batches</h2><p>Buying in small batches means fresher stock and fairer prices.</p>",
    },
    {
        "title": "A beginner's guide to tasting notes", "handle": "guide-to-tasting-notes", "days_ago": 11,
        "category": "Guides", "tags": ["Learn"], "author": "Asha Rao",
        "bio": "Asha writes about food, drink and the people who make them.",
        "excerpt": "Sweet, bright, earthy: learn the words that help you find what you like.",
        "body": "<p>Tasting notes are a shared language. Start with three questions: is it sweet, is it sharp, "
                "what does it remind you of?</p>",
    },
    {
        "title": "Hosting a tasting evening", "handle": "hosting-a-tasting-evening", "days_ago": 18,
        "category": "Guides", "tags": ["Events", "Easy"], "author": "Ben Carter",
        "bio": "Ben looks after the people and farms we buy from.",
        "excerpt": "Everything you need for a relaxed evening with friends, from glasses to snacks.",
        "body": "<p>Pick three to five things to taste, go from light to strong, and keep water close by.</p>",
    },
]


def _date_text(value):
    """"October 14, 2026" (no zero padding, on every OS)."""
    return f"{value:%B} {value.day}, {value.year}"


def _slug_link(base_url, path, name):
    """{"name", "handle", "url"} for a category, tag or author, or None when there is no name."""
    handle = slugify(name or "")
    return {"name": name, "handle": handle, "url": f"{base_url}/blog/{path}/{handle}"} if handle else None


def _sample_post_drop(post, base_url):
    """A sample post as the theme sees a post: what article cards and the article page use."""
    published = timezone.now() - timedelta(days=post["days_ago"])
    words = len(re.sub(r"<[^>]+>", " ", post["body"]).split())
    author = _slug_link(base_url, "author", post["author"])
    author.update(bio=post["bio"], avatar=None)
    return {
        "id": post["handle"],
        "title": post["title"],
        "handle": post["handle"],
        # Sample posts have no page of their own: their links open the blog page, not a "Not found"
        "url": f"{base_url}/blog",
        "excerpt": post["excerpt"],
        "body": Markup(sanitize_embed(post["body"])),
        "cover_image": None,
        "published_at": published,
        "date": _date_text(published),
        "reading_time": max(1, round(words / 200)),
        "author": author,
        "category": _slug_link(base_url, "category", post["category"]),
        "tags": [_slug_link(base_url, "tagged", tag) for tag in post["tags"]],
    }


def published_posts(store):
    """The store's posts that shoppers can read: published, newest first."""
    return BlogPost.objects.filter(
        store=store, status=BlogPost.PUBLISHED, published_at__lte=timezone.now(),
    ).order_by("-published_at", "-created_at")


def _post_drop(post, base_url):
    """A saved post as the theme sees a post (same fields as a sample post)."""
    words = len(re.sub(r"<[^>]+>", " ", post.body or "").split())
    author = _slug_link(base_url, "author", post.author)
    if author:
        author.update(bio="", avatar=None)
    tags = [_slug_link(base_url, "tagged", tag) for tag in post.tags or []]
    return {
        "id": str(post.pk),
        "title": post.title,
        "handle": post.handle,
        "url": f"{base_url}/blog/{post.handle}",
        "excerpt": post.excerpt,
        "body": Markup(sanitize_embed(post.body or "")),
        "cover_image": post.cover_image or None,
        "published_at": post.published_at,
        "date": _date_text(post.published_at),
        "reading_time": max(1, round(words / 200)),
        "author": author,
        "category": _slug_link(base_url, "category", post.category),
        "tags": [t for t in tags if t],
    }


def blog_posts(store, base_url, editor):
    """Published posts newest first; sample posts in the customizer preview while there are none."""
    posts = [_post_drop(post, base_url) for post in published_posts(store)]
    if not posts and editor:
        posts = [_sample_post_drop(post, base_url) for post in SAMPLE_POSTS]
    return posts


def _filtered(posts, blog_filter):
    """Posts in a category, with a tag or by an author: (posts, the category, tag or author name)."""
    kind, handle = blog_filter["kind"], blog_filter["handle"]
    if kind == "tagged":
        shown = [p for p in posts if any(t["handle"] == handle for t in p["tags"])]
        name = next((t["name"] for p in shown for t in p["tags"] if t["handle"] == handle), handle)
    else:
        key = "author" if kind == "author" else "category"
        shown = [p for p in posts if p[key] and p[key]["handle"] == handle]
        name = shown[0][key]["name"] if shown else handle
    return shown, name


def _blog_drop(posts, settings, base_url, per_page, page_number, blog_filter=None):
    """The blog page: this page's posts, the categories to filter by, and paging."""
    categories = {}
    for post in posts:
        if post["category"]:
            categories.setdefault(post["category"]["handle"], dict(post["category"], active=False))
    url, active = f"{base_url}/blog", None
    if blog_filter:
        posts, name = _filtered(posts, blog_filter)
        url = f"{base_url}/blog/{blog_filter['kind']}/{blog_filter['handle']}"
        active = {"kind": blog_filter["kind"], "handle": blog_filter["handle"], "name": name}
        if blog_filter["kind"] == "category" and blog_filter["handle"] in categories:
            categories[blog_filter["handle"]]["active"] = True
    pages = max(1, -(-len(posts) // per_page))
    page_number = min(max(1, page_number), pages)
    return {
        "title": settings.get("blog_title") or "Blog",
        "description": settings.get("blog_description") or "",
        # Paging links keep the filter
        "url": url,
        "all_url": f"{base_url}/blog",
        "filter": active,
        "articles": posts[(page_number - 1) * per_page:page_number * per_page],
        "articles_count": len(posts),
        "categories": sorted(categories.values(), key=lambda c: c["name"].lower()),
        "page": page_number,
        "pages": pages,
    }


def _article_drop(posts, index):
    """Post `index` of `posts` with its neighbours and related posts (same category first)."""
    post = dict(posts[index])
    post["previous"] = posts[index + 1] if index + 1 < len(posts) else None  # older
    post["next"] = posts[index - 1] if index > 0 else None  # newer
    others = [p for i, p in enumerate(posts) if i != index]
    same = [p for p in others if post["category"] and p["category"] and p["category"]["handle"] == post["category"]["handle"]]
    post["related"] = (same + [p for p in others if p not in same])[:6]
    return post


def render_page(store, page, product=None, collection=None, custom_data=None, editor=False, only_section=None,
                sort="newest", page_number=1, article=None, blog_filter=None):
    """
    page: "index", "product", "collection", "blog" or "article".
    product: a catalog Product (product page).
    collection: {"name", "obj"} where obj is a Collection, or None for "all".
    article: the handle of the post the article page shows (the newest when None or not found).
    blog_filter: {"kind": "category" | "tagged" | "author", "handle"} narrows the blog page.
    sort, page_number: the collection page's order (see SORTS) and page (PAGE_SIZE products each).
    custom_data: unsaved section data to render instead of the saved one (customizer preview).
    editor: wrap each section in a marker the customizer preview uses for drag and drop.
    only_section: return just that section's HTML (customizer preview updates one section in place).
    """
    store_theme = get_store_theme(store)
    files = theme_files(store_theme)
    data = resolve(store_theme.custom_data if custom_data is None else custom_data)
    settings = data["settings"]
    base_url = shop_url(store)
    # The header and footer are synced sections (sections/<name>.liquid) when the theme has
    # them: the layout's {% render 'header' %} / {% render 'footer' %} then show them (see section_sync)
    synced = {}
    for name in sync.SYNCED_SECTIONS:
        section = synced_section(store_theme, name, files)
        if section:
            synced[name] = section
    overrides = {sync.SYNCED_SECTIONS[name]["snippet"]: SECTION_PLACEHOLDER.format(name) for name in synced}
    env = _environment(files, base_url, settings["currency_symbol"], overrides or None)

    shop = {"name": store.store_name, "url": base_url}
    nav = _nav(store, base_url)
    product_drop_ = product_drop(product, base_url) if product else None
    if product_drop_:
        product_drop_["details"] = product_details(product, store.field_labels)
    collection_drop = None

    if page == "collection":
        sort = sort if sort in SORTS else "newest"
        products = _products(store, base_url, collection=collection["obj"])
        if SORTS[sort]:
            products.sort(key=SORTS[sort])
        pages = max(1, -(-len(products) // PAGE_SIZE))
        page_number = min(max(1, page_number), pages)
        obj = collection["obj"]
        collection_drop = {
            "name": collection["name"],
            "url": collection_url(base_url, obj) if obj else f"{base_url}/collections/all",
            "handle": obj.handle if obj else "all",
            # Written in the admin's rich-text editor; cleaned like section rich text
            "description": Markup(sanitize_html(obj.description)) if obj and obj.description else "",
            "image": obj.image_url if obj else "",
            "products": products[(page_number - 1) * PAGE_SIZE:page_number * PAGE_SIZE],
            "products_count": len(products),
            "sort": sort,
            "page": page_number,
            "pages": pages,
        }

    template_data = data["templates"][page]

    # Posts are read only where they show: the blog pages and "Latest posts" sections
    shows_posts = page in ("blog", "article") or any(
        SECTIONS.get(s["type"], {}).get("uses_articles") for s in template_data["sections"].values()
    )
    posts = blog_posts(store, base_url, editor) if shows_posts else []
    blog_drop = article_drop = None
    if page == "blog":
        per_page = next(
            (s["settings"]["posts_per_page"] for s in template_data["sections"].values() if s["type"] == "main-blog"), 9
        )
        blog_drop = _blog_drop(posts, settings, base_url, per_page, page_number, blog_filter)
    elif page == "article" and posts:
        index = next((i for i, p in enumerate(posts) if p["handle"] == article), 0)
        article_drop = _article_drop(posts, index)

    common = {
        "shop": dict(shop, blog_url=f"{base_url}/blog"),
        "settings": settings,
        "product": product_drop_,
        "collection": collection_drop,
        "collections": nav,
        "blog": blog_drop,
        "article": article_drop,
        "template": page,
        # True in the customizer preview: templates may show hints that shoppers never see
        "design_mode": editor,
        **layout_data(settings, base_url, nav),
    }

    # The synced sections, rendered from their saved (or, in the preview, unsaved) values
    synced_html = {}
    env_key = _env_key(files, base_url, settings["currency_symbol"]) if synced else None
    for name, section in synced.items():
        synced_html[name] = _synced_html(store, store_theme, name, section, data, env, env_key, common, editor)
        if only_section == name:
            return synced_html[name]

    visible = [
        (section_id, template_data["sections"][section_id])
        for section_id in template_data["order"]
        if not template_data["sections"][section_id].get("hidden")
    ]
    if only_section is not None:
        visible = [(i, sec) for i, sec in visible if i == only_section]
    elif page == "index" and not visible:
        fallback = resolve({"templates": {"index": {
            "order": [i for i, _ in FALLBACK_INDEX], "sections": dict(FALLBACK_INDEX),
        }}})
        visible = list(fallback["templates"]["index"]["sections"].items())

    real_ids = set(template_data["sections"])
    theme_button = _theme_button(settings)
    html = [google_fonts_link(_collect_fonts(data)), theme_css(settings), SECTION_CSS, TYPE_CSS]

    html.append(CUSTOM_CSS)
    page_styles = len(html)

    for section_id, raw_section in visible:
        # "Latest posts" with no posts to show is left out of the live store
        if SECTIONS[raw_section["type"]].get("uses_articles") and not posts and not editor:
            continue
        section = dict(raw_section, id=section_id)
        section["settings"] = _prepare(section["type"], section["settings"])
        path = f"sections/{section['type']}.liquid"
        wrap = editor and section_id in real_ids

        if wrap:
            # The customizer preview labels a section with the name the sections list shows
            label = section["settings"].get("heading") or SECTIONS.get(section["type"], {}).get("name", "")
            html.append(Markup(f'<div data-tss-section="{escape(section_id)}" data-tss-label="{escape(label)}">'))
        html.append(_wrap_attrs(section_id, raw_section["settings"], theme_button))

        if section["type"] == "custom":
            html.append(_render_custom(section, store, env, base_url, theme_button))

        elif path not in files:
            html.append(Markup(f"<!-- {escape(path)} is missing -->"))

        else:
            # The template sees only its own blocks (e.g. slides); visible ones only
            all_blocks = section.get("blocks", [])
            section["blocks"] = [b for b in all_blocks if b["type"] not in GENERIC_BLOCK_TYPES and not b.get("hidden")]
            # Slides carry the same text options as blocks; the template puts them on the slide's text
            for own in section["blocks"]:
                t_classes, t_style = text_attrs(own["settings"])
                own["settings"] = dict(own["settings"], _t_class=" ".join(t_classes), _t_style=";".join(t_style))
            extra_blocks = [dict(b, _index=i) for i, b in enumerate(all_blocks) if b["type"] in GENERIC_BLOCK_TYPES]

            # The product page's "Product information" accordion; the template places it under the description
            product_info = ""
            if section["type"] == "main-product" and product_drop_:
                info = next(
                    ((i, b) for i, b in enumerate(all_blocks) if b["type"] == "product_info" and not b.get("hidden")),
                    None,
                )
                if info:
                    product_info = _product_info_html(info[1], info[0], product_drop_["details"], section_id, editor)
                section["product_info"] = Markup(product_info)

            if SECTIONS[section["type"]].get("uses_products"):
                ss = section["settings"]
                source = ss.get("source", "collection")
                chosen = ss.get("products") or []
                limit = ss.get("limit")
                # "Load more": render more cards than `limit`; the template shows `limit` first
                if ss.get("load_more") and ss.get("layout") == "grid" and source != "products":
                    limit = LOAD_MORE_MAX
                exclude = product.pk if product else None
                # Products picked before "source" existed still show
                if source == "products" or (source == "collection" and chosen and not ss.get("collection")):
                    section["products"] = _chosen_products(store, base_url, chosen, limit)
                elif source == "best_sellers":
                    section["products"] = _best_sellers(store, base_url, limit)
                else:
                    collection_obj = _find_collection(store, ss.get("collection")) if source == "collection" else None
                    section["products"] = _products(
                        store, base_url, collection=collection_obj, limit=limit, exclude=exclude,
                    )

            if SECTIONS[section["type"]].get("uses_articles"):
                ss = section["settings"]
                wanted = slugify(ss.get("category") or "")
                shown = [p for p in posts if not wanted or (p["category"] and p["category"]["handle"] == wanted)]
                if article_drop:
                    shown = [p for p in shown if p["handle"] != article_drop["handle"]]
                section["articles"] = shown[:ss.get("limit") or 3]

            # The slideshow template has no heading of its own; show the section heading above it
            if section["type"] == "slideshow" and section["settings"].get("heading") and section["blocks"]:
                html.append(Markup(
                    '<div class="page-width" style="padding-top:32px">'
                    f'<h2 class="section-heading">{escape(section["settings"]["heading"])}</h2></div>'
                ))

            try:
                rendered = env.get_template(path).render(section=section, **common)
                html.append(Markup(rendered))
                # A product template edited before the accordion existed doesn't place it: show it after the section
                if product_info and "data-product-info" not in rendered:
                    html.append(Markup(f'<div class="page-width">{product_info}</div>'))
            except LiquidError as error:
                html.append(_error_box(path, error))

            # Blocks added to a ready-made section show after its own content
            extra = _render_blocks(extra_blocks, store, env, base_url, theme_button)
            if extra:
                html.append(Markup(
                    f'<div class="page-width tss-extra-blocks"><div class="tss-blocks">{"".join(extra)}</div></div>'
                ))

        html.append(Markup("</div>"))
        if wrap:
            html.append(Markup("</div>"))

    if only_section is not None:
        return Markup("\n".join(html[page_styles:]))

    html.append(ANIMATION_JS)
    html.append(BLOCKS_JS)
    html.append(PRODUCT_INFO_JS)

    content = env.get_template(f"templates/{page}.liquid").render(
        sections=Markup("\n".join(html)),
        **common,
    )

    if product:
        title = product.seo_title or product.title
    elif collection_drop:
        title = (collection["obj"].seo_title if collection["obj"] else "") or collection_drop["name"]
    elif article_drop:
        title = article_drop["title"]
    elif blog_drop:
        title = blog_drop["title"]
    else:
        title = store.store_name

    page_html = env.get_template("layout/theme.liquid").render(
        content_for_layout=Markup(content),
        page_title=title,
        **common,
    )
    for name, html in synced_html.items():
        page_html = page_html.replace(SECTION_PLACEHOLDER.format(name), str(html))
    return page_html
