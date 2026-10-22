"""ANALYTICS_EXTENSIONS panels declare a minimum board role, fail closed (#1826).

The summary is readable by every board member. Before #1826 each panel had to
check ``request.user``'s role itself, and one that forgot showed its data to
viewers. The summary view now resolves the caller's role once and drops every
panel whose ``min_role`` ranks above it before any panel runs. An unset or
invalid ``min_role`` means ``admin``.

Call counts are asserted on mocks, not only the response: a panel above the
caller's role must never be invoked, because its code (queries, side effects)
is what the gate keeps from running for that caller.
"""

import ast
import pathlib
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards import hooks
from boards.models import Board, BoardMembership, Column, Swimlane
from boards.permissions import (
    ANALYTICS_PANEL_DEFAULT_MIN_ROLE,
    SITE_ADMIN,
    analytics_extensions_for_role,
    analytics_panel_min_role,
)
from groups.models import Group, GroupMembership

_LADDER = ("viewer", "collaborator", "member", "admin", "site_admin")


def _panel_dict(panel_id):
    return {
        "id": panel_id,
        "title": f"Panel {panel_id}",
        "type": "stats",
        "description": "d",
        "data": {"items": [{"label": "A", "value": 1, "hint": "h"}]},
    }


def _panel(panel_id, min_role=None, *, declare=True):
    """A mock panel callable. ``spec=[]`` so an undeclared attribute reads as absent."""
    fn = mock.Mock(spec=[], return_value=_panel_dict(panel_id))
    if declare:
        fn.min_role = min_role
    return fn


class _Registered:
    """Register callables on the real hook list for one block, always unregister."""

    def __init__(self, *fns):
        self.fns = fns

    def __enter__(self):
        hooks.ANALYTICS_EXTENSIONS.extend(self.fns)
        return self.fns

    def __exit__(self, *exc):
        for fn in self.fns:
            hooks.ANALYTICS_EXTENSIONS.remove(fn)
        return False


class AnalyticsPanelMinRoleTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner1826", password="pw")
        self.admin = User.objects.create_user(username="admin1826", password="pw")
        self.member = User.objects.create_user(username="member1826", password="pw")
        self.collaborator = User.objects.create_user(username="collab1826", password="pw")
        self.viewer = User.objects.create_user(username="viewer1826", password="pw")
        self.outsider = User.objects.create_user(username="outsider1826", password="pw")
        self.site_admin = User.objects.create_user(
            username="siteadmin1826", password="pw", can_access_all_content=True
        )

        self.board = Board.objects.create(name="Board 1826", owner=self.owner)
        for user, role in (
            (self.admin, "admin"),
            (self.member, "member"),
            (self.collaborator, "collaborator"),
            (self.viewer, "viewer"),
        ):
            BoardMembership.objects.create(board=self.board, user=user, role=role)
        Column.objects.create(board=self.board, name="To Do", position=0)
        Swimlane.objects.create(board=self.board, name="Lane", position=0)

    def _get(self, user, suffix="summary/"):
        client = APIClient()
        client.force_authenticate(user=user)
        return client.get(f"/api/v1/boards/{self.board.pk}/{suffix}")

    def _ladder_panels(self):
        return {role: _panel(role, role) for role in _LADDER} | {"unset": _panel("unset", declare=False)}

    def _assert_sees(self, user, expected_ids):
        panels = self._ladder_panels()
        with _Registered(*panels.values()):
            resp = self._get(user)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([p["id"] for p in resp.data["extension_panels"]], expected_ids)
        for panel_id, fn in panels.items():
            # Called exactly once when shown, never when dropped.
            self.assertEqual(fn.call_count, 1 if panel_id in expected_ids else 0, panel_id)
        return panels

    # -- the role matrix --------------------------------------------------------

    def test_viewer_sees_only_viewer_panels(self):
        self._assert_sees(self.viewer, ["viewer"])

    def test_collaborator_sees_viewer_and_collaborator_panels(self):
        self._assert_sees(self.collaborator, ["viewer", "collaborator"])

    def test_member_sees_panels_up_to_member(self):
        self._assert_sees(self.member, ["viewer", "collaborator", "member"])

    def test_admin_sees_panels_up_to_admin_including_undeclared(self):
        self._assert_sees(self.admin, ["viewer", "collaborator", "member", "admin", "unset"])

    def test_owner_without_membership_row_is_admin(self):
        self.assertFalse(BoardMembership.objects.filter(board=self.board, user=self.owner).exists())
        self._assert_sees(self.owner, ["viewer", "collaborator", "member", "admin", "unset"])

    def test_all_content_user_with_no_membership_sees_every_panel(self):
        self._assert_sees(
            self.site_admin, ["viewer", "collaborator", "member", "admin", "site_admin", "unset"]
        )

    def test_group_inherited_roles_are_honored(self):
        group = Group.objects.create(name="Group 1826", owner=self.owner)
        self.board.group = group
        self.board.save(update_fields=["group"])
        g_viewer = User.objects.create_user(username="gviewer1826", password="pw")
        g_member = User.objects.create_user(username="gmember1826", password="pw")
        GroupMembership.objects.create(group=group, user=g_viewer, role=GroupMembership.Role.VIEWER)
        GroupMembership.objects.create(group=group, user=g_member, role=GroupMembership.Role.MEMBER)
        self._assert_sees(g_viewer, ["viewer"])
        self._assert_sees(g_member, ["viewer", "collaborator", "member"])

    def test_explicit_membership_overrides_a_higher_group_role(self):
        """The view uses the ladder's precedence, not the highest role found."""
        group = Group.objects.create(name="Group 1826b", owner=self.owner)
        self.board.group = group
        self.board.save(update_fields=["group"])
        GroupMembership.objects.create(group=group, user=self.viewer, role=GroupMembership.Role.ADMIN)
        self._assert_sees(self.viewer, ["viewer"])

    def test_non_member_gets_404_and_no_panel_runs(self):
        panels = self._ladder_panels()
        with _Registered(*panels.values()):
            resp = self._get(self.outsider)
        self.assertEqual(resp.status_code, 404)
        for fn in panels.values():
            self.assertEqual(fn.call_count, 0)

    # -- fail closed -------------------------------------------------------------

    def test_unset_min_role_is_admin_only(self):
        self.assertEqual(ANALYTICS_PANEL_DEFAULT_MIN_ROLE, "admin")
        for user in (self.viewer, self.collaborator, self.member):
            legacy = _panel("legacy", declare=False)
            with _Registered(legacy):
                resp = self._get(user)
            self.assertEqual(resp.data["extension_panels"], [])
            self.assertEqual(legacy.call_count, 0)
        legacy = _panel("legacy", declare=False)
        with _Registered(legacy):
            resp = self._get(self.admin)
        self.assertEqual(resp.data["extension_panels"], [_panel_dict("legacy")])
        legacy.assert_called_once()

    def test_plain_function_without_attribute_is_admin_only(self):
        """The pre-1.2 registration shape: a bare function, no attribute at all."""
        calls = []

        def legacy_panel(board, request):
            calls.append(request.user.username)
            return _panel_dict("fn")

        with _Registered(legacy_panel):
            self.assertEqual(self._get(self.viewer).data["extension_panels"], [])
            self.assertEqual(self._get(self.admin).data["extension_panels"], [_panel_dict("fn")])
        self.assertEqual(calls, [self.admin.username])

    def test_none_min_role_is_admin_only(self):
        fn = _panel("none", None)
        with _Registered(fn):
            self.assertEqual(self._get(self.member).data["extension_panels"], [])
        self.assertEqual(fn.call_count, 0)

    def test_invalid_min_role_fails_closed_to_admin(self):
        for bad in ("Viewer", "owner", "", " viewer", 1, ["viewer"], {"viewer": 1}, object()):
            with self.subTest(min_role=bad):
                fn = _panel("bad", bad)
                with _Registered(fn), self.assertLogs("boards.permissions", level="WARNING") as cm:
                    resp = self._get(self.viewer)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.data["extension_panels"], [])
                self.assertEqual(fn.call_count, 0)
                self.assertIn("invalid min_role", "\n".join(cm.output))
                with _Registered(fn), self.assertLogs("boards.permissions", level="WARNING"):
                    resp = self._get(self.admin)
                self.assertEqual(resp.data["extension_panels"], [_panel_dict("bad")])
                self.assertEqual(fn.call_count, 1)

    def test_invalid_min_role_warning_never_logs_the_value(self):
        fn = _panel("bad", "SECRET-ROLE")
        with _Registered(fn), self.assertLogs("boards.permissions", level="WARNING") as cm:
            self._get(self.admin)
        self.assertNotIn("SECRET", "\n".join(cm.output))

    def test_unreadable_min_role_skips_the_panel_for_everyone(self):
        calls = []

        class Exploding:
            @property
            def min_role(self):
                raise RuntimeError("SECRET")

            def __call__(self, board, request):
                calls.append(1)
                return _panel_dict("boom")

        with _Registered(Exploding()), self.assertLogs("boards.permissions", level="WARNING") as cm:
            resp = self._get(self.site_admin)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["extension_panels"], [])
        self.assertEqual(calls, [])
        self.assertNotIn("SECRET", "\n".join(cm.output))

    # -- declaration forms and payload ----------------------------------------------

    def test_callable_instance_and_bound_method_declarations(self):
        class InstancePanel:
            min_role = "viewer"

            def __call__(self, board, request):
                return _panel_dict("instance")

        class Provider:
            def render(self, board, request):
                return _panel_dict("method")

            render.min_role = "viewer"

        with _Registered(InstancePanel(), Provider().render):
            resp = self._get(self.viewer)
        self.assertEqual([p["id"] for p in resp.data["extension_panels"]], ["instance", "method"])

    def test_allowed_panel_payload_is_unchanged_and_order_kept(self):
        first, second = _panel("a", "viewer"), _panel("b", "viewer")
        with _Registered(first, second):
            resp = self._get(self.viewer)
        self.assertEqual(resp.data["extension_panels"], [_panel_dict("a"), _panel_dict("b")])
        first.assert_called_once()
        board_arg, request_arg = first.call_args.args
        self.assertEqual(board_arg.pk, self.board.pk)
        self.assertEqual(request_arg.user, self.viewer)

    def test_analytics_endpoint_runs_no_panel(self):
        """``/analytics/`` does not render panels; it must not call them either."""
        panels = self._ladder_panels()
        with _Registered(*panels.values()):
            resp = self._get(self.site_admin, "analytics/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("extension_panels", resp.data)
        for fn in panels.values():
            self.assertEqual(fn.call_count, 0)


class AnalyticsExtensionsForRoleUnitTests(TestCase):
    def test_no_or_unknown_role_gets_nothing(self):
        fn = _panel("v", "viewer")
        with _Registered(fn):
            self.assertEqual(analytics_extensions_for_role(None), [])
            self.assertEqual(analytics_extensions_for_role("owner"), [])
        self.assertEqual(fn.call_count, 0)

    def test_filter_never_calls_a_panel(self):
        fn = _panel("v", "viewer")
        with _Registered(fn):
            self.assertEqual(analytics_extensions_for_role(SITE_ADMIN), [fn])
        self.assertEqual(fn.call_count, 0)

    def test_valid_values_are_the_role_ladder(self):
        for role in _LADDER:
            self.assertEqual(analytics_panel_min_role(_panel("x", role)), role)
        self.assertEqual(
            analytics_panel_min_role(_panel("x", BoardMembership.Role.VIEWER)), "viewer"
        )

    def test_reads_the_live_list(self):
        """Per the hooks stability guarantee, never a copy taken at import time."""
        fn = _panel("late", "viewer")
        with _Registered(fn):
            self.assertIn(fn, analytics_extensions_for_role("viewer"))
        self.assertNotIn(fn, analytics_extensions_for_role("viewer"))


class AnalyticsExtensionsEntryPointTests(TestCase):
    """Security Invariants Rule 12: one reader of ANALYTICS_EXTENSIONS.

    A second surface that iterated the list itself (an MCP tool, an export)
    would skip the min_role filter. Scans every backend module outside tests
    and migrations for a reference to the name.
    """

    ALLOWED = {
        "boards/hooks.py": "defines the list",
        "boards/permissions.py": "analytics_extensions_for_role, the shared filter",
    }

    def test_only_the_shared_filter_reads_the_list(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        offenders = []
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            parts = set(path.relative_to(root).parts)
            if parts & {"tests", "migrations", ".venv", "node_modules"} or rel in self.ALLOWED:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                hit = (
                    (isinstance(node, ast.Attribute) and node.attr == "ANALYTICS_EXTENSIONS")
                    or (isinstance(node, ast.Name) and node.id == "ANALYTICS_EXTENSIONS")
                    or (
                        isinstance(node, ast.alias)
                        and node.name == "ANALYTICS_EXTENSIONS"
                    )
                )
                if hit:
                    offenders.append(f"{rel}:{getattr(node, 'lineno', '?')}")
        self.assertEqual(
            offenders, [],
            "Read ANALYTICS_EXTENSIONS through boards.permissions.analytics_extensions_for_role",
        )
