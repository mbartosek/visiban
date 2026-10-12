"""The Sites admin is superuser-only (#1621)."""
from django.contrib import admin
from django.contrib.auth.models import Permission
from django.contrib.sites.models import Site
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.admin import SuperuserOnlyAdminMixin
from accounts.models import User

PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=PLAIN_STATIC)
class SitesAdminRestrictionTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser(
            username="root_1621", email="root1621@example.com", password="rootpass123!"
        )
        self.staff = User.objects.create_user(
            username="staff_1621", email="staff1621@example.com", password="pw123456!x",
            is_staff=True,
        )
        self.staff.user_permissions.set(Permission.objects.filter(content_type__app_label="sites"))
        self.site = Site.objects.get_current()

    def test_registered_with_mixin(self):
        self.assertIsInstance(admin.site._registry[Site], SuperuserOnlyAdminMixin)

    def test_non_superuser_with_model_perms_is_refused_everywhere(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("admin:sites_site_changelist")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:sites_site_add")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("admin:sites_site_change", args=[self.site.pk])).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("admin:sites_site_delete", args=[self.site.pk]), {"post": "yes"}
            ).status_code,
            403,
        )
        self.assertNotIn("/admin/sites/", self.client.get(reverse("admin:index")).content.decode())

    def test_non_superuser_cannot_change_or_delete_site(self):
        self.client.force_login(self.staff)
        domain = self.site.domain
        self.client.post(
            reverse("admin:sites_site_change", args=[self.site.pk]),
            {"domain": "changed.example.org", "name": "x"},
        )
        self.client.post(reverse("admin:sites_site_delete", args=[self.site.pk]), {"post": "yes"})
        self.site.refresh_from_db()
        self.assertEqual(self.site.domain, domain)

    def test_superuser_keeps_access(self):
        self.client.force_login(self.root)
        self.assertEqual(self.client.get(reverse("admin:sites_site_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:sites_site_add")).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("admin:sites_site_change", args=[self.site.pk])).status_code, 200
        )
        r = self.client.post(
            reverse("admin:sites_site_change", args=[self.site.pk]),
            {"domain": "ok.example.org", "name": "ok"},
        )
        self.assertEqual(r.status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.domain, "ok.example.org")
