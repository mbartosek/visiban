"""Deleting a user revokes the site and board invite links they created (#1578)."""

from django.test import TestCase

from accounts.models import InviteLink, User
from boards.models import Board, BoardInviteLink


class DeletedCreatorInviteRevocationTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            username="creator1578", password="pw", is_site_admin=True,
        )
        self.other = User.objects.create_user(
            username="other1578", password="pw", is_site_admin=True,
        )
        self.board = Board.objects.create(name="B", owner=self.other)

    def _make_links(self, user):
        site, _ = InviteLink.generate(created_by=user)
        board, _ = BoardInviteLink.generate(self.board, user)
        return site, board

    def _assert_revoked(self, site, board):
        site.refresh_from_db()
        board.refresh_from_db()
        self.assertIsNone(site.created_by)
        self.assertIsNotNone(site.revoked_at)
        self.assertIsNone(board.created_by)
        self.assertIsNotNone(board.revoked_at)

    def test_instance_delete_revokes_links(self):
        site, board = self._make_links(self.creator)
        self.creator.delete()
        self._assert_revoked(site, board)

    def test_queryset_delete_revokes_links(self):
        site, board = self._make_links(self.creator)
        User.objects.filter(pk=self.creator.pk).delete()
        self._assert_revoked(site, board)

    def test_other_creators_links_untouched(self):
        site, board = self._make_links(self.other)
        self.creator.delete()
        site.refresh_from_db()
        board.refresh_from_db()
        self.assertIsNone(site.revoked_at)
        self.assertIsNone(board.revoked_at)

    def test_consumed_links_keep_history(self):
        from django.utils import timezone

        site, board = self._make_links(self.creator)
        used = timezone.now()
        InviteLink.objects.filter(pk=site.pk).update(used_at=used)
        BoardInviteLink.objects.filter(pk=board.pk).update(single_use=True, used_at=used)
        self.creator.delete()
        site.refresh_from_db()
        board.refresh_from_db()
        self.assertIsNone(site.revoked_at)
        self.assertIsNone(board.revoked_at)

    def test_board_revoke_event_is_recorded(self):
        from boards.broadcast import EVT_INVITE_LINK_REVOKED
        from boards.models import BoardEvent

        _, board_link = self._make_links(self.creator)
        with self.captureOnCommitCallbacks(execute=True):
            self.creator.delete()
        board_link.refresh_from_db()
        self.assertIsNotNone(board_link.revoked_at)
        event = BoardEvent.objects.get(board_id=self.board.pk, event=EVT_INVITE_LINK_REVOKED)
        self.assertEqual(event.data, {"id": board_link.pk})

    def test_deleting_board_owner_with_pending_link(self):
        owner = User.objects.create_user(username="owner1578", password="pw")
        board = Board.objects.create(name="Owned", owner=owner)
        link, _ = BoardInviteLink.generate(board, owner)
        owner.delete()
        self.assertFalse(BoardInviteLink.objects.filter(pk=link.pk).exists())

    def test_deleting_user_without_links(self):
        user = User.objects.create_user(username="nolinks1578", password="pw")
        user.delete()
        self.assertFalse(User.objects.filter(pk=user.pk).exists())
