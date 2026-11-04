"""
JSON parser that only accepts an object at the top level.

Every endpoint reads its input as named fields (request.data.get(...)); a body like [] or "x" would
otherwise reach the view and crash it with a 500.
"""
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


class ObjectJSONParser(JSONParser):
    def parse(self, stream, media_type=None, parser_context=None):
        data = super().parse(stream, media_type, parser_context)
        if not isinstance(data, dict):
            raise ParseError("Send a JSON object.")
        return data
