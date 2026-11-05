"""
Renders a store's theme to HTML for the public storefront.

Page = layout/theme.liquid
         └ templates/<page>.liquid
             └ sections/<type>.liquid for each section, in the store's order
"""
import hashlib
import html
import json
from contextvars import ContextVar
import re
import uuid
from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode, urlsplit

from liquid import DictLoader, Environment
from liquid.exceptions import LiquidError
from markupsafe import Markup, escape

from django.db.models import Avg, Count, Exists, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify

from apps.catalog.models import EXTRA_FIELDS, ProductCollection
from apps.catalog.services import (
    collection_product_ids, include_graph, published_collections, published_products, walk_included,
)
from apps.customers.models import Customer, CustomerAddress
from apps.orders.models import Order, OrderItem

from .schemas import ALL_PRODUCTS_SETTINGS, FONTS, custom_css_problem, GENERIC_BLOCK_TYPES, SECTIONS, THEME_SETTINGS, base_page, default_template, resolve, sanitize_embed, sanitize_html
from . import cart as cart_data
from . import section_sync as sync
from .customizer import _UNSAFE as _UNSAFE_TOKEN
from .models import BlogPost, Menu, StorePage, ThemeFile, UrlRedirect
from .services import _editing_theme, get_store_theme, synced_section, theme_files

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
.page-width{max-width:max(var(--page,1200px),1600px)}
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
.card-hover--none .product-card:hover .product-card__media img{transform:none}
.card-hover--lift .product-card{transition:transform .2s ease,box-shadow .2s ease}
.card-hover--lift .product-card:hover{transform:translateY(-4px)}.card-hover--lift .product-card:hover .product-card__media img{transform:none}
.product-card__rating{font-size:13px;color:#B7791F}.product-card__rating span{color:var(--muted)}
.product-card__media{position:relative}
.product-card__badges{position:absolute;top:10px;left:10px;z-index:1;display:flex;flex-direction:column;align-items:flex-start;gap:6px}.product-card__badge{padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;background:#141b2d;color:#fff}.product-card__badge--new{background:var(--accent)}
.product-card--sold-out .product-card__media img,.product-card--sold-out .product-card__media .placeholder{opacity:.6}
@media (prefers-reduced-motion:reduce){.product-card .product-card__media img{transition:none}.product-card:hover .product-card__media img{transform:none}}
.product__stock{font-size:14px;font-weight:600;margin:-12px 0 16px}
.product__stock--few{color:#B45309}.product__stock--out{color:var(--muted)}
.product-list__more{display:flex;justify-content:center;margin-top:28px}
.pagination{display:flex;align-items:center;justify-content:center;gap:16px;margin-top:32px}
.collection-toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;margin-bottom:24px}
.product-card__tags{display:flex;flex-wrap:wrap;gap:6px}
.product-card__tags span{font-size:11px;padding:2px 8px;border-radius:999px;background:var(--soft);color:var(--muted)}
.product-card__cta{min-height:38px;padding:0 16px;font-size:14px;align-self:flex-start}
.product-card-shell{position:relative;min-width:0}.product-card-shell .product-card{width:100%}
.product-card__wishlist-form{position:absolute;z-index:2;top:calc(var(--card-padding,16px) + 9px);right:calc(var(--card-padding,16px) + 9px);margin:0}
.product-card__wishlist-form{display:flex;align-items:center;gap:6px}
.product__cart-icon{margin:-8px 0 16px}.product__cart-icon .site-header__cart{margin-left:0}
.product-card__cart-area{position:absolute;z-index:2;top:calc(var(--card-padding,16px) + 1px);left:calc(var(--card-padding,16px) + 1px);right:calc(var(--card-padding,16px) + 1px);aspect-ratio:1;pointer-events:none}
.product-card__cart-area--portrait{aspect-ratio:3/4}.product-card__cart-area--landscape{aspect-ratio:4/3}
.product-card__cart{position:absolute;right:12px;bottom:12px;display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;padding:0;border:0;border-radius:50%;background:var(--accent);color:var(--bg);cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25);text-decoration:none;pointer-events:auto;opacity:0;transform:translateY(6px);transition:opacity .2s,transform .2s}
.product-card-shell:hover .product-card__cart,.product-card__cart:focus-visible,.product-card__cart.is-added{opacity:1;transform:none}
@media (hover:none){.product-card__cart{opacity:1;transform:none}}
.product-card__cart svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.product-card__cart.is-added{background:var(--text)}
.product-card__cart:disabled{opacity:.6;cursor:wait}
.product-card__wishlist-add,.product__wishlist-add{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-width:40px;min-height:40px;padding:8px;border:0;border-radius:50%;background:var(--bg);color:var(--text);cursor:pointer;box-shadow:0 1px 5px rgba(0,0,0,.16)}
.product-card__wishlist-add{transition:transform .15s,box-shadow .15s}.product-card__wishlist-add:hover{transform:scale(1.1);box-shadow:0 2px 8px rgba(0,0,0,.25)}.product-card__wishlist-add:hover svg{stroke:#e11d48}
.product-card__wishlist-add svg,.product__wishlist-add svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.product-card__wishlist-add.is-wishlisted svg,.product-card__wishlist-add[aria-pressed="true"] svg,.product__wishlist-add.is-wishlisted svg,.product__wishlist-add[aria-pressed="true"] svg{fill:#e11d48;stroke:#e11d48}
.product__wishlist-form{margin:-8px 0 20px}.product__wishlist-add{border:1px solid var(--line);border-radius:999px}
.site-header__wishlist-count{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:18px;margin-left:4px;font-size:12px;font-weight:600}
.wishlist-page{padding:48px 0 64px}
.wishlist-page__empty{padding:48px 24px;text-align:center;border:1px solid var(--line);border-radius:var(--radius)}
.wishlist-page__empty p{color:var(--muted)}
.site-header__search,.site-header__account,.site-header__cart{position:relative;display:inline-flex;align-items:center;justify-content:center;min-width:40px;min-height:40px;margin-left:4px;color:inherit;text-decoration:none;border-radius:50%}
.site-header__search svg,.site-header__account svg,.site-header__cart svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.site-header__cart svg{width:24px;height:24px;stroke-width:1.8}
.site-header__search:hover,.site-header__account:hover,.site-header__cart:hover{background:var(--soft)}
.site-header__search-block{position:relative;display:inline-flex;align-items:center;order:-1;overflow:visible}
.site-header__search{padding:0;border:0;background:transparent;color:inherit;cursor:pointer}
.site-header__search:focus-visible,.site-header__search-input:focus-visible,.site-header__search-submit:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.site-header__search-panel{position:absolute;top:100%;right:0;left:auto;z-index:60;display:flex;gap:8px;width:min(92vw,360px);box-sizing:border-box;margin-top:8px;padding:8px;border:1px solid var(--line);border-radius:999px;background:var(--bg);color:var(--text);box-shadow:0 8px 24px rgba(0,0,0,.16)}
.site-header__search-panel[hidden]{display:none}
.tss-wrap:has(.site-header__search-block){overflow:visible;transform:none!important}
.site-header__search-input{flex:1;min-width:0;min-height:40px;padding:0 12px;border:0;border-radius:999px;background:transparent;color:inherit;font:inherit}
.site-header__search-submit{min-height:40px;padding:0 16px;border:0;border-radius:999px;background:var(--accent);color:var(--bg);font:inherit;cursor:pointer}
.site-header__cart-count{position:absolute;top:2px;right:0;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:var(--accent);color:var(--bg);font-size:11px;font-weight:600;line-height:18px;text-align:center}
.site-header__cart-count[hidden]{display:none}
.site-header__actions+.site-header__actions{margin-left:0}
/* The cart icon is 24px inside a 40px tap target: pull it out so it lines up with the page edge, like the logo does on the left */
.site-header__actions:last-child{margin-right:-8px}
html.cart-open{overflow:hidden}
.cart-drawer[hidden]{display:none}
.cart-drawer{position:fixed;inset:0;z-index:1000}
.cart-drawer__overlay{position:absolute;inset:0;background:rgba(0,0,0,.45)}
.cart-drawer__panel{position:absolute;top:0;right:0;bottom:0;width:min(420px,100%);display:flex;flex-direction:column;background:var(--bg);color:var(--text);box-shadow:-8px 0 30px rgba(0,0,0,.18)}
.cart-drawer__head{display:flex;align-items:center;justify-content:space-between;padding:18px 20px;border-bottom:1px solid var(--line)}
.cart-drawer__head h2{margin:0;font-size:20px}
.cart-drawer__close{display:inline-flex;align-items:center;justify-content:center;width:36px;height:36px;border:0;background:none;color:inherit;cursor:pointer;border-radius:50%}
.cart-drawer__close svg,.cart-line__remove svg{width:20px;height:20px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.cart-drawer__body{flex:1;overflow-y:auto;padding:0 20px 20px}
.cart-line{display:grid;grid-template-columns:72px 1fr auto;gap:14px;padding:16px 0;border-bottom:1px solid var(--line)}
.cart-line__image{display:block;width:72px;height:90px;background:var(--soft);border-radius:8px;overflow:hidden}
.cart-line__image img{width:100%;height:100%;object-fit:cover}
.cart-line__info{display:flex;flex-direction:column;gap:6px;min-width:0}
.cart-line__title{font-weight:600;text-decoration:none;color:inherit}
.cart-line__price{color:var(--muted);font-size:14px}
.cart-line__warning{color:#b42318;font-size:13px}
.cart-line__controls{display:flex;align-items:center;gap:10px;margin-top:4px}
.cart-qty{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:999px}
.cart-qty button{width:32px;height:32px;border:0;background:none;color:inherit;font-size:18px;cursor:pointer}
.cart-qty button:disabled{opacity:.35;cursor:not-allowed}
.cart-qty__value{min-width:28px;text-align:center;font-size:14px}
.cart-line__remove{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;border:0;background:none;color:var(--muted);cursor:pointer}
.cart-line__total{font-weight:600;white-space:nowrap}
.cart-summary{padding-top:18px}
.cart-summary__row{display:flex;justify-content:space-between;padding:6px 0}
.cart-summary__muted{color:var(--muted)}
.cart-summary__total{font-weight:700;font-size:18px}
.cart-summary__note{color:var(--muted);font-size:13px;margin:8px 0 14px}
.cart-summary__notice{min-height:1.2em;margin:10px 0 0;color:var(--muted);font-size:14px}
.cart-clear{display:block;margin:8px auto 0;border:0;background:none;color:var(--muted);font-size:14px;text-decoration:underline;cursor:pointer}
.cart-checkout{display:block;width:100%;text-align:center}
.cart-empty{padding:56px 0;text-align:center}
.cart-empty__title{margin:0 0 12px;font-size:24px}
.cart-empty__login{color:var(--muted);margin:0 0 20px}
.cart-page{padding:48px 0 64px}
.cart-page .cart-lines,.cart-page .cart-summary{max-width:760px}

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
@media (max-width:749px){body{font-size:var(--tss-base-size-m,var(--tss-base-size,16px))}}
.button:focus-visible,.tss-btn .button:focus-visible{outline:2px solid var(--b-focus,var(--accent));outline-offset:2px}
h1,h2,h3,h4,h5,.section-heading,.site-header__logo{font-family:var(--tss-heading-font)}
.tss-wrap[data-hfont] :is(h1,h2,h3,h4,h5,.section-heading){font-family:var(--tss-section-hfont)}
.button,.tss-btn .button{background:var(--b-bg);color:var(--b-tx);border:var(--b-bw) solid var(--b-bd);border-radius:var(--b-r);
  min-height:var(--b-mh);padding:0 var(--b-px);text-decoration:var(--b-dec);width:var(--b-w,auto);font-family:var(--b-ff);
  font-size:var(--b-fs);font-weight:var(--b-fw);text-transform:var(--b-tt);box-shadow:var(--b-sh);
  transition:background var(--b-sp) ease,color var(--b-sp) ease,border-color var(--b-sp) ease,transform var(--b-sp) ease,filter var(--b-sp) ease}
.button:hover,.tss-btn .button:hover{background:var(--b-hbg);color:var(--b-htx);border-color:var(--b-hbd);transform:var(--b-ht);filter:var(--b-hf);opacity:1}
.button:disabled,.button[disabled],.button.button--disabled,.button[aria-disabled="true"],.tss-btn .button:disabled,.tss-btn .button[disabled],.tss-btn .button.button--disabled,.tss-btn .button[aria-disabled="true"]{opacity:var(--b-dis-op,.55);cursor:not-allowed;pointer-events:none;transform:none !important;box-shadow:none !important}
.button.button--outline{background:var(--b2-bg,transparent);color:var(--b2-tx,var(--b-bd));border-color:var(--b2-bd,var(--b-bd))}
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
        "b_border": settings.get("button_border") or "",
        "b_border_width": settings.get("button_border_width", 1),
        "b_hover_bg": settings.get("button_hover_bg") or "",
        "b_hover_text": settings.get("button_hover_text") or "",
        "b_hover_border": settings.get("button_hover_border") or "",
        "b_size": settings.get("button_size") or "medium",
        "b_padding_x": settings.get("button_padding_x") or 0,
        "b_font": settings.get("button_font") or "",
        "b_font_size": settings.get("button_font_size") or 0,
        "b_weight": settings.get("button_weight") or "",
        "b_transform": settings.get("button_case") or "",
    }


def _button_vars(o, theme):
    """Every button CSS variable for override values `o` on top of the theme's defaults."""
    def pick(key):
        return o.get(key) or theme.get(key) or ""

    style = pick("b_style") or "filled"
    color = pick("b_bg") or "var(--accent)"
    text = o.get("b_text") or ""
    border = pick("b_border")

    if style == "filled":
        bg, tx, bd = color, text or theme.get("b_text") or "var(--bg)", border or color
    elif style == "outline":
        bg, tx, bd = "transparent", text or color, border or color
    elif style == "soft":
        bg, tx, bd = f"color-mix(in srgb, {color} 14%, transparent)", text or color, border or "transparent"
    else:  # text only, link style
        bg, tx, bd = "transparent", text or color, border or "transparent"

    height, padding, font_size = SIZES.get(pick("b_size") or "medium", SIZES["medium"])
    if style in ("text", "link"):
        height, padding = "auto", "0px"
    if o.get("b_height"):
        height = f"{int(o['b_height'])}px"
    if pick("b_padding_x"):
        padding = f"{int(pick('b_padding_x'))}px"
    if pick("b_font_size"):
        font_size = f"{int(pick('b_font_size'))}px"
    # A section's border width "" (its default) follows the theme's
    border_width = o.get("b_border_width")
    if border_width in (None, ""):
        border_width = theme.get("b_border_width", 1)

    hover_t, hover_f = HOVERS.get(pick("b_hover") or "darken", HOVERS["darken"])
    vars_ = {
        "--b-bg": bg, "--b-tx": tx, "--b-bd": bd,
        "--b-hbg": pick("b_hover_bg") or bg, "--b-htx": pick("b_hover_text") or tx, "--b-hbd": pick("b_hover_border") or bd,
        "--b-mh": height, "--b-px": padding, "--b-fs": font_size,
        "--b-r": RADII.get(pick("b_radius") or "pill", "999px"),
        "--b-bw": f"{int(border_width)}px",
        "--b-dec": "underline" if style == "link" else "none",
        "--b-w": "100%" if o.get("b_full_width") else "auto",
        "--b-ff": _font_stack(pick("b_font"), "inherit"),
        "--b-fw": pick("b_weight") or "600",
        "--b-tt": pick("b_transform") or "none",
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
    defaults = {"b_speed": 150, "b_gap": 12, "b_icon_position": "right", "b_shadow": "none"}
    return any(
        value not in ("", 0, False, None) and value != defaults.get(key)
        for key, value in o.items()
        if key.startswith("b_")
    )


def _style_attr(vars_):
    return ";".join(f"{k}:{v}" for k, v in vars_.items())


# Customizer tokens (Theme.theme_data["tokens"], see customizer.py) -> the CSS variables the theme uses
TOKEN_COLORS = {"primary": "--text", "accent": "--accent", "background": "--bg"}


def _token(value):
    """A saved token value, or None when it is empty or could break out of the CSS (same rule as customizer.py)."""
    value = str(value or "").strip()
    return value if value and not (_UNSAFE_TOKEN & set(value)) else None


HEADING_SPACING = {"tight": "-0.02em", "normal": "0", "wide": "0.05em", "wider": "0.1em"}
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
PANEL_KEYS = (
    "heading_color", "link_color", "border_color", "button_focus_color", "button2_bg", "button2_text", "button2_border",
    "link_underline", "body_weight", "body_line_height", "base_font_size_mobile",
    "heading_line_height", "heading_letter_spacing", "heading_case", "heading_size", "heading_size_mobile",
    "button_border", "button_disabled_opacity", "button_border_width", "button_radius",
    "price_color", "price_sale_color", "price_compare_color", "price_show_compare", "price_size", "price_size_mobile",
    "price_weight",
    "card_bg", "card_border_width", "card_border_color", "card_radius", "card_padding", "card_shadow", "card_image_ratio",
    "card_image_fit", "card_image_padding", "card_text_align", "card_title_size", "card_title_size_mobile",
    "card_title_weight", "card_zoom",
    "badge_bg", "badge_text", "badge_new_bg", "badge_corners", "sale_badge_show", "sale_badge_bg", "sale_badge_text",
    "logo_height_mobile", "page_width", "animations_enabled", "animation_speed", "button_press",
    "cart_count_bg", "cart_count_text", "drawer_width", "drawer_bg", "drawer_overlay", "search_submit_bg",
    "search_submit_text", "search_panel_width", "icon_size", "icon_stroke", "input_corners", "input_border_color",
    "input_bg", "input_focus_color", "input_height", "popover_radius", "popover_shadow", "popover_border",
)
# ":not(#tss)" adds an id's weight, so these win over the header's and the sign-in page's own (longer) rules.
# Fields on the checkout page keep the checkout's look.
_FIELD = "body:not(.template-checkout) :is(.form-input,.newsletter__input):not(#tss)"
_MENU = "body .site-header__nav :is(.site-nav__dropdown,.mega-menu,.site-nav__more):not(#tss)"
_HEADER_ICONS = ":is(.site-header__search,.site-header__account,.site-header__wishlist) svg"
_MOTION = "html.tss-js body :is(.tss-anim-fade,.tss-anim-slide-up)"
MENU_SHADOWS = {"none": "none", "strong": "0 18px 40px rgba(0,0,0,.28)"}
REVEAL_SPEEDS = {"fast": ".35s", "slow": "1.2s"}
BADGE_CORNERS = {"rounded": "6px", "square": "0px"}
_FAVICON_URL = re.compile(r"(?:/|https?://)[^\s\x22\x27<>\x5c]+")
# Where prices show: product cards and the product page (its script rebuilds the same classes), then cart lines
_PRICE = "body :is(.product-card,.product__price) .price:not(.price--compare,.price--muted)"
_SALE_PRICE = "body :is(.product-card,.product__price):has(.price--compare) .price:not(.price--compare,.price--muted)"
_COMPARE_PRICE = "body :is(.product-card,.product__price) .price--compare"
# Product cards: ".product-card.product-card" beats a preset's own "body .product-card" rules; cards in a section
# with "Card border" on keep that section's border and padding
_CARD = "body .product-card.product-card"
_CARD_UNBORDERED = "body .product-card.product-card:not(:where(.cards--bordered) *)"
_THEME_RATIO = ":not(.product-card__media--square,.product-card__media--portrait,.product-card__media--landscape)"
CARD_RATIOS = {"square": "1", "portrait": "3/4", "landscape": "4/3"}
CARD_ALIGN = {"left": "flex-start", "center": "center", "right": "flex-end"}


def _panel_css(settings):
    """
    Theme settings panel: palette, typography and button-state values as (:root variables, rules).
    Only settings changed from their default add anything, so an untouched store renders as before.
    """
    def ok(key):
        # Saved values are validated on save; check again against the schema before they go into <style>
        value, spec = settings.get(key), THEME_SETTINGS[key]
        if spec["type"] == "color_optional":
            return value if isinstance(value, str) and _HEX_COLOR.match(value) else ""
        if spec["type"] == "select":
            return value if value in spec["options"] else spec["default"]
        if spec["type"] == "checkbox":
            return value if isinstance(value, bool) else spec["default"]
        return value if isinstance(value, int) and not isinstance(value, bool) else spec["default"]

    settings = {key: ok(key) for key in PANEL_KEYS}
    vars_, rules = {}, []
    for key, var in (
        ("heading_color", "--tss-heading-color"), ("link_color", "--tss-link-color"), ("border_color", "--line"),
        ("button_focus_color", "--b-focus"), ("button2_bg", "--b2-bg"), ("button2_text", "--b2-tx"),
        ("button2_border", "--b2-bd"),
    ):
        if settings.get(key):
            vars_[var] = settings[key]
    if settings.get("heading_color"):
        rules.append(":where(h1,h2,h3,.section-heading):not(:where(.hero,.slideshow,.site-header,.site-footer,.announcement) *)"
                     "{color:var(--tss-heading-color)}")
    if settings.get("link_color"):
        rules.append(":where(main :is(p,li,blockquote)) a:where(:not(.button)){color:var(--tss-link-color)}")
    if settings.get("link_underline") in ("hover", "none"):
        hover = "underline" if settings["link_underline"] == "hover" else "none"
        rules.append(":where(main :is(p,li,blockquote)) a:where(:not(.button)){text-decoration:none}"
                     f":where(main :is(p,li,blockquote)) a:where(:not(.button)):hover{{text-decoration:{hover}}}")

    body = []
    if settings.get("body_weight") not in (None, "", "400"):
        body.append(f"font-weight:{settings['body_weight']}")
    if settings.get("body_line_height") not in (None, "", "1.55"):
        body.append(f"line-height:{settings['body_line_height']}")
    if body:
        rules.append("body{" + ";".join(body) + "}")
    if settings.get("base_font_size_mobile"):
        # A variable: TYPE_CSS (after this <style>) sets the mobile body size from it
        vars_["--tss-base-size-m"] = f"{int(settings['base_font_size_mobile'])}px"

    heading = []
    if settings.get("heading_line_height") not in (None, "", "1.1"):
        heading.append(f"line-height:{settings['heading_line_height']}")
    if settings.get("heading_letter_spacing") in HEADING_SPACING and settings["heading_letter_spacing"] != "tight":
        heading.append(f"letter-spacing:{HEADING_SPACING[settings['heading_letter_spacing']]}")
    if settings.get("heading_case") not in (None, "", "none"):
        heading.append(f"text-transform:{settings['heading_case']}")
    if heading:
        rules.append("h1,h2,h3{" + ";".join(heading) + "}")
    # "body ..." matches a preset's own rules (e.g. Bold's "body .section-heading"); this <style> comes after them.
    # A section's own heading size or button options (two classes) still win.
    if settings.get("heading_size_mobile"):
        rules.append(f"@media (max-width:749px){{body .section-heading{{font-size:{int(settings['heading_size_mobile'])}px}}}}")
    if settings.get("heading_size"):
        rules.append(f"@media (min-width:750px){{body .section-heading{{font-size:{int(settings['heading_size'])}px}}}}")
    button = []
    if settings.get("button_border"):
        button.append("border-color:var(--b-bd)")
    if settings.get("button_border_width") not in (None, 1):
        button.append("border-width:var(--b-bw)")
    if settings.get("button_radius") not in (None, THEME_SETTINGS["button_radius"]["default"]):
        button.append("border-radius:var(--b-r)")
    if button:
        rules.append("body .button{" + ";".join(button) + "}")

    if settings.get("button_disabled_opacity") not in (None, 55):
        vars_["--b-dis-op"] = f"{int(settings['button_disabled_opacity']) / 100:g}"

    price = []
    if settings.get("price_color"):
        price.append(f"color:{settings['price_color']}")
    if settings.get("price_weight"):
        price.append(f"font-weight:{settings['price_weight']}")
    if price:
        rules.append(f"{_PRICE},body .cart-line__price{{{';'.join(price)}}}")
    if settings.get("price_sale_color"):
        rules.append(f"{_SALE_PRICE}{{color:{settings['price_sale_color']}}}")
    if settings.get("price_compare_color"):
        rules.append(f"{_COMPARE_PRICE}{{color:{settings['price_compare_color']}}}")
    if settings.get("price_show_compare") is False:
        rules.append(f"{_COMPARE_PRICE}{{display:none}}")
    if settings.get("price_size_mobile"):
        rules.append(f"@media (max-width:749px){{{_PRICE}{{font-size:{int(settings['price_size_mobile'])}px}}}}")
    if settings.get("price_size"):
        rules.append(f"@media (min-width:750px){{{_PRICE}{{font-size:{int(settings['price_size'])}px}}}}")

    # Product cards. Background through --card-bg, so a section's own card background (set on the section) still wins
    if settings.get("card_bg"):
        vars_["--card-bg"] = settings["card_bg"]
        rules.append(f"{_CARD}{{background:var(--card-bg)}}")
    unbordered = []
    if settings.get("card_border_width") not in (None, 1) or settings.get("card_border_color"):
        vars_["--card-border"] = f"{int(settings['card_border_width'])}px solid {settings.get('card_border_color') or 'var(--line)'}"
        unbordered.append("border:var(--card-border)")
    if settings.get("card_padding") not in (None, 16):
        vars_["--card-padding"] = f"{int(settings['card_padding'])}px"  # the wishlist heart and cart icon follow it
        unbordered.append("padding:var(--card-padding)")
    if unbordered:
        rules.append(f"{_CARD_UNBORDERED}{{{';'.join(unbordered)}}}")
    card = []
    if settings.get("card_radius") not in (None, 6):
        card.append(f"border-radius:{int(settings['card_radius'])}px")
    if settings.get("card_shadow") in SHADOWS:
        card.append(f"box-shadow:{SHADOWS[settings['card_shadow']]}")
    if settings.get("card_text_align") in ("center", "right"):
        card.append(f"text-align:{settings['card_text_align']}")
        rules.append(f"{_CARD} :is(.price,.product-card__cta,.product-card__rating){{align-self:{CARD_ALIGN[settings['card_text_align']]}}}")
    if card:
        rules.append(f"{_CARD}{{{';'.join(card)}}}")
    if settings.get("card_image_padding") not in (None, 8):
        vars_["--card-media-padding"] = f"{int(settings['card_image_padding'])}px"
    if settings.get("card_image_fit") == "cover":
        vars_["--card-media-fit"] = "cover"
    if settings.get("card_image_ratio") in ("portrait", "landscape"):
        ratio = CARD_RATIOS[settings["card_image_ratio"]]
        rules.append(f"body .product-card__media{_THEME_RATIO}{{aspect-ratio:{ratio}}}"
                     f"body .product-card-shell:has(.product-card__media{_THEME_RATIO}) .product-card__cart-area{{aspect-ratio:{ratio}}}")
    if settings.get("card_title_weight"):
        rules.append(f"{_CARD} .product-card__title{{font-weight:{settings['card_title_weight']}}}")
    if settings.get("card_title_size_mobile"):
        rules.append(f"@media (max-width:749px){{{_CARD} .product-card__title{{font-size:{int(settings['card_title_size_mobile'])}px}}}}")
    if settings.get("card_title_size"):
        rules.append(f"@media (min-width:750px){{{_CARD} .product-card__title{{font-size:{int(settings['card_title_size'])}px}}}}")
    if settings.get("card_zoom") not in (None, 104):
        zoom = int(settings["card_zoom"])
        img = ".product-card:hover .product-card__media img"
        rules.append(f"body {img}{{transform:{f'scale({zoom / 100:g})' if zoom > 100 else 'none'}}}"
                     f"body .card-hover--none {img},body .card-hover--lift {img}{{transform:none}}"
                     f"@media (prefers-reduced-motion:reduce){{body {img}{{transform:none}}}}")

    def colours(selector, *pairs):
        decl = [f"{prop}:{settings[key]}" for key, prop in pairs if settings.get(key)]
        if decl:
            rules.append(f"{selector}{{{';'.join(decl)}}}")

    # Badges
    colours("body .product-card__badge:not(.product-card__badge--new)", ("badge_bg", "background"))
    colours("body .product-card__badge", ("badge_text", "color"))
    colours("body .product-card__badge--new", ("badge_new_bg", "background"))
    if settings.get("badge_corners") in ("rounded", "square"):
        rules.append(f"body .product-card__badge{{border-radius:{BADGE_CORNERS[settings['badge_corners']]}}}")
    if settings.get("sale_badge_show") is False:
        rules.append("body .product-card__ribbon{display:none}body .product-card__ribbon~.product-card__badges{top:10px}")
    colours("body .product-card__ribbon", ("sale_badge_bg", "background"), ("sale_badge_text", "color"))

    # Logo (the header sets the image height on the image itself) and page
    if settings.get("logo_height_mobile"):
        rules.append(f"@media (max-width:749px){{body .site-header__logo img{{height:{int(settings['logo_height_mobile'])}px!important}}}}")
    if settings.get("page_width"):
        rules.append(f"body .page-width{{max-width:{max(int(settings['page_width']), 600)}px}}")

    # Animations: reveal speed, or every motion effect off
    if settings.get("animation_speed") in REVEAL_SPEEDS:
        rules.append(f"{_MOTION}{{transition-duration:{REVEAL_SPEEDS[settings['animation_speed']]}}}")
    if settings.get("animations_enabled") is False:
        rules.append(f"{_MOTION}{{opacity:1;transform:none;transition:none}}body .tss-marquee__track{{animation:none}}"
                     "body .product-card:hover .product-card__media img{transform:none}"
                     "body .product-card .product-card__media img{transition:none}")

    # Buttons: pressed state (hover border is a button variable, see _button_vars)
    if settings.get("button_press") == "shrink":
        rules.append('body .button:active:not(:disabled,[aria-disabled="true"]){transform:scale(.97)}'
                     '@media (prefers-reduced-motion:reduce){body .button:active{transform:none}}')

    # Cart, drawers, search
    colours("body .site-header__cart-count", ("cart_count_bg", "background"), ("cart_count_text", "color"))
    drawer = [f"background:{settings['drawer_bg']}"] if settings.get("drawer_bg") else []
    if settings.get("drawer_width") not in (None, 420):
        drawer.append(f"width:min({int(settings['drawer_width'])}px,100%)")
    if drawer:
        rules.append(f"body .cart-drawer__panel{{{';'.join(drawer)}}}")
    if settings.get("drawer_overlay") not in (None, 45):
        rules.append(f"body .cart-drawer__overlay{{background:rgba(0,0,0,{int(settings['drawer_overlay']) / 100:g})}}")
    colours("body .site-header__search-submit", ("search_submit_bg", "background"), ("search_submit_text", "color"))
    if settings.get("search_panel_width") not in (None, 360):
        rules.append(f"body .site-header__search-panel{{width:min(92vw,{int(settings['search_panel_width'])}px)}}")

    # Header icons (the cart icon is drawn 4px larger)
    icons, cart_icon = [], []
    if settings.get("icon_size") not in (None, 20):
        size = int(settings["icon_size"])
        icons += [f"width:{size}px", f"height:{size}px"]
        cart_icon += [f"width:{size + 4}px", f"height:{size + 4}px"]
    if settings.get("icon_stroke") in ("1.5", "2.5"):
        icons.append(f"stroke-width:{settings['icon_stroke']}")
        cart_icon.append(f"stroke-width:{float(settings['icon_stroke']) - .2:g}")
    if icons:
        rules.append(f"body {_HEADER_ICONS}{{{';'.join(icons)}}}body .site-header__cart svg{{{';'.join(cart_icon)}}}")

    # Input fields
    field = []
    if settings.get("input_corners") in RADII:
        field.append(f"border-radius:{RADII[settings['input_corners']]}")
    if settings.get("input_border_color"):
        field.append(f"border-color:{settings['input_border_color']}")
    if settings.get("input_bg"):
        field.append(f"background:{settings['input_bg']}")
    if settings.get("input_height"):
        field.append(f"height:{int(settings['input_height'])}px;min-height:0")
    if field:
        rules.append(f"{_FIELD}{{{';'.join(field)}}}")
    if settings.get("input_focus_color"):
        focus = settings["input_focus_color"]
        rules.append(f"{_FIELD}:is(:focus,:focus-visible){{border-color:{focus};outline-color:{focus}}}")

    # Header menus (dropdown and mega menu)
    menu = []
    if settings.get("popover_radius") not in (None, 8):
        menu.append(f"border-radius:{int(settings['popover_radius'])}px")
    if settings.get("popover_shadow") in MENU_SHADOWS:
        menu.append(f"box-shadow:{MENU_SHADOWS[settings['popover_shadow']]}")
    if settings.get("popover_border") is False:
        menu.append("border-color:transparent")
    if menu:
        rules.append(f"{_MENU}{{{';'.join(menu)}}}")
    return vars_, rules


_SECTION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _section_css(section_id, s):
    """A section's own custom CSS when it is safe to use (checked again: it may come from Edit code)."""
    css = str(s.get("custom_css") or "").strip()
    if css and len(css) <= 500 and _SECTION_ID.fullmatch(str(section_id)) and custom_css_problem(css) is None:
        return css
    return ""


def store_custom_css(settings):
    """Theme settings > Custom CSS as a <style> tag for storefront pages (never checkout or the admin)."""
    css = str(settings.get("custom_css") or "").strip()
    if not css or len(css) > 1500 or custom_css_problem(css):
        return Markup("")
    return Markup(f'<style data-tss-custom-css>{css}</style>')


def theme_css(settings, tokens=None):
    """
    :root variables for the theme's fonts, base size and button defaults. Saved customizer
    tokens (colours, heading/body font, button radius) take the place of the matching Theme
    settings; this <style> comes after styles.liquid's :root, so its --text, --accent and --bg win.
    """
    root = {
        "--tss-heading-font": _font_stack(settings.get("heading_font"), FONT_BY_KEY["system"][0]),
        "--tss-body-font": _font_stack(settings.get("body_font"), FONT_BY_KEY["system"][0]),
        "--tss-base-size": f"{int(settings.get('base_font_size') or 16)}px",
        **_button_vars({}, _theme_button(settings)),
        # The theme's button colour whatever the button style; presets use it for their highlight colour
        "--tss-button-color": settings.get("button_bg") if _HEX_COLOR.match(str(settings.get("button_bg") or "")) else "var(--accent)",
        "--tss-button-text": settings.get("button_text") if _HEX_COLOR.match(str(settings.get("button_text") or "")) else "var(--bg)",
    }
    panel_vars, panel_rules = _panel_css(settings)
    root.update(panel_vars)
    tokens = tokens or {}
    colors, typography, buttons = (tokens.get(g) or {} for g in ("colors", "typography", "buttons"))
    for name, var in TOKEN_COLORS.items():
        if _token(colors.get(name)):
            root[var] = _token(colors[name])
    for name, var in (("heading_font", "--tss-heading-font"), ("body_font", "--tss-body-font")):
        if _token(typography.get(name)):
            # A font key of the theme ("serif", ...) becomes its stack; anything else is used as written
            root[var] = _font_stack(_token(typography[name]), _token(typography[name]))
    if _token(buttons.get("radius")):
        root["--b-r"] = RADII.get(_token(buttons["radius"]), _token(buttons["radius"]))
    # Every value comes from fixed tables, validated colours/numbers or checked tokens, so it can't break out of <style>
    return Markup(f"<style>:root{{{_style_attr(root)}}}{''.join(panel_rules)}</style>")


CUSTOMER_AUTH_CSS = Markup("""<style>
body .auth-page.auth-page{display:flex;justify-content:center;align-items:center;padding:40px 0 56px;color:var(--text);font-family:var(--tss-body-font);}
body .auth-page.auth-page .page-width{width:100%;}
body .auth-page.auth-page .customer-auth{width:100%;max-width:420px;margin:0 auto;text-align:left;}
body .auth-page.auth-page .customer-card{box-sizing:border-box;width:100%;padding:32px;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:var(--b-r);box-shadow:none;font:inherit;line-height:1.5;letter-spacing:normal;text-transform:none;}
body .auth-page.auth-page .customer-card__header{margin:0 0 22px;text-align:left;}
body .auth-page.auth-page .customer-card__header svg,body .auth-page.auth-page .customer-card__icon,body .auth-page.auth-page .customer-card__badge,body .auth-page.auth-page .auth-icon-badge{display:none!important;}
body .auth-page.auth-page .customer-card__title{margin:0 0 6px;color:var(--text);font-family:var(--tss-heading-font);font-size:clamp(24px,3vw,30px);font-weight:600;line-height:1.2;letter-spacing:normal;text-transform:none;}
body .auth-page.auth-page .customer-card__subtitle{margin:0;color:var(--muted);font:inherit;font-size:14px;}
body .auth-page.auth-page .customer-auth-tabs{display:flex;gap:22px;margin:0 0 22px;border-bottom:1px solid var(--line);}
body .auth-page.auth-page .customer-auth-tab{flex:0 0 auto;min-height:40px;margin:0;padding:8px 0;border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent;color:var(--muted);font:inherit;font-size:14px;text-decoration:none;}
body .auth-page.auth-page .customer-auth-tab.is-active{border-bottom-color:var(--accent);color:var(--text);}
body .auth-page.auth-page .customer-form{display:grid;gap:16px;}
body .auth-page.auth-page .form-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
body .auth-page.auth-page .form-group{display:grid;gap:6px;margin:0;min-width:0;}
body .auth-page.auth-page .form-group--checkbox{display:flex;align-items:center;}
body .auth-page.auth-page .form-label{color:var(--text);font:inherit;font-size:14px;font-weight:500;letter-spacing:normal;text-transform:none;}
body .auth-page.auth-page .form-label-opt{color:var(--muted);font-weight:400;}
body .auth-page.auth-page .form-input{box-sizing:border-box;display:block;width:100%;min-width:0;height:46px;margin:0;padding:0 12px;border:1px solid var(--line);border-radius:var(--b-r);background:var(--bg);box-shadow:none;color:var(--text);font:inherit;font-size:15px;font-weight:400;letter-spacing:normal;text-transform:none;}
body .auth-page.auth-page .form-input::placeholder{color:var(--muted);opacity:1;}
body .auth-page.auth-page .form-input:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-color:var(--accent);box-shadow:none;}
body .auth-page.auth-page .password-wrapper{position:relative;display:flex;align-items:center;width:100%;}
body .auth-page.auth-page .password-wrapper .form-input{padding-right:44px;}
body .auth-page.auth-page .password-toggle{position:absolute;right:6px;display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;padding:6px;border:0;border-radius:0;background:transparent;color:var(--muted);cursor:pointer;}
body .auth-page.auth-page .password-toggle:hover{background:transparent;color:var(--text);}
body .auth-page.auth-page .password-toggle:focus-visible,body .auth-page.auth-page .auth-link:focus-visible,body .auth-page.auth-page .auth-link-subtle:focus-visible,body .auth-page.auth-page .customer-auth-tab:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
body .auth-page.auth-page .checkbox-label{display:inline-flex;align-items:center;gap:8px;color:var(--text);font:inherit;font-size:14px;cursor:pointer;}
body .auth-page.auth-page .checkbox-label input[type=checkbox]{flex:none;width:16px;height:16px;margin:0;accent-color:var(--accent);cursor:pointer;}
body .auth-page.auth-page .button.customer-card__submit{display:flex;align-items:center;justify-content:center;width:100%;min-height:46px;margin:2px 0 0;padding:0 18px;border:var(--b-bw) solid var(--b-bd);border-radius:var(--b-r);background:var(--b-bg);box-shadow:none;color:var(--b-tx);font:inherit;font-weight:600;letter-spacing:normal;text-transform:none;cursor:pointer;}
body .auth-page.auth-page .button.customer-card__submit:hover{background:var(--b-hbg);border-color:var(--b-hbd);color:var(--b-htx);}
body .auth-page.auth-page .button.customer-card__submit:disabled{opacity:.6;cursor:default;}
body .auth-page.auth-page .customer-card__footer{display:grid;gap:10px;margin:22px 0 0;padding:16px 0 0;border-top:1px solid var(--line);background:transparent;color:var(--muted);font:inherit;font-size:13px;text-align:left;}
body .auth-page.auth-page .customer-card__footer p{margin:0;}
body .auth-page.auth-page .auth-link{color:var(--accent);font:inherit;font-weight:500;text-decoration:underline;text-underline-offset:2px;}
body .auth-page.auth-page .auth-link-subtle{color:var(--muted);font:inherit;font-size:13px;}
body .auth-page.auth-page .alert{display:block;margin:0 0 16px;padding:12px;border:1px solid var(--line);border-radius:var(--b-r);background:var(--soft);color:var(--text);font:inherit;font-size:14px;line-height:1.45;}
body .auth-page.auth-page .alert--error,body .auth-page.auth-page .alert--success{border-color:var(--accent);background:var(--soft);color:var(--text);}
body .auth-page.auth-page .auth-field-error{color:var(--accent)!important;font-size:13px;}
@media(max-width:520px){body .auth-page.auth-page{padding:24px 0 36px;}body .auth-page.auth-page .customer-card{padding:24px 20px;}body .auth-page.auth-page .form-row{grid-template-columns:minmax(0,1fr);gap:16px;}}
</style>""")


def customer_auth_content(content, mode):
    """Use the active theme variables for account pages, ignoring template-local fixed styling."""
    if "data-auth-form" in str(content):
        # The auth blocks (snippets/auth-*.liquid) bring their own theme-aware styles and the Sign in / Create account tabs
        return Markup(content)
    markup = re.sub(r"<style\b[^>]*>.*?</style\s*>", "", str(content), flags=re.IGNORECASE | re.DOTALL)
    if mode == "login":
        heading = re.search(r"<h1\b", markup, flags=re.IGNORECASE)
        if heading:
            before_heading = markup[:heading.start()]
            before_heading = re.sub(
                r'''<a\b(?=[^>]*\bhref=["'][^"']*/account/register(?:[/?#][^"']*)?["'])[^>]*>\s*Create\s+account\s*</a\s*>''',
                "", before_heading, count=1, flags=re.IGNORECASE | re.DOTALL,
            )
            markup = before_heading + markup[heading.start():]
    return Markup(markup + str(CUSTOMER_AUTH_CSS))


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
.tss-menu ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.tss-menu--horizontal ul{flex-direction:row;flex-wrap:wrap;gap:8px 20px}
.tss-balign-center .tss-menu--horizontal ul{justify-content:center}.tss-balign-right .tss-menu--horizontal ul{justify-content:flex-end}
.tss-menu a{color:inherit;text-decoration:none}.tss-menu a:hover{text-decoration:underline}
.tss-menu__heading{font-size:16px;margin:0 0 10px}
.tss-marquee{overflow:hidden;white-space:nowrap}
.tss-marquee__track{display:inline-flex;animation:tss-marquee var(--tss-marquee-s,20s) linear infinite}
.tss-marquee__track span{padding-right:3em;font-weight:600}
.tss-marquee--pause:hover .tss-marquee__track{animation-play-state:paused}
@keyframes tss-marquee{to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){.tss-marquee__track{animation:none}}
.tss-jumbo{margin:0;font-weight:800;line-height:1.05;font-family:var(--tss-heading-font)}
.tss-jumbo--large{font-size:clamp(32px,6vw,56px)}.tss-jumbo--xlarge{font-size:clamp(44px,9vw,96px)}.tss-jumbo--huge{font-size:clamp(56px,13vw,160px)}
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
# Common Storefront Search Controller: unified handling for existing search boxes
SEARCH_JS = Markup(r"""<script>
(function () {
  function initCommonSearch() {
    // 1. Header search block(s) across any theme
    var searchBlocks = document.querySelectorAll('.site-header__search-block, [data-header-' + 'search]');
    searchBlocks.forEach(function (block) {
      if (block._searchBound) return;
      block._searchBound = true;
      var toggle = block.querySelector('.site-header__search');
      var panel = block.querySelector('.site-header__search-panel');
      var input = block.querySelector('.site-header__search-input, input[type="search"], input[name="q"]');
      var form = block.querySelector('form');

      function executeSearch() {
        if (!input) return;
        var q = input.value.trim();
        input.value = q;
        if (form) {
          // If in theme preview iframe, communicate with parent editor
          if (window.parent && window.parent !== window && window.parent.postMessage) {
            var actionUrl = form.getAttribute('action') || '/collections/all';
            var cleanPath = (actionUrl.replace(/^https?:\/\/[^/]+/, '').replace(/^\/s\/[^/]+/, '') || '/collections/all').split('?')[0];
            var path = q ? cleanPath + '?q=' + encodeURIComponent(q) : cleanPath;
            window.parent.postMessage({
              source: 'tss-preview',
              type: 'navigate',
              path: path
            }, '*');
            return;
          }
          if (typeof form.requestSubmit === 'function') {
            form.requestSubmit();
          } else {
            form.submit();
          }
        }
      }

      // If user clicks the magnifying glass when panel is open, execute search!
      if (toggle && panel && input) {
        toggle.addEventListener('click', function (e) {
          if (!panel.hidden) {
            e.preventDefault();
            e.stopPropagation();
            executeSearch();
            return;
          }
        });
      }

      if (input) {
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') {
            e.preventDefault();
            executeSearch();
          }
        });
      }

      if (form) {
        form.addEventListener('submit', function (e) {
          var q = input ? input.value.trim() : '';
          input.value = q;
          if (window.parent && window.parent !== window && window.parent.postMessage) {
            e.preventDefault();
            var actionUrl = form.getAttribute('action') || '/collections/all';
            var cleanPath = (actionUrl.replace(/^https?:\/\/[^/]+/, '').replace(/^\/s\/[^/]+/, '') || '/collections/all').split('?')[0];
            var path = q ? cleanPath + '?q=' + encodeURIComponent(q) : cleanPath;
            window.parent.postMessage({
              source: 'tss-preview',
              type: 'navigate',
              path: path
            }, '*');
          }
        });
      }
    });

    // 2. Collection page search forms across any theme
    var colForms = document.querySelectorAll('.collection-search, form[role="search"]:not(.site-header__search-panel)');
    colForms.forEach(function (form) {
      if (form._searchBound) return;
      form._searchBound = true;
      var input = form.querySelector('input[type="search"], input[name="q"]');
      var btn = form.querySelector('button[type="submit"], button.button');

      function executeColSearch() {
        if (!input) return;
        var q = input.value.trim();
        input.value = q;
        if (window.parent && window.parent !== window && window.parent.postMessage) {
          var actionUrl = form.getAttribute('action') || window.location.pathname;
          var cleanPath = (actionUrl.replace(/^https?:\/\/[^/]+/, '').replace(/^\/s\/[^/]+/, '') || '/collections/all').split('?')[0];
          var path = q ? cleanPath + '?q=' + encodeURIComponent(q) : cleanPath;
          window.parent.postMessage({
            source: 'tss-preview',
            type: 'navigate',
            path: path
          }, '*');
          return;
        }
        if (typeof form.requestSubmit === 'function') {
          form.requestSubmit();
        } else {
          form.submit();
        }
      }

      if (input) {
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') {
            e.preventDefault();
            executeColSearch();
          }
        });
      }

      if (btn) {
        btn.addEventListener('click', function (e) {
          e.preventDefault();
          executeColSearch();
        });
      }

      form.addEventListener('submit', function (e) {
        var q = input ? input.value.trim() : '';
        input.value = q;
        if (window.parent && window.parent !== window && window.parent.postMessage) {
          e.preventDefault();
          var actionUrl = form.getAttribute('action') || window.location.pathname;
          var cleanPath = (actionUrl.replace(/^https?:\/\/[^/]+/, '').replace(/^\/s\/[^/]+/, '') || '/collections/all').split('?')[0];
          var path = q ? cleanPath + '?q=' + encodeURIComponent(q) : cleanPath;
          window.parent.postMessage({
            source: 'tss-preview',
            type: 'navigate',
            path: path
          }, '*');
        }
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initCommonSearch);
  } else {
    initCommonSearch();
  }
})();
</script>""")

def _stock_config_script(settings):
    oos_flag = "true" if settings.get("disable_out_of_stock_add_to_cart", True) else "false"
    oos_label = escape(str(settings.get("sold_out_button_label") or "Sold out"))
    return Markup(f'<script id="tss-stock-config" type="application/json">{{"disable_out_of_stock": {oos_flag}, "sold_out_label": "{oos_label}"}}</script>')


STOCK_JS = Markup("""<script>
(function () {
  function getStockConfig(sec) {
    var cfgEl = sec ? sec.querySelector("[data-stock-config]") : null;
    if (cfgEl) {
      return {
        disable_out_of_stock: cfgEl.getAttribute("data-disable-oos") !== "false",
        sold_out_label: cfgEl.getAttribute("data-sold-out-label") || "Sold out"
      };
    }
    var globalEl = document.getElementById("tss-stock-config");
    if (globalEl && globalEl.textContent) {
      try {
        var parsed = JSON.parse(globalEl.textContent);
        return {
          disable_out_of_stock: parsed.disable_out_of_stock !== false,
          sold_out_label: parsed.sold_out_label || "Sold out"
        };
      } catch (e) {}
    }
    return { disable_out_of_stock: true, sold_out_label: "Sold out" };
  }

  function syncFormStock(form) {
    if (!form) return;
    var sec = form.closest("[data-tss-" + "section]") || form.closest("section") || document;
    var config = getStockConfig(sec);
    var add = form.querySelector("[data-add]");
    if (!add) return;
    var qty = form.querySelector("[data-quantity]");

    var select = form.querySelector("select[data-variant], select[name='variant']");
    var isOut = false;
    var avail = 99;

    if (select && select.selectedOptions && select.selectedOptions[0]) {
      var opt = select.selectedOptions[0];
      var availAttr = opt.getAttribute("data-variant-available");
      avail = availAttr != null ? Number(availAttr) : 0;
      var contAttr = opt.getAttribute("data-variant-continue");
      var cont = contAttr === "true";
      isOut = (avail <= 0) && !cont;
    } else {
      if (add.hasAttribute("data-sold-out-initial") || add.disabled || add.getAttribute("data-sold-out") === add.textContent.trim()) {
        isOut = true;
      }
    }

    if (config.disable_out_of_stock) {
      if (isOut) {
        add.disabled = true;
        add.setAttribute("aria-disabled", "true");
        add.classList.add("button--disabled");
        var soldOutText = add.getAttribute("data-sold-out") || config.sold_out_label || "Sold out";
        if (add.textContent !== soldOutText) {
          add.textContent = soldOutText;
        }
        if (qty) qty.disabled = true;
      } else {
        add.disabled = false;
        add.removeAttribute("aria-disabled");
        add.classList.remove("button--disabled");
        var labelText = add.getAttribute("data-label") || "Add to cart";
        if (add.textContent !== labelText) {
          add.textContent = labelText;
        }
        if (qty) {
          qty.disabled = false;
          qty.max = (avail > 0 && avail < 99) ? avail : 99;
        }
      }
    } else {
      add.disabled = false;
      add.removeAttribute("aria-disabled");
      add.classList.remove("button--disabled");
      if (qty) qty.disabled = false;
    }
  }

  function initStock() {
    document.querySelectorAll("form[data-cart-add]").forEach(function (form) {
      var add = form.querySelector("[data-add]");
      if (add && add.disabled) {
        add.setAttribute("data-sold-out-initial", "true");
      }
      syncFormStock(form);
    });

    var globalCfg = getStockConfig(null);
    document.querySelectorAll(".product-card__cta[data-sold-out]").forEach(function (cta) {
      if (globalCfg.disable_out_of_stock) {
        cta.setAttribute("aria-disabled", "true");
        cta.classList.add("button--disabled");
      } else {
        cta.removeAttribute("aria-disabled");
        cta.classList.remove("button--disabled");
      }
    });
  }

  document.addEventListener("change", function (e) {
    var select = e.target && e.target.closest && e.target.closest("select[data-variant], select[name='variant']");
    if (select) {
      var form = select.closest("form[data-cart-add]") || select.closest("form");
      if (form) syncFormStock(form);
    }
  });

  document.addEventListener("click", function (e) {
    var tier = e.target && e.target.closest && e.target.closest(".product__tier");
    if (tier) {
      var sec = tier.closest("section") || document;
      var select = sec.querySelector("select[data-variant], select[name='variant']");
      if (select) {
        setTimeout(function () {
          var form = select.closest("form[data-cart-add]") || select.closest("form");
          if (form) syncFormStock(form);
        }, 0);
      }
    }
  });

  document.addEventListener("submit", function (e) {
    var form = e.target && e.target.closest && e.target.closest("form[data-cart-add]");
    if (!form) return;
    var add = form.querySelector("[data-add]");
    if (add && (add.disabled || add.getAttribute("aria-disabled") === "true" || add.classList.contains("button--disabled"))) {
      e.preventDefault();
      e.stopImmediatePropagation();
      return false;
    }
  }, true);

  document.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest && e.target.closest("[data-add], [data-card-add], .product-card__cta[data-sold-out]");
    if (!btn) return;
    if (btn.disabled || btn.getAttribute("aria-disabled") === "true" || btn.classList.contains("button--disabled")) {
      e.preventDefault();
      e.stopImmediatePropagation();
      return false;
    }
  }, true);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initStock);
  } else {
    initStock();
  }

  if (typeof MutationObserver !== "undefined") {
    var timer = null;
    var observer = new MutationObserver(function () {
      if (timer) clearTimeout(timer);
      timer = setTimeout(initStock, 40);
    });
    if (document.body) {
      observer.observe(document.body, { childList: true, subtree: true });
    }
  }

  if (window.addEventListener) {
    window.addEventListener("message", function (event) {
      if (event.data && typeof event.data === "object") {
        if (event.data.type === "section:load" || event.data.type === "settings:change" || (event.data.source && event.data.source.indexOf("editor") !== -1)) {
          setTimeout(initStock, 50);
        }
      }
    });
  }
})();
</script>""")




def shop_url(store):
    return f"/s/{store.store_slug}"


def _resolve_link(base_url, value):
    """Store-relative links ("/collections/all") point inside this store."""
    value = str(value or "")
    return base_url + value if value.startswith("/") and not value.startswith("//") else value


def _menu_lines(text):
    """[(label, url)] from "Label | /page" lines (already validated by the menu setting)."""
    lines = []
    for line in str(text or "").splitlines():
        label, _, url = line.partition("|")
        label, url = label.strip(), url.strip()
        if label and not url:  # a bare link is its own label
            url = label
        if label and url:
            lines.append((label, url))
    return lines


def _menu_links(text, base_url, dead=()):
    """[{"label", "url"}] from "Label | /page" lines, without the links in `dead`."""
    return [
        {"label": label, "url": _resolve_link(base_url, url)} for label, url in _menu_lines(text) if url not in dead
    ]


def redirect_forms(store, path):
    """(the plain path, every way a redirect from `path` may have been saved): with or without /s/<store> and a final /."""
    base = shop_url(store)
    relative = path[len(base):] if path.startswith(base) else path
    plain = "/" + relative.strip("/")
    forms = {plain} if plain == "/" else {plain, plain + "/"}
    return plain, sorted(forms | {base + f for f in forms})


_PRODUCT_LINK = re.compile(r"^/products/([^/?#]+)/?(?:[?#].*)?$")
_COLLECTION_LINK = re.compile(r"^/collections/([^/?#]+)/?(?:[?#].*)?$")


def dead_menu_urls(store, urls):
    """
    The links among `urls` (as saved in a menu) that go to a product or collection of this store that shoppers
    can't open: deleted, Draft, Archived or off the Online Store. A link whose address has a redirect in this store
    is not dead: following it still ends at a page. Other links are never judged. Two queries for any number of
    links, three when some of them are dead.
    """
    base = shop_url(store)
    products, collections = {}, {}
    for url in set(urls):
        path = str(url or "").strip()
        path = path[len(base):] if path.startswith(base + "/") else path
        found = _PRODUCT_LINK.match(path)
        if found:
            products[url] = found.group(1)
            continue
        found = _COLLECTION_LINK.match(path)
        if found and found.group(1) not in ("all", "promotions"):  # the built-in pages are always there
            collections[url] = found.group(1)

    def as_uuid(value):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None

    dead = set()
    ids = {as_uuid(v) for v in products.values()} - {None}
    live = set(published_products(store).prefetch_related(None).filter(pk__in=ids).values_list("pk", flat=True)) if ids else set()
    dead.update(url for url, value in products.items() if as_uuid(value) not in live)

    if collections:
        ids = {as_uuid(v) for v in collections.values()} - {None}
        handles = {v for v in collections.values() if as_uuid(v) is None}
        rows = list(
            published_collections(store).filter(Q(pk__in=ids) | Q(handle__in=handles)).values_list("pk", "handle")
        )
        live_ids, live_handles = {pk for pk, _ in rows}, {handle for _, handle in rows}
        for url, value in collections.items():
            number = as_uuid(value)
            if (number not in live_ids) if number is not None else (value not in live_handles):
                dead.add(url)

    if dead:
        by_form = {}
        for url in dead:
            path = str(url or "").strip()
            path = path[len(base):] if path.startswith(base + "/") else path
            for form in redirect_forms(store, urlsplit(path).path)[1]:
                by_form.setdefault(form, set()).add(url)
        for form in UrlRedirect.objects.filter(store=store, path__in=list(by_form)).values_list("path", flat=True):
            dead -= by_form[form]
    return dead


NAVBAR_MENU_HANDLES = ("main-menu", "header", "navbar", "main")
NOT_NAVBAR = ("footer", "account")


def navbar_menu_links(store, base_url):
    """
    [{"label", "url", ...}] from the store's navbar menu in Admin > Menus, [] when it has none.
    The menu is the one with handle main-menu/header/navbar/main, else the oldest that is not a
    footer or account menu. Supports 3-level menu hierarchy (main links, columns, sublinks),
    mega menus, badges, and promotional banners.
    """
    menus = list(Menu.objects.filter(store=store).prefetch_related("items").order_by("created_at"))
    menu = next((m for h in NAVBAR_MENU_HANDLES for m in menus if m.handle == h), None) or next(
        (m for m in menus if not any(w in f"{m.name} {m.handle}".lower() for w in NOT_NAVBAR)), None
    )
    if menu is None:
        return []

    all_items = list(menu.items.all())
    urls_to_check = [i.url for i in all_items if i.url]
    urls_to_check += [i.promo_url for i in all_items if getattr(i, "promo_url", None)]
    dead = dead_menu_urls(store, urls_to_check)

    by_parent = {}
    for item in all_items:
        by_parent.setdefault(item.parent_id, []).append(item)
    for pid in by_parent:
        by_parent[pid].sort(key=lambda i: (i.order, i.created_at))

    top_items = by_parent.get(None, [])
    result = []
    for top in top_items:
        if not top.title:
            continue
        children_l2 = by_parent.get(top.id, [])
        top_url_dead = top.url in dead
        if (top_url_dead or not top.url) and not children_l2:
            continue

        top_url = "" if top_url_dead else _resolve_link(base_url, top.url)
        top_raw = "" if top_url_dead else (top.url or "")

        columns = []
        for l2 in children_l2:
            if not l2.title:
                continue
            children_l3 = by_parent.get(l2.id, [])
            l2_url_dead = l2.url in dead
            if (l2_url_dead or not l2.url) and not children_l3:
                continue

            l2_url = "" if l2_url_dead else _resolve_link(base_url, l2.url)
            l2_raw = "" if l2_url_dead else (l2.url or "")

            links = []
            for l3 in children_l3:
                if not l3.title or l3.url in dead:
                    continue
                links.append({
                    "id": l3.id,
                    "label": l3.title,
                    "url": _resolve_link(base_url, l3.url),
                    "raw": l3.url,
                    "badge": l3.badge or "",
                })

            columns.append({
                "id": l2.id,
                "label": l2.title,
                "url": l2_url,
                "raw": l2_raw,
                "badge": l2.badge or "",
                "links": links,
            })

        has_promo = bool(top.promo_image or top.promo_title or top.promo_subtitle)
        promo = None
        if has_promo:
            promo_url = top.promo_url or ""
            resolved_promo_url = "" if promo_url in dead else _resolve_link(base_url, promo_url)
            promo = {
                "image": top.promo_image or "",
                "title": top.promo_title or "",
                "subtitle": top.promo_subtitle or "",
                "url": resolved_promo_url,
                "raw_url": promo_url,
            }

        is_mega = bool(top.is_mega)
        has_dropdown = len(columns) > 0
        result.append({
            "id": top.id,
            "label": top.title,
            "url": top_url,
            "raw": top_raw,
            "badge": top.badge or "",
            "is_mega": is_mega,
            "has_dropdown": has_dropdown,
            "columns": columns,
            "promo": promo,
        })
    return result


def layout_data(settings, base_url, nav, menu_links=None, dead=()):
    """Header links, footer links and footer social links for layout/theme.liquid."""
    header = _menu_links(settings.get("header_menu"), base_url, dead) or list(menu_links or [])
    if not header:
        header = [{"label": "Home", "url": f"{base_url}/"}, {"label": "Shop all", "url": f"{base_url}/collections/all"}]
        # The automatic list: collections with something to buy in them
        filled = [c for c in nav if c.get("has_products", True)]
        header += [{"label": c["name"], "url": c["url"]} for c in filled[: int(settings.get("header_collections") or 0)]]

    footer = _menu_links(settings.get("footer_menu"), base_url, dead) or [
        {"label": "Home", "url": f"{base_url}/"},
        {"label": "Shop all", "url": f"{base_url}/collections/all"},
    ]
    social = [
        {"name": name, "url": settings[f"footer_{key}"]}
        for key, name in SOCIAL_NAMES.items()
        if settings.get(f"footer_{key}")
    ]
    return {"header_links": header, "footer_links": footer, "footer_social": social}


DEFAULT_CURRENCY_SYMBOL = THEME_SETTINGS["currency_symbol"]["default"]  # what the theme setting holds until changed
CURRENCY_SYMBOLS = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "KRW": "₩", "AUD": "A$", "CAD": "C$",
    "NZD": "NZ$", "SGD": "S$", "HKD": "HK$", "MXN": "MX$", "BRL": "R$", "ZAR": "R", "AED": "AED ", "SAR": "SAR ",
    "RUB": "₽", "TRY": "₺", "THB": "฿", "PHP": "₱", "VND": "₫", "NGN": "₦", "ILS": "₪", "PLN": "zł ",
}


def currency_symbol(store, theme_symbol):
    """
    The symbol prices are shown with. It follows the store's currency. The theme's "Currency symbol" setting
    wins only when the owner changed it from its default, so existing stores look the same.
    """
    if theme_symbol and theme_symbol != DEFAULT_CURRENCY_SYMBOL:
        return theme_symbol
    code = (store.currency or "").strip().upper()
    return CURRENCY_SYMBOLS.get(code, f"{code} " if code else DEFAULT_CURRENCY_SYMBOL)


class _ParsedOnceLoader(DictLoader):
    """A DictLoader that parses each template once (the source never changes under a cached environment)."""

    def __init__(self, templates):
        super().__init__(templates)
        self._parsed = {}

    def load(self, env, name, *, globals=None, context=None, **kwargs):  # noqa: A002
        # {% render %} passes the tag and the render context; they don't change which source is loaded
        template = self._parsed.get(name)
        if template is None:
            template = self._parsed[name] = super().load(env, name, globals=globals, context=context, **kwargs)
        return template


# Environments by what they load (a digest of every file) and the shop's url and currency, so a page is parsed
# once, not on every render. Editing any file changes the digest, so the next render builds a fresh one.
_ENVIRONMENTS = {}
_ENVIRONMENTS_MAX = 64


def _environment(files, base_url, currency, overrides=None):
    loader = {}
    for path, content in files.items():
        loader[path] = content
        # Snippets can be rendered by name: {% render 'product-card' %}
        if path.startswith("snippets/") and path.endswith(".liquid"):
            loader[path[len("snippets/"):-len(".liquid")]] = content
    # e.g. the footer snippet name pointing at the synced footer section's output
    loader.update(overrides or {})

    digest = hashlib.sha1()
    for path in sorted(loader):
        digest.update(f"{path}|{loader[path]}|".encode("utf-8"))
    key = (digest.hexdigest(), base_url, currency)
    cached = _ENVIRONMENTS.get(key)
    if cached is not None:
        return cached

    env = Environment(loader=_ParsedOnceLoader(loader), autoescape=True)

    def money(value):
        if value is None or value == "":
            return ""
        return f"{currency}{Decimal(str(value)):,.2f}"

    def link(value):
        return _resolve_link(base_url, value)

    env.filters["money"] = money
    env.filters["link"] = link
    if len(_ENVIRONMENTS) >= _ENVIRONMENTS_MAX:
        _ENVIRONMENTS.pop(next(iter(_ENVIRONMENTS)), None)
    _ENVIRONMENTS[key] = env
    return env


def _catalog(store):
    """Published products with what the product cards can show (tags, rating, category)."""
    shown = Q(reviews__status__in=SHOWN_REVIEWS)
    return published_products(store).select_related("category").prefetch_related("product_tags__tag").annotate(
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
PAGE_SIZE = 20
SORTS = {
    "newest": None,  # the catalog's own order
    "price-asc": lambda d: (d["price"] is None, d["price"] or 0),
    "price-desc": lambda d: (d["price"] is None, -(d["price"] or 0)),
}


def _number(value):
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and number >= 0 else None


def _with_category_ids(store, filters):
    """Category filters given by name (older links) are turned into ids: the links built from them then keep working."""
    wanted = filters.get("category") or []
    if not wanted:
        return filters
    from apps.catalog.models import Category

    by_name = {}
    for pk, name in Category.objects.filter(store=store).values_list("pk", "name"):
        by_name.setdefault(name.strip().lower(), []).append(str(pk))
    ids = []
    for value in wanted:
        for found in by_name.get(value.strip().lower(), [value]):
            if found not in ids:
                ids.append(found)
    return {**filters, "category": ids}


def parse_collection_filters(params):
    """?available=1&stock_status=in_stock|out_of_stock&category=&min_rating=&min_price=&max_price=&vendor=&tag= -> filter dict."""
    many = params.getlist if hasattr(params, "getlist") else lambda k: [params[k]] if params.get(k) else []

    stock_status = set(many("stock_status"))
    if params.get("in_stock") == "1" or params.get("available") == "1":
        stock_status.add("in_stock")
    if params.get("out_of_stock") == "1":
        stock_status.add("out_of_stock")

    rating_vals = [_number(r) for r in many("rating") + many("min_rating") if _number(r) is not None]
    min_rating = min(rating_vals) if rating_vals else None

    return {
        "stock_status": sorted(stock_status),
        "in_stock": "in_stock" in stock_status,
        "out_of_stock": "out_of_stock" in stock_status,
        "available": "in_stock" in stock_status and "out_of_stock" not in stock_status,
        "category": [c for c in many("category") if c],
        "min_rating": min_rating,
        "min_price": _number(params.get("min_price", "")),
        "max_price": _number(params.get("max_price", "")),
        "vendor": [v for v in many("vendor") if v],
        "tag": [t for t in many("tag") if t],
        # ?q= : the words typed in the header's search box
        "q": " ".join(str(params.get("q") or "").split())[:100],
    }


def _matches_filters(product, f, skip=None):
    """Whether a product passes the filters, ignoring the group named in `skip` (for facet counts)."""
    # 1. Stock Status: In stock / Out of stock
    # If both selected or neither selected: do not restrict products by stock status
    if skip not in ("available", "stock_status"):
        in_stock = f.get("in_stock")
        out_of_stock = f.get("out_of_stock")
        if in_stock and not out_of_stock:
            if product["is_sold_out"]:
                return False
        elif out_of_stock and not in_stock:
            if not product["is_sold_out"]:
                return False

    # 2. Category filtering
    if skip != "category" and f.get("category"):
        cat_id = product.get("category_id")
        cat_name = product.get("category_name")
        if not (cat_id in f["category"] or (cat_name and cat_name in f["category"])):
            return False

    # 3. Customer Rating filtering
    if skip != "rating" and f.get("min_rating") is not None:
        rating = product.get("rating")
        if rating is None or rating < f["min_rating"]:
            return False

    # 4. Price range filtering
    if skip != "price":
        price = product["price"]
        if f["min_price"] is not None and (price is None or price < f["min_price"]):
            return False
        if f["max_price"] is not None and (price is None or price > f["max_price"]):
            return False

    # 5. Vendor
    if skip != "vendor" and f["vendor"] and product["vendor"] not in f["vendor"]:
        return False

    # 6. Tag
    if skip != "tag" and f["tag"] and not set(f["tag"]) & set(product["tags"]):
        return False

    # 7. Search: matches product title, vendor, product type, or tags
    if f.get("q"):
        query = str(f["q"]).strip().lower()
        if query:
            words = query.split()
            title_lower = (product.get("title") or "").lower()
            haystack = " ".join([
                title_lower,
                (product.get("vendor") or "").lower(),
                (product.get("product_type") or "").lower(),
                *(str(t).lower() for t in product.get("tags") or []),
            ])
            # Partial match on title substring or all search words present in product data
            if not (query in title_lower or all(w in haystack for w in words)):
                return False

    return True


def _filter_query(f, sort):
    """The query string (no page) that keeps the current filters and sort in links."""
    pairs = [("sort", sort)]
    for s in f.get("stock_status", []):
        pairs.append(("stock_status", s))
    for c in f.get("category", []):
        pairs.append(("category", c))
    if f.get("min_rating") is not None:
        pairs.append(("min_rating", f["min_rating"]))
    if f["min_price"] is not None:
        pairs.append(("min_price", f["min_price"]))
    if f["max_price"] is not None:
        pairs.append(("max_price", f["max_price"]))
    pairs += [("vendor", v) for v in f["vendor"]] + [("tag", t) for t in f["tag"]]
    if f.get("q"):
        pairs.append(("q", f["q"]))
    return urlencode(pairs)


# "Only a few left" at this quantity or lower, unless the store's product page sets its own (see _few_left_threshold)
FEW_LEFT = 5
_few_left_at = ContextVar("few_left_at", default=FEW_LEFT)


def _few_left_threshold(data):
    """The product page's "Only a few left" setting (low_stock_threshold, default 5): cards use the same number."""
    template = (data.get("templates") or {}).get("product") or default_template("product")
    for section in (template.get("sections") or {}).values():
        if section.get("type") == "main-product":
            try:
                return max(int((section.get("settings") or {}).get("low_stock_threshold")), 1)
            except (TypeError, ValueError):
                break
    return FEW_LEFT
NEW_PRODUCT_DAYS = 14
MAX_BADGES = 2


def product_badges(tags, sold_out, few_left, is_new):
    """
    The labels a product card shows over its image, most important first: "Sold out", "Few left",
    the merchant's own (a tag written "badge:Best seller" shows "Best seller") and "New" for
    products added in the last NEW_PRODUCT_DAYS days.
    """
    badges = []
    if sold_out:
        badges.append("Sold out")
    elif few_left:
        badges.append("Few left")
    for tag in tags:
        label = tag[6:].strip() if tag.lower().startswith("badge:") else ""
        if label and label not in badges:
            badges.append(label[:24])
    if is_new and "New" not in badges:
        badges.append("New")
    return badges[:MAX_BADGES]


def _available(variant):
    """What shoppers can buy of one variant: max(sum of available over all locations, 0), as inventory.sellable."""
    return max(sum(item.available for item in variant.inventory_items.all()), 0)


def product_drop(product, base_url):
    images = [i.url for i in sorted(product.images.all(), key=lambda i: i.position)]
    variants = sorted(product.variants.all(), key=lambda v: v.created_at)
    prices = [v.price for v in variants]
    # On sale when the cheapest variant has a compare-at price above its price
    cheapest = min(variants, key=lambda v: v.price) if variants else None
    compare_at = cheapest.compare_at_price if cheapest else None
    on_sale = compare_at is not None and compare_at > cheapest.price
    if not on_sale:
        compare_at = None
    stock = {v.id: _available(v) for v in variants}
    available_total = sum(stock.values())
    # A variant that keeps selling when out of stock is never "sold out"
    selling_on = any(v.continue_selling for v in variants)
    sold_out = available_total <= 0 and not selling_on

    tags = [pt.tag.name for pt in product.product_tags.all()]
    few_left = 0 < available_total <= _few_left_at.get()
    created = getattr(product, "created_at", None)
    is_new = bool(created) and (timezone.now() - created).days < NEW_PRODUCT_DAYS

    category_id = str(product.category_id) if getattr(product, "category_id", None) else ""
    category_name = product.category.name if getattr(product, "category", None) else ""

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
        "compare_at_price": compare_at,
        "on_sale": on_sale,
        "images": images,
        "featured_image": images[0] if images else None,
        "tags": tags,
        "badges": product_badges(tags, sold_out, few_left, is_new),
        "is_new": is_new,
        "category_id": category_id,
        "category_name": category_name,
        # Stock for the templates: a number and a true/false. Quantity 0 (or no stock record) is sold out.
        "available_total": available_total,
        "is_sold_out": sold_out,
        "few_left": few_left,
        "sold_out": sold_out,  # older name, kept for theme copies that still use it
        "rating": round(float(product.avg_rating), 1) if getattr(product, "avg_rating", None) else None,
        "review_count": getattr(product, "review_count", 0) or 0,
        "variants": [
            {
                "id": str(v.id), "sku": v.sku or "", "price": v.price, "available_total": stock[v.id],
                # The "was" price, only while it is above the price
                "compare_at_price": v.compare_at_price if v.compare_at_price is not None and v.compare_at_price > v.price else None,
                "on_sale": v.compare_at_price is not None and v.compare_at_price > v.price,
                "continue_selling": v.continue_selling,
                "is_sold_out": stock[v.id] <= 0 and not v.continue_selling,
            }
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


def _clean_text(html_text, max_len=None):
    """Strip HTML tags safely, unescape HTML entities, and collapse whitespace."""
    if not html_text:
        return ""
    text = re.sub(r"<(br|p|div|li)[^>]*>", " ", str(html_text), flags=re.IGNORECASE)
    cleaned = " ".join(html.unescape(strip_tags(text)).split())
    if max_len and len(cleaned) > max_len:
        return cleaned[:max_len]
    return cleaned


def _product_json_ld(product, product_drop_, store, canonical_url, request=None):
    """Schema.org Product JSON-LD structured data."""
    if not product or not product_drop_:
        return Markup("")

    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": product.title,
    }

    desc = (product.seo_description or "").strip()
    if not desc and product.description:
        desc = _clean_text(product.description, 5000)
    else:
        desc = _clean_text(desc, 5000)
    if desc:
        data["description"] = desc

    images = []
    for img_url in product_drop_.get("images", []):
        if img_url:
            if img_url.startswith(("http://", "https://")):
                images.append(img_url)
            elif request:
                images.append(request.build_absolute_uri(img_url))
            else:
                images.append(img_url)
    if images:
        data["image"] = images

    if product.vendor:
        data["brand"] = {
            "@type": "Brand",
            "name": product.vendor,
        }

    first_variant = product.variants.order_by("created_at").first()
    if first_variant and first_variant.sku:
        data["sku"] = first_variant.sku

    if product_drop_.get("price") is not None:
        data["offers"] = {
            "@type": "Offer",
            "price": str(product_drop_["price"]),
            "priceCurrency": (getattr(store, "currency", None) or "USD").upper(),
            "availability": (
                "https://schema.org/OutOfStock"
                if product_drop_.get("is_sold_out")
                else "https://schema.org/InStock"
            ),
            "url": canonical_url,
        }

    avg_rating = getattr(product, "avg_rating", None)
    review_count = getattr(product, "review_count", 0)
    if avg_rating and review_count:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": str(round(float(avg_rating), 1)),
            "reviewCount": str(review_count),
        }

    json_str = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return Markup(f'<script type="application/ld+json">{json_str}</script>')



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
        # Its own products and those of the collections it includes, each product once
        qs = qs.filter(pk__in=collection_product_ids(collection))
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
    if _section_css(section_id, s):
        extra_attrs_css = f' data-tss-css="{section_id}"'
    else:
        extra_attrs_css = ""

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

    extra_attrs = extra_attrs_css
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
    schema = SECTIONS.get(section_type, {}).get("settings", {})
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

    if kind == "group":
        parts = _render_blocks(block.get("children") or [], store, env, base_url, nested=True)
        if not parts:
            return ""
        direction = "row" if b.get("direction") == "horizontal" else "column"
        align = b.get("items_align") if b.get("items_align") in ("start", "center", "end", "stretch") else "start"
        justify = b.get("justify") if b.get("justify") in ("start", "center", "end", "space-between") else "start"
        style = (
            f"display:flex;flex-direction:{direction};flex-wrap:wrap;gap:{int(b.get('gap') or 0)}px;align-items:{align};"
            f"justify-content:{justify};padding:{int(b.get('padding') or 0)}px;border-radius:{int(b.get('radius') or 0)}px"
        )
        if b.get("background"):
            style += f";background:{escape(b['background'])}"
        return f'<div class="tss-group" style="{style}">{"".join(parts)}</div>'

    if kind == "icon":
        size = int(b.get("size") or 40)
        svg = (
            f'<svg viewBox="0 0 24 24" width="{size}" height="{size}" fill="none" stroke="currentColor" stroke-width="1.7" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICON_PATHS.get(b.get("icon"), ICON_PATHS["star"])}</svg>'
        )
        if b.get("link"):
            svg = f'<a href="{escape(link(b["link"]))}" style="color:inherit">{svg}</a>'
        return f'<div class="tss-icon" style="color:var(--accent)">{svg}</div>'

    if kind == "menu":
        rows = []
        for line in (b.get("links") or "").splitlines():
            label, _, url = line.partition("|")
            label, url = label.strip(), (url or label).strip()
            if label and url:
                rows.append(f'<li><a href="{escape(link(url))}">{escape(label)}</a></li>')
        if not rows:
            return '<div class="tss-empty">Add menu links.</div>'
        heading = f'<h3 class="tss-menu__heading">{escape(b["heading"])}</h3>' if b.get("heading") else ""
        direction = "horizontal" if b.get("direction") == "horizontal" else "vertical"
        return f'<nav class="tss-menu tss-menu--{direction}">{heading}<ul>{"".join(rows)}</ul></nav>'

    if kind == "marquee" and b.get("text"):
        item = f'<span>{escape(b["text"])}</span>'
        pause = " tss-marquee--pause" if b.get("pause_on_hover", True) else ""
        return (
            f'<div class="tss-marquee{pause}" style="--tss-marquee-s:{int(b.get("speed") or 20)}s">'
            f'<div class="tss-marquee__track">{item * 6}</div></div>'
        )

    if kind == "logo" and (b.get("image_url") or b.get("text")):
        inner = (
            f'<img src="{escape(b["image_url"])}" alt="{escape(b.get("text") or "")}" style="width:{int(b.get("width") or 120)}px;max-width:100%;height:auto">'
            if b.get("image_url") else f'<strong>{escape(b["text"])}</strong>'
        )
        return f'<a class="tss-logo" href="{escape(link(b.get("link") or "/"))}" style="color:inherit;text-decoration:none">{inner}</a>'

    if kind in ("collection_card", "collection_link"):
        ids = (b.get("collections") or [])[:1]
        found = published_collections(store).filter(id__in=ids).first() if store and ids else None
        if not found:
            return '<div class="tss-empty">Choose a collection.</div>'
        href, name = escape(collection_url(base_url, found)), escape(b.get("label") or found.name)
        if kind == "collection_link":
            return f'<a class="tss-collection-link" href="{href}">{name}</a>'
        bg = f' style="background:url({escape(b["image_url"])}) center/cover;color:#fff"' if b.get("image_url") else ""
        return f'<a class="tss-collection" href="{href}"{bg}>{name}</a>'

    if kind == "product_card":
        products = _chosen_products(store, base_url, (b.get("products") or [])[:1])
        if not products:
            return '<div class="tss-empty">Choose a product.</div>'
        try:
            return env.from_string("{% render 'product-card', product: product, show_vendor: false %}").render(product=products[0])
        except LiquidError:
            return f'<a href="{escape(products[0]["url"])}">{escape(products[0]["title"])}</a>'

    if kind == "jumbo_text" and b.get("text"):
        size = b.get("size") if b.get("size") in ("large", "xlarge", "huge") else "xlarge"
        return f'<p class="tss-jumbo tss-jumbo--{size}">{escape(b["text"])}</p>'

    if kind == "html" and b.get("html"):
        return f'<div class="tss-embed">{sanitize_embed(b["html"])}</div>'

    return ""


def _render_blocks(blocks, store, env, base_url, theme=None, nested=False):
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
        marker = "" if nested else f' data-tss-block="{int(index)}"'
        parts.append(
            f'<div class="{escape(" ".join(classes))}" style="{escape(";".join(style))}"{marker}>{inner}</div>'
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


# Line icons for the "Why SipNow" features (24x24, drawn with the icon colour)
WHY_SIPNOW_ICON_PATHS = {
    "award": '<circle cx="12" cy="9" r="6.5"/><path d="M12 5.6l1.05 2.15 2.35.34-1.7 1.66.4 2.35L12 11l-2.1 1.1.4-2.35-1.7-1.66 2.35-.34z"/><path d="M8.2 14.3L7 21l5-2.6 5 2.6-1.2-6.7"/>',
    "bolt": '<path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z"/>',
    "tag": '<path d="M20.6 13.4l-7.2 7.2a2 2 0 0 1-2.83 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "bars": '<path d="M5 21V8M12 21V4M19 21V10M3 21h18"/><path d="M3 8h4M10 4h4M17 10h4"/>',
    "truck": '<path d="M2 6h11v10H2zM13 9h4l3 3v4h-7"/><circle cx="6" cy="17.5" r="1.8"/><circle cx="16.5" cy="17.5" r="1.8"/>',
    "heart": '<path d="M12 20s-7.5-4.6-7.5-10A4.3 4.3 0 0 1 12 7.3 4.3 4.3 0 0 1 19.5 10c0 5.4-7.5 10-7.5 10z"/>',
    "gift": '<path d="M3 8h18v4H3zM5 12v9h14v-9M12 8v13"/><path d="M12 8C10 4 6.5 4.5 7.5 7c.4 1 2.5 1 4.5 1zM12 8c2-4 5.5-3.5 4.5-1-.4 1-2.5 1-4.5 1z"/>',
    "shield": '<path d="M12 2.5l8 3v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10v-6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    "bottle": '<path d="M10 2h4v4.2c0 .9.5 1.6 1.2 2.2A4.5 4.5 0 0 1 17 12v8.5a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 7 20.5V12a4.5 4.5 0 0 1 1.8-3.6c.7-.6 1.2-1.3 1.2-2.2z"/><path d="M7 14h10M7 18.5h10"/>',
    "glass": '<path d="M7 3h10l-.6 6a4.4 4.4 0 0 1-8.8 0zM12 13.5V21M8 21h8"/>',
}

WHY_SIPNOW_CSS = """<style>
.why-sipnow{background:var(--ws-section);text-align:center}
.why-sipnow .page-width{padding-top:80px;padding-bottom:80px}
.why-sipnow .why-sipnow__eyebrow{margin:0 0 12px;font-family:"Inter",system-ui,sans-serif;font-size:12px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--ws-eyebrow)}
.why-sipnow .why-sipnow__heading{margin:0;font-family:Georgia,serif;font-size:56px;font-weight:700;line-height:1.1;color:var(--ws-heading);text-wrap:balance}
.why-sipnow .why-sipnow__sub{margin:14px auto 0;max-width:560px;font-family:"Inter",system-ui,sans-serif;font-size:16px;line-height:1.6;color:var(--ws-body)}
.why-sipnow__panel{margin:44px auto 0;max-width:1180px;padding:42px 34px;background:var(--ws-panel);border-radius:20px;box-shadow:0 12px 35px rgba(62,35,42,.10);display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:28px;text-align:center}
.why-sipnow__item{display:flex;flex-direction:column;align-items:center;min-width:0}
.why-sipnow__icon{width:54px;height:54px;border-radius:10px;background:var(--ws-icon-bg);color:var(--ws-icon);display:grid;place-items:center}
.why-sipnow__icon svg{width:28px;height:28px}
.why-sipnow .why-sipnow__title{margin:18px 0 0;font-family:"Inter",system-ui,sans-serif;font-size:18px;font-weight:700;line-height:1.3;color:var(--ws-heading)}
.why-sipnow .why-sipnow__text{margin:8px 0 0;font-family:"Inter",system-ui,sans-serif;font-size:14px;line-height:1.6;color:var(--ws-body)}
@media (max-width:749px){
.why-sipnow .page-width{padding-top:56px;padding-bottom:56px}
.why-sipnow .why-sipnow__heading{font-size:40px}
.why-sipnow__panel{grid-template-columns:repeat(2,minmax(0,1fr));padding:32px 20px;gap:28px 16px;margin-top:32px}
}
</style>"""


def _render_why_sipnow(section):
    """The "Why SipNow" section: eyebrow, heading, sub-text, then four icon features in a panel."""
    s = section["settings"]
    colours = {
        "--ws-section": s["section_background"], "--ws-panel": s["panel_background"],
        "--ws-heading": s["heading_colour"], "--ws-body": s["body_colour"], "--ws-eyebrow": s["eyebrow_colour"],
        "--ws-icon-bg": s["icon_background"], "--ws-icon": s["icon_colour"],
    }
    style = ";".join(f"{name}:{value}" for name, value in colours.items())

    items = []
    for n in range(1, 5):
        title, text = s.get(f"feature_{n}_title") or "", s.get(f"feature_{n}_text") or ""
        if not title and not text:
            continue
        icon = WHY_SIPNOW_ICON_PATHS.get(s.get(f"feature_{n}_icon"), "")
        items.append(
            '<div class="why-sipnow__item">'
            '<div class="why-sipnow__icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icon}</svg></div>'
            f'<h3 class="why-sipnow__title">{escape(title)}</h3>'
            f'<p class="why-sipnow__text">{escape(text)}</p></div>'
        )

    eyebrow = f'<p class="why-sipnow__eyebrow">{escape(s["eyebrow"])}</p>' if s.get("eyebrow") else ""
    heading = f'<h2 class="why-sipnow__heading">{escape(s["heading"])}</h2>' if s.get("heading") else ""
    sub = f'<p class="why-sipnow__sub">{escape(s["subheading"])}</p>' if s.get("subheading") else ""
    panel = f'<div class="why-sipnow__panel">{"".join(items)}</div>' if items else ""
    return Markup(
        f'{google_fonts_link({"inter"})}{WHY_SIPNOW_CSS}'
        f'<section id="section-{escape(section["id"])}" class="section why-sipnow" style="{escape(style)}">'
        f'<div class="page-width">{eyebrow}{heading}{sub}{panel}</div></section>'
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
    """
    The store's published collections that have at least one published product in them (one query; one more only
    when a collection includes others). This is the `collections` the header's automatic menu lists; a menu the
    owner built is not affected by it.
    """
    filled = ProductCollection.objects.filter(
        collection=OuterRef("pk"), product__in=published_products(store).prefetch_related(None).order_by().values("pk")
    )
    published =list(published_collections(store).annotate(own_products=Exists(filled)))
    # A collection that includes others is also filled when one of them is (a second query, only then)
    unfilled = [c for c in published if not c.own_products and c.include_collections]
    reached, with_products = {}, set()
    if unfilled:
        graph = include_graph(store.pk)
        reached = {c.pk: walk_included(c.pk, c.include_collections, graph) for c in unfilled}
        with_products = {
            str(pk) for pk in ProductCollection.objects.filter(
                collection__store=store, collection_id__in={i for ids in reached.values() for i in ids},
                product__in=published_products(store).prefetch_related(None).order_by().values("pk"),
            ).values_list("collection_id", flat=True).distinct()
        }
    shown = [
        c for c in published
        if c.own_products or any(i in with_products for i in reached.get(c.pk, ()))
    ]
    return [
        {"name": c.name, "url": collection_url(base_url, c), "has_products": True}
        for c in shown[:20]
    ]


def _env_key(files, base_url, currency):
    """What a compiled section depends on besides its own file: the snippets it can render."""
    snippets = sorted((p, hashlib.sha256(c.encode("utf-8")).hexdigest()) for p, c in files.items() if p.startswith("snippets/"))
    return hashlib.sha256(json.dumps([base_url, currency, snippets]).encode("utf-8")).hexdigest()


def _synced_html(store, store_theme, name, section, data, env, env_key, context, editor):
    raw = (data.get("sections") or {}).get(name)
    if name == "header":
        # Links from Admin > Menus fill the navbar while the header has no menu links of its own
        values = raw if raw is not None else section["values"]
        blocks = list(values.get("blocks") or [])
        # Suppress stale or hand-edited duplicate Search entries while the upgrade
        # cleans up their saved values.
        unique_blocks, seen_search = [], False
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "search":
                if seen_search:
                    continue
                seen_search = True
            unique_blocks.append(block)
        if unique_blocks != blocks:
            values = dict(values, blocks=unique_blocks)
            raw = values
        has_custom_links = any(b.get("type") == "link" and not b.get("hidden") for b in blocks)
        context["has_custom_links"] = has_custom_links
        if not has_custom_links and context.get("menu_links"):
            link = [{"type": "link", "settings": {"label": m["label"], "url": m["raw"]}} for m in context["menu_links"]]
            raw = dict(values, blocks=link + unique_blocks)
    return sync.render_section(
        name, theme_id=store_theme.theme_id, content=section["content"],
        values=raw if raw is not None else section["values"], env=env, env_key=env_key,
        context=context, editor=editor, link=lambda url: _resolve_link(shop_url(store), url),
    )


def _template_slots(content, page):
    """The page template with its {% section %} lines as slots, or None (not a synced template)."""
    synced = [name for name, config in sync.SYNCED_TEMPLATES.items() if config["page"] == page]
    if not synced or content is None or sync.is_legacy_template(content):
        return None
    try:
        return sync.template_markup(content, sync.template_tags(content))
    except sync.SectionCodeError:
        return None  # unreadable lines (saving refuses them): show the sections the usual way


def _fill_slots(rendered, order, pieces, visible, styles, scripts):
    """
    Each slot takes the next section of the page's order (a hidden one leaves it empty), so the
    page shows the order being edited even before the file is saved. Sections without a slot
    (more sections than lines, or the empty page's default sections) follow the last one.
    """
    queue = list(order)
    placed = set()

    def fill(_match):
        if not queue:
            return ""
        sid = queue.pop(0)
        placed.add(sid)
        return pieces.get(sid, "")

    body = re.sub(re.escape(sync.SLOT), fill, rendered)
    rest = "\n".join(pieces[sid] for sid in visible if sid in pieces and sid not in placed)
    return "\n".join(part for part in (styles, body, rest, scripts) if part)


def render_synced_section_fast(store, name, custom_data):
    """
    One synced section only (the customizer's live preview while editing it):
    a single database read for its code, and its compiled template from the cache.
    Returns None when it can't be done this way (the caller renders it the full way).
    """
    path = sync.SYNCED_SECTIONS[name]["path"]
    files = ThemeFile.objects.filter(file_path=path, theme__store_assignments__store=store)
    editing = _editing_theme.get()
    if editing:
        files = files.filter(theme_id=editing)  # the draft being edited
    else:
        files = files.filter(theme__store_assignments__is_active=True)
    row = files.values("theme_id", "content").first()
    if row is None:
        return None
    data = resolve(custom_data)
    settings = data["settings"]
    settings["currency_symbol"] = currency_symbol(store, settings["currency_symbol"])
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


# ---------------------------------------------------------------- pages
#
# Pages (About us, Shipping policy, ...) are written in the admin (StorePage); /pages/<handle> shows
# a visible one. The customizer previews the page template with a sample page until the store has one.

SAMPLE_PAGE = {
    "title": "About us",
    "handle": "about-us",
    "body": "<p>Tell your customers who you are. This is a sample page: write your own in Pages.</p>"
            "<h2>Our story</h2><p>Share how the store started and what you care about.</p>"
            "<h2>Get in touch</h2><p>Add your contact details, opening hours or shipping policy here.</p>",
}


def published_pages(store):
    """The store's pages that shoppers can read: visible ones."""
    return StorePage.objects.filter(store=store, status=StorePage.VISIBLE)


def _page_drop(page, base_url):
    """A saved page as the theme sees `page`."""
    return {
        "id": str(page.pk),
        "title": page.title,
        "handle": page.handle,
        "url": f"{base_url}/pages/{page.handle}",
        "body": Markup(sanitize_embed(page.body or "")),
        "published_at": page.published_at,
        "updated_at": page.updated_at,
        "date": _date_text(page.updated_at),
        "seo_title": page.seo_title,
        "seo_description": page.seo_description,
    }


def _sample_page_drop(base_url):
    now = timezone.now()
    return {
        "id": SAMPLE_PAGE["handle"], "title": SAMPLE_PAGE["title"], "handle": SAMPLE_PAGE["handle"],
        "url": f"{base_url}/pages/{SAMPLE_PAGE['handle']}",
        "body": Markup(sanitize_embed(SAMPLE_PAGE["body"])),
        "published_at": now, "updated_at": now, "date": _date_text(now), "seo_title": "", "seo_description": "",
    }


def page_content(store, base_url, handle, editor, template="page"):
    """
    The page to show: the visible one with `handle` (else, in the preview, the first one that uses
    `template`, then the first one); a sample if none.
    """
    pages = published_pages(store)
    page = pages.filter(handle=handle).first() if handle else None
    if page is None and editor:
        ordered = pages.order_by("created_at")
        page = ordered.filter(template="" if template == "page" else template).first() or ordered.first()
    if page is not None:
        return _page_drop(page, base_url)
    return _sample_page_drop(base_url) if editor else None


def render_cart_html(store, data, opts):
    """The cart's lines and totals as HTML (the drawer's body, and the cart page), from snippets/cart.liquid."""
    store_theme = get_store_theme(store)
    files = theme_files(store_theme)
    base_url = shop_url(store)
    currency = currency_symbol(store, resolve(store_theme.custom_data)["settings"]["currency_symbol"])
    env = _environment(files, base_url, currency)
    return env.from_string("{% render 'cart', mode: 'lines', cart: cart, settings: opts %}").render(
        shop={"name": store.store_name, "url": base_url}, cart=data, opts=opts,
    )


def _customer_drop(customer, user, store, base_url):
    """A customer drop for Liquid: name, email, phone, addresses, orders."""
    if customer is None:
        return None
    addresses = list(CustomerAddress.objects.filter(customer=customer).order_by("-is_default", "-created_at"))
    orders_qs = Order.objects.filter(store=store, customer=customer).prefetch_related("items", "items__product").order_by("-created_at")

    def format_address(addr):
        return {
            "id": str(addr.pk),
            "address_type": addr.address_type,
            "first_name": addr.first_name or "",
            "last_name": addr.last_name or "",
            "name": f"{addr.first_name or ''} {addr.last_name or ''}".strip(),
            "company": addr.company or "",
            "address1": addr.address_line_1,
            "address2": addr.address_line_2 or "",
            "city": addr.city,
            "state": addr.state,
            "postal_code": addr.postal_code,
            "country": addr.country,
            "phone": addr.phone or "",
            "is_default": addr.is_default,
        }

    def format_order(ord_obj):
        items = []
        for itm in ord_obj.items.all():
            items.append({
                "id": str(itm.pk),
                "title": itm.product.title if itm.product else "Product",
                "quantity": itm.quantity,
                "price": itm.price_per_unit,
                "total_price": itm.total_price,
                "product_url": f"{base_url}/products/{itm.product.pk}" if itm.product else "#",
            })
        return {
            "id": str(ord_obj.pk),
            "order_number": ord_obj.order_number,
            "created_at": ord_obj.created_at,
            "date": _date_text(ord_obj.created_at),
            "status": ord_obj.status,
            "fulfillment_status": ord_obj.fulfillment_status,
            "total": ord_obj.total,
            "items": items,
            "items_count": sum(i["quantity"] for i in items),
        }

    addr_drops = [format_address(a) for a in addresses]
    default_addr = next((a for a in addr_drops if a["is_default"]), addr_drops[0] if addr_drops else None)
    order_drops = [format_order(o) for o in orders_qs]

    return {
        "id": str(customer.pk),
        "name": f"{customer.first_name or ''} {customer.last_name or ''}".strip() or "Customer",
        "first_name": customer.first_name or "",
        "last_name": customer.last_name or "",
        "email": customer.email,
        "phone": (user.phone if user else "") or "",
        "addresses": addr_drops,
        "addresses_count": len(addr_drops),
        "default_address": default_addr,
        "orders": order_drops,
        "orders_count": len(order_drops),
    }


# The store has no country setting, so its country and dialling code follow its currency
_STORE_COUNTRY_BY_CURRENCY = {
    "INR": ("India", "+91"),
    "AUD": ("Australia", "+61"),
    "USD": ("United States", "+1"),
    "GBP": ("United Kingdom", "+44"),
    "CAD": ("Canada", "+1"),
    "NZD": ("New Zealand", "+64"),
    "SGD": ("Singapore", "+65"),
    "AED": ("United Arab Emirates", "+971"),
}


def _sample_customer_drop(base_url):
    """Realistic customer data for the theme customizer preview."""
    sample_address = {
        "id": "sample-addr-1",
        "address_type": "shipping",
        "first_name": "Jane",
        "last_name": "Doe",
        "name": "Jane Doe",
        "company": "",
        "address1": "123 Market Street",
        "address2": "Suite 400",
        "city": "Sydney",
        "state": "NSW",
        "postal_code": "2000",
        "country": "Australia",
        "phone": "+61 400 000 000",
        "is_default": True,
    }
    sample_orders = [
        {
            "id": "sample-order-1",
            "order_number": "#1001",
            "created_at": timezone.now(),
            "date": _date_text(timezone.now()),
            "status": "paid",
            "fulfillment_status": "fulfilled",
            "total": Decimal("145.00"),
            "items": [
                {
                    "id": "sample-item-1",
                    "title": "Signature Canvas Tote",
                    "quantity": 1,
                    "price": Decimal("85.00"),
                    "total_price": Decimal("85.00"),
                    "product_url": "#",
                },
                {
                    "id": "sample-item-2",
                    "title": "Minimalist Water Bottle",
                    "quantity": 1,
                    "price": Decimal("60.00"),
                    "total_price": Decimal("60.00"),
                    "product_url": "#",
                },
            ],
            "items_count": 2,
        }
    ]
    return {
        "id": "sample-customer",
        # Preview-only account data: forms (checkout) must never pre-fill from it
        "is_sample": True,
        "name": "Jane Doe",
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane.doe@example.com",
        "phone": "+61 400 000 000",
        "addresses": [sample_address],
        "addresses_count": 1,
        "default_address": sample_address,
        "orders": sample_orders,
        "orders_count": 1,
    }


CUSTOMER_LINK_SCRIPT = r"""<script>
(function () {
  // The page is sandboxed and may not send the sign-in cookie: carry the signed-in token in the store's own links and forms
  var token = %(token)s, base = %(base)s, here = new URL(location.href);
  function ours(url) {
    try {
      var u = new URL(url, here);
      return u.origin === here.origin && (u.pathname === base || u.pathname.indexOf(base + "/") === 0) ? u : null;
    } catch (e) { return null; }
  }
  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a[href]");
    if (!link) return;
    var href = link.getAttribute("href");
    if (href.charAt(0) === "#") return;
    var u = ours(href);
    if (!u || u.searchParams.has("ct") || /\/account\/logout\/?$/.test(u.pathname)) return;
    u.searchParams.set("ct", token);
    link.setAttribute("href", u.pathname + u.search + u.hash);
  }, true);
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.getAttribute || (form.method || "get").toLowerCase() !== "post") return;
    var u = ours(form.getAttribute("action") || here.href);
    if (!u || form.querySelector('input[name="ct"]')) return;
    var input = document.createElement("input");
    input.type = "hidden"; input.name = "ct"; input.value = token;
    form.appendChild(input);
  }, true);
  try {
    if (here.searchParams.has("ct")) {
      here.searchParams.delete("ct");
      history.replaceState(null, "", here.pathname + here.search + here.hash);
    }
  } catch (e) { /* keep the token in the address bar */ }
})();
</script>"""


def _with_customer_link(html, token, base_url):
    """A signed-in shopper's token goes along in links and forms (see CUSTOMER_LINK_SCRIPT)."""
    script = CUSTOMER_LINK_SCRIPT % {
        "token": json.dumps(token).replace("</", "<\\/"),
        "base": json.dumps(base_url).replace("</", "<\\/"),
    }
    index = html.lower().rfind("</body>")
    return html + script if index < 0 else html[:index] + script + html[index:]


def _match_quiz_links(store, base_url, blocks):
    """
    "Find your match quiz" (priority order): each answer's collection setting becomes answer_N_link_url and
    answer_N_link_title for the template. The setting is a collection's id, handle or name; a path (/collections/x)
    or a web address is kept as it is, and a collection that is not (yet) published still links to its handle.
    """
    lookup = None
    for block in blocks:
        if block["type"] != "question":
            continue
        settings = block["settings"]
        for n in range(1, 5):
            value = str(settings.get(f"answer_{n}_link") or "").strip()
            url = title = ""
            if value:
                if lookup is None:
                    lookup = {}
                    for c in published_collections(store):
                        for key in (str(c.id), c.handle, (c.name or "").casefold()):
                            if key:
                                lookup.setdefault(key, c)
                found = lookup.get(value) or lookup.get(value.casefold())
                if found:
                    url, title = collection_url(base_url, found), found.name
                elif value.startswith(("http://", "https://")):
                    url = value
                elif value.startswith("/") and not value.startswith("//"):
                    url = base_url + value
                else:
                    url = f"{base_url}/collections/{slugify(value)}"
            settings[f"answer_{n}_link_url"] = url
            settings[f"answer_{n}_link_title"] = title


def render_page(*args, **kwargs):
    """Render one storefront page (see _render_page). The "few left" number is per render and never leaks to the next."""
    token = _few_left_at.set(FEW_LEFT)
    try:
        return _render_page(*args, **kwargs)
    finally:
        _few_left_at.reset(token)


def _render_page(store, page, product=None, collection=None, custom_data=None, editor=False, only_section=None,
                 sort="newest", page_number=1, article=None, blog_filter=None, request=None,
                 wishlist_page_ids=None, wishlist_products=None, page_handle=None, cart_page=False, cart_id=None,
                 filters=None, customer_page=None, customer=None, customer_user=None,
                 auth_error=None, auth_success=None, order_completed=False, order=None):
    """
    page: "index", "product", "collection", "blog", "article", "page" or an alternate page template ("page.contact").
    page_handle: the handle of the content page the "page" page shows (the first visible one when None, in the preview).
    product: a catalog Product (product page).
    collection: {"name", "obj"} where obj is a Collection, or None for "all".
    article: the handle of the post the article page shows (the newest when None or not found).
    blog_filter: {"kind": "category" | "tagged" | "author", "handle"} narrows the blog page.
    sort, page_number: the collection page's order (see SORTS) and page (the section's "Products per page", PAGE_SIZE by default).
    custom_data: unsaved section data to render instead of the saved one (customizer preview).
    editor: wrap each section in a marker the customizer preview uses for drag and drop.
    only_section: return just that section's HTML (customizer preview updates one section in place).
    """
    store_theme = get_store_theme(store)
    files = theme_files(store_theme)
    promotions_path = "sections/promotions.liquid"
    if promotions_path in files:
        promotions_markup, promotions_schema, _ = sync.split(files[promotions_path])
        if promotions_schema is not None:
            files[promotions_path] = promotions_markup
    data = resolve(store_theme.custom_data if custom_data is None else custom_data)
    _few_left_at.set(_few_left_threshold(data))
    # A page template whose file is gone shows the base Page template
    if not customer_page and page != base_page(page) and f"templates/{page}.liquid" not in files:
        page = base_page(page)
    settings = data["settings"]
    settings["currency_symbol"] = currency_symbol(store, settings["currency_symbol"])
    base_url = shop_url(store)
    # The header and footer are synced sections (sections/<name>.liquid) when the theme has
    # them: the layout's {% render 'header' %} / {% render 'footer' %} then show them (see section_sync)
    synced = {}
    for name in sync.SYNCED_SECTIONS:
        section = synced_section(store_theme, name, files)
        if section:
            synced[name] = section
    overrides = {sync.SYNCED_SECTIONS[name]["snippet"]: SECTION_PLACEHOLDER.format(name) for name in synced}
    # A synced page template (templates/index.liquid) lists its sections as {% section %} lines:
    # each becomes a slot that a section's HTML goes into, in the page's order
    template_path = f"templates/{page}.liquid"
    slots = _template_slots(files.get(template_path), page)
    if slots is not None:
        overrides[template_path] = slots
    env = _environment(files, base_url, settings["currency_symbol"], overrides or None)

    shop = {"name": store.store_name, "url": base_url}
    nav = _nav(store, base_url)
    product_drop_ = product_drop(product, base_url) if product else None
    if product_drop_:
        product_drop_["details"] = product_details(product, store.field_labels)
    collection_drop = None
    # The shopper's cart id: the cookie's, or a new one that the view puts in a cookie (see cart.py)
    # (cart_id: given by the customizer's preview, which has no shopper request)
    cart_session, cart_new = (cart_id, False) if cart_id else cart_data.session_for_page(store, request)
    if cart_new:
        request._cart_session = (cart_session, True)

    wishlist_ids = []
    if request is not None:
        stored_ids = cart_data.get_wishlist(store, request, cart_session)
        if isinstance(stored_ids, list):
            for value in stored_ids:
                try:
                    product_id = str(uuid.UUID(str(value)))
                except (ValueError, TypeError, AttributeError):
                    continue
                if product_id not in wishlist_ids:
                    wishlist_ids.append(product_id)
        # What was saved stays saved: only entries that are not ids at all, or repeated, are tidied away. A product
        # that is not for sale right now is left out of what is shown and counted, and is back when it is for sale.
        if wishlist_ids != stored_ids:
            cart_data.put_wishlist(store, request, cart_session, wishlist_ids)
        available_ids = set(
            str(product_id)
            for product_id in published_products(store).filter(pk__in=wishlist_ids).values_list("pk", flat=True)
        )
        wishlist_ids = [product_id for product_id in wishlist_ids if product_id in available_ids]

    cart_count = cart_data.item_count(store, cart_session)

    if page == "collection":
        sort = sort if sort in SORTS else "newest"
        all_products = _products(store, base_url, collection=collection["obj"])
        filters = _with_category_ids(store, filters or parse_collection_filters({}))
        products = [p for p in all_products if _matches_filters(p, filters)]
        if SORTS[sort]:
            products.sort(key=SORTS[sort])
        # Facets list every value in the collection; counts follow the other active filters
        vendors, tags = {}, {}
        for p in all_products:
            if _matches_filters(p, filters, skip="vendor") and p["vendor"]:
                vendors[p["vendor"]] = vendors.get(p["vendor"], 0) + 1
        for p in all_products:
            if _matches_filters(p, filters, skip="tag"):
                for tag in set(p["tags"]):
                    tags[tag] = tags.get(tag, 0) + 1

        # Categories facet: count available products matching other filters
        cat_counts = {}
        for p in all_products:
            if _matches_filters(p, filters, skip="category") and p.get("category_id"):
                cat_counts[p["category_id"]] = cat_counts.get(p["category_id"], 0) + 1

        from apps.catalog.models import Category
        store_cats = Category.objects.filter(store=store).order_by("name")
        categories_facet = []
        seen_cids = set()
        for cat in store_cats:
            cid = str(cat.id)
            seen_cids.add(cid)
            categories_facet.append({
                "id": cid,
                "name": cat.name,
                "count": cat_counts.get(cid, 0),
                "selected": cid in filters["category"] or cat.name in filters["category"],
            })
        for p in all_products:
            cid = p.get("category_id")
            cname = p.get("category_name")
            if cid and cid not in seen_cids:
                seen_cids.add(cid)
                categories_facet.append({
                    "id": cid,
                    "name": cname,
                    "count": cat_counts.get(cid, 0),
                    "selected": cid in filters["category"] or cname in filters["category"],
                })

        # Ratings facet: count products meeting each star threshold under other filters
        ratings_facet = []
        for thresh in (5, 4, 3, 2):
            rcount = sum(
                1 for p in all_products
                if _matches_filters(p, filters, skip="rating")
                and p.get("rating") is not None
                and p["rating"] >= thresh
            )
            ratings_facet.append({
                "min_rating": thresh,
                "stars": thresh,
                "label": f"{thresh} & Up" if thresh < 5 else "5 Stars",
                "count": rcount,
                "selected": filters.get("min_rating") == thresh,
            })

        # Stock status counts
        in_stock_count = sum(
            1 for p in all_products
            if _matches_filters(p, filters, skip="stock_status") and not p["is_sold_out"]
        )
        out_of_stock_count = sum(
            1 for p in all_products
            if _matches_filters(p, filters, skip="stock_status") and p["is_sold_out"]
        )

        priced = [p["price"] for p in all_products if p["price"] is not None]
        filter_query = _filter_query(filters, sort)
        # Products per page: the collection section's "Products per page" setting (PAGE_SIZE if it has none)
        collection_template = data["templates"].get(page) or default_template(page)
        per_page = next(
            (s["settings"].get("per_page") for s in collection_template["sections"].values() if s["type"] == "main-collection"),
            None,
        )
        per_page = per_page if isinstance(per_page, int) and not isinstance(per_page, bool) and per_page > 0 else PAGE_SIZE
        pages = max(1, -(-len(products) // per_page))
        page_number = min(max(1, page_number), pages)
        obj = collection["obj"]

        filters_active = bool(
            filters["in_stock"] or filters["out_of_stock"]
            or filters["category"]
            or filters["min_rating"] is not None
            or filters["min_price"] is not None or filters["max_price"] is not None
            or filters["vendor"] or filters["tag"] or filters["q"]
        )

        collection_drop = {
            "name": collection["name"],
            "url": collection_url(base_url, obj) if obj else f"{base_url}/collections/all",
            "handle": obj.handle if obj else "all",
            # Written in the admin's rich-text editor; cleaned like section rich text
            "description": Markup(sanitize_html(obj.description)) if obj and obj.description else "",
            "image": obj.image_url if obj else "",
            "products": products[(page_number - 1) * per_page:page_number * per_page],
            "products_count": len(products),
            "total_count": len(all_products),
            "filters": {
                "active": filters_active,
                "available": filters["in_stock"] and not filters["out_of_stock"],
                "in_stock": filters["in_stock"],
                "out_of_stock": filters["out_of_stock"],
                "in_stock_count": in_stock_count,
                "out_of_stock_count": out_of_stock_count,
                "category": filters["category"],
                "categories": categories_facet,
                "selected_category": filters["category"][0] if filters["category"] else "",
                "min_rating": filters["min_rating"] if filters["min_rating"] is not None else "",
                "ratings": ratings_facet,
                "min_price": filters["min_price"] if filters["min_price"] is not None else "",
                "max_price": filters["max_price"] if filters["max_price"] is not None else "",
                "price_floor": min(priced) if priced else None,
                "price_ceiling": max(priced) if priced else None,
                "vendors": [{"name": n, "count": c, "selected": n in filters["vendor"]} for n, c in sorted(vendors.items())],
                "tags": [{"name": n, "count": c, "selected": n in filters["tag"]} for n, c in sorted(tags.items())],
                "query": filter_query,
                "q": filters["q"],
            },
            "sort": sort,
            "page": page_number,
            "pages": pages,
        }

    template_data = data["templates"].get(page) or ({"order": [], "sections": {}} if customer_page else default_template(page))

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
    page_drop = page_content(store, base_url, page_handle, editor, page) if base_page(page) == "page" else None

    canonical_url = ""
    if product:
        prod_path = f"{base_url}/products/{product.id}"
        canonical_url = request.build_absolute_uri(prod_path) if request else prod_path
    elif collection_drop and collection_drop.get("url"):
        col_url = collection_drop["url"]
        canonical_url = request.build_absolute_uri(col_url) if request else col_url
    elif page_drop and page_drop.get("url"):
        p_url = page_drop["url"]
        canonical_url = request.build_absolute_uri(p_url) if request else p_url
    elif page == "index":
        home_path = f"{base_url}/"
        canonical_url = request.build_absolute_uri(home_path) if request else home_path
    elif request and request.path:
        canonical_url = request.build_absolute_uri(request.path)

    product_json_ld = _product_json_ld(product, product_drop_, store, canonical_url, request) if product else Markup("")
    if product_drop_:
        product_drop_["json_ld"] = product_json_ld

    if customer is None and request is not None and not editor:
        from .views import get_storefront_customer
        customer, customer_user = get_storefront_customer(store, request)

    cart_lines_data = cart_data.view_data(cart_data.open_cart(store, cart_session), base_url)
    if editor and page == "checkout" and not cart_lines_data.get("lines"):
        sample_prod = published_products(store).first()
        sample_variant = sample_prod.variants.order_by("created_at").first() if sample_prod else None
        title = sample_prod.title if sample_prod else "Sample Product"
        price = str(sample_variant.price) if sample_variant and sample_variant.price is not None else "49.00"
        img = sample_prod.images.first().url if sample_prod and sample_prod.images.exists() else ""
        cart_lines_data = {
            "count": 1,
            "subtotal": price,
            "lines": [{
                "id": "sample-1",
                "title": title,
                "url": f"{base_url}/collections/all",
                "image": img,
                "unit_price": price,
                "quantity": 1,
                "total": price,
                "at_max": False,
                "warning": "",
            }],
        }

    menu_links = navbar_menu_links(store, base_url)
    common = {
        "shop": dict(
            shop, blog_url=f"{base_url}/blog", slug=store.store_slug,
            currency=(getattr(store, "currency", None) or "USD").upper(),
            **dict(zip(("country", "phone_code"), _STORE_COUNTRY_BY_CURRENCY.get((getattr(store, "currency", None) or "").upper(), ("", "")))),
        ),
        "settings": settings,
        "product": product_drop_,
        "product_json_ld": product_json_ld,
        "canonical_url": canonical_url,
        "collection": collection_drop,
        "collections": nav,
        "blog": blog_drop,
        "article": article_drop,
        "page": page_drop,
        "template": page,
        "return_url": request.get_full_path() if request else "",
        "wishlist_ids": wishlist_ids,
        "wishlist_count": len(wishlist_ids),
        "cart_count": cart_count or cart_lines_data.get("count", 0),
        # Liquid's `cart` (item_count + lines & subtotal for cart and checkout templates)
        "cart": {"item_count": cart_count or cart_lines_data.get("count", 0), **cart_lines_data},
        "cart_session": cart_session,
        # The layout's visit-tracking beacon: not in the customizer, nor for store members previewing the store
        "track_visits": bool(request) and not editor and not getattr(request, "_no_track", False),
        "store_details": {
            "tax_rate": str(store.tax_rate or 0),
            "prices_include_tax": bool(store.prices_include_tax),
            "shipping_flat_rate": str(store.shipping_flat_rate or "0.00"),
            "free_shipping_threshold": str(store.free_shipping_threshold or "") if store.free_shipping_threshold is not None else "",
        },
        "customer": _customer_drop(customer, customer_user, store, base_url) if customer else (_sample_customer_drop(base_url) if editor else None),
        "customer_logged_in": customer is not None,
        "error": auth_error or "",
        "success": auth_success or "",
        "order_completed": order_completed,
        "order": order or {},
        # True in the customizer preview: templates may show hints that shoppers never see
        "design_mode": editor,
        "menu_links": menu_links,
        **layout_data(
            settings, base_url, nav, menu_links,
            dead=dead_menu_urls(store, [u for _, u in _menu_lines(settings.get("header_menu"))]
                                + [u for _, u in _menu_lines(settings.get("footer_menu"))]),
        ),
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
    tokens = (store_theme.theme.theme_data or {}).get("tokens")
    html = [google_fonts_link(_collect_fonts(data)), theme_css(settings, tokens), SECTION_CSS, TYPE_CSS]

    html.append(CUSTOM_CSS)
    if page != "checkout":
        html.append(store_custom_css(settings))
    page_styles = len(html)

    spans = {}  # section id -> its part of `html` (to put it in its slot)
    for section_id, raw_section in visible:
        # "Latest posts" with no posts to show is left out of the live store
        if SECTIONS.get(raw_section.get("type"), {}).get("uses_articles") and not posts and not editor:
            continue
        span_start = len(html)
        section = dict(raw_section, id=section_id)
        section["settings"] = _prepare(section["type"], section["settings"])
        path = f"sections/{section['type']}.liquid"
        wrap = editor and section_id in real_ids

        if wrap:
            # The customizer preview labels a section with the name the sections list shows
            label = section["settings"].get("heading") or SECTIONS.get(section["type"], {}).get("name", "")
            html.append(Markup(f'<div data-tss-section="{escape(section_id)}" data-tss-label="{escape(label)}">'))
        html.append(_wrap_attrs(section_id, raw_section["settings"], theme_button))
        if page != "checkout" and _section_css(section_id, raw_section["settings"]):
            # Section > Advanced > Custom CSS, nested inside the section's own wrapper so it stays there
            html.append(Markup(f'<style>[data-tss-css="{section_id}"]{{{_section_css(section_id, raw_section["settings"])}}}</style>'))

        if section["type"] == "custom":
            html.append(_render_custom(section, store, env, base_url, theme_button))

        elif section["type"] == "why-sipnow":
            html.append(_render_why_sipnow(section))

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

            if section["type"] == "match-quiz":
                _match_quiz_links(store, base_url, section["blocks"])

            if section["type"] == "promotions":
                # The cart icon and wishlist heart blocks are read below, they are not promotion cards
                section["blocks"] = [b for b in section["blocks"] if b["type"] == "promotion"]
                promotions_path = "/collections/promotions"
                for block in section["blocks"]:
                    if str(block["settings"].get("button_label") or "").strip().casefold() == "shop offers":
                        block["settings"]["button_link"] = promotions_path
                for block in extra_blocks:
                    if block["type"] == "button" and str(
                        block["settings"].get("label") or ""
                    ).strip().casefold() == "shop offers":
                        block["settings"]["link"] = promotions_path

                promotions_collection = _find_collection(
                    store, section["settings"].get("view_all_collection")
                )
                section["view_all_url"] = (
                    collection_url(base_url, promotions_collection) if promotions_collection else ""
                )
                # The chosen collection's products (published, newest first), read on every render;
                # no collection: the promotion cards only
                section["products"] = (
                    _products(store, base_url, collection=promotions_collection,
                              limit=section["settings"].get("product_limit"))
                    if promotions_collection else []
                )
                section["has_collection"] = promotions_collection is not None

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
                # The "Wishlist heart" block: its settings, or None when the owner removed it (or hid it)
                heart = next((b for b in all_blocks if b["type"] == "wishlist_heart" and not b.get("hidden")), None)
                section["wishlist_heart"] = heart["settings"] if heart else None

            _sec_schema = SECTIONS.get(section.get("type"), {})
            _sec_blocks = _sec_schema.get("blocks", {})
            if "wishlist_heart" in _sec_blocks and section.get("type") != "main-product":
                # The "Wishlist heart" block of a section with product cards: its settings, or None when hidden or never added
                heart_block = next((b for b in all_blocks if b["type"] == "wishlist_heart" and not b.get("hidden")), None)
                section["wishlist_heart"] = heart_block["settings"] if heart_block else None

            if "cart_icon" in _sec_blocks:
                # The "Cart icon" block: its settings, or None when the owner hid it (or never added it)
                cart_block = next((b for b in all_blocks if b["type"] == "cart_icon" and not b.get("hidden")), None)
                section["cart_icon"] = cart_block["settings"] if cart_block else None

            if _sec_schema.get("uses_products"):
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

            if _sec_schema.get("uses_articles"):
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
                if section["type"] == "main-product":
                    oos_flag = "true" if section["settings"].get("disable_out_of_stock", settings.get("disable_out_of_stock_add_to_cart", True)) else "false"
                    oos_label = escape(str(section["settings"].get("sold_out_label") or settings.get("sold_out_button_label") or "Sold out"))
                    rendered = f'<div data-stock-config data-disable-oos="{oos_flag}" data-sold-out-label="{oos_label}" style="display:none"></div>\n' + rendered
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
        spans[section_id] = (span_start, len(html))

    if only_section is not None:
        return Markup("\n".join(html[page_styles:]))

    scripts_start = len(html)
    html.append(ANIMATION_JS)
    html.append(BLOCKS_JS)
    html.append(PRODUCT_INFO_JS)
    html.append(SEARCH_JS)
    html.append(_stock_config_script(settings))
    html.append(STOCK_JS)

    if slots is None:
        content = env.get_template(template_path).render(
            sections=Markup("\n".join(html)),
            **common,
        )
    else:
        content = _fill_slots(
            env.get_template(template_path).render(sections=Markup(""), **common),
            [section_id for section_id in template_data["order"]],
            {sid: "\n".join(html[a:b]) for sid, (a, b) in spans.items()},
            [sid for sid, _ in visible],
            "\n".join(html[:page_styles]),
            "\n".join(html[scripts_start:]),
        )

    if cart_page:
        # The cart page is the cart snippet in 'page' mode (see snippets/cart.liquid): no template file
        cart_opts = cart_data.options(store_theme, files)
        cart_lines = cart_data.view_data(cart_data.open_cart(store, cart_session), base_url)
        cart_content = env.from_string(
            "{% render 'cart', mode: 'page', cart: cart, settings: cart_opts, session: cart_session %}"
        ).render(**{**common, "cart": cart_lines, "cart_opts": cart_opts})
        content = Markup("".join((
            str(theme_css(settings, tokens)),
            str(SECTION_CSS),
            str(TYPE_CSS),
            str(CUSTOM_CSS),
            cart_content,
        )))

    if wishlist_page_ids is not None:
        # The wishlist page is the wishlist snippet in 'page' mode (see snippets/wishlist.liquid): no template file
        products = wishlist_products or {}
        saved = [product_drop(products[pid], base_url) for pid in wishlist_page_ids if pid in products]
        wishlist_content = env.from_string(
            "{%- assign back_to = shop.url | append: '/wishlist/' -%}"
            "{% render 'wishlist', mode: 'page', items: wishlist_items, wishlist_ids: wishlist_ids, return_url: back_to %}"
        ).render(**{**common, "wishlist_items": saved, "wishlist_ids": wishlist_page_ids})
        content = Markup("".join((
            str(theme_css(settings, tokens)),
            str(SECTION_CSS),
            str(TYPE_CSS),
            str(CUSTOM_CSS),
            wishlist_content,
        )))

    if customer_page:
        tpl_name = f"templates/{customer_page}.liquid"
        alt_tpl_name = f"templates/customers-{customer_page}.liquid"
        target_tpl = tpl_name if tpl_name in files else (alt_tpl_name if alt_tpl_name in files else None)

        if target_tpl:
            cust_content = env.get_template(target_tpl).render(**common)
        else:
            if customer_page in ("login", "register"):
                cust_content = env.from_string(
                    "{% render 'customer-auth', mode: mode, customer: customer, return_url: return_url, error: error, success: success %}"
                ).render(**{**common, "mode": customer_page})
            else:
                cust_content = env.from_string(
                    "{% render 'customer-account', customer: customer, orders: customer.orders, addresses: customer.addresses, error: error, success: success, wishlist_count: wishlist_count %}"
                ).render(**common)

        if customer_page in ("login", "register"):
            cust_content = customer_auth_content(cust_content, customer_page)

        content = Markup("".join((
            str(theme_css(settings, tokens)),
            str(SECTION_CSS),
            str(TYPE_CSS),
            str(CUSTOM_CSS),
            cust_content,
        )))

    description = ""
    if product:
        title = (product.seo_title or "").strip() or product.title
        raw_desc = (product.seo_description or "").strip() or (product.description or "")
        description = _clean_text(raw_desc, 160)
    elif cart_page:
        title = "Cart"
    elif page == "checkout":
        title = "Checkout"
    elif wishlist_page_ids is not None:
        title = "Wishlist"
    elif customer_page == "login":
        title = "Sign in or create account"
    elif customer_page == "register":
        title = "Create Account"
    elif customer_page == "account":
        title = "My Account"
    elif collection_drop:
        title = (collection["obj"].seo_title if collection["obj"] else "") or collection_drop["name"]
        raw_desc = (collection["obj"].seo_description if collection["obj"] else "") or (collection["obj"].description if collection["obj"] else "")
        description = _clean_text(raw_desc, 160)
    elif page_drop:
        title = page_drop["seo_title"] or page_drop["title"]
        description = _clean_text(page_drop.get("seo_description"), 160)
    elif article_drop:
        title = article_drop["title"]
        raw_desc = article_drop.get("excerpt") or article_drop.get("body") or ""
        description = _clean_text(raw_desc, 160)
    elif blog_drop:
        title = blog_drop["title"]
        description = _clean_text(blog_drop.get("description"), 160)
    else:
        title = store.store_name

    page_html = env.get_template("layout/theme.liquid").render(
        content_for_layout=Markup(content),
        page_title=title,
        page_description=description,
        **common,
    )
    for name, html in synced_html.items():
        page_html = page_html.replace(SECTION_PLACEHOLDER.format(name), str(html))
    # Theme settings > Logo and favicon: the layout file (Edit code) has no favicon tag, so it is added here
    favicon = str(settings.get("favicon_url") or "")
    if _FAVICON_URL.fullmatch(favicon):
        page_html = page_html.replace("</head>", f'<link rel="icon" href="{escape(favicon)}">\n</head>', 1)
    if customer is not None and customer_user is not None and request is not None and not editor:
        from .views import customer_link_token
        page_html = _with_customer_link(page_html, customer_link_token(store, customer_user), base_url)
    return page_html
