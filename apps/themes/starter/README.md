# Starter theme structure

Every store gets its own copy of these files. Rendering order:

    layout/theme.liquid              header, {{ content_for_layout }}, footer
      └ templates/<page>.liquid      the page's default template
          └ sections/<type>.liquid   the page's sections, in the order set in the customizer
              └ snippets/*.liquid    shared blocks, called with {% render 'name' %}

## Pages and their default templates

| Page | Template | Sections / blocks it uses |
|---|---|---|
| Home | `templates/index.liquid` | any sections (hero, slideshow, product-grid, product-carousel, rich-text, ...) |
| Product | `templates/product.liquid` | `main-product`, `product-carousel` |
| Collection | `templates/collection.liquid` | `main-collection` → `collection-header`, `collection-search`, `collection-toolbar` (→ `collection-filters`, `collection-sort`), `collection-pagination` |
| Blog | `templates/blog.liquid` | `main-blog` → `blog-filters`, `article-card` |
| Article | `templates/article.liquid` | `main-article` |
| Page | `templates/page.liquid` (+ `page.about`, `page.contact`, `page.faq`, `page.policy`) | `main-page`, `rich-text` |
| Checkout | `templates/checkout.liquid` | `main-checkout` |
| Login / Register | `templates/login.liquid`, `templates/register.liquid` | `auth-styles`, `auth-header`, `auth-login-form` / `auth-register-form`, `auth-links`, `auth-script` |
| Account | `templates/account.liquid` | `account-styles`, `account-header`, `account-profile`, `account-addresses`, `account-orders`, `account-script` |
| Cart, Wishlist | no template (by design) | the page is `snippets/cart.liquid` / `snippets/wishlist.liquid` in `mode: 'page'` |

## Shared blocks (used on more than one page)

| Snippet | What it is | Called from |
|---|---|---|
| `product-card` | one product card | `product-grid`, `product-carousel`, `main-collection`, `wishlist`, `product-grid-items` |
| `product-grid-items` | the cart/wishlist scripts and the product-card loop | meant for the three product-list sections above (see note) |
| `collection-filters`, `collection-sort`, `collection-toolbar` | filter panel, sort menu, toolbar | `main-collection` |
| `blog-filters` | category links | `main-blog` |
| `cart`, `wishlist` | cart icon/drawer/page, wishlist heart/page | header, product cards, product page |
| `header`, `footer`, `styles`, `theme-css` | layout parts and CSS | `layout/theme.liquid` |

## Rules for changing these files

- A store keeps its own copy. `STARTER_UPGRADES` in `services.py` replaces a copy only when its hash is a registered
  earlier starter version; edited copies are never touched.
- Changing an existing starter file therefore means registering the old file's SHA-256 in `STARTER_UPGRADES` and
  bumping `STARTER_REVISION` (and the tests that pin it).
- A new snippet must exist in a store's copy before a file that renders it is upgraded: add it to `REQUIRED_FILES`.
- Adding a new snippet file on its own is safe: new stores get it, existing stores are unaffected.

Note: `main-collection`, `product-grid` and `product-carousel` still contain their own copy of the product loop.
Switching them to `product-grid-items` is a behaviour-neutral refactor that needs the steps above.
