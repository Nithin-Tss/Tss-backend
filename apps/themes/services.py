"""
Store themes.

Every store edits its own copy of a theme: the first time a store's theme
is used, the starter theme (apps/themes/starter/) - or the shared theme the
store had installed - is copied into a private Theme row whose slug is
"store-<store id>". Code edits from the editor change only that copy.
A store can keep several such themes (its library): exactly one is published (the
StoreTheme with is_active) and shows on the storefront; the others are drafts that can be
edited and previewed, and become live only when published. See set_editing_theme.
Section settings (hero text, slides, ...) live in StoreTheme.custom_data.
"""
import copy
import hashlib
import json
import re
import secrets
import time
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Sum
from django.utils import timezone
from liquid import Environment
from liquid.exceptions import LiquidError
from rest_framework import exceptions

from . import section_sync as sync
from django.utils.text import slugify

from .models import StorePage, StoreTheme, Theme, ThemeFile
from .theme_paths import check_new_path, is_private_block
from .schemas import (
    MAX_PAGE_TEMPLATES, PAGE_TEMPLATE, PAGES, SECTIONS as SECTION_SCHEMAS, STARTER_PAGE_TEMPLATES,
    default_template, resolve, validate_custom_data,
)

STARTER_DIR = Path(__file__).resolve().parent / "starter"
STARTER_VERSION = "1.0.0"

KEEP = ".keep"  # marks an empty folder (used by the code editor)
REQUIRED_FILES = {
    "layout/theme.liquid",
    "templates/index.liquid",
    "templates/product.liquid",
    "templates/collection.liquid",
    # Blog pages: a theme copied before they existed gets the starter's missing files once
    "templates/blog.liquid",
    "templates/article.liquid",
    # Pages (About us, ...)
    "templates/page.liquid",
    # The product card drawn from the section's "Product card" block
    "snippets/product-card-blocks.liquid",
    # Wishlist: one snippet with all its markup, the page and the script
    "snippets/wishlist.liquid",
    # Cart: one snippet with the icon, the drawer, the cart page and the script
    "snippets/cart.liquid",
    # Customer account & auth
    "templates/login.liquid",
    "templates/register.liquid",
    "templates/account.liquid",
    "snippets/customer-auth.liquid",
    "snippets/customer-account.liquid",
    "snippets/auth-styles.liquid",
    "snippets/auth-header.liquid",
    "snippets/auth-links.liquid",
    "snippets/auth-login-form.liquid",
    "snippets/auth-register-form.liquid",
    "snippets/auth-script.liquid",
    "snippets/account-styles.liquid",
    "snippets/account-header.liquid",
    "snippets/account-profile.liquid",
    "snippets/account-addresses.liquid",
    "snippets/account-orders.liquid",
    "snippets/account-script.liquid",
    # Checkout
    "templates/checkout.liquid",
    "sections/main-checkout.liquid",
    # Promotions is available in the customizer for existing private starter themes too
    "sections/promotions.liquid",
    # Newsletter sign-up, notice box and "Find your match" quiz
    "sections/newsletter.liquid",
    "sections/static-text.liquid",
    "sections/match-quiz.liquid",
    # Feature snippets: the collection page's heading, search, filters, toolbar and pagination, the blog's category links
    "snippets/theme-css.liquid",
    "snippets/collection-header.liquid",
    "snippets/collection-search.liquid",
    "snippets/collection-filters.liquid",
    "snippets/collection-sort.liquid",
    "snippets/collection-toolbar.liquid",
    "snippets/collection-pagination.liquid",
    "snippets/blog-filters.liquid",
    # Default theme tree (default_provisioning): required once a theme has it
    "config/settings_schema.json",
}
# Required files the starter doesn't ship: a theme without them must not trigger the starter refill below
NOT_IN_STARTER = {"config/settings_schema.json"}
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 500
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# Starter files that have newer versions. Each store has its own copy of the
# theme; a copy whose content still matches one of these earlier versions
# (SHA-256) was never edited, so it is replaced with the current starter file.
# Edited copies are left alone. Bump STARTER_REVISION when adding entries.
STARTER_REVISION = 61
STARTER_UPGRADES = {
    # Revision 59: every card part carries its place and name in the customizer preview (data-tss-block markers), so hovering
    # and clicking a part works. Stores that got the first version of the snippet (revision 58) are upgraded if they never edited it.
    # Revision 61: the card's shell keeps its plain class (product-card-shell); the section's card CSS finds it by data-pcb.
    # Revision 60: the price part can show the compare-at price first and "Taxes included" under the price.
    "snippets/product-card-blocks.liquid": {
        "e909cdf4c37cb3b00dafc3f871ae37c69164d37ce95f450e0926317120e7f7ff",  # revision 61: before the shell's data-pcb attribute
        "4109e553a6656bed33125bae3911e2b77427373108962be2940f122874716235",  # revision 61: before the shell's data-pcb attribute (CRLF)
        "1128993c55a2e35ded97df7397faed4545d72ae71939eb02f832f2e6c688a9ad",  # revision 60: before sale-price order and tax information
        "3d7c6eb33f7e9355d30f6db3a028b788dd2db6229bc1c12202d71467cb6eaeee",  # revision 60: before sale-price order and tax information (CRLF)
        "3b1d406562c47dad4d89933f4234aee4d2f2f36017adb5bf47190210be9a8801",  # revision 59: before the preview markers
        "80c71003f929e78e8488c6b5bc0e508d0ff0137367ac1849cc05e4dc3631a9a3",  # revision 59: before the preview markers (CRLF)
    },
    # Revision 58: the product card can be drawn from a "Product card" block (sections call product-card-blocks when they have one).
    # Only copies still equal to an earlier starter version are replaced; a file the owner edited keeps its own code.
    "snippets/product-grid-items.liquid": {
        "8861200927088c79ee0762bcd7f4be3ab4597bd66b3de213b7adf33772b2c47e",  # revision 58: before the product card block
        "2c4f5870658a4b66dc432d7082986e3e311d56ba9ecd175f4405d163eb1fa585",  # revision 58: same, with CRLF line endings
    },
    # Revision 56: the filters, toolbar, search, heading and pagination are snippets (collection-*), the category links too (blog-filters)
    "sections/main-blog.liquid": {
        "30c262da103a06435fb43913dac7cc97b3060003098fc713452128b9f4965fb0",  # revision 56: before the feature snippets
        "58722c432bf6c82c1e356e29ba6c051dce614700f48925ec3463fe32a375d706",  # revision 56: same, with CRLF line endings
    },
    # Revision 55: the checkout's discount field is also a gift card field ("Discount code or gift card")
    "sections/main-checkout.liquid": {
        "c8540f4e04bfc2339c9bc8dc236c3d47977ef2afc3ef4303f2ca87b53734e88f",  # revision 56: before several gift cards could be applied (CRLF)
        "038b297820da93deb8527d796939459fe5dbcc14c29b9ac4a713537353bbf84b",  # revision 56: before several gift cards could be applied
        "5356cdde411cfc16dae67f8d314f29ba5407a9c3cdc028e083164889adaba80b",  # revision 55: before the gift card field
        "6d7f6dbc44adfb06e5d3f35bf9b4ae58912003cb3b7ad0db74fc47af8cb75bb4",  # revision 55: same, with CRLF line endings
    },
    # Revision 42: the account page is built from blocks
    "templates/account.liquid": {
        "a52c2767aa520c9043cabf866f7407878fff7e3935cc74c84e7e7063e664822d",  # revision 42: before the account blocks (814f062)
        "b203c7a1309345f0d22e22556ca4929ef54c525c8b684303fd57bb82d57c3e92",  # revision 42: before the account blocks (814f062 (CRLF))
    },
    # Revision 42: the account page is redesigned (sidebar, order cards, address cards) / sign-in pages follow the theme colors
    "snippets/customer-account.liquid": {
        "7041fe212de2e5d04be4bcba8f0b5e373a89dbdbb4be34db045ea2ee174c0373",  # revision 42: before the redesign (814f062)
        "aae6026a602b5438e18943e80bd2838bbd5b23f3b843cc64ea3b1441c28f081d",  # revision 42: before the redesign (814f062 (CRLF))
    },
    # Revision 42: the account page is redesigned (sidebar, order cards, address cards) / sign-in pages follow the theme colors
    "snippets/customer-auth.liquid": {
        "b84db27b1f03df85937a47220b777f04eadc0385bce48011be5474d39fa0b718",  # revision 42: before the redesign (814f062)
        "d1f648ba99a850284d02cf36afcd3d7511105cc33d1211b4686117c835d5d0af",  # revision 42: before the redesign (814f062 (CRLF))
    },
    # Revision 42: the account page is redesigned (sidebar, order cards, address cards) / sign-in pages follow the theme colors
    "templates/login.liquid": {
        "ec55159ed4ffbf789663b2925aa7eaf5d22e02c67828e9f715534e5f41b4f0bc",  # revision 42: before the redesign (96507bc)
        "8fd5b0138ee0ae8c8f4ee4329c276a5f924d6c9995bc16f3359a7f934e809356",  # revision 42: before the redesign (96507bc (CRLF))
        "839add7a0cd2e6909c02ebf3ed2c761cd59e8bd8910923e9c6a30cc53578f133",  # revision 42: before the redesign (814f062)
        "7d9ba1a698f7d803e8a7564a8cac1bfdb3df1e18eaeb13c66b0b5b38e84b96fd",  # revision 42: before the redesign (814f062 (CRLF))
    },
    # Revision 42: the account page is redesigned (sidebar, order cards, address cards) / sign-in pages follow the theme colors
    "templates/register.liquid": {
        "1842702f2cd8ced80c9b59202c817d7b3f219a1c01e095c5e48e1d099e4020d4",  # revision 42: before the redesign (96507bc)
        "68ff38b379a5e26175d08745f55fa2c8b9f4af2a56ba05b884e7dd7214c06dea",  # revision 42: before the redesign (96507bc (CRLF))
        "35c1822760dfb327cb082f9bb473b0e5faaf44ade6f29bc0dcea5cd657762ba4",  # revision 42: before the redesign (814f062)
        "c49eee1297c1efc439957e288b7d28f3c8558fa6733ae765b55852b5676deb19",  # revision 42: before the redesign (814f062 (CRLF))
    },
    # Revision 30: the heart and the navbar link carry the shopper's cart id (the sandboxed page cannot send cookies)
    "snippets/wishlist.liquid": {
        "81f0d8ce1e90b5ab748dad8930cf4a713f6479f523fa349adced708e26a7a762",  # revision 46: before every store link carried the cart id (CRLF)
        "6130a9dd14a1f6261a71ff5812dc036304933b6e658114225732a9857fb9f6c1",  # revision 46: before every store link carried the cart id
        "bacf2337cd2d3e93a4ab383cb933621ab0d032e6e1dc8c7cd43355da5e06aff6",  # revision 35: before the heart script read form.getAttribute("action")
        "9a483173a5c6859cd41f92b60b4418e554ab51eb1807574b3f4b4234892146c0",  # revision 35: before the heart script read form.getAttribute("action") (CRLF)
        "c25c619e33d40a7df100870fea1e0d6da00f84e484197a3d44397c83dd77b339",  # revision 34: before the heart was always visible
        "fc5df79003d8ee16938a0dee3e1327fcc925e8e8471ad0bc2a7ba2a087df259b",  # revision 34: before the heart was always visible (CRLF)
        "985811bf51e06385a6386f549df2f5e9468b772dd04045b5b8c13ac5e0987b65",  # revision 33: before the heart block has a label
        "69b666f890275a7952dcaac9856119d1a8257b9fda05fec5db705beda6ced4d6",  # revision 33: before the heart block has a label (CRLF)
        "be5500d95437e865507e26779c33e18221703bb53c5db47427cbe8f8b5430e41",  # revision 32: before the heart decides its own hover
        "84bd411c7471d9f4560177f86989819fc7df6371ba33f821f2de9d4af5275ddf",  # revision 32: before the heart decides its own hover (CRLF)
        "bdfd3c4c66db7becf69166866166209334cd7a56ce4f250c127ab9cd68b00292",  # revision 31: before the wishlist heart became a block
        "8bde6bb48555205f2ab7fd9411e53c6ca00bb37ee6e39eaaeffd07eb6013135e",  # revision 31: before the wishlist heart became a block (CRLF)
        "875d4ad6e2fe56977f8331287812348d798207013fe80c645e851d232fa28102",  # before revision 30
        "d7934cacb00087dd0cced6cf84ff16fed5488dfd464eb58e1a59900c892ff690",  # before revision 30 (CRLF)
    },
    "sections/hero.liquid": {
        "c713c5559e80c118413fad28450cbf01bbfbf5b145377320eb8b06ef73dc2c3d",
        "fd2985d83d849f0cf76976dda632fb6d3747cb551e84d2d8dab86f87585618a3",  # revision 13: image description, no unused button settings
    },
    "sections/slideshow.liquid": {
        "1676de060a502cce01bded76ca923306ffb596e2b5e4a824630b2398e5b1fb58",
        "32a46aa7269245969967e3c388440384f79a8c57e40060332a6a9ec047cc8732",  # revision 9: slide text options
        "67d575481eee66766ed8c904813a8ccf965c8d288376881e60e24a51cecab125",
        "8a3471bb606877eda22c385605c975c942aaa2b1b5bbfcbb9afbd57af24b029c",  # revision 12: slide alignment follows text alignment
        "06e6537da8a47e1d8e62fbc1615ca4636aee391c7627342f20707c95fc630313",  # revision 11: slide image fit and focus
        "004beb6dad2713d62967443126f69483bc99da33e548b62e325090c65ad57032",  # revision 10: slide layout, overlay, padding, video
        "a831a51ab4d6a82271abf32b7238911be9d920471b2b2490dc13dc1b83ad4fe6",  # revision 12: before slideshow accessibility and swipe
    },
    "sections/product-grid.liquid": {
        "73970df9555d2ff91349c339e8143ebf12129f4c35f8fb104e5f1e2ca7e07f50",  # revision 58: before the product card block
        "b3f19f53dc675bb3ab27a6e9d7df8fa56f59b9f60f5e6089e6f6f8ba7953ef76",  # revision 58: before the product card block (CRLF)
        "2490f671fe83ecc1cf97fe530ee29a9e147b9e0cd0f0928463481110d521b4e2",  # revision 31: before the wishlist heart became a block
        "1124400a0a1acc01e6bbdc80b0ccf44e1a17ac4969942673c78e44aecff107fe",  # revision 31: before the wishlist heart became a block (CRLF)
        "849a65ea615ec1126270a01748910eed928680a253b1cfc1562641fd52c97a5c",  # revision 27: before the cart icon became a block
        "424292334f8291c2c65454b6a2bef4b1c49ae553e76aeded619ddb2812e7c44b",  # revision 27: before the cart icon became a block (CRLF)
        "5918ac10692c4b012468cf17a6098d28048585bbfe50d12eb849c2d23fe30518",  # revision 26: an earlier cart icon draft
        "01c75442af5225cec879b9e2bac509f871d1de6e3d0606dd8b0ba5ab67b1af23",  # revision 25: before the cart icon on product cards
        "667f08581ad9472d7855c582ca505e0b4a4428626206e1b20e981939c8280bf7",  # revision 25: before the cart icon on product cards (CRLF)
        "b5bc74b7ad19eb53aa7935de4d44ecd407b7748445ba5a73ab65ada18b24e184",
        "197e7ad9e76dda5adcb0b762a69623d0ebb407baf1e3391edd794d26fa41275f",
        "d98591d946fbc257e35e491777052a64ddea4602a59047da771ad6245d2fbe59",  # revision 18: pass wishlist state to cards
        "32c98d43a2af2295fe65fa4034b5d96b987721004ccb72386511164b4502c2a5",  # revision 19: before the wishlist snippet
        "e6f1c4afcbd4615ea14a2fc5567c6c6af98ba5abdc1205314609b8e19ac9d08c",  # revision 19: same, with CRLF line endings
    },
    "sections/product-carousel.liquid": {
        "1e91b9b269d8d636e76f6280c1abc43e20dbe89e7f4fd5bd22980e637b181b32",  # revision 58: before the product card block
        "914398779c62ea0e1840493accba8825f53bc9908e9460f4a377ecbc33f42bfa",  # revision 58: before the product card block (CRLF)
        "85ed27b927b263e8c4bb71256c87070b24cd14bb62d6e69c1694f9a0990af3df",  # revision 31: before the wishlist heart became a block
        "0fec34b8d1deafffbeb9b99f2be253e65469b75a0b56795291ba318a2b9c52cb",  # revision 31: before the wishlist heart became a block (CRLF)
        "6e8c18942a33473eb6d3a62d3ba4885ab73cc48fdb813e83f7609acafb21b473",  # revision 27: before the cart icon became a block
        "cee64069541967e657e8276b0a8a05d698fb73394305b84ff0eabdb80593f4d9",  # revision 27: before the cart icon became a block (CRLF)
        "a86cba7067b63ffa5ee2fdf527db65d849f42723790cb405842b6d6ade55ebed",  # revision 25: before the cart icon on product cards
        "a562630dc065bf795acb39d99335e5bb98091b7545254286d83a4763a9e70567",  # revision 25: before the cart icon on product cards (CRLF)
        "ed4653a5d5af7c5caee91d9839486ed92c854b758e6e11ac10f76d729fc8af2f",  # revision 25: older starter version some stores have
        "0609acb6e9af090ad850a2f416005ac19ae7716ba1acb730c063864e7e6776d4",
        "a79be796dde0feb9dd8fceda3a0b4e00c8c1cc3469e6066e02c506dfa8d88055",  # revision 16: no empty box for shoppers, pause on focus
        "684361444547f8952a62f6f5f86c2ddd3e4c86c56e60b717ff38ad3e148d3897",  # revision 18: pass wishlist state to cards
        "6c204389405b4509ce727a77be94de76318c5830102786803fb5c78be5a105bd",  # revision 19: before the wishlist snippet
        "4ec5499f34fe94154eb984801cdb0dd50045de966a963bcab0bb04961e994604",  # revision 19: same, with CRLF line endings
    },
    "sections/rich-text.liquid": {
        "37ec79026e20ea886f79c150c660ffdfb6ca04b4614bb4878a6d263e1a58b10b",
        "8c115bfae9de1cfa75a8931aaafa91c3781837d743e9340440e22bbc95a6ebe8",  # revision 11: image shape, fit and focus
        "1a5b5ca920bb489f2047302eda3da0179b9b5b270c26e4b0a4d693f4afc61746",  # revision 12: before image description
    },
    "snippets/product-card.liquid": {
        "398d8d724bab5ad97065bd9b16a31145d181518600b8ed553b1e0011c47f6cf7",  # revision 40: before product badges
        "c44ea98c359fa9e3b1b9ae047ed460245c9b095042fb0b6579524ef3a157b762",  # revision 40: before product badges (CRLF)
        "341a3a0163521377c7875e8a3cfdf8e08fd9dd62706e571f1694fb92b5904a55",  # revision 34: before the heart was always visible
        "a2e09794890de06e1db5a67c118dd86cef2c18272f7dded78e128a7bc238eea3",  # revision 34: before the heart was always visible (CRLF)
        "329dd5b842b8f6f8abdd9889a6decda78ab452d10163e330becf5ddf6564cde7",  # revision 33: before the heart block has a label
        "a318fb03c038f1cf4df159ffae72df3ca05c325007dd8fca7db64660eb86242d",  # revision 33: before the heart block has a label (CRLF)
        "efc9ce8bb519a39c06c643dae60217dcbbd533313bd348513c7bf49c96830efa",  # revision 32: before the heart decides its own hover
        "9dc82c232603748f890c01e53b78886865b30a24e41db4c1837036f0a886915c",  # revision 32: before the heart decides its own hover (CRLF)
        "b4ab8c333e9ea4c149ad730561b2d122ac67134d4b401a9233c371f8374a4f4e",  # revision 31: before the wishlist heart became a block
        "af1f67c951e00667d9e432d3c4071a2819c6a675e6251d25e7dc4b555f196075",  # revision 31: before the wishlist heart became a block (CRLF)
        "8bea0981af9916bf3f0cf690082e4a1e7af0b409b39131ba50c8c2296fb8c419",  # revision 29: before the cart icon was added to the card by its block
        "fcd06f9be05274f8bf3ad855a66c337d4f3c3a22798cfec66b00effa9bc80880",  # revision 29: before the cart icon was added to the card by its block (CRLF)
        "f4ac32c3302297ee3417ba41b88d94fca4a8316e1214b2b2645fef3de3cda957",  # revision 27: before the cart icon became a block
        "fa8c2e225e5757b397003c24219be553dbabf86150a44c0949156d819d939143",  # revision 27: before the cart icon became a block (CRLF)
        "34b896b532e6d75aa9e00770be9bddbe0a2e62f7a5475db1ee56961c470ee561",  # revision 26: an earlier cart icon draft
        "fdf65276bf0ca2cd83113cbfa434325e5c08608885530bc41c66a2699959678b",  # revision 25: before the cart icon on product cards
        "86bfa99a2c46e4750e9b7a9f0558a8e8f443fae62551dcbda6e0165254ad13f5",  # revision 25: before the cart icon on product cards (CRLF)
        "3a3fbbc1982c57368b943d644f32f1c51aaffd475c5e3aab6c5d52abac5b1cff",  # revision 25: first cart icon draft
        "bc624bf8346fce0c29de1419f3d32ad1f66f052fe75ec9830685f219aee43eff",
        "386b5a2d330ef931de50ce9fa6f66cb49f0bae00b23582dfd4e27b6a84ad3723",
        "4af9942dd8108371e0d9225c91686e3b55a7aa04fad897345fbee7d6a1875dd3",
        "1930541f426b1a8fb7c816b9c63b2afbf84379aa249d4871556299ba0e8f9dce",  # revision 15: price shown inline, no 'price' snippet
        "ec61086466c120f8ad0463a727c98777b07d9155bb9f167bd1224b51132fb4b7",  # revision 17: wishlist actions
        "211c4b936c465237301a2a6a4373ff684ab6995e6ad769adbdc2e185ee8077e3",  # revision 18: wishlist toggle state
        "78fc98475a47063470123fffaa31a6058ccfe5c25ff7420137ea708fad314a83",  # revision 19: before the wishlist snippet
        "3197e89478b76a5ece309e65af5d1990856e32f29d8229f3ad04bd38b17b50a5",  # revision 19: same, with CRLF line endings
        "b2d26d3b6c5b6958d032ef6c8d98250b02ae9575e9f2a4115bbe14272b035e3e",  # revision 20: the heart was on unless a section turned it off
        "4be9e7feb8659b7f7f792207a15d7adf0924e9a0795770fb541fd81ad81d0360",  # revision 20: same, with CRLF line endings
    },
    # Revision 4: "Sold out" / "In stock" / "Only a few left" on the product page
    # Revision 5: "Product information" accordion under the description
    # Revision 53: variant picker is option-value buttons (not a SKU dropdown), SKU removed from the page
    # Revision 54: the "In stock" / "Only a few left" message is back on the product page
    # Revision 55: a gift card product shows "for myself / for someone else" recipient fields
    "sections/main-product.liquid": {
        "4ffa9bb1baa12d274ae272fccccdd78d14d23a6373289a62f64f82874c616512",  # revision 55: before the gift card recipient fields
        "beea8f1d6eb1610d57583cc18db5ad385c4cbdc4f49a8b0e2f702ac059b2583f",  # revision 55: same, with CRLF line endings
        "b7cc7c3ab5b4d28277fddf652f5eb145b1018167856f1712905a142a22f82a62",  # revision 54: before "Only a few left" skipped options that keep selling
        "7a4285f2bfc1c2d58e810035ba181f766ccf81ef2c307c42dd4bbe86064d4926",  # revision 53: before the stock message came back
        "79ce976f68235686a58778b943748aaa6bf05949517e4204f58929ba0ddb3519",  # revision 52: before the option-button redesign
        "221ab289f74da87fb4c7860aa09813981eaa1f376a7951f6cfc14f3a334ad718",  # revision 52: before the option-button redesign (CRLF)
        "f2e87477d18719a79f68bff217b82ce6415d3fbbefed5ebfea20a3b6f6c65a31",  # revision 44: before options that keep selling stay orderable
        "3c8b55dc638fb076f8c8e49a63c993ddeea59e6708c9c0eed86a63743cd8e1ae",  # revision 44: same, with CRLF line endings
        "a06239b4053ab69f9e0b19ca518ad3d1f6980250da33e609b95648dbb6317b19",  # revision 40: before tier cards, image zoom and badges
        "f683ef5ba444617bc7095bbee0a9cdf06cc5fb8a5064020ecf5492fd80c606e2",  # revision 40: before tier cards, image zoom and badges (CRLF)
        "2685ec1e87b5219e6e06820aa4ae289a5535386e223a2224b03bf6fe4908eb44",  # revision 28: before the cart icon block on the product page
        "9ce1235a40d399e6785771e625150423c6d0a63d360fb557fc23f4c45a575ac2",  # revision 28: before the cart icon block on the product page (CRLF)
        "a7e5eb025dc28325a5b321a418da4f4fe0ec60105e4d9b683752a2c53f1b90ce",  # revision 21: before the cart snippet
        "0e1504eb9340bb893a9b83c173943ff55430a9efde792269119e1441fdb17d35",  # revision 21: same, with CRLF line endings
        "18d5dd4556bc252eab73fafd20965a871816b44843a55adf743ae341493d1729",
        "3f5327f9d977c78b2e4e56eb89489be0ef544d06e258d91a2d8e3ca9d40f6211",
        "6c8de76dd784aabf1a7e13c29a4a30eed779f80e8b2824ace5b232109ffad098",  # revision 15: price shown inline, no 'price' snippet
        "e365056fcc5eb05b563e29fca7ab581c8158c4521faedfdc4acbe1701c887552",  # revision 16: option price/SKU fix, low-stock setting
        "e50ac85c4dd9c6e69b590632b6b0e2333fd9dff7521c7493e5fca6a8a39f97e5",  # revision 17: wishlist action
        "533aa3c35440fbb925de41f7e9fc62f01286439a5252bb4194ee449144e74817",  # revision 18: wishlist toggle state
        "d4c0b99cd99aeaf5718b6e828d101e4a83e3aa80e56dcee06afbc9c10fdc6432",  # revision 19: before the wishlist snippet
        "81382355092a70c8283ccde62122814ddd4b657663747c81de2003833709c464",  # revision 19: same, with CRLF line endings
    },
    # Revision 19: the header's wishlist heart is a "Wishlist icon" block that renders snippets/wishlist.liquid
    "sections/header.liquid": {
        "3c397975d7ccc807b322de2f895e9907043d6b97b3b53f18b5143588a36fbdfb",  # revision 51: before the mega menu panel followed the header width
        "06abcf0322802d08414f520abb64c726e4c24c0c474c3ec7e34decb1a397474e",  # revision 51: before the mega menu panel followed the header width (CRLF)
        "a6c7d0813a1059fc6f28fc7d7ca18503bf6e03c6a6827beb6209e39ce3ec460a",  # revision 50: before the ShopNow-style navigation
        "3fcc073642dc12db2a015c663e9381733a822cb9d4c819f470292ccaf8910011",  # revision 50: before the ShopNow-style navigation (CRLF)
        "5f762425519ec74471a1758ad9ba87509887f7562738958bc9c54491f19ce3d4",  # revision 49: before the mega menu look settings and Shop All
        "184f6cf6357886e396e971eb1a96548a17f80878557c68e3c281b2c866a32e03",  # revision 49: before the mega menu look settings and Shop All (CRLF)
        "968932f6ec131d7062429bba9cbb3e2005fb711bf51c0f552c9a9c15ff9e78ad",  # revision 47: before the rotating announcement bar
        "617d6d3d40e3684af3e5ded9e23d75e84754c32238f7e8aba9503683192db6ed",  # revision 47: before the rotating announcement bar (CRLF)
        "2a4098b38d89db4b74379ed9d6bdbf7e344864db5bac20b2c9e96abf5c5f5db2",  # revision 41: before the announcement bar
        "9d15d2135de541b6cdc494c32c325e490d151d8e984f222015cc80b017843685",  # revision 20: legacy wishlist block fixture
        "1d988651e7e347b5e4eba971acebc1e7a11a49d8a4f9974ba3d74ea8b35ef3de",  # revision 18: legacy Show Wishlist fixture
        # Revision 40: before the header Search block
        "a4042599d876c80166e60601ed1a2763f264e79678ad99552f46c233402b1e49",  # revision 40: before the Search block
        "0f1b0c1d6442b03d7604b4a82f00e99d6d7a05685b312a621f07aace30925b8a",  # revision 39: before the sign-in icon
        "4056175621541f0119feb436226b459a59cb3b63818cf25cd04c49c6a6b9c736",  # revision 21: before the cart snippet
        "6dbee4862d37ededcdf93a750dda3f6ca60794cd16bdb1a0fa0a3995a274962d",  # revision 21: same, with CRLF line endings
        "0447235775798d7cf5839634cfea58c3437e0571c024a24c300ac2f866a85d8b",  # revision 19: before the wishlist snippet
        "5fb084f88335fd7971a07f17eb38a52fa5d81749e4ea625a1678b23f088b5a19",  # revision 19: same, with CRLF line endings
    },
    # Revision 22: the cart script also keeps a header's own cart icon and badge up to date
    # Revision 55: add-to-cart forwards data-meta fields (a gift card's recipient) as line metadata
    "snippets/cart.liquid": {
        "020c3da88a33b73edd4e0a3efc5f14d9efbaab5907f0653d7b22c9a5a2753ecc",  # revision 55: before data-meta fields were forwarded
        "156ccc2554e4ba3b9b35ab5d393db2ac38e5b483c097d9c0505c19bf1842a496",  # revision 55: same, with CRLF line endings
        "bf6180d6c1db029759afe489ea7f753f99897e16d0394f296736abe101704a31",  # revision 43: before "Check out" went to the checkout page
        "3bd8efbcc5e448be5369a998b85d83c64c02916442ada043303971153dda4e7c",  # revision 43: before "Check out" went to the checkout page (CRLF)
        "4c5704af71070bde93c5088802de1e571978e92fe093bd0a1dae721291dab8b0",  # revision 37: before "Clear cart"
        "b21d8cbf1318bf5b02f7cae3833e7d847ebbe41a22c58d0080d08378c6f3a835",  # revision 37: before "Clear cart" (CRLF)
        "477a7af3604ffac5bcd7b20d6536ba4d9cc9b399fe024ddfce348c5326da043f",  # revision 25: before the cart icon on product cards
        "727965e2164a84285cb071a2fffe9265c80be8e817428e6e18aa04203d135d9b",  # revision 25: before the cart icon on product cards (CRLF)
        "4c9f20d277bfcd838ff1dc79c9983d48fcd2979e5950c2a6b10556f905b87579",
        "2c1428519b499c93128ff4616ba32bcc3d46c3ee1acb1951c8c272def021e091",  # same, with CRLF line endings
        "15a7993c54d5ebf2dba728947c7588b6a2d051224ec312aaf438396f7a913b3a",  # revision 23: before the cart link carried the cart id
        "c2f6ce7b1f7ace894ce3e08c7db7e2c04fb23e510cd2af90918140d223d994dd",  # same, with CRLF line endings
    },
    # Revision 7: announcement bar, header (logo, menu) and footer follow the Theme settings
    "layout/theme.liquid": {
        "5d3ad0d605d0aa39aeedaa75491d8bfa746cf9f41d6a65b508aa3651c869b8b9",
        "1e15fb022e606f4564860490a7b4e62843f4cbba02475d210af15c77e8ca7c3f",  # revision 40: before the utility bar
        "4dba4b04f36e6b14a9b43dc8ced1387eda1d44a0c78d6dd4d86d9f89df15cc2d",  # revision 40: before the utility bar (CRLF)
        "0987e907fc71ca53d60862620a3adea06198870982b9895bfdf748b15594f7e3",  # revision 50: before the social link click listener
        "0dc7cbfbf4afbd1e07d916c8a0da1c239d2a28ac51595beee81fb86630c35524",  # revision 50: same, with CRLF line endings
    },
    "snippets/header.liquid": {
        "d8989f6198f2593c81303fd80a9b64eecbd0c91cda3fd05fe5297f2c2c0205d3",  # revision 51: before the mega menu panel followed the header width
        "fb57134df11b1de0f109858ee9209016634cf327f01ec87e3df2fe783225d2bb",  # revision 51: before the mega menu panel followed the header width (CRLF)
        "8cfe7c6a3f81bc906f7d9c6c75be2113e9d109e041ce033605dcce93ad0042a7",  # revision 50: before the ShopNow-style navigation
        "6842a0fcc327395aa20f37fc0980fdb1e2f0f0a5075e3a7aeeda3b5348b2010b",  # revision 50: before the ShopNow-style navigation (CRLF)
        "6e36e4b3429052522d9242b9f5cbb83c98ae509b028f38bc0381169ff4c9dec1",
    },
    "sections/footer.liquid": {
        "aed60ddb600d8c22f5c29b97a8898a9abcaa3dccc1114878e288acaa9d11a0da",  # revision 53: before the footer blocks (menu columns, social icons, accordion)
        "fe2823e8cfb36ada85b3091d01914fe232bab461e6d7918df2bc78deb9c57802",  # revision 53: same, with CRLF line endings
        "2c9bc12df53d8dbeba97ced10cbee17a107be0ca18dd58822bb09cc6cc6825bc",  # revision 45: before the footer style, utility and mobile settings
        "8fc8359490e9824b2d4c442e6793f72113569e865840dd0ca11653cbf419024e",  # revision 45: same, with CRLF line endings
        "f37dffa266ec5f3a173bf9eec4b7a87547bd9389118ffa9597aeeacf4d32ff3d",  # revision 41: before the utility bar
    },
    "snippets/footer.liquid": {"46998966bcd74dab088da530528ad25f6fac556a3cd7d2224bcdb728e6bf3573"},
    "snippets/styles.liquid": {
        "83a3d9accc2af4aa68a6a58ad84c8eb662acc3b6e422adb71458afc2d80b0079",
        "1c26147431b1ab810a18a33cb68cbc8955a685afa106874eed0ff2775d143f8e",  # revision 8: centred/right-aligned slide and hero text
    },
    # Revision 14: "Read more" link on blog post cards
    "snippets/article-card.liquid": {"d2911b0825b5f87470be4b1118e6f31617ce75c1a327d18dc2a58d52b97e38f9"},
    # Revision 3: "Sold out" badge, "Load more", sort and pages on the all-products page
    # Revision 6: the collection's image and description above its products
    "sections/main-collection.liquid": {
        "1a56191df58e3e7ca4a95510b56d90a2dcbf1e20e3ef35a2bf2cbb1dac2f061f",  # revision 58: before the product card block
        "cfd2c74149b4a3909019f3111f3a32ec1b18074ceadd92c16f726be94da60964",  # revision 58: before the product card block (CRLF)
        "ec44f24d9c12d55663679ef96219007819d220865768926f2ce855709158a7f2",  # revision 56: before the feature snippets
        "dd1f5443cf1342ec1bf3158dbe8b692216e88d04c6f06e2ff93e1f4fbcaf4ddb",  # revision 56: same, with CRLF line endings
        "ccf3d4833be7820ed1c60e86ecc807f3ba8b4350e61ffed9eb29d1106ea33c00",  # revision 40: before search results
        "bb1b45933761020191e633f0e635976d989c272c95c23653bdbb8c2fae6501f0",  # revision 40: before search results (CRLF)
        "2e2ebeacc9f1e9d175ff16ba6ab53cf5b9633838bf3a4532988d300fa8f215d7",  # revision 31: before the wishlist heart became a block
        "a6e33d8e535ba95094934f457f49dbe769020c86a41342f2ba1ea60a6232d2d2",  # revision 31: before the wishlist heart became a block (CRLF)
        "7230ca87ff484930cf8b845da2c3edf6cf6c17ee2d73fd25e7e0f24378559a5b",  # revision 27: before the cart icon became a block
        "2b0841eb55642dd3430e0e79b558a96e6c3239a09cb352dc254b321bea4142a2",  # revision 27: before the cart icon became a block (CRLF)
        "fdb1ffcf01148bfbf770f46c74e0a91bca95c84a1c4fa62798dcd452d3efd6f4",  # revision 25: before the cart icon on product cards
        "67b4adb1291c001b094cd21e8b5ca589c380910704dbbfff883dceda6c229553",  # revision 25: before the cart icon on product cards (CRLF)
        "74ee833ab04feed15b3ea97d9145a7a28dd34d17bd61035fb6c2efb43fdc8550",  # revision 25: older starter version some stores have
        "80571d6bf2f85bfe5f4dc13210c1a575a1a0c57bc4a6b8bddaeb954241d294b5",
        "878a1da3a5ee2d1bd4e8d09dfd5c44f2509a75905e0177782b3e1f06ca018567",
        "0c7227f63f577546d1926e69940b5e949b3a440f24621438231d33147d3445a8",  # revision 16: escaped pagination links, sort only with 2+ products
        "a67252e6fa4f8a542b9581022bf8037ed5facf20b03603aaaf9114ee53d82035",  # revision 18: pass wishlist state to cards
        "920955c03a592c48c8eb8abda3a32656231eff12dc1caa4acb59a3096eb1c306",  # revision 19: before the wishlist snippet
        "f5741f0851fa01f20743d4d6c0cd8080b19ac1b31f1589af909cfc8a1043a866",  # revision 19: same, with CRLF line endings
    },
    "sections/promotions.liquid": {
        "320e4ff9b5d1c7c2ce4c20dc74c151a8d1bf29211eb554e42039cccba1ef1031",  # revision 58: before the product card block
        "a7862907f4e96e9f3c1bdc2c8d601724de5e6054db86fb8f757ba18f6815370e",  # revision 58: before the product card block (CRLF)
        "cd2410ae1fb9c2213d1e549004ebf3e452af8506686638925b0aac16b8563a38",  # revision 36: before the collection's products
        "e08231172a0b48b7759637318a7175e9af93cb47ba1f5f18137b18cf213be7cd",  # revision 36: same, with CRLF line endings
        "908032a6073010b387eb6bb2af8c01c48cc511b2d50a8c6211ae46852eb116cb",  # revision 36: before the cart icon and wishlist heart blocks
        "4ee528be332fc9f1d1c96b99a0ef3572710e945ca1b93ec662bd0d3ebd6cf233",  # revision 36: same, with CRLF line endings
    },
    "snippets/account-profile.liquid": {
        "0e7113af12897548e8b99d9b42c0e5802fd385db3c14895174462047f584b3e6",  # revision 48: before the 10-digit mobile number
        "8f3610dfd87295c6e39f5342acc7cf7e8044e3e72cbb3bda15b7a12e759f3e16",  # revision 48: same, with CRLF line endings
    },
}


_CARD_PATH = "snippets/product-card.liquid"


@lru_cache(maxsize=1)
def _starter_card_digests():
    """Every version of snippets/product-card.liquid that is the starter's own: the current one (with the cart
    icon the "Cart icon" block used to add to it, too) and the older ones registered in STARTER_UPGRADES."""
    current = starter_files()[_CARD_PATH]
    digests = set(STARTER_UPGRADES.get(_CARD_PATH, ()))
    for content in (current, sync.add_card_cart_icon(current)):
        if content is not None:
            digests.add(hashlib.sha256(content.encode("utf-8")).hexdigest())
    return frozenset(digests)


def card_code_is_starter(content):
    """True while the store's snippets/product-card.liquid was never edited. A section draws its cards from its
    "Product card" block only then; an edited card keeps being drawn by the owner's own code."""
    if not isinstance(content, str):
        return False
    digests = _starter_card_digests()
    lf = content.replace("\r\n", "\n")
    return any(hashlib.sha256(text.encode("utf-8")).hexdigest() in digests for text in (content, lf))


def starter_files():
    return {
        path.relative_to(STARTER_DIR).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(STARTER_DIR.rglob("*"))
        if path.is_file() and path.suffix in (".liquid", ".css", ".js", ".json")
    }


def private_slug(store):
    return f"store-{store.store_id}"


def is_private(store, theme):
    """True for a theme that is this store's own copy (as opposed to a shared theme)."""
    return (theme.theme_data or {}).get("store_id") == str(store.store_id)


MAX_THEMES = 20

# The theme the store's admin is editing (a draft, or the published one), set per request by the
# theme API from the X-Theme-Id header. The public storefront never sets it: it shows the published theme.
_editing_theme = ContextVar("editing_theme", default=None)


def set_editing_theme(theme_id):
    return _editing_theme.set(str(theme_id) if theme_id else None)


def reset_editing_theme(token):
    _editing_theme.reset(token)


def _install(store, files, custom_data, name=None, publish=True, theme_data=None, created_by=""):
    """A new private copy of a theme for the store. Published, or kept as a draft next to the live theme."""
    base = private_slug(store)
    slug = base if not Theme.objects.filter(slug=base).exists() else f"{base}-{secrets.token_hex(4)}"
    theme, created = Theme.objects.get_or_create(
        slug=slug,
        defaults={
            "name": (name or f"{store.store_name} theme")[:100],
            "description": "This store's own copy of its theme.",
            "category": "store",
            "version": STARTER_VERSION,
            "theme_data": {"store_id": str(store.store_id), **(theme_data or {})},
        },
    )

    if created:
        ThemeFile.objects.bulk_create(
            [_new_file(theme, path, content) for path, content in files.items()]
        )

    if publish:
        StoreTheme.objects.filter(store=store, is_active=True).update(is_active=False)

    return StoreTheme.objects.create(
        store=store,
        theme=theme,
        installed_version=theme.version,
        custom_data=custom_data,
        is_active=publish,
        created_by=created_by,
    )


def _add_missing_starter_files(theme):
    """
    Themes created before the starter theme existed lack its templates,
    sections and snippets. Add only the missing ones; never overwrite.
    """
    have = set(theme.files.values_list("file_path", flat=True))

    if REQUIRED_FILES - NOT_IN_STARTER <= have:
        return

    ThemeFile.objects.bulk_create(
        [
            _new_file(theme, path, content)
            for path, content in starter_files().items()
            if path not in have
        ]
    )


def _prepare(store_theme, new_store=False):
    """Bring a private theme up to date with the starter (see the helpers it calls)."""
    _add_missing_starter_files(store_theme.theme)
    _upgrade_untouched_starter_files(store_theme.theme)
    _add_synced_sections(store_theme.theme, store_theme.custom_data)
    _add_wishlist_blocks(store_theme)
    _convert_footer_social(store_theme)
    _offer_wishlist_icon_block(store_theme.theme)
    _offer_cart_icon_block(store_theme.theme)
    _offer_search_block(store_theme)
    _keep_wishlist_hearts(store_theme, new_store=new_store)
    _add_page_templates(store_theme)
    sync_card_cart_icon(store_theme)
    _upgrade_footer_icon(store_theme.theme)


# A theme is brought up to date (_prepare) once per state of its files and settings, not on every request.
# The state is read in one cheap query; any save of a file or of the theme's settings changes it, so the
# next request prepares again. A new deploy starts with an empty cache, so starter upgrades still run, and the
# short lifetime bounds how long another worker's edit (or a write that skips updated_at) can go unseen.
_CACHE_TTL = 60
_CACHE_MAX = 256
_prepared = {}
_files_cache = {}


def _files_state(theme):
    """(file count, newest save, total size) of a theme's files: changes whenever one is added, saved or removed."""
    row = theme.files.aggregate(n=Count("pk"), newest=Max("updated_at"), size=Sum("file_size"))
    return (row["n"], row["newest"], row["size"])


def _remember(cache, key, value):
    if len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)), None)
    cache[key] = (time.monotonic(), value)


def _recall(cache, key):
    hit = cache.get(key)
    if hit is None or time.monotonic() - hit[0] > _CACHE_TTL:
        return None
    return hit[1]


def clear_theme_caches():
    """Forget what was prepared and loaded (tests, and after changes made behind the ORM's back)."""
    _prepared.clear()
    _files_cache.clear()


def _prepare_once(store_theme, new_store=False):
    theme = store_theme.theme
    key = (store_theme.pk, store_theme.theme_id)
    state = (store_theme.updated_at, theme.updated_at, _files_state(theme))
    if _recall(_prepared, key) == state:
        return
    _prepare(store_theme, new_store=new_store)
    # _prepare may have saved things: remember the state it left, read again
    store_theme.refresh_from_db(fields=["updated_at"])
    theme.refresh_from_db(fields=["updated_at"])
    _remember(_prepared, key, (store_theme.updated_at, theme.updated_at, _files_state(theme)))


def get_store_theme(store):
    """
    The theme being used: the one the admin is editing (a draft or the published one, see
    set_editing_theme), otherwise the published theme. Installs a private copy of the starter
    on first use.
    """
    wanted = _editing_theme.get()
    if wanted:
        try:
            found = StoreTheme.objects.select_related("theme").filter(store=store, theme_id=wanted).first()
        except (ValueError, ValidationError):  # not a uuid
            found = None
        if found is None or not is_private(store, found.theme):
            raise exceptions.NotFound("That theme doesn't exist.")
        _prepare_once(found)
        return found

    current = (
        StoreTheme.objects.select_related("theme")
        .filter(store=store, is_active=True)
        .first()
    )

    if current and is_private(store, current.theme):
        _prepare_once(current)
        return current

    if current:
        files = {f.file_path: f.content for f in current.theme.files.all()}
        custom_data = current.custom_data
    else:
        files, custom_data = starter_files(), {}

    try:
        with transaction.atomic():
            # Installed on first use (maybe by a shopper's visit): credited to the store's creator
            installed = _install(store, files, custom_data, created_by=store.created_by)
    except IntegrityError:
        # Another request installed it at the same moment.
        installed = StoreTheme.objects.select_related("theme").get(store=store, is_active=True)

    _prepare(installed, new_store=current is None)
    if current is None:  # a brand-new theme from the starter: new stores also get the default tree
        from .default_provisioning import provision_default_files
        provision_default_files(installed)
    return installed


# ------------------------------------------------- the store's themes: one published, the rest drafts

def store_themes(store):
    """The store's own themes, the published one first -> [StoreTheme]."""
    rows = [t for t in StoreTheme.objects.select_related("theme").filter(store=store) if is_private(store, t.theme)]
    return sorted(rows, key=lambda t: (not t.is_active, -t.theme.created_at.timestamp()))


def _theme_of(store, theme_id):
    try:
        row = StoreTheme.objects.select_related("theme").filter(store=store, theme_id=theme_id).first()
    except (ValueError, ValidationError):
        row = None
    if row is None or not is_private(store, row.theme):
        raise exceptions.NotFound("That theme doesn't exist.")
    return row


def _clean_name(name):
    name = " ".join(str(name or "").split())
    if not name:
        raise exceptions.ValidationError({"name": "Give the theme a name."})
    if len(name) > 60:
        raise exceptions.ValidationError({"name": "Use 60 characters or fewer."})
    return name


@transaction.atomic
def create_theme(store, name, files, custom_data, theme_data=None, created_by=""):
    """
    Add a theme to the store's library. The store's first theme is published (nothing is live yet);
    every later one is a draft: the published theme is not touched.
    """
    name = _clean_name(name)
    if len(store_themes(store)) >= MAX_THEMES:
        raise exceptions.ValidationError({"detail": f"A store can have at most {MAX_THEMES} themes. Delete one first."})
    first = not StoreTheme.objects.filter(store=store, is_active=True).exists()
    created = _install(store, files, custom_data, name=name, publish=first, theme_data=theme_data,
                       created_by=created_by)
    _prepare(created, new_store=True)
    return created


def duplicate_theme(store, theme_id, name, created_by=""):
    source = _theme_of(store, theme_id)
    _prepare(source)
    files = {f.file_path: f.content for f in source.theme.files.all()}
    data = {k: v for k, v in (source.theme.theme_data or {}).items() if k == "preset"}
    return create_theme(store, name, files, copy.deepcopy(source.custom_data), theme_data=data,
                        created_by=created_by)


@transaction.atomic
def publish_theme(store, theme_id):
    """Make a draft the store's live theme. The one that was live becomes a draft; nothing in it changes."""
    target = _theme_of(store, theme_id)
    if target.is_active:
        return target
    StoreTheme.objects.filter(store=store, is_active=True).update(is_active=False)
    target.is_active = True
    target.save(update_fields=["is_active", "updated_at"])
    return target


def rename_theme(store, theme_id, name):
    row = _theme_of(store, theme_id)
    row.theme.name = _clean_name(name)
    row.theme.save(update_fields=["name", "updated_at"])
    return row


@transaction.atomic
def delete_theme(store, theme_id):
    row = _theme_of(store, theme_id)
    if row.is_active:
        raise exceptions.ValidationError({"detail": "This theme is published. Publish another theme first, then delete this one."})
    theme = row.theme
    row.delete()
    ThemeFile.objects.filter(theme=theme).delete()
    theme.delete()


@transaction.atomic
def remove_store_themes(store):
    """
    For a store being permanently deleted (tenancy.services.delete_store): its theme assignments go,
    and each theme they used goes too only when it is this store's own copy (is_private) and no other
    store's assignment still uses it. Shared themes, and other stores' assignments, files and
    customizations, are never touched.
    """
    themes = {row.theme_id: row.theme for row in StoreTheme.objects.select_related("theme").filter(store=store)}
    StoreTheme.objects.filter(store=store).delete()
    for theme in themes.values():
        if is_private(store, theme) and not StoreTheme.objects.filter(theme=theme).exists():
            ThemeFile.objects.filter(theme=theme).delete()
            theme.delete()


def _upgrade_untouched_starter_files(theme):
    """Bring never-edited starter files up to date (see STARTER_UPGRADES). Runs once per revision."""
    theme_data = theme.theme_data or {}
    if theme_data.get("starter_revision", 1) >= STARTER_REVISION:
        return

    current = starter_files()
    for file in theme.files.filter(file_path__in=STARTER_UPGRADES):
        digest = hashlib.sha256(file.content.encode("utf-8")).hexdigest()
        new = current.get(file.file_path)
        if new is not None and digest in STARTER_UPGRADES[file.file_path] and file.content != new:
            section = sync.PATH_TO_SECTION.get(file.file_path)
            if section:
                # A synced section (the header): keep the replaced file in its history, like any other change
                sync.record_version(theme, section, file.content, "upgrade")
                sync.invalidate(theme.pk, file.file_path)
            file.content = new
            file.file_size = len(new.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])

    theme.theme_data = {**(theme.theme_data or {}), "starter_revision": STARTER_REVISION}
    theme.save(update_fields=["theme_data", "updated_at"])


def _add_wishlist_blocks(store_theme):
    """
    Once per store: the header's heart and the product page's heart became blocks ("Wishlist icon",
    "Wishlist heart"). A store that showed them keeps them, so nothing disappears. Only where its files
    render the blocks: a file the owner edited keeps its own markup and its own settings.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("wishlist_blocks"):
        return
    header_path, product_path = "sections/header.liquid", "sections/main-product.liquid"
    files = dict(theme.files.filter(file_path__in=[header_path, product_path]).values_list("file_path", "content"))

    saved = (store_theme.custom_data.get("sections") or {}).get("header")
    if saved is not None and "{% render 'wishlist', mode: 'icon'" in files.get(header_path, ""):
        settings = dict(saved.get("settings") or {})
        blocks = [b for b in saved.get("blocks") or [] if isinstance(b, dict)]
        if settings.pop("show_wishlist", True) and not any(b.get("type") == "wishlist" for b in blocks):
            blocks.append({"type": "wishlist", "settings": {"label": "Wishlist", "show_count": True}})
            try:
                save_section_values(store_theme, "header", {"settings": settings, "blocks": blocks}, client="upgrade")
            except exceptions.ValidationError:
                pass  # the header code is not valid right now: it keeps working as it is

    if "section.wishlist_heart" in files.get(product_path, ""):
        store_theme.refresh_from_db(fields=["custom_data"])
        templates = copy.deepcopy(store_theme.custom_data.get("templates") or {})
        changed = False
        for section in ((templates.get("product") or {}).get("sections") or {}).values():
            if section.get("type") == "main-product" and not any(
                b.get("type") == "wishlist_heart" for b in section.get("blocks") or []
            ):
                section["blocks"] = [*(section.get("blocks") or []), {"type": "wishlist_heart", "settings": {}}]
                changed = True
        if changed:
            store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
            store_theme.save(update_fields=["custom_data", "updated_at"])

    theme.theme_data = {**(theme.theme_data or {}), "wishlist_blocks": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


FOOTER_SOCIAL_NETWORKS = ("instagram", "facebook", "x", "tiktok", "youtube", "pinterest", "linkedin")


def _footer_social_lines(settings):
    """The footer's Social links in the "network | url" lines of its icons setting."""
    return "\n".join(f"{n} | {settings[n].strip()}" for n in FOOTER_SOCIAL_NETWORKS if str(settings.get(n) or "").strip())


def _convert_footer_social(store_theme):
    """
    Once per store, when its footer file offers social icons: a Social links block that has one address per
    network (instagram, facebook, ...) becomes a list of icons, with the same networks in the same order.
    A footer file the owner edited keeps the old settings, and is looked at again later.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("footer_social_icons"):
        return
    file = theme.files.filter(file_path="sections/footer.liquid").first()
    if file is None or '"id": "icons"' not in file.content:
        return
    saved = (store_theme.custom_data.get("sections") or {}).get("footer")
    if saved:
        blocks, changed = [], False
        for block in saved.get("blocks") or []:
            settings = block.get("settings") if isinstance(block, dict) else None
            if block.get("type") == "social" and isinstance(settings, dict) and "icons" not in settings:
                rest = {k: v for k, v in settings.items() if k not in FOOTER_SOCIAL_NETWORKS}
                block = {**block, "settings": {**rest, "icons": _footer_social_lines(settings)}}
                changed = True
            blocks.append(block)
        if changed:
            store_theme.custom_data = _with_section(store_theme.custom_data, "footer", {**saved, "blocks": blocks})
            store_theme.save(update_fields=["custom_data", "updated_at"])
    theme.theme_data = {**(theme.theme_data or {}), "footer_social_icons": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


def _card_sections(templates):
    """
    Every section of the pages that shows product cards with the old card and can have the "Cart icon" block.
    A section with a "Product card" block draws its own card (cart icon inside it), so the card's code and the
    blocks never touch it: the two ways of drawing a card stay apart.
    """
    for page in templates.values():
        for section in page["sections"].values():
            blocks = SECTION_SCHEMAS.get(section.get("type"), {}).get("blocks", {})
            if "cart_icon" in blocks and section.get("type") != "main-product" \
                    and not any(b.get("type") == "card_layout" for b in section.get("blocks") or []):
                yield section


def _has_cart_icon_block(templates):
    return any(b["type"] == "cart_icon" for s in _card_sections(templates) for b in s.get("blocks") or [])


def _has_product_card_block(templates):
    return any(
        b.get("type") == "card_layout"
        for page in templates.values() for s in page["sections"].values() for b in s.get("blocks") or []
    )


def sync_card_cart_icon(store_theme, before=None):
    """
    Blocks -> Code: the sections' "Cart icon" blocks were saved (customizer, or templates/<page>.json), or the
    store's files were just brought up to date. While a section has the block, the card's code has the icon: it
    is added to snippets/product-card.liquid if it is missing. When the last block was just removed (`before`
    is the pages as they were), the icon is taken out of the card's code. Everything else in the file stays.
    """
    file = store_theme.theme.files.filter(file_path=sync.CARD_PATH).first()
    if file is None:
        return
    templates = resolve(store_theme.custom_data)["templates"]
    if _has_cart_icon_block(templates):
        patched = sync.add_card_cart_icon(file.content)
    elif before is not None and _has_cart_icon_block(before) and not _has_product_card_block(templates):
        # (A section that just moved to a "Product card" block took its icon with it: the card's code is not touched then.)
        patched = sync.remove_card_cart_icon(file.content)
    else:
        return
    if patched is None or patched == file.content:
        return
    try:
        _check_liquid(patched)
    except exceptions.ValidationError:
        return  # the card's own code can't take it: it keeps what it has
    file.content = patched
    file.file_size = len(patched.encode("utf-8"))
    file.save(update_fields=["content", "file_size", "updated_at"])


def _blocks_from_card_code(store_theme, new):
    """
    Code -> Blocks: snippets/product-card.liquid was saved. While it has the icon, every product section that
    has no "Cart icon" block gets one (blocks that are there, hidden or not, are left as they are); when the icon
    is not in it, the blocks go (the code has no icon to show).
    """
    has = sync.has_card_cart_icon(new)
    merged = resolve(store_theme.custom_data)
    changed = False
    for section in _card_sections(merged["templates"]):
        blocks = section.get("blocks") or []
        if has:
            if not any(b["type"] == "cart_icon" for b in blocks):
                section["blocks"] = [*blocks, {"type": "cart_icon", "hidden": False, "settings": {}}]
                changed = True
        else:
            kept = [b for b in blocks if b["type"] != "cart_icon"]
            if len(kept) != len(blocks):
                section["blocks"] = kept
                changed = True
    if changed:
        store_theme.custom_data = validate_custom_data(merged)
        store_theme.save(update_fields=["custom_data", "updated_at"])


def _offer_cart_icon_block(theme):
    """
    Once per store: a header the owner edited, or one that came from an older starter, does not offer the
    "Cart icon" block, so it is missing from "Add block". Same as the wishlist icon (see
    _offer_wishlist_icon_block): the block type and its loop are inserted into the header, nothing else
    changes, the replaced code is kept in the header's history, and a header the insert cannot be made
    into safely is left exactly as it is. No cart icon is added to the page: the owner adds the block.
    """
    if (theme.theme_data or {}).get("cart_icon_offered"):
        return
    file = theme.files.filter(file_path="sections/header.liquid").first()
    patched = sync.add_cart_block(file.content) if file else None
    if patched is not None:
        sync.record_version(theme, "header", file.content, "upgrade")
        file.content = patched
        file.file_size = len(patched.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        sync.invalidate(theme.pk, file.file_path)
    theme.theme_data = {**(theme.theme_data or {}), "cart_icon_offered": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


def _offer_search_block(store_theme):
    """Add Search to an existing header by insertion only, and append it to saved block values."""
    theme = store_theme.theme
    if (theme.theme_data or {}).get("search_block_offered") == 2:
        return
    file = theme.files.filter(file_path="sections/header.liquid").first()
    if file:
        content = file.content
        try:
            schema = sync.parse(content, "sections/header.liquid")
        except sync.SectionCodeError:
            schema = None
        supports_search = bool(
            schema
            and any(block.get("type") == "search" for block in schema.get("blocks") or [])
            and "data-header-search" in content
        )
        # Also run the safe patch on already-upgraded sections so a legacy show_search
        # link left beside the Search block is removed from the same historical code row.
        patched = sync.add_search_block(content)
        if patched is not None and patched != content:
            sync.record_version(theme, "header", content, "upgrade")
            file.content = patched
            file.file_size = len(patched.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
            sync.invalidate(theme.pk, file.file_path)
            content = patched
            supports_search = True

        if supports_search:
            # A preceding legacy migration may have written the saved block list to the schema defaults.
            # Append Search to that list too, without replacing its order or settings.
            try:
                schema = sync.parse(content, "sections/header.liquid")
                spec = sync.spec_of("header", schema)
                defaults = sync.defaults_of(spec, schema)
            except sync.SectionCodeError:
                defaults = None
            if defaults is not None:
                blocks, seen_search = [], False
                for block in defaults["blocks"]:
                    if block.get("type") == "search":
                        if seen_search:
                            continue
                        seen_search = True
                    blocks.append(block)
                defaults["blocks"] = blocks
                if not seen_search:
                    defaults["blocks"].append({"type": "search", "settings": {"label": "Search", "placeholder": "Search products"}})
                updated = sync.write_values(content, defaults)
                if updated != content:
                    sync.record_version(theme, "header", content, "upgrade")
                    file.content = updated
                    file.file_size = len(updated.encode("utf-8"))
                    file.save(update_fields=["content", "file_size", "updated_at"])
                    sync.invalidate(theme.pk, file.file_path)

            sections = store_theme.custom_data.get("sections") or {}
            saved = sections.get("header")
            if saved is not None:
                values = copy.deepcopy(saved)
                blocks = values.get("blocks")
                if not isinstance(blocks, list):
                    blocks = []
                unique_blocks, seen_search = [], False
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "search":
                        if seen_search:
                            continue
                        seen_search = True
                    unique_blocks.append(block)
                if not seen_search:
                    unique_blocks.append({"type": "search", "settings": {"label": "Search", "placeholder": "Search products"}})
                if unique_blocks != blocks:
                    values["blocks"] = unique_blocks
                    store_theme.custom_data = _with_section(store_theme.custom_data, "header", values)
                    store_theme.save(update_fields=["custom_data", "updated_at"])

    # Done once. A header that does not match the known safe schema/loop shape is left alone.
    theme.theme_data = {**(theme.theme_data or {}), "search_block_offered": 2}
    theme.save(update_fields=["theme_data", "updated_at"])


def _offer_wishlist_icon_block(theme):
    """
    Once per store: a header the owner edited, or one that came from an older starter, may not offer the
    "Wishlist icon" block, so it is missing from "Add block". It is added by inserting only: the block type
    into the schema's "blocks" list, and the loop that renders it unless the header has one already
    (see section_sync.add_wishlist_block). Nothing else in the header changes, the replaced code is kept
    in the header's history, and a header the insert cannot be made into safely is left exactly as it is.
    Done once: an owner who later removes the block type from the code does not get it back.
    """
    if (theme.theme_data or {}).get("wishlist_icon_offered"):
        return
    file = theme.files.filter(file_path="sections/header.liquid").first()
    patched = sync.add_wishlist_block(file.content) if file else None
    if patched is not None:
        sync.record_version(theme, "header", file.content, "upgrade")
        file.content = patched
        file.file_size = len(patched.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        sync.invalidate(theme.pk, file.file_path)
    theme.theme_data = {**(theme.theme_data or {}), "wishlist_icon_offered": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


# Sections that render product cards: their "Show wishlist heart" setting
CARD_SECTIONS = ("product-grid", "product-carousel", "main-collection")


def _keep_wishlist_hearts(store_theme, new_store=False):
    """
    Once per store: "Show wishlist heart" is now off unless the owner turns it on, and the product page
    has no "Wishlist heart" block unless the owner adds it. A store that exists already showed both, so
    it keeps them: show_wishlist = true is saved into every card section that never saved the setting,
    and the product page gets its block.

    Telling the stores apart:
    - a NEW store (its theme is created just now) has nothing to keep: it starts with the hearts off;
    - an existing store's section that has no "show_wishlist" in its saved settings never saved the
      setting, so it has been showing the heart (it was on by default): true is saved. A section that
      has the key, true or false, is the owner's choice and is left alone;
    - a page the owner never customized (no saved template) shows its default sections, hearts
      included: those default sections are saved as they are, with the heart on.
    Runs inside get_store_theme, i.e. before any request reads the settings, so the customizer never
    sees (and saves back) the new default for a section that was showing the heart.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("wishlist_hearts"):
        return
    if not new_store:
        product_file = theme.files.filter(file_path="sections/main-product.liquid").values_list("content", flat=True).first()
        renders_block = "section.wishlist_heart" in (product_file or "")
        templates = copy.deepcopy(store_theme.custom_data.get("templates") or {})
        changed = False
        for page in ("index", "collection", "product"):
            if page not in templates:
                templates[page] = {**default_template(page), "_default": True}
                changed = True
        for page in templates.values():
            was_default = page.pop("_default", False)
            for section in (page.get("sections") or {}).values():
                kind = section.get("type")
                if kind in CARD_SECTIONS:
                    if not isinstance(section.get("settings"), dict):
                        section["settings"] = {}
                    if "show_wishlist" not in section["settings"]:
                        section["settings"]["show_wishlist"] = True
                        changed = True
                elif kind == "main-product" and was_default and renders_block:
                    section["blocks"] = [*(section.get("blocks") or []), {"type": "wishlist_heart", "settings": {}}]
        if changed:
            store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
            store_theme.save(update_fields=["custom_data", "updated_at"])
    theme.theme_data = {**(theme.theme_data or {}), "wishlist_hearts": 1}
    theme.save(update_fields=["theme_data", "updated_at"])


def theme_files(store_theme):
    """The theme's Liquid files (what the renderer loads)."""
    theme = store_theme.theme
    state = _files_state(theme)
    hit = _recall(_files_cache, theme.pk)
    if hit is None or hit[0] != state:
        hit = (state, {f.file_path: f.content for f in theme.files.order_by("file_path")})
        _remember(_files_cache, theme.pk, hit)
    return dict(hit[1])  # callers add and replace entries


# ------------------------------------------------- settings as code
#
# The customizer's choices (store-wide settings, and each page's sections and
# blocks) are also shown in the code editor as JSON files, like Shopify's
# config/settings_data.json and templates/*.json. They are not stored as theme
# files: they are generated from StoreTheme.custom_data when the file list is
# read, and saving one validates it and writes it back to custom_data. So an
# edit in the customizer shows up in the code editor, and an edit in the code
# shows up in the customizer, from one source of truth.

SETTINGS_FILE = "config/settings_data.json"
TEMPLATE_FILES = {f"templates/{page}.json": page for page in PAGES}
SETTINGS_FILES = (SETTINGS_FILE, *TEMPLATE_FILES, "assets/images.json")
# Alternate page templates (page.contact, ...) have a settings file and a Liquid file each
PAGE_TEMPLATE_JSON = re.compile(r"^templates/(page\.[a-z0-9]+(?:-[a-z0-9]+)*)\.json$")
PAGE_TEMPLATE_LIQUID = re.compile(r"^templates/(page\.[a-z0-9]+(?:-[a-z0-9]+)*)\.liquid$")
PAGE_TEMPLATES_REVISION = 1
DEFAULT_PAGE_LIQUID = (
    "{%- comment -%}\n"
    "  Page template. `sections` is the HTML of this template's sections;\n"
    "  `page` is the page being shown. Add your own Liquid around {{ sections }}.\n"
    "{%- endcomment -%}\n"
    "{{ sections }}\n"
)


def page_template_keys(custom_data):
    """The alternate page templates the store has: ["page.about", "page.contact", ...]."""
    return sorted(key for key in resolve(custom_data)["templates"] if key not in PAGES)


def settings_paths(store_theme):
    """Every settings file of the theme, including the alternate page templates'."""
    return (*SETTINGS_FILES, *(f"templates/{key}.json" for key in page_template_keys(store_theme.custom_data)))


def _is_settings_path(path):
    return path in SETTINGS_FILES or bool(PAGE_TEMPLATE_JSON.match(path))


def _dump(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def settings_files(store_theme):
    """{path: JSON text} of the customizer's settings, with defaults filled in."""
    data = resolve(store_theme.custom_data)
    files = {SETTINGS_FILE: _dump(data["settings"])}
    for path, page in TEMPLATE_FILES.items():
        files[path] = _dump(data["templates"][page])
    for key in page_template_keys(store_theme.custom_data):
        files[f"templates/{key}.json"] = _dump(data["templates"][key])
    return files


IMAGES_FILE = "assets/images.json"


def images_file(store_theme):
    """The pictures uploaded in the customizer (stored in the database), listed as read-only JSON."""
    from apps.media.models import MediaAsset

    from .models import ThemeImage

    used = _dump(store_theme.custom_data)
    rows = [
        (asset.id, asset.mime_type, asset.file_size, asset.created_at)
        for asset in MediaAsset.objects.filter(store_id=store_theme.store_id).defer("data")
    ] + [  # product photos and older pictures, kept in the older table
        (image.id, image.content_type, image.size, image.created_at)
        for image in ThemeImage.objects.filter(store_id=store_theme.store_id).defer("data")
    ]
    images = []
    for image_id, content_type, size, created_at in sorted(rows, key=lambda row: row[3], reverse=True):
        key = str(image_id)
        images.append({
            "id": key,
            "url": f"/s/_img/{key}",
            "type": content_type,
            "bytes": size,
            "uploaded": created_at.isoformat(timespec="seconds"),
            "in_use": key in used,
        })
    return _dump({
        "_note": "Pictures uploaded in the customizer (see Media assets). They are stored in the database; use the url in a "
                 "Liquid file or pick the picture in the customizer. This file is read-only.",
        "images": images,
    })


def code_files(store_theme):
    """Everything the code editor lists: the Liquid files, the settings files and the uploaded images."""
    return {**theme_files(store_theme), **settings_files(store_theme), IMAGES_FILE: images_file(store_theme)}


def _settings_conflict(store_theme, path):
    """A path that is, or holds, a settings file: those can't be deleted, moved or replaced."""
    path = path.strip("/")
    if any(p == path or p.startswith(path + "/") for p in settings_paths(store_theme)):
        raise exceptions.ValidationError(
            "Settings files are managed by the customizer. Edit what is inside them instead of deleting or renaming them."
        )


def _parse_json(path, content):
    try:
        return json.loads(content)
    except json.JSONDecodeError as error:
        raise exceptions.ValidationError(
            {"content": f"{path} is not valid JSON: {error.msg} (line {error.lineno}, column {error.colno})."}
        )


_ERROR_WHERE = re.compile(r"sections\.([A-Za-z0-9_-]+)(?:\.blocks\[(\d+)\])?(?:\.settings\.([A-Za-z0-9_]+))?")


def _json_error_line(content, message):
    """The line of the section, block or setting a validation message is about (None when it can't be found)."""
    found = _ERROR_WHERE.search(message)
    if not found:
        return None
    section_id, block_index, setting = found.group(1), found.group(2), found.group(3)
    unknown = re.search(r"unknown settings: ([A-Za-z0-9_]+)", message)
    if setting is None and unknown:
        setting = unknown.group(1)
    decoder = json.JSONDecoder()
    # The section's key inside "sections" (its id also appears earlier, in the "order" list)
    sections_at = re.search(r'"sections"\s*:', content)
    key_at = re.compile(r'"%s"\s*:' % re.escape(section_id)).search(content, sections_at.end() if sections_at else 0)
    if not key_at:
        return None
    at = key_at.start()
    if block_index is not None:
        key = content.find('"blocks"', at)
        start = content.find("[", key) if key >= 0 else -1
        if start < 0:
            return content.count("\n", 0, at) + 1
        pos = start + 1
        for _ in range(int(block_index) + 1):
            while pos < len(content) and content[pos] in " \t\r\n,":
                pos += 1
            block_start = pos
            try:
                _, pos = decoder.raw_decode(content, pos)
            except ValueError:
                return content.count("\n", 0, at) + 1
        at = block_start
        if setting:
            inside = content.find(f'"{setting}"', at, pos)
            if inside >= 0:
                at = inside
    return content.count("\n", 0, at) + 1


def _save_settings_file(store_theme, path, content):
    parsed = _parse_json(path, content)
    if not isinstance(parsed, dict):
        raise exceptions.ValidationError({"content": f"{path} must contain a JSON object."})

    merged = resolve(store_theme.custom_data)
    if path == SETTINGS_FILE:
        merged["settings"] = parsed
    else:
        page = TEMPLATE_FILES.get(path) or PAGE_TEMPLATE_JSON.match(path).group(1)
        if set(parsed) - {"order", "sections"}:
            raise exceptions.ValidationError({"content": f"{path} may only contain \"order\" and \"sections\"."})
        merged["templates"][page] = parsed

    before = resolve(store_theme.custom_data)["templates"]
    try:
        store_theme.custom_data = validate_custom_data(merged)
    except exceptions.ValidationError as error:
        # Say which file the problem is in, and (for a section or block) which line
        message = " ".join(str(m) for m in _messages(error.detail))
        line = _json_error_line(content, message)
        detail = {"content": f"{path}{f' (line {line})' if line else ''}: {message}"}
        if line:
            detail["line"] = line
        raise exceptions.ValidationError(detail)
    store_theme.save(update_fields=["custom_data", "updated_at"])
    if PAGE_TEMPLATE_JSON.match(path):
        _ensure_page_file(store_theme.theme, page)
    already = templates_changed(store_theme, before, source="code")
    _publish_json_edit(store_theme, before, already)
    return SimpleNamespace(file_path=path, file_size=len(content.encode("utf-8")))


@transaction.atomic
def _publish_json_edit(store_theme, before, already):
    """
    templates/<page>.json was saved with a change inside a section (a new or edited block, a setting) that
    leaves the template's section lines alone: tell the open customizer anyway, so its sidebar and preview follow.
    """
    for name, config in sync.SYNCED_TEMPLATES.items():
        page = config["page"]
        after = resolve(store_theme.custom_data)["templates"][page]
        if name in already or after == before[page]:
            continue
        theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
        file = theme.files.filter(file_path=config["path"]).first()
        if file is None or sync.is_legacy_template(file.content):
            continue
        version = sync.record_version(theme, name, file.content, "code")
        theme.save(update_fields=["theme_data", "updated_at"])
        _publish_template(store_theme, name, "code", version, after, file.content, "")


def _messages(detail):
    if isinstance(detail, dict):
        return [m for value in detail.values() for m in _messages(value)]
    if isinstance(detail, (list, tuple)):
        return [m for value in detail for m in _messages(value)]
    return [detail]


# ------------------------------------------------- alternate page templates (page.contact, ...)
#
# A page template is templates/page.<name>.liquid (the markup) plus its sections in
# custom_data["templates"]["page.<name>"] (shown as templates/page.<name>.json). Each store page
# picks one (StorePage.template; empty = the base Page template).

def _ensure_page_file(theme, key, content=DEFAULT_PAGE_LIQUID):
    path = f"templates/{key}.liquid"
    if not theme.files.filter(file_path=path).exists():
        _new_file(theme, path, content).save()


def _ensure_page_data(store_theme, key, template=None):
    """The template's sections in custom_data (the Page template's defaults when none are given)."""
    templates = dict(store_theme.custom_data.get("templates") or {})
    if key in templates:
        return
    templates[key] = template if template is not None else default_template(key)
    store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
    store_theme.save(update_fields=["custom_data", "updated_at"])


def _forget_page_template(store_theme, key):
    """Its file is gone: drop its sections and send its pages back to the base Page template."""
    templates = dict(store_theme.custom_data.get("templates") or {})
    if key in templates:
        templates.pop(key)
        store_theme.custom_data = {**store_theme.custom_data, "templates": templates}
        store_theme.save(update_fields=["custom_data", "updated_at"])
    StorePage.objects.filter(store_id=store_theme.store_id, template=key).update(template="")


def template_label(key):
    return "Page" if key == "page" else key.split(".", 1)[1].replace("-", " ").capitalize()


def page_templates(store_theme):
    """[{key, label, path, liquid_path}] for the base Page template and every alternate one."""
    keys = ["page", *page_template_keys(store_theme.custom_data)]
    return [
        {"key": key, "label": template_label(key), "path": f"templates/{key}.json", "liquid_path": f"templates/{key}.liquid"}
        for key in keys
    ]


def _make_page_template(store_theme, key, header=""):
    """templates/<key>.liquid (the base Page's Liquid, under `header`) and the base Page's sections."""
    theme = store_theme.theme
    base = theme.files.filter(file_path="templates/page.liquid").values_list("content", flat=True).first()
    with transaction.atomic():
        _ensure_page_file(theme, key, header + (base if base is not None else DEFAULT_PAGE_LIQUID))
        _ensure_page_data(store_theme, key, copy.deepcopy(resolve(store_theme.custom_data)["templates"]["page"]))
    return key


def _template_exists(store_theme, key):
    return key in page_template_keys(store_theme.custom_data) or         store_theme.theme.files.filter(file_path=f"templates/{key}.liquid").exists()


def create_page_template(store_theme, name):
    """A new page template that starts as a copy of the base Page template (its Liquid and sections)."""
    slug = slugify(name or "")[:30].strip("-")
    if not slug or not PAGE_TEMPLATE.match(f"page.{slug}"):
        raise exceptions.ValidationError({"name": "Give the template a name (letters and numbers)."})
    key = f"page.{slug}"
    if _template_exists(store_theme, key):
        raise exceptions.ValidationError({"name": "A page template with that name already exists."})
    if len(page_template_keys(store_theme.custom_data)) >= MAX_PAGE_TEMPLATES:
        raise exceptions.ValidationError({"name": f"A theme can have at most {MAX_PAGE_TEMPLATES} page templates."})
    return _make_page_template(store_theme, key)


def create_template_for_page(store_theme, page):
    """
    A template of its own for a new store page: templates/page.<handle>.liquid (and .json) appear in
    the code editor and the customizer's page list. Returns its key, or "" when the theme is at its limit.
    """
    if len(page_template_keys(store_theme.custom_data)) >= MAX_PAGE_TEMPLATES:
        return ""
    base = slugify(page.handle or page.title or "")[:26].strip("-") or "page"
    slug, n = base, 2
    while not PAGE_TEMPLATE.match(f"page.{slug}") or _template_exists(store_theme, f"page.{slug}"):
        slug, n = f"{base}-{n}", n + 1
    key = f"page.{slug}"
    title = (page.title or "").replace("-%}", "").replace("{%", "").replace("%}", "")
    header = "\n".join([
        "{%- comment -%}",
        f"  Template of the page \"{title}\" (/pages/{page.handle}). Edit the Liquid below, or its sections",
        f"  in templates/{key}.json or the theme editor.",
        "{%- endcomment -%}",
        "",
    ])
    return _make_page_template(store_theme, key, header)


def _add_page_templates(store_theme):
    """
    The starter's page templates (About, Contact, FAQ, Policy) for themes that don't have them yet.
    Runs once per theme: a template the store deletes later is not brought back.
    """
    theme = store_theme.theme
    if (theme.theme_data or {}).get("page_templates", 0) >= PAGE_TEMPLATES_REVISION:
        return
    with transaction.atomic():
        locked = Theme.objects.select_for_update().get(pk=theme.pk)
        data = locked.theme_data or {}
        if data.get("page_templates", 0) < PAGE_TEMPLATES_REVISION:
            starter = starter_files()
            have = set(locked.files.values_list("file_path", flat=True))
            ThemeFile.objects.bulk_create([
                _new_file(locked, f"templates/{key}.liquid", starter[f"templates/{key}.liquid"])
                for key in STARTER_PAGE_TEMPLATES
                if f"templates/{key}.liquid" not in have and f"templates/{key}.liquid" in starter
            ])
            for key, template in STARTER_PAGE_TEMPLATES.items():
                _ensure_page_data(store_theme, key, copy.deepcopy(template))
            locked.theme_data = {**data, "page_templates": PAGE_TEMPLATES_REVISION}
            locked.save(update_fields=["theme_data", "updated_at"])
    theme.theme_data = locked.theme_data


# ---------------------------------------------------------------- files


def clean_path(path):
    if not isinstance(path, str):
        raise exceptions.ValidationError({"path": "Path is required."})

    path = path.strip().strip("/")
    parts = path.split("/")
    names_ok = all(_SEGMENT.match(p) for p in parts[:-1]) and (
        parts[-1] == KEEP or _SEGMENT.match(parts[-1]) or is_private_block(path)
    )

    if not path or len(path) > 500 or not names_ok:
        raise exceptions.ValidationError(
            {"path": "Use letters, numbers, dots, dashes and underscores, separated by /."}
        )
    if ".." in parts:
        raise exceptions.ValidationError({"path": "Path may not contain '..'."})

    name = parts[-1]
    allowed_exts = (".liquid", ".css", ".js", ".json")
    if name != KEEP and not any(name.lower().endswith(ext) for ext in allowed_exts):
        raise exceptions.ValidationError({"path": "Only Liquid (.liquid), CSS (.css), JavaScript (.js) and JSON (.json) files are allowed."})

    return path


def _new_file(theme, path, content):
    directory, _, name = path.rpartition("/")
    ext = name.rpartition(".")[2].lower() if "." in name else "liquid"
    return ThemeFile(
        theme=theme,
        file_path=path,
        file_name=name,
        directory=(directory.split("/")[0] if directory else "")[:100],
        file_type="keep" if name == KEEP else ext,
        content=content,
        is_required=path in REQUIRED_FILES,
        file_size=len(content.encode("utf-8")),
    )


# Product list code (the card every list renders, and the Product grid section) and the Promotions
# section: code that doesn't compile is refused (with its line), so the saved version keeps the home page working
CHECKED_FILES = {"snippets/product-card.liquid", "snippets/product-card-blocks.liquid", "sections/product-grid.liquid",
                 "sections/promotions.liquid"}


def _check_liquid(content):
    try:
        Environment().from_string(content)
    except LiquidError as error:
        token = getattr(error, "token", None)
        line = sync.line_of(content, token.start_index) if token is not None and token.start_index is not None else None
        message = f"Liquid error: {getattr(error, 'message', None) or error}"
        raise exceptions.ValidationError({"content": f"Line {line}: {message}" if line else message, "line": line})


def save_file(store_theme, path, content, base_version=None, client="", started_at=None):
    if isinstance(path, str) and path.strip().strip("/") == IMAGES_FILE:
        raise exceptions.ValidationError({"content": "assets/images.json lists your uploaded pictures and can't be edited."})

    if isinstance(path, str) and _is_settings_path(path.strip().strip("/")):
        path = path.strip().strip("/")
        if not isinstance(content, str):
            raise exceptions.ValidationError({"content": "Content must be text."})
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise exceptions.ValidationError({"content": "File is larger than 256 KB."})
        return _save_settings_file(store_theme, path, content)

    path = clean_path(path)

    if not isinstance(content, str):
        raise exceptions.ValidationError({"content": "Content must be text."})
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise exceptions.ValidationError({"content": "File is larger than 256 KB."})
    if path.lower().endswith(".json"):  # plain JSON (locales, settings_schema, section groups)
        _parse_json(path, content)

    synced_name = sync.PATH_TO_SECTION.get(path)
    if synced_name and not (sync.SYNCED_FILES[synced_name]["kind"] == "template" and sync.is_legacy_template(content)):
        return save_synced_code(store_theme, sync.PATH_TO_SECTION[path], content, base_version=base_version, client=client,
                                started_at=started_at)

    page_template = PAGE_TEMPLATE_LIQUID.match(path)
    if path in CHECKED_FILES or page_template:
        _check_liquid(content)

    theme = store_theme.theme
    existing = theme.files.filter(file_path=path).first()

    if existing:
        existing.content = content
        existing.file_size = len(content.encode("utf-8"))
        existing.save(update_fields=["content", "file_size", "updated_at"])
        if path == sync.CARD_PATH:
            _blocks_from_card_code(store_theme, content)
        return existing

    check_new_path(path)
    if theme.files.count() >= MAX_FILES:
        raise exceptions.ValidationError({"path": f"A theme can have at most {MAX_FILES} files."})

    if page_template and page_template.group(1) not in page_template_keys(store_theme.custom_data) \
            and len(page_template_keys(store_theme.custom_data)) >= MAX_PAGE_TEMPLATES:
        raise exceptions.ValidationError({"path": f"A theme can have at most {MAX_PAGE_TEMPLATES} page templates."})

    new = _new_file(theme, path, content)
    new.save()
    if page_template:
        # A new templates/page.<name>.liquid is a new template in the customizer's page list
        _ensure_page_data(store_theme, page_template.group(1))
    return new


def _matching(theme, path):
    """The file at `path`, or every file inside the folder `path`."""
    files = list(theme.files.filter(file_path=path))
    return files or list(theme.files.filter(file_path__startswith=path + "/"))


@transaction.atomic
def delete_path(store_theme, path):
    path = (path or "").strip().strip("/")
    if path:
        _settings_conflict(store_theme, path)
    files = _matching(store_theme.theme, path) if path else []

    if not files:
        raise exceptions.NotFound("No such file or folder.")

    required = [f.file_path for f in files if f.file_path in REQUIRED_FILES]
    if required:
        raise exceptions.ValidationError(f"{required[0]} is required by the theme and can't be deleted.")

    removed_templates = [m.group(1) for f in files if (m := PAGE_TEMPLATE_LIQUID.match(f.file_path))]
    ThemeFile.objects.filter(pk__in=[f.pk for f in files]).delete()
    for key in removed_templates:
        _forget_page_template(store_theme, key)
    return len(files)


@transaction.atomic
def rename_path(store_theme, source, target):
    source = (source or "").strip().strip("/")
    theme = store_theme.theme
    if source:
        _settings_conflict(store_theme, source)
    files = _matching(theme, source) if source else []

    if not files:
        raise exceptions.NotFound("No such file or folder.")

    is_folder = files[0].file_path != source
    target = target.strip().strip("/") if isinstance(target, str) else target
    # A folder target is checked through a file name inside it.
    check_new_path(clean_path(f"{target}/{KEEP}" if is_folder else target))

    moves = {
        f.file_path: target + f.file_path[len(source):] if is_folder else target
        for f in files
    }

    if any(old in REQUIRED_FILES for old in moves):
        raise exceptions.ValidationError("Required theme files can't be moved or renamed.")
    if any(PAGE_TEMPLATE_LIQUID.match(old) or PAGE_TEMPLATE_LIQUID.match(new) for old, new in moves.items()):
        raise exceptions.ValidationError(
            "Page templates can't be renamed or moved. Create a new template and delete this one."
        )
    if theme.files.filter(file_path__in=list(moves.values())).exclude(pk__in=[f.pk for f in files]).exists():
        raise exceptions.ValidationError({"target": "A file with that name already exists."})

    for f in files:
        updated = _new_file(theme, moves[f.file_path], f.content)
        f.file_path, f.file_name, f.directory, f.file_type = (
            updated.file_path, updated.file_name, updated.directory, updated.file_type,
        )
        f.save(update_fields=["file_path", "file_name", "directory", "file_type", "updated_at"])

    return moves
    
def generate_css_variables(theme_data):
    tokens = theme_data.get("tokens", {})

    colors = tokens.get("colors", {})
    typography = tokens.get("typography", {})
    buttons = tokens.get("buttons", {})

    css_variables = []

    # Colors
    for name, value in colors.items():
        css_variables.append(f"--color-{name}: {value};")

    # Typography
    if typography.get("heading_font"):
        css_variables.append(
            f"--font-heading: {typography['heading_font']};"
        )

    if typography.get("body_font"):
        css_variables.append(
            f"--font-body: {typography['body_font']};"
        )

    # Buttons
    if buttons.get("radius"):
        css_variables.append(
            f"--button-radius: {buttons['radius']};"
        )

    return ":root {\n    " + "\n    ".join(css_variables) + "\n}"


# ------------------------------------------------- synced sections (code <-> customizer, see section_sync)

def _checked_sections(data):
    """{name: ORIGINALS_REVISION it was checked with}. Older themes stored a list (revision 1)."""
    checked = data.get("synced_sections")
    if isinstance(checked, dict):
        return checked
    if isinstance(checked, list):
        return {name: 1 for name in checked}
    return {"footer": 1} if data.get("synced_sections_added") else {}


def _add_synced_sections(theme, custom_data=None):
    """
    Themes made before a section became synced (sections/<name>.liquid) get its file -
    unless the store changed the snippet it replaces, which then keeps working as it is.
    A synced template (templates/index.liquid) that is still the starter's {{ sections }}
    file gets one {% section %} line per section of the store's page, in its order.
    Each is checked once per ORIGINALS_REVISION: when more starter versions are known to be
    untouched, themes checked before are checked again.
    """
    if not [n for n in sync.SYNCED_FILES if _checked_sections(theme.theme_data or {}).get(n, 0) < sync.ORIGINALS_REVISION]:
        return
    with transaction.atomic():
        # One request at a time: two at once would both add the file
        locked = Theme.objects.select_for_update().get(pk=theme.pk)
        data = locked.theme_data or {}
        checked = _checked_sections(data)
        todo = [n for n in sync.SYNCED_FILES if checked.get(n, 0) < sync.ORIGINALS_REVISION]
        if todo:
            paths = []
            for name in todo:
                config = sync.SYNCED_FILES[name]
                paths.append(config["path"])
                if config["kind"] == "section":
                    paths.append(f"snippets/{config['snippet']}.liquid")
                    if name == "header":
                        paths.append("snippets/styles.liquid")
            files = dict(locked.files.filter(file_path__in=paths).values_list("file_path", "content"))
            for name in todo:
                config = sync.SYNCED_FILES[name]
                if config["kind"] == "template":
                    _convert_template(locked, name, files.get(config["path"]), custom_data)
                    continue
                if config["path"] in files:
                    if name == "header":
                        file = locked.files.filter(file_path=config["path"]).order_by("-updated_at").first()
                        if file:
                            upgraded = _add_header_wishlist_setting(file.content)
                            if upgraded is not None:
                                sync.record_version(locked, name, file.content, "upgrade")
                                file.content = upgraded
                                file.file_size = len(upgraded.encode("utf-8"))
                                file.save(update_fields=["content", "file_size", "updated_at"])
                                sync.invalidate(locked.pk, config["path"])
                    if name == "footer":
                        file = locked.files.filter(file_path=config["path"]).order_by("-updated_at").first()
                        if file:
                            upgraded = _add_footer_logo_block(file.content)
                            if upgraded is not None:
                                sync.record_version(locked, name, file.content, "upgrade")
                                file.content = upgraded
                                file.file_size = len(upgraded.encode("utf-8"))
                                file.save(update_fields=["content", "file_size", "updated_at"])
                                sync.invalidate(locked.pk, config["path"])
                            upgraded_icon = _add_footer_icon_setting(file.content)
                            if upgraded_icon is not None:
                                sync.record_version(locked, name, file.content, "upgrade")
                                file.content = upgraded_icon
                                file.file_size = len(upgraded_icon.encode("utf-8"))
                                file.save(update_fields=["content", "file_size", "updated_at"])
                                sync.invalidate(locked.pk, config["path"])
                    if name == "header":
                        _upgrade_header_wishlist_styles(locked, files.get("snippets/styles.liquid"))
                    continue
                snippet = files.get(f"snippets/{config['snippet']}.liquid")
                if snippet is None or sync.is_original_snippet(name, snippet):
                    _new_file(locked, config["path"], starter_files()[config["path"]]).save()
                if name == "header":
                    _upgrade_header_wishlist_styles(locked, files.get("snippets/styles.liquid"))
            locked.theme_data = {
                **(locked.theme_data or data),
                "synced_sections": {**checked, **{n: sync.ORIGINALS_REVISION for n in todo}},
            }
            locked.save(update_fields=["theme_data", "updated_at"])
    theme.theme_data = locked.theme_data


def _upgrade_header_wishlist_styles(theme, content):
    if content is None:
        return
    upgraded = _add_header_wishlist_styles(content)
    if upgraded is None:
        return
    file = theme.files.filter(file_path="snippets/styles.liquid").order_by("-updated_at").first()
    if file is None:
        return
    file.content = upgraded
    file.file_size = len(upgraded.encode("utf-8"))
    file.save(update_fields=["content", "file_size", "updated_at"])


def _add_header_wishlist_styles(content):
    """Upgrade wishlist icon sizing or add responsive styles in a standard theme stylesheet."""
    old_icon_rule = (
        "  .site-header__wishlist svg { width: 22px; height: 22px; fill: none; stroke: currentColor; "
        "stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }"
    )
    icon_rule = (
        "  .site-header__wishlist svg { width: 20px; height: 20px; fill: none; stroke: currentColor; "
        "stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }"
    )
    if old_icon_rule in content:
        return content.replace(old_icon_rule, icon_rule, 1)
    if ".site-header__wishlist" in content:
        return None
    anchor = "  .site-header__nav a:hover { opacity: 1; }"
    if content.count(anchor) != 1:
        return None
    newline = "\r\n" if "\r\n" in content else "\n"
    styles = newline.join((
        "  .site-header__actions { display: flex; align-items: center; margin-left: auto; }",
        "  .site-header__wishlist {",
        "    display: inline-flex; align-items: center; justify-content: center; width: 40px; height: 40px;",
        "    padding: 0; border: 0; border-radius: 50%; background: transparent; color: inherit;",
        "    cursor: pointer; opacity: .8; transition: opacity .15s, background-color .15s;",
        "  }",
        "  .site-header__wishlist:hover { opacity: 1; background: var(--soft); }",
        "  .site-header__wishlist:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }",
        icon_rule,
        '  .site-header[data-layout="center"] .site-header__actions { margin-left: 0; }',
        "  @media (max-width: 640px) {",
        "    .site-header__inner { flex-wrap: wrap; }",
        "    .site-header__nav { order: 3; flex: 1 0 100%; justify-content: center; gap: 14px; }",
        "    .site-header__actions { order: 2; }",
        '    .site-header[data-layout="center"] .site-header__nav { order: 2; flex: initial; }',
        '    .site-header[data-layout="center"] .site-header__actions { order: 3; }',
        "  }",
    ))
    return content.replace(anchor, anchor + newline + styles, 1)


def _add_footer_logo_block(content):
    """Add (or bring up to date) the Logo block of a standard synced footer; None when nothing changes."""
    if "site-footer__brand" in content or ('"id": "position"' in content and '"type": "logo"' in content):
        return None
    nl = "\r\n" if "\r\n" in content else "\n"
    starter = starter_files()["sections/footer.liquid"].replace("\r\n", "\n")

    def part(text, start, end):
        return text[text.index(start):text.index(end)]

    try:
        markup = part(starter, "          {%- when 'logo' -%}", "          {%- when 'text' -%}").replace("\n", nl)
        block = part(starter, '    {\n      "type": "logo"', '    {\n      "type": "text"').replace("\n", nl)
        css = part(starter, "  .site-footer__logo {", "  .site-footer__nav strong").replace("\n", nl)
    except ValueError:
        return None  # the starter footer is built from other blocks now: a footer of the old kind keeps its own
    if '"type": "logo"' in content:  # an earlier version of the block: swap in the current one
        try:
            content = content.replace(part(content, "          {%- when 'logo' -%}", "          {%- when 'text' -%}"), markup, 1)
            content = content.replace(part(content, '    {' + nl + '      "type": "logo"', '    {' + nl + '      "type": "text"'), block, 1)
            old_css = part(content, "  .site-footer__logo {", "  .site-footer__nav strong")
        except ValueError:
            return None
        return content.replace(old_css, css, 1)
    markup_anchor = "        {%- case block.type -%}" + nl + "          {%- when 'text' -%}"
    schema_anchor = '  "blocks": [' + nl + "    {" + nl + '      "type": "text",'
    if content.count(markup_anchor) != 1 or content.count(schema_anchor) != 1:
        return None
    content = content.replace(markup_anchor, "        {%- case block.type -%}" + nl + markup + "          {%- when 'text' -%}", 1)
    content = content.replace(schema_anchor, '  "blocks": [' + nl + block + "    {" + nl + '      "type": "text",', 1)
    content = content.replace("<style>" + nl, "<style>" + nl + css, 1)
    return re.sub(r'"max_blocks": 8\b', '"max_blocks": 9', content, count=1)


def _add_footer_icon_setting(content):
    """Ensure footer image upload field is in Text block settings, not section settings, and rendered above description."""
    if "site-footer__brand" in content:
        return None  # the block footer has its images in its blocks already
    newline = "\r\n" if "\r\n" in content else "\n"
    patched = content

    # 1. Remove icon from section settings if present
    patched = re.sub(
        r'\{\s*"type":\s*"image",\s*"id":\s*"(?:icon|footer_icon)",\s*"label":\s*"[^"]*",\s*"default":\s*"[^"]*"\s*\},?\s*',
        '',
        patched
    )

    # 2. Add image field to text and links block settings if not already present
    if '"type": "text"' in patched and '"id": "image"' not in patched.split('"type": "text"')[-1].split('"type":')[0]:
        text_block_anchor = re.compile(r'(\{\s*"type":\s*"text",\s*"name":\s*"Text",\s*"settings":\s*\[\s*)')
        match = text_block_anchor.search(patched)
        if match:
            image_setting = '{"type": "image", "id": "image", "label": "Image", "default": ""},'
            patched = patched[:match.end()] + newline + "        " + image_setting + patched[match.end():]

    if '"type": "links"' in patched and '"id": "image"' not in patched.split('"type": "links"')[-1].split('"type":')[0]:
        links_block_anchor = re.compile(r'(\{\s*"type":\s*"links",\s*"name":\s*"Links",\s*"settings":\s*\[\s*)')
        match = links_block_anchor.search(patched)
        if match:
            image_setting = '{"type": "image", "id": "image", "label": "Image", "default": ""},'
            patched = patched[:match.end()] + newline + "        " + image_setting + patched[match.end():]

    # 3. Remove old links-wrapper and any icon check in links
    if "site-footer__links-wrapper" in patched:
        patched = re.sub(
            r'<div class="site-footer__links-wrapper">\s*(?:\{%- if icon_rendered == false .*?\{%- endif -%\}\s*)?(<nav class="site-footer__nav"[^>]*>.*?</nav>)\s*</div>',
            r'\1',
            patched,
            flags=re.DOTALL
        )

    # 4. Remove old icon markup right above copyright in legal
    patched = re.sub(
        r'<div class="page-width site-footer__legal">\s*(?:\{%- assign footer_icon = s\.icon -%\}\s*\{%- if footer_icon == blank or footer_icon == nil -%\}\s*\{%- assign footer_icon = s\.footer_icon -%\}\s*\{%- endif -%\}\s*)?\{%- if footer_icon != blank and footer_icon != nil -%\}\s*<div class="site-footer__icon">\s*<img src="\{\{ footer_icon \}\}"[^>]*>\s*</div>\s*\{%- endif -%}',
        '<div class="page-width site-footer__legal">',
        patched,
        flags=re.DOTALL
    )

    # 5. Remove old fallback icon markup at the end of section.blocks loop or before loop
    patched = re.sub(
        r'(\{%- endfor -%\})\s*\{%- if icon_rendered == false and footer_icon != blank and footer_icon != nil -%\}\s*\{%- assign icon_rendered = true -%\}\s*<div class="site-footer__icon">\s*<img src="\{\{ footer_icon \}\}"[^>]*>\s*</div>\s*\{%- endif -%}',
        r'\1',
        patched,
        flags=re.DOTALL
    )
    patched = re.sub(
        r'\{%- assign has_text_block = false -%\}\s*\{%- for block in section\.blocks -%\}\s*\{%- if block\.type == \'text\' -%\}\{%- assign has_text_block = true -%\}\{%- endif -%\}\s*\{%- endfor -%\}\s*\{%- if has_text_block == false and footer_icon != blank and footer_icon != nil -%\}\s*\{%- assign icon_rendered = true -%\}\s*<div class="site-footer__about">\s*<div class="site-footer__icon">\s*<img src="\{\{ footer_icon \}\}"[^>]*>\s*</div>\s*</div>\s*\{%- endif -%}',
        '',
        patched,
        flags=re.DOTALL
    )
    patched = re.sub(
        r'\{%- elsif footer_icon != blank and footer_icon != nil -%\}\s*<div class="page-width site-footer__top">\s*(?:<div class="site-footer__about">\s*)?<div class="site-footer__icon">\s*<img src="\{\{ footer_icon \}\}"[^>]*>\s*</div>\s*(?:</div>\s*)?</div>',
        '',
        patched,
        flags=re.DOTALL
    )

    # 6. Ensure icon is rendered inside when 'text' directly above heading and text
    text_block_pattern = r'(\{%- when \'text\' -%\}\s*<div class="site-footer__about"\{\{ block\.attrs \}\}>)(?:.*?)(?:\{%- if b\.heading != blank -%\})'
    replacement_markup = (
        r'\1' + newline
        + "              {%- assign block_image = b.image | default: b.icon | default: s.icon -%}" + newline
        + "              {%- if block_image != blank and block_image != nil -%}" + newline
        + "                <div class=\"site-footer__icon\">" + newline
        + "                  <img src=\"{{ block_image }}\" alt=\"{{ b.heading | default: shop.name | escape }}\" loading=\"lazy\">" + newline
        + "                </div>" + newline
        + "              {%- endif -%}" + newline
        + "              {%- if b.heading != blank -%}"
    )
    if "block_image" not in patched:
        patched = re.sub(text_block_pattern, replacement_markup, patched, count=1, flags=re.DOTALL)

    # 6b. Ensure icon is rendered inside when 'links' directly above heading and links
    links_block_pattern = r'(\{%- when \'links\' -%\}\s*<nav class="site-footer__nav" aria-label="\{\{ b\.heading \| default: \'Footer\' \}\}"\{\{ block\.attrs \}\}>)(?:.*?)(?:\{%- if b\.heading != blank -%\})'
    replacement_links_markup = (
        r'\1' + newline
        + "              {%- assign link_image = b.image | default: b.icon -%}" + newline
        + "              {%- if link_image != blank and link_image != nil -%}" + newline
        + "                <div class=\"site-footer__icon\">" + newline
        + "                  <img src=\"{{ link_image }}\" alt=\"{{ b.heading | default: shop.name | escape }}\" loading=\"lazy\">" + newline
        + "                </div>" + newline
        + "              {%- endif -%}" + newline
        + "              {%- if b.heading != blank -%}"
    )
    if "link_image" not in patched:
        patched = re.sub(links_block_pattern, replacement_links_markup, patched, count=1, flags=re.DOTALL)

    # 7. Styles
    patched = patched.replace("  .site-footer__links-wrapper { display: flex; align-items: flex-start; gap: 20px; }" + newline, "")
    patched = patched.replace("  .site-footer__links-wrapper { display: flex; align-items: flex-start; gap: 20px; }\n", "")
    if ".site-footer__about .site-footer__icon" not in patched:
        style_anchor = "</style>"
        if style_anchor in patched:
            new_styles = (
                "  .site-footer__about .site-footer__icon { margin-bottom: 16px; }" + newline
                + "  .site-footer__about .site-footer__icon + p { margin-top: 0; }" + newline
                + "  .site-footer__icon { flex-shrink: 0; }" + newline
                + "  .site-footer__icon img { max-height: 48px; max-width: 140px; width: auto; height: auto; object-fit: contain; display: block; }" + newline
            )
            patched = re.sub(r'  \.site-footer__icon \{[^\}]*\}\s*', '', patched)
            patched = re.sub(r'  \.site-footer__icon img \{[^\}]*\}\s*', '', patched)
            patched = patched.replace(style_anchor, new_styles + style_anchor, 1)

    if patched == content:
        return None

    try:
        sync.parse(patched, "sections/footer.liquid")
    except sync.SectionCodeError:
        return None
    return patched


def _upgrade_footer_icon(theme):
    file = theme.files.filter(file_path="sections/footer.liquid").first()
    if file:
        upgraded = _add_footer_icon_setting(file.content)
        if upgraded is not None:
            sync.record_version(theme, "footer", file.content, "upgrade")
            file.content = upgraded
            file.file_size = len(upgraded.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
            sync.invalidate(theme.pk, "sections/footer.liquid")


def _add_header_wishlist_setting(content):
    """Upgrade the wishlist icon and add its setting to standard synced headers."""
    if "{% render 'wishlist'" in content:
        return None  # already the newer header: its heart is the "Wishlist icon" block
    old_heart = (
        'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 '
        '7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z'
    )
    heart = (
        'M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 '
        '7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78Z'
    )
    has_old_heart = old_heart in content
    if has_old_heart:
        content = content.replace(old_heart, heart, 1)
    if "show_wishlist" in content:
        upgraded = re.sub(
            r'<button class="site-header__wishlist" type="button" aria-label="Wishlist" title="Wishlist">(.*?)</button>',
            r'<a class="site-header__wishlist" href="{{ shop.url }}/wishlist/" aria-label="Wishlist" title="Wishlist">\1</a>',
            content,
            count=1,
            flags=re.DOTALL,
        )
        if "data-wishlist-count" not in upgraded:
            upgraded = re.sub(
                r'(<a class="site-header__wishlist"[^>]*>.*?</svg>)(\s*)(</a>)',
                r'\1\2<span class="site-header__wishlist-count" data-wishlist-count aria-live="polite">{{ wishlist_count }}</span>\2\3',
                upgraded,
                count=1,
                flags=re.DOTALL,
            )
        return upgraded if upgraded != content else (content if has_old_heart else None)

    _, schema_text, schema_start = sync.split(content)
    if schema_text is None:
        return None
    newline = "\r\n" if "\r\n" in content else "\n"
    markup_anchor = f'    {{%- endif -%}}{newline}  </div>{newline}</header>'
    schema_anchor = re.compile(
        r'^([ \t]*\{"type": "checkbox", "id": "show_nav", "label": "Show the navigation menu", '
        r'"default": (?:true|false)\},)[ \t]*\r?$',
        re.MULTILINE,
    )
    markup_prefix = content[:schema_start]
    if markup_prefix.count(markup_anchor) != 1 or len(schema_anchor.findall(schema_text)) != 1:
        return None

    wishlist_markup = newline.join((
        '    {%- if s.show_wishlist -%}',
        '      <div class="site-header__actions">',
        '        <a class="site-header__wishlist" href="{{ shop.url }}/wishlist/" aria-label="Wishlist" title="Wishlist">',
        '          <svg viewBox="0 0 24 24" aria-hidden="true">',
        f'            <path d="{heart}" />',
        '          </svg>',
        '          <span class="site-header__wishlist-count" data-wishlist-count aria-live="polite">{{ wishlist_count }}</span>',
        '        </a>',
        '      </div>',
        '    {%- endif -%}',
    )) + newline
    upgraded_markup = markup_prefix.replace(markup_anchor, wishlist_markup + markup_anchor, 1)
    wishlist_setting = (
        '{"type": "checkbox", "id": "show_wishlist", "label": "Show Wishlist", "default": true},'
    )
    upgraded_schema = schema_anchor.sub(
        lambda match: match.group(1) + newline + "    " + wishlist_setting,
        schema_text,
        count=1,
    )
    return upgraded_markup + upgraded_schema + content[schema_start + len(schema_text):]


def _convert_template(theme, name, content, custom_data):
    """An untouched starter {{ sections }} template becomes one section line per section, in the store's order."""
    config = sync.SYNCED_FILES[name]
    if content is None or not sync.is_original_snippet(name, content):
        return
    entries = sync.template_entries(resolve(custom_data or {})["templates"][config["page"]])
    new = sync.template_from_entries(entries, "\r\n" if "\r\n" in content else "\n")
    sync.record_version(theme, name, content, "upgrade")
    theme.files.filter(file_path=config["path"]).update(
        content=new, file_size=len(new.encode("utf-8")), updated_at=timezone.now()
    )


def _seed_header(s, store_name):
    """The header's Theme settings (Header and navigation) as the header section's values."""
    blocks = []
    for line in str(s.get("header_menu") or "").splitlines():
        label, _, url = line.partition("|")
        label, url = label.strip(), url.strip() or label.strip()
        if label:
            blocks.append({"type": "link", "settings": {"label": label, "url": url}})
    # The header has always shown the wishlist heart: it is now a block the owner can remove
    blocks.append({"type": "wishlist", "settings": {"label": "Wishlist", "show_count": True}})
    # New headers start with the cart icon (its options are the block's defaults)
    blocks.append({"type": "cart", "settings": {}})
    # Keep the existing header blocks in their original order and append Search.
    blocks.append({"type": "search", "settings": {"label": "Search", "placeholder": "Search products"}})
    return {
        "settings": {
            "logo_text": s.get("header_logo_text", ""), "logo_image": s.get("header_logo_url", ""),
            "logo_height": s.get("header_logo_height", 36), "layout": s.get("header_layout", "left"),
            "show_nav": s.get("header_show_nav", True), "collections": s.get("header_collections", 4),
            "sticky": s.get("header_sticky", True), "border": s.get("header_border", True),
            "background": s.get("header_bg", ""), "text_color": s.get("header_text", ""),
            # The announcement bar is part of the header now
            "announcement_text": s.get("announcement", "") if s.get("announcement_show") is not False else "",
            "announcement_link": s.get("announcement_link", ""), "announcement_bg": s.get("announcement_bg", ""),
            "announcement_color": s.get("announcement_text", ""),
        },
        "blocks": blocks,
    }


def _seed_footer(s, store_name):
    """The footer's Theme settings as the footer section's values (None: none were set, the footer's own default content)."""
    custom = ("footer_text", "footer_menu", "footer_copyright", "footer_bg", "footer_text_color", *(f"footer_{k}" for k in FOOTER_SOCIAL_NETWORKS))
    if not any(s.get(key) for key in custom):
        return None
    blocks = []
    if s.get("footer_text"):
        blocks.append({"type": "text", "settings": {"heading": store_name, "text": s["footer_text"]}})
    if s.get("footer_show_links", True):
        blocks.append({"type": "links", "settings": {"heading": "", "menu": s.get("footer_menu") or "Home | /\nShop all | /collections/all"}})
    social = _footer_social_lines({k: s.get(f"footer_{k}", "") for k in FOOTER_SOCIAL_NETWORKS})
    if social:
        blocks.append({"type": "social", "settings": {"icons": social}})
    return {
        "settings": {"copyright": s.get("footer_copyright", ""), "background": s.get("footer_bg", ""),
                     "text_color": s.get("footer_text_color", "")},
        "blocks": blocks,
    }


# A store's first values for a synced section: what it had set before in Theme settings,
# so nothing is lost when a section becomes synced. One entry per section.
SEEDS = {"header": _seed_header, "footer": _seed_footer}


def _seed(name, theme_settings, store_name):
    seed = SEEDS.get(name)
    return seed(theme_settings, store_name) if seed else None


def _with_section(custom_data, name, values):
    return {**custom_data, "sections": {**(custom_data.get("sections") or {}), name: values}}


def synced_section(store_theme, name, files=None):
    """
    {path, content, schema, spec, values, version} of a synced section in this store's theme,
    or None (the theme has no such file, or its code is invalid: the old snippet is used).
    A store's first read fills in its values (from its earlier Theme settings) and saves them.
    """
    path = sync.SYNCED_SECTIONS[name]["path"]
    theme = store_theme.theme
    if files is not None:
        content = files.get(path)
    else:
        content = theme.files.filter(file_path=path).values_list("content", flat=True).first()
    if content is None:
        return None
    try:
        schema = sync.parse_cached(theme.pk, name, content)
    except sync.SectionCodeError:
        return None
    spec = sync.spec_of(name, schema)
    saved = (store_theme.custom_data.get("sections") or {}).get(name)
    if saved is None:
        seed = _seed(name, resolve(store_theme.custom_data)["settings"], store_theme.store.store_name)
        values = sync.clean_values(spec, seed) if seed else sync.defaults_of(spec, schema)
        store_theme.custom_data = _with_section(store_theme.custom_data, name, values)
        store_theme.save(update_fields=["custom_data", "updated_at"])
    else:
        values = sync.clean_values(spec, saved)
    return {"path": path, "content": content, "schema": schema, "spec": spec, "values": values,
            **sync.section_state(theme, name)}


def synced_sections(store_theme, files=None):
    found = {}
    for name in sync.SYNCED_SECTIONS:
        section = synced_section(store_theme, name, files)
        if section:
            found[name] = section
    return found


def _label(name):
    return sync.SYNCED_FILES[name]["label"].lower()


def _when(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else "earlier"


def _publish(store_theme, name, source, version, values, spec, code, client, started_at=None):
    sync.EVENTS.publish(store_theme.store_id, {
        "type": "section", "section": name, "source": source, "client": client or "",
        "version": version, "values": values, "spec": spec, "code": code, "at": time.time() * 1000,
        # When the change was made in the browser (ms), for the "change made -> preview updated" times
        "started_at": started_at,
    })


@transaction.atomic
def save_section_values(store_theme, name, raw, client="", base_version=None, started_at=None):
    """
    Blocks -> Code: the customizer saved the section's values. They are stored, and
    written into the file's schema (defaults and block order) - only those lines change.
    The newest change wins; if the code editor changed the file since, the reply says so.
    """
    current = synced_section(store_theme, name)
    if current is None:
        raise exceptions.ValidationError({"sections": f"This theme has no {sync.SYNCED_SECTIONS[name]['path']} to save into."})
    try:
        values = sync.clean_values(current["spec"], raw, strict=True)
    except ValueError as error:
        raise exceptions.ValidationError({"sections": str(error)})

    theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
    file = theme.files.filter(file_path=current["path"]).order_by("-updated_at").first()
    if file is None:
        raise exceptions.ValidationError({"sections": f"This theme has no {current['path']} file."})
    original_code = file.content
    state = sync.section_state(theme, name)
    conflict = None
    if base_version is not None and base_version != state["version"] and state["source"] in ("code", "restore"):
        conflict = (f"The code editor changed the {_label(name)} at {_when(state['updated_at'])}. Your change here was saved on top; "
                    "the code editor's version is kept in its History.")

    if name == "header":
        was_search = any(b.get("type") == "search" for b in current["values"]["blocks"])
        has_search = any(b.get("type") == "search" for b in values["blocks"])
        if was_search != has_search:
            patched = sync.add_search_block(file.content) if has_search else sync.remove_search_render_branch(file.content)
            if patched is not None:
                file.content = patched

    code = sync.write_values(file.content, values)
    version = state["version"]
    if code != original_code:
        version = sync.record_version(theme, name, original_code, "customizer")
        file.content = code
        file.file_size = len(code.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
        theme.save(update_fields=["theme_data", "updated_at"])
        sync.invalidate(theme.pk, current["path"])

    store_theme.custom_data = _with_section(store_theme.custom_data, name, values)
    store_theme.save(update_fields=["custom_data", "updated_at"])
    _publish(store_theme, name, "customizer", version, values, current["spec"], code, client, started_at)
    return {"version": version, "values": values, "conflict": conflict}


@transaction.atomic
def save_synced_code(store_theme, name, content, base_version=None, client="", source="code", started_at=None):
    if sync.SYNCED_FILES[name]["kind"] == "template":
        return save_template_code(store_theme, name, content, base_version, client, source, started_at)
    return _save_section_code(store_theme, name, content, base_version, client, source, started_at)


def _save_section_code(store_theme, name, content, base_version=None, client="", source="code", started_at=None):
    """
    Code -> Blocks: the section's file was saved in Edit code (or restored). Its code is
    checked first - invalid Liquid or schema JSON is refused with its line, and the saved
    version keeps working. Settings and blocks are rebuilt: new ones appear, removed ones
    go, ids that stay keep their values.
    """
    path = sync.SYNCED_SECTIONS[name]["path"]
    try:
        new_schema = sync.parse(content, path)
    except sync.SectionCodeError as error:
        raise exceptions.ValidationError({"content": str(error), "line": error.line})

    theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
    file = theme.files.filter(file_path=path).first()
    old_schema = None
    if file is not None:
        try:
            old_schema = sync.parse(file.content, path)
        except sync.SectionCodeError:
            old_schema = None

    # Removing Search's Liquid render branch must not erase its existing Add block option.
    if name == "header" and old_schema:
        content = sync.preserve_search_block_type(content, old_schema)
        new_schema = sync.parse(content, path)

    state = sync.section_state(theme, name)
    conflict = None
    if base_version is not None and base_version != state["version"] and state["source"] == "customizer":
        conflict = (f"The customizer changed the {_label(name)} at {_when(state['updated_at'])}. Your code was saved on top; "
                    "the customizer's version is kept in History.")

    saved = (store_theme.custom_data.get("sections") or {}).get(name)
    values = sync.merge_for_new_code(old_schema, new_schema, saved, name)
    version = sync.record_version(theme, name, file.content if file else None, source)
    if file is None:
        file = _new_file(theme, path, content)
        file.save()
    else:
        file.content = content
        file.file_size = len(content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
    theme.save(update_fields=["theme_data", "updated_at"])
    sync.invalidate(theme.pk, path)

    store_theme.custom_data = _with_section(store_theme.custom_data, name, values)
    store_theme.save(update_fields=["custom_data", "updated_at"])
    spec = sync.spec_of(name, new_schema)
    _publish(store_theme, name, source, version, values, spec, content, client, started_at)
    return SimpleNamespace(file_path=path, file_size=file.file_size,
                           sync={"section": name, "version": version, "conflict": conflict, "values": values, "spec": spec})


def restore_section(store_theme, name, index, client=""):
    """Put back a version from the section's history (it becomes the newest version)."""
    content = sync.history_content(store_theme.theme, name, index)
    if content is None:
        raise exceptions.NotFound("That version is no longer kept.")
    return save_synced_code(store_theme, name, content, client=client, source="restore")


# ------------------------------------------------- synced page templates (templates/index.liquid)

def synced_template(store_theme, name, files=None):
    """{path, content, version, ..., template} of a synced template, or None (no file, or a {{ sections }} one)."""
    config = sync.SYNCED_FILES[name]
    theme = store_theme.theme
    if files is not None:
        content = files.get(config["path"])
    else:
        content = theme.files.filter(file_path=config["path"]).values_list("content", flat=True).first()
    if content is None or sync.is_legacy_template(content):
        return None
    return {"path": config["path"], "content": content, **sync.section_state(theme, name),
            "template": resolve(store_theme.custom_data)["templates"][config["page"]]}


def synced_files(store_theme):
    """Everything synced with the customizer in this theme (for the customizer and the code editor)."""
    found = {}
    for name, section in synced_sections(store_theme).items():
        found[name] = {"kind": "section", "path": section["path"], "label": sync.SYNCED_FILES[name]["label"],
                       "spec": section["spec"], "values": section["values"], "version": section["version"]}
    for name in sync.SYNCED_TEMPLATES:
        template = synced_template(store_theme, name)
        if template:
            found[name] = {"kind": "template", "path": template["path"], "label": sync.SYNCED_FILES[name]["label"],
                           "page": sync.SYNCED_FILES[name]["page"], "template": template["template"],
                           "version": template["version"]}
    return found


def _publish_template(store_theme, name, source, version, template, code, client, started_at=None):
    sync.EVENTS.publish(store_theme.store_id, {
        "type": "section", "kind": "template", "section": name, "page": sync.SYNCED_FILES[name]["page"],
        "source": source, "client": client or "",
        "version": version, "template": template, "code": code, "at": time.time() * 1000, "started_at": started_at,
    })


def _remember_removed(theme, name, before, after):
    """Sections taken off the page are kept aside, so putting their line back restores their values."""
    gone = {sid: section for sid, section in before["sections"].items() if sid not in after["sections"]}
    if gone:
        removed = sync.removed_sections(theme, name)
        removed.update(gone)
        sync.set_removed_sections(theme, name, removed)


@transaction.atomic
def templates_changed(store_theme, before, client="", base_versions=None, started_at=None, source="customizer"):
    """
    Blocks -> Code: the page's sections were saved (customizer, or templates/<page>.json).
    If their order, list or hidden state changed, the template's section lines are rewritten -
    only those lines. The newest change wins; if Edit code changed the file since, the reply says so.
    Returns {name: {"version", "conflict"}} for the templates whose file changed.
    """
    results = {}
    base_versions = base_versions or {}
    for name, config in sync.SYNCED_TEMPLATES.items():
        page = config["page"]
        after = resolve(store_theme.custom_data)["templates"][page]
        if sync.template_entries(before[page]) == sync.template_entries(after):
            continue
        theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
        file = theme.files.filter(file_path=config["path"]).first()
        if file is None or sync.is_legacy_template(file.content):
            continue
        _remember_removed(theme, name, before[page], after)
        try:
            code = sync.write_template(file.content, sync.template_entries(after))
        except sync.SectionCodeError:
            continue  # the file's own lines can't be read: it keeps them until it is fixed in Edit code
        state = sync.section_state(theme, name)
        base = base_versions.get(name)
        conflict = None
        if base is not None and base != state["version"] and state["source"] in ("code", "restore"):
            conflict = (f"The code editor changed the {_label(name)} at {_when(state['updated_at'])}. Your change here was saved "
                        "on top; the code editor's version is kept in its History.")
        version = state["version"]
        if code != file.content:
            version = sync.record_version(theme, name, file.content, source)
            file.content = code
            file.file_size = len(code.encode("utf-8"))
            file.save(update_fields=["content", "file_size", "updated_at"])
        theme.save(update_fields=["theme_data", "updated_at"])
        _publish_template(store_theme, name, source, version, after, code, client, started_at)
        results[name] = {"version": version, "conflict": conflict}
    sync_card_cart_icon(store_theme, before)
    return results


@transaction.atomic
def save_template_code(store_theme, name, content, base_version=None, client="", source="code", started_at=None):
    """
    Code -> Blocks: templates/<page>.liquid was saved in Edit code (or restored). Its section
    lines become the page's sections, in that order: known ids keep their values, a new id
    with a type is a new section, and a section whose line is gone is kept aside (its line
    coming back restores it). Invalid lines or Liquid are refused with their line number.
    """
    config = sync.SYNCED_FILES[name]
    path, page = config["path"], config["page"]
    try:
        tags = sync.parse_template(content, path)
    except sync.SectionCodeError as error:
        raise exceptions.ValidationError({"content": str(error), "line": error.line})

    theme = Theme.objects.select_for_update().get(pk=store_theme.theme_id)
    store_theme.refresh_from_db(fields=["custom_data"])
    current = resolve(store_theme.custom_data)["templates"][page]
    removed = sync.removed_sections(theme, name)

    sections = {}
    for tag in tags:
        sid, kind = tag["id"], tag["type"]
        known = current["sections"].get(sid) or removed.get(sid)
        if known is not None:
            if kind and kind != known["type"]:
                raise exceptions.ValidationError({
                    "content": f"Line {tag['line']}: \"{sid}\" is a {SECTION_SCHEMAS.get(known['type'], {}).get('name', known['type'])} section. "
                               "To add a different one, give it a new id.", "line": tag["line"]})
            section = copy.deepcopy(known)
        elif not kind:
            raise exceptions.ValidationError({
                "content": f"Line {tag['line']}: there is no section \"{sid}\". To add one, give it a type, "
                           f"e.g. {{% section '{sid}', type: 'rich-text' %}}.", "line": tag["line"]})
        elif kind not in SECTION_SCHEMAS or page not in SECTION_SCHEMAS[kind]["pages"]:
            usable = ", ".join(t for t, sc in SECTION_SCHEMAS.items() if page in sc["pages"])
            raise exceptions.ValidationError({
                "content": f"Line {tag['line']}: \"{kind}\" can't be used on this page. Types: {usable}.", "line": tag["line"]})
        else:
            section = {"type": kind, "settings": {}, **({"blocks": []} if "blocks" in SECTION_SCHEMAS[kind] else {})}
        section["hidden"] = tag["hidden"]
        sections[sid] = section

    merged = resolve(store_theme.custom_data)
    merged.pop("sections", None)
    merged["templates"][page] = {"order": [t["id"] for t in tags], "sections": sections}
    try:
        clean = validate_custom_data(merged)
    except exceptions.ValidationError as error:
        raise exceptions.ValidationError({"content": f"{path}: " + " ".join(str(m) for m in _messages(error.detail))})
    if store_theme.custom_data.get("sections"):
        clean["sections"] = store_theme.custom_data["sections"]

    state = sync.section_state(theme, name)
    conflict = None
    if base_version is not None and base_version != state["version"] and state["source"] == "customizer":
        conflict = (f"The customizer changed the {_label(name)} at {_when(state['updated_at'])}. Your code was saved on top; "
                    "the customizer's version is kept in History.")

    after = resolve(clean)["templates"][page]
    file = theme.files.filter(file_path=path).first()
    version = sync.record_version(theme, name, file.content if file else None, source)
    _remember_removed(theme, name, current, after)
    # Sections that are back on the page no longer wait aside
    sync.set_removed_sections(theme, name, {k: v for k, v in sync.removed_sections(theme, name).items() if k not in sections})
    if file is None:
        file = _new_file(theme, path, content)
        file.save()
    else:
        file.content = content
        file.file_size = len(content.encode("utf-8"))
        file.save(update_fields=["content", "file_size", "updated_at"])
    theme.save(update_fields=["theme_data", "updated_at"])

    store_theme.custom_data = clean
    store_theme.save(update_fields=["custom_data", "updated_at"])
    _publish_template(store_theme, name, source, version, after, content, client, started_at)
    return SimpleNamespace(file_path=path, file_size=file.file_size,
                           sync={"section": name, "kind": "template", "version": version, "conflict": conflict, "template": after})


# ---------------------------------------------------------------- policies
#
# Settings > Policies: each written policy is the body of a store page at a fixed address, so it shows
# on /pages/<handle> (and in the sitemap and the link pickers) like any other page. Emptying a policy
# hides its page; the Pages admin can still edit them.

POLICY_PAGES = {
    "privacyPolicy": ("Privacy policy", "privacy-policy"),
    "refundPolicy": ("Return and refund policy", "refund-policy"),
    "termsOfService": ("Terms of service", "terms-of-service"),
    "shippingPolicy": ("Shipping policy", "shipping-policy"),
    "contactInformation": ("Contact information", "contact-information"),
    "legalNotice": ("Legal notice", "legal-notice"),
}
MAX_POLICY_LENGTH = 200000

# Other spellings of a policy's address that shoppers, old menus or old Pages use. The policy always owns
# them: a visible policy text is served from its one canonical address (/pages/<handle>), never from an
# old Page that happens to have a similar handle.
POLICY_ALIASES = {
    "privacyPolicy": ("privacy", "privacy-policies", "privacy-notice"),
    "refundPolicy": ("refund", "refunds", "refund-policies", "returns", "return-policy", "returns-policy",
                     "return-and-refund-policy", "returns-and-refunds"),
    "termsOfService": ("terms", "terms-of-services", "terms-and-conditions", "terms-conditions", "terms-of-use",
                       "tos"),
    "shippingPolicy": ("shipping", "shipping-policies", "delivery-policy", "shipping-and-delivery"),
    "contactInformation": ("contact-info", "contact-us", "contact", "contactus", "contact-details"),
    "legalNotice": ("legal",),
}
POLICY_ALIAS_TO_KEY = {alias: key for key, aliases in POLICY_ALIASES.items() for alias in aliases}


def policy_key_for_handle(handle):
    """The policy key a /pages/<handle> address belongs to (canonical handle or alias), else None."""
    handle = (handle or "").strip().lower()
    for key, (_, canonical) in POLICY_PAGES.items():
        if handle == canonical:
            return key
    return POLICY_ALIAS_TO_KEY.get(handle)


def canonical_policy_handle(handle):
    """The one stable handle for a policy address, or None when `handle` is not a policy address."""
    key = policy_key_for_handle(handle)
    return POLICY_PAGES[key][1] if key else None


def reserved_policy_handles():
    """Every handle a policy owns: canonical ones and their aliases."""
    return {h for _, h in POLICY_PAGES.values()} | set(POLICY_ALIAS_TO_KEY)


def policy_pages(store):
    """{key: StorePage or None} for the store's designated policy pages."""
    by_handle = {p.handle: p for p in StorePage.objects.filter(store=store, handle__in=[h for _, h in POLICY_PAGES.values()])}
    return {key: by_handle.get(handle) for key, (_, handle) in POLICY_PAGES.items()}


def read_policies(store, base_url=""):
    """Each policy as the admin edits it: {key: {title, handle, body, visible, url}}."""
    result = {}
    for key, page in policy_pages(store).items():
        title, handle = POLICY_PAGES[key]
        result[key] = {
            "title": page.title if page else title,
            "handle": handle,
            "body": page.body if page else "",
            "visible": bool(page and page.status == StorePage.VISIBLE),
            "url": f"{base_url}/pages/{handle}",
        }
    return result


def save_policies(store, data):
    """
    Write the policies in `data` ({key: body}); keys that are left out stay as they are. A policy with
    text goes live on its page; a blank one hides the page.
    """
    unknown = sorted(set(data) - set(POLICY_PAGES))
    if unknown:
        raise exceptions.ValidationError({unknown[0]: "Unknown policy."})
    for key, body in data.items():
        if not isinstance(body, str):
            raise exceptions.ValidationError({key: "Must be text."})
        if len(body) > MAX_POLICY_LENGTH:
            raise exceptions.ValidationError({key: f"Can be at most {MAX_POLICY_LENGTH} characters."})

    with transaction.atomic():
        existing = policy_pages(store)
        # Policy pages use the starter's policy layout when the theme has it
        template = "page.policy" if data and "page.policy" in page_template_keys(get_store_theme(store).custom_data) else ""
        for key, body in data.items():
            title, handle = POLICY_PAGES[key]
            page = existing[key]
            # Rich text editors leave an empty paragraph behind when cleared
            has_text = bool(re.sub(r"<[^>]*>|&nbsp;|\s", "", body))
            body = body if has_text else ""
            if page is None:
                if not has_text:
                    continue
                StorePage.objects.create(
                    store=store, title=title, handle=handle, body=body, template=template,
                    status=StorePage.VISIBLE, published_at=timezone.now(),
                )
                continue
            page.body = body
            page.status = StorePage.VISIBLE if has_text else StorePage.HIDDEN
            if has_text and not page.published_at:
                page.published_at = timezone.now()
            page.save(update_fields=["body", "status", "published_at", "updated_at"])
