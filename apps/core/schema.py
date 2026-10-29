import inspect

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.drainage import has_override
from rest_framework import serializers


class JWTAuthenticationScheme(OpenApiAuthenticationExtension):
    """Documents apps.core.authentication.JWTAuthentication as a Bearer token."""

    target_class = "apps.core.authentication.JWTAuthentication"
    name = "jwtAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


from drf_spectacular.openapi import AutoSchema  # noqa: E402
from drf_spectacular.types import OpenApiTypes  # noqa: E402
from rest_framework.generics import GenericAPIView  # noqa: E402
from rest_framework.viewsets import ViewSetMixin  # noqa: E402


class TSSAutoSchema(AutoSchema):
    """
    Many views here are plain APIViews that validate input by hand, so there
    is no serializer to introspect. Instead of dropping them from the docs
    (or logging an error each), document them as free-form JSON objects, use
    the view docstring as the description, and give list endpoints a
    distinct operationId so they don't collide with the detail endpoint.
    """

    def _is_plain_apiview(self):
        view = self.view
        return not isinstance(view, (GenericAPIView, ViewSetMixin)) and not hasattr(view, "serializer_class")

    def _get_serializer(self):
        if self._is_plain_apiview():
            return None
        return super()._get_serializer()

    def get_request_serializer(self):
        if self._is_plain_apiview() and self.method in ("POST", "PUT", "PATCH"):
            return OpenApiTypes.OBJECT
        return super().get_request_serializer()

    def get_response_serializers(self):
        if self._is_plain_apiview():
            if self.method == "DELETE":
                return {204: None}
            return OpenApiTypes.OBJECT
        return super().get_response_serializers()

    def get_operation_id(self):
        op_id = super().get_operation_id()
        if self._is_plain_apiview() and self.method == "GET" and not self.path.rstrip("/").endswith("}") and op_id.endswith("_retrieve"):
            op_id = op_id[: -len("_retrieve")] + "_list"
        return op_id

    def _map_serializer_field(self, field, direction, bypass_extensions=False):
        # A SerializerMethodField without a return type hint can return anything
        # (dicts, lists, numbers): document it as free-form rather than "string".
        if isinstance(field, serializers.SerializerMethodField):
            method = getattr(field.parent, field.method_name, None)
            if (
                method is not None
                and not has_override(field, "field")
                and not hasattr(method, "_spectacular_annotation")
                and inspect.signature(method).return_annotation is inspect.Signature.empty
            ):
                return {**self._get_serializer_field_meta(field, direction)}
        return super()._map_serializer_field(field, direction, bypass_extensions)
