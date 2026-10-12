"""Admins for emails, login identities and API tokens are superuser-only (#1618)."""
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token, TokenProxy

from django.contrib import admin

from accounts.admin import SuperuserOnlyAdminMixin
from accounts.models import User

PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

MODELS = (EmailAddress, SocialAccount, SocialToken, SocialApp, TokenProxy)


def _name(model):
    return f"{model._meta.app_label}_{model._meta.model_name}"


@override_settings(STORAGES=PLAIN_STATIC)
class ThirdPartyAdminRestrictionTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser(
            username="root_1618", email="root1618@example.com", password="rootpass123!"
        )
        self.staff = User.objects.create_user(
            username="staff_1618", email="staff1618@example.com", password="pw123456!x",
            is_staff=True,
        )
        perms = Permission.objects.filter(
            content_type__app_label__in={m._meta.app_label for m in MODELS},
            content_type__model__in={m._meta.model_name for m in MODELS},
        )
        self.staff.user_permissions.set(perms)
        self.victim = User.objects.create_user(
            username="victim_1618", email="victim1618@example.com", password="pw123456!x"
        )
        self.app = SocialApp.objects.create(provider="github", name="gh", client_id="x", secret="y")
        self.sa = SocialAccount.objects.create(user=self.victim, provider="github", uid="1")
        self.stok = SocialToken.objects.create(app=self.app, account=self.sa, token="t")
        self.email = EmailAddress.objects.create(user=self.victim, email="v@example.com")
        self.token = Token.objects.create(user=self.victim)

    def _change_pk(self, model):
        return {
            EmailAddress: self.email.pk, SocialAccount: self.sa.pk,
            SocialToken: self.stok.pk, SocialApp: self.app.pk, TokenProxy: TokenProxy.objects.get(key=self.token.key).pk,
        }[model]

    def _denied(self, response):
        self.assertIn(response.status_code, (302, 403, 404), response.status_code)
        if response.status_code == 302:
            self.assertIn("login", response["Location"])

    def test_each_admin_is_registered_with_the_superuser_only_mixin(self):
        for model in (EmailAddress, SocialAccount, SocialToken, SocialApp, TokenProxy):
            with self.subTest(model=model.__name__):
                self.assertIsInstance(
                    admin.site._registry[model], SuperuserOnlyAdminMixin
                )

    def test_non_superuser_with_model_perms_is_refused_everywhere(self):
        self.client.force_login(self.staff)
        for model in MODELS:
            n = _name(model)
            with self.subTest(model=n):
                pk = self._change_pk(model)
                self.assertEqual(self.client.get(reverse(f"admin:{n}_changelist")).status_code, 403)
                self.assertEqual(self.client.get(reverse(f"admin:{n}_add")).status_code, 403)
                self.assertEqual(
                    self.client.get(reverse(f"admin:{n}_change", args=[pk])).status_code, 403
                )
                self.assertEqual(
                    self.client.post(reverse(f"admin:{n}_delete", args=[pk]), {"post": "yes"}).status_code,
                    403,
                )
        self.assertNotIn("Authtoken", self.client.get(reverse("admin:index")).content.decode())

    def test_non_superuser_cannot_attach_credentials_to_another_user(self):
        self.client.force_login(self.staff)
        before = (EmailAddress.objects.count(), SocialAccount.objects.count(), Token.objects.count())
        self.client.post(reverse("admin:account_emailaddress_add"), {
            "user": self.root.pk, "email": "evil@example.com", "verified": "on", "primary": "on",
        })
        self.client.post(reverse("admin:socialaccount_socialaccount_add"), {
            "user": self.root.pk, "provider": "github", "uid": "99", "extra_data": "{}",
        })
        self.client.post(reverse("admin:authtoken_tokenproxy_add"), {"user": self.root.pk})
        after = (EmailAddress.objects.count(), SocialAccount.objects.count(), Token.objects.count())
        self.assertEqual(before, after)
        self.assertFalse(EmailAddress.objects.filter(email="evil@example.com").exists())

    def test_non_superuser_cannot_edit_or_delete_existing_rows(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("admin:account_emailaddress_change", args=[self.email.pk]),
            {"user": self.root.pk, "email": "changed@example.com"},
        )
        self.client.post(
            reverse("admin:authtoken_tokenproxy_delete", args=[TokenProxy.objects.get(key=self.token.key).pk]), {"post": "yes"}
        )
        self.email.refresh_from_db()
        self.assertEqual(self.email.email, "v@example.com")
        self.assertTrue(Token.objects.filter(pk=self.token.pk).exists())

    def test_superuser_can_list_view_and_add(self):
        self.client.force_login(self.root)
        for model in MODELS:
            n = _name(model)
            with self.subTest(model=n):
                self.assertEqual(self.client.get(reverse(f"admin:{n}_changelist")).status_code, 200)
                self.assertEqual(self.client.get(reverse(f"admin:{n}_add")).status_code, 200)
                self.assertEqual(
                    self.client.get(reverse(f"admin:{n}_change", args=[self._change_pk(model)])).status_code,
                    200,
                )

    def test_superuser_post_succeeds(self):
        self.client.force_login(self.root)
        r = self.client.post(reverse("admin:account_emailaddress_add"), {
            "user": self.victim.pk, "email": "added@example.com", "verified": "on",
        })
        self.assertEqual(r.status_code, 302)
        self.assertTrue(EmailAddress.objects.filter(email="added@example.com").exists())
        other = User.objects.create_user(username="tok_1618", password="pw123456!x")
        r = self.client.post(reverse("admin:authtoken_tokenproxy_add"), {"user": other.pk})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Token.objects.filter(user=other).exists())

    def test_inactive_superuser_flag_is_not_enough(self):
        from accounts.admin import SuperuserOnlyAdminMixin

        class R:
            user = User(is_active=False, is_superuser=True)

        self.assertFalse(SuperuserOnlyAdminMixin().has_view_permission(R()))
