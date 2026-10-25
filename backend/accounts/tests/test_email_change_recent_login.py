"""An email change on an account without a password needs a recent provider sign-in (#1932).

Registry Rule 3 (``docs/development/security-invariants.md``, "Account email
change" row): an account with no usable password has no current password to
prove, so adding or changing its email address needs the same proof as setting
a first password (#1260): a recent provider sign-in to the account, recorded in
this session (``accounts.credentials.email_change_requires_recent_login``).
Every surface a signed-in user can reach is covered:

- ``PATCH``/``PUT /api/v1/auth/user/`` and ``PATCH /api/v1/auth/me/`` (the
  shared ``UserSerializer``): 403 ``{"detail", "code": "recent_login_required"}``
  before any other email validation, in every ``EMAIL_VERIFICATION`` mode.
- allauth's ``/accounts/email/`` page: "Add" and "Make Primary" are refused
  with an error message and change nothing.

Accounts with a usable password keep the current-password rule
(``test_email_change_reauth.py``), and password reset to an address that was
already verified keeps working.
"""

from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts import credentials
from accounts.models import User
from accounts.tests.provider_sign_in_helpers import (
    link_identity,
    record_provider_sign_in,
    sign_in_through_provider,
)

PROFILE_ENDPOINTS = ("/api/v1/auth/me/", "/api/v1/auth/user/")
VERIFICATION_MODES = ("mandatory", "optional", "none")
PASSWORD = "pass12345678"  # gitleaks:allow -- test-only fixture password, not a credential
OLD = "sso@old.example"
NEW = "sso@new.example"


def _no_password_user(username="sso", email=OLD):
    user = User.objects.create_user(username=username, email=email)
    user.set_unusable_password()
    user.save()
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


class RestEmailChangeRecentLoginTests(TestCase):
    """The two profile routes, which share ``UserSerializer``."""

    def setUp(self):
        cache.clear()
        self.user = _no_password_user()
        self.client = APIClient()
        self.client.force_login(self.user)

    def _assert_refused(self, r):
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.json()["code"], credentials.FIRST_PASSWORD_RECENT_LOGIN_REQUIRED_CODE)
        self.assertEqual(r.json()["detail"], credentials.EMAIL_CHANGE_RECENT_LOGIN_REQUIRED_MESSAGE)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)
        self.assertIsNone(self.user.pending_email_address_id)
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())

    def test_session_without_a_provider_sign_in_is_refused_in_every_mode(self):
        # force_login writes no socialaccount record, like a REST login.
        for mode in VERIFICATION_MODES:
            for url in PROFILE_ENDPOINTS:
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    self._assert_refused(self.client.patch(url, {"email": NEW}, format="json"))

    def test_put_is_refused_too(self):
        r = self.client.put(
            "/api/v1/auth/user/", {"username": "sso", "email": NEW}, format="json"
        )
        self._assert_refused(r)

    def test_blanking_or_recasing_the_email_is_refused(self):
        for value in ("", OLD.upper()):
            with self.subTest(value=value):
                r = self.client.patch("/api/v1/auth/me/", {"email": value}, format="json")
                self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
                self.user.refresh_from_db()
                self.assertEqual(self.user.email, OLD)

    def test_provider_sign_in_older_than_the_window_is_refused(self):
        sign_in_through_provider(self.client, self.user, seconds_ago=301)
        self._assert_refused(self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json"))

    def test_recent_sign_in_with_an_identity_not_linked_to_this_account_is_refused(self):
        link_identity(self.user, "google", "google-mine")
        record_provider_sign_in(self.client, "google", "google-someone-else")
        self._assert_refused(self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json"))

    def test_recent_provider_sign_in_allows_the_change_when_not_mandatory(self):
        sign_in_through_provider(self.client, self.user)
        for mode in ("optional", "none"):
            for n, url in enumerate(PROFILE_ENDPOINTS):
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    new = f"sso-{mode}-{n}@new.example"
                    r = self.client.patch(url, {"email": new}, format="json")
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.email, new)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_recent_provider_sign_in_starts_a_pending_change_when_mandatory(self):
        sign_in_through_provider(self.client, self.user)
        r = self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["pending_email"], NEW)

    def test_refusal_comes_before_the_address_taken_answer(self):
        User.objects.create_user(username="other", email=NEW, password=PASSWORD)
        with override_settings(ACCOUNT_EMAIL_VERIFICATION="optional"):
            r = self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.json()["code"], "recent_login_required")

    def test_requests_that_leave_the_email_alone_are_not_gated(self):
        for body in ({"display_name": "Kim"}, {"email": OLD, "display_name": "Kim"}):
            with self.subTest(body=body):
                r = self.client.patch("/api/v1/auth/me/", body, format="json")
                self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_token_caller_still_gets_the_session_refusal(self):
        from rest_framework.authtoken.models import Token

        token = Token.objects.create(user=self.user)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        r = client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.json(), {"detail": credentials.EMAIL_CHANGE_SESSION_REQUIRED_MESSAGE})


class PasswordAccountUnchangedTests(TestCase):
    """An account with a usable password keeps the current-password rule."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="kim", email=OLD, password=PASSWORD)
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_missing_password_is_a_400_not_the_recent_login_403(self):
        r = self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", r.json())

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_correct_password_applies_the_change_without_a_provider_sign_in(self):
        r = self.client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": PASSWORD}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)


class ManageEmailPageRecentLoginTests(TestCase):
    """allauth's ``/accounts/email/`` for an account without a password."""

    def setUp(self):
        cache.clear()
        self.user = _no_password_user()
        self.client = Client()
        self.client.force_login(self.user)

    def test_add_without_a_provider_sign_in_is_refused(self):
        from django.contrib.messages import get_messages

        r = self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())
        self.assertIn(
            credentials.EMAIL_CHANGE_RECENT_LOGIN_REQUIRED_MESSAGE,
            [str(m) for m in get_messages(r.wsgi_request)],
        )

    def test_add_without_a_provider_sign_in_redirects_back_not_to_reauthenticate(self):
        r = self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertEqual(r.status_code, status.HTTP_302_FOUND)
        self.assertNotIn("/accounts/reauthenticate/", r["Location"])
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())

    def test_make_primary_without_a_provider_sign_in_is_refused(self):
        EmailAddress.objects.create(user=self.user, email=NEW, verified=True, primary=False)
        r = self.client.post("/accounts/email/", {"email": NEW, "action_primary": ""})
        self.assertEqual(r.status_code, status.HTTP_302_FOUND)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)
        self.assertTrue(EmailAddress.objects.get(user=self.user, email=OLD).primary)

    def test_stale_provider_sign_in_is_refused(self):
        sign_in_through_provider(self.client, self.user, seconds_ago=301)
        self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())

    def test_recent_provider_sign_in_allows_add_and_make_primary(self):
        sign_in_through_provider(self.client, self.user)
        self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertTrue(EmailAddress.objects.filter(user=self.user, email=NEW).exists())
        EmailAddress.objects.filter(user=self.user, email=NEW).update(verified=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/accounts/email/", {"email": NEW, "action_primary": ""})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)

    def test_other_actions_are_not_gated(self):
        EmailAddress.objects.create(user=self.user, email=NEW, verified=False, primary=False)
        self.client.post("/accounts/email/", {"email": NEW, "action_remove": ""})
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())


class ResetToAPreviouslyVerifiedAddressTests(TestCase):
    """Password reset to an address the account already had keeps working."""

    def setUp(self):
        cache.clear()

    def test_reset_still_sets_a_first_password(self):
        user = _no_password_user(username="stuck", email="stuck@example.com")
        client = APIClient()
        r = client.post("/api/v1/auth/password/reset/", {"email": "stuck@example.com"})
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(len(mail.outbox), 1)

        r = client.post("/api/v1/auth/password/reset/confirm/", {
            "uid": user_pk_to_url_str(user),
            "token": default_token_generator.make_token(user),
            "new_password1": "NewPassword9876",
            "new_password2": "NewPassword9876",
        })
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.check_password("NewPassword9876"))


class RecentLoginPredicateTests(TestCase):
    def test_predicate(self):
        from django.test import RequestFactory

        user = User(username="p", email=OLD)
        user.set_unusable_password()
        request = RequestFactory().patch("/api/v1/auth/me/")
        self.assertFalse(credentials.email_change_requires_recent_login(None, user, NEW))
        self.assertFalse(credentials.email_change_requires_recent_login(request, user, OLD))
        # No session on the request: fails closed.
        self.assertTrue(credentials.email_change_requires_recent_login(request, user, NEW))
        user.set_password(PASSWORD)
        self.assertFalse(credentials.email_change_requires_recent_login(request, user, NEW))
