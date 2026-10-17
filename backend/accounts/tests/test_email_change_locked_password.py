"""The current password for an email change is verified against the locked, stored row (#1711).

Registry Rule 3 (``docs/development/security-invariants.md``, "Account email
change" row): the password is accepted against the copy of the account loaded
when the request started, and ``UserSerializer.update`` then re-reads the row
under ``accounts.credentials.lock_user_row`` and refuses when the stored hash is
no longer the one that was checked, mirroring Personal Access Token creation.
"""

import threading
from unittest.mock import patch

from allauth.account.models import EmailAddress
from django.core import mail
from django.core.cache import cache
from django.db import connection, connections, transaction
from django.test import (
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from rest_framework import status
from rest_framework.test import APIClient

from accounts import credentials
from accounts.models import User
from accounts.serializers import UserSerializer
from boards.tests.lock_wait import assert_blocked_on_lock

PASSWORD = (
    "pass12345678"  # gitleaks:allow -- test-only fixture password, not a credential
)
ROTATED = "rotated-pass-9876"  # gitleaks:allow -- test-only fixture password, not a credential
OLD = "lee@old.example"
NEW = "lee@new.example"
URLS = ("/api/v1/auth/me/", "/api/v1/auth/user/")


def _rotate_password(pk, new_password):
    user = User.objects.get(pk=pk)
    user.set_password(new_password)
    user.save(update_fields=["password"])


class StalePasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username="lee", email=OLD, password=PASSWORD
        )
        EmailAddress.objects.create(
            user=self.user, email=OLD, verified=True, primary=True
        )
        self.client = APIClient()
        self.client.force_login(self.user)

    def _patch_after_rotation(self, url, mode):
        """Submit the old password; the password is rotated after it is accepted."""
        original = UserSerializer._check_current_password_for_email_change

        def check_then_rotate(serializer, data):
            original(serializer, data)
            _rotate_password(self.user.pk, ROTATED)

        with (
            override_settings(ACCOUNT_EMAIL_VERIFICATION=mode),
            patch.object(
                UserSerializer,
                "_check_current_password_for_email_change",
                check_then_rotate,
            ),
        ):
            return self.client.patch(
                url,
                {"email": NEW, "current_password": PASSWORD, "first_name": "Lee"},
                format="json",
            )

    def test_password_rotated_after_the_check_refuses_the_change_in_every_mode(self):
        for mode in ("mandatory", "optional", "none"):
            for url in URLS:
                with self.subTest(mode=mode, url=url):
                    _rotate_password(self.user.pk, PASSWORD)
                    # The session is bound to the password hash; sign in again.
                    self.client.force_login(User.objects.get(pk=self.user.pk))
                    r = self._patch_after_rotation(url, mode)
                    self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertEqual(
                        r.json()["current_password"],
                        [credentials.EMAIL_CHANGE_PASSWORD_INCORRECT_MESSAGE],
                    )
                    self.user.refresh_from_db()
                    self.assertEqual(self.user.email, OLD)
                    self.assertEqual(self.user.first_name, "")
                    self.assertIsNone(self.user.pending_email_address_id)
                    self.assertEqual(mail.outbox, [])

    def test_unrotated_password_still_applies_the_change(self):
        with override_settings(ACCOUNT_EMAIL_VERIFICATION="none"):
            r = self.client.patch(
                "/api/v1/auth/me/",
                {"email": NEW, "current_password": PASSWORD},
                format="json",
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)

    def test_mandatory_success_sends_the_confirmation_only_after_commit(self):
        with override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory"):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                r = self.client.patch(
                    "/api/v1/auth/me/",
                    {"email": NEW, "current_password": PASSWORD},
                    format="json",
                )
            self.assertEqual(r.status_code, status.HTTP_200_OK)
            # Nothing is mailed while the transaction holding the lock is open.
            self.assertEqual(mail.outbox, [])
            pending = EmailAddress.objects.get(user=self.user, email=NEW)
            self.assertFalse(pending.verified)
            for callback in callbacks:
                callback()
        self.assertEqual([m.to for m in mail.outbox], [[NEW]])
        self.user.refresh_from_db()
        self.assertEqual(self.user.pending_email_address_id, pending.pk)
        self.assertEqual(self.user.email, OLD)

    def test_refused_request_sends_no_mail(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self._patch_after_rotation("/api/v1/auth/me/", "mandatory")
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(mail.outbox, [])
        self.assertFalse(
            EmailAddress.objects.filter(user=self.user, email=NEW).exists()
        )

    def test_deactivated_account_is_refused(self):
        original = UserSerializer._check_current_password_for_email_change

        def check_then_deactivate(serializer, data):
            original(serializer, data)
            User.objects.filter(pk=self.user.pk).update(is_active=False)

        with (
            override_settings(ACCOUNT_EMAIL_VERIFICATION="none"),
            patch.object(
                UserSerializer,
                "_check_current_password_for_email_change",
                check_then_deactivate,
            ),
        ):
            r = self.client.patch(
                "/api/v1/auth/me/",
                {"email": NEW, "current_password": PASSWORD},
                format="json",
            )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", r.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)

    def test_request_that_does_not_change_the_email_takes_no_lock_check(self):
        # No password was verified, so a concurrent rotation is not this
        # request's concern and the profile edit applies.
        original = UserSerializer.update

        def rotate_then_update(serializer, instance, validated_data):
            _rotate_password(self.user.pk, ROTATED)
            return original(serializer, instance, validated_data)

        with patch.object(UserSerializer, "update", rotate_then_update):
            r = self.client.patch(
                "/api/v1/auth/me/", {"first_name": "Lee"}, format="json"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Lee")


@skipUnlessDBFeature("has_select_for_update")
class LockedPasswordRaceTests(TransactionTestCase):
    """A password change holding the row lock is waited for, then the change is refused."""

    HOLD_TIMEOUT = 10

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username="lee", email=OLD, password=PASSWORD
        )
        EmailAddress.objects.create(
            user=self.user, email=OLD, verified=True, primary=True
        )

    def test_email_change_waits_for_a_concurrent_password_change_then_is_refused(self):
        if connection.vendor != "postgresql":
            self.skipTest("row-lock wait behavior needs PostgreSQL")
        # Signed in up front: force_login writes last_login on the user row,
        # which would itself wait for the lock the holder takes below.
        client = APIClient()
        client.force_login(self.user)
        holding = threading.Event()
        release = threading.Event()
        result = {}

        def holder():
            try:
                with transaction.atomic():
                    credentials.lock_user_row(self.user)
                    _rotate_password(self.user.pk, ROTATED)
                    holding.set()
                    release.wait(self.HOLD_TIMEOUT)
            finally:
                connections.close_all()

        def changer():
            try:
                with override_settings(ACCOUNT_EMAIL_VERIFICATION="none"):
                    result["response"] = client.patch(
                        "/api/v1/auth/me/",
                        {"email": NEW, "current_password": PASSWORD},
                        format="json",
                    )
            finally:
                connections.close_all()

        holder_thread = threading.Thread(target=holder, name="password-holder")
        changer_thread = threading.Thread(target=changer, name="email-changer")
        holder_thread.start()
        try:
            self.assertTrue(holding.wait(self.HOLD_TIMEOUT))
            changer_thread.start()
            assert_blocked_on_lock(
                self, changer_thread, "email change did not wait for the row lock"
            )
        finally:
            release.set()
            holder_thread.join(self.HOLD_TIMEOUT)
            changer_thread.join(self.HOLD_TIMEOUT)
        self.assertEqual(result["response"].status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", result["response"].json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, OLD)
        self.assertTrue(self.user.check_password(ROTATED))
