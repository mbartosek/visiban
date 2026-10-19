"""OpenAPI contract for POST /auth/password/change/ (#1781).

drf-spectacular builds the serializer with the view's AnonymousUser, which has no
``has_usable_password()``. The serializer used to crash there, so the generated
schema silently lost the request body and with it the required ``old_password``
(#1257). Left alone, the bodyless schema would have become the v1.2.0 compat
baseline.
"""

from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory, TestCase
from drf_spectacular.generators import SchemaGenerator

from accounts.models import User
from accounts.serializers import VisibanPasswordChangeSerializer

_PATH = "/api/v1/auth/password/change/"


class PasswordChangeSchemaTests(TestCase):
    def test_serializer_instantiates_for_anonymous_user(self):
        request = RequestFactory().post(_PATH)
        request.user = AnonymousUser()
        serializer = VisibanPasswordChangeSerializer(context={"request": request})
        self.assertTrue(serializer.fields["old_password"].required)

    def test_schema_documents_required_old_password(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        body = schema["paths"][_PATH]["post"]["requestBody"]["content"]["application/json"]["schema"]
        ref = body["$ref"].rsplit("/", 1)[-1]
        component = schema["components"]["schemas"][ref]
        self.assertIn("old_password", component["required"])
        self.assertIn("new_password1", component["required"])

    def test_password_less_account_still_may_omit_old_password(self):
        user = User.objects.create_user(username="sso1781", email="sso1781@example.com")
        user.set_unusable_password()
        user.save()
        request = RequestFactory().post(_PATH)
        request.user = user
        serializer = VisibanPasswordChangeSerializer(context={"request": request})
        self.assertFalse(serializer.fields["old_password"].required)
