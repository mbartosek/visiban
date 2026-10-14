"""Card writes naming a label or custom field definition vs its DELETE (#1632).

Card validation resolves ``label_ids`` and ``custom_field_values`` definitions
before the request's transaction and before any lock. A DELETE of one of them
that committed after that (or that commits before the card write does) left
the M2M / value row the card writer inserts pointing at a missing row: a 500
from the deferred foreign key at COMMIT. ``CardSerializer`` now locks the
referenced label and definition rows ``FOR NO KEY UPDATE`` inside the card
transaction, the mode the DELETE's own ``lock_board_and_row_or_404`` takes
first, and re-resolves them under the lock.

Two deterministic interleavings per resource, no sleeps:

* ``stale`` - the card PATCH is paused after validation and before its lock,
  the DELETE commits, the PATCH resumes: it answers 400 (the answer a request
  naming an unknown id always got) and writes nothing.
* ``queued`` - the card PATCH is paused after taking its lock; the DELETE must
  wait, then (once the PATCH commits) cascades the row the PATCH wrote.

The swimlane ``custom_field_values`` writer takes the board row before it
re-resolves its definitions, so it was already covered by #1602
(``SwimlanePatchVsDefinitionDeleteTests`` in
``test_row_writers_stale_snapshot.py``); ``SwimlaneValuesVsDefinitionDeleteTests``
here adds the same interleaving with the writer paused before the lock, so both
writers named by #1632 are exercised in this file.

Label assignment vs label DELETE has the same shape as the definition case and
is exercised by ``CardLabelVsLabelDeleteTests``; both interleavings failed
with a 500 before this change on PostgreSQL. PostgreSQL only: SQLite has no
row locks.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from accounts.models import User
from boards.models import (
    Board,
    BoardMembership,
    Card,
    Column,
    CustomFieldDefinition,
    CustomFieldValue,
    Label,
    Swimlane,
    SwimlaneCustomFieldDefinition,
    SwimlaneCustomFieldValue,
)
from boards.serializers import CardSerializer

HOLD_TIMEOUT = 10.0
BLOCK_PROBE = 0.5


@skipUnless(
    connection.vendor == "postgresql", "row locks are only observable on PostgreSQL"
)
class _Base(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner1632", password="pass")
        self.board = Board.objects.create(name="B", owner=self.owner)
        BoardMembership.objects.create(
            board=self.board, user=self.owner, role=BoardMembership.Role.ADMIN
        )
        self.col = Column.objects.create(board=self.board, name="A", position=0)
        self.lane = Swimlane.objects.create(board=self.board, name="G", position=0)
        self.card = Card.objects.create(
            board=self.board,
            column=self.col,
            swimlane=self.lane,
            title="C",
            created_by=self.owner,
            position=0,
        )
        patcher = patch("boards.broadcast.broadcast_board_event")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = f"/api/v1/boards/{self.board.pk}"

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.owner)
        return client

    def _thread(self, name, fn, results):
        def run():
            try:
                results[name] = fn()
            except Exception as exc:  # surfaced by the assertions
                results[name] = exc
            finally:
                connections.close_all()

        t = threading.Thread(target=run, name=name)
        t.start()
        return t

    def _race(self, hold_target, hold_attr, holder_fn, other_fn, *, after_lock):
        """Pause ``holder_fn`` at ``hold_target.hold_attr`` (before the real call
        when ``after_lock`` is false, after it when true), run ``other_fn`` to
        completion or until it blocks, then release the holder.

        Returns ``(holder_response, other_response, other_waited)``.
        """
        real = getattr(hold_target, hold_attr)
        paused, release, results = threading.Event(), threading.Event(), {}

        def wrapper(*args, **kwargs):
            on_holder = threading.current_thread().name == "holder"
            if on_holder and not after_lock and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)
            out = real(*args, **kwargs)
            if on_holder and after_lock and not paused.is_set():
                paused.set()
                release.wait(HOLD_TIMEOUT)
            return out

        with patch.object(hold_target, hold_attr, side_effect=wrapper):
            holder = self._thread("holder", holder_fn, results)
            self.assertTrue(
                paused.wait(HOLD_TIMEOUT), "holder never reached its hold point"
            )
            other = self._thread("other", other_fn, results)
            other.join(BLOCK_PROBE)
            waited = other.is_alive()
            release.set()
            holder.join(HOLD_TIMEOUT)
            other.join(HOLD_TIMEOUT)
        for name in ("holder", "other"):
            self.assertNotIsInstance(results.get(name), Exception, results.get(name))
        return results["holder"], results["other"], waited


class CardValuesVsDefinitionDeleteTests(_Base):
    def setUp(self):
        super().setUp()
        self.definition = CustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )
        self.url = f"{self.base}/cards/{self.card.pk}/"
        self.def_url = f"{self.base}/custom-fields/{self.definition.pk}/"

    def _patch(self):
        return lambda: self._client().patch(
            self.url,
            {
                "custom_field_values": [
                    {"field_definition": self.definition.pk, "value": "gold"}
                ]
            },
            format="json",
        )

    def test_stale_definition_answers_400_not_500(self):
        patched, deleted, _ = self._race(
            CardSerializer,
            "_lock_live_referents",
            self._patch(),
            lambda: self._client().delete(self.def_url),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(patched.status_code, 400, patched.content[:300])
        self.assertIn("Unknown custom field", str(patched.json()))
        self.assertFalse(CustomFieldValue.objects.filter(card=self.card).exists())

    def test_delete_queues_behind_the_write_then_cascades_it(self):
        patched, deleted, waited = self._race(
            CardSerializer,
            "_lock_live_referents",
            self._patch(),
            lambda: self._client().delete(self.def_url),
            after_lock=True,
        )
        self.assertTrue(waited, "definition DELETE did not queue behind the card write")
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertFalse(
            CustomFieldDefinition.objects.filter(pk=self.definition.pk).exists()
        )
        self.assertFalse(CustomFieldValue.objects.filter(card=self.card).exists())


class CardLabelVsLabelDeleteTests(_Base):
    def setUp(self):
        super().setUp()
        self.label = Label.objects.create(board=self.board, name="Bug")
        self.url = f"{self.base}/cards/{self.card.pk}/"
        self.label_url = f"{self.base}/labels/{self.label.pk}/"

    def _patch(self):
        return lambda: self._client().patch(
            self.url, {"label_ids": [self.label.pk]}, format="json"
        )

    def test_stale_label_answers_400_not_500(self):
        patched, deleted, _ = self._race(
            CardSerializer,
            "_lock_live_referents",
            self._patch(),
            lambda: self._client().delete(self.label_url),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(patched.status_code, 400, patched.content[:300])
        self.assertIn("label_ids", patched.json())
        self.assertEqual(self.card.labels.count(), 0)

    def test_delete_queues_behind_the_assignment_then_removes_it(self):
        patched, deleted, waited = self._race(
            CardSerializer,
            "_lock_live_referents",
            self._patch(),
            lambda: self._client().delete(self.label_url),
            after_lock=True,
        )
        self.assertTrue(waited, "label DELETE did not queue behind the assignment")
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertFalse(Label.objects.filter(pk=self.label.pk).exists())
        self.assertEqual(self.card.labels.count(), 0)


class SwimlaneValuesVsDefinitionDeleteTests(_Base):
    """Swimlane writer paused before its board lock; the delete commits first."""

    def test_stale_definition_answers_400_not_500(self):
        from boards.views import swimlanes as swimlane_views

        definition = SwimlaneCustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )
        patched, deleted, _ = self._race(
            swimlane_views,
            "lock_board_and_row_or_404",
            lambda: self._client().patch(
                f"{self.base}/swimlanes/{self.lane.pk}/",
                {
                    "custom_field_values": [
                        {"field_definition": definition.pk, "value": "gold"}
                    ]
                },
                format="json",
            ),
            lambda: self._client().delete(
                f"{self.base}/swimlane-custom-fields/{definition.pk}/"
            ),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(patched.status_code, 400, patched.content[:300])
        self.assertFalse(
            SwimlaneCustomFieldValue.objects.filter(swimlane=self.lane).exists()
        )
