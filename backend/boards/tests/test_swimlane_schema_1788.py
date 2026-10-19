"""OpenAPI contract for the swimlane write operations (#1788).

SwimlaneViewSet resolved ``self.kwargs["board_pk"]`` in get_serializer_class(),
which drf-spectacular calls without a request or URL kwargs. The KeyError made
it drop the view's serializer, so the generated schema silently lost every
swimlane request body. Left alone, the bodyless schema would have become the
v1.2.0 compat baseline (same hazard as #1781).
"""

import logging

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator

_LIST = "/api/v1/boards/{board_pk}/swimlanes/"
_DETAIL = "/api/v1/boards/{board_pk}/swimlanes/{id}/"
_REORDER = "/api/v1/boards/{board_pk}/swimlanes/reorder/"


class SwimlaneSchemaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def _body_props(self, path, method):
        op = self.schema["paths"][path][method]
        content = op["requestBody"]["content"]["application/json"]["schema"]
        ref = content["$ref"].rsplit("/", 1)[-1]
        return self.schema["components"]["schemas"][ref]

    def test_schema_generation_logs_no_swimlane_serializer_error(self):
        with self.assertLogs("drf_spectacular", level=logging.WARNING) as cm:
            logging.getLogger("drf_spectacular").warning("sentinel")
            SchemaGenerator().get_schema(request=None, public=True)
        bad = [m for m in cm.output if "SwimlaneViewSet" in m and "board_pk" in m]
        self.assertEqual(bad, [])

    def test_create_documents_request_body(self):
        self.assertIn("name", self._body_props(_LIST, "post")["required"])

    def test_update_documents_request_body(self):
        self.assertIn("name", self._body_props(_DETAIL, "put")["required"])

    def test_partial_update_documents_request_body(self):
        props = self._body_props(_DETAIL, "patch")["properties"]
        self.assertIn("name", props)

    def test_reorder_documents_request_body(self):
        op = self.schema["paths"][_REORDER]["post"]
        self.assertIn("requestBody", op)
