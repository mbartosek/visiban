"""allauth's stock password-reset pages redirect to the SPA (#1554).

Reset emails sent before the redirect existed link to
``/accounts/password/reset/key/<uidb36>-<key>/``; the uid and key must reach
the SPA reset route intact so those links keep working.
"""

from django.conf import settings
from django.test import Client, TestCase
from django.urls import resolve, reverse

from allauth.account.forms import default_token_generator
from allauth.account.utils import user_pk_to_url_str

from accounts.models import User
from accounts.views import PasswordResetRedirectView


def _spa(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


class PasswordResetRedirectTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_request_and_done_pages_go_to_the_spa(self):
        cases = {
            "/accounts/password/reset/": "/forgot-password",
            "/accounts/password/reset/done/": "/forgot-password",
            "/accounts/password/reset/key/done/": "/forgot-password",
        }
        for path, target in cases.items():
            for method in ("get", "head", "post"):
                with self.subTest(path=path, method=method):
                    r = getattr(self.client, method)(path, {"email": "x@example.com"} if method == "post" else None)
                    self.assertEqual(r.status_code, 302)
                    self.assertEqual(r["Location"], _spa(target))

    def test_previously_emailed_key_link_lands_on_the_spa_reset_page(self):
        user = User.objects.create_user(username="resetme", email="r@example.com", password="OldPass12345!")
        uid = user_pk_to_url_str(user)
        token = default_token_generator.make_token(user)
        for method in ("get", "post"):
            with self.subTest(method=method):
                r = getattr(self.client, method)(f"/accounts/password/reset/key/{uid}-{token}/")
                self.assertEqual(r.status_code, 302)
                self.assertEqual(r["Location"], _spa(f"/reset-password/{uid}/{token}"))
        user.refresh_from_db()
        self.assertTrue(user.check_password("OldPass12345!"))

    def test_post_to_key_link_does_not_set_a_password(self):
        user = User.objects.create_user(username="resetme2", email="r2@example.com", password="OldPass12345!")
        uid = user_pk_to_url_str(user)
        token = default_token_generator.make_token(user)
        self.client.post(
            f"/accounts/password/reset/key/{uid}-{token}/",
            {"password1": "BrandNew45678!", "password2": "BrandNew45678!"},
        )
        user.refresh_from_db()
        self.assertTrue(user.check_password("OldPass12345!"))

    def test_query_string_cannot_steer_the_redirect(self):
        r = self.client.get("/accounts/password/reset/?next=https://evil.example/&redirect=//evil.example")
        self.assertEqual(r["Location"], _spa("/forgot-password"))

    def test_malformed_or_oversized_key_is_not_forwarded(self):
        for path in (
            "/accounts/password/reset/key/Mg-" + "a" * 201 + "/",
            "/accounts/password/reset/key/Mg-a b/",
            "/accounts/password/reset/key/Mg-a/../../x/",
        ):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertNotIn("/reset-password/", r.get("Location", ""))

    def test_names_and_reverse_are_unchanged(self):
        self.assertEqual(reverse("account_reset_password"), "/accounts/password/reset/")
        self.assertEqual(
            reverse("account_reset_password_from_key", kwargs={"uidb36": "Mg", "key": "abc-def"}),
            "/accounts/password/reset/key/Mg-abc-def/",
        )
        match = resolve("/accounts/password/reset/key/Mg-abc-def/")
        self.assertEqual(match.url_name, "account_reset_password_from_key")
        self.assertIs(match.func.view_class, PasswordResetRedirectView)

    def test_sso_routes_are_untouched(self):
        for path in ("/accounts/login/", "/accounts/logout/", "/accounts/email/", "/accounts/3rdparty/"):
            with self.subTest(path=path):
                self.assertIsNot(
                    getattr(resolve(path).func, "view_class", None), PasswordResetRedirectView
                )
        for name in ("socialaccount_login_cancelled", "socialaccount_connections"):
            with self.subTest(name=name):
                self.assertIsNot(
                    getattr(resolve(reverse(name)).func, "view_class", None), PasswordResetRedirectView
                )

    def test_signed_in_user_with_forced_change_still_reaches_the_redirect(self):
        user = User.objects.create_user(username="forced", email="f@example.com", password="OldPass12345!")
        user.must_change_password = True
        user.save(update_fields=["must_change_password"])
        self.client.force_login(user)
        r = self.client.get("/accounts/password/reset/key/Mg-abc-def/")
        self.assertEqual(r["Location"], _spa("/reset-password/Mg/abc-def"))
