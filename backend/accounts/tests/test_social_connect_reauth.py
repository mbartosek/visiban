"""Connecting a sign-in provider: recent authentication and the no-password notice (#1710).

- An account with a usable password connects a new provider identity only
  after a recent authentication (allauth's ``did_recently_authenticate``);
  otherwise the callback redirects to ``/accounts/reauthenticate/`` with
  ``next`` pointing back at the provider's connect start, and nothing is
  linked.
- An account without a usable password connects as before, and its primary
  address is sent a fixed-text notice after commit.
- Signing in with a provider (``process=login``) is unaffected.

Exercised at the ``pre_social_login`` / ``complete_login`` level (a real IdP
round trip can't run in a test), plus the reauthenticate page itself through
the test client.
"""
from urllib.parse import parse_qs, urlparse

from allauth.account.internal.flows.login import (
    AUTHENTICATION_METHODS_SESSION_KEY,
    record_authentication,
)
from allauth.account.models import EmailAddress
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import get_adapter
from allauth.socialaccount.internal.flows.login import complete_login, pre_social_login
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.db import transaction
from django.test import RequestFactory, TestCase, override_settings
from unittest import mock

from accounts.models import User
from accounts.social_connect import stash_pending_connect

FRONTEND = "https://app.example.com"
PASSWORD = "testpassword123"
PROVIDERS = {
    "google": {"APP": {"client_id": "g-id", "secret": "g-secret"}},
    "github": {"APP": {"client_id": "gh-id", "secret": "gh-secret"}},
}
REAUTH_PATH = "/accounts/reauthenticate/"
GITHUB_CONNECT_START = "/accounts/github/login/?process=connect"
VERIFICATION_MODES = ("none", "optional", "mandatory")
NOTICE_SUBJECT = "A sign-in method was added to your"


def _request(user=None, session=None):
    request = RequestFactory().get("/accounts/github/login/callback/")
    if session is None:
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
    else:
        request.session = session
    request.user = user or AnonymousUser()
    request._messages = FallbackStorage(request)
    return request


def _recently_authenticated(user):
    request = _request(user)
    record_authentication(request, user, method="password", reauthenticated=True)
    return request


def _sociallogin(request, email="someone@example.com", *, uid="gh-1", process="connect"):
    """An unsaved SocialLogin as allauth builds one mid-callback."""
    prov = get_adapter().get_provider(request, "github")
    account = SocialAccount(provider="github", uid=uid, extra_data={})
    addresses = [EmailAddress(email=email, verified=True, primary=True)]
    sociallogin = SocialLogin(user=User(username="", email=email), account=account, email_addresses=addresses)
    sociallogin.provider = prov
    sociallogin.state = {"process": process}
    return sociallogin


def _split(location):
    parsed = urlparse(location)
    return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}


@override_settings(LOGIN_REDIRECT_URL=FRONTEND, SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class PasswordAccountConnectTests(TestCase):
    """Accounts with a usable password: a recent authentication is required."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice", email="alice@example.com", password=PASSWORD)
        self.bob = User.objects.create_user(username="bob", email="bob@example.com", password=PASSWORD)

    def test_without_recent_authentication_redirects_to_reauthenticate_and_links_nothing(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                request = _request(self.alice)
                with self.captureOnCommitCallbacks(execute=True):
                    response = complete_login(request, _sociallogin(request))

                path, params = _split(response["Location"])
                self.assertEqual(path, REAUTH_PATH)
                self.assertEqual(params, {"next": GITHUB_CONNECT_START})
                self.assertFalse(SocialAccount.objects.filter(user=self.alice).exists())
                self.assertEqual(mail.outbox, [])

    def test_stale_authentication_is_not_recent(self):
        request = _recently_authenticated(self.alice)
        records = request.session[AUTHENTICATION_METHODS_SESSION_KEY]
        records[-1]["at"] -= 301  # ACCOUNT_REAUTHENTICATION_TIMEOUT defaults to 300s
        request.session[AUTHENTICATION_METHODS_SESSION_KEY] = records

        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, _sociallogin(request))

        self.assertEqual(_split(ctx.exception.response["Location"])[0], REAUTH_PATH)

    def test_with_recent_authentication_connects(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                SocialAccount.objects.filter(user=self.alice).delete()
                request = _recently_authenticated(self.alice)
                with self.captureOnCommitCallbacks(execute=True):
                    response = complete_login(request, _sociallogin(request))

                self.assertEqual(_split(response["Location"]), ("/settings", {"connected": "github"}))
                self.assertTrue(SocialAccount.objects.filter(user=self.alice, provider="github").exists())
                # The password confirmation is the safeguard here; no notice.
                self.assertEqual(mail.outbox, [])

    def test_openid_connect_retry_points_at_its_own_login_path(self):
        request = _request(self.alice)
        sociallogin = _sociallogin(request)
        sociallogin.provider = mock.Mock()
        sociallogin.provider.get_login_url.return_value = "/accounts/oidc/oidc/login/?process=connect"

        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, sociallogin)

        sociallogin.provider.get_login_url.assert_called_once_with(request, process="connect")
        self.assertEqual(
            _split(ctx.exception.response["Location"]),
            (REAUTH_PATH, {"next": "/accounts/oidc/oidc/login/?process=connect"}),
        )

    @override_settings(SOCIALACCOUNT_EMAIL_AUTHENTICATION=True)
    def test_email_authentication_match_is_still_a_new_link(self):
        """With email authentication on, lookup sets the user by email but the
        identity row is unsaved: connecting it still needs a recent authentication."""
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                request = _request(self.alice)
                sociallogin = _sociallogin(request, email="alice@example.com")
                with self.captureOnCommitCallbacks(execute=True):
                    response = complete_login(request, sociallogin)

                self.assertEqual(sociallogin.user.pk, self.alice.pk)  # matched by email
                self.assertIsNone(sociallogin.account.pk)
                path, params = _split(response["Location"])
                self.assertEqual(path, REAUTH_PATH)
                self.assertEqual(params, {"next": GITHUB_CONNECT_START})
                self.assertFalse(SocialAccount.objects.filter(user=self.alice).exists())

    def test_identity_already_linked_to_this_account_is_not_asked(self):
        """Nothing is added, so there is nothing to confirm."""
        SocialAccount.objects.create(user=self.alice, provider="github", uid="gh-1", extra_data={})
        request = _request(self.alice)

        self.assertIsNone(pre_social_login(request, _sociallogin(request)))

    def test_identity_linked_to_another_account_is_refused_without_asking(self):
        SocialAccount.objects.create(user=self.bob, provider="github", uid="gh-1", extra_data={})
        request = _request(self.alice)

        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, _sociallogin(request))

        self.assertEqual(
            _split(ctx.exception.response["Location"]),
            ("/settings", {"connect_error": "provider_already_connected", "provider": "github"}),
        )

    def test_reauthentication_redirect_keeps_the_pending_prompt_binding(self):
        """The prompt's identity check still applies to the connect after reauthentication."""
        request = _request(self.alice)
        stash_pending_connect(request, "github", [self.alice.pk], identity="alice-gh", uid="gh-attempt")

        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, _sociallogin(request, uid="gh-attempt"))
        self.assertEqual(_split(ctx.exception.response["Location"])[0], REAUTH_PATH)

        record_authentication(request, self.alice, method="password", reauthenticated=True)
        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, _sociallogin(request, uid="gh-other"))
        self.assertEqual(
            _split(ctx.exception.response["Location"]),
            ("/settings", {"connect_error": "connect_identity_mismatch", "provider": "github"}),
        )

    def test_reauthenticate_page_returns_to_the_connect_start_and_unlocks_the_connect(self):
        self.client.force_login(self.alice)
        next_query = "next=%2Faccounts%2Fgithub%2Flogin%2F%3Fprocess%3Dconnect"

        wrong = self.client.post(f"{REAUTH_PATH}?{next_query}", {"password": "wrong-one"})
        self.assertNotEqual(wrong.get("Location"), GITHUB_CONNECT_START)

        response = self.client.post(f"{REAUTH_PATH}?{next_query}", {"password": PASSWORD})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], GITHUB_CONNECT_START)
        # The connect start is allauth's confirm-and-continue page, not a dead end.
        self.assertEqual(self.client.get(GITHUB_CONNECT_START).status_code, 200)

        # The callback that follows, on the same session, passes the gate.
        session = SessionStore(session_key=self.client.session.session_key)
        request = _request(self.alice, session=session)
        with self.captureOnCommitCallbacks(execute=True):
            response = complete_login(request, _sociallogin(request))
        self.assertEqual(_split(response["Location"]), ("/settings", {"connected": "github"}))

    def test_rest_api_login_alone_does_not_count_as_recent(self):
        client = self.client_class()
        login = client.post(
            "/api/v1/auth/login/", {"username": "alice", "password": PASSWORD}, content_type="application/json"
        )
        self.assertEqual(login.status_code, 200)
        session = SessionStore(session_key=client.session.session_key)
        request = _request(self.alice, session=session)

        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, _sociallogin(request))

        self.assertEqual(_split(ctx.exception.response["Location"])[0], REAUTH_PATH)


@override_settings(LOGIN_REDIRECT_URL=FRONTEND, SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class PasswordlessAccountConnectTests(TestCase):
    """Accounts without a usable password: connect proceeds, the primary address is told."""

    def setUp(self):
        self.carol = User.objects.create_user(username="carol", email="carol@example.com")
        self.carol.set_unusable_password()
        self.carol.save()
        SocialAccount.objects.create(user=self.carol, provider="google", uid="g-1", extra_data={})

    def test_connects_without_reauthentication_and_notifies_the_primary_address(self):
        for mode in VERIFICATION_MODES:
            with self.subTest(mode=mode), override_settings(ACCOUNT_EMAIL_VERIFICATION=mode):
                SocialAccount.objects.filter(user=self.carol, provider="github").delete()
                mail.outbox.clear()
                request = _request(self.carol)
                with self.captureOnCommitCallbacks(execute=True):
                    response = complete_login(request, _sociallogin(request))

                self.assertEqual(_split(response["Location"]), ("/settings", {"connected": "github"}))
                self.assertTrue(SocialAccount.objects.filter(user=self.carol, provider="github").exists())
                self.assertEqual(len(mail.outbox), 1)
                message = mail.outbox[0]
                self.assertEqual(message.to, ["carol@example.com"])
                self.assertTrue(message.subject.startswith(NOTICE_SUBJECT))

    def test_notice_goes_to_the_primary_email_address_row_only(self):
        EmailAddress.objects.create(user=self.carol, email="carol-primary@example.com", primary=True, verified=True)
        EmailAddress.objects.create(user=self.carol, email="carol-other@example.com", primary=False, verified=True)
        request = _request(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            complete_login(request, _sociallogin(request))

        self.assertEqual([m.to for m in mail.outbox], [["carol-primary@example.com"]])

    def test_notice_is_fixed_text(self):
        request = _request(self.carol)
        with self.captureOnCommitCallbacks(execute=True):
            complete_login(request, _sociallogin(request, email="idp-name@example.com", uid="gh-identity-42"))

        body = mail.outbox[0].body + mail.outbox[0].subject
        for value in ("github", "GitHub", "gh-identity-42", "idp-name@example.com", "carol", "http"):
            self.assertNotIn(value, body)

    def test_rolled_back_connect_sends_nothing(self):
        request = _request(self.carol)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    complete_login(request, _sociallogin(request))
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass

        self.assertEqual(callbacks, [])
        self.assertEqual(mail.outbox, [])
        self.assertFalse(SocialAccount.objects.filter(user=self.carol, provider="github").exists())

    def test_mail_failure_does_not_raise_and_logs_no_address(self):
        request = _request(self.carol)
        with mock.patch("django.core.mail.EmailMessage.send", side_effect=OSError("smtp down")), \
                self.assertLogs("accounts.social_connect", level="WARNING") as logs, \
                self.captureOnCommitCallbacks(execute=True):
            response = complete_login(request, _sociallogin(request))

        self.assertEqual(_split(response["Location"]), ("/settings", {"connected": "github"}))
        output = "\n".join(logs.output)
        self.assertIn(f"user_id={self.carol.pk}", output)
        self.assertIn("exc=OSError", output)
        self.assertNotIn("carol@example.com", output)

    def test_no_notice_when_a_password_was_set_before_commit(self):
        request = _request(self.carol)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            complete_login(request, _sociallogin(request))
        self.carol.set_password(PASSWORD)
        self.carol.save(update_fields=["password"])
        for callback in callbacks:
            callback()

        self.assertEqual(mail.outbox, [])


@override_settings(LOGIN_REDIRECT_URL=FRONTEND, SOCIALACCOUNT_PROVIDERS=PROVIDERS)
class SignInUnaffectedTests(TestCase):
    """process=login never asks for reauthentication and never sends the notice."""

    def setUp(self):
        self.alice = User.objects.create_user(username="alice", email="alice@example.com", password=PASSWORD)

    def test_sign_in_with_a_linked_identity_is_not_asked(self):
        SocialAccount.objects.create(user=self.alice, provider="github", uid="gh-1", extra_data={})
        request = _request()

        self.assertIsNone(pre_social_login(request, _sociallogin(request, process="login")))

    def test_email_collision_routing_is_unchanged(self):
        request = _request()

        with self.assertRaises(ImmediateHttpResponse) as ctx:
            pre_social_login(request, _sociallogin(request, email="alice@example.com", process="login"))

        _, params = _split(ctx.exception.response["Location"])
        self.assertEqual(params.get("auth_error"), "account_exists")
        self.assertFalse(SocialAccount.objects.filter(provider="github").exists())

    def test_social_signup_sends_no_connect_notice(self):
        request = _request()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                complete_login(request, _sociallogin(request, email="new@example.com", process="login"))
            except Exception:  # noqa: BLE001 - only the absence of the notice matters here
                pass

        self.assertFalse(any(m.subject.startswith(NOTICE_SUBJECT) for m in mail.outbox))
