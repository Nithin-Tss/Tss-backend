from django.contrib import admin
from .models import Category, Product, ProductVariant, ProductImage, Collection, ProductCollection, Tag, ProductTag, SalesChannel, ProductSalesChannel, ProductReview

# Registering models
admin.site.register(Category)
admin.site.register(Product)
admin.site.register(ProductVariant)
admin.site.register(ProductImage)
admin.site.register(Collection) 
admin.site.register(ProductCollection) 
admin.site.register(Tag) 
admin.site.register(ProductTag) 
admin.site.register(SalesChannel)
admin.site.register(ProductSalesChannel) 
admin.site.register(ProductReview)