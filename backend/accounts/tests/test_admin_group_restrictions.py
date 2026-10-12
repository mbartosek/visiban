"""The Django admin Group page is limited to superusers (#1617)."""
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import User


# Rendered admin pages need a static storage that works without collectstatic.
@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class GroupAdminRestrictionTests(TestCase):
    def setUp(self):
        self.group = Group.objects.create(name="ops_1617")
        self.perm = Permission.objects.get(codename="view_user")
        self.staff = User.objects.create_user(
            username="staff_1617", password="pw123456!x", is_staff=True
        )
        self.staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="auth",
                codename__in=[
                    "view_group", "add_group", "change_group", "delete_group",
                ],
            )
        )
        self.staff.groups.add(self.group)
        self.root = User.objects.create_superuser(
            username="root_1617", email="root1617@example.com", password="rootpass123!"
        )

    def _change_url(self):
        return reverse("admin:auth_group_change", args=[self.group.pk])

    def test_staff_with_group_perms_is_refused_everywhere(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("admin:auth_group_changelist")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:auth_group_add")).status_code, 403)
        self.assertEqual(self.client.get(self._change_url()).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("admin:auth_group_delete", args=[self.group.pk])).status_code,
            403,
        )

    def test_staff_post_cannot_change_own_group_permissions(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._change_url(), {"name": self.group.name, "permissions": [self.perm.pk]}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(self.group.permissions.exists())

    def test_staff_post_cannot_add_or_delete_group(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("admin:auth_group_add"), {"name": "new_1617", "permissions": [self.perm.pk]}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Group.objects.filter(name="new_1617").exists())
        resp = self.client.post(
            reverse("admin:auth_group_delete", args=[self.group.pk]), {"post": "yes"}
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Group.objects.filter(pk=self.group.pk).exists())

    def test_staff_app_index_hides_group_model(self):
        self.client.force_login(self.staff)
        index = self.client.get(reverse("admin:index"))
        self.assertNotContains(index, reverse("admin:auth_group_changelist"))

    def test_superuser_can_list_add_change_delete(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("admin:auth_group_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:auth_group_add")).status_code, 200)
        self.assertEqual(self.client.get(self._change_url()).status_code, 200)
        self.assertContains(
            self.client.get(reverse("admin:index")), reverse("admin:auth_group_changelist")
        )

        resp = self.client.post(
            self._change_url(), {"name": self.group.name, "permissions": [self.perm.pk]}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(list(self.group.permissions.all()), [self.perm])

        resp = self.client.post(
            reverse("admin:auth_group_add"), {"name": "new_1617", "permissions": []}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(Group.objects.filter(name="new_1617").exists())

        resp = self.client.post(
            reverse("admin:auth_group_delete", args=[self.group.pk]), {"post": "yes"}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Group.objects.filter(pk=self.group.pk).exists())
