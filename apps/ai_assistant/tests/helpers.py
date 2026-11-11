import json
from types import SimpleNamespace

from apps.catalog.models import Collection, Product
from apps.core.tests.test_flow import StoreClientMixin
from apps.platform_admin.models import FeatureFlag, FeatureFlagOverride
from apps.tenancy.models import Store


def enable_flag(store_id):
    flag, _ = FeatureFlag.objects.get_or_create(key="ai_page_builder", defaults={"name": "AI page builder", "enabled": False})
    FeatureFlagOverride.objects.update_or_create(flag=flag, store_id=store_id, defaults={"enabled": True})


def call(name, arguments, call_id="call_1"):
    """A tool call as the openai package returns it."""
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=args))


def reply(content=None, calls=()):
    """A model reply message."""
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


class TwoStores(StoreClientMixin):
    """Two merchants, each with a store; self.a / self.b hold (token, store json)."""

    def make_stores(self):
        self.a = self.sign_up("a@example.com", "Store A")
        self.b = self.sign_up("b@example.com", "Store B")
        self.store_a = Store.objects.get(pk=self.a[1]["storeId"])
        self.store_b = Store.objects.get(pk=self.b[1]["storeId"])

    def as_(self, who):
        self.use(who[0], who[1]["storeId"])

    def ctx(self, store):
        from apps.ai_assistant.tools import Context
        from apps.identity.models import User

        owner = User.objects.get(email="a@example.com" if store.pk == self.store_a.pk else "b@example.com")
        return Context(store=store, user=owner)


def make_product(store, title="Linen Shirt"):
    return Product.objects.create(store=store, title=title, status="active")


def make_collection(store, name="Summer"):
    return Collection.objects.create(store=store, name=name)
