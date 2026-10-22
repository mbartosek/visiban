"""Changing the username requires an interactive session.

Registry rule (``docs/development/security-invariants.md``, "Account username
change" row): ``PATCH``/``PUT /api/v1/auth/user/``, ``PATCH /api/v1/auth/me/``
and ``POST /api/v1/auth/choose-username/`` accept a changed ``username`` only
from a session. A request authenticated with a personal access token or the
DRF auth ``Token`` gets 403 and nothing is written. Omitting ``username``, or
sending the stored value back unchanged, is not a change, so token clients
can still edit the rest of the profile.
"""

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts import credentials
from accounts.authentication import SCOPE_READ, SCOPE_WRITE
from accounts.models import PersonalAccessToken, User

PROFILE_ENDPOINTS = ("/api/v1/auth/me/", "/api/v1/auth/user/")
CHOOSE_URL = "/api/v1/auth/choose-username/"
OLD = "dana"
NEW = "dana-renamed"


def _token_client(raw):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {raw}")
    return client


class UsernameChangeSessionRequirementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username=OLD, email="dana@example.com", password="pass12345678"
        )
        _, self.pat = PersonalAccessToken.generate(
            self.user, "agent", scopes=[SCOPE_READ, SCOPE_WRITE]
        )
        self.login_key = Token.objects.create(user=self.user).key

    def _assert_refused(self, r):
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.json()["detail"], credentials.USERNAME_CHANGE_SESSION_REQUIRED_MESSAGE)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, OLD)

    def _credentials(self):
        return {"pat": self.pat, "login token": self.login_key}

    # -- token credentials are refused ------------------------------------

    def test_token_profile_rename_is_refused_and_writes_nothing(self):
        for label, raw in self._credentials().items():
            for url in PROFILE_ENDPOINTS:
                with self.subTest(credential=label, url=url):
                    r = _token_client(raw).patch(
                        url, {"username": NEW, "first_name": "Dana"}, format="json"
                    )
                    self._assert_refused(r)
                    self.assertEqual(self.user.first_name, "")

    def test_token_put_rename_is_refused(self):
        r = _token_client(self.pat).put("/api/v1/auth/user/", {"username": NEW}, format="json")
        self._assert_refused(r)

    def test_token_recasing_or_blank_is_a_change_and_is_refused(self):
        for value in ("", OLD.upper()):
            with self.subTest(value=value):
                r = _token_client(self.pat).patch(
                    "/api/v1/auth/me/", {"username": value}, format="json"
                )
                self._assert_refused(r)

    def test_refusal_does_not_depend_on_whether_the_name_is_taken(self):
        User.objects.create_user(username=NEW, email="x@example.com", password="pass12345678")
        r = _token_client(self.pat).patch("/api/v1/auth/me/", {"username": NEW}, format="json")
        self._assert_refused(r)

    def test_token_choose_username_is_refused_and_writes_nothing(self):
        for label, raw in self._credentials().items():
            with self.subTest(credential=label):
                self.user.must_change_username = True
                self.user.save(update_fields=["must_change_username"])
                r = _token_client(raw).post(CHOOSE_URL, {"username": NEW}, format="json")
                self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
                self.user.refresh_from_db()
                self.assertEqual(self.user.username, OLD)
                self.assertTrue(self.user.must_change_username)

    def test_legacy_unscoped_pat_is_refused(self):
        _, legacy = PersonalAccessToken.generate(self.user, "legacy")
        r = _token_client(legacy).patch("/api/v1/auth/me/", {"username": NEW}, format="json")
        self._assert_refused(r)

    def test_token_alongside_a_session_is_refused(self):
        client = APIClient()
        client.force_login(self.user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {self.pat}")
        r = client.patch("/api/v1/auth/me/", {"username": NEW}, format="json")
        self._assert_refused(r)

    # -- token edits that leave the username alone still work -------------

    def test_token_can_edit_other_fields_and_resend_same_username(self):
        for url in PROFILE_ENDPOINTS:
            for body in ({"first_name": "Dana"}, {"username": OLD, "first_name": "Dana"}):
                with self.subTest(url=url, body=body):
                    r = _token_client(self.pat).patch(url, body, format="json")
                    self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.username, OLD)
                    self.assertEqual(self.user.first_name, "Dana")

    # -- sessions are unaffected ------------------------------------------

    def test_session_can_rename_through_every_route(self):
        for i, (method, url) in enumerate(
            [("patch", "/api/v1/auth/me/"), ("patch", "/api/v1/auth/user/"), ("post", CHOOSE_URL)]
        ):
            with self.subTest(url=url):
                client = APIClient()
                client.force_login(self.user)
                name = f"dana-s{i}"
                r = getattr(client, method)(url, {"username": name}, format="json")
                self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
                self.user.refresh_from_db()
                self.assertEqual(self.user.username, name)


class UsernameChangeRuleTests(SimpleTestCase):
    def _request(self, auth):
        request = RequestFactory().patch("/")
        request.auth = auth
        return request

    def test_no_request_is_not_refused(self):
        self.assertFalse(credentials.username_change_requires_session(None, "a", "b"))

    def test_unchanged_is_not_a_change_for_a_token(self):
        self.assertFalse(
            credentials.username_change_requires_session(self._request(object()), "a", "a")
        )

    def test_changed_is_refused_for_a_token_and_allowed_for_a_session(self):
        self.assertTrue(
            credentials.username_change_requires_session(self._request(object()), "a", "b")
        )
        self.assertFalse(
            credentials.username_change_requires_session(self._request(None), "a", "b")
        )
