"""Card renders and Django admin callers racing a card delete answer 404, not 500 (#1585).

Follow-up to ``test_card_child_write_vs_delete_race.py`` (#1584). Same
deterministic interleaving, no sleeps: the card is deleted (on the request's own
connection) at the moment the render re-reads it.
"""

from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory
from rest_framework.test import APITestCase

from accounts.models import User
from boards.admin import CardAdmin, CardChecklistAdmin, CardCommentAdmin
from boards.models import (
    Board, BoardEvent, BoardMembership, Card, CardChecklist, CardComment, CardMovement,
    Column, Swimlane,
)
from boards.serializers import _card_queryset as _real_card_queryset


def _deleting(pk, target):
    """Patch ``target`` (a ``_card_queryset`` binding) to delete card ``pk`` first."""
    def racing(qs):
        Card.objects.filter(pk=pk).delete()
        return _real_card_queryset(qs)

    return patch(target, side_effect=racing)


class _Base(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="racer1585", password="pass")
        self.board = Board.objects.create(name="B", owner=self.user)
        BoardMembership.objects.create(
            board=self.board, user=self.user, role=BoardMembership.Role.ADMIN,
        )
        self.col = Column.objects.create(board=self.board, name="A", position=0)
        self.col2 = Column.objects.create(board=self.board, name="B", position=1)
        self.swim = Swimlane.objects.create(board=self.board, name="G", position=0)
        self.card = Card.objects.create(
            board=self.board, column=self.col, swimlane=self.swim, title="C",
            created_by=self.user, position=0,
        )
        self.client.force_authenticate(self.user)
        self.base = f"/api/v1/boards/{self.board.pk}/cards/{self.card.pk}"


class ViewRenderVsDeleteTests(_Base):
    VIEW_QS = "boards.views.cards._card_queryset"

    def test_move_render_returns_404_and_rolls_back(self):
        events_before = BoardEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            with _deleting(self.card.pk, self.VIEW_QS):
                resp = self.client.post(
                    f"{self.base}/move/",
                    {"column_id": self.col2.pk, "swimlane_id": self.swim.pk, "position": 0},
                    format="json",
                )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(BoardEvent.objects.count(), events_before)
        # The move and the simulated delete share the request's savepoint, which
        # the 404 rolls back: the card is back in its original cell, unmoved.
        self.card.refresh_from_db()
        self.assertEqual(self.card.column_id, self.col.pk)
        self.assertEqual(CardMovement.objects.filter(card=self.card).count(), 0)

    def test_move_render_movement_row_gone_returns_404_and_rolls_back(self):
        # Only the CardMovement row vanishes (the card survives), exercising the
        # CardMovement.DoesNotExist branch of the move render.
        events_before = BoardEvent.objects.count()
        real_qs = _real_card_queryset

        def drop_movement(qs):
            CardMovement.objects.filter(card_id=self.card.pk).delete()
            return real_qs(qs)

        with self.captureOnCommitCallbacks(execute=True):
            with patch(self.VIEW_QS, side_effect=drop_movement):
                resp = self.client.post(
                    f"{self.base}/move/",
                    {"column_id": self.col2.pk, "swimlane_id": self.swim.pk, "position": 0},
                    format="json",
                )
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(BoardEvent.objects.count(), events_before)
        self.card.refresh_from_db()
        self.assertEqual(self.card.column_id, self.col.pk)

    def test_unraced_move_still_200(self):
        resp = self.client.post(
            f"{self.base}/move/",
            {"column_id": self.col2.pk, "swimlane_id": self.swim.pk, "position": 0},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

    def test_archive_render_after_commit_returns_404(self):
        # archive_card renders after its atomic block: the archive is already
        # applied when the card vanishes, and 404 is the chosen answer.
        with self.captureOnCommitCallbacks(execute=True):
            with _deleting(self.card.pk, self.VIEW_QS):
                resp = self.client.post(f"{self.base}/archive/")
        self.assertEqual(resp.status_code, 404)

    def test_unarchive_render_in_atomic_returns_404_and_rolls_back(self):
        Card.objects.filter(pk=self.card.pk).update(archived_at="2026-01-01T00:00:00Z")
        events_before = BoardEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            with _deleting(self.card.pk, self.VIEW_QS):
                resp = self.client.post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(BoardEvent.objects.count(), events_before)
        # Restore rolled back with the 404 (the delete rolled back with it).
        self.assertIsNotNone(Card.objects.get(pk=self.card.pk).archived_at)

    def test_unarchive_concurrent_restore_noop_render_returns_404(self):
        # The card is archived at the service's first read, restored by someone
        # else before the lock is granted, so unarchive becomes the in-atomic
        # no-op and its render finds the card deleted.
        from boards.services import cards as card_services

        Card.objects.filter(pk=self.card.pk).update(archived_at="2026-01-01T00:00:00Z")
        real_lock = card_services.lock_card_for_write

        def restore_then_lock(card_id):
            Card.objects.filter(pk=card_id).update(archived_at=None)
            return real_lock(card_id)

        events_before = BoardEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            with patch("boards.services.cards.lock_card_for_write", side_effect=restore_then_lock):
                with _deleting(self.card.pk, self.VIEW_QS):
                    resp = self.client.post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(BoardEvent.objects.count(), events_before)

    def test_unarchive_noop_render_returns_404(self):
        # Not archived: the no-op path renders after nothing was written.
        with _deleting(self.card.pk, self.VIEW_QS):
            resp = self.client.post(f"{self.base}/unarchive/")
        self.assertEqual(resp.status_code, 404)

    def test_unraced_archive_unarchive_still_200(self):
        self.assertEqual(self.client.post(f"{self.base}/archive/").status_code, 200)
        self.assertEqual(self.client.post(f"{self.base}/unarchive/").status_code, 200)


class ArchiveStaysAppliedTests(_Base):
    def test_archived_at_is_set_when_card_vanishes_after_commit(self):
        # Delete the card only on the render's re-read, then restore a row
        # snapshot check: the archive write must already be durable at that
        # point, so capture archived_at as the render starts.
        seen = {}

        def racing(qs):
            seen["archived_at"] = Card.objects.get(pk=self.card.pk).archived_at
            Card.objects.filter(pk=self.card.pk).delete()
            return _real_card_queryset(qs)

        with patch("boards.views.cards._card_queryset", side_effect=racing):
            resp = self.client.post(f"{self.base}/archive/")
        self.assertEqual(resp.status_code, 404)
        self.assertIsNotNone(seen["archived_at"])


class AdminVsDeleteTests(_Base):
    HELPER_QS = "boards.views._helpers._card_queryset"

    def _request(self):
        request = RequestFactory().post("/admin/")
        request.user = self.user
        return request

    def test_card_admin_save_model_skips_broadcast_when_card_gone(self):
        adm = CardAdmin(Card, AdminSite())
        self.card.title = "edited"
        events_before = BoardEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            with _deleting(self.card.pk, self.HELPER_QS):
                adm.save_model(self._request(), self.card, None, change=True)
        self.assertEqual(BoardEvent.objects.count(), events_before)

    def test_child_admin_save_model_skips_broadcast_when_card_gone(self):
        adm = CardCommentAdmin(CardComment, AdminSite())
        comment = CardComment(card=self.card, author=self.user, body="x")
        events_before = BoardEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            with _deleting(self.card.pk, self.HELPER_QS):
                adm.save_model(self._request(), comment, None, change=False)
        self.assertEqual(BoardEvent.objects.count(), events_before)

    def test_child_admin_delete_model_skips_broadcast_when_card_gone(self):
        adm = CardChecklistAdmin(CardChecklist, AdminSite())
        item = CardChecklist.objects.create(
            card=self.card, text="t", created_by=self.user, position=0,
        )
        events_before = BoardEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            with _deleting(self.card.pk, self.HELPER_QS):
                adm.delete_model(self._request(), item)
        self.assertEqual(BoardEvent.objects.count(), events_before)

    def test_delete_queryset_skips_vanished_card_and_keeps_the_rest(self):
        card2 = Card.objects.create(
            board=self.board, column=self.col, swimlane=self.swim, title="C2",
            created_by=self.user, position=1,
        )
        for c in (self.card, card2):
            CardChecklist.objects.create(card=c, text="t", created_by=self.user, position=0)
        adm = CardChecklistAdmin(CardChecklist, AdminSite())
        events_before = BoardEvent.objects.count()

        # card2 vanishes when its refetch runs, i.e. after the first card's
        # refetch succeeded. Refetch-all-then-record means the first card's event
        # is still recorded exactly once and nothing raises.
        real = _real_card_queryset

        calls = {"n": 0}

        def racing_second(qs):
            calls["n"] += 1
            if calls["n"] == 2:
                Card.objects.filter(pk=card2.pk).delete()
            return real(qs)

        with self.captureOnCommitCallbacks(execute=True):
            with patch(self.HELPER_QS, side_effect=racing_second):
                adm.delete_queryset(self._request(), CardChecklist.objects.all())
        self.assertEqual(calls["n"], 2)
        self.assertEqual(BoardEvent.objects.count(), events_before + 1)
