"""
Dry run of "Delete selected": shows which products would be deleted and which kept, then rolls everything back.

    .\venv\Scripts\python.exe manage.py shell -c "STORE='<store id>'; exec(open('dry_run_bulk_delete.py', encoding='utf-8').read())"

Optional: TITLES=['Black Pepper', ...] limits it to those product titles (default: every product in the store).
Nothing is ever committed: the whole run is inside a transaction that is rolled back at the end.
"""
from django.db import transaction

from apps.catalog.models import Product
from apps.catalog.services import delete_products
from apps.tenancy.models import Store

store = Store.objects.get(pk=STORE)
products = Product.objects.filter(store=store)
if globals().get("TITLES"):
    products = products.filter(title__in=TITLES)
ids = list(products.values_list("pk", flat=True))
print(f"Store {store.pk}: {len(ids)} product(s) selected")


class Rollback(Exception):
    pass


stats = {}
try:
    with transaction.atomic():
        deleted, blocked = delete_products(store, ids, confirm_stock=True, stats=stats)
        print(f"WOULD DELETE: {deleted}")
        print(f"STOCK ROWS THAT WOULD GO: {stats['stock_rows']}")
        print(f"WOULD KEEP (orders/carts/wishlists/discounts): {len(stats['blocked_in_use'])} {stats['blocked_in_use'][:10]}")
        print(f"WOULD KEEP (other records): {len(stats['blocked_other'])} {stats['blocked_other'][:10]}")
        raise Rollback()
except Rollback:
    print("Rolled back: nothing was changed.")
print("Products still in the store:", Product.objects.filter(store=store).count())
