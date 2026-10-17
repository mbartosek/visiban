"""Every non-test ``GroupSerializer`` construction states its viewer (#1681).

``GroupSerializer`` withholds non-navigation fields when its context names no
viewer (no ``group_access`` and no authenticated request user). That default
is safe but easy to trip over, so each call site passes ``context=`` on
purpose: a view's resolver, a request, or ``full_group_access`` with a
comment saying why. This test scans the backend source with ``ast`` and fails
on a construction without ``context``. A schema-only declaration such as
``responses=GroupSerializer(many=True)`` (no instance, no data) is exempt:
it is never rendered.
"""
from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

_BACKEND = Path(__file__).resolve().parents[2]
_SERIALIZERS = {"GroupSerializer", "GroupDetailSerializer"}
_SKIP_PARTS = {"tests", "migrations", ".venv", "venv", "node_modules", "__pycache__"}


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def find_contextless_calls(source: str, filename: str = "<src>") -> list[tuple[str, int]]:
    """``(filename, lineno)`` of each rendering construction missing ``context=``."""
    missing = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Call) or _callee_name(node) not in _SERIALIZERS:
            continue
        keywords = {kw.arg for kw in node.keywords}
        schema_only = not node.args and not keywords & {"instance", "data"}
        if "context" not in keywords and not schema_only:
            missing.append((filename, node.lineno))
    return sorted(missing)


def _source_files():
    for path in sorted(_BACKEND.rglob("*.py")):
        rel = path.relative_to(_BACKEND)
        if _SKIP_PARTS & set(rel.parts) or rel.name.startswith("test_"):
            continue
        yield rel, path.read_text(encoding="utf-8")


class GroupSerializerContextGuardTests(SimpleTestCase):
    def test_every_construction_passes_context(self):
        missing, seen = [], 0
        for rel, source in _source_files():
            if not any(name + "(" in source for name in _SERIALIZERS):
                continue
            for node in ast.walk(ast.parse(source, str(rel))):
                if isinstance(node, ast.Call) and _callee_name(node) in _SERIALIZERS:
                    seen += 1
            missing += find_contextless_calls(source, str(rel))
        # The scan must actually reach the call sites, or it passes vacuously.
        self.assertGreaterEqual(seen, 8)
        self.assertEqual(missing, [], "GroupSerializer built without context=")

    def test_checker_flags_a_contextless_construction(self):
        src = "data = GroupSerializer(group).data\nx = serializers.GroupDetailSerializer(g, data={})\n"
        self.assertEqual(find_contextless_calls(src), [("<src>", 1), ("<src>", 2)])

    def test_checker_accepts_context_and_schema_only_declarations(self):
        src = (
            "a = GroupSerializer(g, context={'group_access': full_group_access})\n"
            "b = extend_schema(responses=GroupSerializer(many=True))\n"
        )
        self.assertEqual(find_contextless_calls(src), [])
