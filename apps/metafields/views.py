from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.viewsets import StoreScopedViewSet

from .models import Metafield, MetafieldDefinition, Metaobject, MetaobjectDefinition
from .serializers import (
    MetafieldDefinitionSerializer, MetafieldSerializer, MetaobjectDefinitionSerializer, MetaobjectSerializer,
)


class MetafieldDefinitionViewSet(StoreScopedViewSet):
    """/api/v1/metafields/definitions/  ?owner_type=product|variant|collection|brand"""

    permission_resource = "products"
    queryset = MetafieldDefinition.objects.order_by("owner_type", "namespace", "key")
    serializer_class = MetafieldDefinitionSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.request.query_params.get("owner_type"):
            qs = qs.filter(owner_type=self.request.query_params["owner_type"])
        return qs


class MetafieldViewSet(StoreScopedViewSet):
    """/api/v1/metafields/values/  ?owner_type=&owner_id=&definition=

    POST sets a value: if the owner already has one for that definition it is replaced.
    """

    permission_resource = "products"
    queryset = Metafield.objects.select_related("definition").order_by("definition__namespace", "definition__key")
    serializer_class = MetafieldSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for param, lookup in (("owner_type", "definition__owner_type"), ("owner_id", "owner_id"), ("definition", "definition")):
            if params.get(param):
                qs = qs.filter(**{lookup: params[param]})
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        existing = Metafield.objects.filter(
            store=request.store, definition=data["definition"], owner_id=data["owner_id"],
        ).first()
        if existing:
            existing.value = data.get("value")
            existing.save(update_fields=["value", "updated_at"])
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)
        self.perform_create(serializer)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class MetaobjectDefinitionViewSet(StoreScopedViewSet):
    """/api/v1/metafields/metaobject-definitions/"""

    permission_resource = "products"
    queryset = MetaobjectDefinition.objects.order_by("name")
    serializer_class = MetaobjectDefinitionSerializer


class MetaobjectViewSet(StoreScopedViewSet):
    """/api/v1/metafields/metaobjects/  ?definition=<id>  ?type=<type>  ?status="""

    permission_resource = "products"
    queryset = Metaobject.objects.select_related("definition").order_by("definition__type", "handle")
    serializer_class = MetaobjectSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        for param, lookup in (("definition", "definition"), ("type", "definition__type"), ("status", "status")):
            if params.get(param):
                qs = qs.filter(**{lookup: params[param]})
        return qs
