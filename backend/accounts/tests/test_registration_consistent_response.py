"""Registration answers an address in use the same way as a new one (#1877).

Under ``EMAIL_VERIFICATION=mandatory`` a new registration is answered
``201 {"detail": "Verification e-mail sent."}`` and is not signed in. A
registration for an address another account already uses now gets that same
answer: no account is created or changed, and the address is mailed allauth's
"account already exists" notice in place of a confirmation. This lines REST
registration up with the invite, member and login flows.

Under ``optional`` / ``none`` a new registration is signed in and receives a
token, which a registration that creates no account cannot reproduce, so the
existing ``400`` is kept there (pinned below, and by
``test_identifier_collision.RegistrationCollisionTests``).
"""

from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from rest_framework.test import APIClient

from allauth.account.models import EmailAddress

from accounts.models import InviteLink, InviteLinkRedemption, SiteSetting, User
from accounts.validators import answers_in_use_address_as_new
from accounts.views import RegisterAnonThrottle

REGISTER_URL = "/api/v1/auth/registration/"
CURRENT_USER_URL = "/api/v1/auth/user/"
PASSWORD = "Sup3rS3cr3t!xyz"
OWNER_PASSWORD = "Own3rS3cr3t!xyz"
ACCEPTED = {"detail": "Verification e-mail sent."}


def _set_mode(mode):
    row = SiteSetting.get()
    row.registration_mode = mode
    row.save()
    cache.clear()  # drop the cached registration mode


class _RegistrationMixin:
    def setUp(self):
        cache.clear()  # throttle buckets and the cached registration mode
        self.addCleanup(cache.clear)
        self.owner = User.objects.create_user(
            username="owner1877", email="owner1877@example.com", password=OWNER_PASSWORD
        )
        self.owner_password_hash = User.objects.get(pk=self.owner.pk).password

    def _register(self, email, client=None, **extra):
        body = {"email": email, "password1": PASSWORD, "password2": PASSWORD, **extra}
        return (client or APIClient()).post(REGISTER_URL, body, format="json")

    def _assert_same_answer(self, new_resp, in_use_resp):
        self.assertEqual(in_use_resp.status_code, new_resp.status_code)
        self.assertEqual(in_use_resp.json(), new_resp.json())
        self.assertEqual(sorted(in_use_resp.cookies.keys()), sorted(new_resp.cookies.keys()))
        self.assertEqual(in_use_resp.get("Content-Type"), new_resp.get("Content-Type"))

    def _assert_owner_untouched(self):
        owner = User.objects.get(pk=self.owner.pk)
        self.assertEqual(owner.password, self.owner_password_hash)
        self.assertTrue(check_password(OWNER_PASSWORD, owner.password))
        self.assertTrue(owner.is_active)
        self.assertEqual(owner.email, "owner1877@example.com")


@override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
class MandatoryVerificationTests(_RegistrationMixin, TestCase):
    def test_setting_selects_the_consistent_answer(self):
        self.assertTrue(answers_in_use_address_as_new())

    def test_unverified_address_in_use_gets_the_new_address_answer(self):
        new_resp = self._register("fresh1877@example.com")
        self.assertEqual(new_resp.status_code, 201, new_resp.content[:300])
        self.assertEqual(new_resp.json(), ACCEPTED)
        users_before = User.objects.count()
        mail.outbox.clear()

        in_use_resp = self._register("Owner1877@Example.com")

        self._assert_same_answer(new_resp, in_use_resp)
        self.assertEqual(User.objects.count(), users_before)
        self.assertEqual(User.objects.filter(email__iexact="owner1877@example.com").count(), 1)
        self.assertFalse(EmailAddress.objects.filter(email__iexact="owner1877@example.com").exists())
        self._assert_owner_untouched()

    def test_verified_address_in_use_gets_the_new_address_answer(self):
        EmailAddress.objects.create(
            user=self.owner, email="owner1877@example.com", verified=True, primary=True
        )
        new_resp = self._register("fresh1877@example.com")
        in_use_resp = self._register("owner1877@example.com")

        self._assert_same_answer(new_resp, in_use_resp)
        self.assertEqual(EmailAddress.objects.filter(email__iexact="owner1877@example.com").count(), 1)
        self.assertEqual(User.objects.filter(email__iexact="owner1877@example.com").count(), 1)
        self._assert_owner_untouched()

    def test_address_equal_to_a_username_gets_the_new_address_answer(self):
        User.objects.create_user(username="name1877@example.com", email="", password=OWNER_PASSWORD)
        users_before = User.objects.count()
        new_resp = self._register("fresh1877@example.com")
        in_use_resp = self._register("name1877@example.com")

        self._assert_same_answer(new_resp, in_use_resp)
        self.assertEqual(User.objects.count(), users_before + 1)  # only the new address
        self.assertFalse(User.objects.filter(email__iexact="name1877@example.com").exists())

    def test_neither_registration_is_signed_in(self):
        for email in ("fresh1877@example.com", "owner1877@example.com"):
            client = APIClient()
            resp = self._register(email, client=client)
            self.assertEqual(resp.status_code, 201)
            self.assertIn(client.get(CURRENT_USER_URL).status_code, (401, 403))

    def test_owner_is_mailed_a_notice_and_new_address_a_confirmation(self):
        self._register("fresh1877@example.com")
        self.assertEqual(len(mail.outbox), 1)
        confirmation = mail.outbox[0]
        mail.outbox.clear()

        self._register("owner1877@example.com")

        self.assertEqual(len(mail.outbox), 1)
        notice = mail.outbox[0]
        self.assertEqual(notice.to, ["owner1877@example.com"])
        self.assertIn("Account Already Exists", notice.subject)
        self.assertNotEqual(notice.subject, confirmation.subject)

    def test_form_errors_are_the_same_for_both(self):
        for extra in ({"password2": "different-Passw0rd!"}, {"password1": "short", "password2": "short"}):
            new_resp = self._register("fresh1877@example.com", **extra)
            in_use_resp = self._register("owner1877@example.com", **extra)
            self.assertEqual(new_resp.status_code, 400)
            self._assert_same_answer(new_resp, in_use_resp)
        self.assertFalse(User.objects.filter(email="fresh1877@example.com").exists())
        self._assert_owner_untouched()

    def test_password_resembling_the_address_is_refused_for_both(self):
        # dj-rest-auth re-validates the password against the account's own
        # attributes after validation; the in-use path runs the same check.
        similar = "zebracorn1877x"  # also the local part of both addresses below
        weak = {"password1": similar, "password2": similar}
        new_resp = self._register("zebracorn1877x@example.com", **weak)
        User.objects.create_user(username="zc1877", email="zebracorn1877x@example.org", password=OWNER_PASSWORD)
        in_use_resp = self._register("zebracorn1877x@example.org", **weak)

        self.assertEqual(new_resp.status_code, 400, new_resp.content[:300])
        self._assert_same_answer(new_resp, in_use_resp)
        self.assertFalse(User.objects.filter(email="zebracorn1877x@example.com").exists())

    def test_closed_registration_refuses_both_the_same_way(self):
        _set_mode(SiteSetting.RegistrationMode.CLOSED)
        self.addCleanup(_set_mode, SiteSetting.RegistrationMode.OPEN)
        new_resp = self._register("fresh1877@example.com")
        in_use_resp = self._register("owner1877@example.com")

        self.assertEqual(new_resp.status_code, 403)
        self._assert_same_answer(new_resp, in_use_resp)
        self.assertEqual(mail.outbox, [])
        self._assert_owner_untouched()

    def test_throttle_counts_both_kinds_of_request(self):
        # RegisterAnonThrottle (10/min per IP in production) applies before
        # validation, so an address in use spends the same budget as a new one.
        client = APIClient()
        with patch.object(RegisterAnonThrottle, "get_rate", return_value="4/min"):
            for i in range(4):
                email = "owner1877@example.com" if i % 2 else f"fresh{i}-1877@example.com"
                resp = self._register(email, client=client)
                self.assertEqual(resp.status_code, 201, (i, resp.content[:300]))
            self.assertEqual(self._register("owner1877@example.com", client=client).status_code, 429)
            self.assertEqual(self._register("another1877@example.com", client=client).status_code, 429)

    def test_invite_registration_with_address_in_use_keeps_the_invite(self):
        _set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.addCleanup(_set_mode, SiteSetting.RegistrationMode.OPEN)
        admin = User.objects.create_user(username="admin1877", password=OWNER_PASSWORD, is_site_admin=True)
        link, raw = InviteLink.generate(created_by=admin, single_use=True)

        with self.captureOnCommitCallbacks(execute=True):
            in_use_resp = self._register("owner1877@example.com", invite_token=raw)
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertFalse(InviteLinkRedemption.objects.filter(invite_link=link).exists())
        # The notice is sent after the commit, like the confirmation (#1639).
        self.assertEqual([m.to for m in mail.outbox], [["owner1877@example.com"]])
        self._assert_owner_untouched()

        with self.captureOnCommitCallbacks(execute=True):
            new_resp = self._register("fresh1877@example.com", invite_token=raw)
        self._assert_same_answer(new_resp, in_use_resp)
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertTrue(User.objects.filter(email="fresh1877@example.com").exists())


@override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
class OptionalVerificationTests(_RegistrationMixin, TestCase):
    """A new registration is signed in here, so an address in use keeps its 400."""

    def test_setting_keeps_the_existing_answer(self):
        self.assertFalse(answers_in_use_address_as_new())

    def test_address_in_use_is_refused_and_nothing_changes(self):
        users_before = User.objects.count()
        resp = self._register("owner1877@example.com")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"email": ["User is already registered with this e-mail address."]})
        self.assertEqual(User.objects.count(), users_before)
        self.assertEqual(mail.outbox, [])
        self._assert_owner_untouched()

    def test_new_address_is_signed_in_with_a_token(self):
        resp = self._register("fresh1877@example.com")
        self.assertEqual(resp.status_code, 201)
        self.assertIn("key", resp.json())


@override_settings(ACCOUNT_EMAIL_VERIFICATION="mandatory")
class HtmlSignupFormTests(_RegistrationMixin, TestCase):
    """allauth's HTML signup form (``/accounts/signup/``) follows the same rule."""

    def _signup(self, email):
        return Client().post(
            "/accounts/signup/", {"email": email, "password1": PASSWORD, "password2": PASSWORD}
        )

    def test_address_in_use_gets_the_new_address_answer(self):
        User.objects.create_user(username="name1877@example.com", email="", password=OWNER_PASSWORD)
        new_resp = self._signup("fresh1877@example.com")
        self.assertEqual(new_resp.status_code, 302)
        users_before = User.objects.count()
        mail.outbox.clear()

        # An account's email (allauth's own check) and an account's username
        # (the extra match #1221 adds) are answered the same way.
        for email in ("Owner1877@example.com", "name1877@example.com"):
            with self.subTest(email=email):
                in_use_resp = self._signup(email)
                self.assertEqual(in_use_resp.status_code, new_resp.status_code)
                self.assertEqual(in_use_resp["Location"], new_resp["Location"])
        self.assertEqual(User.objects.count(), users_before)
        self.assertEqual(
            [[a.lower() for a in m.to] for m in mail.outbox], [["owner1877@example.com"], ["name1877@example.com"]]
        )
        self._assert_owner_untouched()

    @override_settings(ACCOUNT_EMAIL_VERIFICATION="optional")
    def test_optional_verification_keeps_the_form_error(self):
        User.objects.create_user(username="name1877@example.com", email="", password=OWNER_PASSWORD)
        resp = self._signup("name1877@example.com")
        self.assertEqual(resp.status_code, 200)  # re-rendered form
        self.assertFalse(User.objects.filter(email__iexact="name1877@example.com").exists())
