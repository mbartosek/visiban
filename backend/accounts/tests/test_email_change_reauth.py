"""Changing the account email needs the current password, and tells the old address.

Registry Rule 3 (``docs/development/security-invariants.md``, "Account email
change" row):

- ``PATCH``/``PUT /api/v1/auth/user/`` and ``PATCH /api/v1/auth/me/``: a
  request that changes ``email`` on an account with a usable password must
  carry the correct ``current_password`` (400 with a ``current_password``
  field error otherwise), checked before any other email validation and
  against the same failed-attempt budget as login. Accounts without a usable
  password are not asked. Requests that leave the email alone need nothing.
- allauth's ``/accounts/email/`` page: "Add" and "Make Primary" need a recent
  reauthentication for an account with a usable password.
- Whatever route moved ``User.email``, the previous address gets a notice
  once the change commits.
"""

from unittest.mock import patch

from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import Client, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts import credentials
from accounts.authentication import SCOPE_READ, SCOPE_WRITE
from accounts.models import PersonalAccessToken, User
from accounts.tests.email_change_helpers import reauthenticate

PROFILE_ENDPOINTS = ("/api/v1/auth/me/", "/api/v1/auth/user/")
VERIFICATION_MODES = ("mandatory", "optional", "none")
VERIFY_EMAIL_URL = "/api/v1/auth/registration/verify-email/"
PASSWORD = "pass12345678"  # gitleaks:allow -- test-only fixture password, not a credential
OLD = "kim@old.example"
NEW = "kim@new.example"
NOTICE_SUBJECT = "Your Visiban email address was changed"


def _notices():
    return [m for m in mail.outbox if m.subject == NOTICE_SUBJECT]


class EmailChangeCurrentPasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="kim", email=OLD, password=PASSWORD)
        EmailAddress.objects.create(user=self.user, email=OLD, verified=True, primary=True)
        self.client = APIClient()
        self.client.force_login(self.user)

    def _assert_unchanged(self):
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)
        self.assertIsNone(self.user.pending_email_address_id)
        self.assertEqual(
            list(EmailAddress.objects.filter(user=self.user).values_list("email", flat=True)),
            [OLD],
        )
        self.assertEqual(mail.outbox, [])

    def test_missing_current_password_is_refused_in_every_mode(self):
        for mode in VERIFICATION_MODES:
            for url in PROFILE_ENDPOINTS:
                for body in ({"email": NEW}, {"email": NEW, "current_password": ""}):
                    with self.subTest(mode=mode, url=url, body=body), override_settings(
                        ACCOUNT_EMAIL_VERIFICATION=mode
                    ):
                        r = self.client.patch(url, {**body, "first_name": "Kim"}, format="json")
                        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
                        self.assertEqual(
                            r.json()["current_password"],
                            [credentials.EMAIL_CHANGE_PASSWORD_REQUIRED_MESSAGE],
                        )
                        self._assert_unchanged()
                        # Nothing in the request is applied.
                        self.assertEqual(self.user.first_name, "")

    def test_wrong_current_password_is_refused_in_every_mode(self):
        for mode in VERIFICATION_MODES:
            for url in PROFILE_ENDPOINTS:
                cache.clear()
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    r = self.client.patch(
                        url, {"email": NEW, "current_password": "not-the-password"}, format="json"
                    )
                    self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertEqual(
                        r.json()["current_password"],
                        [credentials.EMAIL_CHANGE_PASSWORD_INCORRECT_MESSAGE],
                    )
                    self._assert_unchanged()

    def test_put_with_changed_email_needs_the_password(self):
        r = self.client.put(
            "/api/v1/auth/user/", {"username": "kim", "email": NEW}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", r.json())
        self._assert_unchanged()

    def test_blanking_or_recasing_the_email_needs_the_password(self):
        for value in ("", OLD.upper()):
            with self.subTest(value=value):
                r = self.client.patch("/api/v1/auth/me/", {"email": value}, format="json")
                self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("current_password", r.json())
                self._assert_unchanged()

    def test_correct_password_applies_the_change_when_not_mandatory(self):
        for mode in ("optional", "none"):
            for n, url in enumerate(PROFILE_ENDPOINTS):
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    new = f"kim-{mode}-{n}@new.example"
                    r = self.client.patch(
                        url, {"email": new, "current_password": PASSWORD}, format="json"
                    )
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    self.assertNotIn("current_password", r.json())
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.email, new)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_correct_password_starts_a_pending_change_when_mandatory(self):
        r = self.client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": PASSWORD}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["pending_email"], NEW)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)

    def test_requests_that_leave_the_email_alone_need_no_password(self):
        for mode in VERIFICATION_MODES:
            for n, url in enumerate(PROFILE_ENDPOINTS):
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    r = self.client.patch(url, {"first_name": f"K{n}{mode}"}, format="json")
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    r = self.client.patch(
                        url, {"email": OLD, "last_name": f"L{n}{mode}"}, format="json"
                    )
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.first_name, f"K{n}{mode}")
                    self.assertEqual(self.user.last_name, f"L{n}{mode}")
                    self._assert_unchanged()

    def test_whitespace_around_the_stored_email_is_not_a_change(self):
        r = self.client.patch("/api/v1/auth/me/", {"email": f"  {OLD} "}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self._assert_unchanged()

    def test_a_stray_current_password_on_an_unrelated_patch_is_ignored(self):
        r = self.client.patch(
            "/api/v1/auth/me/", {"first_name": "Kim", "current_password": "anything"}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Kim")
        self.assertTrue(self.user.check_password(PASSWORD))

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_withdrawing_a_pending_change_needs_no_password(self):
        self.client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": PASSWORD}, format="json"
        )
        r = self.client.patch("/api/v1/auth/me/", {"email": OLD}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsNone(r.json()["pending_email"])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_password_is_checked_before_the_address_taken_answer(self):
        User.objects.create_user(username="other", email=NEW, password=PASSWORD)
        r = self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(list(r.json()), ["current_password"])
        r = self.client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": "wrong-one"}, format="json"
        )
        self.assertEqual(list(r.json()), ["current_password"])

    def test_wrong_passwords_share_the_login_lockout(self):
        for _ in range(5):
            r = self.client.patch(
                "/api/v1/auth/me/", {"email": NEW, "current_password": "wrong-one"}, format="json"
            )
            self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        r = self.client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": PASSWORD}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self._assert_unchanged()
        # The same account is now locked at the password-change route too.
        r = self.client.post(
            "/api/v1/auth/change-password/",
            {"current_password": PASSWORD, "new_password": "Another-Pass-987"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_correct_password_does_not_consume_the_budget(self):
        for _ in range(4):
            self.client.patch(
                "/api/v1/auth/me/", {"email": NEW, "current_password": "wrong-one"}, format="json"
            )
        r = self.client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": PASSWORD}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        r = self.client.patch(
            "/api/v1/auth/me/", {"email": "kim@third.example", "current_password": "wrong-one"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_token_caller_gets_the_session_refusal_even_with_the_password(self):
        _, pat = PersonalAccessToken.generate(self.user, "agent", scopes=[SCOPE_READ, SCOPE_WRITE])
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {pat}")
        r = client.patch(
            "/api/v1/auth/me/", {"email": NEW, "current_password": PASSWORD}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.json()["detail"], credentials.EMAIL_CHANGE_SESSION_REQUIRED_MESSAGE)
        self._assert_unchanged()

    def test_password_never_appears_in_the_response(self):
        r = self.client.get("/api/v1/auth/me/")
        self.assertNotIn("current_password", r.json())


class EmailChangeWithoutPasswordAccountTests(TestCase):
    """An account that signs in through an identity provider only is not asked."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="sso", email=OLD)
        self.user.set_unusable_password()
        self.user.save()
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_change_needs_no_password_when_not_mandatory(self):
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
    def test_change_needs_no_password_when_mandatory(self):
        r = self.client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.json()["pending_email"], NEW)


class CurrentPasswordPredicateTests(TestCase):
    def test_predicate(self):
        user = User(username="p", email=OLD)
        user.set_password(PASSWORD)
        self.assertFalse(credentials.email_change_requires_current_password(user, OLD))
        self.assertTrue(credentials.email_change_requires_current_password(user, NEW))
        self.assertTrue(credentials.email_change_requires_current_password(user, ""))
        user.set_unusable_password()
        self.assertFalse(credentials.email_change_requires_current_password(user, NEW))


class PreviousAddressNoticeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="kim", email=OLD, password=PASSWORD)
        EmailAddress.objects.create(user=self.user, email=OLD, verified=True, primary=True)
        self.client = APIClient()
        self.client.force_login(self.user)

    def _change(self, new):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch(
                "/api/v1/auth/me/", {"email": new, "current_password": PASSWORD}, format="json"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        return r

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_direct_change_notifies_the_previous_address_only(self):
        self._change(NEW)
        notices = _notices()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].to, [OLD])
        # The new address is not disclosed to the old mailbox.
        self.assertNotIn(NEW, notices[0].body)
        self.assertNotIn(NEW, notices[0].subject)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_blanking_the_email_notifies_the_previous_address(self):
        self._change("")
        self.assertEqual([m.to for m in _notices()], [[OLD]])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_case_only_change_sends_nothing(self):
        self._change(OLD.upper())
        self.assertEqual(_notices(), [])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_account_without_a_previous_address_gets_nothing(self):
        User.objects.filter(pk=self.user.pk).update(email="")
        EmailAddress.objects.filter(user=self.user).delete()
        self._change(NEW)
        self.assertEqual(_notices(), [])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_pending_change_notifies_only_once_confirmed(self):
        self._change(NEW)
        self.assertEqual(_notices(), [])
        pending = EmailAddress.objects.get(user=self.user, email=NEW)
        with self.captureOnCommitCallbacks(execute=True):
            r = APIClient().post(
                VERIFY_EMAIL_URL, {"key": EmailConfirmationHMAC(pending).key}, format="json"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)
        self.assertEqual([m.to for m in _notices()], [[OLD]])

    def test_rolled_back_change_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.user.email = NEW
                    self.user.save(update_fields=["email"])
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(_notices(), [])

    def test_saves_that_do_not_write_the_email_read_nothing_extra(self):
        with self.captureOnCommitCallbacks(execute=True), self.assertNumQueries(1):
            self.user.first_name = "Kim"
            self.user.save(update_fields=["first_name"])
        self.assertEqual(_notices(), [])

    def test_any_route_that_writes_the_email_is_covered(self):
        # e.g. the Django admin, or a management command: a plain model save.
        user = User.objects.get(pk=self.user.pk)
        with self.captureOnCommitCallbacks(execute=True):
            user.email = NEW
            user.save()
        self.assertEqual([m.to for m in _notices()], [[OLD]])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_mail_failure_does_not_fail_the_change_or_log_the_address(self):
        with patch(
            "django.core.mail.EmailMessage.send", side_effect=OSError("relay down")
        ), self.assertLogs("accounts.email_change", level="WARNING") as logs:
            self._change(NEW)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)
        joined = "\n".join(logs.output)
        self.assertNotIn(OLD, joined)
        self.assertNotIn(NEW, joined)
        self.assertIn(f"user_id={self.user.pk}", joined)


class ManageEmailPageReauthTests(TestCase):
    """allauth's ``/accounts/email/``: "Add" and "Make Primary" need a recent password check."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="kim", email=OLD, password=PASSWORD)
        EmailAddress.objects.create(user=self.user, email=OLD, verified=True, primary=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_add_without_reauthentication_redirects_and_adds_nothing(self):
        r = self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertEqual(r.status_code, status.HTTP_302_FOUND)
        self.assertIn("/accounts/reauthenticate/", r["Location"])
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())

    def test_make_primary_without_reauthentication_redirects_and_changes_nothing(self):
        EmailAddress.objects.create(user=self.user, email=NEW, verified=True, primary=False)
        r = self.client.post("/accounts/email/", {"email": NEW, "action_primary": ""})
        self.assertEqual(r.status_code, status.HTTP_302_FOUND)
        self.assertIn("/accounts/reauthenticate/", r["Location"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)

    def test_wrong_password_on_the_reauthenticate_page_does_not_unlock_the_page(self):
        self.client.post("/accounts/reauthenticate/", {"password": "wrong-one"})
        r = self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertIn("/accounts/reauthenticate/", r["Location"])
        self.assertFalse(EmailAddress.objects.filter(user=self.user, email=NEW).exists())

    def test_after_reauthentication_add_and_make_primary_work(self):
        reauthenticate(self.client, PASSWORD)
        self.client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertTrue(EmailAddress.objects.filter(user=self.user, email=NEW).exists())
        EmailAddress.objects.filter(user=self.user, email=NEW).update(verified=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/accounts/email/", {"email": NEW, "action_primary": ""})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)
        self.assertEqual([m.to for m in _notices()], [[OLD]])

    def test_account_without_a_password_is_not_asked(self):
        sso = User.objects.create_user(username="sso", email="sso@old.example")
        sso.set_unusable_password()
        sso.save()
        client = Client()
        client.force_login(sso)
        r = client.post("/accounts/email/", {"email": NEW, "action_add": ""})
        self.assertNotIn("/accounts/reauthenticate/", r.get("Location", ""))
        self.assertTrue(EmailAddress.objects.filter(user=sso, email=NEW).exists())
