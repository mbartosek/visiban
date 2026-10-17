"""Changing the account email requires an interactive session.

Registry Rule 3 (``docs/development/security-invariants.md``, "Account email
change" row): ``PATCH``/``PUT /api/v1/auth/user/`` and ``PATCH
/api/v1/auth/me/`` accept a changed ``email`` (a direct write, or a pending
change confirmed from the new mailbox under ``EMAIL_VERIFICATION=mandatory``)
only from a session. A request authenticated with a personal access token or
the DRF auth ``Token`` gets 403 and nothing is written. Omitting ``email``,
or sending the stored value back unchanged, is not a change, so token clients
can still edit the rest of the profile.
"""

from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts import credentials
from accounts.authentication import SCOPE_READ, SCOPE_WRITE
from accounts.models import PersonalAccessToken, User
from accounts.tests.email_change_helpers import PasswordConfirmingAPIClient

PROFILE_ENDPOINTS = ("/api/v1/auth/me/", "/api/v1/auth/user/")
VERIFICATION_MODES = ("mandatory", "optional", "none")
OLD = "dana@old.example"
NEW = "dana@new.example"


def _token_client(raw):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {raw}")
    return client


class EmailChangeSessionRequirementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="dana", email=OLD, password="pass12345678"
        )
        EmailAddress.objects.create(user=self.user, email=OLD, verified=True, primary=True)
        _, self.pat = PersonalAccessToken.generate(
            self.user, "agent", scopes=[SCOPE_READ, SCOPE_WRITE]
        )

    def _assert_unchanged(self):
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)
        self.assertIsNone(self.user.pending_email_address_id)
        self.assertEqual(
            list(EmailAddress.objects.filter(user=self.user).values_list("email", flat=True)),
            [OLD],
        )
        self.assertEqual(len(mail.outbox), 0)

    def _assert_refused(self, r):
        self.assertEqual(r.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(r.json()["detail"], credentials.EMAIL_CHANGE_SESSION_REQUIRED_MESSAGE)
        self._assert_unchanged()

    # ── token credentials are refused ─────────────────────────────────────

    def test_pat_email_change_is_refused_in_every_verification_mode(self):
        for mode in VERIFICATION_MODES:
            for url in PROFILE_ENDPOINTS:
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    mail.outbox.clear()
                    r = _token_client(self.pat).patch(
                        url, {"email": NEW, "first_name": "Dana"}, format="json"
                    )
                    self._assert_refused(r)
                    self.assertEqual(self.user.first_name, "")

    def test_pat_put_with_changed_email_is_refused(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                r = _token_client(self.pat).put(
                    "/api/v1/auth/user/", {"username": "dana", "email": NEW}, format="json"
                )
                self._assert_refused(r)

    def test_legacy_unscoped_pat_is_refused(self):
        _, legacy = PersonalAccessToken.generate(self.user, "legacy")
        r = _token_client(legacy).patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self._assert_refused(r)

    def test_drf_auth_token_is_refused(self):
        key = Token.objects.create(user=self.user).key
        for url in PROFILE_ENDPOINTS:
            with self.subTest(url=url):
                r = _token_client(key).patch(url, {"email": NEW}, format="json")
                self._assert_refused(r)

    def test_token_alongside_a_session_is_refused(self):
        client = PasswordConfirmingAPIClient(password="pass12345678")
        client.force_login(self.user)
        client.credentials(HTTP_AUTHORIZATION=f"Token {self.pat}")
        r = client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self._assert_refused(r)

    def test_pat_blanking_or_recasing_email_is_refused(self):
        for value in ("", OLD.upper()):
            with self.subTest(value=value):
                r = _token_client(self.pat).patch(
                    "/api/v1/auth/me/", {"email": value}, format="json"
                )
                self._assert_refused(r)

    def test_pat_refusal_does_not_depend_on_whether_the_address_is_taken(self):
        User.objects.create_user(username="other", email=NEW, password="pass12345678")
        with override_settings(ACCOUNT_EMAIL_VERIFICATION="optional"):
            r = _token_client(self.pat).patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self._assert_refused(r)

    # ── token edits that leave the email alone still work ─────────────────

    def test_pat_patch_without_email_succeeds(self):
        for mode in VERIFICATION_MODES:
            for n, url in enumerate(PROFILE_ENDPOINTS):
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    r = _token_client(self.pat).patch(
                        url, {"first_name": f"Dana{n}{mode}"}, format="json"
                    )
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.first_name, f"Dana{n}{mode}")
                    self._assert_unchanged()

    def test_pat_patch_with_unchanged_email_succeeds(self):
        for mode in VERIFICATION_MODES:
            for n, url in enumerate(PROFILE_ENDPOINTS):
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    r = _token_client(self.pat).patch(
                        url, {"email": OLD, "last_name": f"L{n}{mode}"}, format="json"
                    )
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    self.assertEqual(r.json()["email"], OLD)
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.last_name, f"L{n}{mode}")
                    self._assert_unchanged()

    def test_pat_put_with_unchanged_email_succeeds(self):
        r = _token_client(self.pat).put(
            "/api/v1/auth/user/",
            {"username": "dana", "email": OLD, "first_name": "Put"},
            format="json",
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Put")
        self._assert_unchanged()

    def test_account_without_email_can_save_profile_with_a_pat(self):
        User.objects.filter(pk=self.user.pk).update(email="")
        EmailAddress.objects.filter(user=self.user).delete()
        r = _token_client(self.pat).patch(
            "/api/v1/auth/me/", {"email": "", "first_name": "NoMail"}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "")
        self.assertEqual(self.user.first_name, "NoMail")

    # ── sessions (with the current password) behave as before ──────────

    def test_session_change_is_written_directly_when_not_mandatory(self):
        for mode in ("optional", "none"):
            for n, url in enumerate(PROFILE_ENDPOINTS):
                with self.subTest(mode=mode, url=url), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    client = PasswordConfirmingAPIClient(password="pass12345678")
                    client.force_login(self.user)
                    new = f"dana-{mode}-{n}@new.example"
                    r = client.patch(url, {"email": new}, format="json")
                    self.assertEqual(r.status_code, status.HTTP_200_OK)
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.email, new)

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_session_change_becomes_pending_when_mandatory(self):
        for n, url in enumerate(PROFILE_ENDPOINTS):
            with self.subTest(url=url):
                mail.outbox.clear()
                client = PasswordConfirmingAPIClient(password="pass12345678")
                client.force_login(self.user)
                new = f"dana-pending-{n}@new.example"
                r = client.patch(url, {"email": new}, format="json")
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                self.assertEqual(r.json()["pending_email"], new)
                self.user.refresh_from_db()
                self.assertEqual(self.user.email, OLD)
                self.assertEqual(len(mail.outbox), 1)
                self.assertEqual(mail.outbox[0].to, [new])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_pat_can_withdraw_a_pending_change_by_sending_the_current_address(self):
        client = PasswordConfirmingAPIClient(password="pass12345678")
        client.force_login(self.user)
        client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.pending_email_address_id)

        r = _token_client(self.pat).patch("/api/v1/auth/me/", {"email": OLD}, format="json")

        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertIsNone(r.json()["pending_email"])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)
        self.assertIsNone(self.user.pending_email_address_id)


class UnchangedEmailIsNotWrittenTests(TestCase):
    """An unchanged email is dropped before the save, not rewritten from a stale copy."""

    def test_unchanged_email_does_not_overwrite_a_concurrent_change(self):
        from accounts.serializers import CurrentUserSerializer

        user = User.objects.create_user(username="erin", email=OLD, password="pass12345678")
        serializer = CurrentUserSerializer(
            user, data={"email": OLD, "first_name": "Erin"}, partial=True
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        # Another request changes the address after this one was validated.
        User.objects.filter(pk=user.pk).update(email=NEW)

        serializer.save()

        user.refresh_from_db()
        self.assertEqual(user.email, NEW)
        self.assertEqual(user.first_name, "Erin")


class EmailChangeRequiresSessionHelperTests(SimpleTestCase):
    """The predicate on its own, including the no-request case."""

    def _request(self, auth):
        request = RequestFactory().patch("/api/v1/auth/me/")
        request.auth = auth
        return request

    def test_session_request_is_interactive(self):
        self.assertTrue(credentials.request_is_interactive_session(self._request(None)))

    def test_any_credential_object_is_not_interactive(self):
        self.assertFalse(credentials.request_is_interactive_session(self._request(object())))

    def test_unchanged_value_is_never_refused(self):
        request = self._request(object())
        self.assertFalse(credentials.email_change_requires_session(request, OLD, OLD))
        self.assertFalse(credentials.email_change_requires_session(request, None, ""))

    def test_changed_value_is_refused_only_for_a_token(self):
        self.assertTrue(credentials.email_change_requires_session(self._request(object()), OLD, NEW))
        self.assertFalse(credentials.email_change_requires_session(self._request(None), OLD, NEW))

    def test_no_request_is_not_refused(self):
        self.assertFalse(credentials.email_change_requires_session(None, OLD, NEW))
