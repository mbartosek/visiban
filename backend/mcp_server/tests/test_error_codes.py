"""Pin the MCP tool error `code` vocabulary (#1953).

`code` is a public, never-removed contract for agents. These tests make it
impossible to add a `CardServiceError` subclass, or a new tool-level code,
without choosing its wire code deliberately and documenting it.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

from boards.services import errors as service_errors
from boards.services.errors import CardServiceError
from mcp_server import tools

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")

DOCS = Path(__file__).resolve().parents[3] / "docs" / "api" / "mcp.md"

# One constructor per CardServiceError subclass. A subclass missing from this
# table fails test_every_subclass_has_a_factory, forcing whoever adds it here.
FACTORIES = {
    "NotPermitted": lambda: service_errors.NotPermitted(),
    "MoveNotPermitted": lambda: service_errors.MoveNotPermitted(),
    "ForceNotPermitted": lambda: service_errors.ForceNotPermitted("wip"),
    "ObjectNotFound": lambda: service_errors.ObjectNotFound(),
    "CardNotFound": lambda: service_errors.CardNotFound(),
    "ColumnNotFound": lambda: service_errors.ColumnNotFound(),
    "SwimlaneNotFound": lambda: service_errors.SwimlaneNotFound(),
    "BoardNotFound": lambda: service_errors.BoardNotFound(),
    "InvalidVersion": lambda: service_errors.InvalidVersion(),
    "InvalidPosition": lambda: service_errors.InvalidPosition(),
    "VersionConflict": lambda: service_errors.VersionConflict(3),
    "ContainerChanged": lambda: service_errors.ContainerChanged(),
    "WipLimitExceeded": lambda: service_errors.WipLimitExceeded(
        column_name="c", current_count=1, wip_limit=1),
    "WipHardBlocked": lambda: service_errors.WipHardBlocked(
        column_name="c", current_count=1, wip_limit=1),
    "WeightLimitExceeded": lambda: service_errors.WeightLimitExceeded(
        column_name="c", current_weight=1, weight_limit=1, card_weight=1),
    "CardCreationNotAllowed": lambda: service_errors.CardCreationNotAllowed(),
    "UseMoveEndpoint": lambda: service_errors.UseMoveEndpoint(
        "column", board_pk=1, card_pk=2),
}

# Codes that do not come from a CardServiceError: built directly in
# mcp_server/server.py, mcp_server/throttling.py and _error_payload itself.
NON_SERVICE_CODES = {
    "validation_error", "missing_scope", "maintenance_mode", "throttled",
    "demo_read_only",
}

# The complete public vocabulary. Adding to it means updating docs/api/mcp.md
# (test_documented_codes_match_pinned_set); removing from it is a breaking change.
PINNED_CODES = {
    "board_not_found", "card_not_found", "column_not_found", "swimlane_not_found",
    "object_not_found", "permission_denied", "force_not_permitted",
    "invalid_version", "invalid_position", "version_conflict",
    "container_changed", "wip_limit_exceeded", "wip_hard_blocked",
    "weight_limit_exceeded", "card_creation_not_allowed", "use_move_endpoint",
    "internal_error",
} | NON_SERVICE_CODES


def _all_subclasses(cls):
    # __subclasses__() returns direct children only; ObjectNotFound's children
    # would be missed without the recursion.
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


class ErrorCodeVocabularyTests(SimpleTestCase):
    def test_every_subclass_has_a_factory(self):
        names = {c.__name__ for c in _all_subclasses(CardServiceError)}
        self.assertEqual(names, set(FACTORIES))

    def test_every_subclass_yields_a_snake_case_code(self):
        for name, factory in FACTORIES.items():
            with self.subTest(name):
                with self.assertNoLogs(tools.logger, level="ERROR"):
                    payload = tools._error_payload(factory())
                code = payload["error"]["code"]
                self.assertRegex(code, SNAKE_CASE)
                self.assertIn(code, PINNED_CODES)
                self.assertNotEqual(code, "internal_error")

    def test_unregistered_subclass_never_leaks_its_class_name(self):
        class BrandNewError(CardServiceError):
            def body(self):
                return {"detail": "x"}

        with self.assertLogs(tools.logger, level="ERROR"):
            payload = tools._error_payload(BrandNewError())
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["detail"], "x")

    def test_validation_error_carries_errors_not_detail(self):
        payload = tools._error_payload(tools.ValidationFailed({"f": ["bad"]}))
        self.assertEqual(
            payload, {"error": {"code": "validation_error", "errors": {"f": ["bad"]}}})

    def test_pinned_set_covers_emitted_service_codes(self):
        emitted = {
            tools._error_payload(f())["error"]["code"] for f in FACTORIES.values()
        }
        self.assertLessEqual(emitted, PINNED_CODES)

    def test_documented_codes_match_pinned_set(self):
        text = DOCS.read_text()
        section = text.split("### Tool errors", 1)[1].split("\n### ", 1)[0]
        documented = set()
        for line in section.splitlines():
            if line.startswith("| `"):
                cell = line.split("|")[1]
                documented.update(re.findall(r"`([a-z][a-z0-9_]*)`", cell))
        documented.discard("code")
        self.assertEqual(documented, PINNED_CODES)
