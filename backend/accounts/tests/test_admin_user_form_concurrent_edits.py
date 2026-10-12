"""Django admin user form: many-to-many saves and the rendered-state check (#1622).

- ``VisibanUserAdmin.save_related`` writes only the many-to-many fields
  (``groups``, ``user_permissions``) the administrator changed; a field left
  alone keeps the value stored when the save runs.
- ``VisibanUserChangeForm`` renders a hidden ``row_version`` and refuses a
  submission when a field the form shows was changed after the page was
  rendered, with a form error rather than a server error.

``AdminUserFormConcurrentEditTests`` runs on every backend: a change committed
after the request loaded the row is simulated by committing it from the
form's ``clean``, the step between the load and ``save_model``'s row lock.
``AdminUserFormInterleavingTests`` reproduces that ordering with real threads
and connections on PostgreSQL.
"""

import re
import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from django.contrib.admin.models import LogEntry

from accounts.admin import (
    STALE_FORM_MESSAGE,
    STALE_SAVE_MESSAGE,
    VisibanUserAdmin,
    VisibanUserChangeForm,
)
from accounts.models import User
from accounts.tests.test_admin_user_form_guards import _staff

PW = "pw123456!x"

_PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)

_ROW_VERSION_RE = re.compile(r'name="row_version" value="([0-9a-f]+)"')


def _form_data(user, **overrides):
    data = {
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_active": "on" if user.is_active else "",
        "is_staff": "on" if user.is_staff else "",
        "is_superuser": "on" if user.is_superuser else "",
        "date_joined_0": "2026-01-01",
        "date_joined_1": "00:00:00",
        "groups": [str(pk) for pk in user.groups.values_list("pk", flat=True)],
        "user_permissions": [
            str(pk) for pk in user.user_permissions.values_list("pk", flat=True)
        ],
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v not in ("", [])}


class _Fixtures:
    def make_fixtures(self):
        self.root = User.objects.create_superuser(
            username="root_1622", email="root1622@example.com", password=PW
        )
        self.target = User.objects.create_user(
            username="target_1622", email="target1622@example.com", password=PW
        )
        self.group_a = Group.objects.create(name="g_a_1622")
        self.group_b = Group.objects.create(name="g_b_1622")
        self.perm_a, self.perm_b = Permission.objects.filter(
            content_type__app_label="accounts", content_type__model="user"
        ).order_by("pk")[:2]
        self.target.groups.add(self.group_a)
        self.target.user_permissions.add(self.perm_a)

    def change_url(self):
        return reverse("admin:accounts_user_change", args=[self.target.pk])

    def render_version(self, client):
        """GET the change page with *client*; return its ``row_version``."""
        response = client.get(self.change_url())
        assert response.status_code == 200, response.status_code
        match = _ROW_VERSION_RE.search(response.content.decode())
        assert match is not None, "change form does not render row_version"
        return match.group(1)

    def assert_refused_under_lock(self, response, client):
        """A refusal under the lock redirects back to the change page with a message."""
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.change_url())
        page = client.get(response["Location"])
        self.assertContains(page, STALE_SAVE_MESSAGE)

    def stored(self):
        return User.objects.get(pk=self.target.pk)

    def group_ids(self):
        return set(self.stored().groups.values_list("pk", flat=True))

    def perm_ids(self):
        return set(self.stored().user_permissions.values_list("pk", flat=True))


@_PLAIN_STATIC
class AdminUserFormConcurrentEditTests(_Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.root)

    def render(self):
        """GET the change page; return its ``row_version``."""
        return self.render_version(self.client)

    def post_with_change_after_load(self, data, change):
        """POST *data*, running *change* after the row is loaded, before the lock."""
        original_clean = VisibanUserChangeForm.clean

        def clean_then_change(form):
            cleaned = original_clean(form)
            change()
            return cleaned

        with patch.object(VisibanUserChangeForm, "clean", clean_then_change):
            return self.client.post(self.change_url(), data)

    # -- many-to-many fields left alone keep their stored values ----------------

    def test_unchanged_groups_keep_a_group_granted_after_load(self):
        data = _form_data(self.target, first_name="Ada")
        response = self.post_with_change_after_load(
            data, lambda: self.target.groups.add(self.group_b)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored().first_name, "Ada")
        self.assertEqual(self.group_ids(), {self.group_a.pk, self.group_b.pk})

    def test_unchanged_groups_keep_a_group_removed_after_load(self):
        data = _form_data(self.target, first_name="Ada")
        response = self.post_with_change_after_load(
            data, lambda: self.target.groups.remove(self.group_a)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.group_ids(), set())

    def test_unchanged_permissions_keep_a_permission_granted_after_load(self):
        data = _form_data(self.target, first_name="Ada")
        response = self.post_with_change_after_load(
            data, lambda: self.target.user_permissions.add(self.perm_b)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    def test_unchanged_permissions_keep_a_permission_removed_after_load(self):
        data = _form_data(self.target, first_name="Ada")
        response = self.post_with_change_after_load(
            data, lambda: self.target.user_permissions.remove(self.perm_a)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.perm_ids(), set())

    def test_changed_groups_take_the_submitted_value(self):
        response = self.client.post(
            self.change_url(), _form_data(self.target, groups=[str(self.group_b.pk)])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.group_ids(), {self.group_b.pk})
        self.assertEqual(self.perm_ids(), {self.perm_a.pk})

    def test_changed_permissions_take_the_submitted_value(self):
        response = self.client.post(
            self.change_url(), _form_data(self.target, user_permissions=[str(self.perm_b.pk)])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.perm_ids(), {self.perm_b.pk})
        self.assertEqual(self.group_ids(), {self.group_a.pk})

    def test_changed_groups_leave_unchanged_permissions_alone(self):
        response = self.post_with_change_after_load(
            _form_data(self.target, groups=[]),
            lambda: self.target.user_permissions.add(self.perm_b),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.group_ids(), set())
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    # -- row_version: changes made between rendering and submitting -------------

    def test_change_page_renders_row_version(self):
        self.render()

    def test_add_page_has_no_row_version(self):
        response = self.client.get(reverse("admin:accounts_user_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('name="row_version"', response.content.decode())

    def test_matching_row_version_saves(self):
        version = self.render()
        response = self.client.post(
            self.change_url(), _form_data(self.target, first_name="Ada", row_version=version)
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored().first_name, "Ada")

    def _assert_refused_after(self, change, **edits):
        version = self.render()
        data = _form_data(self.target, row_version=version, **edits)
        change()
        before = self.stored()
        before_groups, before_perms = self.group_ids(), self.perm_ids()
        response = self.client.post(self.change_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, STALE_FORM_MESSAGE)
        after = self.stored()
        self.assertEqual(after.first_name, before.first_name)
        self.assertEqual(after.email, before.email)
        self.assertEqual(after.is_active, before.is_active)
        self.assertEqual(self.group_ids(), before_groups)
        self.assertEqual(self.perm_ids(), before_perms)

    def test_group_granted_after_render_refuses_the_save(self):
        self._assert_refused_after(
            lambda: self.target.groups.add(self.group_b), first_name="Ada"
        )

    def test_group_removed_after_render_refuses_the_save(self):
        self._assert_refused_after(
            lambda: self.target.groups.remove(self.group_a), first_name="Ada"
        )

    def test_permission_granted_after_render_refuses_the_save(self):
        self._assert_refused_after(
            lambda: self.target.user_permissions.add(self.perm_b), first_name="Ada"
        )

    def test_permission_removed_after_render_refuses_the_save(self):
        self._assert_refused_after(
            lambda: self.target.user_permissions.remove(self.perm_a), first_name="Ada"
        )

    def test_concrete_field_changed_after_render_refuses_the_save(self):
        self._assert_refused_after(
            lambda: User.objects.filter(pk=self.target.pk).update(is_active=False),
            first_name="Ada",
        )

    def test_change_to_a_field_the_form_does_not_write_is_not_a_conflict(self):
        # The password is shown read-only and never written by this form, and
        # must_change_password is not on the form; both keep the stored value.
        version = self.render()
        User.objects.filter(pk=self.target.pk).update(must_change_password=True)
        self.target.set_password("Another-pw-1622")
        self.target.save(update_fields=["password"])
        response = self.client.post(
            self.change_url(), _form_data(self.target, first_name="Ada", row_version=version)
        )
        self.assertEqual(response.status_code, 302)
        stored = self.stored()
        self.assertEqual(stored.first_name, "Ada")
        self.assertTrue(stored.must_change_password)
        self.assertTrue(stored.check_password("Another-pw-1622"))

    def test_row_version_does_not_contain_stored_values(self):
        version = self.render()
        self.assertEqual(len(version), 40)  # salted_hmac's SHA-1 hex digest
        for value in (self.target.username, self.target.email, self.target.password):
            self.assertNotIn(value, version)

    # -- row_version re-checked under the row lock ------------------------------

    def test_changed_field_also_changed_after_load_is_refused_under_the_lock(self):
        version = self.render()
        response = self.post_with_change_after_load(
            _form_data(self.target, first_name="Ada", row_version=version),
            lambda: User.objects.filter(pk=self.target.pk).update(first_name="Grace"),
        )
        self.assert_refused_under_lock(response, self.client)
        # On one connection the simulated concurrent write shares the request's
        # transaction and is rolled back with it; the PostgreSQL case below
        # checks that a concurrent write is kept.
        self.assertNotEqual(self.stored().first_name, "Ada")

    def test_group_granted_after_load_is_refused_under_the_lock(self):
        version = self.render()
        response = self.post_with_change_after_load(
            _form_data(self.target, first_name="Ada", row_version=version),
            lambda: self.target.groups.add(self.group_b),
        )
        self.assert_refused_under_lock(response, self.client)
        self.assertEqual(self.stored().first_name, "")
        self.assertIn(self.group_a.pk, self.group_ids())

    def test_permission_change_before_save_related_rolls_the_save_back(self):
        version = self.render()
        original = VisibanUserAdmin.save_model

        def save_then_change(admin_self, request, obj, form, change):
            original(admin_self, request, obj, form, change)
            self.target.user_permissions.add(self.perm_b)

        with patch.object(VisibanUserAdmin, "save_model", save_then_change):
            response = self.client.post(
                self.change_url(),
                _form_data(self.target, first_name="Ada", groups=[], row_version=version),
            )
        self.assert_refused_under_lock(response, self.client)
        # The concrete field and the group change written before the refusal
        # are rolled back with the rest of the save.
        self.assertEqual(self.stored().first_name, "")
        self.assertEqual(self.group_ids(), {self.group_a.pk})

    def test_omitted_row_version_is_not_in_the_history_message(self):
        response = self.client.post(self.change_url(), _form_data(self.target, first_name="Ada"))
        self.assertEqual(response.status_code, 302)
        entry = LogEntry.objects.filter(object_id=str(self.target.pk)).latest("action_time")
        self.assertIn("First name", entry.get_change_message())
        self.assertNotIn("Row version", entry.get_change_message())
        self.assertNotIn("row_version", entry.change_message)


@_PLAIN_STATIC
class NonSuperuserRowVersionTests(_Fixtures, TestCase):
    """Non-superuser staff: groups and permissions are outside the form and its digest."""

    def setUp(self):
        self.make_fixtures()
        self.staff = _staff("staff_1622")
        self.client.force_login(self.staff)

    def test_groups_and_permissions_are_not_in_the_form_or_the_digest(self):
        response = self.client.get(self.change_url())
        self.assertEqual(response.status_code, 200)
        form = response.context["adminform"].form
        self.assertNotIn("groups", form.fields)
        self.assertNotIn("user_permissions", form.fields)
        versioned = form._versioned_fields()
        self.assertNotIn("groups", versioned)
        self.assertNotIn("user_permissions", versioned)
        self.assertIn("first_name", versioned)

    def test_save_keeps_a_group_granted_after_render(self):
        version = self.render_version(self.client)
        self.target.groups.add(self.group_b)
        self.target.user_permissions.add(self.perm_b)
        response = self.client.post(
            self.change_url(),
            {"username": self.target.username, "email": self.target.email,
             "first_name": "Ada", "is_active": "on",
             "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
             "row_version": version},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:accounts_user_changelist"))
        self.assertEqual(self.stored().first_name, "Ada")
        self.assertEqual(self.group_ids(), {self.group_a.pk, self.group_b.pk})
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    def test_change_permission_page_has_the_hidden_row_version(self):
        response = self.client.get(self.change_url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('type="hidden" name="row_version"', html)
        # Django puts a hidden field's row in a "hidden" form-row, so its label
        # is not shown.
        self.assertIn('class="form-row hidden field-row_version"', html)

    def test_view_only_page_has_no_row_version(self):
        viewer = _staff("viewer_1622")
        viewer.user_permissions.remove(
            Permission.objects.get(content_type__app_label="accounts", codename="change_user")
        )
        self.client.force_login(User.objects.get(pk=viewer.pk))
        response = self.client.get(self.change_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Row version")
        self.assertNotContains(response, "row_version")

    def test_submitted_privilege_keys_are_ignored_with_and_without_row_version(self):
        for with_version in (True, False):
            with self.subTest(row_version=with_version):
                data = {
                    "username": self.target.username, "email": self.target.email,
                    "first_name": "Ada", "is_active": "on",
                    "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
                    "is_staff": "on", "is_superuser": "on",
                    "groups": [str(self.group_b.pk)],
                    "user_permissions": [str(self.perm_b.pk)],
                }
                if with_version:
                    data["row_version"] = self.render_version(self.client)
                response = self.client.post(self.change_url(), data)
                self.assertEqual(response.status_code, 302)
                stored = self.stored()
                self.assertFalse(stored.is_staff)
                self.assertFalse(stored.is_superuser)
                self.assertEqual(self.group_ids(), {self.group_a.pk})
                self.assertEqual(self.perm_ids(), {self.perm_a.pk})


@skipUnless(connection.vendor == "postgresql", "interleaving needs separate PostgreSQL connections")
@_PLAIN_STATIC
class AdminUserFormInterleavingTests(_Fixtures, TransactionTestCase):
    """The load-to-lock ordering with real threads and connections on PostgreSQL."""

    def setUp(self):
        self.make_fixtures()

    def _post_paused(self, data, concurrent_change):
        """POST *data* in a thread, paused in the form's ``clean``, while *concurrent_change* commits."""
        original = VisibanUserChangeForm.clean
        paused, release = threading.Event(), threading.Event()
        results, errors = {}, []

        def pause_then_clean(form):
            if threading.current_thread().name == "request" and not paused.is_set():
                paused.set()
                release.wait(timeout=15)
            return original(form)

        def worker():
            try:
                client = Client()
                client.force_login(User.objects.get(pk=self.root.pk))
                results["response"] = client.post(self.change_url(), data)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(VisibanUserChangeForm, "clean", pause_then_clean):
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

    def test_unchanged_groups_and_permissions_keep_grants_committed_mid_request(self):
        data = _form_data(self.target, first_name="Ada")

        def grant():
            self.target.groups.add(self.group_b)
            self.target.user_permissions.add(self.perm_b)

        response = self._post_paused(data, grant)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored().first_name, "Ada")
        self.assertEqual(self.group_ids(), {self.group_a.pk, self.group_b.pk})
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    def test_unchanged_groups_and_permissions_keep_revocations_committed_mid_request(self):
        data = _form_data(self.target, first_name="Ada")

        def revoke():
            self.target.groups.remove(self.group_a)
            self.target.user_permissions.remove(self.perm_a)

        response = self._post_paused(data, revoke)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.group_ids(), set())
        self.assertEqual(self.perm_ids(), set())

    def _root_client(self):
        client = Client()
        client.force_login(User.objects.get(pk=self.root.pk))
        return client

    def test_changed_field_also_changed_mid_request_is_refused(self):
        client = self._root_client()
        version = self.render_version(client)
        response = self._post_paused(
            _form_data(self.target, first_name="Ada", row_version=version),
            lambda: User.objects.filter(pk=self.target.pk).update(first_name="Grace"),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.change_url())
        self.assertEqual(self.stored().first_name, "Grace")

    def test_group_changed_mid_request_is_refused(self):
        client = self._root_client()
        version = self.render_version(client)
        response = self._post_paused(
            _form_data(self.target, first_name="Ada", row_version=version),
            lambda: self.target.groups.add(self.group_b),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.change_url())
        self.assertEqual(self.stored().first_name, "")
        self.assertEqual(self.group_ids(), {self.group_a.pk, self.group_b.pk})

    def test_two_administrators_submitting_the_same_rendered_state(self):
        # Both render the same state; the first save commits while the second
        # is paused before its lock, so the second is refused.
        client = self._root_client()
        version = self.render_version(client)

        def first_save():
            response = client.post(
                self.change_url(),
                _form_data(self.target, first_name="Grace", row_version=version),
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], reverse("admin:accounts_user_changelist"))

        response = self._post_paused(
            _form_data(self.target, first_name="Ada", row_version=version), first_save
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.change_url())
        self.assertEqual(self.stored().first_name, "Grace")
