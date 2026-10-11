"""Tests for the provision_fuzz_token management command (#1080).

Covers the guardrail that matters most: the command must refuse to mint a
token for a privileged account, because backend-schema-fuzz is required to
authenticate as a normal board member, not a superuser/site-admin.
"""
import os
import stat
import tempfile
from io import StringIO

from django.core.management import call_command, CommandError
from django.test import TestCase

from accounts.models import PAT_DEFAULT_SCOPES, PersonalAccessToken, User


class ProvisionFuzzTokenTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="demo2", password="unused")
        self.tmp_dir = tempfile.mkdtemp()
        self.token_file = os.path.join(self.tmp_dir, "token")

    def test_mints_scoped_token_for_normal_member(self):
        out = StringIO()
        call_command(
            "provision_fuzz_token",
            "--username",
            "demo2",
            "--token-file",
            self.token_file,
            "--set-random-password",
            stdout=out,
        )

        pat = PersonalAccessToken.objects.get(user=self.member)
        self.assertEqual(pat.scopes, PAT_DEFAULT_SCOPES)
        self.assertEqual(pat.name, "backend-schema-fuzz (CI)")

        with open(self.token_file) as fh:
            raw_token = fh.read().strip()
        self.assertTrue(raw_token.startswith("vbn_"))
        self.assertIn("Minted", out.getvalue())

    def test_token_file_is_not_world_or_group_readable(self):
        call_command(
            "provision_fuzz_token",
            "--username",
            "demo2",
            "--token-file",
            self.token_file,
            "--set-random-password",
        )
        mode = stat.S_IMODE(os.stat(self.token_file).st_mode)
        self.assertEqual(mode, 0o600)

    def test_social_only_member_gets_unguessable_password(self):
        """#1607: a passwordless member must not let the fuzzer revoke its own token.

        Both password-change routes skip the current-password check for a
        social-only account, and a successful change revokes every PAT — so the
        deep fuzz run 401ed for the rest of its budget. With --set-random-password
        the command gives such an account a random password, and the change route
        then rejects a body without the right current password, leaving the token
        alive.
        """
        social = User.objects.create_user(username="social")
        social.set_unusable_password()
        social.save()
        out = StringIO()
        call_command(
            "provision_fuzz_token",
            "--username",
            "social",
            "--token-file",
            self.token_file,
            "--set-random-password",
            stdout=out,
        )
        social.refresh_from_db()
        self.assertTrue(social.has_usable_password())
        self.assertIn("random, unrecorded password", out.getvalue())

        with open(self.token_file) as fh:
            raw_token = fh.read().strip()
        new_password = "Correct-Horse-Battery-1607"
        for url, body, why in (
            (
                "/api/v1/auth/password/change/",
                {"new_password1": new_password, "new_password2": new_password},
                b"old_password",
            ),
            (
                "/api/v1/auth/change-password/",
                {"new_password": new_password},
                b"Current password is incorrect",
            ),
        ):
            resp = self.client.post(
                url,
                body,
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Token {raw_token}",
            )
            # Rejected for the missing/wrong current password specifically.
            self.assertEqual(resp.status_code, 400, (url, resp.content))
            self.assertIn(why, resp.content)
        self.assertTrue(PersonalAccessToken.objects.filter(user=social).exists())
        resp = self.client.get(
            "/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Token {raw_token}"
        )
        self.assertEqual(resp.status_code, 200)

    def test_passwordless_member_untouched_without_flag(self):
        """The password write is opt-in (#1607 security review).

        On a real social-only account a usable password would lift the
        verified-email gate on password reset, so without the flag the command
        must leave a password-less user password-less.
        """
        social = User.objects.create_user(username="social")
        social.set_unusable_password()
        social.save()
        out = StringIO()
        call_command(
            "provision_fuzz_token",
            "--username",
            "social",
            "--token-file",
            self.token_file,
            stdout=out,
        )
        social.refresh_from_db()
        self.assertFalse(social.has_usable_password())
        self.assertTrue(PersonalAccessToken.objects.filter(user=social).exists())
        self.assertNotIn("random, unrecorded password", out.getvalue())

    def test_without_flag_a_password_change_revokes_the_token(self):
        """The #1607 failure mode, which --set-random-password exists to prevent.

        A password-less user's change route needs no current password, and the
        change revokes every PAT, so the fuzz token locks itself out.
        """
        social = User.objects.create_user(username="social")
        social.set_unusable_password()
        social.save()
        call_command(
            "provision_fuzz_token",
            "--username",
            "social",
            "--token-file",
            self.token_file,
            stdout=StringIO(),
        )
        with open(self.token_file) as fh:
            auth = f"Token {fh.read().strip()}"
        new_password = "Correct-Horse-Battery-1607"
        resp = self.client.post(
            "/api/v1/auth/password/change/",
            {"new_password1": new_password, "new_password2": new_password},
            content_type="application/json",
            HTTP_AUTHORIZATION=auth,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(PersonalAccessToken.objects.filter(user=social).exists())
        resp = self.client.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=auth)
        self.assertEqual(resp.status_code, 401)

    def test_existing_password_is_left_alone_with_or_without_flag(self):
        before = User.objects.get(pk=self.member.pk).password
        for flags in ([], ["--set-random-password"]):
            with self.subTest(flags=flags):
                token_file = os.path.join(self.tmp_dir, f"token{len(flags)}")
                out = StringIO()
                call_command(
                    "provision_fuzz_token",
                    "--username",
                    "demo2",
                    "--token-file",
                    token_file,
                    *flags,
                    stdout=out,
                )
                self.assertEqual(User.objects.get(pk=self.member.pk).password, before)
                self.assertNotIn("random, unrecorded password", out.getvalue())

    def test_refuses_missing_user(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_fuzz_token",
                "--username",
                "nobody",
                "--token-file",
                self.token_file,
            )
        self.assertIn("does not exist", str(ctx.exception))

    def test_refuses_superuser(self):
        User.objects.create_superuser(username="admin2", password="unused", email="admin2@example.com")
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_fuzz_token",
                "--username",
                "admin2",
                "--token-file",
                self.token_file,
            )
        self.assertIn("elevated privileges", str(ctx.exception))

    def test_refuses_site_admin(self):
        User.objects.create_user(username="siteadmin", password="unused", is_site_admin=True)
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_fuzz_token",
                "--username",
                "siteadmin",
                "--token-file",
                self.token_file,
            )
        self.assertIn("elevated privileges", str(ctx.exception))

    def test_refuses_can_access_all_content(self):
        User.objects.create_user(username="poweruser", password="unused", can_access_all_content=True)
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "provision_fuzz_token",
                "--username",
                "poweruser",
                "--token-file",
                self.token_file,
            )
        self.assertIn("elevated privileges", str(ctx.exception))
