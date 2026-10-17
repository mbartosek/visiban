"""Card and swimlane writes naming a label or custom field definition vs its
DELETE (#1632).

Card validation resolves ``label_ids`` and ``custom_field_values`` definitions
before the request's transaction and before any lock. A DELETE of one of them
that committed after that (or that commits before the card write does) left
the M2M / value row the card writer inserts pointing at a missing row: a 500
from the deferred foreign key at COMMIT.

The fix: ``create_card`` and ``update_card`` take the board row ``FOR SHARE``
as the first lock of their transaction (``lock_board_for_card_write``), and
``CardSerializer._require_live_referents`` re-reads the named rows under it.
Every label and definition delete takes the board row ``FOR NO KEY UPDATE``
first, which conflicts with ``FOR SHARE``, so it either committed before the
re-read (the write answers 400, as for any unknown id) or queues behind the
whole write and cascades what it wrote. ``FOR SHARE`` does not conflict with
itself, so card writes do not queue behind each other.

Interleavings, deterministic, no sleeps:

* ``stale`` - the writer is paused before its board lock, the DELETE commits,
  the writer resumes: 400 and nothing written.
* ``queued`` - the writer is paused holding its board lock; the DELETE must
  wait, then (once the write commits) cascades the row it wrote.

Also here: the swimlane create and update writers, the Django admin card form
(``labels`` M2M) and bulk label delete, the custom field definition reorder,
the deadlock the first attempt (row locks on the label and definition rows)
introduced between a card create and a weight-raising update, and SQL-level
pins of the lock order. PostgreSQL only: SQLite has no row locks.
"""

import threading
from unittest import skipUnless
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.db import connection, connections
from django.test import Client, RequestFactory, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from boards import admin as boards_admin
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
from boards.services import cards as card_services
from boards.tests.test_admin_card_cell_entry_race import _admin_post_data
from boards.tests.lock_wait import wait_until_blocked_on_lock

BOARD_LOCK = "lock_board_for_card_write"

HOLD_TIMEOUT = 10.0


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
            waited = wait_until_blocked_on_lock(other)
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
            card_services,
            BOARD_LOCK,
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
            card_services,
            BOARD_LOCK,
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

    def test_reorder_queues_behind_the_write(self):
        """The definition reorder takes the board row first (#1632), so it
        queues whole behind a card write holding the board row, instead of
        meeting the write's commit-time FOR KEY SHARE on definition rows in
        bulk_update's scan order."""
        second = CustomFieldDefinition.objects.create(
            board=self.board, name="Size", field_type="text", position=1
        )
        patched, reordered, waited = self._race(
            card_services,
            BOARD_LOCK,
            self._patch(),
            lambda: self._client().put(
                f"{self.base}/custom-fields/reorder/",
                {"order": [second.pk, self.definition.pk]},
                format="json",
            ),
            after_lock=True,
        )
        self.assertTrue(
            waited, "definition reorder did not queue behind the card write"
        )
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(reordered.status_code, 200, reordered.content[:300])
        self.assertEqual(
            list(
                CustomFieldDefinition.objects.filter(board=self.board)
                .order_by("position")
                .values_list("pk", flat=True)
            ),
            [second.pk, self.definition.pk],
        )


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
            card_services,
            BOARD_LOCK,
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
            card_services,
            BOARD_LOCK,
            self._patch(),
            lambda: self._client().delete(self.label_url),
            after_lock=True,
        )
        self.assertTrue(waited, "label DELETE did not queue behind the assignment")
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertFalse(Label.objects.filter(pk=self.label.pk).exists())
        self.assertEqual(self.card.labels.count(), 0)

    def test_admin_bulk_label_delete_queues_behind_the_assignment(self):
        """``LabelAdmin.delete_queryset`` took no lock before #1632; it now
        takes the board row first, like the single-object admin delete."""
        superuser = User.objects.create_superuser(
            username="admin1632", password="pass", email="a1632@example.com"
        )

        def bulk_delete():
            request = RequestFactory().post("/")
            request.user = superuser
            model_admin = django_admin.site._registry[Label]
            model_admin.delete_queryset(request, Label.objects.filter(pk=self.label.pk))
            return "deleted"

        patched, deleted, waited = self._race(
            card_services,
            BOARD_LOCK,
            self._patch(),
            bulk_delete,
            after_lock=True,
        )
        self.assertTrue(
            waited, "admin bulk label delete did not queue behind the assignment"
        )
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(deleted, "deleted")
        self.assertFalse(Label.objects.filter(pk=self.label.pk).exists())
        self.assertEqual(self.card.labels.count(), 0)


class CardCreateVsLabelDeleteTests(_Base):
    """The ``create()`` half: a create resolves labels before its transaction
    too, and re-reads them under the board row."""

    def setUp(self):
        super().setUp()
        self.col.allow_card_creation = True
        self.col.save()
        self.label = Label.objects.create(board=self.board, name="Bug")
        self.label_url = f"{self.base}/labels/{self.label.pk}/"

    def _create(self):
        return lambda: self._client().post(
            f"{self.base}/cards/",
            {
                "title": "New",
                "column": self.col.pk,
                "swimlane": self.lane.pk,
                "label_ids": [self.label.pk],
            },
            format="json",
        )

    def test_stale_label_answers_400_not_500(self):
        created, deleted, _ = self._race(
            card_services,
            BOARD_LOCK,
            self._create(),
            lambda: self._client().delete(self.label_url),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(created.status_code, 400, created.content[:300])
        self.assertIn("label_ids", created.json())
        self.assertFalse(Card.objects.filter(title="New").exists())

    def test_delete_queues_behind_the_create_then_removes_the_label(self):
        created, deleted, waited = self._race(
            card_services,
            BOARD_LOCK,
            self._create(),
            lambda: self._client().delete(self.label_url),
            after_lock=True,
        )
        self.assertTrue(waited, "label DELETE did not queue behind the create")
        self.assertEqual(created.status_code, 201, created.content[:300])
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(Card.objects.get(title="New").labels.count(), 0)


# Plain static storage: the re-rendered change form (the error case) needs no
# collected manifest.
@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class AdminCardFormVsLabelDeleteTests(_Base):
    """The Django admin card form's ``labels`` M2M (#1632)."""

    def setUp(self):
        super().setUp()
        self.superuser = User.objects.create_superuser(
            username="admin1632", password="pass", email="a1632@example.com"
        )
        self.label = Label.objects.create(board=self.board, name="Bug")
        self.label_url = f"{self.base}/labels/{self.label.pk}/"
        self.data = _admin_post_data(self.card, labels=[self.label.pk])

    def _admin_save(self):
        def run():
            client = Client()
            client.force_login(self.superuser)
            return client.post(
                reverse("admin:boards_card_change", args=[self.card.pk]), self.data
            )

        return run

    def _delete_as_owner(self):
        return lambda: self._client().delete(self.label_url)

    def test_stale_label_is_a_form_error_not_500(self):
        saved, deleted, _ = self._race(
            boards_admin,
            BOARD_LOCK,
            self._admin_save(),
            self._delete_as_owner(),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        # 200 is the change form re-rendered with the error; 302 would be a save.
        self.assertEqual(saved.status_code, 200)
        self.assertIn(b"A selected label was deleted", saved.content)
        self.assertEqual(self.card.labels.count(), 0)

    def test_delete_queues_behind_the_admin_save(self):
        saved, deleted, waited = self._race(
            boards_admin,
            BOARD_LOCK,
            self._admin_save(),
            self._delete_as_owner(),
            after_lock=True,
        )
        self.assertTrue(waited, "label DELETE did not queue behind the admin save")
        self.assertEqual(saved.status_code, 302)
        self.assertEqual(deleted.status_code, 204, deleted.content[:300])
        self.assertEqual(self.card.labels.count(), 0)


class SwimlaneValuesVsDefinitionDeleteTests(_Base):
    """Swimlane writers paused before their board lock; the delete commits first."""

    def setUp(self):
        super().setUp()
        self.definition = SwimlaneCustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )
        self.values = [{"field_definition": self.definition.pk, "value": "gold"}]
        self.def_url = f"{self.base}/swimlane-custom-fields/{self.definition.pk}/"

    def test_patch_stale_definition_answers_400_not_500(self):
        from boards.views import swimlanes as swimlane_views

        patched, deleted, _ = self._race(
            swimlane_views,
            "lock_board_and_row_or_404",
            lambda: self._client().patch(
                f"{self.base}/swimlanes/{self.lane.pk}/",
                {"custom_field_values": self.values},
                format="json",
            ),
            lambda: self._client().delete(self.def_url),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(patched.status_code, 400, patched.content[:300])
        self.assertFalse(
            SwimlaneCustomFieldValue.objects.filter(swimlane=self.lane).exists()
        )

    def test_create_stale_definition_answers_400_not_name_collision(self):
        from boards.views import swimlanes as swimlane_views

        created, deleted, _ = self._race(
            swimlane_views,
            "lock_board_row_or_404",
            lambda: self._client().post(
                f"{self.base}/swimlanes/",
                {"name": "New", "custom_field_values": self.values},
                format="json",
            ),
            lambda: self._client().delete(self.def_url),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(created.status_code, 400, created.content[:300])
        body = created.json()
        self.assertIn("custom_field_values", body)
        self.assertIn("Unknown swimlane custom field", str(body))
        self.assertNotIn("name", body)
        self.assertFalse(Swimlane.objects.filter(name="New").exists())


class CardCreateVsWeightPatchDeadlockTests(_Base):
    def test_create_and_weight_patch_naming_one_label_do_not_deadlock(self):
        """The first attempt at #1632 locked the named label rows: after the
        column row on create, before it on a weight-raising update. A create
        paused holding its column, racing such an update naming the same
        label, then deadlocked (``deadlock detected ... relation "columns"``).
        Paused at ``require_swimlane_exists``, i.e. holding the column row.
        """
        self.board.enforce_weight_limits = True
        self.board.save()
        self.col.allow_card_creation = True
        self.col.weight_limit = 1000
        self.col.save()
        label = Label.objects.create(board=self.board, name="Bug")
        created, patched, _ = self._race(
            card_services,
            "require_swimlane_exists",
            lambda: self._client().post(
                f"{self.base}/cards/",
                {
                    "title": "x",
                    "column": self.col.pk,
                    "swimlane": self.lane.pk,
                    "label_ids": [label.pk],
                },
                format="json",
            ),
            lambda: self._client().patch(
                f"{self.base}/cards/{self.card.pk}/",
                {"weight": 5, "label_ids": [label.pk]},
                format="json",
            ),
            after_lock=False,
        )
        self.assertEqual(created.status_code, 201, created.content[:300])
        self.assertEqual(patched.status_code, 200, patched.content[:300])


class CardWriteLockOrderTests(_Base):
    """Pins the lock statements, which the race tests cannot (#1632).

    Board row ``FOR SHARE`` first, then the card row, then the column row;
    never a row lock on a label or definition row (those row locks are what
    deadlocked). ``FOR SHARE`` specifically: ``FOR NO KEY UPDATE`` would
    serialize every card write on a board, and ``FOR KEY SHARE`` would not
    make a label or definition delete wait.
    """

    def setUp(self):
        super().setUp()
        self.board.enforce_weight_limits = True
        self.board.save()
        self.col.allow_card_creation = True
        self.col.weight_limit = 1000
        self.col.save()
        self.label = Label.objects.create(board=self.board, name="Bug")
        self.definition = CustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )

    def _locking(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            response = fn()
        self.assertLess(response.status_code, 300, response.content[:300])
        return [q["sql"] for q in ctx.captured_queries if " FOR " in q["sql"]]

    def _assert_no_referent_row_locks(self, locking):
        for table in (Label._meta.db_table, CustomFieldDefinition._meta.db_table):
            self.assertFalse(
                [sql for sql in locking if f'FROM "{table}"' in sql], locking
            )

    def test_update_locks_board_then_card_then_column(self):
        locking = self._locking(
            lambda: self._client().patch(
                f"{self.base}/cards/{self.card.pk}/",
                {
                    "weight": 5,
                    "label_ids": [self.label.pk],
                    "custom_field_values": [
                        {"field_definition": self.definition.pk, "value": "gold"}
                    ],
                },
                format="json",
            )
        )
        self.assertEqual(
            locking[0],
            'SELECT 1 FROM "boards" WHERE "id" = %s FOR SHARE' % self.board.pk,
            locking,
        )
        self.assertIn('FROM "cards"', locking[1])
        self.assertIn("FOR NO KEY UPDATE", locking[1])
        self.assertIn('FROM "columns"', locking[2])
        self._assert_no_referent_row_locks(locking)

    def test_create_locks_board_then_column(self):
        locking = self._locking(
            lambda: self._client().post(
                f"{self.base}/cards/",
                {
                    "title": "New",
                    "column": self.col.pk,
                    "swimlane": self.lane.pk,
                    "label_ids": [self.label.pk],
                    "custom_field_values": [
                        {"field_definition": self.definition.pk, "value": "gold"}
                    ],
                },
                format="json",
            )
        )
        self.assertEqual(
            locking[0],
            'SELECT 1 FROM "boards" WHERE "id" = %s FOR SHARE' % self.board.pk,
            locking,
        )
        self.assertIn('FROM "columns"', locking[1])
        self._assert_no_referent_row_locks(locking)

    def test_definition_reorder_locks_board_then_rows_in_pk_order(self):
        second = CustomFieldDefinition.objects.create(
            board=self.board, name="Size", field_type="text", position=1
        )
        table = CustomFieldDefinition._meta.db_table
        locking = self._locking(
            lambda: self._client().put(
                f"{self.base}/custom-fields/reorder/",
                {"order": [second.pk, self.definition.pk]},
                format="json",
            )
        )
        self.assertIn('FROM "boards"', locking[0])
        self.assertIn("FOR NO KEY UPDATE", locking[0])
        self.assertIn(f'FROM "{table}"', locking[1])
        self.assertIn(f'ORDER BY "{table}"."id" ASC', locking[1])
        self.assertTrue(locking[1].rstrip().endswith("FOR UPDATE"), locking[1])


class CardWriteVsOwnerDeactivationTests(_Base):
    """A card write holds the board row ``FOR SHARE`` and needs ``FOR KEY
    SHARE`` on its actor's user row at COMMIT (the deferred foreign keys of
    ``CardActivity.actor``, ``created_by``, ``assignee``). Deactivating that
    user with a board transfer locked the user row ``FOR UPDATE`` (which
    blocks ``FOR KEY SHARE``) and then UPDATEd the owned board (which waits on
    ``FOR SHARE``): ``deadlock detected ... relation "boards"``, a 500 on the
    deactivation. The deactivation now locks the user ``FOR NO KEY UPDATE``,
    as the user admin delete does (#1616), so the card write commits and the
    transfer follows it.
    """

    def test_owner_card_write_and_deactivation_with_transfer_both_succeed(self):
        heir = User.objects.create_user(username="heir1632", password="pass")
        BoardMembership.objects.create(
            board=self.board, user=heir, role=BoardMembership.Role.ADMIN
        )
        admin = User.objects.create_superuser(
            username="su1632", password="pass", email="su1632@example.com"
        )

        def deactivate():
            client = APIClient()
            client.force_authenticate(admin)
            return client.post(
                f"/api/v1/admin/users/{self.owner.pk}/deactivate/",
                {"transfers": [{"board_id": self.board.pk, "transfer_to": heir.pk}]},
                format="json",
            )

        patched, deactivated, waited = self._race(
            card_services,
            "lock_card_for_write",
            lambda: self._client().patch(
                f"{self.base}/cards/{self.card.pk}/", {"title": "T2"}, format="json"
            ),
            deactivate,
            after_lock=True,
        )
        self.assertTrue(
            waited, "the board transfer did not queue behind the card write"
        )
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(deactivated.status_code, 200, deactivated.content[:300])
        self.board.refresh_from_db()
        self.assertEqual(self.board.owner_id, heir.pk)
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_active)


class ValidatedBeforeTheLockTests(_Base):
    """Callers that validate before the card service's transaction (#1632).

    The REST PATCH validates inside ``apply()``, under the board lock, so its
    stale tests above never reach ``CardSerializer._require_live_referents``.
    The MCP ``create_card``/``update_card`` tools (and the REST create)
    validate first; these pin the re-read in both ``create()`` and
    ``update()``: without it the stale write fails its deferred foreign key
    at COMMIT instead of answering a validation error.
    """

    def setUp(self):
        super().setUp()
        self.col.allow_card_creation = True
        self.col.save()
        self.label = Label.objects.create(board=self.board, name="Bug")
        self.definition = CustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )

    def _as_mcp_user(self, fn):
        def run():
            from mcp_server.context import reset_current_user, set_current_user

            token = set_current_user(self.owner)
            try:
                return fn()
            finally:
                reset_current_user(token)

        return run

    def _delete(self, url):
        return lambda: self._client().delete(url)

    def test_mcp_update_with_stale_label_is_a_validation_error(self):
        from mcp_server import tools

        result, deleted, _ = self._race(
            card_services,
            BOARD_LOCK,
            self._as_mcp_user(
                lambda: tools.update_card(card_id=self.card.pk, labels=["Bug"])
            ),
            self._delete(f"{self.base}/labels/{self.label.pk}/"),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertIn("label_ids", str(result["error"]["errors"]))
        self.assertEqual(self.card.labels.count(), 0)

    def test_mcp_create_with_stale_label_is_a_validation_error(self):
        from mcp_server import tools

        result, deleted, _ = self._race(
            card_services,
            BOARD_LOCK,
            self._as_mcp_user(
                lambda: tools.create_card(
                    board_id=self.board.pk,
                    column_id=self.col.pk,
                    swimlane_id=self.lane.pk,
                    title="New",
                    labels=["Bug"],
                )
            ),
            self._delete(f"{self.base}/labels/{self.label.pk}/"),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(result["error"]["code"], "validation_error", result)
        self.assertFalse(Card.objects.filter(title="New").exists())

    def test_create_with_stale_definition_answers_400(self):
        created, deleted, _ = self._race(
            card_services,
            BOARD_LOCK,
            lambda: self._client().post(
                f"{self.base}/cards/",
                {
                    "title": "New",
                    "column": self.col.pk,
                    "swimlane": self.lane.pk,
                    "custom_field_values": [
                        {"field_definition": self.definition.pk, "value": "gold"}
                    ],
                },
                format="json",
            ),
            self._delete(f"{self.base}/custom-fields/{self.definition.pk}/"),
            after_lock=False,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(created.status_code, 400, created.content[:300])
        self.assertIn("Unknown custom field", str(created.json()))
        self.assertFalse(Card.objects.filter(title="New").exists())


class SwimlaneDefinitionReorderTests(_Base):
    """The swimlane definition reorder takes the board row first, then its
    rows in pk order (#1632), like the card definition reorder."""

    def setUp(self):
        super().setUp()
        self.first = SwimlaneCustomFieldDefinition.objects.create(
            board=self.board, name="Tier", field_type="text", position=0
        )
        self.second = SwimlaneCustomFieldDefinition.objects.create(
            board=self.board, name="Size", field_type="text", position=1
        )
        self.reorder_url = f"{self.base}/swimlane-custom-fields/reorder/"

    def _reorder(self):
        return lambda: self._client().put(
            self.reorder_url,
            {"order": [self.second.pk, self.first.pk]},
            format="json",
        )

    def test_reorder_queues_behind_a_swimlane_write(self):
        from boards.views import swimlanes as swimlane_views

        patched, reordered, waited = self._race(
            swimlane_views,
            "lock_board_and_row_or_404",
            lambda: self._client().patch(
                f"{self.base}/swimlanes/{self.lane.pk}/",
                {
                    "custom_field_values": [
                        {"field_definition": self.first.pk, "value": "gold"}
                    ]
                },
                format="json",
            ),
            self._reorder(),
            after_lock=True,
        )
        self.assertTrue(waited, "swimlane definition reorder did not queue")
        self.assertEqual(patched.status_code, 200, patched.content[:300])
        self.assertEqual(reordered.status_code, 200, reordered.content[:300])
        self.assertEqual(
            list(
                SwimlaneCustomFieldDefinition.objects.filter(board=self.board)
                .order_by("position")
                .values_list("pk", flat=True)
            ),
            [self.second.pk, self.first.pk],
        )

    def test_reorder_locks_board_then_rows_in_pk_order(self):
        table = SwimlaneCustomFieldDefinition._meta.db_table
        with CaptureQueriesContext(connection) as ctx:
            response = self._reorder()()
        self.assertEqual(response.status_code, 200, response.content[:300])
        locking = [q["sql"] for q in ctx.captured_queries if " FOR " in q["sql"]]
        self.assertIn('FROM "boards"', locking[0])
        self.assertIn("FOR NO KEY UPDATE", locking[0])
        self.assertIn(f'FROM "{table}"', locking[1])
        self.assertIn(f'ORDER BY "{table}"."id" ASC', locking[1])
        self.assertTrue(locking[1].rstrip().endswith("FOR UPDATE"), locking[1])
