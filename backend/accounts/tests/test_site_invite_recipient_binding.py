"""Emailed site invites are bound to the invited address (#1703).

Same rule as emailed board and group invites (#1678; the shared helper is
``visiban/invite_recipient.py``): the send endpoint stores a keyed hash of the
address, and every redemption path checks it on the locked row before the
invite is consumed. Shareable links and emailed invites that predate the column
carry NULL and redeem as before.
"""

import re
import threading
from datetime import timedelta
from unittest import mock

from allauth.core.exceptions import ImmediateHttpResponse
from django.core import mail
from django.core.cache import cache
from django.db import connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from accounts.adapter import PENDING_INVITE_SESSION_KEY, SocialRegistrationAdapter
from accounts.invite_utils import (
    InviteTokenError,
    consume_invite_token,
    validate_invite_token,
)
from accounts.models import InviteLink, SiteSetting, User
from visiban.invite_recipient import (
    MISMATCH_CODE,
    REGISTRATION_MISMATCH_DETAIL,
    hash_recipient,
)

PASSWORD = "Sup3rS3cr3t!xyz"
REGISTER_URL = "/api/v1/auth/registration/"
SEND_URL = "/api/v1/admin/invite-links/send/"
INVITED = "Invitee.Person@Example.org"


def set_mode(mode):
    row = SiteSetting.get()
    row.registration_mode = mode
    row.save()
    cache.clear()


def make_admin(name="siteadmin"):
    return User.objects.create_user(
        username=name,
        password="p",
        email=f"{name}@acme.test",
        is_site_admin=True,
    )


def make_link(admin, *, recipient=INVITED, bound=True):
    """An emailed single-use site invite; ``bound=False`` mimics a pre-#1703 row."""
    link, raw = InviteLink.generate(
        created_by=admin,
        single_use=True,
        delivery=InviteLink.Delivery.EMAIL,
        expires_at=timezone.now() + timedelta(days=7),
        recipient_email=recipient,
    )
    if not bound:
        InviteLink.objects.filter(pk=link.pk).update(recipient_hash=None)
        link.recipient_hash = None
    return link, raw


class GenerateTests(TestCase):
    def test_emailed_invite_stores_only_the_hash(self):
        link, _raw = make_link(make_admin())
        self.assertEqual(link.recipient_hash, hash_recipient(INVITED))
        self.assertNotIn("invitee", link.recipient_hash.lower())

    def test_emailed_invite_without_an_address_is_refused(self):
        with self.assertRaises(ValueError):
            InviteLink.generate(
                created_by=make_admin(), delivery=InviteLink.Delivery.EMAIL
            )

    def test_shareable_link_is_unbound(self):
        link, _raw = InviteLink.generate(created_by=make_admin())
        self.assertIsNone(link.recipient_hash)


@override_settings(
    INVITE_EMAIL_ENABLED=True, DEMO_MODE=False, FRONTEND_URL="https://kanban.acme.test"
)
class SendBindsInviteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = make_admin()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def tearDown(self):
        cache.clear()

    def test_send_binds_invite_and_hash_never_leaves_the_server(self):
        r = self.client.post(SEND_URL, {"email": INVITED}, format="json")
        self.assertEqual(r.status_code, status.HTTP_202_ACCEPTED, r.content)
        link = InviteLink.objects.get(delivery=InviteLink.Delivery.EMAIL)
        self.assertEqual(link.recipient_hash, hash_recipient(INVITED))
        self.assertNotIn(link.recipient_hash, r.content.decode())
        listing = self.client.get("/api/v1/admin/invite-links/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertNotIn("recipient_hash", listing.content.decode())
        self.assertNotIn(link.recipient_hash, listing.content.decode())
        # The emailed token is the one stored.
        raw = re.search(r"vbnl_[0-9a-f]{40}", mail.outbox[0].body).group(0)
        self.assertEqual(raw[:8], link.prefix)

    def test_shareable_link_creation_stays_unbound(self):
        r = self.client.post(
            "/api/v1/admin/invite-links/", {"single_use": True}, format="json"
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED, r.content)
        self.assertIsNone(InviteLink.objects.get().recipient_hash)


class RegistrationBindingTests(TestCase):
    def setUp(self):
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.admin = make_admin()

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)

    def _register(self, email, raw):
        return APIClient().post(
            REGISTER_URL,
            {
                "email": email,
                "password1": PASSWORD,
                "password2": PASSWORD,
                "invite_token": raw,
            },
        )

    def test_matching_signup_address_registers_and_consumes(self):
        link, raw = make_link(self.admin)
        r = self._register("invitee.PERSON@example.org", raw)
        self.assertIn(r.status_code, (201, 204), r.content)
        self.assertTrue(User.objects.filter(email__iexact=INVITED).exists())
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)

    def test_non_matching_signup_address_is_refused_and_not_consumed(self):
        link, raw = make_link(self.admin)
        r = self._register("someone.else@example.org", raw)
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST, r.content)
        self.assertEqual(
            r.json(), {"email": [REGISTRATION_MISMATCH_DETAIL], "code": MISMATCH_CODE}
        )
        self.assertFalse(User.objects.filter(email="someone.else@example.org").exists())
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertEqual(link.use_count, 0)
        # The person it was sent to can still use it.
        self.assertIn(self._register(INVITED, raw).status_code, (201, 204))

    def test_legacy_unbound_emailed_invite_registers_any_address(self):
        _link, raw = make_link(self.admin, bound=False)
        self.assertIn(self._register("anyone@example.org", raw).status_code, (201, 204))

    def test_shareable_link_registers_any_address(self):
        _link, raw = InviteLink.generate(created_by=self.admin)
        self.assertIn(self._register("anyone@example.org", raw).status_code, (201, 204))


class OAuthBindingTests(TestCase):
    def setUp(self):
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.admin = make_admin()

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)

    def _save(self, raw, username, email):
        request = RequestFactory().get("/accounts/google/login/callback/")
        request.session = {PENDING_INVITE_SESSION_KEY: raw}

        def create(*args, **kwargs):
            return User.objects.create_user(
                username=username, password="p", email=email
            )

        with mock.patch.object(
            SocialRegistrationAdapter.__bases__[0], "save_user", side_effect=create
        ):
            return SocialRegistrationAdapter().save_user(
                request, mock.MagicMock(), form=None
            )

    def test_matching_provider_address_signs_up(self):
        link, raw = make_link(self.admin)
        user = self._save(raw, "oauth_ok", "INVITEE.person@example.org")
        self.assertEqual(user.username, "oauth_ok")
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)

    def test_non_matching_provider_address_is_refused_and_rolled_back(self):
        link, raw = make_link(self.admin)
        with self.assertRaises(ImmediateHttpResponse) as ctx:
            self._save(raw, "oauth_bad", "elsewhere@example.org")
        self.assertIn(f"auth_error={MISMATCH_CODE}", ctx.exception.response.url)
        self.assertFalse(User.objects.filter(username="oauth_bad").exists())
        link.refresh_from_db()
        self.assertIsNone(link.used_at)

    def test_legacy_unbound_invite_signs_up_any_address(self):
        _link, raw = make_link(self.admin, bound=False)
        self.assertEqual(
            self._save(raw, "oauth_legacy", "anyone@example.org").username,
            "oauth_legacy",
        )


class ConsumeInviteTokenTests(TestCase):
    """The check lives in ``consume_invite_token`` itself, so any caller gets it."""

    def setUp(self):
        self.admin = make_admin()

    def test_mismatch_consumes_nothing(self):
        link, raw = make_link(self.admin)
        stranger = User.objects.create_user(
            username="stranger", password="p", email="s@example.org"
        )
        with transaction.atomic():
            locked = validate_invite_token(raw)
            with self.assertRaises(InviteTokenError) as ctx:
                consume_invite_token(locked, email=stranger.email, user=stranger)
        self.assertEqual(ctx.exception.code, MISMATCH_CODE)
        link.refresh_from_db()
        self.assertIsNone(link.used_at)
        self.assertEqual(link.use_count, 0)

    def test_match_consumes(self):
        link, raw = make_link(self.admin)
        invitee = User.objects.create_user(
            username="invitee", password="p", email=INVITED.lower()
        )
        with transaction.atomic():
            consume_invite_token(
                validate_invite_token(raw), email=invitee.email, user=invitee
            )
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)
        self.assertEqual(link.use_count, 1)

    def test_bound_invite_without_a_user_fails_closed(self):
        link, raw = make_link(self.admin)
        with transaction.atomic():
            with self.assertRaises(InviteTokenError) as ctx:
                consume_invite_token(validate_invite_token(raw), email=INVITED)
        self.assertEqual(ctx.exception.code, MISMATCH_CODE)
        link.refresh_from_db()
        self.assertIsNone(link.used_at)

    def test_unbound_invite_without_a_user_is_unchanged(self):
        link, raw = make_link(self.admin, bound=False)
        with transaction.atomic():
            consume_invite_token(validate_invite_token(raw))
        link.refresh_from_db()
        self.assertIsNotNone(link.used_at)


class BoundSiteInviteRaceTests(TransactionTestCase):
    """The address check sits inside the locked claim: two simultaneous
    redemptions of one bound invite yield exactly one success."""

    def setUp(self):
        if connection.vendor == "sqlite":
            self.skipTest(
                "SQLite does not support SELECT FOR UPDATE; this test requires PostgreSQL."
            )
        cache.clear()
        set_mode(SiteSetting.RegistrationMode.INVITE_ONLY)
        self.link, self.raw = make_link(make_admin("race_admin"))

    def tearDown(self):
        set_mode(SiteSetting.RegistrationMode.OPEN)
        cache.clear()

    def _race(self, emails):
        results = []
        barrier = threading.Barrier(len(emails))

        def run(email):
            from django.db import connections as _conns

            try:
                barrier.wait()
                r = APIClient().post(
                    REGISTER_URL,
                    {
                        "email": email,
                        "password1": PASSWORD,
                        "password2": PASSWORD,
                        "invite_token": self.raw,
                    },
                )
                results.append((email, r.status_code))
            finally:
                _conns.close_all()

        threads = [threading.Thread(target=run, args=(e,)) for e in emails]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_two_registrations_with_the_invited_address_yield_one_success(self):
        # Distinct usernames are derived from the email, so the two attempts
        # differ only by case; the second must find the invite consumed.
        results = self._race([INVITED, INVITED.lower()])
        successes = [s for _e, s in results if s in (201, 204)]
        self.assertEqual(len(successes), 1, results)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.used_at)
        self.assertEqual(self.link.use_count, 1)

    def test_invited_address_wins_against_another_address(self):
        results = self._race([INVITED, "other@example.org"])
        outcome = dict(results)
        self.assertIn(outcome[INVITED], (201, 204), results)
        self.assertEqual(outcome["other@example.org"] // 100, 4, results)
        self.assertFalse(User.objects.filter(email="other@example.org").exists())
        self.link.refresh_from_db()
        self.assertEqual(self.link.use_count, 1)
