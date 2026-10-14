"""Django admin board and membership forms: unchanged fields and the rendered-state check.

- ``BoardAdmin`` and ``BoardMembershipAdmin`` write only the fields the
  administrator changed; a field left alone (a board's ``owner`` or
  ``group``, a membership's ``role`` or ``is_moderator``) keeps the value
  stored when the save runs.
- Both change forms render a hidden ``row_version`` and refuse a submission
  when a field the form writes was changed after the page was rendered. A
  refused submission writes nothing, records no board event and broadcasts
  nothing.

A change committed after the request loaded the row is simulated by
committing it from the form's ``clean``, the step between the load and
``save_model``'s row lock. ``BoardAdminInterleavingTests`` reproduces that
ordering with real threads and connections on PostgreSQL.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django import forms
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import User
from boards.admin import (
    BoardAdmin,
    BoardAdminForm,
    BoardMembershipAdmin,
    BoardMembershipAdminForm,
)
from boards.models import Board, BoardEvent, BoardMembership
from groups.models import Group

PW = "pw123456!x"

_PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)


def page_data(response, *, row_version=True, **overrides):
    """POST data for the change form rendered in *response*, as a browser submits it."""
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        if field.disabled or (name == "row_version" and not row_version):
            continue
        value = form[name].value()
        if isinstance(field, forms.BooleanField):
            if value:
                data[name] = "on"
        elif isinstance(value, (list, tuple)):
            data[name] = [str(v) for v in value]
        elif value is not None:
            data[name] = str(value)
    for name, value in overrides.items():
        if value is False:
            data.pop(name, None)
        elif value is True:
            data[name] = "on"
        else:
            data[name] = value
    return data


def patched_clean(form_class, change):
    """Run *change* after *form_class*'s clean: after the row is loaded, before the lock."""
    original = form_class.clean

    def clean_then_change(form):
        cleaned = original(form)
        change()
        return cleaned

    return patch.object(form_class, "clean", clean_then_change)


class _Fixtures:
    def make_fixtures(self):
        self.root = User.objects.create_superuser(
            username="root_rvb", email="rootrvb@example.com", password=PW
        )
        self.owner = User.objects.create_user(
            username="owner_rv", email="ownerrv@example.com", password=PW
        )
        self.heir = User.objects.create_user(
            username="heir_rv", email="heirrv@example.com", password=PW
        )
        self.member = User.objects.create_user(
            username="member_rv", email="memberrv@example.com", password=PW
        )
        self.group = Group.objects.create(name="grp_rv", owner=self.owner)
        self.other_group = Group.objects.create(name="grp2_rv", owner=self.owner)
        self.board = Board.objects.create(
            name="Board RV", owner=self.owner, group=self.group, allowed_priorities=["low"]
        )
        self.membership = BoardMembership.objects.create(
            board=self.board, user=self.member, role=BoardMembership.Role.MEMBER
        )

    def board_url(self):
        return reverse("admin:boards_board_change", args=[self.board.pk])

    def membership_url(self):
        return reverse("admin:boards_boardmembership_change", args=[self.membership.pk])

    def stored_board(self):
        return Board.objects.get(pk=self.board.pk)

    def stored_membership(self):
        return BoardMembership.objects.get(pk=self.membership.pk)

    def render(self, client, url):
        response = client.get(url)
        assert response.status_code == 200, response.status_code
        return response

    def events(self, name):
        return BoardEvent.objects.filter(event=name).count()


@_PLAIN_STATIC
class BoardAdminConcurrentEditTests(_Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.root)

    def post(self, data, change=None):
        """POST to the board form, with broadcasts captured and run on commit."""
        with patch("boards.broadcast.broadcast_board_event") as board_bc, \
             patch("boards.admin._broadcast_group_event") as group_bc, \
             self.captureOnCommitCallbacks(execute=True):
            if change is None:
                response = self.client.post(self.board_url(), data)
            else:
                with patched_clean(BoardAdminForm, change):
                    response = self.client.post(self.board_url(), data)
        return response, board_bc, group_bc

    def test_change_page_renders_row_version(self):
        self.assertContains(
            self.render(self.client, self.board_url()), 'type="hidden" name="row_version"'
        )

    def test_add_page_has_no_row_version(self):
        response = self.client.get(reverse("admin:boards_board_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="row_version"')

    def test_matching_row_version_saves_and_broadcasts(self):
        data = page_data(self.render(self.client, self.board_url()), owner=str(self.heir.pk))
        response, board_bc, group_bc = self.post(data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_board().owner_id, self.heir.pk)
        board_bc.assert_called_once()
        group_bc.assert_called_once()

    def test_unchanged_owner_and_group_keep_changes_made_after_load(self):
        data = page_data(
            self.render(self.client, self.board_url()), row_version=False, name="Renamed RV"
        )

        def transfer():
            Board.objects.filter(pk=self.board.pk).update(
                owner=self.heir, group=self.other_group
            )

        response, board_bc, _ = self.post(data, change=transfer)
        self.assertEqual(response.status_code, 302)
        stored = self.stored_board()
        self.assertEqual(stored.name, "Renamed RV")
        self.assertEqual(stored.owner_id, self.heir.pk)
        self.assertEqual(stored.group_id, self.other_group.pk)
        board_bc.assert_called_once()

    def test_owner_change_after_render_refuses_the_save(self):
        data = page_data(self.render(self.client, self.board_url()), name="Renamed RV")
        Board.objects.filter(pk=self.board.pk).update(owner=self.heir)
        before = self.events("board.updated")
        response, board_bc, group_bc = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, BoardAdminForm.stale_form_message)
        self.assertEqual(self.stored_board().name, "Board RV")
        self.assertEqual(self.events("board.updated"), before)
        board_bc.assert_not_called()
        group_bc.assert_not_called()

    def test_group_change_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(self.client, self.board_url()), name="Renamed RV")
        before = self.events("board.updated")
        move = lambda: Board.objects.filter(pk=self.board.pk).update(  # noqa: E731
            group=self.other_group
        )
        response, board_bc, group_bc = self.post(data, change=move)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.board_url())
        self.assertContains(self.client.get(response["Location"]), BoardAdmin.stale_save_message)
        self.assertEqual(self.stored_board().name, "Board RV")
        self.assertEqual(self.events("board.updated"), before)
        board_bc.assert_not_called()
        group_bc.assert_not_called()

    def test_omitted_row_version_saves_the_changed_field(self):
        data = page_data(
            self.render(self.client, self.board_url()), row_version=False, owner=str(self.heir.pk)
        )
        response, board_bc, _ = self.post(data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_board().owner_id, self.heir.pk)
        board_bc.assert_called_once()


@_PLAIN_STATIC
class BoardMembershipAdminConcurrentEditTests(_Fixtures, TestCase):
    def setUp(self):
        self.make_fixtures()
        self.client.force_login(self.root)

    def post(self, data, change=None):
        with patch("boards.broadcast.broadcast_board_event") as board_bc, \
             self.captureOnCommitCallbacks(execute=True):
            if change is None:
                response = self.client.post(self.membership_url(), data)
            else:
                with patched_clean(BoardMembershipAdminForm, change):
                    response = self.client.post(self.membership_url(), data)
        return response, board_bc

    def test_change_page_renders_row_version(self):
        self.assertContains(
            self.render(self.client, self.membership_url()), 'type="hidden" name="row_version"'
        )

    def test_matching_row_version_saves_and_broadcasts(self):
        data = page_data(
            self.render(self.client, self.membership_url()), role=BoardMembership.Role.ADMIN
        )
        response, board_bc = self.post(data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_membership().role, BoardMembership.Role.ADMIN)
        board_bc.assert_called_once()
        self.assertEqual(board_bc.call_args[0][1], "member.updated")

    def test_unchanged_role_keeps_a_demotion_made_after_load(self):
        data = page_data(
            self.render(self.client, self.membership_url()), row_version=False, is_moderator=True
        )
        demote = lambda: BoardMembership.objects.filter(pk=self.membership.pk).update(  # noqa: E731
            role=BoardMembership.Role.VIEWER
        )
        response, board_bc = self.post(data, change=demote)
        self.assertEqual(response.status_code, 302)
        stored = self.stored_membership()
        self.assertTrue(stored.is_moderator)
        self.assertEqual(stored.role, BoardMembership.Role.VIEWER)
        board_bc.assert_called_once()

    def test_unchanged_moderator_flag_keeps_a_revocation_made_after_load(self):
        BoardMembership.objects.filter(pk=self.membership.pk).update(is_moderator=True)
        data = page_data(
            self.render(self.client, self.membership_url()),
            row_version=False, role=BoardMembership.Role.COLLABORATOR,
        )
        revoke = lambda: BoardMembership.objects.filter(pk=self.membership.pk).update(  # noqa: E731
            is_moderator=False
        )
        response, _ = self.post(data, change=revoke)
        self.assertEqual(response.status_code, 302)
        stored = self.stored_membership()
        self.assertEqual(stored.role, BoardMembership.Role.COLLABORATOR)
        self.assertFalse(stored.is_moderator)

    def test_role_change_after_render_refuses_the_save(self):
        data = page_data(self.render(self.client, self.membership_url()), is_moderator=True)
        BoardMembership.objects.filter(pk=self.membership.pk).update(
            role=BoardMembership.Role.VIEWER
        )
        before = self.events("member.updated")
        response, board_bc = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, BoardMembershipAdminForm.stale_form_message)
        self.assertFalse(self.stored_membership().is_moderator)
        self.assertEqual(self.events("member.updated"), before)
        board_bc.assert_not_called()

    def test_role_change_after_load_is_refused_under_the_lock(self):
        data = page_data(self.render(self.client, self.membership_url()), is_moderator=True)
        before = self.events("member.updated")
        demote = lambda: BoardMembership.objects.filter(pk=self.membership.pk).update(  # noqa: E731
            role=BoardMembership.Role.VIEWER
        )
        response, board_bc = self.post(data, change=demote)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.membership_url())
        self.assertContains(
            self.client.get(response["Location"]), BoardMembershipAdmin.stale_save_message
        )
        self.assertFalse(self.stored_membership().is_moderator)
        self.assertEqual(self.events("member.updated"), before)
        board_bc.assert_not_called()

    # -- moving a membership to another board -----------------------------------

    def other_board(self):
        return Board.objects.create(
            name="Other RV", owner=self.owner, allowed_priorities=["low"]
        )

    def test_move_locks_both_boards_in_pk_order_and_saves(self):
        other = self.other_board()
        data = page_data(self.render(self.client, self.membership_url()), board=str(other.pk))
        with CaptureQueriesContext(connection) as ctx:
            response, board_bc = self.post(data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_membership().board_id, other.pk)
        board_bc.assert_called_once()
        self.assertEqual(board_bc.call_args[0][0], other.pk)

        table = Board._meta.db_table
        sqls = [q["sql"] for q in ctx.captured_queries]
        # One statement over both boards (the stored one and the target),
        # ordered by pk, so concurrent saves lock them in the same order.
        both = [
            i for i, sql in enumerate(sqls)
            if f'FROM "{table}" WHERE "{table}"."id" IN (' in sql and "ORDER BY" in sql
        ]
        self.assertEqual(len(both), 1, sqls)
        lock_sql = sqls[both[0]]
        self.assertRegex(lock_sql, r"ORDER BY \S+ ASC")
        for pk in (self.board.pk, other.pk):
            self.assertRegex(lock_sql, rf"IN \([^)]*\b{pk}\b")
        if connection.vendor == "postgresql":
            self.assertIn("FOR NO KEY UPDATE", lock_sql)
        # Taken before the membership row is written.
        membership_table = BoardMembership._meta.db_table
        update = next(
            i for i, sql in enumerate(sqls) if sql.startswith(f'UPDATE "{membership_table}"')
        )
        self.assertLess(both[0], update)

    def test_edit_without_move_takes_no_two_board_lock(self):
        data = page_data(self.render(self.client, self.membership_url()), is_moderator=True)
        table = Board._meta.db_table
        with CaptureQueriesContext(connection) as ctx:
            response, _ = self.post(data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(
            f'"{table}"."id" IN (' in q["sql"] for q in ctx.captured_queries
        ))

    def test_stale_move_is_refused_with_no_write_or_broadcast(self):
        other = self.other_board()
        data = page_data(self.render(self.client, self.membership_url()), board=str(other.pk))
        BoardMembership.objects.filter(pk=self.membership.pk).update(
            role=BoardMembership.Role.VIEWER
        )
        before = self.events("member.updated")
        response, board_bc = self.post(data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, BoardMembershipAdminForm.stale_form_message)
        self.assertEqual(self.stored_membership().board_id, self.board.pk)
        self.assertEqual(self.events("member.updated"), before)
        board_bc.assert_not_called()

    def test_move_with_a_change_after_load_is_refused_under_the_lock(self):
        other = self.other_board()
        data = page_data(self.render(self.client, self.membership_url()), board=str(other.pk))
        before = self.events("member.updated")
        demote = lambda: BoardMembership.objects.filter(pk=self.membership.pk).update(  # noqa: E731
            role=BoardMembership.Role.VIEWER
        )
        response, board_bc = self.post(data, change=demote)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.membership_url())
        self.assertEqual(self.stored_membership().board_id, self.board.pk)
        self.assertEqual(self.events("member.updated"), before)
        board_bc.assert_not_called()

    def test_omitted_row_version_saves_the_changed_field(self):
        data = page_data(
            self.render(self.client, self.membership_url()),
            row_version=False, role=BoardMembership.Role.ADMIN,
        )
        response, board_bc = self.post(data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.stored_membership().role, BoardMembership.Role.ADMIN)
        board_bc.assert_called_once()


@skipUnless(connection.vendor == "postgresql", "interleaving needs separate PostgreSQL connections")
@_PLAIN_STATIC
class BoardAdminInterleavingTests(_Fixtures, TransactionTestCase):
    """The load-to-lock ordering with real threads and connections on PostgreSQL."""

    def setUp(self):
        self.make_fixtures()

    def root_client(self):
        client = Client()
        client.force_login(User.objects.get(pk=self.root.pk))
        return client

    def post_paused(self, form_class, url, data, concurrent_change):
        """POST in a thread paused after *form_class*'s clean while *concurrent_change* commits."""
        original = form_class.clean
        paused, release = threading.Event(), threading.Event()
        results, errors = {}, []

        def clean_then_pause(form):
            cleaned = original(form)
            if threading.current_thread().name == "request" and not paused.is_set():
                paused.set()
                release.wait(timeout=15)
            return cleaned

        def worker():
            try:
                with patch("boards.broadcast.broadcast_board_event") as board_bc:
                    results["response"] = self.root_client().post(url, data)
                    results["broadcasts"] = board_bc.call_count
            except Exception as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(form_class, "clean", clean_then_pause):
            thread = threading.Thread(target=worker, name="request")
            thread.start()
            try:
                self.assertTrue(paused.wait(timeout=15), "request never reached the pause")
                concurrent_change()
            finally:
                release.set()
                thread.join(timeout=30)
        self.assertEqual(errors, [])
        return results["response"], results["broadcasts"]

    def test_owner_transfer_committed_mid_request_is_kept_and_refuses_the_save(self):
        data = page_data(self.render(self.root_client(), self.board_url()), name="Renamed RV")
        response, broadcasts = self.post_paused(
            BoardAdminForm, self.board_url(), data,
            lambda: Board.objects.filter(pk=self.board.pk).update(owner=self.heir),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.board_url())
        stored = self.stored_board()
        self.assertEqual(stored.owner_id, self.heir.pk)
        self.assertEqual(stored.name, "Board RV")
        self.assertEqual(broadcasts, 0)
        self.assertEqual(self.events("board.updated"), 0)

    def test_unchanged_owner_keeps_a_transfer_committed_mid_request(self):
        data = page_data(
            self.render(self.root_client(), self.board_url()),
            row_version=False, name="Renamed RV",
        )
        response, broadcasts = self.post_paused(
            BoardAdminForm, self.board_url(), data,
            lambda: Board.objects.filter(pk=self.board.pk).update(owner=self.heir),
        )
        self.assertEqual(response.status_code, 302)
        stored = self.stored_board()
        self.assertEqual(stored.owner_id, self.heir.pk)
        self.assertEqual(stored.name, "Renamed RV")
        self.assertEqual(broadcasts, 1)

    def test_role_change_committed_mid_request_is_kept_and_refuses_the_save(self):
        data = page_data(self.render(self.root_client(), self.membership_url()), is_moderator=True)
        response, broadcasts = self.post_paused(
            BoardMembershipAdminForm, self.membership_url(), data,
            lambda: BoardMembership.objects.filter(pk=self.membership.pk).update(
                role=BoardMembership.Role.VIEWER
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.membership_url())
        stored = self.stored_membership()
        self.assertEqual(stored.role, BoardMembership.Role.VIEWER)
        self.assertFalse(stored.is_moderator)
        self.assertEqual(broadcasts, 0)

    def test_unchanged_role_keeps_a_change_committed_mid_request(self):
        data = page_data(
            self.render(self.root_client(), self.membership_url()),
            row_version=False, is_moderator=True,
        )
        response, broadcasts = self.post_paused(
            BoardMembershipAdminForm, self.membership_url(), data,
            lambda: BoardMembership.objects.filter(pk=self.membership.pk).update(
                role=BoardMembership.Role.VIEWER
            ),
        )
        self.assertEqual(response.status_code, 302)
        stored = self.stored_membership()
        self.assertEqual(stored.role, BoardMembership.Role.VIEWER)
        self.assertTrue(stored.is_moderator)
        self.assertEqual(broadcasts, 1)
