"""Credential issuance and password-change finalization are serialized on the user row (#1564, #1586).

Creating a personal access token, issuing the DRF auth token at REST login
(#1586) and changing the password all take the user row lock first
(``accounts.credentials.lock_user_row``), so for one user they run one after
the other and the later one sees the earlier one's committed state. The
threaded tests use a PostgreSQL pause-and-barrier (as in
``boards/tests/test_concurrent_lock_sites.py``, #1524) to make the ordering
deterministic, and are skipped on SQLite, which has no row locks.
``LoginRechecksLockedRowTests`` runs on every backend.

The login-time password hash upgrade writes only over the hash it was derived
from (#1601): ``LoginHashUpgradeRowLockTests`` interleaves it with a password
set on PostgreSQL, and ``ConditionalHashUpgradeTests`` runs on every backend.
"""

import threading
import time
from unittest import skipUnless
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.hashers import check_password, make_password
from django.db import connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
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
            response.json(),
            {"non_field_errors": ["Unable to log in with provided credentials."]},
        )
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_login_refused_when_account_inactive(self):
        response = self._login_with_locked_row(is_active=False)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_login_refused_when_user_row_no_longer_exists(self):
        with patch.object(credentials, "lock_user_row", side_effect=User.DoesNotExist):
            response = APIClient().post(
                "/api/v1/auth/login/",
                {"username": "relogin", "password": OLD_PASSWORD},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json(),
            {"non_field_errors": ["Unable to log in with provided credentials."]},
        )
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_login_with_unchanged_row_returns_key(self):
        response = self._login_with_locked_row()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), {"key"})
        self.assertEqual(response.json()["key"], Token.objects.get(user_id=self.user.pk).key)


UPGRADE_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.MD5PasswordHasher",
]


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
@override_settings(PASSWORD_HASHERS=UPGRADE_HASHERS)
class LoginHashUpgradeRowLockTests(TransactionTestCase):
    """The login-time hash upgrade only replaces the hash it was derived from (#1601).

    The stored hash uses a hasher that is not the preferred one, so a login
    with the right password makes Django rewrite it. A password set committed
    by another transaction in between stays in place.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="upgrader", password=OLD_PASSWORD)
        self.legacy = make_password(OLD_PASSWORD, hasher="md5")
        User.objects.filter(pk=self.user.pk).update(password=self.legacy)

    def _set_password(self):
        # Reset and admin-set shape: no check_password on the way in.
        with transaction.atomic():
            locked = credentials.lock_user_row(self.user)
            locked.set_password(NEW_PASSWORD)
            locked.save(update_fields=["password"])
            credentials.finalize_password_change(locked)
        return True

    def _login(self):
        return APIClient().post(
            "/api/v1/auth/login/",
            {"username": "upgrader", "password": OLD_PASSWORD},
            format="json",
        )

    def _run_pair(self, first, second):
        """Run ``first`` paused just after it takes the user-row lock, then ``second``."""
        paused = threading.Event()
        results, errors, marks = {}, [], {}
        original = credentials.lock_user_row

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

        with patch.object(credentials, "lock_user_row", side_effect=hold):
            t1 = threading.Thread(target=worker, args=("first", first), name="first")
            t1.start()
            self.assertTrue(paused.wait(timeout=15), "first thread never reached the pause")
            t2 = threading.Thread(target=worker, args=("second", second), name="second")
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
        self.assertEqual(errors, [])
        return results, marks["hold_ended"]

    def _assert_new_password_stored_and_no_token(self):
        stored = User.objects.get(pk=self.user.pk).password
        self.assertTrue(check_password(NEW_PASSWORD, stored))
        self.assertFalse(check_password(OLD_PASSWORD, stored))
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())

    def test_password_set_during_login_hash_upgrade_is_kept(self):
        results, _ = self._run_pair(self._set_password, self._login)
        login, _ = results["second"]
        self.assertEqual(login.status_code, 400)
        self._assert_new_password_stored_and_no_token()

    def test_password_set_after_login_hash_upgrade_is_kept(self):
        # The login's hash upgrade commits before it takes the row lock; the
        # login holds the lock while it issues its Token, and the password set
        # waits for it, then replaces the upgraded hash and revokes the Token.
        def read_then_set():
            seen = User.objects.get(pk=self.user.pk).password
            self._set_password()
            return seen

        results, hold_ended = self._run_pair(self._login, read_then_set)
        login, _ = results["first"]
        seen_before_set, set_done = results["second"]
        self.assertEqual(login.status_code, 200)
        self.assertNotEqual(seen_before_set, self.legacy)
        self.assertTrue(check_password(OLD_PASSWORD, seen_before_set))
        self.assertGreaterEqual(set_done, hold_ended)
        self._assert_new_password_stored_and_no_token()


@override_settings(PASSWORD_HASHERS=UPGRADE_HASHERS)
class ConditionalHashUpgradeTests(TestCase):
    """``User.check_password`` upgrades only the hash it verified against (#1601).

    Runs on every backend: the competing write is a plain ``UPDATE`` made
    between reading the instance and checking its password.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="cond", password=OLD_PASSWORD)
        self.legacy = make_password(OLD_PASSWORD, hasher="md5")
        User.objects.filter(pk=self.user.pk).update(password=self.legacy)
        self.user.refresh_from_db()

    def test_upgrade_is_written_when_row_unchanged(self):
        self.assertTrue(self.user.check_password(OLD_PASSWORD))
        stored = User.objects.get(pk=self.user.pk).password
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertEqual(stored, self.user.password)
        self.assertTrue(check_password(OLD_PASSWORD, stored))

    def test_upgrade_is_not_written_when_row_changed(self):
        replacement = make_password(NEW_PASSWORD)
        User.objects.filter(pk=self.user.pk).update(password=replacement)
        self.assertTrue(self.user.check_password(OLD_PASSWORD))
        self.assertEqual(User.objects.get(pk=self.user.pk).password, replacement)
        # The instance keeps the hash it was read with.
        self.assertEqual(self.user.password, self.legacy)

    def test_login_refused_when_row_changed_before_upgrade(self):
        replacement = make_password(NEW_PASSWORD)
        original_check = User.check_password

        def check_after_change(user, raw_password):
            User.objects.filter(pk=user.pk).update(password=replacement)
            return original_check(user, raw_password)

        with patch.object(User, "check_password", check_after_change):
            response = APIClient().post(
                "/api/v1/auth/login/",
                {"username": "cond", "password": OLD_PASSWORD},
                format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Token.objects.filter(user_id=self.user.pk).exists())
        self.assertEqual(User.objects.get(pk=self.user.pk).password, replacement)

    def test_async_upgrade_is_not_written_when_row_changed(self):
        replacement = make_password(NEW_PASSWORD)
        User.objects.filter(pk=self.user.pk).update(password=replacement)
        self.assertTrue(async_to_sync(self.user.acheck_password)(OLD_PASSWORD))
        self.assertEqual(User.objects.get(pk=self.user.pk).password, replacement)
        self.assertEqual(self.user.password, self.legacy)

    def test_async_upgrade_is_written_when_row_unchanged(self):
        self.assertTrue(async_to_sync(self.user.acheck_password)(OLD_PASSWORD))
        stored = User.objects.get(pk=self.user.pk).password
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertEqual(stored, self.user.password)


@skipUnless(connection.vendor == "postgresql", "row locks are only observable on PostgreSQL")
class ResetLinkRedeemedOnceTests(TransactionTestCase):
    """Reset link redemption is decided under the user row lock (#1623).

    Two simultaneous redemptions of one link yield exactly one success. The
    first thread is paused just after it takes the row lock.
    """

    def setUp(self):
        from allauth.account.forms import default_token_generator
        from allauth.account.utils import user_pk_to_url_str

        self.user = User.objects.create_user(username="resetonce", email="once@example.com", password=OLD_PASSWORD)
        self.uid = user_pk_to_url_str(self.user)
        self.token = default_token_generator.make_token(self.user)

    def _rest(self, password):
        def run():
            response = APIClient().post(
                "/api/v1/auth/password/reset/confirm/",
                {"uid": self.uid, "token": self.token, "new_password1": password, "new_password2": password},
                format="json",
            )
            return response.status_code

        return run

    def _html(self, password):
        from django.core.exceptions import PermissionDenied

        from accounts.forms import VisibanResetPasswordKeyForm

        def run():
            user = User.objects.get(pk=self.user.pk)
            form = VisibanResetPasswordKeyForm(
                data={"password1": password, "password2": password}, user=user, temp_key=self.token
            )
            self.assertTrue(form.is_valid(), form.errors)
            try:
                form.save()
            except PermissionDenied:
                return 403
            return 200

        return run

    def _run_pair(self, first, second):
        paused = threading.Event()
        results, errors = {}, []
        original = credentials.lock_user_row

        def hold(*args, **kwargs):
            out = original(*args, **kwargs)
            if threading.current_thread().name == "first" and not paused.is_set():
                paused.set()
                threading.Event().wait(HOLD_SECONDS)
            return out

        def worker(name, fn):
            try:
                results[name] = fn()
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(credentials, "lock_user_row", side_effect=hold):
            t1 = threading.Thread(target=worker, args=("first", first), name="first")
            t1.start()
            self.assertTrue(paused.wait(timeout=15), "first thread never reached the pause")
            t2 = threading.Thread(target=worker, args=("second", second), name="second")
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)
        self.assertEqual(errors, [])
        return results

    def test_rest_simultaneous_redemptions_yield_one_success(self):
        results = self._run_pair(self._rest("FirstPassword111!"), self._rest("SecondPassword222!"))
        self.assertEqual(results["first"], 200)
        self.assertEqual(results["second"], 400)
        stored = User.objects.get(pk=self.user.pk).password
        self.assertTrue(check_password("FirstPassword111!", stored))
        self.assertFalse(check_password("SecondPassword222!", stored))

    def test_html_simultaneous_redemptions_yield_one_success(self):
        results = self._run_pair(self._html("FirstPassword111!"), self._html("SecondPassword222!"))
        self.assertEqual(results["first"], 200)
        self.assertEqual(results["second"], 403)
        stored = User.objects.get(pk=self.user.pk).password
        self.assertTrue(check_password("FirstPassword111!", stored))
        self.assertFalse(check_password("SecondPassword222!", stored))

    def test_rest_and_html_simultaneous_redemptions_yield_one_success(self):
        results = self._run_pair(self._rest("FirstPassword111!"), self._html("SecondPassword222!"))
        self.assertEqual(results["first"], 200)
        self.assertEqual(results["second"], 403)


@override_settings(ROOT_URLCONF="accounts.tests.allauth_reset_urls")
class ResetLinkRecheckTests(TestCase):
    """The reset link is re-validated on the locked row in ``save()`` (#1623); runs on every backend."""

    NEW = "BrandNewPassword77!"

    def setUp(self):
        from allauth.account.forms import default_token_generator
        from allauth.account.utils import user_pk_to_url_str

        self.user = User.objects.create_user(username="recheck", email="re@example.com", password=OLD_PASSWORD)
        self.uid = user_pk_to_url_str(self.user)
        self.token = default_token_generator.make_token(self.user)

    def _change_elsewhere(self):
        u = User.objects.get(pk=self.user.pk)
        u.set_password("ChangedElsewhere99!")
        u.save()
        return u.password

    def _serializer(self):
        from accounts.serializers import VisibanPasswordResetConfirmSerializer

        s = VisibanPasswordResetConfirmSerializer(
            data={"uid": self.uid, "token": self.token, "new_password1": self.NEW, "new_password2": self.NEW},
            context={"request": None},
        )
        self.assertTrue(s.is_valid(), s.errors)
        return s

    def _form(self, user=None):
        from accounts.forms import VisibanResetPasswordKeyForm

        form = VisibanResetPasswordKeyForm(
            data={"password1": self.NEW, "password2": self.NEW},
            user=user or User.objects.get(pk=self.user.pk),
            temp_key=self.token,
        )
        self.assertTrue(form.is_valid(), form.errors)
        return form

    def test_rest_save_refused_when_link_no_longer_valid(self):
        from rest_framework.exceptions import ValidationError

        s = self._serializer()
        stored = self._change_elsewhere()
        with self.assertRaises(ValidationError) as ctx:
            s.save()
        self.assertEqual(ctx.exception.detail, {"token": ["Invalid value"]})
        self.assertEqual(User.objects.get(pk=self.user.pk).password, stored)

    def test_rest_save_refused_when_user_no_longer_exists(self):
        from rest_framework.exceptions import ValidationError

        s = self._serializer()
        User.objects.filter(pk=self.user.pk).delete()
        with self.assertRaises(ValidationError) as ctx:
            s.save()
        self.assertEqual(ctx.exception.detail, {"token": ["Invalid value"]})

    def test_html_save_refused_when_link_no_longer_valid(self):
        from django.core.exceptions import PermissionDenied

        form = self._form()
        stored = self._change_elsewhere()
        with self.assertRaises(PermissionDenied):
            form.save()
        self.assertEqual(User.objects.get(pk=self.user.pk).password, stored)

    def test_html_save_refused_when_user_no_longer_exists(self):
        from django.core.exceptions import PermissionDenied

        form = self._form()
        User.objects.filter(pk=self.user.pk).delete()
        with self.assertRaises(PermissionDenied):
            form.save()

    def test_html_key_page_submit_refused_with_403_when_link_no_longer_valid(self):
        from django.test import Client

        client = Client()
        r = client.get(f"/accounts/password/reset/key/{self.uid}-{self.token}/")
        self.assertEqual(r.status_code, 302)
        original = credentials.lock_user_row

        def change_then_lock(user):
            # Stands in for the link becoming invalid after the page's own checks.
            self._change_elsewhere()
            return original(user)

        client.raise_request_exception = False
        with patch.object(credentials, "lock_user_row", side_effect=change_then_lock):
            r = client.post(r["Location"], {"password1": self.NEW, "password2": self.NEW})
        self.assertEqual(r.status_code, 403)
        # The refusal rolls back the save's transaction, so only the absence
        # of the new password is asserted.
        self.assertFalse(check_password(self.NEW, User.objects.get(pk=self.user.pk).password))

    def test_html_save_keeps_original_instance_in_step_with_stored_row(self):
        original = User.objects.get(pk=self.user.pk)
        self._form(user=original).save()
        self.assertEqual(original.password, User.objects.get(pk=self.user.pk).password)
        self.assertTrue(check_password(self.NEW, original.password))

    def test_rest_save_keeps_original_instance_in_step_with_stored_row(self):
        s = self._serializer()
        original = s.user
        s.save()
        self.assertEqual(original.password, User.objects.get(pk=self.user.pk).password)
