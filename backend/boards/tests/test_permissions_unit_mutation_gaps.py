"""Direct unit tests for ``boards.permissions``, added from the #1443 baseline.

The RBAC suites exercise this module through HTTP, which leaves its pure
predicates and resolvers with no assertion that pins their exact output. The
mutation baseline showed survivors in every one of them, so each is asserted
here directly: ``moderator_field_visible``, ``site_admin_field_visible``,
``get_board_roles``, the group-ancestry cap, the demo-visitor carve-out,
``can_modify_others_content``, and the two DRF permission classes.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings

from boards import permissions as perms
from boards.models import BoardMembership
from boards.permissions import (
    SITE_ADMIN, IsBoardAdminOrOwner, IsBoardMember, can_modify_others_content,
    get_board_role, get_board_roles, moderator_field_visible,
    site_admin_field_visible,
)
from boards.tests.conftest import _make_board, _make_membership, _make_user
from groups.models import Group, GroupMembership

ADMIN = BoardMembership.Role.ADMIN
MEMBER = BoardMembership.Role.MEMBER
VIEWER = BoardMembership.Role.VIEWER


class FieldVisibilityTests(TestCase):
    def test_moderator_field_visible_to_admins_on_any_row(self):
        self.assertIs(moderator_field_visible(ADMIN, 1, 2), True)
        self.assertIs(moderator_field_visible(SITE_ADMIN, 1, 2), True)

    def test_moderator_field_visible_on_own_row_only_for_others(self):
        self.assertIs(moderator_field_visible(MEMBER, 5, 5), True)
        self.assertIs(moderator_field_visible(MEMBER, 5, 6), False)
        self.assertIs(moderator_field_visible(VIEWER, 5, 6), False)

    def test_moderator_field_fails_closed_on_unresolvable_ids(self):
        self.assertIs(moderator_field_visible(MEMBER, None, None), False)
        self.assertIs(moderator_field_visible(MEMBER, 5, None), False)
        self.assertIs(moderator_field_visible(MEMBER, None, 5), False)
        self.assertIs(moderator_field_visible(None, None, None), False)

    def test_site_admin_flag_visible_only_to_admin_roles(self):
        self.assertIs(site_admin_field_visible(ADMIN), True)
        self.assertIs(site_admin_field_visible(SITE_ADMIN), True)
        self.assertIs(site_admin_field_visible(MEMBER), False)
        self.assertIs(site_admin_field_visible(VIEWER), False)
        self.assertIs(site_admin_field_visible(None), False)

    def test_public_constants(self):
        self.assertEqual(SITE_ADMIN, "site_admin")
        self.assertEqual(len(perms.MODERATOR_BEARING_EVENTS), 2)


class RoleLadderTests(TestCase):
    def setUp(self):
        self.owner = _make_user("rl_owner")
        self.board = _make_board(self.owner)
        self.user = _make_user("rl_user")

    def test_role_is_stamped_for_the_user_it_was_resolved_for(self):
        _make_membership(self.board, self.user, MEMBER)
        board = type(self.board).objects.get(pk=self.board.pk)
        self.assertEqual(get_board_role(self.user, board), MEMBER)
        self.assertEqual(board._resolved_role, (self.user.id, MEMBER))
        self.assertEqual(board._cached_membership.user_id, self.user.id)

    def test_non_member_has_no_role_and_the_none_is_stamped(self):
        board = type(self.board).objects.get(pk=self.board.pk)
        self.assertIsNone(get_board_role(self.user, board))
        self.assertEqual(board._resolved_role, (self.user.id, None))

    def test_prefetched_memberships_are_scanned_for_the_right_user(self):
        other = _make_user("rl_other")
        mine = _make_membership(self.board, self.user, VIEWER)
        theirs = _make_membership(self.board, other, ADMIN)
        board = type(self.board).objects.get(pk=self.board.pk)
        board._prefetched_memberships = [theirs, mine]
        with self.assertNumQueries(0):
            self.assertEqual(get_board_role(self.user, board), VIEWER)
        board2 = type(self.board).objects.get(pk=self.board.pk)
        board2._prefetched_memberships = [theirs]
        self.assertIsNone(get_board_role(self.user, board2))

    def test_site_admin_user_resolves_without_a_membership_query(self):
        boss = _make_user("rl_boss", can_access_all_content=True)
        board = type(self.board).objects.get(pk=self.board.pk)
        with self.assertNumQueries(0):
            self.assertEqual(get_board_role(boss, board), SITE_ADMIN)

    def test_is_site_admin_alone_grants_no_board_access(self):
        admin_only = _make_user("rl_adminonly", is_site_admin=True)
        board = type(self.board).objects.get(pk=self.board.pk)
        self.assertIsNone(get_board_role(admin_only, board))


class GroupInheritanceTests(TestCase):
    def setUp(self):
        self.owner = _make_user("gi_owner")
        self.user = _make_user("gi_user")

    def _chain(self, depth):
        """Return groups [leaf, ..., root] of the given length."""
        parent = None
        groups = []
        for i in range(depth):
            parent = Group.objects.create(name=f"g{i}", owner=self.owner, parent=parent)
            groups.append(parent)
        return list(reversed(groups))

    def _board(self, group):
        board = _make_board(self.owner)
        board.group = group
        board.save(update_fields=["group"])
        return type(board).objects.select_related(
            perms.GROUP_ANCESTOR_SELECT_RELATED
        ).get(pk=board.pk)

    def test_nearest_ancestor_wins(self):
        leaf, mid, root = self._chain(3)
        GroupMembership.objects.create(group=root, user=self.user, role="admin")
        GroupMembership.objects.create(group=mid, user=self.user, role="viewer")
        board = self._board(leaf)
        self.assertEqual(perms._group_ancestor_ids(board), [leaf.pk, mid.pk, root.pk])
        self.assertEqual(get_board_role(self.user, board), "viewer")

    def test_roles_bulk_matches_single_resolver_for_group_roles(self):
        leaf, mid, root = self._chain(3)
        GroupMembership.objects.create(group=root, user=self.user, role="member")
        board = self._board(leaf)
        self.assertEqual(get_board_roles(self.user, [board]), {board.pk: "member"})

    def test_ancestry_cap_is_exactly_six_and_warns_when_truncated(self):
        groups = self._chain(7)  # leaf first; 7 levels, cap is 6
        board = self._board(groups[0])
        with self.assertLogs("boards.permissions", level="WARNING") as logs:
            ids = perms._group_ancestor_ids(board)
        self.assertEqual(ids, [g.pk for g in groups[:6]])
        message = logs.records[0].getMessage()
        self.assertTrue(message.startswith("Group ancestry traversal capped at depth 6 for board "))
        self.assertTrue(message.endswith("Memberships at deeper levels were not evaluated."))

    def test_chain_of_exactly_six_is_not_truncated_and_does_not_warn(self):
        groups = self._chain(6)
        board = self._board(groups[0])
        with self.assertNoLogs("boards.permissions", level="WARNING"):
            ids = perms._group_ancestor_ids(board)
        self.assertEqual(len(ids), 6)

    def test_board_without_group_has_no_ancestors(self):
        board = _make_board(self.owner)
        self.assertEqual(perms._group_ancestor_ids(board), [])


class GetBoardRolesTests(TestCase):
    def setUp(self):
        self.owner = _make_user("gr_owner")
        self.user = _make_user("gr_user")
        self.b1 = _make_board(self.owner, name="B1")
        self.b2 = _make_board(self.owner, name="B2")
        self.b3 = _make_board(self.owner, name="B3")
        _make_membership(self.b1, self.user, MEMBER)
        _make_membership(self.b2, self.user, VIEWER)

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(get_board_roles(self.user, []), {})

    def test_generator_input_is_accepted(self):
        boards = (b for b in [self.b1, self.b2])
        self.assertEqual(
            get_board_roles(self.user, boards), {self.b1.pk: MEMBER, self.b2.pk: VIEWER},
        )

    def test_every_board_gets_a_key_and_no_access_is_none(self):
        roles = get_board_roles(self.user, [self.b1, self.b2, self.b3])
        self.assertEqual(roles, {self.b1.pk: MEMBER, self.b2.pk: VIEWER, self.b3.pk: None})

    def test_each_board_is_stamped_for_the_user(self):
        get_board_roles(self.user, [self.b1, self.b3])
        self.assertEqual(self.b1._resolved_role, (self.user.id, MEMBER))
        self.assertEqual(self.b3._resolved_role, (self.user.id, None))
        self.assertEqual(self.b1._cached_membership.user_id, self.user.id)

    def test_owner_resolves_to_admin_and_other_users_rows_are_ignored(self):
        roles = get_board_roles(self.owner, [self.b1, self.b2])
        self.assertEqual(roles, {self.b1.pk: ADMIN, self.b2.pk: ADMIN})
        stranger = _make_user("gr_stranger")
        self.assertEqual(
            get_board_roles(stranger, [self.b1, self.b2]), {self.b1.pk: None, self.b2.pk: None},
        )

    def test_site_admin_short_circuits_with_zero_queries(self):
        boss = _make_user("gr_boss", can_access_all_content=True)
        with self.assertNumQueries(0):
            roles = get_board_roles(boss, [self.b1, self.b2])
        self.assertEqual(roles, {self.b1.pk: SITE_ADMIN, self.b2.pk: SITE_ADMIN})
        self.assertEqual(self.b1._resolved_role, (boss.id, SITE_ADMIN))

    def test_explicit_membership_beats_group_role(self):
        group = Group.objects.create(name="G", owner=self.owner)
        GroupMembership.objects.create(group=group, user=self.user, role="admin")
        self.b2.group = group
        self.b2.save(update_fields=["group"])
        board = type(self.b2).objects.select_related(
            perms.GROUP_ANCESTOR_SELECT_RELATED
        ).get(pk=self.b2.pk)
        self.assertEqual(get_board_roles(self.user, [board]), {board.pk: VIEWER})

    def test_group_role_is_used_when_no_explicit_membership(self):
        group = Group.objects.create(name="G", owner=self.owner)
        GroupMembership.objects.create(group=group, user=self.user, role="admin")
        self.b3.group = group
        self.b3.save(update_fields=["group"])
        board = type(self.b3).objects.select_related(
            perms.GROUP_ANCESTOR_SELECT_RELATED
        ).get(pk=self.b3.pk)
        self.assertEqual(get_board_roles(self.user, [board]), {board.pk: "admin"})


class DemoVisitorTests(TestCase):
    def setUp(self):
        self.user = _make_user("Demo_Visitor")

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="demo_visitor")
    def test_published_member_is_a_visitor_case_insensitively(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, self.user), True)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="demo_visitor")
    def test_only_plain_members_qualify(self):
        self.assertIs(perms._is_demo_visitor(VIEWER, self.user), False)
        self.assertIs(perms._is_demo_visitor(BoardMembership.Role.COLLABORATOR, self.user), False)
        self.assertIs(perms._is_demo_visitor(ADMIN, self.user), False)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="demo_visitor")
    def test_other_usernames_do_not_qualify(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, _make_user("someone_else")), False)

    @override_settings(DEMO_MODE=False, DEMO_LOGIN_USERNAME="demo_visitor")
    def test_demo_mode_off_disables_the_carve_out(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, self.user), False)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="")
    def test_no_published_username_disables_the_carve_out(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace(username="")), False)
        # A user literally named like a placeholder must not match a missing setting.
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace(username="XXXX")), False)
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace(username="xxxx")), False)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME=None)
    def test_a_none_published_username_disables_the_carve_out(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace(username="XXXX")), False)

    @override_settings(DEMO_MODE=True)
    def test_an_unset_published_username_disables_the_carve_out(self):
        from django.conf import settings as dj_settings
        if hasattr(dj_settings, "DEMO_LOGIN_USERNAME"):
            del dj_settings.DEMO_LOGIN_USERNAME
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace(username="XXXX")), False)

    @override_settings(DEMO_MODE="true", DEMO_LOGIN_USERNAME="demo_visitor")
    def test_demo_mode_must_be_the_boolean_true(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, self.user), False)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="demo_visitor")
    def test_user_without_username_attribute_does_not_qualify(self):
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace()), False)
        self.assertIs(perms._is_demo_visitor(MEMBER, SimpleNamespace(username=None)), False)


class CanModifyOthersContentTests(TestCase):
    def setUp(self):
        self.owner = _make_user("cm_owner")
        self.board = _make_board(self.owner)
        self.mod = _make_user("cm_mod")
        _make_membership(self.board, self.mod, MEMBER)
        BoardMembership.objects.filter(board=self.board, user=self.mod).update(is_moderator=True)
        self.plain = _make_user("cm_plain")
        _make_membership(self.board, self.plain, MEMBER)

    def _fresh(self):
        return type(self.board).objects.get(pk=self.board.pk)

    def test_admin_and_site_admin_roles_always_can(self):
        self.assertIs(can_modify_others_content(self._fresh(), ADMIN, self.plain), True)
        self.assertIs(can_modify_others_content(self._fresh(), SITE_ADMIN, self.plain), True)

    def test_board_owner_can_regardless_of_role_argument(self):
        self.assertIs(can_modify_others_content(self._fresh(), MEMBER, self.owner), True)

    def test_moderator_member_can_and_plain_member_cannot(self):
        self.assertIs(can_modify_others_content(self._fresh(), MEMBER, self.mod), True)
        self.assertIs(can_modify_others_content(self._fresh(), MEMBER, self.plain), False)

    def test_non_member_cannot(self):
        self.assertIs(can_modify_others_content(self._fresh(), MEMBER, _make_user("cm_x")), False)

    @override_settings(DEMO_MODE=True, DEMO_LOGIN_USERNAME="cm_plain")
    def test_demo_visitor_can(self):
        self.assertIs(can_modify_others_content(self._fresh(), MEMBER, self.plain), True)

    def test_cached_membership_of_another_user_is_not_trusted(self):
        """A stamp left by a different user's resolution must not leak the
        moderator flag (the #1107 escalation the check exists to prevent)."""
        board = self._fresh()
        board._cached_membership = BoardMembership.objects.get(board=board, user=self.mod)
        self.assertIs(can_modify_others_content(board, MEMBER, self.plain), False)

    def test_cached_membership_of_the_same_user_is_used_without_a_query(self):
        board = self._fresh()
        board._cached_membership = BoardMembership.objects.get(board=board, user=self.mod)
        with self.assertNumQueries(0):
            self.assertIs(can_modify_others_content(board, MEMBER, self.mod), True)

    def test_prefetched_memberships_are_scanned_for_the_right_user(self):
        board = self._fresh()
        rows = list(BoardMembership.objects.filter(board=board))
        board._prefetched_memberships = rows
        stranger = _make_user("cm_y")
        with self.assertNumQueries(0):
            self.assertIs(can_modify_others_content(board, MEMBER, self.mod), True)
            self.assertIs(can_modify_others_content(board, MEMBER, self.plain), False)
            self.assertIs(can_modify_others_content(board, MEMBER, stranger), False)


class PermissionClassTests(TestCase):
    def setUp(self):
        self.owner = _make_user("pc_owner")
        self.board = _make_board(self.owner)
        self.member = _make_user("pc_member")
        _make_membership(self.board, self.member, MEMBER)
        self.outsider = _make_user("pc_outsider")
        # A child object exposing ``.board`` — the permission classes accept
        # either a board or any object that points at one.
        self.child = SimpleNamespace(board=self.board)

    def _req(self, user):
        return SimpleNamespace(user=user)

    def test_is_board_member(self):
        perm = IsBoardMember()
        for obj in (self.board, self.child):
            self.assertTrue(perm.has_object_permission(self._req(self.member), None, obj))
            self.assertTrue(perm.has_object_permission(self._req(self.owner), None, obj))
            self.assertFalse(perm.has_object_permission(self._req(self.outsider), None, obj))

    def test_is_board_admin_or_owner(self):
        perm = IsBoardAdminOrOwner()
        boss = _make_user("pc_boss", can_access_all_content=True)
        for obj in (self.board, self.child):
            self.assertTrue(perm.has_object_permission(self._req(self.owner), None, obj))
            self.assertTrue(perm.has_object_permission(self._req(boss), None, obj))
            self.assertFalse(perm.has_object_permission(self._req(self.member), None, obj))
            self.assertFalse(perm.has_object_permission(self._req(self.outsider), None, obj))


# ── #1930: predicates and analytics filter added after the 2026-10-09 baseline ──
#
# The weekly mutation run of 2026-10-25 found every mutant of these survived:
# the RBAC suites in the `permissions` target never call them, and the suites
# that do (the consumers, the change feed, the analytics summary) are not in
# the target's test list. Each is pinned here directly.


class BroadcastVisibilityPredicateTests(SimpleTestCase):
    def test_site_admin_roster_visible_to_either_kind_of_site_admin(self):
        self.assertIs(perms.site_admin_roster_visible(SITE_ADMIN, False), True)
        self.assertIs(perms.site_admin_roster_visible(ADMIN, True), True)
        self.assertIs(perms.site_admin_roster_visible(SITE_ADMIN, True), True)

    def test_site_admin_roster_hidden_from_everyone_else(self):
        self.assertIs(perms.site_admin_roster_visible(ADMIN, False), False)
        self.assertIs(perms.site_admin_roster_visible(MEMBER, None), False)
        # Only the boolean True counts: a truthy non-bool must not open it.
        self.assertIs(perms.site_admin_roster_visible(VIEWER, 1), False)

    def test_invite_field_visible_only_to_admin_roles(self):
        self.assertIs(perms.invite_field_visible(ADMIN), True)
        self.assertIs(perms.invite_field_visible(SITE_ADMIN), True)
        self.assertIs(perms.invite_field_visible(MEMBER), False)
        self.assertIs(perms.invite_field_visible(VIEWER), False)
        self.assertIs(perms.invite_field_visible(None), False)

    def test_per_user_events_are_the_star_and_saved_filter_events(self):
        self.assertEqual(
            perms.PER_USER_EVENTS,
            frozenset({"board.star_changed", "saved_filter.created", "saved_filter.deleted"}),
        )

    def test_per_user_event_reaches_only_its_owner(self):
        self.assertIs(perms.per_user_event_visible(5, {"user_id": 5}), True)
        self.assertIs(perms.per_user_event_visible(5, {"user_id": 6}), False)

    def test_per_user_event_fails_closed(self):
        self.assertIs(perms.per_user_event_visible(None, {"user_id": None}), False)
        self.assertIs(perms.per_user_event_visible(None, {"user_id": 5}), False)
        self.assertIs(perms.per_user_event_visible(5, {}), False)
        self.assertIs(perms.per_user_event_visible(5, {"user_id": None}), False)
        # A non-dict payload is refused before it is read.
        self.assertIs(perms.per_user_event_visible(5, "not-a-dict"), False)
        self.assertIs(perms.per_user_event_visible(5, None), False)

    def test_actor_scoped_events_and_fields(self):
        self.assertEqual(
            perms.ACTOR_SCOPED_FIELD_EVENTS,
            frozenset({"board.created", "board.updated", "group.created", "group.updated"}),
        )
        self.assertEqual(perms.ACTOR_SCOPED_FIELDS, frozenset({"is_starred"}))

    def test_actor_scoped_fields_visible_only_to_the_actor(self):
        self.assertIs(perms.actor_scoped_fields_visible(7, 7), True)
        self.assertIs(perms.actor_scoped_fields_visible(7, 8), False)
        self.assertIs(perms.actor_scoped_fields_visible(7, None), False)
        self.assertIs(perms.actor_scoped_fields_visible(None, 7), False)
        self.assertIs(perms.actor_scoped_fields_visible(None, None), False)

    def test_is_starred_dropped_for_other_viewers_on_scoped_events(self):
        data = {"id": 1, "name": "b", "is_starred": True}
        for event in ("board.created", "board.updated", "group.created", "group.updated"):
            self.assertEqual(
                perms.without_actor_scoped_fields(event, data, 7, 8), {"id": 1, "name": "b"},
            )
        self.assertEqual(data, {"id": 1, "name": "b", "is_starred": True})  # not mutated

    def test_is_starred_kept_for_the_actor_and_for_other_events(self):
        data = {"id": 1, "is_starred": True}
        self.assertIs(perms.without_actor_scoped_fields("board.updated", data, 7, 7), data)
        self.assertIs(perms.without_actor_scoped_fields("card.updated", data, 7, 8), data)
        self.assertIs(perms.without_actor_scoped_fields("board.updated", {"id": 1}, 7, 8)["id"], 1)
        self.assertEqual(perms.without_actor_scoped_fields("board.updated", "raw", 7, 8), "raw")


class InvalidExplicitRoleTests(TestCase):
    def test_unrecognized_explicit_role_is_no_access_even_with_a_group_role(self):
        """An out-of-set board role decides the ladder (no access); groups are not consulted."""
        owner = _make_user("ier_owner")
        user = _make_user("ier_user")
        group = Group.objects.create(name="ier_g", owner=owner)
        GroupMembership.objects.create(group=group, user=user, role="admin")
        board = _make_board(owner)
        board.group = group
        board.save(update_fields=["group"])
        BoardMembership.objects.create(board=board, user=user, role="bogus")
        board = type(board).objects.select_related(perms.GROUP_ANCESTOR_SELECT_RELATED).get(pk=board.pk)
        self.assertIsNone(get_board_role(user, board))
        self.assertFalse(hasattr(board, "_cached_membership"))


def _named_panel(board, request):  # pragma: no cover - never invoked by the filter
    """A plain-function panel with no ``min_role``."""
    return {}


class _StubPanel:
    """A panel callable whose ``__module__``, ``__qualname__`` and ``min_role`` are set per instance.

    ``module`` / ``qualname`` of ``None`` make the attribute absent (an
    ``AttributeError``, as for an object that has none), so the log falls back
    to ``?`` and the type name. ``raise_on_min_role`` makes reading
    ``min_role`` raise: the "unreadable" case.
    """

    def __init__(self, *, module=None, qualname=None, min_role=None, raise_on_min_role=False):
        object.__setattr__(self, "_attrs", {
            "__module__": module, "__qualname__": qualname, "min_role": min_role,
        })
        object.__setattr__(self, "_raise", raise_on_min_role)

    def __getattribute__(self, name):
        attrs = object.__getattribute__(self, "_attrs")
        if name == "min_role" and object.__getattribute__(self, "_raise"):
            raise RuntimeError("unreadable")
        if name in attrs:
            if attrs[name] is None and name != "min_role":
                raise AttributeError(name)
            return attrs[name]
        return object.__getattribute__(self, name)

    def __call__(self, board, request):  # pragma: no cover - never invoked by the filter
        return {}


class AnalyticsPanelFilterTests(SimpleTestCase):
    def setUp(self):
        from boards import hooks

        self.hooks = hooks
        self.saved = list(hooks.ANALYTICS_EXTENSIONS)
        hooks.ANALYTICS_EXTENSIONS.clear()

    def tearDown(self):
        self.hooks.ANALYTICS_EXTENSIONS[:] = self.saved

    @staticmethod
    def _fn(min_role):
        def panel(board, request):  # pragma: no cover - never invoked by the filter
            return {}
        panel.min_role = min_role
        return panel

    def test_default_is_admin(self):
        self.assertEqual(perms.ANALYTICS_PANEL_DEFAULT_MIN_ROLE, "admin")
        self.assertEqual(perms.analytics_panel_min_role(_named_panel), "admin")
        self.assertEqual(perms.analytics_panel_min_role(self._fn(None)), "admin")

    def test_declared_role_is_returned(self):
        for role in ("viewer", "collaborator", "member", "admin", "site_admin"):
            self.assertEqual(perms.analytics_panel_min_role(self._fn(role)), role)

    def test_unknown_string_or_non_string_falls_back_to_admin_and_logs(self):
        for bad in ("owner", "Viewer", 3):
            with self.assertLogs("boards.permissions", level="WARNING") as logs:
                self.assertEqual(perms.analytics_panel_min_role(self._fn(bad)), "admin")
            self.assertEqual(len(logs.records), 1)

    def test_invalid_min_role_log_names_module_and_qualname(self):
        fn = self._fn(3)
        with self.assertLogs("boards.permissions", level="WARNING") as logs:
            perms.analytics_panel_min_role(fn)
        self.assertEqual(
            logs.records[0].getMessage(),
            f"ANALYTICS_EXTENSIONS callable {__name__}.{fn.__qualname__} "
            "has an invalid min_role (int); treated as admin",
        )

    def test_invalid_min_role_log_falls_back_for_a_callable_without_names(self):
        with self.assertLogs("boards.permissions", level="WARNING") as logs:
            perms.analytics_panel_min_role(_StubPanel(min_role=3))
        self.assertEqual(
            logs.records[0].getMessage(),
            "ANALYTICS_EXTENSIONS callable ?._StubPanel has an invalid min_role (int); "
            "treated as admin",
        )

    def test_filter_ranks_the_caller_inclusively(self):
        viewer, member, admin = self._fn("viewer"), self._fn("member"), self._fn("admin")
        self.hooks.ANALYTICS_EXTENSIONS.extend([viewer, member, admin, _named_panel])
        self.assertEqual(perms.analytics_extensions_for_role("viewer"), [viewer])
        self.assertEqual(perms.analytics_extensions_for_role("collaborator"), [viewer])
        self.assertEqual(perms.analytics_extensions_for_role("member"), [viewer, member])
        everything = [viewer, member, admin, _named_panel]
        self.assertEqual(perms.analytics_extensions_for_role("admin"), everything)
        self.assertEqual(perms.analytics_extensions_for_role(SITE_ADMIN), everything)

    def test_unknown_or_missing_role_gets_no_panel(self):
        self.hooks.ANALYTICS_EXTENSIONS.append(self._fn("viewer"))
        self.assertEqual(perms.analytics_extensions_for_role(None), [])
        self.assertEqual(perms.analytics_extensions_for_role("owner"), [])

    def test_unreadable_min_role_skips_only_that_panel_and_logs_its_names(self):
        bad = _StubPanel(module="ext.panels", qualname="Panels.velocity", raise_on_min_role=True)
        good = self._fn("viewer")
        self.hooks.ANALYTICS_EXTENSIONS.extend([bad, good])
        with self.assertLogs("boards.permissions", level="WARNING") as logs:
            self.assertEqual(perms.analytics_extensions_for_role(SITE_ADMIN), [good])
        self.assertEqual(
            [r.getMessage() for r in logs.records],
            ["ANALYTICS_EXTENSIONS callable ext.panels.Panels.velocity skipped: "
             "min_role unreadable (RuntimeError)"],
        )

    def test_unreadable_min_role_log_falls_back_for_a_callable_without_names(self):
        self.hooks.ANALYTICS_EXTENSIONS.append(_StubPanel(raise_on_min_role=True))
        with self.assertLogs("boards.permissions", level="WARNING") as logs:
            self.assertEqual(perms.analytics_extensions_for_role(SITE_ADMIN), [])
        self.assertEqual(
            logs.records[0].getMessage(),
            "ANALYTICS_EXTENSIONS callable ?._StubPanel skipped: min_role unreadable (RuntimeError)",
        )
