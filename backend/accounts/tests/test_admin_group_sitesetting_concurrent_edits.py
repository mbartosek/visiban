"""Django admin group and site-settings forms: unchanged fields and the rendered-state check.

- ``SuperuserOnlyGroupAdmin`` and ``SiteSettingAdmin`` write only the fields
  the administrator changed; a field left alone keeps the value stored when
  the save runs (the group's ``permissions`` set included).
- Both change forms render a hidden ``row_version`` and refuse a submission
  when a field the form writes was changed after the page was rendered: with
  a form error when the change is visible when the submission arrives, and
  with a redirect and a message when it is found under the row lock.
- The shared helpers keep the user change form's digest key.

A change committed after the request loaded the row is simulated by
committing it from the form's ``clean``, the step between the load and
``save_model``'s row lock. ``*InterleavingTests`` reproduce that ordering with
real threads and connections on PostgreSQL.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django import forms
from django.contrib.auth.models import Group, Permission
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from accounts.admin import GroupAdminForm, SiteSettingAdmin, SiteSettingAdminForm
from accounts.models import AdminActionLog, SiteSetting, User
from visiban.admin_row_version import default_row_version_salt

PW = "pw123456!x"

_PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


def page_data(response, *, row_version=True, **overrides):
    """POST data for the change form rendered in *response*, as a browser submits it."""
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        if field.disabled or (name == "row_version" and not row_version):
            continue
        value = form[name].value()
        if isinstance(field, forms.BooleanField):
            if value:
                data[name] = "on"
        elif isinstance(value, (list, tuple)):
            data[name] = [str(v) for v in value]
        elif value is not None:
            data[name] = str(value)
    for name, value in overrides.items():
        if value is False:
            data.pop(name, None)
        elif value is True:
            data[name] = "on"
        else:
            data[name] = value
    return data


def patched_clean(form_class, change):
    """Run *change* after *form_class*'s clean: after the row is loaded, before the lock."""
    original = form_class.clean

    def clean_then_change(form):
        cleaned = original(form)
        change()
        return cleaned

    return patch.object(form_class, "clean", clean_then_change)


class _Fixtures:
    def make_fixtures(self):
        self.root = User.objects.create_superuser(
            username="root_rv", email="rootrv@example.com", password=PW
        )
        self.group = Group.objects.create(name="g_rv")
        self.perm_a, self.perm_b = Permission.objects.filter(
            content_type__app_label="accounts", content_type__model="user"
        ).order_by("pk")[:2]
        self.group.permissions.add(self.perm_a)
        self.setting = SiteSetting.get()

    def group_url(self):
        return reverse("admin:auth_group_change", args=[self.group.pk])

    def setting_url(self):
        return reverse("admin:accounts_sitesetting_change", args=[self.setting.pk])

    def perm_ids(self):
        return set(Group.objects.get(pk=self.group.pk).permissions.values_list("pk", flat=True))

    def stored_setting(self):
        return SiteSetting.objects.get(pk=self.setting.pk)

    def render(self, client, url):
        response = client.get(url)
        assert response.status_code == 200, response.status_code
        return response


@_PLAIN_STATIC
class SharedHelperTests(TestCase):
    def test_user_form_digest_key_is_unchanged(self):
        self.assertEqual(default_row_version_salt(User), "accounts.admin.user_row_version")

    def test_each_model_has_its_own_digest_key(self):
        self.assertEqual(default_row_version_salt(Group), "auth.admin.group_row_version")
        self.assertEqual(
            default_row_version_salt(SiteSetting), "accounts.admin.sitesetting_row_version"
        )


@_PLAIN_STATIC
class GroupAdminConcurrentEditTests(_Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.root)

    def test_change_page_renders_row_version(self):
        response = self.render(self.client, self.group_url())
        self.assertContains(response, 'type="hidden" name="row_version"')

    def test_add_page_has_no_row_version(self):
        response = self.client.get(reverse("admin:auth_group_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="row_version"')

    def test_matching_row_version_saves(self):
        data = page_data(
            self.render(self.client, self.group_url()),
            name="renamed_rv", permissions=[str(self.perm_b.pk)],
        )
        response = self.client.post(self.group_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "renamed_rv")
        self.assertEqual(self.perm_ids(), {self.perm_b.pk})

    def test_unchanged_permissions_keep_a_grant_made_after_load(self):
        data = page_data(
            self.render(self.client, self.group_url()), row_version=False, name="renamed_rv"
        )
        with patched_clean(GroupAdminForm, lambda: self.group.permissions.add(self.perm_b)):
            response = self.client.post(self.group_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "renamed_rv")
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    def test_unchanged_permissions_keep_a_revocation_made_after_load(self):
        data = page_data(
            self.render(self.client, self.group_url()), row_version=False, name="renamed_rv"
        )
        with patched_clean(GroupAdminForm, lambda: self.group.permissions.remove(self.perm_a)):
            response = self.client.post(self.group_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.perm_ids(), set())

    def test_unchanged_name_keeps_a_rename_made_after_load(self):
        data = page_data(
            self.render(self.client, self.group_url()),
            row_version=False, permissions=[str(self.perm_b.pk)],
        )
        rename = lambda: Group.objects.filter(pk=self.group.pk).update(name="other_rv")  # noqa: E731
        with patched_clean(GroupAdminForm, rename):
            response = self.client.post(self.group_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "other_rv")
        self.assertEqual(self.perm_ids(), {self.perm_b.pk})

    def _assert_refused_after(self, change):
        data = page_data(self.render(self.client, self.group_url()), name="renamed_rv")
        change()
        before_name, before_perms = Group.objects.get(pk=self.group.pk).name, self.perm_ids()
        response = self.client.post(self.group_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, GroupAdminForm.stale_form_message)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, before_name)
        self.assertEqual(self.perm_ids(), before_perms)

    def test_permission_granted_after_render_refuses_the_save(self):
        self._assert_refused_after(lambda: self.group.permissions.add(self.perm_b))

    def test_permission_removed_after_render_refuses_the_save(self):
        self._assert_refused_after(lambda: self.group.permissions.remove(self.perm_a))

    def test_rename_after_render_refuses_the_save(self):
        self._assert_refused_after(
            lambda: Group.objects.filter(pk=self.group.pk).update(name="other_rv")
        )

    def _assert_refused_under_lock(self, response):
        from accounts.admin import SuperuserOnlyGroupAdmin

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.group_url())
        page = self.client.get(response["Location"])
        self.assertContains(page, SuperuserOnlyGroupAdmin.stale_save_message)

    def test_permission_granted_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(self.client, self.group_url()), name="renamed_rv")
        with patched_clean(GroupAdminForm, lambda: self.group.permissions.add(self.perm_b)):
            response = self.client.post(self.group_url(), data)
        self._assert_refused_under_lock(response)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "g_rv")

    def test_permission_change_before_save_related_rolls_the_save_back(self):
        from accounts.admin import SuperuserOnlyGroupAdmin

        data = page_data(self.render(self.client, self.group_url()), name="renamed_rv")
        original = SuperuserOnlyGroupAdmin.save_model

        def save_then_change(admin_self, request, obj, form, change):
            original(admin_self, request, obj, form, change)
            self.group.permissions.add(self.perm_b)

        with patch.object(SuperuserOnlyGroupAdmin, "save_model", save_then_change):
            response = self.client.post(self.group_url(), data)
        self._assert_refused_under_lock(response)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "g_rv")
        self.assertEqual(self.perm_ids(), {self.perm_a.pk})

    def test_omitted_row_version_saves_the_changed_field(self):
        data = page_data(
            self.render(self.client, self.group_url()),
            row_version=False, permissions=[str(self.perm_b.pk)],
        )
        response = self.client.post(self.group_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.perm_ids(), {self.perm_b.pk})


@_PLAIN_STATIC
class SiteSettingAdminConcurrentEditTests(_Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.root)

    def test_change_page_renders_row_version(self):
        response = self.render(self.client, self.setting_url())
        self.assertContains(response, 'type="hidden" name="row_version"')

    def test_matching_row_version_saves_and_is_audited(self):
        data = page_data(self.render(self.client, self.setting_url()), maintenance_mode=True)
        response = self.client.post(self.setting_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.stored_setting().maintenance_mode)
        self.assertEqual(
            list(AdminActionLog.objects.values_list("action", flat=True)),
            [AdminActionLog.Action.MAINTENANCE_MODE_ENABLED],
        )

    def test_unchanged_setting_keeps_a_change_made_after_load(self):
        data = page_data(
            self.render(self.client, self.setting_url()),
            row_version=False, maintenance_mode=True,
        )
        close = lambda: SiteSetting.objects.filter(pk=self.setting.pk).update(  # noqa: E731
            registration_mode=SiteSetting.RegistrationMode.CLOSED, uploads_enabled=False
        )
        with patched_clean(SiteSettingAdminForm, close):
            response = self.client.post(self.setting_url(), data)
        self.assertEqual(response.status_code, 302)
        stored = self.stored_setting()
        self.assertTrue(stored.maintenance_mode)
        self.assertEqual(stored.registration_mode, SiteSetting.RegistrationMode.CLOSED)
        self.assertFalse(stored.uploads_enabled)
        # The audit diff describes only the change this save made.
        self.assertEqual(
            list(AdminActionLog.objects.values_list("action", flat=True)),
            [AdminActionLog.Action.MAINTENANCE_MODE_ENABLED],
        )

    def test_change_after_render_refuses_the_save(self):
        data = page_data(self.render(self.client, self.setting_url()), maintenance_mode=True)
        SiteSetting.objects.filter(pk=self.setting.pk).update(uploads_enabled=False)
        response = self.client.post(self.setting_url(), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, SiteSettingAdminForm.stale_form_message)
        self.assertFalse(self.stored_setting().maintenance_mode)
        self.assertFalse(AdminActionLog.objects.exists())

    def test_change_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(self.client, self.setting_url()), maintenance_mode=True)
        disable_uploads = lambda: SiteSetting.objects.filter(pk=self.setting.pk).update(  # noqa: E731
            uploads_enabled=False
        )
        with patched_clean(SiteSettingAdminForm, disable_uploads):
            response = self.client.post(self.setting_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.setting_url())
        page = self.client.get(response["Location"])
        self.assertContains(page, SiteSettingAdmin.stale_save_message)
        self.assertFalse(self.stored_setting().maintenance_mode)
        self.assertFalse(AdminActionLog.objects.exists())

    def test_omitted_row_version_saves_the_changed_setting(self):
        data = page_data(
            self.render(self.client, self.setting_url()),
            row_version=False, maintenance_mode=True,
        )
        response = self.client.post(self.setting_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.stored_setting().maintenance_mode)


class _Paused:
    """POST in a thread paused after the form's ``clean`` while a change commits."""

    def post_paused(self, form_class, url, data, concurrent_change):
        original = form_class.clean
        paused, release = threading.Event(), threading.Event()
        results, errors = {}, []

        def clean_then_pause(form):
            cleaned = original(form)
            if threading.current_thread().name == "request" and not paused.is_set():
                paused.set()
                release.wait(timeout=15)
            return cleaned

        def worker():
            try:
                client = Client()
                client.force_login(User.objects.get(pk=self.root.pk))
                results["response"] = client.post(url, data)
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(form_class, "clean", clean_then_pause):
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

    def root_client(self):
        client = Client()
        client.force_login(User.objects.get(pk=self.root.pk))
        return client


@skipUnless(connection.vendor == "postgresql", "interleaving needs separate PostgreSQL connections")
@_PLAIN_STATIC
class GroupAndSiteSettingInterleavingTests(_Fixtures, _Paused, TransactionTestCase):
    def setUp(self):
        self.make_fixtures()

    def test_unchanged_permissions_keep_a_grant_committed_mid_request(self):
        data = page_data(
            self.render(self.root_client(), self.group_url()),
            row_version=False, name="renamed_rv",
        )
        response = self.post_paused(
            GroupAdminForm, self.group_url(), data,
            lambda: self.group.permissions.add(self.perm_b),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "renamed_rv")
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    def test_permission_grant_committed_mid_request_is_refused(self):
        data = page_data(self.render(self.root_client(), self.group_url()), name="renamed_rv")
        response = self.post_paused(
            GroupAdminForm, self.group_url(), data,
            lambda: self.group.permissions.add(self.perm_b),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.group_url())
        self.assertEqual(Group.objects.get(pk=self.group.pk).name, "g_rv")
        self.assertEqual(self.perm_ids(), {self.perm_a.pk, self.perm_b.pk})

    def test_setting_change_committed_mid_request_is_kept_and_refuses_the_save(self):
        data = page_data(self.render(self.root_client(), self.setting_url()), maintenance_mode=True)
        response = self.post_paused(
            SiteSettingAdminForm, self.setting_url(), data,
            lambda: SiteSetting.objects.filter(pk=self.setting.pk).update(uploads_enabled=False),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.setting_url())
        stored = self.stored_setting()
        self.assertFalse(stored.uploads_enabled)
        self.assertFalse(stored.maintenance_mode)
        self.assertFalse(AdminActionLog.objects.exists())

    def test_unchanged_setting_keeps_a_change_committed_mid_request(self):
        data = page_data(
            self.render(self.root_client(), self.setting_url()),
            row_version=False, maintenance_mode=True,
        )
        response = self.post_paused(
            SiteSettingAdminForm, self.setting_url(), data,
            lambda: SiteSetting.objects.filter(pk=self.setting.pk).update(uploads_enabled=False),
        )
        self.assertEqual(response.status_code, 302)
        stored = self.stored_setting()
        self.assertTrue(stored.maintenance_mode)
        self.assertFalse(stored.uploads_enabled)
