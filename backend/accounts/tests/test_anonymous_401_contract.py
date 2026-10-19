"""Pin the documented anonymous-request contract (#1771, docs: #1745).

``docs/api/authentication.md`` promises that an unauthenticated request gets
``401`` with ``WWW-Authenticate: Token``. DRF only emits 401 (rather than 403)
when the *first* authentication class returns an ``authenticate_header``;
``PATAuthentication`` is first in ``DEFAULT_AUTHENTICATION_CLASSES`` for exactly
that reason. Looser ``in (401, 403)`` assertions elsewhere would let a
regression to 403 pass, so this test locks the contract on a representative
set of routes.
"""
from django.conf import settings
from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient

ANONYMOUS_ROUTES = (
    "/api/v1/version/",
    "/api/v1/boards/",
    "/api/v1/notifications/",
    "/api/v1/auth/user/",
)


class AnonymousUnauthorizedContractTests(TestCase):
    def test_anonymous_gets_401_with_www_authenticate_token(self):
        client = APIClient()
        for url in ANONYMOUS_ROUTES:
            with self.subTest(url=url):
                r = client.get(url)
                self.assertEqual(r.status_code, status.HTTP_401_UNAUTHORIZED)
                self.assertEqual(r["WWW-Authenticate"], "Token")


class AuthenticationClassOrderTests(SimpleTestCase):
    def test_pat_authentication_is_first(self):
        """The mechanism behind the 401: the first class supplies the header."""
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"][0],
            "accounts.authentication.PATAuthentication",
        )
