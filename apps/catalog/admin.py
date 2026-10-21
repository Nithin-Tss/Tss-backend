from django.contrib import admin

from apps.core.admin_guards import StoreGuardAdmin
from .models import Category, Product, ProductVariant, ProductImage, Collection, ProductCollection, Tag, ProductTag, SalesChannel, ProductSalesChannel, CollectionSalesChannel, ProductReview, Brand


@admin.register(Category)
class CategoryAdmin(StoreGuardAdmin):
    list_display = ("name", "store_name", "parent")
    search_fields = ("name",)
    store_links = {"parent": "store"}


@admin.register(Brand)
class BrandAdmin(StoreGuardAdmin):
    list_display = ("name", "store_name")
    search_fields = ("name",)


@admin.register(Product)
class ProductAdmin(StoreGuardAdmin):
    list_display = ("title", "store_name", "status", "category", "brand")
    search_fields = ("title",)
    store_links = {"category": "store", "brand": "store"}


@admin.register(Collection)
class CollectionAdmin(StoreGuardAdmin):
    list_display = ("name", "store_name", "handle", "type", "status")
    list_filter = ("type", "status")
    search_fields = ("name", "handle", "store__store_name")


@admin.register(Tag)
class TagAdmin(StoreGuardAdmin):
    list_display = ("name", "store_name")
    search_fields = ("name",)


@admin.register(SalesChannel)
class SalesChannelAdmin(StoreGuardAdmin):
    list_display = ("name", "store_name", "type")
    search_fields = ("name",)


@admin.register(ProductReview)
class ProductReviewAdmin(StoreGuardAdmin):
    list_display = ("__str__", "store_name", "status")
    store_links = {"product": "store", "customer": "store", "order_item": "order.store"}


@admin.register(ProductVariant)
class ProductVariantAdmin(StoreGuardAdmin):
    store_path = "product__store"
    parent_fields = ("product",)
    store_links = {"product": "store"}


@admin.register(ProductImage)
class ProductImageAdmin(StoreGuardAdmin):
    store_path = "product__store"
    parent_fields = ("product",)
    store_links = {"product": "store"}


@admin.register(ProductCollection)
class ProductCollectionAdmin(StoreGuardAdmin):
    store_path = "product__store"
    store_links = {"product": "store", "collection": "store"}


@admin.register(ProductTag)
class ProductTagAdmin(StoreGuardAdmin):
    store_path = "product__store"
    store_links = {"product": "store", "tag": "store"}


@admin.register(ProductSalesChannel)
class ProductSalesChannelAdmin(StoreGuardAdmin):
    store_path = "product__store"
    store_links = {"product": "store", "sales_channel": "store"}


@admin.register(CollectionSalesChannel)
class CollectionSalesChannelAdmin(StoreGuardAdmin):
    store_path = "collection__store"
    store_links = {"collection": "store", "sales_channel": "store"}
