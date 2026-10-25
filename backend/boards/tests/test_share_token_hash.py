"""Public share links are stored and resolved by a hash of the token (#1880).

``Board.share_token_hash`` holds the SHA-256 of the token, the same scheme as
the invite models' ``token_hash``. The public view resolves a link by that
hash only; the plaintext column remains for the admin-only API fields and is
never matched against a presented token. Migration 0088 backfills the hash
for links enabled before the column existed, so those keep working.
"""

import hashlib
import importlib
import uuid

from django.apps import apps
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import Board, BoardMembership

_backfill = importlib.import_module("boards.migrations.0088_backfill_board_share_token_hash")


def _make_board(owner, name="Hash Board"):
    board = Board.objects.create(name=name, owner=owner)
    BoardMembership.objects.create(board=board, user=owner, role=BoardMembership.Role.ADMIN)
    return board


class HashShareTokenTests(TestCase):
    """``Board.hash_share_token`` and the migration's inlined copy."""

    def test_is_sha256_of_the_canonical_uuid(self):
        token = uuid.uuid4()
        self.assertEqual(
            Board.hash_share_token(token), hashlib.sha256(str(token).encode()).hexdigest(),
        )

    def test_every_uuid_spelling_hashes_the_same(self):
        token = uuid.uuid4()
        expected = Board.hash_share_token(token)
        for spelling in (str(token), str(token).upper(), token.hex, f"{{{token}}}", f"urn:uuid:{token}"):
            self.assertEqual(Board.hash_share_token(spelling), expected, spelling)

    def test_non_uuid_raises_value_error(self):
        with self.assertRaises(ValueError):
            Board.hash_share_token("not-a-uuid")

    def test_migration_copy_matches_the_model_helper(self):
        """0088 inlines the helper; the two must agree or backfilled links 404."""
        for _ in range(5):
            token = uuid.uuid4()
            self.assertEqual(_backfill.hash_share_token(token), Board.hash_share_token(token))
            self.assertEqual(_backfill.hash_share_token(str(token)), Board.hash_share_token(token))

    def test_issue_share_token_sets_token_and_hash_together(self):
        board = Board(name="x")
        token = board.issue_share_token()
        self.assertEqual(board.share_token, token)
        self.assertEqual(board.share_token_hash, Board.hash_share_token(token))

    def test_clear_share_link_clears_every_share_column(self):
        board = Board(name="x")
        board.issue_share_token()
        board.clear_share_link()
        for name in Board.SHARE_LINK_CLEARED:
            self.assertIsNone(getattr(board, name), name)
        self.assertEqual(
            set(Board.SHARE_LINK_CLEARED),
            {"share_token", "share_token_hash", "share_token_expires_at", "share_enabled_by"},
        )


class ShareLinkResolvedByHashTests(TestCase):
    """The share action stores the hash; the public view resolves by it alone."""

    def setUp(self):
        self.admin = User.objects.create_user(username="hash_admin", password="pass")
        self.board = _make_board(self.admin)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.anon = APIClient()

    def enable(self):
        r = self.admin_client.post(f"/api/v1/boards/{self.board.pk}/share/")
        self.assertEqual(r.status_code, status.HTTP_200_OK, r.content)
        return r.data["share_token"]

    def public(self, token):
        return self.anon.get(f"/api/share/{token}/")

    def test_enable_stores_the_hash_of_the_returned_token(self):
        token = self.enable()
        self.board.refresh_from_db()
        self.assertEqual(self.board.share_token_hash, Board.hash_share_token(token))
        self.assertNotEqual(self.board.share_token_hash, token)
        self.assertEqual(self.public(token).status_code, status.HTTP_200_OK)

    def test_wrong_token_404s(self):
        self.enable()
        self.assertEqual(self.public(uuid.uuid4()).status_code, status.HTTP_404_NOT_FOUND)

    def test_other_spellings_of_the_token_resolve(self):
        token = uuid.UUID(self.enable())
        for spelling in (str(token).upper(), token.hex):
            self.assertEqual(self.public(spelling).status_code, status.HTTP_200_OK, spelling)

    def test_lookup_filters_on_the_hash_not_the_plaintext(self):
        """The board query matches the presented token by its hash; the
        stored token column is never compared with the presented value."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        token = self.enable()
        token_hash = Board.hash_share_token(token)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.public(token).status_code, status.HTTP_200_OK)
        lookups = [q["sql"] for q in ctx.captured_queries if token_hash in q["sql"]]
        self.assertEqual(len(lookups), 1, lookups)
        self.assertIn('"share_token_hash" =', lookups[0])
        for q in ctx.captured_queries:
            for spelling in (str(token), uuid.UUID(token).hex):
                self.assertNotIn(spelling, q["sql"])

    def test_token_changed_without_its_hash_does_not_resolve(self):
        """A stale pair (token rotated by a writer that left the old hash, as
        a previous-release backend does during a rolling upgrade) fails
        closed: neither the old nor the new token resolves."""
        old = self.enable()
        new = uuid.uuid4()
        Board.objects.filter(pk=self.board.pk).update(share_token=new)
        self.assertEqual(self.public(old).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.public(new).status_code, status.HTTP_404_NOT_FOUND)

    def test_link_cleared_then_reenabled_without_hash_keeps_old_token_dead(self):
        """Clear and re-enable by a writer that never touches the hash: the
        revoked token does not come back."""
        old = self.enable()
        Board.objects.filter(pk=self.board.pk).update(share_token=None)
        self.assertEqual(self.public(old).status_code, status.HTTP_404_NOT_FOUND)
        Board.objects.filter(pk=self.board.pk).update(share_token=uuid.uuid4())
        self.assertEqual(self.public(old).status_code, status.HTTP_404_NOT_FOUND)

    def test_plaintext_without_a_hash_does_not_resolve(self):
        token = self.enable()
        Board.objects.filter(pk=self.board.pk).update(share_token_hash=None)
        self.assertEqual(self.public(token).status_code, status.HTTP_404_NOT_FOUND)

    def test_hash_without_a_live_token_does_not_resolve(self):
        """Defense in depth: a hash left behind by a clear does not keep a link alive."""
        token = self.enable()
        Board.objects.filter(pk=self.board.pk).update(share_token=None)
        self.assertEqual(self.public(token).status_code, status.HTTP_404_NOT_FOUND)

    def test_regenerate_replaces_the_hash(self):
        old = self.enable()
        self.board.refresh_from_db()
        old_hash = self.board.share_token_hash
        new = self.enable()
        self.board.refresh_from_db()
        self.assertNotEqual(self.board.share_token_hash, old_hash)
        self.assertEqual(self.board.share_token_hash, Board.hash_share_token(new))
        self.assertEqual(self.public(old).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.public(new).status_code, status.HTTP_200_OK)

    def test_disable_clears_the_hash(self):
        token = self.enable()
        r = self.admin_client.delete(f"/api/v1/boards/{self.board.pk}/share/")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.board.refresh_from_db()
        self.assertIsNone(self.board.share_token_hash)
        self.assertEqual(self.public(token).status_code, status.HTTP_404_NOT_FOUND)

    def test_hash_is_in_no_response(self):
        token = self.enable()
        responses = [
            self.admin_client.post(f"/api/v1/boards/{self.board.pk}/share/").data,
            self.admin_client.get(f"/api/v1/boards/{self.board.pk}/").data,
            self.admin_client.get(f"/api/v1/boards/{self.board.pk}/full/").data,
        ]
        self.board.refresh_from_db()
        responses.append(self.public(self.board.share_token).data)
        for data in responses:
            self.assertNotIn("share_token_hash", data)
            self.assertNotIn(self.board.share_token_hash, str(data))
        self.assertNotIn(Board.hash_share_token(token), str(responses[0]))

    def test_hash_is_unique(self):
        token = self.enable()
        other = _make_board(self.admin, name="Other")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Board.objects.filter(pk=other.pk).update(
                share_token=uuid.uuid4(), share_token_hash=Board.hash_share_token(token),
            )


class NoSerializerExposesTheHashTests(TestCase):

    def test_hash_is_an_admin_only_payload_field(self):
        from boards.broadcast import ADMIN_ONLY_BOARD_FIELDS, without_admin_only_board_fields

        self.assertIn("share_token_hash", ADMIN_ONLY_BOARD_FIELDS)
        self.assertNotIn(
            "share_token_hash", without_admin_only_board_fields({"id": 1, "share_token_hash": "x"}),
        )

    def test_no_board_serializer_declares_the_hash(self):
        from rest_framework import serializers as drf_serializers

        import boards.serializers as board_serializers

        for name in dir(board_serializers):
            cls = getattr(board_serializers, name)
            if not (isinstance(cls, type) and issubclass(cls, drf_serializers.ModelSerializer)):
                continue
            meta = getattr(cls, "Meta", None)
            if getattr(meta, "model", None) is not Board:
                continue
            fields = getattr(meta, "fields", ())
            self.assertNotEqual(fields, "__all__", name)
            self.assertNotIn("share_token_hash", fields, name)


class BackfillMigrationTests(TestCase):
    """0088 hashes links enabled before the column existed, so they keep working."""

    def setUp(self):
        self.owner = User.objects.create_user(username="hash_backfill", password="pass")
        self.anon = APIClient()

    def _legacy_link(self, name):
        """A board as an earlier release left it: a token and no hash."""
        board = _make_board(self.owner, name=name)
        token = uuid.uuid4()
        Board.objects.filter(pk=board.pk).update(share_token=token, share_token_hash=None)
        return board, token

    def test_pre_existing_link_resolves_after_backfill(self):
        board, token = self._legacy_link("Legacy")
        self.assertEqual(self.anon.get(f"/api/share/{token}/").status_code, status.HTTP_404_NOT_FOUND)

        _backfill.backfill_share_token_hash(apps, None)

        board.refresh_from_db()
        self.assertEqual(board.share_token_hash, Board.hash_share_token(token))
        self.assertEqual(board.share_token, token)
        self.assertEqual(self.anon.get(f"/api/share/{token}/").status_code, status.HTTP_200_OK)

    def test_boards_without_a_link_stay_null_and_rerun_is_idempotent(self):
        legacy, token = self._legacy_link("Legacy")
        unshared = _make_board(self.owner, name="Unshared")
        current = _make_board(self.owner, name="Current")
        current.issue_share_token()
        current.save(update_fields=["share_token", "share_token_hash"])
        current_hash = current.share_token_hash

        _backfill.backfill_share_token_hash(apps, None)
        _backfill.backfill_share_token_hash(apps, None)

        unshared.refresh_from_db()
        current.refresh_from_db()
        legacy.refresh_from_db()
        self.assertIsNone(unshared.share_token_hash)
        self.assertEqual(current.share_token_hash, current_hash)
        self.assertEqual(legacy.share_token_hash, Board.hash_share_token(token))
