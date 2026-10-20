"""CardMovement history is append-only and survives a card hard delete (#1469).

- Hard-deleting a card keeps its movements: ``card`` becomes NULL and the
  row keeps ``board``, ``card_uid`` and ``card_title``.
- The board-level History still lists them, under the original card uid and
  title; live cards serialize exactly as before (current uid and title).
- Deleting the whole board removes every movement, including those of cards
  deleted earlier.
- Metrics that counted only live cards are unchanged by retained rows.
- The model refuses to update or delete a movement row; the Django admin is
  view-only for movements, and a board can still be deleted from the admin.
- Migration 0080's backfill fills existing rows, in batches, idempotently.
"""
import importlib
import io
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.core.cache import cache
from django.contrib.contenttypes.models import ContentType
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import connection
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import (
    AppendOnlyViolation, Board, BoardMembership, Card, CardMovement, Column, Swimlane,
)
from boards.tests.test_import_history_attribution import URL as IMPORT_URL, _file, _payload

PW = "pw123456!x"

_PLAIN_STATIC = override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)

_migration_0080 = importlib.import_module(
    "boards.migrations.0080_backfill_cardmovement_board_snapshots"
)


class _BoardFixture(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password=PW)
        self.board = Board.objects.create(name="Retention", owner=self.owner)
        BoardMembership.objects.create(board=self.board, user=self.owner, role="admin")
        self.col_todo = Column.objects.create(board=self.board, name="To Do", position=0)
        self.col_done = Column.objects.create(
            board=self.board, name="Done", position=1, is_done=True,
        )
        self.swim = Swimlane.objects.create(board=self.board, name="General", position=0)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def _card(self, title="Card"):
        return Card.objects.create(
            board=self.board, column=self.col_todo, swimlane=self.swim,
            title=title, created_by=self.owner, position=0,
        )

    def _move(self, card, column):
        resp = self.client.post(
            f"/api/v1/boards/{self.board.pk}/cards/{card.pk}/move/",
            {"column_id": column.pk, "swimlane_id": self.swim.pk, "position": 0},
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp

    def _delete_card(self, card):
        resp = self.client.delete(f"/api/v1/boards/{self.board.pk}/cards/{card.pk}/")
        self.assertEqual(resp.status_code, 204, resp.content)

    def _history(self, **params):
        resp = self.client.get(f"/api/v1/boards/{self.board.pk}/movements/", params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.data


class WriteTimeSnapshotTests(_BoardFixture):
    def test_create_fills_board_and_card_snapshot(self):
        card = self._card("Write me")
        mv = CardMovement.objects.create(card=card, to_column=self.col_done, moved_by=self.owner)
        mv.refresh_from_db()
        self.assertEqual(
            (mv.board_id, mv.card_uid, mv.card_title), (self.board.pk, card.uid, "Write me"),
        )

    def test_move_endpoint_row_carries_snapshot_and_response_is_unchanged(self):
        card = self._card("Moved")
        resp = self._move(card, self.col_done)
        mv = CardMovement.objects.get(card=card)
        self.assertEqual((mv.board_id, mv.card_uid, mv.card_title), (self.board.pk, card.uid, "Moved"))
        self.assertEqual(resp.data["movement"]["card_uid"], card.uid)
        self.assertEqual(resp.data["movement"]["card_title"], "Moved")
        self.assertNotIn("card_id", resp.data["movement"])
        self.assertNotIn("board", resp.data["movement"])

    def test_import_bulk_create_sets_board_and_snapshot(self):
        cache.clear()
        resp = self.client.post(
            IMPORT_URL, {"file": _file(_payload())}, format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        board = Board.objects.get(pk=resp.data["id"])
        card = Card.objects.get(board=board)
        mv = CardMovement.objects.get(card=card)
        self.assertEqual((mv.board_id, mv.card_uid, mv.card_title), (board.pk, card.uid, "Card"))


class HardDeleteKeepsHistoryTests(_BoardFixture):
    def test_card_delete_keeps_movements_with_snapshot(self):
        card = self._card("Doomed")
        self._move(card, self.col_done)
        uid = card.uid
        self._delete_card(card)
        self.assertFalse(Card.objects.filter(uid=uid).exists())
        kept = CardMovement.objects.get(card_uid=uid)
        self.assertEqual(
            (kept.card_id, kept.board_id, kept.card_title, kept.to_column_name),
            (None, self.board.pk, "Doomed", "Done"),
        )

    def test_board_history_lists_deleted_cards_movements_under_the_original_card(self):
        card = self._card("Doomed")
        self._move(card, self.col_done)
        uid = card.uid
        before = self._history()["results"]
        self._delete_card(card)
        after = self._history()
        self.assertEqual(after["count"], 1)
        # Byte-identical: same row, same keys, same values as while the card lived.
        self.assertEqual(after["results"], before)
        self.assertEqual(after["results"][0]["card_uid"], uid)
        self.assertEqual(after["results"][0]["card_title"], "Doomed")

    def test_live_card_serializes_its_current_title_not_the_snapshot(self):
        card = self._card("Old name")
        self._move(card, self.col_done)
        Card.objects.filter(pk=card.pk).update(title="New name")
        row = self._history()["results"][0]
        self.assertEqual((row["card_uid"], row["card_title"]), (card.uid, "New name"))
        self.assertEqual(CardMovement.objects.get(card=card).card_title, "Old name")

    def test_swimlane_filter_excludes_deleted_cards_rows(self):
        # The filter is "the card's current swimlane"; a deleted card has none.
        card = self._card()
        self._move(card, self.col_done)
        self._delete_card(card)
        self.assertEqual(self._history(swimlane_id=self.swim.pk)["count"], 0)
        self.assertEqual(self._history()["count"], 1)

    def test_other_boards_history_never_shows_the_rows(self):
        card = self._card()
        self._move(card, self.col_done)
        self._delete_card(card)
        other = Board.objects.create(name="Other", owner=self.owner)
        BoardMembership.objects.create(board=other, user=self.owner, role="admin")
        resp = self.client.get(f"/api/v1/boards/{other.pk}/movements/")
        self.assertEqual(resp.data["count"], 0)

    def test_non_member_cannot_read_retained_history(self):
        card = self._card()
        self._move(card, self.col_done)
        self._delete_card(card)
        stranger = User.objects.create_user(username="stranger", password=PW)
        c = APIClient()
        c.force_authenticate(stranger)
        resp = c.get(f"/api/v1/boards/{self.board.pk}/movements/")
        self.assertIn(resp.status_code, (403, 404))

    def test_export_hook_receives_live_card_rows_only(self):
        live = self._card("Live")
        gone = self._card("Gone")
        self._move(live, self.col_done)
        self._move(gone, self.col_done)
        self._delete_card(gone)
        backend = MagicMock(return_value=HttpResponse("ok"))
        with patch("boards.hooks.MOVEMENT_EXPORT_BACKENDS", [backend]):
            resp = self.client.get(f"/api/v1/boards/{self.board.pk}/movements/?export=csv")
        self.assertEqual(resp.status_code, 200)
        handed = list(backend.call_args.args[1])
        self.assertEqual([m.card_id for m in handed], [live.pk])


class BoardDeleteCascadeTests(_BoardFixture):
    def test_board_delete_removes_live_and_retained_history(self):
        kept_card, live_card = self._card("Deleted first"), self._card("Live")
        self._move(kept_card, self.col_done)
        self._move(live_card, self.col_done)
        self._delete_card(kept_card)
        self.assertEqual(CardMovement.objects.filter(board=self.board).count(), 2)
        board_pk = self.board.pk
        resp = self.client.delete(f"/api/v1/boards/{board_pk}/")
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertEqual(CardMovement.objects.filter(board_id=board_pk).count(), 0)
        self.assertEqual(CardMovement.objects.count(), 0)

    def test_board_delete_leaves_other_boards_history_alone(self):
        other = Board.objects.create(name="Other", owner=self.owner)
        col = Column.objects.create(board=other, name="A", position=0)
        swim = Swimlane.objects.create(board=other, name="S", position=0)
        other_card = Card.objects.create(
            board=other, column=col, swimlane=swim, title="x", created_by=self.owner, position=0,
        )
        CardMovement.objects.create(card=other_card, to_column=col)
        self.client.delete(f"/api/v1/boards/{self.board.pk}/")
        self.assertEqual(CardMovement.objects.filter(board=other).count(), 1)


class MetricsIgnoreRetainedHistoryTests(_BoardFixture):
    """Summary and analytics counted only live cards before #1469; a deleted
    card's retained movements must not change them."""

    def test_summary_and_analytics_unchanged_by_a_deleted_cards_history(self):
        live = self._card("Live")
        self._move(live, self.col_done)
        summary_before = self.client.get(f"/api/v1/boards/{self.board.pk}/summary/").data
        analytics_before = self.client.get(f"/api/v1/boards/{self.board.pk}/analytics/").data

        gone = self._card("Gone")
        self._move(gone, self.col_done)
        self._delete_card(gone)
        self.assertTrue(CardMovement.objects.filter(card__isnull=True, board=self.board).exists())

        self.assertEqual(
            self.client.get(f"/api/v1/boards/{self.board.pk}/summary/").data, summary_before,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/boards/{self.board.pk}/analytics/").data, analytics_before,
        )


class AppendOnlyModelGuardTests(_BoardFixture):
    def test_saving_an_existing_movement_is_refused(self):
        mv = CardMovement.objects.create(card=self._card(), to_column=self.col_done)
        mv.notes = "rewritten"
        with self.assertRaises(AppendOnlyViolation):
            mv.save()
        mv.refresh_from_db()
        self.assertEqual(mv.notes, "")

    def test_saving_with_a_preset_pk_is_refused(self):
        card = self._card()
        existing = CardMovement.objects.create(card=card, to_column=self.col_done)
        with self.assertRaises(AppendOnlyViolation):
            CardMovement(pk=existing.pk, card=card, notes="overwrite").save()
        existing.refresh_from_db()
        self.assertEqual(existing.notes, "")

    def test_deleting_a_movement_instance_is_refused(self):
        mv = CardMovement.objects.create(card=self._card(), to_column=self.col_done)
        with self.assertRaises(AppendOnlyViolation):
            mv.delete()
        self.assertTrue(CardMovement.objects.filter(pk=mv.pk).exists())


@_PLAIN_STATIC
class CardMovementAdminTests(_BoardFixture):
    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser(
            username="root1469", email="root1469@example.com", password=PW,
        )
        self.admin_client = Client()
        self.admin_client.force_login(self.root)
        self.mv = CardMovement.objects.create(card=self._card("Seen"), to_column=self.col_done)

    def test_admin_grants_view_only(self):
        from django.contrib import admin as django_admin

        model_admin = django_admin.site.get_model_admin(CardMovement)
        request = MagicMock(user=self.root)
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.mv))
        self.assertFalse(model_admin.has_delete_permission(request, self.mv))
        self.assertTrue(model_admin.has_view_permission(request, self.mv))
        self.assertNotIn("delete_selected", model_admin.get_actions(MagicMock(user=self.root, GET={})))

    def test_changelist_and_detail_render_read_only(self):
        changelist = self.admin_client.get(reverse("admin:boards_cardmovement_changelist"))
        self.assertEqual(changelist.status_code, 200)
        self.assertContains(changelist, "Seen")
        detail = self.admin_client.get(reverse("admin:boards_cardmovement_change", args=[self.mv.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'name="_save"')

    def test_add_change_and_delete_requests_are_refused(self):
        self.assertEqual(
            self.admin_client.get(reverse("admin:boards_cardmovement_add")).status_code, 403,
        )
        change_url = reverse("admin:boards_cardmovement_change", args=[self.mv.pk])
        self.admin_client.post(change_url, {"notes": "rewritten"})
        delete_url = reverse("admin:boards_cardmovement_delete", args=[self.mv.pk])
        self.assertEqual(self.admin_client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.mv.refresh_from_db()
        self.assertEqual(self.mv.notes, "")

    def test_staff_with_view_permission_cannot_change(self):
        staff = User.objects.create_user(username="staff1469", password=PW, is_staff=True)
        ct = ContentType.objects.get_for_model(CardMovement)
        staff.user_permissions.add(*Permission.objects.filter(content_type=ct))
        c = Client()
        c.force_login(staff)
        self.assertEqual(
            c.get(reverse("admin:boards_cardmovement_changelist")).status_code, 200,
        )
        delete_url = reverse("admin:boards_cardmovement_delete", args=[self.mv.pk])
        self.assertEqual(c.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertTrue(CardMovement.objects.filter(pk=self.mv.pk).exists())

    def test_admin_board_delete_still_cascades_movements(self):
        url = reverse("admin:boards_board_delete", args=[self.board.pk])
        confirm = self.admin_client.get(url)
        self.assertEqual(confirm.status_code, 200)
        self.assertFalse(confirm.context["perms_lacking"])
        resp = self.admin_client.post(url, {"post": "yes"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())
        self.assertEqual(CardMovement.objects.count(), 0)

    def test_admin_bulk_board_delete_still_cascades_movements(self):
        resp = self.admin_client.post(
            reverse("admin:boards_board_changelist"),
            {"action": "delete_selected", "_selected_action": [self.board.pk], "post": "yes"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Board.objects.filter(pk=self.board.pk).exists())
        self.assertEqual(CardMovement.objects.count(), 0)


@_PLAIN_STATIC
class UserAdminDeleteWithHistoryTests(_BoardFixture):
    """User deletion in the admin is unaffected by the read-only movement admin.

    A user's own movements are only SET_NULL (``moved_by``), never collected
    for deletion; a board owner is refused before the confirmation page by
    the owned-boards guard (#1616), exactly as before #1469.
    """

    def setUp(self):
        super().setUp()
        self.root = User.objects.create_superuser(
            username="root1469u", email="root1469u@example.com", password=PW,
        )
        self.admin_client = Client()
        self.admin_client.force_login(self.root)

    def test_deleting_a_user_who_moved_cards_keeps_the_movements(self):
        mover = User.objects.create_user(username="mover1469", password=PW)
        mv = CardMovement.objects.create(card=self._card(), to_column=self.col_done, moved_by=mover)
        url = reverse("admin:accounts_user_delete", args=[mover.pk])
        confirm = self.admin_client.get(url)
        self.assertEqual(confirm.status_code, 200)
        self.assertFalse(confirm.context["perms_lacking"])
        self.assertEqual(self.admin_client.post(url, {"post": "yes"}).status_code, 302)
        self.assertFalse(User.objects.filter(pk=mover.pk).exists())
        mv.refresh_from_db()
        self.assertIsNone(mv.moved_by_id)

    def test_board_owner_is_still_refused_by_the_owner_guard(self):
        CardMovement.objects.create(card=self._card(), to_column=self.col_done)
        url = reverse("admin:accounts_user_delete", args=[self.owner.pk])
        resp = self.admin_client.post(url, {"post": "yes"}, follow=True)
        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())
        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("board", text.lower())
        self.assertNotIn("card movement", text.lower())


class AdminCardMoveBetweenBoardsTests(_BoardFixture):
    """The card admin can move a card to another board; its history follows."""

    def test_history_follows_the_card_to_its_new_board(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from boards.admin import CardAdmin
        from boards.tests.test_admin_broadcast import _DummyForm

        card = self._card("Travels")
        self._move(card, self.col_done)
        b2 = Board.objects.create(name="B2", owner=self.owner)
        BoardMembership.objects.create(board=b2, user=self.owner, role="admin")
        col2 = Column.objects.create(board=b2, name="X", position=0)
        swim2 = Swimlane.objects.create(board=b2, name="Y", position=0)
        root = User.objects.create_superuser(
            username="root1469m", email="root1469m@example.com", password=PW,
        )
        request = RequestFactory().post("/admin/")
        request.user = root
        card.board, card.column, card.swimlane = b2, col2, swim2
        with self.captureOnCommitCallbacks(execute=True):
            CardAdmin(Card, AdminSite()).save_model(request, card, _DummyForm(), change=True)

        self.assertEqual(
            list(CardMovement.objects.filter(card=card).values_list("board_id", flat=True)), [b2.pk],
        )
        b2_rows = self.client.get(f"/api/v1/boards/{b2.pk}/movements/").data
        self.assertEqual([r["card_uid"] for r in b2_rows["results"]], [card.uid])
        self.assertEqual(self._history()["count"], 0)
        # Deleting the old board leaves the live card's history alone.
        self.assertEqual(self.client.delete(f"/api/v1/boards/{self.board.pk}/").status_code, 204)
        self.assertEqual(CardMovement.objects.filter(card=card, board=b2).count(), 1)


class RollingUpgradeRowInHistoryTests(_BoardFixture):
    """A row an old pod wrote after 0080 (no board, empty snapshot) is not lost."""

    def _late_row(self, card):
        mv = CardMovement.objects.create(card=card, to_column=self.col_done)
        CardMovement.objects.filter(pk=mv.pk).update(board=None, card_uid="", card_title="")
        return mv

    def test_history_lists_the_row_before_any_backfill_runs(self):
        card = self._card("Late")
        mv = self._late_row(card)
        data = self._history()
        self.assertEqual([r["id"] for r in data["results"]], [mv.pk])
        self.assertEqual(data["results"][0]["card_uid"], card.uid)
        self.assertEqual(data["results"][0]["card_title"], "Late")

    def test_history_does_not_list_another_boards_unbackfilled_row(self):
        other = Board.objects.create(name="Other", owner=self.owner)
        BoardMembership.objects.create(board=other, user=self.owner, role="admin")
        col = Column.objects.create(board=other, name="C", position=0)
        swim = Swimlane.objects.create(board=other, name="S", position=0)
        foreign = Card.objects.create(
            board=other, column=col, swimlane=swim, title="Foreign",
            created_by=self.owner, position=0,
        )
        self._late_row(foreign)
        self.assertEqual(self._history()["count"], 0)

    def test_admin_save_fills_the_snapshot_of_such_a_row(self):
        from django.contrib.admin.sites import AdminSite
        from django.test import RequestFactory

        from boards.admin import CardAdmin
        from boards.tests.test_admin_broadcast import _DummyForm

        card = self._card("Repointed")
        mv = self._late_row(card)
        root = User.objects.create_superuser(
            username="root1469r", email="root1469r@example.com", password=PW,
        )
        request = RequestFactory().post("/admin/")
        request.user = root
        with self.captureOnCommitCallbacks(execute=True):
            CardAdmin(Card, AdminSite()).save_model(request, card, _DummyForm(), change=True)
        mv.refresh_from_db()
        self.assertEqual((mv.board_id, mv.card_uid, mv.card_title), (self.board.pk, card.uid, "Repointed"))
        self._delete_card(card)
        self.assertEqual(self._history()["results"][0]["card_title"], "Repointed")


class DropNotNullKeepingForeignKeyTests(TestCase):
    """0079's operation must stay a bare DROP NOT NULL on PostgreSQL."""

    def _op(self):
        migration = importlib.import_module("boards.migrations.0079_cardmovement_card_nullable")
        return migration.Migration.operations[0]

    def _states(self):
        from django.db.migrations.state import ProjectState

        state = ProjectState.from_apps(apps)
        return state, state

    def test_postgres_forwards_is_only_drop_not_null(self):
        schema_editor = MagicMock()
        schema_editor.connection.vendor = "postgresql"
        schema_editor.quote_name = lambda n: f'"{n}"'
        state, _ = self._states()
        self._op().database_forwards("boards", schema_editor, state, state)
        schema_editor.execute.assert_called_once_with(
            'ALTER TABLE "card_movements" ALTER COLUMN "card_id" DROP NOT NULL'
        )

    def test_backwards_refuses_while_history_of_deleted_cards_exists(self):
        owner = User.objects.create_user(username="o0079", password=PW)
        board = Board.objects.create(name="B", owner=owner)
        col = Column.objects.create(board=board, name="C", position=0)
        CardMovement.objects.create(board=board, card=None, card_uid="u", to_column=col)
        schema_editor = MagicMock()
        schema_editor.connection = connection
        schema_editor.quote_name = connection.ops.quote_name
        state, _ = self._states()
        with self.assertRaisesMessage(RuntimeError, "Cannot reverse boards.0079"):
            self._op().database_backwards("boards", schema_editor, state, state)


class RollingUpgradeRowOnCardDeleteTests(_BoardFixture):
    """A row an older release wrote (no board, no snapshot) whose card is then
    hard-deleted is filled from the card, not left unreachable."""

    def test_card_delete_fills_board_and_snapshot_on_rows_missing_them(self):
        card = self._card("Mid-upgrade")
        self._move(card, self.col_done)
        CardMovement.objects.filter(card=card).update(board=None, card_uid="", card_title="")
        uid = card.uid
        self._delete_card(card)
        row = CardMovement.objects.get()
        self.assertEqual(
            (row.card_id, row.board_id, row.card_uid, row.card_title),
            (None, self.board.pk, uid, "Mid-upgrade"),
        )
        self.assertEqual(
            [(r["card_uid"], r["card_title"]) for r in self._history()["results"]],
            [(uid, "Mid-upgrade")],
        )
        # ...and now leaves with its board.
        self.client.delete(f"/api/v1/boards/{self.board.pk}/")
        self.assertEqual(CardMovement.objects.count(), 0)

    def test_board_delete_removes_rows_missing_board(self):
        card = self._card()
        self._move(card, self.col_done)
        CardMovement.objects.filter(card=card).update(board=None, card_uid="", card_title="")
        self.client.delete(f"/api/v1/boards/{self.board.pk}/")
        self.assertEqual(CardMovement.objects.count(), 0)

    def test_column_delete_fills_rows_missing_board(self):
        card = self._card("In column")
        self._move(card, self.col_done)
        CardMovement.objects.filter(card=card).update(board=None, card_uid="", card_title="")
        self.col_done.delete()
        self.assertEqual(
            CardMovement.objects.values_list("card_id", "board_id", "card_title").get(),
            (None, self.board.pk, "In column"),
        )

    def test_admin_delete_confirmation_page_writes_nothing(self):
        from django.contrib.admin.utils import NestedObjects

        card = self._card()
        self._move(card, self.col_done)
        CardMovement.objects.filter(card=card).update(board=None, card_uid="", card_title="")
        NestedObjects(using="default").collect([card])
        self.assertTrue(CardMovement.objects.filter(board__isnull=True).exists())


class BackfillTests(_BoardFixture):
    """Migration 0080's function, run against the live registry (same models)."""

    def _strip(self, *movements):
        CardMovement.objects.filter(pk__in=[m.pk for m in movements]).update(
            board=None, card_uid="", card_title="",
        )

    def test_backfill_fills_rows_missing_board_and_snapshot(self):
        a, b = self._card("A"), self._card("B")
        mv_a = CardMovement.objects.create(card=a, to_column=self.col_done)
        mv_b = CardMovement.objects.create(card=b, to_column=self.col_done)
        self._strip(mv_a, mv_b)
        _migration_0080.backfill(apps, None)
        self.assertEqual(
            set(CardMovement.objects.values_list("pk", "board_id", "card_uid", "card_title")),
            {(mv_a.pk, self.board.pk, a.uid, "A"), (mv_b.pk, self.board.pk, b.uid, "B")},
        )

    def test_backfill_walks_batches_and_is_idempotent(self):
        cards = [self._card(f"C{i}") for i in range(7)]
        mvs = [CardMovement.objects.create(card=c, to_column=self.col_done) for c in cards]
        self._strip(*mvs)
        with patch.object(_migration_0080, "BATCH_SIZE", 3):
            _migration_0080.backfill(apps, None)
            self.assertFalse(CardMovement.objects.filter(board__isnull=True).exists())
            _migration_0080.backfill(apps, None)
        self.assertEqual(
            sorted(CardMovement.objects.values_list("card_title", flat=True)),
            sorted(f"C{i}" for i in range(7)),
        )

    def test_backfill_leaves_filled_and_deleted_card_rows_alone(self):
        card = self._card("Kept")
        self._move(card, self.col_done)
        self._delete_card(card)
        filled = CardMovement.objects.get()
        _migration_0080.backfill(apps, None)
        self.assertEqual(
            CardMovement.objects.filter(pk=filled.pk).values_list("card_id", "board_id", "card_title").get(),
            (None, self.board.pk, "Kept"),
        )

    def test_management_command_heals_rows_written_after_the_migration(self):
        mv = CardMovement.objects.create(card=self._card("Late"), to_column=self.col_done)
        self._strip(mv)
        out = io.StringIO()
        call_command("backfill_card_movement_snapshots", stdout=out)
        self.assertIn("Filled 1 movement row(s).", out.getvalue())
        mv.refresh_from_db()
        self.assertEqual((mv.board_id, mv.card_title), (self.board.pk, "Late"))
