"""Tests for ensure_site_admin and set_site_admin management commands (#407)."""
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase

from rest_framework.authtoken.models import Token

from accounts.models import PersonalAccessToken, User


class EnsureSiteAdminTests(TestCase):
    """ensure_site_admin creates a site admin on first run and is idempotent on subsequent runs."""

    def test_creates_admin_when_none_exists(self):
        """Running ensure_site_admin with no site admins creates one."""
        self.assertFalse(User.objects.filter(is_site_admin=True).exists())

        out = StringIO()
        call_command("ensure_site_admin", stdout=out)

        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_site_admin)
        self.assertTrue(admin.can_access_all_content)
        self.assertTrue(admin.is_staff)
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.must_change_password)

    def test_noop_when_admin_already_exists(self):
        """Running ensure_site_admin when a site admin exists does nothing."""
        User.objects.create_user(username="existing", password="pass", is_site_admin=True)

        out = StringIO()
        call_command("ensure_site_admin", stdout=out)

        # No new admin was created
        self.assertFalse(User.objects.filter(username="admin").exists())

    @patch.dict(os.environ, {"DJANGO_SUPERUSER_USERNAME": "boss", "DJANGO_SUPERUSER_EMAIL": "boss@example.com"})
    def test_respects_env_vars_for_username_and_email(self):
        out = StringIO()
        call_command("ensure_site_admin", stdout=out)

        admin = User.objects.get(username="boss")
        self.assertEqual(admin.email, "boss@example.com")
        self.assertTrue(admin.is_site_admin)

    def test_promotes_existing_user_if_username_matches(self):
        """If a non-admin user with the target username exists, promote rather than duplicate."""
        existing = User.objects.create_user(username="admin", password="oldpass")
        self.assertFalse(existing.is_site_admin)

        out = StringIO()
        call_command("ensure_site_admin", stdout=out)

        existing.refresh_from_db()
        self.assertTrue(existing.is_site_admin)
        self.assertTrue(existing.must_change_password)
        # Exactly one user with this username
        self.assertEqual(User.objects.filter(username="admin").count(), 1)

    def test_promoting_existing_user_revokes_issued_credentials(self):
        """The password reset of an existing account follows the standard credential-change path (#1596)."""
        existing = User.objects.create_user(username="admin", password="oldpass")
        old_key = Token.objects.create(user=existing).key
        PersonalAccessToken.generate(existing, "ci")
        self.assertEqual(existing.personal_access_tokens.count(), 1)

        call_command("ensure_site_admin", stdout=StringIO())

        existing.refresh_from_db()
        self.assertFalse(Token.objects.filter(key=old_key).exists())
        self.assertFalse(Token.objects.filter(user=existing).exists())
        self.assertEqual(existing.personal_access_tokens.count(), 0)
        self.assertFalse(existing.check_password("oldpass"))
        self.assertTrue(existing.must_change_password)
        self.assertTrue(existing.is_site_admin)

    def test_new_account_branch_leaves_other_users_credentials_alone(self):
        """Creating the account does not touch credentials that belong to other users."""
        other = User.objects.create_user(username="someone", password="pass")
        Token.objects.create(user=other)
        PersonalAccessToken.generate(other, "keep")

        call_command("ensure_site_admin", stdout=StringIO())

        admin = User.objects.get(username="admin")
        self.assertTrue(admin.is_site_admin)
        self.assertTrue(admin.must_change_password)
        self.assertTrue(Token.objects.filter(user=other).exists())
        self.assertEqual(other.personal_access_tokens.count(), 1)

    def test_writes_password_to_file(self):
        """The generated password is written to a file, not printed to stdout."""
        pw_fd, pw_file = tempfile.mkstemp(prefix="visiban_test_admin_pw_")
        os.close(pw_fd)
        os.unlink(pw_file)  # remove so the command creates it fresh
        try:
            with patch("accounts.management.commands.ensure_site_admin._PASSWORD_FILE", pw_file):
                out = StringIO()
                call_command("ensure_site_admin", stdout=out)
            self.assertTrue(os.path.exists(pw_file))
            with open(pw_file) as f:
                password = f.read().strip()
            self.assertGreater(len(password), 10)
            # File must be created with 0o600 permissions atomically (TOCTOU fix)
            self.assertEqual(oct(os.stat(pw_file).st_mode & 0o777), oct(0o600))
            # Password should not appear in stdout
            self.assertNotIn(password, out.getvalue())
        finally:
            if os.path.exists(pw_file):
                os.remove(pw_file)

    def _run_with_pw_path(self, path):
        out = StringIO()
        with patch("accounts.management.commands.ensure_site_admin._PASSWORD_FILE", path):
            call_command("ensure_site_admin", stdout=out)
        return out.getvalue()

    def test_password_file_replaces_stale_own_file_with_0600(self):
        """A stale own file (even world-readable) is replaced by a fresh 0600 file (S5443, #1379)."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pw")
            with open(path, "w") as f:
                f.write("old\n")
            # Deliberately world-writable: the stale file the helper must replace.
            os.chmod(path, 0o666)  # nosec B103
            output = self._run_with_pw_path(path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with open(path) as f:
                self.assertNotEqual(f.read().strip(), "old")
            self.assertIn("REDACTED", output)

    def test_password_file_refuses_planted_symlink(self):
        """A symlink planted at the path is never followed or removed; password goes to stdout."""
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "victim")
            path = os.path.join(d, "pw")
            os.symlink(target, path)
            output = self._run_with_pw_path(path)
            self.assertFalse(os.path.exists(target))
            self.assertTrue(os.path.islink(path))
            self.assertNotIn("REDACTED", output)

    def test_password_file_refuses_file_owned_by_another_user(self):
        """A pre-existing file not owned by us is not reused or unlinked (fail closed to stdout)."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "pw")
            with open(path, "w") as f:
                f.write("attacker\n")
            with patch("accounts.management._secure_file.os.geteuid", return_value=os.geteuid() + 1):
                output = self._run_with_pw_path(path)
            with open(path) as f:
                self.assertEqual(f.read(), "attacker\n")
            self.assertNotIn("REDACTED", output)

    def test_password_file_refuses_directory(self):
        """A directory at the path is never unlinked; password goes to stdout."""
        with tempfile.TemporaryDirectory() as d:
            output = self._run_with_pw_path(d)
            self.assertTrue(os.path.isdir(d))
            self.assertNotIn("REDACTED", output)

    def test_password_printed_to_stdout_when_file_write_fails(self):
        """When the password file cannot be written, the password is printed directly to stdout.

        This covers the OSError fallback path: the banner must contain the actual
        credential (so the operator can retrieve it) and must NOT contain "REDACTED"
        (which would be useless if the file write is the only delivery mechanism).
        """
        # Patch _PASSWORD_FILE to a directory path so open(..., "w") raises OSError.
        with patch(
            "accounts.management.commands.ensure_site_admin._PASSWORD_FILE",
            "/",  # opening a directory for writing always raises OSError
        ):
            out = StringIO()
            call_command("ensure_site_admin", stdout=out)

        output = out.getvalue()
        # The generated password must appear somewhere in the banner.
        # We cannot know the exact value, but we can confirm the banner does NOT
        # fall back to the REDACTED placeholder — that only appears on the success path.
        self.assertNotIn("REDACTED", output)
        # The banner heading must still be present so the operator notices the output.
        self.assertIn("VISIBAN INITIAL ADMIN CREDENTIALS", output)
        # The admin user must have been created despite the file write failure.
        self.assertTrue(User.objects.filter(username="admin", is_site_admin=True).exists())


class SetSiteAdminTests(TestCase):
    """set_site_admin grants or revokes site admin status for an existing user."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_grant_site_admin(self):
        out = StringIO()
        call_command("set_site_admin", "testuser", stdout=out)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_site_admin)
        self.assertTrue(self.user.can_access_all_content)
        self.assertIn("Granted", out.getvalue())

    def test_revoke_site_admin(self):
        self.user.is_site_admin = True
        self.user.can_access_all_content = True
        self.user.save()

        out = StringIO()
        call_command("set_site_admin", "testuser", "--revoke", stdout=out)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_site_admin)
        self.assertFalse(self.user.can_access_all_content)
        self.assertIn("Revoked", out.getvalue())

    def test_revoke_lapses_group_links_held_only_through_the_flag(self):
        """--revoke clears can_access_all_content, so group invite links the
        user administered only through it lapse; links on a group the user
        still administers by membership are kept (same as the admin API)."""
        from groups.models import Group, GroupInviteLink, GroupMembership

        User.objects.filter(pk=self.user.pk).update(is_site_admin=True, can_access_all_content=True)
        owner = User.objects.create_user(username="owner", password="pass")
        g1 = Group.objects.create(name="G1", owner=owner)
        g2 = Group.objects.create(name="G2", owner=owner)
        GroupMembership.objects.create(group=g2, user=self.user, role=GroupMembership.Role.ADMIN)
        l1, _ = GroupInviteLink.generate(g1, self.user)
        l2, _ = GroupInviteLink.generate(g2, self.user)

        with patch("groups.broadcast.broadcast_group_event"), self.captureOnCommitCallbacks(execute=True):
            call_command("set_site_admin", "testuser", "--revoke", stdout=StringIO())

        l1.refresh_from_db()
        l2.refresh_from_db()
        self.assertFalse(l1.is_active)
        self.assertTrue(l2.is_active)

    def test_revoke_without_the_content_flag_leaves_group_links(self):
        from groups.models import Group, GroupInviteLink, GroupMembership

        User.objects.filter(pk=self.user.pk).update(is_site_admin=True, can_access_all_content=False)
        owner = User.objects.create_user(username="owner", password="pass")
        g1 = Group.objects.create(name="G1", owner=owner)
        GroupMembership.objects.create(group=g1, user=self.user, role=GroupMembership.Role.ADMIN)
        l1, _ = GroupInviteLink.generate(g1, self.user)
        with patch("groups.views._revoke_lapsed_admin_invite_links") as lapse:
            call_command("set_site_admin", "testuser", "--revoke", stdout=StringIO())
        lapse.assert_not_called()
        l1.refresh_from_db()
        self.assertTrue(l1.is_active)

    def test_revoke_on_a_superuser_is_refused_without_side_effects(self):
        from accounts.models import InviteLink

        User.objects.filter(pk=self.user.pk).update(is_superuser=True)
        self.user.refresh_from_db()
        self.user.save()  # superuser promotion derives the site admin flags
        link, _ = InviteLink.generate(created_by=self.user)

        out = StringIO()
        with self.assertRaises(CommandError) as ctx:
            call_command("set_site_admin", "testuser", "--revoke", stdout=out)

        self.assertIn("superuser", str(ctx.exception))
        self.assertNotIn("Revoked", out.getvalue())
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_superuser)
        self.assertTrue(self.user.is_site_admin)
        self.assertTrue(self.user.can_access_all_content)
        link.refresh_from_db()
        self.assertIsNone(link.revoked_at)

    def test_revoke_still_revokes_site_links_for_already_demoted_user(self):
        from accounts.models import InviteLink

        User.objects.filter(pk=self.user.pk).update(is_site_admin=False, can_access_all_content=False)
        link, _ = InviteLink.generate(created_by=self.user)
        call_command("set_site_admin", "testuser", "--revoke", stdout=StringIO())
        link.refresh_from_db()
        self.assertIsNotNone(link.revoked_at)

    def test_revoke_lapses_group_links_for_inactive_user(self):
        from groups.models import Group, GroupInviteLink

        User.objects.filter(pk=self.user.pk).update(
            is_active=False, is_site_admin=True, can_access_all_content=True
        )
        owner = User.objects.create_user(username="owner2", password="pass")
        g1 = Group.objects.create(name="G1", owner=owner)
        l1, _ = GroupInviteLink.generate(g1, self.user)
        with patch("groups.broadcast.broadcast_group_event"), self.captureOnCommitCallbacks(execute=True):
            call_command("set_site_admin", "testuser", "--revoke", stdout=StringIO())
        l1.refresh_from_db()
        self.assertFalse(l1.is_active)

    def test_nonexistent_user_raises_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("set_site_admin", "nobody")
        self.assertIn("does not exist", str(ctx.exception))

    def test_grant_is_idempotent(self):
        """Granting site admin to an already-admin user does not error."""
        self.user.is_site_admin = True
        self.user.can_access_all_content = True
        self.user.save()

        out = StringIO()
        call_command("set_site_admin", "testuser", stdout=out)

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_site_admin)


class ProvisionFuzzTokenFileTests(TestCase):
    """provision_fuzz_token writes the PAT exclusively with mode 0600 (#1379)."""

    def setUp(self):
        User.objects.create_user(username="fuzzer", password="x", email="f@example.com")
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "tok")

    def _run(self):
        call_command("provision_fuzz_token", username="fuzzer", token_file=self.path, stdout=StringIO())

    def test_writes_token_0600(self):
        self._run()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_replaces_stale_own_file(self):
        with open(self.path, "w") as f:
            f.write("old\n")
        # Deliberately world-writable: the stale file the helper must replace.
        os.chmod(self.path, 0o666)  # nosec B103
        self._run()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with open(self.path) as f:
            self.assertNotEqual(f.read().strip(), "old")

    def test_refuses_foreign_owned_file(self):
        with open(self.path, "w") as f:
            f.write("attacker\n")
        with patch("accounts.management._secure_file.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(OSError):
                self._run()
        with open(self.path) as f:
            self.assertEqual(f.read(), "attacker\n")

    def test_refuses_symlink(self):
        os.symlink(os.path.join(self.dir.name, "victim"), self.path)
        with self.assertRaises(OSError):
            self._run()
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "victim")))


class ChangePasswordCommandTests(TestCase):
    """The operator ``changepassword`` command revokes credentials issued under the old password."""

    def _run(self, username, password="a-Strong-new-pass-77"):
        out = StringIO()
        with patch("getpass.getpass", side_effect=[password, password]):
            call_command("changepassword", username, stdout=out)
        return out.getvalue()

    def test_project_command_shadows_django_builtin(self):
        """The name resolves to the project's command, not django.contrib.auth's."""
        from django.core.management import get_commands

        self.assertEqual(get_commands()["changepassword"], "operator_commands")

    def test_reset_revokes_tokens_and_keeps_forced_change_flag(self):
        user = User.objects.create_user(username="opuser", password="old-pass-1234!x")
        Token.objects.create(user=user)
        PersonalAccessToken.generate(user, "ci")

        self.assertIn("Password changed successfully", self._run("opuser"))

        user.refresh_from_db()
        self.assertTrue(user.check_password("a-Strong-new-pass-77"))
        self.assertFalse(Token.objects.filter(user=user).exists())
        self.assertEqual(user.personal_access_tokens.count(), 0)
        self.assertFalse(user.must_change_password)

    def test_reset_does_not_set_forced_change_when_already_set(self):
        user = User.objects.create_user(username="opuser2", password="old-pass-1234!x")
        user.must_change_password = True
        user.save()
        self._run("opuser2")
        user.refresh_from_db()
        self.assertTrue(user.must_change_password)

    def test_other_users_credentials_untouched(self):
        user = User.objects.create_user(username="opuser3", password="old-pass-1234!x")
        other = User.objects.create_user(username="bystander", password="old-pass-1234!x")
        Token.objects.create(user=other)
        PersonalAccessToken.generate(other, "ci")
        self._run("opuser3")
        self.assertTrue(Token.objects.filter(user=other).exists())
        self.assertEqual(other.personal_access_tokens.count(), 1)
        self.assertEqual(user.personal_access_tokens.count(), 0)

    def test_mismatched_passwords_leave_credentials_in_place(self):
        user = User.objects.create_user(username="opuser4", password="old-pass-1234!x")
        Token.objects.create(user=user)
        PersonalAccessToken.generate(user, "ci")
        with patch("getpass.getpass", side_effect=["aaaaaaaa-1X!", "bbbbbbbb-2Y!"] * 3):
            with self.assertRaises(CommandError):
                call_command("changepassword", "opuser4", stdout=StringIO())
        user.refresh_from_db()
        self.assertTrue(user.check_password("old-pass-1234!x"))
        self.assertTrue(Token.objects.filter(user=user).exists())
        self.assertEqual(user.personal_access_tokens.count(), 1)

    def test_unknown_user_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("changepassword", "nobody-here", stdout=StringIO())

    def test_failure_in_revocation_rolls_back_the_password(self):
        user = User.objects.create_user(username="opuser5", password="old-pass-1234!x")
        Token.objects.create(user=user)
        PersonalAccessToken.generate(user, "ci")
        with patch(
            "operator_commands.management.commands.changepassword.revoke_credentials_after_operator_reset",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self._run("opuser5")
        user.refresh_from_db()
        self.assertTrue(user.check_password("old-pass-1234!x"))
        self.assertTrue(Token.objects.filter(user=user).exists())
        self.assertEqual(user.personal_access_tokens.count(), 1)

    @staticmethod
    def _documented_snippets():
        """Code of each fenced block in first-boot.md that calls the revocation helper, dedented."""
        import re
        import textwrap
        from pathlib import Path

        doc = Path(__file__).resolve().parents[3] / "docs" / "getting-started" / "first-boot.md"
        blocks = re.findall(r"^[ \t]*```\w*\n(.*?)^[ \t]*```", doc.read_text(), flags=re.S | re.M)
        snippets = []
        for block in blocks:
            if "revoke_credentials_after_operator_reset" not in block:
                continue
            code = textwrap.dedent(block)
            # The compose variant wraps the code in `manage.py shell -c "..."`.
            m = re.search(r'shell -c "\n(.*)\n"\s*$', code, flags=re.S)
            snippets.append(textwrap.dedent(m.group(1)) if m else code)
        return snippets

    def test_documented_shell_snippets_revoke_credentials(self):
        """Run each copy-paste recovery snippet from docs/getting-started/first-boot.md."""
        snippets = self._documented_snippets()
        self.assertEqual(len(snippets), 2)
        for snippet in snippets:
            user = User.objects.create_user(username="admin", password="old-pass-1234!x")
            Token.objects.create(user=user)
            PersonalAccessToken.generate(user, "ci")
            # The docs run these via `manage.py shell -c`, so run them the same way.
            call_command("shell", command=snippet)
            user.refresh_from_db()
            self.assertFalse(user.check_password("old-pass-1234!x"))
            self.assertTrue(user.must_change_password)
            self.assertFalse(Token.objects.filter(user=user).exists())
            self.assertEqual(user.personal_access_tokens.count(), 0)
            user.delete()
