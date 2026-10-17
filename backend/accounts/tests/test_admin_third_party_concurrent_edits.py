"""Third-party admins (email addresses, login identities, API tokens, social apps):
unchanged fields and the rendered-state check (#1642).

Each change form writes only the fields the administrator changed and renders
a hidden ``row_version``; a form rendered against an older state is refused.
A change committed after the request loaded the row is simulated by committing
it from the form's ``clean``, the step between the load and ``save_model``'s
row lock.
"""

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialApp
from django.contrib import admin
from django.contrib.sites.models import Site
from django.test import TestCase
from django.urls import reverse
from rest_framework.authtoken.models import Token, TokenProxy

from accounts.models import User
from accounts.tests.test_admin_group_sitesetting_concurrent_edits import (
    _PLAIN_STATIC,
    PW,
    page_data,
    patched_clean,
)
from visiban.admin_row_version import STALE_FORM_MESSAGE, default_row_version_salt


def form_class(model):
    return admin.site._registry[model].form


@_PLAIN_STATIC
class ThirdPartyAdminConcurrentEditTests(TestCase):
    def setUp(self):
        self.root = User.objects.create_superuser(
            username="root_1642", email="root1642@example.com", password=PW
        )
        self.victim = User.objects.create_user(
            username="victim_1642", email="victim1642@example.com", password=PW
        )
        self.other = User.objects.create_user(
            username="other_1642", email="other1642@example.com", password=PW
        )
        self.email = EmailAddress.objects.create(
            user=self.victim, email="v1642@example.com", verified=False, primary=False
        )
        self.account = SocialAccount.objects.create(
            user=self.victim, provider="github", uid="1", extra_data={"k": "v"}
        )
        self.app = SocialApp.objects.create(
            provider="github", name="gh", client_id="cid", secret="sec"
        )
        self.site = Site.objects.get_current()
        self.token = Token.objects.create(user=self.victim)
        self.client.force_login(self.root)

    def url(self, model, pk):
        return reverse(
            f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[pk]
        )

    def render(self, model, pk):
        response = self.client.get(self.url(model, pk))
        self.assertEqual(response.status_code, 200)
        return response

    # -- shared behavior ---------------------------------------------------

    def test_each_form_has_its_own_digest_key(self):
        keys = {
            default_row_version_salt(m)
            for m in (EmailAddress, SocialAccount, SocialApp, TokenProxy)
        }
        self.assertEqual(len(keys), 4)

    def test_change_pages_render_row_version_and_add_pages_do_not(self):
        for model, pk in (
            (EmailAddress, self.email.pk),
            (SocialAccount, self.account.pk),
            (SocialApp, self.app.pk),
            (TokenProxy, self.victim.pk),
        ):
            with self.subTest(model=model.__name__):
                self.assertContains(
                    self.render(model, pk), 'type="hidden" name="row_version"'
                )
                add = self.client.get(
                    reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_add")
                )
                self.assertEqual(add.status_code, 200)
                self.assertNotContains(add, 'name="row_version"')

    # -- EmailAddress ------------------------------------------------------

    def test_email_matching_row_version_saves(self):
        data = page_data(self.render(EmailAddress, self.email.pk), primary=True)
        response = self.client.post(self.url(EmailAddress, self.email.pk), data)
        self.assertEqual(response.status_code, 302)
        self.email.refresh_from_db()
        self.assertTrue(self.email.primary)

    def test_email_unchanged_verified_keeps_a_change_made_after_load(self):
        data = page_data(
            self.render(EmailAddress, self.email.pk), row_version=False, primary=True
        )
        verify = lambda: EmailAddress.objects.filter(pk=self.email.pk).update(  # noqa: E731
            verified=True
        )
        with patched_clean(form_class(EmailAddress), verify):
            response = self.client.post(self.url(EmailAddress, self.email.pk), data)
        self.assertEqual(response.status_code, 302)
        self.email.refresh_from_db()
        self.assertTrue(self.email.verified)
        self.assertTrue(self.email.primary)

    def test_email_unchanged_verified_keeps_an_unverify_made_after_load(self):
        EmailAddress.objects.filter(pk=self.email.pk).update(verified=True)
        data = page_data(
            self.render(EmailAddress, self.email.pk), row_version=False, primary=True
        )
        unverify = lambda: EmailAddress.objects.filter(pk=self.email.pk).update(  # noqa: E731
            verified=False
        )
        with patched_clean(form_class(EmailAddress), unverify):
            self.client.post(self.url(EmailAddress, self.email.pk), data)
        self.email.refresh_from_db()
        self.assertFalse(self.email.verified)
        self.assertTrue(self.email.primary)

    def test_email_stale_form_is_refused(self):
        data = page_data(self.render(EmailAddress, self.email.pk), primary=True)
        EmailAddress.objects.filter(pk=self.email.pk).update(verified=True)
        response = self.client.post(self.url(EmailAddress, self.email.pk), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, STALE_FORM_MESSAGE)
        self.email.refresh_from_db()
        self.assertFalse(self.email.primary)

    def test_email_change_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(EmailAddress, self.email.pk), primary=True)
        verify = lambda: EmailAddress.objects.filter(pk=self.email.pk).update(  # noqa: E731
            verified=True
        )
        with patched_clean(form_class(EmailAddress), verify):
            response = self.client.post(self.url(EmailAddress, self.email.pk), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.url(EmailAddress, self.email.pk))
        self.email.refresh_from_db()
        self.assertFalse(self.email.primary)

    # -- SocialAccount -----------------------------------------------------

    def test_social_account_stale_form_is_refused(self):
        data = page_data(
            self.render(SocialAccount, self.account.pk), user=str(self.other.pk)
        )
        SocialAccount.objects.filter(pk=self.account.pk).update(uid="2")
        response = self.client.post(self.url(SocialAccount, self.account.pk), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, STALE_FORM_MESSAGE)
        self.account.refresh_from_db()
        self.assertEqual(self.account.user_id, self.victim.pk)

    def test_social_account_unchanged_user_keeps_a_change_made_after_load(self):
        data = page_data(
            self.render(SocialAccount, self.account.pk), row_version=False, uid="3"
        )
        move = lambda: SocialAccount.objects.filter(pk=self.account.pk).update(  # noqa: E731
            user=self.other
        )
        with patched_clean(form_class(SocialAccount), move):
            response = self.client.post(self.url(SocialAccount, self.account.pk), data)
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.uid, "3")
        self.assertEqual(self.account.user_id, self.other.pk)

    def test_social_account_change_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(SocialAccount, self.account.pk), uid="3")
        move = lambda: SocialAccount.objects.filter(pk=self.account.pk).update(  # noqa: E731
            user=self.other
        )
        with patched_clean(form_class(SocialAccount), move):
            response = self.client.post(self.url(SocialAccount, self.account.pk), data)
        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.uid, "1")

    # -- TokenProxy --------------------------------------------------------

    def test_token_stale_form_is_refused(self):
        data = page_data(self.render(TokenProxy, self.victim.pk), user=str(self.other.pk))
        Token.objects.filter(pk=self.token.pk).update(user=self.root)
        response = self.client.post(self.url(TokenProxy, self.victim.pk), data)
        self.assertIn(response.status_code, (200, 302))
        self.token.refresh_from_db()
        self.assertEqual(self.token.user_id, self.root.pk)

    def test_token_matching_row_version_saves(self):
        data = page_data(self.render(TokenProxy, self.victim.pk), user=str(self.other.pk))
        response = self.client.post(self.url(TokenProxy, self.victim.pk), data)
        self.assertEqual(response.status_code, 302)
        self.token.refresh_from_db()
        self.assertEqual(self.token.user_id, self.other.pk)

    def test_token_change_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(TokenProxy, self.victim.pk), user=str(self.other.pk))
        move = lambda: Token.objects.filter(pk=self.token.pk).update(  # noqa: E731
            user=self.root
        )
        with patched_clean(form_class(TokenProxy), move):
            response = self.client.post(self.url(TokenProxy, self.victim.pk), data)
        self.assertEqual(response.status_code, 302)
        self.token.refresh_from_db()
        self.assertEqual(self.token.user_id, self.victim.pk)

    # -- SocialApp ---------------------------------------------------------

    def test_social_app_matching_row_version_saves(self):
        data = page_data(self.render(SocialApp, self.app.pk), name="renamed")
        response = self.client.post(self.url(SocialApp, self.app.pk), data)
        self.assertEqual(response.status_code, 302)
        self.app.refresh_from_db()
        self.assertEqual(self.app.name, "renamed")

    def test_social_app_stale_form_is_refused(self):
        data = page_data(self.render(SocialApp, self.app.pk), name="renamed")
        SocialApp.objects.filter(pk=self.app.pk).update(secret="rotated")
        response = self.client.post(self.url(SocialApp, self.app.pk), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, STALE_FORM_MESSAGE)
        self.app.refresh_from_db()
        self.assertEqual(self.app.name, "gh")

    def test_social_app_unchanged_secret_keeps_a_change_made_after_load(self):
        data = page_data(
            self.render(SocialApp, self.app.pk), row_version=False, name="renamed"
        )
        rotate = lambda: SocialApp.objects.filter(pk=self.app.pk).update(  # noqa: E731
            secret="rotated"
        )
        with patched_clean(form_class(SocialApp), rotate):
            response = self.client.post(self.url(SocialApp, self.app.pk), data)
        self.assertEqual(response.status_code, 302)
        self.app.refresh_from_db()
        self.assertEqual(self.app.name, "renamed")
        self.assertEqual(self.app.secret, "rotated")

    def test_social_app_unchanged_sites_keep_a_change_made_after_load(self):
        data = page_data(
            self.render(SocialApp, self.app.pk), row_version=False, name="renamed"
        )
        with patched_clean(form_class(SocialApp), lambda: self.app.sites.add(self.site)):
            response = self.client.post(self.url(SocialApp, self.app.pk), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.app.sites.all()), [self.site])

    def test_social_app_sites_change_after_render_refuses_the_save(self):
        data = page_data(self.render(SocialApp, self.app.pk), name="renamed")
        self.app.sites.add(self.site)
        response = self.client.post(self.url(SocialApp, self.app.pk), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, STALE_FORM_MESSAGE)
        self.app.refresh_from_db()
        self.assertEqual(self.app.name, "gh")

    def test_omitted_row_version_saves_the_changed_field(self):
        data = page_data(
            self.render(SocialApp, self.app.pk), row_version=False, name="renamed"
        )
        response = self.client.post(self.url(SocialApp, self.app.pk), data)
        self.assertEqual(response.status_code, 302)
        self.app.refresh_from_db()
        self.assertEqual(self.app.name, "renamed")
