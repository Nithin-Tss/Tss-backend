from rest_framework.routers import SimpleRouter

from .views import (
    MetafieldDefinitionViewSet, MetafieldViewSet, MetaobjectDefinitionViewSet, MetaobjectViewSet,
)

router = SimpleRouter()
router.register("definitions", MetafieldDefinitionViewSet, basename="metafield-definition")
router.register("values", MetafieldViewSet, basename="metafield")
router.register("metaobject-definitions", MetaobjectDefinitionViewSet, basename="metaobject-definition")
router.register("metaobjects", MetaobjectViewSet, basename="metaobject")

urlpatterns = router.urls
