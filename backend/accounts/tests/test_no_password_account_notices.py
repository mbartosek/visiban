"""Account notices for accounts without a usable password.

Registry Rule 3 (``docs/development/security-invariants.md``, "Account email
change" and "Password reset" rows):

- An address added to or confirmed on an account without a usable password is
  reported to the account's current email, never to the added address, on
  every route that adds or confirms one.
- A first password set from a reset link, or directly by the signed-in user, is reported to every verified address
  on the account, one message per address.
- Accounts with a usable password get neither notice.
- Notices are fixed text and are sent only once the change commits.
"""

from unittest.mock import patch

from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from allauth.account.utils import user_pk_to_url_str
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import Client, TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from accounts import credentials
from accounts.models import User
from accounts.no_password_notices import (
    FIRST_PASSWORD_NOTICE_LIMIT,
    notify_on_email_added,
    notify_on_email_confirmed,
    notify_primary_of_address_change,
)
from accounts.tests.email_change_helpers import reauthenticate
from accounts.tests.test_password_reset import _redeem_via_allauth_html_page

VERIFICATION_MODES = ("none", "optional", "mandatory")
VERIFY_EMAIL_URL = "/api/v1/auth/registration/verify-email/"
RESET_CONFIRM_URL = "/api/v1/auth/password/reset/confirm/"
PASSWORD = "pass12345678"  # gitleaks:allow -- test-only fixture password, not a credential
NEW_PASSWORD = "NewPassword9876"  # gitleaks:allow -- test-only fixture password
PRIMARY = "sso@old.example"
SECOND = "sso@second.example"
NEW = "sso@new.example"
ADDED_SUBJECT = "An email address was added to your Visiban account"
PASSWORD_SUBJECT = "A password was set on your Visiban account"
CHANGED_SUBJECT = "Your Visiban email address was changed"


def _with_subject(subject):
    return [m for m in mail.outbox if m.subject == subject]


def _added_notices():
    return _with_subject(ADDED_SUBJECT)


def _password_notices():
    return _with_subject(PASSWORD_SUBJECT)


def _make_account(username="sso", email=PRIMARY, *, password=None):
    user = User.objects.create_user(username=username, email=email, password=password)
    if password is None:
        user.set_unusable_password()
        user.save()
    EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)
    return user


class AddressNoticeTests(TestCase):
    """An added or confirmed address is reported to the current email."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = _make_account()
        self.client = Client()
        self.client.force_login(self.user)

    def _add_on_manage_page(self, email=NEW):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post("/accounts/email/", {"email": email, "action_add": ""})
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("/accounts/reauthenticate/", r.get("Location", ""))
        return EmailAddress.objects.get(user=self.user, email=email)

    def _confirm(self, row):
        with self.captureOnCommitCallbacks(execute=True):
            r = APIClient().post(
                VERIFY_EMAIL_URL, {"key": EmailConfirmationHMAC(row).key}, format="json"
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)

    def test_add_on_the_manage_page_notifies_the_primary_in_every_mode(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                cache.clear()  # one notice per account per hour; each mode starts fresh
                mail.outbox = []
                EmailAddress.objects.filter(user=self.user, email=NEW).delete()
                self._add_on_manage_page()
                notices = _added_notices()
                self.assertEqual([m.to for m in notices], [[PRIMARY]])
                self.assertNotIn(NEW, notices[0].body)
                self.assertNotIn(NEW, notices[0].subject)
                # The added address itself never receives this notice.
                self.assertFalse(any(NEW in m.to for m in notices))

    def test_confirmation_notifies_the_primary_in_every_mode(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                cache.clear()  # one notice per account per hour; each mode starts fresh
                EmailAddress.objects.filter(user=self.user, email=NEW).delete()
                row = EmailAddress.objects.create(
                    user=self.user, email=NEW, verified=False, primary=False
                )
                mail.outbox = []
                self._confirm(row)
                row.refresh_from_db()
                self.assertTrue(row.verified)
                self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])

    def test_add_then_confirm_within_the_hour_sends_one_notice(self):
        # The text is generic, so a second step inside the window adds nothing.
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                cache.clear()
                mail.outbox = []
                EmailAddress.objects.filter(user=self.user, email=NEW).delete()
                row = self._add_on_manage_page()
                self._confirm(row)
                self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])

    def test_confirm_after_the_window_notifies_again(self):
        row = self._add_on_manage_page()
        cache.clear()  # stands in for the hour passing
        self._confirm(row)
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY], [PRIMARY]])

    def test_notice_is_limited_to_one_per_account_per_hour(self):
        for n in range(3):
            self._add_on_manage_page(f"extra{n}@new.example")
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])
        other = _make_account(username="other", email="other@old.example")
        client = Client()
        client.force_login(other)
        with self.captureOnCommitCallbacks(execute=True):
            client.post("/accounts/email/", {"email": "o@new.example", "action_add": ""})
        self.assertEqual(
            sorted(m.to[0] for m in _added_notices()), sorted([PRIMARY, "other@old.example"])
        )

    def test_rolled_back_change_does_not_use_up_the_window(self):
        from accounts.no_password_notices import notify_primary_of_address_change

        try:
            with transaction.atomic():
                notify_primary_of_address_change(self.user, NEW)
                raise RuntimeError("roll back")
        except RuntimeError:
            pass
        self.assertEqual(_added_notices(), [])
        with self.captureOnCommitCallbacks(execute=True):
            notify_primary_of_address_change(self.user, NEW)
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])

    def test_unavailable_cache_still_sends_the_notice(self):
        with patch("accounts.no_password_notices.cache.add", side_effect=OSError("cache down")):
            self._add_on_manage_page()
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_notice_failure_does_not_roll_back_the_pending_change(self):
        client = APIClient()
        client.force_login(self.user)
        with patch(
            "accounts.no_password_notices.notify_primary_of_address_change",
            side_effect=RuntimeError("boom"),
        ), self.assertLogs("accounts.email_change", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                r = client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertTrue(EmailAddress.objects.filter(user=self.user, email=NEW).exists())
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.pending_email_address_id)
        self.assertNotIn("@", "\n".join(logs.output))

    def _admin_make_verified(self, row):
        # allauth's EmailAddress admin "make verified" action sends
        # email_confirmed without going through the verify-email view.
        from django.contrib import admin
        from django.test import RequestFactory

        model_admin = admin.site._registry[EmailAddress]
        request = RequestFactory().post("/admin/")
        with patch.object(model_admin, "message_user"), self.captureOnCommitCallbacks(
            execute=True
        ):
            model_admin.make_verified(request, EmailAddress.objects.filter(pk=row.pk))

    def test_admin_verify_action_notifies_the_primary(self):
        row = EmailAddress.objects.create(user=self.user, email=NEW, verified=False, primary=False)
        self._admin_make_verified(row)
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])

    def test_confirming_the_primary_itself_sends_nothing(self):
        # A signup confirmation: the confirmed address is the account email.
        EmailAddress.objects.filter(user=self.user).update(verified=False)
        self._confirm(EmailAddress.objects.get(user=self.user, email=PRIMARY))
        self.assertEqual(_added_notices(), [])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_pending_change_notifies_on_request_and_confirm_that_moves_email_sends_only_previous_notice(self):
        client = APIClient()
        client.force_login(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            r = client.patch("/api/v1/auth/me/", {"email": NEW}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])
        mail.outbox = []
        # Clear the hourly claim so the on-commit re-check of User.email is the
        # only thing that can suppress the address notice at this step.
        cache.clear()
        self._confirm(EmailAddress.objects.get(user=self.user, email=NEW))
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, NEW)
        # The confirmation moved the account email, so the previous-address
        # notice covers that mailbox and the address notice is not repeated.
        self.assertEqual(_added_notices(), [])
        self.assertEqual([m.to for m in _with_subject(CHANGED_SUBJECT)], [[PRIMARY]])

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
    def test_confirmed_pending_change_that_is_not_applied_still_notifies(self):
        # The email-change receiver leaves User.email alone when the address
        # collides; the address is verified all the same, so the notice goes.
        row = EmailAddress.objects.create(user=self.user, email=NEW, verified=False, primary=False)
        User.objects.filter(pk=self.user.pk).update(pending_email_address=row)
        with patch("accounts.email_change.confirmation_would_collide", return_value=True):
            self._admin_make_verified(row)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, PRIMARY)
        self.assertEqual([m.to for m in _added_notices()], [[PRIMARY]])

    def test_direct_change_sends_only_the_previous_address_notice(self):
        for mode in ("none", "optional"):
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                user = _make_account(username=f"direct{mode}", email=f"d{mode}@old.example")
                client = APIClient()
                client.force_login(user)
                mail.outbox = []
                with self.captureOnCommitCallbacks(execute=True):
                    r = client.patch(
                        "/api/v1/auth/me/", {"email": f"d{mode}@new.example"}, format="json"
                    )
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                self.assertEqual(_added_notices(), [])
                self.assertEqual(
                    [m.to for m in _with_subject(CHANGED_SUBJECT)], [[f"d{mode}@old.example"]]
                )

    def test_notice_carries_no_user_controlled_text(self):
        User.objects.filter(pk=self.user.pk).update(
            display_name="Visit evil.example now", first_name="Zed"
        )
        self._add_on_manage_page()
        (notice,) = _added_notices()
        for text in ("evil.example", "Zed", "sso", NEW):
            self.assertNotIn(text, notice.body)
            self.assertNotIn(text, notice.subject)
        self.assertIn("Visiban", notice.body)

    def test_rolled_back_add_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    notify_primary_of_address_change(self.user, NEW)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(_added_notices(), [])

    def test_account_without_an_email_gets_nothing(self):
        User.objects.filter(pk=self.user.pk).update(email="")
        self.user.refresh_from_db()
        with self.captureOnCommitCallbacks(execute=True):
            notify_primary_of_address_change(self.user, NEW)
        self.assertEqual(_added_notices(), [])

    def test_mail_failure_is_logged_without_an_address(self):
        with patch(
            "django.core.mail.EmailMessage.send", side_effect=OSError("relay down")
        ), self.assertLogs("accounts.email_change", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                notify_primary_of_address_change(self.user, NEW)
        joined = "\n".join(logs.output)
        self.assertNotIn(PRIMARY, joined)
        self.assertNotIn(NEW, joined)
        self.assertIn(f"user_id={self.user.pk}", joined)


class AccountWithPasswordGetsNoNewNoticeTests(TestCase):
    """Regression guard: accounts with a usable password are unchanged."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = _make_account(password=PASSWORD)

    def test_add_and_confirm_send_no_address_notice(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                EmailAddress.objects.filter(user=self.user, email=NEW).delete()
                mail.outbox = []
                client = Client()
                client.force_login(self.user)
                reauthenticate(client, PASSWORD)
                with self.captureOnCommitCallbacks(execute=True):
                    client.post("/accounts/email/", {"email": NEW, "action_add": ""})
                row = EmailAddress.objects.get(user=self.user, email=NEW)
                with self.captureOnCommitCallbacks(execute=True):
                    APIClient().post(
                        VERIFY_EMAIL_URL, {"key": EmailConfirmationHMAC(row).key}, format="json"
                    )
                self.assertEqual(_added_notices(), [])

    def test_receivers_add_no_query_for_an_account_with_a_password(self):
        from django.test import RequestFactory

        # Added on the manage page: the row carries its user.
        added = EmailAddress(user=self.user, email=NEW, verified=False, primary=False)
        with self.assertNumQueries(0), self.captureOnCommitCallbacks(execute=True):
            notify_on_email_added(None, request=None, user=self.user, email_address=added)
        # Confirmed while signed in: a freshly read row, the request's user.
        row = EmailAddress.objects.create(user=self.user, email=NEW, verified=True, primary=False)
        fresh = EmailAddress.objects.get(pk=row.pk)
        request = RequestFactory().post("/")
        request.user = self.user
        with self.assertNumQueries(0), self.captureOnCommitCallbacks(execute=True):
            notify_on_email_confirmed(None, request=request, email_address=fresh)
        self.assertEqual(_added_notices(), [])

    def test_reset_sends_no_password_notice(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                mail.outbox = []
                self.user.refresh_from_db()
                with self.captureOnCommitCallbacks(execute=True):
                    r = APIClient().post(RESET_CONFIRM_URL, {
                        "uid": user_pk_to_url_str(self.user),
                        "token": default_token_generator.make_token(self.user),
                        "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
                    })
                self.assertEqual(r.status_code, status.HTTP_200_OK)
                self.assertEqual(_password_notices(), [])


class FirstPasswordNoticeTests(TestCase):
    """A first password set from a reset link is reported to every verified address."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _account_with_addresses(self, username):
        user = _make_account(username=username, email=f"{username}@old.example")
        EmailAddress.objects.create(
            user=user, email=f"{username}@second.example", verified=True, primary=False
        )
        EmailAddress.objects.create(
            user=user, email=f"{username}@unverified.example", verified=False, primary=False
        )
        return user

    def _reset_rest(self, user):
        with self.captureOnCommitCallbacks(execute=True):
            r = APIClient().post(RESET_CONFIRM_URL, {
                "uid": user_pk_to_url_str(user),
                "token": default_token_generator.make_token(user),
                "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
            })
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_rest_reset_notifies_every_verified_address_in_every_mode(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                user = self._account_with_addresses(f"r{mode}")
                mail.outbox = []
                self._reset_rest(user)
                user.refresh_from_db()
                self.assertTrue(user.check_password(NEW_PASSWORD))
                notices = _password_notices()
                # One message per address, so the addresses are not shown to each other.
                self.assertEqual(
                    sorted(m.to for m in notices),
                    [[f"r{mode}@old.example"], [f"r{mode}@second.example"]],
                )
                for notice in notices:
                    self.assertNotIn(f"r{mode}", notice.body)
                    self.assertIn("Visiban", notice.body)

    @override_settings(ROOT_URLCONF="accounts.tests.allauth_reset_urls")
    def test_allauth_html_reset_page_notifies_every_verified_address(self):
        user = self._account_with_addresses("html")
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            r = _redeem_via_allauth_html_page(
                user_pk_to_url_str(user), default_token_generator.make_token(user), NEW_PASSWORD
            )
        self.assertEqual(r.status_code, 302)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        self.assertEqual(
            sorted(m.to for m in _password_notices()),
            [["html@old.example"], ["html@second.example"]],
        )

    def test_account_email_without_a_verified_row_is_notified_first(self):
        user = User.objects.create_user(username="unrow", email="unrow@old.example")
        user.set_unusable_password()
        user.save()
        EmailAddress.objects.create(
            user=user, email="unrow@old.example", verified=False, primary=True
        )
        EmailAddress.objects.create(
            user=user, email="unrow@added.example", verified=True, primary=False
        )
        mail.outbox = []
        self._reset_rest(user)
        self.assertEqual(
            [m.to for m in _password_notices()],
            [["unrow@old.example"], ["unrow@added.example"]],
        )

    def test_recipients_are_capped_keeping_the_account_email_and_oldest(self):
        user = _make_account(username="many", email="many@old.example")
        extra = [f"many{i:02d}@example.com" for i in range(FIRST_PASSWORD_NOTICE_LIMIT + 2)]
        for email in extra:
            EmailAddress.objects.create(user=user, email=email, verified=True, primary=False)
        mail.outbox = []
        self._reset_rest(user)
        sent = [m.to[0] for m in _password_notices()]
        self.assertEqual(len(sent), FIRST_PASSWORD_NOTICE_LIMIT)
        self.assertEqual(sent, ["many@old.example", *extra[: FIRST_PASSWORD_NOTICE_LIMIT - 1]])

    def test_a_later_reset_is_not_a_first_password(self):
        user = self._account_with_addresses("again")
        self._reset_rest(user)
        mail.outbox = []
        user.refresh_from_db()
        self._reset_rest(user)
        self.assertEqual(_password_notices(), [])

    def test_rolled_back_reset_sends_nothing(self):
        user = self._account_with_addresses("rollback")
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    credentials.finalize_password_reset(user, first_password=True)
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(_password_notices(), [])

    def test_notices_share_one_connection(self):
        user = self._account_with_addresses("conn")
        with patch(
            "django.core.mail.get_connection", wraps=__import__("django.core.mail", fromlist=["x"]).get_connection
        ) as get_conn:
            self._reset_rest(user)
        self.assertEqual(len(_password_notices()), 2)
        # Allauth/other mail in the request may open its own; the notice loop opens one.
        notice_calls = [c for c in get_conn.call_args_list if c.kwargs.get("timeout") is not None]
        self.assertEqual(len(notice_calls), 1)

    def test_connection_failure_does_not_fail_the_reset(self):
        user = self._account_with_addresses("connfail")
        with patch("django.core.mail.get_connection", side_effect=OSError("down")):
            self._reset_rest(user)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))

    def test_mail_failure_does_not_fail_the_reset_or_log_an_address(self):
        user = self._account_with_addresses("fail")
        with patch(
            "django.core.mail.EmailMessage.send", side_effect=OSError("relay down")
        ), self.assertLogs("accounts.email_change", level="WARNING") as logs:
            self._reset_rest(user)
        user.refresh_from_db()
        self.assertTrue(user.check_password(NEW_PASSWORD))
        joined = "\n".join(logs.output)
        self.assertNotIn("@", joined)
        self.assertIn(f"user_id={user.pk}", joined)


class ResetEntryPointsCarryTheFirstPasswordFlagTests(TestCase):
    """The ``password_reset`` signal backstop cannot tell a first password, so
    every reset entry point must be one that passes the flag itself (the
    allauth page is covered with an exact-count assertion above)."""

    def test_rest_confirm_uses_the_project_serializer(self):
        from dj_rest_auth.app_settings import api_settings

        from accounts.serializers import VisibanPasswordResetConfirmSerializer

        self.assertIs(
            api_settings.PASSWORD_RESET_CONFIRM_SERIALIZER, VisibanPasswordResetConfirmSerializer
        )

    def test_allauth_key_form_is_the_project_form(self):
        from allauth.account import app_settings
        from allauth.account.forms import ResetPasswordKeyForm
        from allauth.utils import get_form_class

        from accounts.forms import VisibanResetPasswordKeyForm

        self.assertIs(
            get_form_class(app_settings.FORMS, "reset_password_from_key", ResetPasswordKeyForm),
            VisibanResetPasswordKeyForm,
        )

    def test_allauth_key_page_is_not_routed_to_allauth_view(self):
        from allauth.account import views as allauth_views
        from django.urls import resolve

        match = resolve("/accounts/password/reset/key/Mg-abc-def/")
        self.assertIsNot(
            getattr(match.func, "view_class", None), allauth_views.PasswordResetFromKeyView
        )


class DirectFirstPasswordNoticeTests(TestCase):
    """A first password set directly by the signed-in user is reported the same way."""

    ROUTES = ("/api/v1/auth/change-password/", "/api/v1/auth/password/change/")

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _account(self, username, *, password=None):
        user = _make_account(username=username, email=f"{username}@old.example", password=password)
        EmailAddress.objects.create(
            user=user, email=f"{username}@second.example", verified=True, primary=False
        )
        return user

    def _recipients(self):
        return sorted(m.to[0] for m in _password_notices())

    def _expected(self, username):
        return sorted([f"{username}@old.example", f"{username}@second.example"])

    def _post(self, url, user, *, with_old=False):
        client = APIClient()
        client.force_login(user)
        body = {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        if url.endswith("/change-password/"):
            body = {"new_password": NEW_PASSWORD}
        if with_old:
            body["old_password" if "password/change" in url else "current_password"] = PASSWORD
        with self.captureOnCommitCallbacks(execute=True):
            r = client.post(url, body, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)

    def test_rest_routes_notify_every_verified_address_in_every_mode(self):
        for url in self.ROUTES:
            for mode in VERIFICATION_MODES:
                with self.subTest(url=url, mode=mode), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    name = f"d{abs(hash((url, mode)))}"
                    user = self._account(name)
                    mail.outbox = []
                    self._post(url, user)
                    user.refresh_from_db()
                    self.assertTrue(user.check_password(NEW_PASSWORD))
                    self.assertEqual(self._recipients(), self._expected(name))
                    for notice in _password_notices():
                        self.assertNotIn("reset", notice.body.lower())
                        self.assertNotIn("http", notice.body)

    def test_rest_routes_send_nothing_for_an_account_with_a_password(self):
        for url in self.ROUTES:
            for mode in VERIFICATION_MODES:
                with self.subTest(url=url, mode=mode), override_settings(
                    ACCOUNT_EMAIL_VERIFICATION=mode
                ):
                    user = self._account(f"p{abs(hash((url, mode)))}", password=PASSWORD)
                    mail.outbox = []
                    self._post(url, user, with_old=True)
                    self.assertEqual(_password_notices(), [])

    def test_html_set_password_form_notifies_in_every_mode(self):
        from accounts.forms import VisibanSetPasswordForm

        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                name = f"f{mode}"
                user = self._account(name)
                mail.outbox = []
                form = VisibanSetPasswordForm(
                    user=user, data={"password1": NEW_PASSWORD, "password2": NEW_PASSWORD}
                )
                self.assertTrue(form.is_valid(), form.errors)
                with self.captureOnCommitCallbacks(execute=True):
                    form.save()
                # The form and the adapter hook inside it both finalize: one notice each.
                self.assertEqual(self._recipients(), self._expected(name))

    def test_html_change_password_form_for_an_account_with_a_password_sends_nothing(self):
        from accounts.forms import VisibanChangePasswordForm

        user = self._account("fchg", password=PASSWORD)
        mail.outbox = []
        form = VisibanChangePasswordForm(
            user=user,
            data={"oldpassword": PASSWORD, "password1": NEW_PASSWORD, "password2": NEW_PASSWORD},
        )
        self.assertTrue(form.is_valid(), form.errors)
        with self.captureOnCommitCallbacks(execute=True):
            form.save()
        self.assertEqual(_password_notices(), [])

    def test_password_set_signal_backstop_notifies_once(self):
        from allauth.account.signals import password_set

        user = self._account("sig")
        mail.outbox = []
        with self.captureOnCommitCallbacks(execute=True):
            password_set.send(sender=User, request=None, user=user)
            password_set.send(sender=User, request=None, user=user)
        self.assertEqual(self._recipients(), self._expected("sig"))

    def test_reset_of_a_first_password_sends_one_notice_per_address(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                name = f"rr{mode}"
                user = self._account(name)
                mail.outbox = []
                with self.captureOnCommitCallbacks(execute=True):
                    r = APIClient().post(RESET_CONFIRM_URL, {
                        "uid": user_pk_to_url_str(user),
                        "token": default_token_generator.make_token(user),
                        "new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD,
                    })
                self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
                self.assertEqual(self._recipients(), self._expected(name))

    def test_notice_text_is_generic(self):
        user = self._account("txt")
        mail.outbox = []
        self._post(self.ROUTES[0], user)
        body = _password_notices()[0].body
        self.assertIn("for the first time", body)
        self.assertNotIn("txt@", body)


class AdapterAndAdminFirstPasswordNoticeTests(TestCase):
    """The allauth adapter hook and the admin's own-account password form."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _staff(self, username, *, password=None):
        user = _make_account(username=username, email=f"{username}@old.example", password=password)
        EmailAddress.objects.create(
            user=user, email=f"{username}@second.example", verified=True, primary=False
        )
        User.objects.filter(pk=user.pk).update(is_staff=True, is_superuser=True)
        user.refresh_from_db()
        return user

    def _expected(self, username):
        return sorted([f"{username}@old.example", f"{username}@second.example"])

    def test_adapter_set_password_notifies_a_first_password_only(self):
        from accounts.adapter import RegistrationAdapter

        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                first = self._staff(f"af{mode}")
                mail.outbox = []
                with self.captureOnCommitCallbacks(execute=True):
                    RegistrationAdapter().set_password(first, NEW_PASSWORD)
                self.assertEqual(
                    sorted(m.to[0] for m in _password_notices()), self._expected(f"af{mode}")
                )
                # Setting it again on the same account is not a first password.
                mail.outbox = []
                cache.clear()
                with self.captureOnCommitCallbacks(execute=True):
                    RegistrationAdapter().set_password(User.objects.get(pk=first.pk), "Another9876x")
                self.assertEqual(_password_notices(), [])

    @override_settings(
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        }
    )
    def test_admin_own_account_password_form(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                for label, password in (("first", None), ("has", PASSWORD)):
                    name = f"ad{label}{mode}"
                    user = self._staff(name, password=password)
                    client = Client()
                    client.force_login(user)
                    mail.outbox = []
                    with self.captureOnCommitCallbacks(execute=True):
                        r = client.post(
                            f"/admin/accounts/user/{user.pk}/password/",
                            {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD,
                             "usable_password": "true"},
                        )
                    self.assertEqual(r.status_code, 302)
                    got = sorted(m.to[0] for m in _password_notices())
                    self.assertEqual(got, self._expected(name) if label == "first" else [])
