"""Attachment files are removed from storage when their rows are deleted (#1817).

A ``post_delete`` receiver on ``CardAttachment`` removes the file after commit,
so every path that cascades attachment rows (card, column, swimlane and board
deletes, ``QuerySet.delete()``, Django admin ``delete_selected``) is covered, and
``purge_orphaned_attachments`` repairs files the receiver could not remove.
Each test points ``MEDIA_ROOT`` at a throwaway directory so no real media is
touched.
"""

import datetime
import os
import tempfile
from io import StringIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase, override_settings

from accounts.models import User
from boards.models import Board, Card, CardAttachment, Column, Swimlane


class _MediaTestCase(TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = override_settings(MEDIA_ROOT=tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_user(username="att1817", password="pw-1817-x!")
        self.board = Board.objects.create(name="B", owner=self.user)
        self.column = Column.objects.create(board=self.board, name="A", position=0)
        self.swimlane = Swimlane.objects.create(board=self.board, name="G", position=0)
        self.card = self._card()

    def _card(self):
        return Card.objects.create(
            board=self.board, column=self.column, swimlane=self.swimlane,
            title="C", created_by=self.user, position=0,
        )

    def _attach(self, card=None, name="attachments/2026/10/f.txt", row=True):
        saved = default_storage.save(name, ContentFile(b"x"))
        if not row:
            return saved
        CardAttachment.objects.create(
            card=card or self.card, file=saved, filename="f.txt", size=1, uploaded_by=self.user,
        )
        return saved

    def _age(self, name, hours):
        path = default_storage.path(name)
        ts = (datetime.datetime.now() - datetime.timedelta(hours=hours)).timestamp()
        os.utime(path, (ts, ts))


class AttachmentFileRemovalTests(_MediaTestCase):
    def _delete_removes_file(self, delete):
        name = self._attach()
        with self.captureOnCommitCallbacks(execute=True):
            delete()
        self.assertFalse(default_storage.exists(name))
        self.assertEqual(CardAttachment.objects.count(), 0)

    def test_card_delete(self):
        self._delete_removes_file(self.card.delete)

    def test_column_delete(self):
        self._delete_removes_file(self.column.delete)

    def test_swimlane_delete(self):
        self._delete_removes_file(self.swimlane.delete)

    def test_board_delete(self):
        self._delete_removes_file(self.board.delete)

    def test_queryset_delete(self):
        self._delete_removes_file(CardAttachment.objects.all().delete)

    def test_file_survives_until_commit(self):
        name = self._attach()
        with self.captureOnCommitCallbacks(execute=False):
            self.card.delete()
            self.assertTrue(default_storage.exists(name))

    def test_file_survives_rollback(self):
        name = self._attach()
        try:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    self.card.delete()
                    raise RuntimeError("roll back")
        except RuntimeError:
            pass
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(CardAttachment.objects.count(), 1)

    def test_shared_name_keeps_file(self):
        name = self._attach()
        other = self._card()
        CardAttachment.objects.create(card=other, file=name, filename="f.txt", size=1)
        with self.captureOnCommitCallbacks(execute=True):
            self.card.delete()
        self.assertTrue(default_storage.exists(name))

    def test_empty_name_and_missing_file_do_not_raise(self):
        CardAttachment.objects.create(card=self.card, file="", filename="e", size=0)
        CardAttachment.objects.create(
            card=self.card, file="attachments/2026/10/gone.txt", filename="g", size=0,
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.card.delete()
        self.assertEqual(CardAttachment.objects.count(), 0)

    def test_name_outside_attachments_tree_is_never_deleted(self):
        outside = default_storage.save("avatars/keep.txt", ContentFile(b"x"))
        for bad in (outside, "attachments/../avatars/keep.txt"):
            CardAttachment.objects.create(card=self.card, file=bad, filename="b", size=1)
        with self.captureOnCommitCallbacks(execute=True):
            self.card.delete()
        self.assertTrue(default_storage.exists(outside))

    def test_storage_failure_is_swallowed_and_logs_class_only(self):
        name = self._attach()
        with patch("django.core.files.storage.FileSystemStorage.delete", side_effect=OSError("secret/path")):
            with self.assertLogs("boards.models", level="WARNING") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    self.card.delete()
        self.assertIn("OSError", logs.output[0])
        self.assertNotIn(name, logs.output[0])
        self.assertNotIn("secret/path", logs.output[0])


class PurgeOrphanedAttachmentsTests(_MediaTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("purge_orphaned_attachments", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_dry_run_reports_and_deletes_nothing(self):
        orphan = self._attach(row=False)
        self._age(orphan, 48)
        out = self._run()
        self.assertNotIn(orphan, out)
        self.assertIn("1 would be deleted", out)
        self.assertTrue(default_storage.exists(orphan))

    def test_names_only_printed_at_verbosity_2(self):
        orphan = self._attach(row=False)
        self._age(orphan, 48)
        self.assertNotIn(orphan, self._run())
        self.assertIn(orphan, self._run("--verbosity", "2"))

    def test_file_vanishing_mid_run_is_skipped(self):
        orphan = self._attach(row=False)
        self._age(orphan, 48)
        with patch("django.core.files.storage.FileSystemStorage.get_modified_time", side_effect=FileNotFoundError):
            self.assertIn("0 would be deleted", self._run())

    def test_unicode_normalization_does_not_orphan_a_live_file(self):
        import unicodedata
        nfc = unicodedata.normalize("NFC", "attachments/2026/10/caf\u00e9.txt")
        nfd = unicodedata.normalize("NFD", nfc)
        CardAttachment.objects.create(card=self.card, file=nfc, filename="c", size=1)
        with patch("boards.management.commands.purge_orphaned_attachments._walk", return_value=iter([nfd])):
            self.assertIn("0 would be deleted", self._run())

    def test_yes_deletes_only_old_orphans(self):
        old = self._attach(name="attachments/2026/10/old.txt", row=False)
        young = self._attach(name="attachments/2026/10/young.txt", row=False)
        kept = self._attach(name="attachments/2026/10/kept.txt")
        outside = default_storage.save("other/outside.txt", ContentFile(b"x"))
        self._age(old, 48)
        self._age(kept, 48)
        self._age(outside, 48)
        out = self._run("--yes")
        self.assertFalse(default_storage.exists(old))
        self.assertTrue(default_storage.exists(young))
        self.assertTrue(default_storage.exists(kept))
        self.assertTrue(default_storage.exists(outside))
        self.assertIn("deleted 1", out)
        self.assertIn("1 too recent", out)

    def test_row_created_after_snapshot_wins(self):
        orphan = self._attach(row=False)
        self._age(orphan, 48)
        real_exists = CardAttachment.objects.filter

        def late_row(*args, **kwargs):
            if "file__in" in kwargs:
                CardAttachment.objects.create(card=self.card, file=orphan, filename="f", size=1)
            return real_exists(*args, **kwargs)

        with patch.object(CardAttachment.objects, "filter", side_effect=late_row):
            self._run("--yes")
        self.assertTrue(default_storage.exists(orphan))

    def test_no_attachments_directory_is_a_clean_noop(self):
        self.assertIn("0 would be deleted", self._run())

    def test_min_age_must_be_positive(self):
        with self.assertRaises(CommandError):
            self._run("--min-age-hours", "0")
