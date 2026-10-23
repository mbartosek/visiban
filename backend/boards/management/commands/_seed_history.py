"""Card history for seeded boards, in the forms the API writes (#1889).

The seed commands build boards row by row instead of through the API, so
nothing writes a card's audit trail for them unless they do it themselves.
Before #1889 they wrote movements only (or a stray, unrelated activity), so a
card could carry labels, a weight, an assignee or a ticked checklist that its
history never mentioned. This module writes the history a person would have
left by making the card through the API: a creation movement, then one
activity per change, each in the exact shape the API's own writers use:

* ``services.cards.create_card``: the creation movement, noted "Card created";
* ``services.cards.update_card``: ``assignee_change`` ("Unassigned" -> username),
  ``label_change`` ("+A, B"), ``priority_change`` (from "medium", the model
  default), ``weight_change`` (from "1"), ``due_date_change`` ("" -> ISO date);
* the checklist endpoints: ``checklist_item_added`` / ``checklist_item_checked``
  with the item text as ``to_value``;
* the comment endpoint: ``comment_added`` with both values empty, at the
  comment's own time and by its author;
* ``services.cards.archive_card``: an ``archived`` movement in place.

A card is modeled as created with its title and description (the creation
movement is the record of both; the API writes no activity for values sent
on create) and then edited into its current state, so every other value it
holds is backed by an activity. Custom-field values, attachments, relations
and MR/PR links have no card activity in the open-source API (it writes only a
``card.updated`` change-feed row with no field diff), so nothing is written
for them here either.

No ``random``: every choice is derived from the card, so a re-seed writes the
same history and no caller's random stream is disturbed.
"""

import datetime
import zlib

from django.db import transaction
from django.utils import timezone

from boards import broadcast as _broadcast
from boards.models import Card, CardActivity, CardMovement
from boards.services.cards import delete_boards_locked

#: The note ``services.cards.create_card`` writes on a card's first movement.
CREATED_NOTE = "Card created"


def _backdate(model, pk, field, at):
    # auto_now_add ignores a value passed to create(), so timestamps are
    # written with a follow-up UPDATE, the pattern every seeder here uses.
    model.objects.filter(pk=pk).update(**{field: at})


def _movement(card, *, src, dst, src_lane, dst_lane, actor, at, notes="", movement_type=None):
    fields = dict(
        card=card,
        from_column=src, to_column=dst, from_swimlane=src_lane, to_swimlane=dst_lane,
        from_column_name=src.name if src else "", to_column_name=dst.name,
        from_column_uid=src.uid if src else "", to_column_uid=dst.uid,
        from_swimlane_name=src_lane.name if src_lane else "", to_swimlane_name=dst_lane.name,
        from_swimlane_uid=src_lane.uid if src_lane else "", to_swimlane_uid=dst_lane.uid,
        moved_by=actor, notes=notes,
    )
    if movement_type:
        fields["movement_type"] = movement_type
    mv = CardMovement.objects.create(**fields)
    _backdate(CardMovement, mv.pk, "moved_at", at)
    return mv


def record_creation(card, *, column, swimlane, actor, at):
    """Write the creation movement at ``at`` and date the card itself to it.

    ``Card.created_at`` would otherwise read as the moment of the seed, after
    every event in its history.
    """
    mv = _movement(card, src=None, dst=column, src_lane=None, dst_lane=swimlane,
                   actor=actor, at=at, notes=CREATED_NOTE)
    _backdate(Card, card.pk, "created_at", at)
    card.created_at = at
    return mv


def record_move(card, *, src, dst, swimlane, actor, at, notes=""):
    """Write one column move within ``swimlane`` at ``at``."""
    return _movement(card, src=src, dst=dst, src_lane=swimlane, dst_lane=swimlane,
                     actor=actor, at=at, notes=notes)


def record_archive(card, *, actor, at):
    """Archive ``card`` at ``at``: ``archived_at`` plus the in-place movement
    ``archive_card`` writes."""
    _movement(card, src=card.column, dst=card.column, src_lane=card.swimlane, dst_lane=card.swimlane,
              actor=actor, at=at, movement_type=CardMovement.MovementType.ARCHIVED)
    Card.objects.filter(pk=card.pk).update(archived_at=at)
    card.archived_at = at


def history_window(card):
    """``(start, end)``: the card's creation and the end of the span its edits fall in.

    ``end`` is the card's last movement, or a day after creation when the card
    never moved, never later than now. Activities land between the two, so
    they never precede the card or postdate the reset.
    """
    moves = list(card.movements.order_by("moved_at", "id").values_list("moved_at", "movement_type"))
    start = moves[0][0]
    last = max((at for at, kind in moves if kind == CardMovement.MovementType.MOVE), default=start)
    end = max(last, start + datetime.timedelta(days=1)) if len(moves) == 1 else last
    end = min(end, timezone.now() - datetime.timedelta(minutes=1))
    return start, max(start, end)


def write_activity_trail(card, *, actors):
    """Write the activity a person would have left editing ``card`` into its current state.

    Call once the card is complete: moved, labeled, commented and with its
    checklist. Reads the state back from the database. ``actors`` are the
    users the edits are credited to (never an account that must not appear
    as an author, such as the hosted demo's published visitor). Comments keep
    their own time and author; everything else is spread across
    :func:`history_window`, in a fixed order.
    """
    actors = list(actors)
    start, end = history_window(card)
    span = end - start
    seed = zlib.crc32(f"{card.board_id}|{card.title}".encode())
    rows = []

    def add(frac, event_type, from_value, to_value, k):
        rows.append((start + span * frac, CardActivity(
            card=card, event_type=event_type, from_value=from_value, to_value=to_value,
            actor=actors[(seed + k) % len(actors)],
        )))

    ET = CardActivity.EventType
    if card.assignee_id:
        add(0.05, ET.ASSIGNEE_CHANGE, "Unassigned", card.assignee.username, 0)
    labels = sorted(card.labels.values_list("name", flat=True))
    if labels:
        add(0.08, ET.LABEL_CHANGE, "", f"+{', '.join(labels)}", 1)
    if card.priority != Card.Priority.MEDIUM:
        add(0.1, ET.PRIORITY_CHANGE, Card.Priority.MEDIUM, card.priority, 2)
    if card.weight != 1:
        add(0.12, ET.WEIGHT_CHANGE, "1", str(card.weight), 1)
    if card.due_date:
        add(0.15, ET.DUE_DATE_CHANGE, "", card.due_date.isoformat(), 0)
    items = list(card.checklist_items.order_by("position", "id"))
    for i, item in enumerate(items):
        # Added in order up front, ticked off later: a check never precedes its item.
        add(0.18 + 0.12 * i / len(items), ET.CHECKLIST_ITEM_ADDED, "", item.text, 2)
        if item.is_checked:
            add(0.4 + 0.5 * i / len(items), ET.CHECKLIST_ITEM_CHECKED, "", item.text, i)
    for comment in card.comments.order_by("created_at", "id"):
        rows.append((comment.created_at, CardActivity(
            card=card, event_type=ET.COMMENT_ADDED, from_value="", to_value="", actor=comment.author,
        )))

    rows.sort(key=lambda r: r[0])
    created = CardActivity.objects.bulk_create([act for _, act in rows])
    for (at, _), act in zip(rows, created):
        act.created_at = at
    CardActivity.objects.bulk_update(created, ["created_at"])
    return len(created)


def delete_seed_boards(boards):
    """Delete ``boards`` and record a ``board.deleted`` change-feed row for each.

    The seed commands' ``--wipe`` deletes boards outside any view. Without
    this, the change feed kept the boards' earlier rows (``BoardEvent.board_id``
    is deliberately not a foreign key) and never said the board was gone, so a
    consumer holding a cursor was left on a board that no longer exists
    (#1636). Same payload and order as ``BoardViewSet.destroy``: the feed row
    is written in the delete's transaction and published after it commits.
    Returns ``QuerySet.delete()``'s result.
    """
    with transaction.atomic():
        doomed = list(boards.values_list("pk", "uid", "group_id"))
        if not doomed:
            return 0, {}
        result = delete_boards_locked(boards.model.objects.filter(pk__in=[pk for pk, _, _ in doomed]))
        for board_id, board_uid, group_id in doomed:
            payload = {"board_uid": board_uid}
            event_id = _broadcast.persist_board_event(board_id, _broadcast.EVT_BOARD_DELETED, payload)

            def _publish(bid=board_id, gid=group_id, pl=payload, eid=event_id):
                _broadcast.broadcast_board_event(bid, _broadcast.EVT_BOARD_DELETED, pl, event_id=eid)
                if gid is not None:
                    from groups.broadcast import broadcast_group_event

                    broadcast_group_event(gid, _broadcast.EVT_BOARD_DELETED, pl)

            transaction.on_commit(_publish)
    return result
