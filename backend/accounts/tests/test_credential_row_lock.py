"""Credential issuance and password-change finalization are serialized on the user row (#1564, #1586).

Creating a personal access token, issuing the DRF auth token at REST login
(#1586) and changing the password all take the user row lock first
(``accounts.credentials.lock_user_row``), so for one user they run one after
the other and the later one sees the earlier one's committed state. The
threaded tests use a PostgreSQL pause-and-barrier (as in
``boards/tests/test_concurrent_lock_sites.py``, #1524) to make the ordering
deterministic, and are skipped on SQLite, which has no row locks.
``LoginRechecksLockedRowTests`` runs on every backend.
"""

import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts import credentials
from accounts.models import PersonalAccessToken, User

HOLD_SECONDS = 2.5
OLD_PASSWORD = "OldPassword123!"
NEW_PASSWORD = "NewPassword456!"


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class CredentialRowLockTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="locker", password=OLD_PASSWORD)
        PersonalAccessToken.generate(self.user, "existing")

    def _change_password(self):
        user = User.objects.get(pk=self.user.pk)
        client = APIClient()
        client.force_authenticate(user)
        return client.post(
            "/api/v1/auth/change-password/",
            {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
            format="json",
        )

    def _create_token(self, earlier_user):
        client = APIClient()
        client.force_authenticate(earlier_user)
        return client.post("/api/v1/auth/tokens/", {"name": "racing"}, format="json")

    def _run_pair(self, first, second, patch_obj, attr):
        """Run ``first`` paused inside its transaction, then ``second``."""
        paused = threading.Event()
        results, errors, marks = {}, [], {}
        original = getattr(patch_obj, attr)

        def hold(*args, **kwargs):
            out = original(*args, **kwargs)
            if threading.current_thread().name == "first" and not paused.is_set():
                paused.set()
                threading.Event().wait(HOLD_SECONDS)
                marks["hold_ended"] = time.monotonic()
            return out

        def worker(name, fn):
            try:
                results[name] = (fn(), time.monotonic())
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(patch_obj, attr, side_effect=hold):
            t1 = threading.Thread(target=worker, args=("first", first), name="first")
            t1.start()
            self.assertTrue(paused.wait(timeout=15), "first request never reached the pause")
            t2 = threading.Thread(target=worker, args=("second", second), name="second")
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
        self.assertEqual(errors, [])
        return results, marks["hold_ended"]

    def test_token_create_after_password_change_is_refused(self):
        earlier_user = User.objects.get(pk=self.user.pk)
        results, hold_ended = self._run_pair(
            self._change_password,
            lambda: self._create_token(earlier_user),
            credentials,
            "lock_user_row",
        )
        # lock_user_row is also called by the second request; only the first
        # thread pauses, and it pauses holding the lock it just took.
        change, _ = results["first"]
        create, create_done = results["second"]
        self.assertEqual(change.status_code, 200)
        self.assertGreaterEqual(create_done, hold_ended)
        self.assertEqual(create.status_code, 401)
        self.assertEqual(PersonalAccessToken.objects.filter(user_id=self.user.pk).count(), 0)

    def test_password_change_after_token_create_removes_token(self):
        earlier_user = User.objects.get(pk=self.user.pk)
        results, hold_ended = self._run_pair(
            lambda: self._create_token(earlier_user),
            self._change_password,
            credentials,
            "lock_user_row",
        )
        create, _ = results["first"]
        change, change_done = results["second"]
        self.assertEqual(create.status_code, 201)
        self.assertEqual(change.status_code, 200)
        self.assertGreaterEqual(change_done, hold_ended)
        self.assertEqual(PersonalAccessToken.objects.filter(user_id=self.user.pk).count(), 0)

    # REST login (#1586): the DRF auth Token is issued under the same lock.

    def _login(self, password):
        return APIClient().post(
            "/api/v1/auth/login/",
            {"username": "locker", "password": password},
            format="json",
        )

    def test_login_after_password_change_issues_no_token(self):
        results, hold_ended = self._run_pair(
            self._change_password,
            lambda: self._login(OLD_PASSWORD),
            credentials,
            "lock_user_row",
        )
        change, _ = results["first"]
        login, login_done = results["second"]
        self.assertEqual(change.status_code, 200)
        self.assertGreaterEqual(login_done, hold_ended)
        self.assertEqual(login.status_code, 400)
        self.assertNotIn("key", login.json())
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_password_change_after_login_removes_token(self):
        results, hold_ended = self._run_pair(
            lambda: self._login(OLD_PASSWORD),
            self._change_password,
            credentials,
            "lock_user_row",
        )
        login, _ = results["first"]
        change, change_done = results["second"]
        self.assertEqual(login.status_code, 200)
        self.assertIn("key", login.json())
        self.assertEqual(change.status_code, 200)
        self.assertGreaterEqual(change_done, hold_ended)
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())


class LoginRechecksLockedRowTests(TestCase):
    """Login re-reads the user row under the lock before issuing a Token (#1586).

    Runs on every backend: the locked read is replaced with a row as another
    transaction would have left it.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="relogin", password=OLD_PASSWORD)

    def _login_with_locked_row(self, **changes):
        def locked_row(user):
            row = User.objects.get(pk=user.pk)
            for field, value in changes.items():
                setattr(row, field, value)
            return row

        with patch.object(credentials, "lock_user_row", side_effect=locked_row):
            return APIClient().post(
                "/api/v1/auth/login/",
                {"username": "relogin", "password": OLD_PASSWORD},
                format="json",
            )

    def test_login_refused_when_stored_password_differs(self):
        response = self._login_with_locked_row(password="pbkdf2_sha256$1$other$hash")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(), {"non_field_errors": ["Unable to log in with provided credentials."]}
        )
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_login_refused_when_account_inactive(self):
        response = self._login_with_locked_row(is_active=False)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_login_with_unchanged_row_returns_key(self):
        response = self._login_with_locked_row()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"key"})
        self.assertEqual(response.json()["key"], Token.objects.get(user_id=self.user.pk).key)
