"""OpenAPI contract for the swimlane write operations (#1788).

SwimlaneViewSet resolved ``self.kwargs["board_pk"]`` in get_serializer_class(),
which drf-spectacular calls without a request or URL kwargs. The KeyError made
it drop the view's serializer, so the generated schema silently lost every
swimlane request body (and its read responses). Left alone, the bodyless schema
would have become the v1.2.0 compat baseline (same hazard as #1781).
"""

import contextlib
import io

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator

_LIST = "/api/v1/boards/{board_pk}/swimlanes/"
_DETAIL = "/api/v1/boards/{board_pk}/swimlanes/{id}/"
_REORDER = "/api/v1/boards/{board_pk}/swimlanes/reorder/"
_COLLAPSE = (
    "/api/v1/boards/{board_pk}/swimlanes/{id}/set-collapsed/",
    "/api/v1/boards/{board_pk}/swimlanes/{id}/set_collapsed/",
)


class SwimlaneSchemaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # drf-spectacular reports serializer errors on stderr, not via logging.
        cls.stderr = io.StringIO()
        with contextlib.redirect_stderr(cls.stderr):
            cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def _component(self, schema):
        return self.schema["components"]["schemas"][schema["$ref"].rsplit("/", 1)[-1]]

    def _body(self, path, method):
        op = self.schema["paths"][path][method]
        return self._component(op["requestBody"]["content"]["application/json"]["schema"])

    def test_schema_generation_reports_no_swimlane_serializer_error(self):
        bad = [
            line for line in self.stderr.getvalue().splitlines()
            if "exception raised while getting serializer" in line
        ]
        self.assertEqual(bad, [])

    def test_read_operations_document_the_serializer(self):
        # On the broken schema these responses were `{}`.
        detail = self.schema["paths"][_DETAIL]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(self._component(detail)["type"], "object")
        self.assertIn("contact_email", self._component(detail)["properties"])
        listing = self.schema["paths"][_LIST]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertIn("results", self._component(listing)["properties"])

    def test_create_documents_request_body(self):
        self.assertIn("name", self._body(_LIST, "post")["required"])

    def test_update_documents_request_body(self):
        self.assertIn("name", self._body(_DETAIL, "put")["required"])

    def test_partial_update_documents_request_body(self):
        self.assertIn("name", self._body(_DETAIL, "patch")["properties"])

    def test_reorder_documents_order_list(self):
        # reorder declares its own request serializer, so this pins the contract
        # rather than the guard.
        props = self._body(_REORDER, "post")["properties"]
        self.assertEqual(props["order"]["type"], "array")

    def test_set_collapsed_documents_required_is_collapsed(self):
        # The view answers 400 without is_collapsed, so the body is required.
        for path in _COLLAPSE:
            op = self.schema["paths"][path]["patch"]
            self.assertTrue(op["requestBody"].get("required"), path)
            body = op["requestBody"]["content"]["application/json"]["schema"]
            self.assertEqual(list(body["properties"]), ["is_collapsed"], path)
            self.assertEqual(body["required"], ["is_collapsed"], path)
