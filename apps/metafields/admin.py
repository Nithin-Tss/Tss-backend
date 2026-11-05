from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin

from .models import Metafield, MetafieldDefinition, Metaobject, MetaobjectDefinition


@admin.register(MetafieldDefinition)
class MetafieldDefinitionAdmin(StoreGuardAdmin):
    pass


@admin.register(Metafield)
class MetafieldAdmin(StoreGuardAdmin):
    store_links = {"definition": "store"}


@admin.register(MetaobjectDefinition)
class MetaobjectDefinitionAdmin(StoreGuardAdmin):
    pass


@admin.register(Metaobject)
class MetaobjectAdmin(StoreGuardAdmin):
    store_links = {"definition": "store"}
