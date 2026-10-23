"""Tests for the deprecated env-var alias helpers (#819, #1873).

The helper must:
- Emit a DeprecationWarning *only* when the legacy env var is set.
- Stay silent when the legacy env var is absent (worker startup must not
  log a spurious warning on every restart).
- Also log the warning, because Python hides a DeprecationWarning raised
  outside __main__ (#1873).
- Say "no longer read" for an alias that was removed (``removed=True``) and
  "deprecated; rename it" for one that still works.

``_env_alias_name`` resolves a renamed variable whose old name must keep
working (#1873): the new name wins when non-empty, else the old one is read.
"""
import json
import os
import subprocess
import sys
import warnings
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from visiban.settings import _env_alias_name, _warn_deprecated_env_alias


class WarnDeprecatedEnvAliasTests(TestCase):

    def test_no_warning_when_alias_absent(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OIDC_SECRET", None)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _warn_deprecated_env_alias("OIDC_SECRET", "OIDC_CLIENT_SECRET")
            relevant = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertEqual(relevant, [])

    def test_warning_emitted_when_alias_set(self):
        with patch.dict(os.environ, {"OIDC_SECRET": "legacy-secret"}, clear=False):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _warn_deprecated_env_alias("OIDC_SECRET", "OIDC_CLIENT_SECRET")
            relevant = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertEqual(len(relevant), 1)
            message = str(relevant[0].message)
            self.assertIn("OIDC_SECRET", message)
            self.assertIn("OIDC_CLIENT_SECRET", message)

    def test_no_warning_when_alias_set_to_empty_string(self):
        # Empty value means the operator did not actually set the alias.
        with patch.dict(os.environ, {"OIDC_SECRET": ""}, clear=False):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                _warn_deprecated_env_alias("OIDC_SECRET", "OIDC_CLIENT_SECRET")
            relevant = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertEqual(relevant, [])

    def test_warning_is_also_logged_for_operators(self):
        # Python hides a DeprecationWarning raised outside __main__, so the
        # warnings.warn() call alone never reached a container log (#1873).
        with patch.dict(os.environ, {"VISIBAN_IMPORT_MAX_SIZE": "1024"}, clear=False):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with self.assertLogs("visiban.env_aliases", level="WARNING") as logs:
                    _warn_deprecated_env_alias("VISIBAN_IMPORT_MAX_SIZE", "IMPORT_MAX_SIZE_BYTES")
        self.assertEqual(len(logs.output), 1)

    def _logged(self, old, new, **kwargs):
        with patch.dict(os.environ, {old: "legacy-value"}, clear=False):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with self.assertLogs("visiban.env_aliases", level="WARNING") as logs:
                    _warn_deprecated_env_alias(old, new, **kwargs)
        return logs.output[0]

    def test_working_alias_wording_says_rename(self):
        message = self._logged("VISIBAN_IMPORT_MAX_SIZE", "IMPORT_MAX_SIZE_BYTES")
        self.assertIn("VISIBAN_IMPORT_MAX_SIZE is deprecated; rename it to IMPORT_MAX_SIZE_BYTES", message)
        self.assertNotIn("no longer read", message)

    def test_removed_alias_wording_says_it_has_no_effect_oidc_secret(self):
        message = self._logged("OIDC_SECRET", "OIDC_CLIENT_SECRET", removed=True)
        self.assertIn("OIDC_SECRET is no longer read and has no effect; replace it with OIDC_CLIENT_SECRET", message)
        self.assertNotIn("is deprecated", message)

    def test_removed_alias_wording_says_it_has_no_effect_account_email_verification(self):
        message = self._logged("ACCOUNT_EMAIL_VERIFICATION", "EMAIL_VERIFICATION", removed=True)
        self.assertIn(
            "ACCOUNT_EMAIL_VERIFICATION is no longer read and has no effect; replace it with EMAIL_VERIFICATION",
            message,
        )
        self.assertNotIn("is deprecated", message)


class EnvAliasNameTests(SimpleTestCase):
    """Resolution order for a renamed env var whose old name must keep working (#1873)."""

    NEW = "VISIBAN_TEST_1873_NEW"
    OLD = "VISIBAN_TEST_1873_OLD"

    def _resolve(self, values):
        with patch.dict(os.environ, values, clear=False):
            for name in (self.NEW, self.OLD):
                if name not in values:
                    os.environ.pop(name, None)
            return _env_alias_name(self.NEW, self.OLD)

    def test_neither_set_reads_the_canonical_name(self):
        self.assertEqual(self._resolve({}), self.NEW)

    def test_new_only(self):
        self.assertEqual(self._resolve({self.NEW: "1"}), self.NEW)

    def test_old_only_keeps_working(self):
        self.assertEqual(self._resolve({self.OLD: "1"}), self.OLD)

    def test_both_set_new_wins(self):
        self.assertEqual(self._resolve({self.NEW: "1", self.OLD: "2"}), self.NEW)

    def test_empty_new_does_not_shadow_the_old_value(self):
        # A compose-style ${NEW:-} passthrough renders an empty NEW.
        self.assertEqual(self._resolve({self.NEW: "", self.OLD: "2"}), self.OLD)

    def test_empty_old_alone_is_still_read_as_before(self):
        self.assertEqual(self._resolve({self.OLD: ""}), self.OLD)


# ── Settings evaluated in a subprocess ──────────────────────────────────────
# Same pattern as accounts/tests/test_proxy_depth.py: reloading settings
# in-process would re-evaluate DATABASES and break later tests.

_BASE_ENV = {
    "DEBUG": "true",
    "DJANGO_SECRET_KEY": "test-secret-key-not-change-me-in-production",
    "DATABASE_URL": "sqlite:///dev.db",
}

_SCRIPT = """\
import json, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "visiban.settings")
import django
django.setup()
from django.conf import settings
print(json.dumps({
    "IMPORT_MAX_SIZE": settings.VISIBAN_IMPORT_MAX_SIZE,
    "USER_RATE": settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["user"],
}))
"""


class RenamedEnvVarSettingsTests(SimpleTestCase):
    """The 1.2 pre-release names stay working aliases of their canonical names (#1873)."""

    databases = []

    def _eval(self, overrides):
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
        return json.loads(result.stdout.strip().splitlines()[-1]), result.stderr

    # IMPORT_MAX_SIZE_BYTES <- VISIBAN_IMPORT_MAX_SIZE

    def test_import_max_size_default_is_unchanged(self):
        values, stderr = self._eval({})
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["IMPORT_MAX_SIZE"], 25 * 1024 * 1024)
        self.assertNotIn("is deprecated", stderr)

    def test_import_max_size_old_name_works_and_warns(self):
        values, stderr = self._eval({"VISIBAN_IMPORT_MAX_SIZE": "1024"})
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["IMPORT_MAX_SIZE"], 1024)
        self.assertIn("VISIBAN_IMPORT_MAX_SIZE is deprecated; rename it to IMPORT_MAX_SIZE_BYTES", stderr)

    def test_import_max_size_new_name_works_without_warning(self):
        values, stderr = self._eval({"IMPORT_MAX_SIZE_BYTES": "2048"})
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["IMPORT_MAX_SIZE"], 2048)
        self.assertNotIn("is deprecated", stderr)

    def test_import_max_size_both_set_new_wins(self):
        values, stderr = self._eval({"IMPORT_MAX_SIZE_BYTES": "2048", "VISIBAN_IMPORT_MAX_SIZE": "1024"})
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["IMPORT_MAX_SIZE"], 2048)

    # DEMO_THROTTLE_USER_RATE <- DEMO_USER_THROTTLE_RATE

    def test_demo_throttle_old_name_works_and_warns(self):
        values, stderr = self._eval({"DEMO_MODE": "true", "DEMO_USER_THROTTLE_RATE": "60000/hour"})
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["USER_RATE"], "60000/hour")
        self.assertIn("DEMO_USER_THROTTLE_RATE is deprecated; rename it to DEMO_THROTTLE_USER_RATE", stderr)

    def test_demo_throttle_new_name_works_without_warning(self):
        values, stderr = self._eval({"DEMO_MODE": "true", "DEMO_THROTTLE_USER_RATE": "50000/hour"})
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["USER_RATE"], "50000/hour")
        self.assertNotIn("is deprecated", stderr)

    def test_demo_throttle_both_set_new_wins(self):
        values, stderr = self._eval(
            {"DEMO_MODE": "true", "DEMO_THROTTLE_USER_RATE": "50000/hour", "DEMO_USER_THROTTLE_RATE": "60000/hour"}
        )
        self.assertIsNotNone(values, stderr)
        self.assertEqual(values["USER_RATE"], "50000/hour")

    def test_demo_throttle_new_name_still_ignored_outside_demo_mode(self):
        values, stderr = self._eval({"DEMO_THROTTLE_USER_RATE": "50000/hour"})
        self.assertIsNotNone(values, stderr)
        self.assertNotEqual(values["USER_RATE"], "50000/hour")

    # Removed aliases (1.1): the settings call sites use the "no effect" wording.

    def test_oidc_secret_boot_warning_says_it_is_no_longer_read(self):
        values, stderr = self._eval({"OIDC_SECRET": "legacy-secret"})
        self.assertIsNotNone(values, stderr)
        self.assertIn("OIDC_SECRET is no longer read and has no effect; replace it with OIDC_CLIENT_SECRET", stderr)

    def test_account_email_verification_boot_warning_says_it_is_no_longer_read(self):
        values, stderr = self._eval({"ACCOUNT_EMAIL_VERIFICATION": "none"})
        self.assertIsNotNone(values, stderr)
        self.assertIn(
            "ACCOUNT_EMAIL_VERIFICATION is no longer read and has no effect; replace it with EMAIL_VERIFICATION",
            stderr,
        )

    def test_demo_throttle_typo_names_the_variable_that_was_set(self):
        values, stderr = self._eval({"DEMO_MODE": "true", "DEMO_THROTTLE_USER_RATE": "lots"})
        self.assertIsNone(values)
        self.assertIn("DEMO_THROTTLE_USER_RATE='lots'", stderr)
