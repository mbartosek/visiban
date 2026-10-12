"""Writes to the user row do not undo a credential change committed since the request started (#1619).

A request authenticates with the user row as it was when the request began. A
password change, reset or deactivation committed by another request after
that must survive whatever this request writes:

- profile updates (``PATCH /api/v1/auth/me/`` and ``/api/v1/auth/user/``)
  write only the fields they change;
- the two REST password-change routes check the current password against,
  and write the new hash to, the row read under ``credentials.lock_user_row``;
- the REST reset-confirm serializer, allauth's password hook
  (``RegistrationAdapter.set_password``) and the Django admin change form
  write only the columns they set;
- a social-account connect (allauth's ``process=connect``) saves only
  ``last_login`` from a fresh read (``SocialRegistrationAdapter.pre_social_login``).

``StaleUserRowTests`` runs on every backend: the request is authenticated
with a copy of the user loaded before the concurrent change, which is what
the request-start instance is. ``StaleUserRowInterleavingTests`` reproduces
the same ordering with real threads on PostgreSQL: the request is paused
after it started, the change is committed on another connection, and the
request then resumes.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.hashers import make_password
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils.encoding import force_str
from rest_framework.test import APIClient

from accounts.admin import VisibanUserChangeForm
from accounts.adapter import RegistrationAdapter
from accounts.models import User
from accounts.permissions import TokenHasScope
from accounts.serializers import VisibanPasswordResetConfirmSerializer

OLD_PASSWORD = "OldPassword123!"
OTHER_PASSWORD = "OtherPassword789!"
NEW_PASSWORD = "NewPassword456!"

PROFILE_ROUTES = ("/api/v1/auth/me/", "/api/v1/auth/user/")


def _commit_concurrent_change(pk, **changes):
    """Write *changes* to the stored row without touching any loaded instance."""
    if "password" in changes:
        changes["password"] = make_password(changes["password"])
    User.objects.filter(pk=pk).update(**changes)


def _change_password(client, route, current):
    if route == "rest":
        return client.post(
            "/api/v1/auth/change-password/",
            {"current_password": current, "new_password": NEW_PASSWORD},
            format="json",
        )
    return client.post(
        "/api/v1/auth/password/change/",
        {"old_password": current, "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        format="json",
    )


class StaleUserRowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="stale_row", email="stale@example.com", password=OLD_PASSWORD
        )

    def _client_for(self, instance):
        client = APIClient()
        client.force_authenticate(instance)
        return client

    def _stale(self):
        return User.objects.get(pk=self.user.pk)

    def _stored(self):
        return User.objects.get(pk=self.user.pk)

    # --- Profile updates -------------------------------------------------

    def test_profile_patch_keeps_concurrent_password_and_deactivation(self):
        for route in PROFILE_ROUTES:
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(
                    password=make_password(OLD_PASSWORD), is_active=True,
                    must_change_password=False, theme="light",
                )
                stale = self._stale()
                _commit_concurrent_change(
                    self.user.pk, password=OTHER_PASSWORD, is_active=False,
                    must_change_password=True,
                )
                response = self._client_for(stale).patch(route, {"theme": "dark"}, format="json")
                self.assertEqual(response.status_code, 200)
                stored = self._stored()
                self.assertEqual(stored.theme, "dark")
                self.assertTrue(stored.check_password(OTHER_PASSWORD))
                self.assertFalse(stored.is_active)
                self.assertTrue(stored.must_change_password)

    def test_profile_patch_writes_only_validated_fields(self):
        stale = self._stale()
        with patch.object(User, "save", autospec=True, side_effect=User.save) as save:
            response = self._client_for(stale).patch(
                "/api/v1/auth/me/", {"first_name": "Ada", "theme": "dark"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(save.call_args.kwargs["update_fields"]), {"first_name", "theme"}
        )
        stored = self._stored()
        self.assertEqual((stored.first_name, stored.theme), ("Ada", "dark"))

    def test_profile_patch_default_board_is_written(self):
        from boards.models import Board, BoardMembership

        board = Board.objects.create(name="Mine", owner=self.user)
        BoardMembership.objects.get_or_create(
            board=board, user=self.user, defaults={"role": BoardMembership.Role.ADMIN}
        )
        response = self._client_for(self._stale()).patch(
            "/api/v1/auth/me/", {"default_board_id": board.pk}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._stored().default_board_id, board.pk)

    def test_profile_patch_rename_still_clears_forced_username_change(self):
        User.objects.filter(pk=self.user.pk).update(must_change_username=True)
        response = self._client_for(self._stale()).patch(
            "/api/v1/auth/me/", {"username": "renamed_row"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        stored = self._stored()
        self.assertEqual(stored.username, "renamed_row")
        self.assertFalse(stored.must_change_username)

    # --- REST password changes -------------------------------------------

    def test_password_change_refuses_current_password_replaced_since_request_start(self):
        for route in ("rest", "dj_rest_auth"):
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(password=make_password(OLD_PASSWORD))
                stale = self._stale()
                _commit_concurrent_change(self.user.pk, password=OTHER_PASSWORD)
                response = _change_password(self._client_for(stale), route, OLD_PASSWORD)
                self.assertEqual(response.status_code, 400)
                if route == "rest":
                    self.assertEqual(response.json(), {"detail": "Current password is incorrect."})
                else:
                    self.assertIn("old_password", response.json())
                self.assertTrue(self._stored().check_password(OTHER_PASSWORD))

    def test_password_change_accepts_the_current_stored_password(self):
        for route in ("rest", "dj_rest_auth"):
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(password=make_password(OLD_PASSWORD))
                stale = self._stale()
                _commit_concurrent_change(self.user.pk, password=OTHER_PASSWORD)
                response = _change_password(self._client_for(stale), route, OTHER_PASSWORD)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(self._stored().check_password(NEW_PASSWORD))

    def test_password_change_keeps_concurrent_deactivation(self):
        for route in ("rest", "dj_rest_auth"):
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(
                    password=make_password(OLD_PASSWORD), is_active=True, is_site_admin=False
                )
                stale = self._stale()
                _commit_concurrent_change(self.user.pk, is_active=False, is_site_admin=True)
                response = _change_password(self._client_for(stale), route, OLD_PASSWORD)
                self.assertEqual(response.status_code, 200)
                stored = self._stored()
                self.assertTrue(stored.check_password(NEW_PASSWORD))
                self.assertFalse(stored.is_active)
                self.assertTrue(stored.is_site_admin)

    def test_password_change_keeps_session_alive(self):
        for route in ("rest", "dj_rest_auth"):
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(password=make_password(OLD_PASSWORD))
                self.client.force_login(self._stale())
                path = (
                    "/api/v1/auth/change-password/" if route == "rest"
                    else "/api/v1/auth/password/change/"
                )
                body = (
                    {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD}
                    if route == "rest"
                    else {"old_password": OLD_PASSWORD, "new_password1": NEW_PASSWORD,
                          "new_password2": NEW_PASSWORD}
                )
                response = self.client.post(path, body, content_type="application/json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.client.get("/api/v1/auth/me/").status_code, 200)
                self.client.logout()

    # --- Other password-setting paths -------------------------------------

    def test_allauth_password_hook_keeps_concurrent_deactivation(self):
        stale = self._stale()
        _commit_concurrent_change(self.user.pk, is_active=False, must_change_username=True)
        RegistrationAdapter().set_password(stale, NEW_PASSWORD)
        stored = self._stored()
        self.assertTrue(stored.check_password(NEW_PASSWORD))
        self.assertFalse(stored.is_active)
        self.assertTrue(stored.must_change_username)

    def test_reset_confirm_keeps_concurrent_deactivation(self):
        from allauth.account.forms import default_token_generator
        from allauth.account.utils import user_pk_to_url_str

        uid = user_pk_to_url_str(self.user)
        token = default_token_generator.make_token(self.user)
        original_validate = VisibanPasswordResetConfirmSerializer.validate

        def validate_then_change(serializer, attrs):
            attrs = original_validate(serializer, attrs)
            # Committed after validate() read the row, before save().
            _commit_concurrent_change(self.user.pk, is_active=False, must_change_username=True)
            return attrs

        with patch.object(VisibanPasswordResetConfirmSerializer, "validate", validate_then_change):
            response = APIClient().post(
                "/api/v1/auth/password/reset/confirm/",
                {"uid": force_str(uid), "token": token,
                 "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
                format="json",
            )
        self.assertEqual(response.status_code, 200, response.content)
        stored = self._stored()
        self.assertTrue(stored.check_password(NEW_PASSWORD))
        self.assertFalse(stored.is_active)
        self.assertTrue(stored.must_change_username)


class SocialConnectStaleRowTests(TestCase):
    """Connecting a provider does not write the request-start user row back (#1619)."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="connector", email="connector@example.com", password=OLD_PASSWORD
        )

    def _connect(self, request_user):
        from allauth.account.models import EmailAddress
        from allauth.socialaccount.adapter import get_adapter
        from allauth.socialaccount.internal.flows.login import complete_login
        from allauth.socialaccount.models import SocialAccount, SocialLogin
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        request = RequestFactory().get("/accounts/github/login/callback/")
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)
        request.user = request_user
        account = SocialAccount(provider="github", uid="gh-1619", extra_data={})
        sociallogin = SocialLogin(
            user=User(username="", email="idp@example.com"),
            account=account,
            email_addresses=[EmailAddress(email="idp@example.com", verified=True, primary=True)],
        )
        sociallogin.provider = get_adapter().get_provider(request, "github")
        sociallogin.state = {"process": "connect"}
        return complete_login(request, sociallogin)

    def test_connect_keeps_concurrent_password_change(self):
        from allauth.socialaccount.models import SocialAccount

        stale = User.objects.get(pk=self.user.pk)
        _commit_concurrent_change(self.user.pk, password=OTHER_PASSWORD)
        self._connect(stale)
        self.assertTrue(
            SocialAccount.objects.filter(user_id=self.user.pk, provider="github").exists()
        )
        self.assertTrue(User.objects.get(pk=self.user.pk).check_password(OTHER_PASSWORD))

    def _assert_connect_refused(self, **change):
        from allauth.socialaccount.models import SocialAccount
        from django.conf import settings as django_settings

        stale = User.objects.get(pk=self.user.pk)
        _commit_concurrent_change(self.user.pk, **change)
        response = self._connect(stale)  # allauth turns the refusal into a redirect
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], django_settings.FRONTEND_URL)
        self.assertFalse(SocialAccount.objects.filter(provider="github").exists())

    def test_connect_refused_when_deactivated_after_request_start(self):
        self._assert_connect_refused(is_active=False)

    def test_connect_refused_when_password_change_forced_after_request_start(self):
        self._assert_connect_refused(must_change_password=True)

    def test_connect_refused_when_username_change_forced_after_request_start(self):
        self._assert_connect_refused(must_change_username=True)

    def test_connect_saves_only_last_login(self):
        from django.db.models.signals import pre_save

        written = []

        def record(sender, instance, update_fields=None, **kwargs):
            if instance.pk == self.user.pk:
                written.append(set(update_fields or ()))

        pre_save.connect(record, sender=User, dispatch_uid="test_1619_connect")
        try:
            response = self._connect(User.objects.get(pk=self.user.pk))
        finally:
            pre_save.disconnect(sender=User, dispatch_uid="test_1619_connect")
        self.assertEqual(response.status_code, 302)
        # Django passes the loaded fields of a deferred instance as update_fields.
        self.assertEqual(written, [{"last_login"}])


class DjangoAdminChangeFormStaleRowTests(TestCase):
    """The admin change form writes only the fields the administrator changed (#1619)."""

    def setUp(self):
        self.root = User.objects.create_superuser(
            username="root_1619", email="root1619@example.com", password="rootpass123!"
        )
        self.client.force_login(self.root)
        self.target = User.objects.create_user(username="target_1619", password=OLD_PASSWORD)

    def _form_data(self, **overrides):
        data = {
            "username": self.target.username,
            "email": self.target.email,
            "first_name": "",
            "last_name": "",
            "is_active": "on",
            "date_joined_0": "2026-01-01",
            "date_joined_1": "00:00:00",
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v != ""}

    def _post_with_change_after_load(self, data, **changes):
        """POST the change form, committing *changes* after the instance loads.

        The form's ``clean`` runs after the admin view loaded the instance and
        before ``save_model`` takes the row lock, which is the window a
        concurrent request can commit in.
        """
        original_clean = VisibanUserChangeForm.clean

        def clean_then_change(form):
            cleaned = original_clean(form)
            _commit_concurrent_change(self.target.pk, **changes)
            return cleaned

        with patch.object(VisibanUserChangeForm, "clean", clean_then_change):
            return self.client.post(
                reverse("admin:accounts_user_change", args=[self.target.pk]), data
            )

    def test_unchanged_fields_keep_values_committed_before_the_save(self):
        response = self._post_with_change_after_load(
            self._form_data(first_name="Ada"),
            password=OTHER_PASSWORD, is_active=False, must_change_password=True,
        )
        self.assertEqual(response.status_code, 302)
        stored = User.objects.get(pk=self.target.pk)
        self.assertEqual(stored.first_name, "Ada")
        self.assertTrue(stored.check_password(OTHER_PASSWORD))
        self.assertFalse(stored.is_active)
        self.assertTrue(stored.must_change_password)

    def test_changed_field_takes_the_submitted_value(self):
        response = self.client.post(
            reverse("admin:accounts_user_change", args=[self.target.pk]),
            self._form_data(is_active=""),
        )
        self.assertEqual(response.status_code, 302)
        stored = User.objects.get(pk=self.target.pk)
        self.assertFalse(stored.is_active)
        self.assertTrue(stored.check_password(OLD_PASSWORD))

    def test_stale_unchanged_active_flag_is_not_refused_as_a_deactivation(self):
        # The form renders the board owner as inactive and the administrator
        # leaves the box alone; another request reactivates the account before
        # the save. The locked-row guards judge the value this save writes
        # (the reactivation is kept), so the board-owner refusal does not fire.
        from boards.models import Board

        Board.objects.create(name="Owned", owner=self.target)
        User.objects.filter(pk=self.target.pk).update(is_active=False)
        response = self._post_with_change_after_load(
            self._form_data(first_name="Ada", is_active=""), is_active=True
        )
        self.assertEqual(response.status_code, 302)
        stored = User.objects.get(pk=self.target.pk)
        self.assertEqual(stored.first_name, "Ada")
        self.assertTrue(stored.is_active)


@skipUnless(connection.vendor == "postgresql", "interleaving needs separate PostgreSQL connections")
class StaleUserRowInterleavingTests(TransactionTestCase):
    """The same orderings with real threads and connections on PostgreSQL."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="interleave", email="interleave@example.com", password=OLD_PASSWORD
        )

    def _run_paused(self, request_fn, concurrent_change):
        """Run *request_fn* in a thread, paused after authentication, while *concurrent_change* commits.

        The request is authenticated with the user loaded before it starts (its
        request-start instance). It stops in its permission check
        (``TokenHasScope.has_permission``, which every route here runs before
        the view body); the main thread commits the concurrent change on its
        own connection; the request then resumes.
        """
        patch_target, attr = TokenHasScope, "has_permission"
        original = getattr(patch_target, attr)
        paused, release = threading.Event(), threading.Event()
        results, errors = {}, []

        def pause_then_call(*args, **kwargs):
            if threading.current_thread().name == "request" and not paused.is_set():
                paused.set()
                release.wait(timeout=15)
            return original(*args, **kwargs)

        def worker():
            try:
                stale = User.objects.get(pk=self.user.pk)
                client = APIClient()
                client.force_authenticate(stale)
                results["response"] = request_fn(client)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(patch_target, attr, pause_then_call):
            thread = threading.Thread(target=worker, name="request")
            thread.start()
            try:
                self.assertTrue(paused.wait(timeout=15), "request never reached the pause")
                concurrent_change()
            finally:
                release.set()
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        return results["response"]

    def _stored(self):
        return User.objects.get(pk=self.user.pk)

    def test_profile_patch_keeps_concurrent_password_and_deactivation(self):
        for route in PROFILE_ROUTES:
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(
                    password=make_password(OLD_PASSWORD), is_active=True, theme="light"
                )
                response = self._run_paused(
                    lambda client: client.patch(route, {"theme": "dark"}, format="json"),
                    lambda: _commit_concurrent_change(
                        self.user.pk, password=OTHER_PASSWORD, is_active=False
                    ),
                )
                self.assertEqual(response.status_code, 200)
                stored = self._stored()
                self.assertEqual(stored.theme, "dark")
                self.assertTrue(stored.check_password(OTHER_PASSWORD))
                self.assertFalse(stored.is_active)

    def test_password_change_refuses_current_password_replaced_mid_request(self):
        for route in ("rest", "dj_rest_auth"):
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(password=make_password(OLD_PASSWORD))
                response = self._run_paused(
                    lambda client: _change_password(client, route, OLD_PASSWORD),
                    lambda: _commit_concurrent_change(self.user.pk, password=OTHER_PASSWORD),
                )
                self.assertEqual(response.status_code, 400)
                self.assertTrue(self._stored().check_password(OTHER_PASSWORD))

    def test_password_change_keeps_deactivation_committed_mid_request(self):
        for route in ("rest", "dj_rest_auth"):
            with self.subTest(route=route):
                User.objects.filter(pk=self.user.pk).update(
                    password=make_password(OLD_PASSWORD), is_active=True
                )
                response = self._run_paused(
                    lambda client: _change_password(client, route, OLD_PASSWORD),
                    lambda: _commit_concurrent_change(self.user.pk, is_active=False),
                )
                self.assertEqual(response.status_code, 200)
                stored = self._stored()
                self.assertTrue(stored.check_password(NEW_PASSWORD))
                self.assertFalse(stored.is_active)
