"""``request_is_interactive_session`` is a positive check on the authenticator (#1839).

The session-only account rules (first password, email change, username change,
provider disconnect) all go through this one helper. It requires the
authenticator that accepted the request to be a ``SessionAuthentication``; an
authenticator that returns ``(user, None)`` is not a session.
"""

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from rest_framework.authentication import BaseAuthentication, SessionAuthentication, TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from accounts import credentials
from accounts.views import ConnectedAccountDetailView

User = get_user_model()


class _NoCredentialAuthentication(BaseAuthentication):
    """Stand-in for an extension authenticator that returns ``(user, None)``."""

    def __init__(self, user=None):
        self._user = user

    def authenticate(self, request):
        return (self._user, None)


class _ExemptSessionAuthentication(SessionAuthentication):
    """A CSRF-exempt variant: still a session, so still interactive."""

    def enforce_csrf(self, request):
        return


class _SessionWithCredentialAuthentication(SessionAuthentication):
    """A session subclass that also reports a credential object."""

    def authenticate(self, request):
        result = super().authenticate(request)
        return None if result is None else (result[0], object())


def _drf_request(user, authenticator, *, session_login=False):
    raw = RequestFactory().patch("/api/v1/auth/me/")
    if session_login:
        # SessionAuthentication reads the user the auth middleware attached.
        raw.user = user
        raw._dont_enforce_csrf_checks = True
    return Request(raw, authenticators=[authenticator])


class InteractiveSessionHelperTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="sess", email="sess@example.com", password="pass12345678"
        )

    def test_session_authenticator_is_interactive(self):
        request = _drf_request(self.user, SessionAuthentication(), session_login=True)
        self.assertTrue(credentials.request_is_interactive_session(request))

    def test_session_subclass_is_interactive(self):
        request = _drf_request(self.user, _ExemptSessionAuthentication(), session_login=True)
        self.assertTrue(credentials.request_is_interactive_session(request))

    def test_authenticator_returning_user_and_none_is_refused(self):
        request = _drf_request(self.user, _NoCredentialAuthentication(self.user))
        self.assertIsNone(request.auth)
        self.assertFalse(credentials.request_is_interactive_session(request))

    def test_session_subclass_that_sets_a_credential_is_refused(self):
        request = _drf_request(
            self.user, _SessionWithCredentialAuthentication(), session_login=True
        )
        self.assertFalse(credentials.request_is_interactive_session(request))

    def test_token_authenticator_is_refused(self):
        token = Token.objects.create(user=self.user)
        raw = RequestFactory().patch("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Token {token.key}")
        request = Request(raw, authenticators=[TokenAuthentication()])
        self.assertFalse(credentials.request_is_interactive_session(request))

    def test_force_authenticate_is_not_a_session(self):
        raw = APIRequestFactory().patch("/api/v1/auth/me/")
        force_authenticate(raw, user=self.user)
        probe = {}

        class Probe(APIView):
            def patch(self, request):
                probe["result"] = credentials.request_is_interactive_session(request)
                from rest_framework.response import Response

                return Response()

        Probe.as_view()(raw)
        self.assertIs(probe["result"], False)

    def test_unauthenticated_request_is_refused(self):
        request = Request(RequestFactory().patch("/x/"), authenticators=[SessionAuthentication()])
        self.assertFalse(credentials.request_is_interactive_session(request))

    def test_no_request_is_refused(self):
        self.assertFalse(credentials.request_is_interactive_session(None))

    def test_plain_django_request_is_refused(self):
        # No authenticator to inspect, so a plain Django request is refused.
        request = RequestFactory().get("/accounts/email/")
        self.assertFalse(credentials.request_is_interactive_session(request))

    def test_underlying_request_of_a_stub_authenticated_request_is_refused(self):
        # DRF copies ``auth=None`` onto ``request._request``; it must not pass.
        request = _drf_request(self.user, _NoCredentialAuthentication(self.user))
        self.assertIsNone(request.auth)
        self.assertFalse(credentials.request_is_interactive_session(request._request))


class WsTicketCredentialTests(TestCase):
    """``credential_for_request`` uses the same positive session check."""

    def _with_session_cookie(self, request):
        from django.contrib.sessions.backends.db import SessionStore

        store = SessionStore()
        store.create()
        request._request.session = store
        return store.session_key

    def test_session_request_gets_a_session_credential(self):
        from accounts import ws_auth

        user = User.objects.create_user(username="wsa", email="wsa@example.com", password="pass12345678")
        request = _drf_request(user, SessionAuthentication(), session_login=True)
        key = self._with_session_cookie(request)
        self.assertEqual(ws_auth.credential_for_request(request), ws_auth.session_credential(key))

    def test_stub_authenticator_with_a_session_cookie_gets_no_credential(self):
        from accounts import ws_auth

        user = User.objects.create_user(username="wsb", email="wsb@example.com", password="pass12345678")
        request = _drf_request(user, _NoCredentialAuthentication(user))
        self._with_session_cookie(request)
        self.assertIsNone(ws_auth.credential_for_request(request))


class StubAuthenticatorRefusedOnSessionOnlyRulesTests(TestCase):
    """The rules built on the helper refuse a ``(user, None)`` authenticator."""

    def test_first_password_email_and_username_rules_refuse_it(self):
        user = User.objects.create_user(username="stubby", email="stubby@example.com")
        user.set_unusable_password()
        user.save()
        request = _drf_request(user, _NoCredentialAuthentication(user))
        self.assertTrue(credentials.first_password_requires_session(request, user))
        self.assertTrue(
            credentials.email_change_requires_session(request, "stubby@example.com", "new@example.com")
        )
        self.assertTrue(credentials.username_change_requires_session(request, "stubby", "other"))

    def test_provider_disconnect_refuses_it(self):
        user = User.objects.create_user(
            username="disc", email="disc@example.com", password="pass12345678"
        )
        original = ConnectedAccountDetailView.authentication_classes
        ConnectedAccountDetailView.authentication_classes = [_StubFor(user)]
        try:
            response = APIClient().delete("/api/v1/auth/me/connected-accounts/github/")
        finally:
            ConnectedAccountDetailView.authentication_classes = original
        self.assertEqual(response.status_code, 403)
        self.assertIn("interactive session only", response.json()["detail"])

    def test_provider_disconnect_still_allows_a_real_session(self):
        user = User.objects.create_user(
            username="disc2", email="disc2@example.com", password="pass12345678"
        )
        client = APIClient()
        client.force_login(user)
        response = client.delete("/api/v1/auth/me/connected-accounts/github/")
        self.assertNotEqual(response.status_code, 403)


def _StubFor(user):
    class _Bound(_NoCredentialAuthentication):
        def __init__(self):
            super().__init__(user)

    return _Bound
