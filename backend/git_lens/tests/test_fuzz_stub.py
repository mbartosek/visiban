"""VISIBAN_GIT_LENS_STUB_PROVIDERS: the CI-only provider stub for schema fuzzing (#1338).

Three groups:

* settings gate: evaluated in a subprocess (same pattern as
  accounts/tests/test_disable_throttling_1569.py), because reloading settings
  in-process would re-evaluate DATABASES. Runs flag-off too.
* call-site gate and transport: no DB, no git_lens models, so these run in the
  main flag-off suite as well.
* end to end through the view, plus the schemathesis hook's lens seed: need the
  app installed, so they run only in backend-test-git-lens.
"""
import importlib
import json
import os
import subprocess
import sys
from unittest import skipIf
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from git_lens import fuzz_stub, providers
from git_lens.types import LensConfig, LensFilters

_BASE_ENV = {
    "DJANGO_SECRET_KEY": "test-secret-key-not-change-me-in-production",
    "DATABASE_URL": "sqlite:///dev.db",
}

_SCRIPT = """\
import json, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "visiban.settings")
import django
django.setup()
from django.conf import settings
print(json.dumps({"STUB": settings.VISIBAN_GIT_LENS_STUB_PROVIDERS}))
"""


class StubSettingsGateTests(SimpleTestCase):
    databases = []

    def _eval(self, **overrides):
        env = {
            **_BASE_ENV,
            **overrides,
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        }
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return None, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1]), None

    def test_off_by_default(self):
        values, err = self._eval(DEBUG="true")
        self.assertIsNone(err, err)
        self.assertIs(values["STUB"], False)

    def test_honored_with_debug(self):
        values, err = self._eval(DEBUG="true", VISIBAN_GIT_LENS_STUB_PROVIDERS="true")
        self.assertIsNone(err, err)
        self.assertIs(values["STUB"], True)

    def test_refuses_to_start_without_debug(self):
        values, err = self._eval(
            DEBUG="false", ALLOWED_HOSTS="localhost", CORS_ALLOWED_ORIGINS="https://x.example",
            VISIBAN_GIT_LENS_STUB_PROVIDERS="true",
        )
        self.assertIsNone(values)
        self.assertIn("VISIBAN_GIT_LENS_STUB_PROVIDERS is only honored", err)

    def test_refuses_to_start_on_a_public_demo_even_with_debug(self):
        values, err = self._eval(
            DEBUG="true", DEMO_MODE="true", VISIBAN_GIT_LENS_STUB_PROVIDERS="true",
        )
        self.assertIsNone(values)
        self.assertIn("VISIBAN_GIT_LENS_STUB_PROVIDERS is only honored", err)


class StubCallSiteGateTests(SimpleTestCase):
    """enabled() fails closed even if the settings.py guard were bypassed."""

    def test_disabled_by_default(self):
        self.assertFalse(fuzz_stub.enabled())

    @override_settings(VISIBAN_GIT_LENS_STUB_PROVIDERS=True, DEBUG=False)
    def test_disabled_without_debug_even_if_the_setting_is_forced_on(self):
        self.assertFalse(fuzz_stub.enabled())

    @override_settings(VISIBAN_GIT_LENS_STUB_PROVIDERS="true", DEBUG=True)
    def test_only_a_literal_true_enables_it(self):
        self.assertFalse(fuzz_stub.enabled())

    @override_settings(VISIBAN_GIT_LENS_STUB_PROVIDERS=True, DEBUG=True)
    def test_enabled_with_setting_and_debug(self):
        self.assertTrue(fuzz_stub.enabled())


class StubTransportTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_real_requests_get_is_used_when_the_stub_is_off(self):
        with patch("git_lens.providers.requests.get") as mock_get:
            mock_get.return_value = fuzz_stub.StubResponse([])
            providers.gitlab_fetch(None, "a/b", LensConfig(column_dim="status"), LensFilters())
        self.assertTrue(mock_get.called)

    @override_settings(VISIBAN_GIT_LENS_STUB_PROVIDERS=True, DEBUG=True)
    def test_stub_makes_no_network_call_and_feeds_the_real_pipeline(self):
        with patch("git_lens.providers.requests.get", side_effect=AssertionError("network")):
            data = providers.gitlab_fetch(
                None, "visiban/fuzz-fixture", LensConfig(column_dim="pipeline"), LensFilters()
            )
        payload = data.to_dict()
        self.assertEqual(payload["source"]["provider"], "gitlab")
        self.assertEqual({i["number"] for i in payload["issues"]}, {1, 2, 3, 4})
        by_number = {i["number"]: i for i in payload["issues"]}
        # Pipeline linking ran on the fixture: branch -> Doing, closing MR -> Review.
        self.assertEqual(by_number[2]["column_keys"], ["doing"])
        self.assertEqual(by_number[3]["column_keys"], ["review"])
        self.assertEqual(by_number[4]["column_keys"], ["done"])

    @override_settings(VISIBAN_GIT_LENS_STUB_PROVIDERS=True, DEBUG=True)
    def test_github_gets_an_empty_board_not_gitlab_shaped_data(self):
        with patch("git_lens.providers.requests.get", side_effect=AssertionError("network")):
            data = providers.github_fetch(
                "tok", "o/r", LensConfig(column_dim="pipeline"), LensFilters()
            )
        self.assertEqual(data.to_dict()["issues"], [])

    def test_pages_after_the_first_are_empty(self):
        self.assertTrue(fuzz_stub.response("gitlab", "issues", {"page": 1}).json())
        self.assertEqual(fuzz_stub.response("gitlab", "issues", {"page": 2}).json(), [])


@skipIf(not settings.GIT_LENS_ENABLED, "git_lens experiment disabled; set GIT_LENS_ENABLED=true")
class StubEndToEndTests(TestCase):
    """The fuzz job's exact setup: hook-seeded connection, stubbed fetch, member token."""

    def setUp(self):
        from boards.models import BoardMembership
        from boards.tests.conftest import _make_board, _make_user

        cache.clear()
        self.owner = _make_user()
        self.member = _make_user()
        self.board = _make_board(self.owner, name="Visiban Demo Board")
        BoardMembership.objects.create(
            board=self.board, user=self.member, role=BoardMembership.Role.MEMBER
        )

    def _hooks(self):
        # Imported lazily: the module queries the DB at import time.
        return importlib.import_module("schemathesis_hooks")

    def test_hook_seeds_one_gitlab_connection_with_a_null_creator(self):
        from git_lens.models import LensConnection

        hooks = self._hooks()
        hooks._seed_lens_connection(self.board)
        hooks._seed_lens_connection(self.board)  # a reload must not duplicate it

        conn = LensConnection.objects.get(board=self.board)
        self.assertEqual(conn.provider, "gitlab")
        self.assertIsNone(conn.created_by)

    @override_settings(VISIBAN_GIT_LENS_STUB_PROVIDERS=True, DEBUG=True)
    def test_member_reaches_200_on_both_lens_reads_without_network(self):
        from rest_framework.test import APIClient

        self._hooks()._seed_lens_connection(self.board)
        client = APIClient()
        client.force_authenticate(self.member)

        with patch("git_lens.providers.requests.get", side_effect=AssertionError("network")):
            conn = client.get(f"/api/v1/git-lens/connections/{self.board.id}/")
            board = client.get(f"/api/v1/git-lens/board/{self.board.id}/")

        self.assertEqual(conn.status_code, 200)
        self.assertIsNone(conn.json()["created_by"])
        self.assertEqual(board.status_code, 200)
        self.assertEqual(len(board.json()["issues"]), 4)

    def test_member_cannot_remove_the_seeded_connection(self):
        from rest_framework.test import APIClient

        from git_lens.models import LensConnection

        self._hooks()._seed_lens_connection(self.board)
        client = APIClient()
        client.force_authenticate(self.member)

        resp = client.delete(f"/api/v1/git-lens/connections/{self.board.id}/")

        self.assertEqual(resp.status_code, 403)
        self.assertTrue(LensConnection.objects.filter(board=self.board).exists())
