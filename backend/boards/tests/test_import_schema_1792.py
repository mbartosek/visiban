"""OpenAPI contract for the board import request bodies (#1792).

Both import views answer 400 "No file provided." without an upload, but
drf-spectacular never marks a dict request body required, so the schema said
the body was optional. Correcting that after the v1.2.0 baseline freezes would
fire REQUEST_BODY_BECAME_REQUIRED. Schema only: no test expects a runtime change.
"""

from django.test import TestCase
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from accounts.models import User

_IMPORT = "/api/v1/boards/import/"
_TRELLO = "/api/v1/boards/import/trello/"


class ImportSchemaTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)

    def _body(self, path):
        return self.schema["paths"][path]["post"]["requestBody"]

    def test_request_body_is_required(self):
        for path in (_IMPORT, _TRELLO):
            with self.subTest(path=path):
                self.assertIs(self._body(path).get("required"), True)

    def test_file_is_a_required_binary_property(self):
        for path in (_IMPORT, _TRELLO):
            with self.subTest(path=path):
                schema = self._body(path)["content"]["multipart/form-data"]["schema"]
                self.assertEqual(schema["required"], ["file"])
                self.assertEqual(schema["properties"]["file"]["format"], "binary")

    def test_views_still_answer_400_without_a_file(self):
        # The documented requirement matches what the views enforce.
        user = User.objects.create_user(username="importer1792", password="pass-1792-Old!")
        client = APIClient()
        client.force_authenticate(user)
        resp = client.post(_IMPORT, {}, format="multipart")
        self.assertEqual(resp.status_code, 400)
        resp = client.post(_TRELLO + "?dry_run=true", {}, format="multipart")
        self.assertEqual(resp.status_code, 400)
