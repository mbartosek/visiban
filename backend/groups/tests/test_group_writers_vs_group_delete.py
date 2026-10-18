"""Group writers other than invite redemption racing a group delete (#1719).

The follow-up to #1638 (``test_group_invite_vs_group_delete``). Each writer
here inserts or updates a row that points at a group, or the group row
itself, and used to take no group lock first. Racing a delete of the same
group it failed the row's deferred foreign key at COMMIT, raised
``DoesNotExist`` or ``DatabaseError`` from a row the delete removed, or had
``save()`` fall back to an ``INSERT`` under the deleted group: a 500 either
way. Each now locks the group row first (``lock_group_for_child_write`` /
``lock_group_for_board_placement`` ``FOR KEY SHARE``, ``lock_group_row``
``FOR NO KEY UPDATE`` or ``FOR UPDATE``), the delete's group -> child order:

* writer first: it commits, then the delete removes the group with what the
  writer wrote (or detaches the board) and answers 204;
* delete first: the writer finds the group gone and answers 404, or the
  unknown-parent 400 for a subgroup create, with nothing written.

Every test runs both orders with the harness from #1638: the first request
pauses inside its transaction holding its locks, the second is proved to wait
on a PostgreSQL lock (``boards.tests.lock_wait``), then both are released.
PostgreSQL only; ``LockHelperUnitTests`` runs everywhere.
"""

import io
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib import admin as dj_admin
from django.db import transaction
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import SiteSetting, User
from boards import broadcast as _bb
from boards.models import Board, BoardMembership
from groups import views as _views
from groups.models import Group, GroupFavorite, GroupInviteLink, GroupLabel, GroupMembership
from groups.services import (
    GroupGone,
    lock_group_for_child_write,
    lock_group_row,
    transfer_group_ownership,
)
from groups.tests.test_group_invite_vs_group_delete import _GroupDeleteRaceBase

TRELLO_FIXTURE = Path(__file__).resolve().parents[2] / "boards" / "tests" / "fixtures" / "trello_export.json"


def _checkpoint():
    """A hold point for a holder that is not a request (patched by ``_race``)."""


def _this_module():
    import sys

    return sys.modules[__name__]


class LockHelperUnitTests(TestCase):
    """The #1719 helpers answer False, and the transfer raises GroupGone, once the group is gone."""

    def setUp(self):
        self.owner = User.objects.create_user(username="o1719u", password="p")
        self.group = Group.objects.create(name="U1719", owner=self.owner)

    def test_lock_helpers_present_and_missing(self):
        missing = self.group.pk + 10_000
        with transaction.atomic():
            self.assertTrue(lock_group_for_child_write(self.group.pk))
            self.assertFalse(lock_group_for_child_write(missing))
            self.assertTrue(lock_group_row(self.group.pk))
            self.assertTrue(lock_group_row(self.group.pk, no_key=False))
            self.assertFalse(lock_group_row(missing))

    def test_transfer_of_a_missing_group_raises_group_gone(self):
        with self.assertRaises(GroupGone) as ctx:
            transfer_group_ownership(self.group.pk + 10_000, self.owner.pk)
        self.assertEqual(str(ctx.exception), "This group no longer exists.")


@override_settings(INVITE_EMAIL_ENABLED=True, DEMO_MODE=False)
class GroupWriterVsGroupDeleteTests(_GroupDeleteRaceBase):
    """REST writers on the deleted group itself."""

    # The first on_commit a writer registers comes after its write, inside
    # its transaction, with its locks held.
    COMMIT_HOLD = (transaction, "on_commit", False)
    # Invite link create registers no on_commit; this serializer is built
    # right after the insert, still inside the transaction.
    INVITE_CREATE_HOLD = (_views, "GroupInviteLinkSerializer", False)

    def setUp(self):
        super().setUp()
        # The middleware creates the SiteSetting row on first use, and that
        # save registers an on_commit: create it now, or COMMIT_HOLD pauses
        # the writer there, before its transaction starts.
        SiteSetting.get()
        self.heir = User.objects.create_user(username="h1719", password="p", email="h1719@example.com")
        GroupMembership.objects.create(group=self.group, user=self.heir, role=GroupMembership.Role.ADMIN)
        GroupMembership.objects.create(group=self.group, user=self.joiner, role=GroupMembership.Role.MEMBER)
        self.label = GroupLabel.objects.create(group=self.group, name="L1719", color="#123456")

    def _client(self):
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=self.sender.pk))
        return client

    # -- writers ---------------------------------------------------------

    def _invite_create(self):
        return self._client().post(
            f"/api/v1/groups/{self.group_id}/invite-links/", {"name": "N1719"}, format="json",
        )

    def _invite_send(self):
        return self._client().post(
            f"/api/v1/groups/{self.group_id}/invite-links/send/", {"email": "r1719@example.org"}, format="json",
        )

    def _invite_revoke(self):
        return self._client().delete(f"/api/v1/groups/{self.group_id}/invite-links/{self.link.pk}/")

    def _label_create(self):
        return self._client().post(
            f"/api/v1/groups/{self.group_id}/labels/", {"name": "N1719", "color": "#654321"}, format="json",
        )

    def _label_patch(self):
        return self._client().patch(
            f"/api/v1/groups/{self.group_id}/labels/{self.label.pk}/", {"name": "P1719"}, format="json",
        )

    def _star(self):
        return self._client().post(f"/api/v1/groups/{self.group_id}/star/")

    def _subgroup_create(self):
        return self._client().post("/api/v1/groups/", {"name": "N1719", "parent": self.group_id}, format="json")

    def _transfer(self):
        return self._client().post(
            f"/api/v1/groups/{self.group_id}/transfer-ownership/",
            {"new_owner_id": self.heir.pk, "confirmation": "G1638"}, format="json",
        )

    def _rename(self):
        return self._client().patch(f"/api/v1/groups/{self.group_id}/", {"name": "P1719"}, format="json")

    def _board_defaults(self):
        return self._client().patch(
            f"/api/v1/groups/{self.group_id}/board-defaults/",
            {"default_board_member_role": "viewer"}, format="json",
        )

    def _member_role(self):
        return self._client().patch(
            f"/api/v1/groups/{self.group_id}/members/{self.joiner.pk}/", {"role": "admin"}, format="json",
        )

    # -- assertions ------------------------------------------------------

    def _assert_writer_then_delete(self, written, deleted, ok=(200, 201, 202, 204)):
        self.assertIn(written.status_code, ok, getattr(written, "content", written)[:300])
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self._assert_everything_gone()

    def _assert_delete_then_writer(self, deleted, written, expected=404):
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(written.status_code, expected, written.content[:300])
        self._assert_everything_gone()

    def _assert_everything_gone(self):
        gid = self.group_id
        self.assertFalse(Group.objects.filter(pk=gid).exists())
        self.assertFalse(Group.objects.filter(parent_id=gid).exists())
        self.assertFalse(GroupMembership.objects.filter(group_id=gid).exists())
        self.assertFalse(GroupInviteLink.objects.filter(group_id=gid).exists())
        self.assertFalse(GroupLabel.objects.filter(group_id=gid).exists())
        self.assertFalse(GroupFavorite.objects.filter(group_id=gid).exists())
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    # -- invite link create ---------------------------------------------

    def test_group_delete_behind_invite_link_create(self):
        self._assert_writer_then_delete(*self._race(self.INVITE_CREATE_HOLD, self._invite_create, self._delete))

    def test_invite_link_create_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._invite_create))

    # -- emailed invite send --------------------------------------------

    def test_group_delete_behind_invite_send(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._invite_send, self._delete))

    def test_invite_send_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._invite_send))

    # -- invite revoke --------------------------------------------------

    def test_group_delete_behind_invite_revoke(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._invite_revoke, self._delete))

    def test_invite_revoke_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._invite_revoke))

    # -- group labels ---------------------------------------------------

    def test_group_delete_behind_label_create(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._label_create, self._delete))

    def test_label_create_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._label_create))

    def test_group_delete_behind_label_patch(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._label_patch, self._delete))

    def test_label_patch_behind_group_delete(self):
        # Before: the UPDATE matched no row and save() re-inserted the label
        # under the deleted group.
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._label_patch))

    # -- star -----------------------------------------------------------

    def test_group_delete_behind_star(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._star, self._delete))

    def test_star_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._star))

    # -- subgroup create ------------------------------------------------

    def test_group_delete_behind_subgroup_create(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._subgroup_create, self._delete))
        self.assertFalse(Group.objects.filter(name="N1719").exists())

    def test_subgroup_create_behind_group_delete(self):
        deleted, created = self._race(self.DELETE_HOLD, self._delete, self._subgroup_create)
        # The documented unknown-parent answer, not a 404 (the endpoint is
        # the group list, which exists).
        self._assert_delete_then_writer(deleted, created, expected=400)
        self.assertEqual(created.json(), {"parent": ["Select a parent group you administer."]})
        self.assertFalse(Group.objects.filter(name="N1719").exists())

    # -- transfer ownership ---------------------------------------------

    def test_group_delete_behind_transfer_rechecks_the_owner(self):
        transferred, deleted = self._race(self.COMMIT_HOLD, self._transfer, self._delete)
        self.assertEqual(transferred.status_code, 200, transferred.content[:300])
        # The sender no longer owns the group (#1638's owner re-check), but
        # is a site admin, which may delete any group.
        self.assertIn(deleted.status_code, (204, 403), deleted.content[:300])
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    def test_transfer_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._transfer))

    # -- group PATCH (rename) and board defaults -------------------------

    def test_group_delete_behind_rename(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._rename, self._delete))

    def test_rename_behind_group_delete(self):
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._rename))

    def test_group_delete_behind_board_defaults(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._board_defaults, self._delete))

    def test_board_defaults_behind_group_delete(self):
        # Before: save() matched no row and re-inserted the deleted group.
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._board_defaults))

    # -- member role change ---------------------------------------------

    def test_group_delete_behind_member_role_change(self):
        self._assert_writer_then_delete(*self._race(self.COMMIT_HOLD, self._member_role, self._delete))

    def test_member_role_change_behind_group_delete(self):
        # Before: save() re-inserted the membership under the deleted group.
        self._assert_delete_then_writer(*self._race(self.DELETE_HOLD, self._delete, self._member_role))


class BoardImportVsGroupDeleteTests(_GroupDeleteRaceBase):
    """Board imports (JSON, CSV, Trello) into a group vs a delete of that group.

    An import creates its board in the group, so it takes the group
    ``FOR KEY SHARE`` before the board insert like every other board
    placement (#1638).
    """

    # Inside the import's transaction, after the board and its rows.
    WRITE_HOLD = (_bb, "persist_board_event", False)

    def setUp(self):
        super().setUp()
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _client(self):
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=self.sender.pk))
        return client

    def _json_import(self):
        data = {
            "name": "I1719",
            "columns": [{"name": "To Do", "position": 0}],
            "swimlanes": [{"name": "General", "position": 0}],
            "labels": [],
            "cards": [],
        }
        f = io.BytesIO(json.dumps(data).encode("utf-8"))
        f.name = "board.json"
        return self._client().post(
            "/api/v1/boards/import/", {"file": f, "name": "I1719", "group_id": self.group_id}, format="multipart",
        )

    def _csv_import(self):
        f = io.BytesIO(b"Title,Column,Swimlane\nA,To Do,General\n")
        f.name = "board.csv"
        return self._client().post(
            "/api/v1/boards/import/", {"file": f, "name": "I1719", "group_id": self.group_id}, format="multipart",
        )

    def _trello_import(self):
        f = io.BytesIO(TRELLO_FIXTURE.read_bytes())
        f.name = "trello.json"
        return self._client().post(
            "/api/v1/boards/import/trello/?confirm=true",
            {"file": f, "name": "I1719", "group_id": self.group_id}, format="multipart",
        )

    def _assert_imported_then_deleted(self, imported, deleted):
        self.assertEqual(imported.status_code, 201, imported.content[:300])
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self.assertIsNone(Board.objects.get(name="I1719").group_id)
        self.assertFalse(Group.objects.filter(pk=self.group_id).exists())
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    def _assert_deleted_then_refused(self, deleted, imported):
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(imported.status_code, 404, imported.content[:300])
        self.assertFalse(Board.objects.filter(name="I1719").exists())
        self.assertFalse(Group.objects.filter(pk=self.group_id).exists())
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    def test_group_delete_behind_json_import(self):
        self._assert_imported_then_deleted(*self._race(self.WRITE_HOLD, self._json_import, self._delete))

    def test_json_import_behind_group_delete(self):
        self._assert_deleted_then_refused(*self._race(self.DELETE_HOLD, self._delete, self._json_import))

    def test_group_delete_behind_csv_import(self):
        self._assert_imported_then_deleted(*self._race(self.WRITE_HOLD, self._csv_import, self._delete))

    def test_csv_import_behind_group_delete(self):
        self._assert_deleted_then_refused(*self._race(self.DELETE_HOLD, self._delete, self._csv_import))

    def test_group_delete_behind_trello_import(self):
        self._assert_imported_then_deleted(*self._race(self.WRITE_HOLD, self._trello_import, self._delete))

    def test_trello_import_behind_group_delete(self):
        self._assert_deleted_then_refused(*self._race(self.DELETE_HOLD, self._delete, self._trello_import))


class BoardAdminSaveVsGroupDeleteTests(_GroupDeleteRaceBase):
    """The Django board admin moving a board into a group vs a delete of that group's subtree.

    The admin held the board row and asked for the target group only at
    COMMIT (the deferred foreign key): the same deadlock #1638 fixed for
    move-group. It now takes the target group first.
    """

    def setUp(self):
        super().setUp()
        self.sub = Group.objects.create(name="S1719", owner=self.sender, parent=self.group)
        self.board = Board.objects.create(name="B1719", owner=self.sender, group=self.group)
        BoardMembership.objects.create(board=self.board, user=self.sender, role=BoardMembership.Role.ADMIN)
        for target in ("boards.broadcast.broadcast_board_event", "boards.admin._broadcast_group_event"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _admin_move(self):
        from boards.admin import BoardAdmin

        request = RequestFactory().post("/")
        request.user = User.objects.get(pk=self.sender.pk)
        try:
            with transaction.atomic():
                obj = Board.objects.get(pk=self.board.pk)
                obj.group_id = self.sub.pk
                BoardAdmin(Board, dj_admin.site).save_model(request, obj, None, True)
                _checkpoint()
        except Http404:
            return "gone"
        return "saved"

    def _assert_board_ungrouped(self):
        self.assertFalse(Group.objects.filter(pk__in=[self.group_id, self.sub.pk]).exists())
        self.board.refresh_from_db()
        self.assertIsNone(self.board.group_id)
        self.assertEqual([r.getMessage() for r in self.errors.records], [])

    def test_group_delete_behind_admin_move_within_subtree(self):
        saved, deleted = self._race((_this_module(), "_checkpoint", False), self._admin_move, self._delete)
        self.assertEqual(saved, "saved")
        self.assertEqual(deleted.status_code, 204, getattr(deleted, "content", deleted))
        self._assert_board_ungrouped()

    def test_admin_move_behind_group_delete(self):
        deleted, saved = self._race(self.DELETE_HOLD, self._delete, self._admin_move)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(saved, "gone")
        self._assert_board_ungrouped()
