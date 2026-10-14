from types import SimpleNamespace

from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404

from rest_framework.exceptions import NotFound

from groups.broadcast import broadcast_group_event as _broadcast_group_event

from . import broadcast as _broadcast
from .models import (
    Board, BoardMembership, Column, Swimlane, Label, Card, CardMovement,
    CardComment, CardActivity, CardAttachment, CardChecklist, Notification,
)
from .serializers import (
    BoardMembershipSerializer, BoardSerializer, ColumnSerializer, LabelSerializer,
    SwimlaneSerializer,
)
from .permissions import SITE_ADMIN
from .admin_mixins import ContainerChangedAdminMixin
from .services.cards import (
    enforce_column_limits, lock_card_cell_entry, lock_card_for_write, lock_card_row,
    lock_cards_for_cascade, require_swimlane_exists,
)
from .services.errors import (
    CardNotFound, SwimlaneNotFound, WeightLimitExceeded, WipHardBlocked, WipLimitExceeded,
)
from .views._helpers import _refetched_card_data


def _record_board_and_group_event(board_id, group_id, event_type, payload, *, actor_id):
    """Persist one board event and broadcast it to the board channel and,
    when the board belongs to a group, the group channel too — deferred
    until commit, same as record_board_event().

    Board is the only admin-registered model that needs this: BoardViewSet's
    perform_create/perform_update/destroy (boards/views/boards.py) already
    dual-broadcast board.created/updated/deleted to both channels in one
    on_commit callback, because GroupDetail.tsx's dashboard/boards-list view
    listens for those events exclusively on the group socket, not the board
    socket. This is BoardAdmin's counterpart to that — see BoardAdmin's
    docstring for why it doesn't just use _BoardEventAdminMixin.
    """
    event_id = _broadcast.persist_board_event(board_id, event_type, payload, actor_id=actor_id)

    def _send(bid=board_id, evt=event_type, pl=payload, gid=group_id, eid=event_id):
        _broadcast.broadcast_board_event(bid, evt, pl, event_id=eid)
        if gid is not None:
            _broadcast_group_event(gid, evt, pl)

    transaction.on_commit(_send)


def _limit_error_message(exc):
    """A sentence for the admin form from a card-service limit error."""
    if isinstance(exc, WipHardBlocked):
        return (
            f'Column "{exc.column_name}" is at its WIP limit ({exc.current_count}/'
            f"{exc.wip_limit}) and the board enforces hard WIP limits, so no role "
            "can add a card to it. Move a card out or raise the limit first."
        )
    if isinstance(exc, WipLimitExceeded):
        return (
            f'Column "{exc.column_name}" is at its WIP limit ({exc.current_count}/'
            f"{exc.wip_limit}). Raise the limit, or override it as a board admin "
            "through the API with ?force=true."
        )
    return (
        f'Column "{exc.column_name}" would exceed its weight limit '
        f"({exc.current_weight} + {exc.card_weight} > {exc.weight_limit}). Raise the "
        "limit, or override it as a board admin through the API with ?force=true."
    )


def _require_present_for_admin(*querysets):
    """``require_present_or_404`` for the admin: ``Http404`` instead of the
    DRF ``NotFound``, so an admin delete that queued behind another delete of
    the same object answers the admin's own 404 and records nothing (#1602).
    ``delete_view`` runs inside one transaction, so the deletion log entry
    Django wrote before ``delete_model`` rolls back with it."""
    for qs in querysets:
        if not qs.exists():
            raise Http404("This object was deleted.")


def _lock_board_for_admin_save(board_id, model=None, pk=None):
    """Lock a board row, then optionally one of its rows, before an admin
    save; raise ``Http404`` if either is gone (#1602).

    Every container delete holds the board row ``FOR NO KEY UPDATE`` from
    the start of its cascade. An admin save queued behind one would
    otherwise re-insert the deleted board, column or swimlane (``save()``
    falls back to an INSERT when its UPDATE matches no row) or insert a row
    whose deferred board foreign key fails at COMMIT (a 500). Board -> row
    is column reorder's order; both locks are ``FOR NO KEY UPDATE``, the
    mode the UPDATE takes anyway, so a card insert's ``FOR KEY SHARE`` is
    never blocked. ``changeform_view``'s transaction holds them until the
    save commits. ``Http404``, as the card-child admin hooks answer, because
    the admin cannot return a DRF 404.
    """
    if Board.objects.select_for_update(no_key=True).filter(pk=board_id).first() is None:
        raise Http404("This board was deleted.")
    if pk is not None and (
        model.objects.select_for_update(no_key=True).filter(pk=pk).first() is None
    ):
        raise Http404("This object was deleted.")


class CardAdminForm(forms.ModelForm):
    """Applies the card service's placement rules to Django admin saves (#1428).

    ``CardAdmin.save_model`` writes the row directly, so without this form a
    staff user could create a card in a column with ``allow_card_creation``
    off, or put a card into (or grow one inside) a column past its WIP or
    weight limit — including under hard WIP mode, which the API documents as
    "blocked for all roles". The rules are the service's own
    (:func:`boards.services.cards.enforce_column_limits`), evaluated in
    ``clean()`` so a refusal is an ordinary form error rather than a 500.

    There is no override here: the admin form has no ``force`` control, so a
    soft limit is refused too. A site admin who needs to exceed a soft limit
    uses the API's ``?force=true`` or raises the column's limit. This is
    stricter than the API for site admins on purpose — the admin is a repair
    tool, not the place to bypass board policy silently.

    ``changeform_view`` runs validation inside ``transaction.atomic()``, so the
    card and column row locks ``clean()`` takes are held until the save commits.

    Lock order (#1588): every lock this form takes follows ``move_card``'s
    card -> column order. For an existing card the card row is locked first
    and its column, swimlane, archive state and weight are re-read under that
    lock (``lock_card_row``), because ``self.instance`` was read unlocked by
    ``get_object`` and a move may have committed since. Then, if the cell
    changes (column or swimlane, archived cards included — they are still
    cell members) or a limit check runs, the fresh source column and the
    target column are locked in one pk-ordered statement, and
    ``enforce_column_limits`` counts under that lock (``lock=False``). A
    created card has no row yet, so only its target column is locked. The
    effect: an admin edit queues behind a concurrent ``move_card`` that holds
    either column instead of committing into its cell behind its membership
    re-check (#1567), and a restore or weight change cannot deadlock with a
    move of the same card by taking the column before the card.
    """

    class Meta:
        model = Card
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        column = cleaned.get("column")
        board = cleaned.get("board")
        if column is None or board is None:
            return cleaned
        # The helper counts cards with board=board, so a column from another
        # board would count zero and silently pass every limit. A card in a
        # column of a different board is never valid anyway.
        if column.board_id != board.pk:
            raise ValidationError({"column": "Column belongs to a different board."})
        original = self.instance
        creating = original.pk is None
        if creating and not column.allow_card_creation:
            raise ValidationError({"column": "Card creation is not allowed in this column."})

        if creating:
            current = None
            source_column_id = None
        else:
            # Card row first, then re-read under the lock: ``self.instance``
            # was loaded unlocked and may be stale, and the columns to lock
            # (and whether the card enters a cell at all) must be decided on
            # the state this transaction will actually overwrite (#1588).
            current = lock_card_row(original.pk)
            if current is None:
                raise ValidationError("This card was deleted while you were editing it.")
            source_column_id = current.column_id

        swimlane = cleaned.get("swimlane")
        swimlane_id = swimlane.pk if swimlane is not None else getattr(current, "swimlane_id", None)
        # Evaluated before the archived early return: an archived card is
        # still a member of its cell, so moving one changes a cell's members
        # just like moving an active card does.
        cell_entry = (
            creating
            or current.column_id != column.pk
            or current.swimlane_id != swimlane_id
        )

        archived_at = cleaned.get("archived_at", getattr(current, "archived_at", None))
        weight = cleaned.get("weight", getattr(current, "weight", original.weight))
        entering = (
            creating
            or current.archived_at is not None  # restore
            or current.column_id != column.pk  # move
        )
        weight_up = not entering and weight > current.weight
        enforce = archived_at is None and (entering or weight_up)

        if cell_entry or enforce:
            # Columns after the card row, source and target in one pk-ordered
            # statement — move_card's order. The card row is already held.
            locked_columns = lock_card_cell_entry(
                card_id=None, column_ids={source_column_id, column.pk} - {None},
            )
            # A column, swimlane or board delete holds these column rows
            # across its cascade (#1602). A form that waited on one finds its
            # target gone once the lock is granted: refuse it as a form error
            # rather than save into a deleted column or swimlane and fail the
            # card's deferred foreign-key check at COMMIT (a 500). A swimlane
            # or board delete holds every column of the board, so the swimlane
            # read here sees one that was deleted.
            if column.pk not in locked_columns:
                raise ValidationError({"column": "This column was deleted. Choose another."})
            if cell_entry and swimlane_id is not None:
                try:
                    require_swimlane_exists(swimlane_id)
                except SwimlaneNotFound:
                    raise ValidationError(
                        {"swimlane": "This swimlane was deleted. Choose another."}
                    ) from None
        if not enforce:
            # An archived card does not count toward a column's limits, and an
            # edit that neither enters a column nor grows a card is unchecked.
            return cleaned
        try:
            enforce_column_limits(
                board=board, column=column,
                card=SimpleNamespace(pk=original.pk, weight=weight),
                role=SITE_ADMIN, force=False,
                # A weight change inside the same column does not change its
                # card count, so it is checked against the weight limit only —
                # same rule as the PATCH path.
                check_wip=entering,
                # Locked above, after the card row.
                lock=False,
            )
        except (WipHardBlocked, WipLimitExceeded, WeightLimitExceeded) as exc:
            raise ValidationError(_limit_error_message(exc)) from None
        return cleaned


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    """Admin for Card that broadcasts mutations to connected WebSocket clients.

    Mutations made through the admin site bypass the REST viewset and its
    perform_create/perform_update/perform_destroy hooks. Without this override
    any client viewing the affected board would not see admin edits until a
    full reload. Broadcast is deferred via transaction.on_commit() so clients
    only see events after the DB write is durably committed.

    WIP, weight and ``allow_card_creation`` are enforced by ``CardAdminForm``
    (#1428); the version bump, ``CardMovement`` audit row and hooks are still
    skipped (see ``docs/architecture/service-layer.md``).
    """

    form = CardAdminForm

    def save_model(self, request, obj, form, change):
        with transaction.atomic():
            self._lock_cell_entry(obj, change)
            super().save_model(request, obj, form, change)
        board = obj.board
        event = _broadcast.EVT_CARD_UPDATED if change else _broadcast.EVT_CARD_CREATED
        card_data = _refetched_card_data_or_none(obj, request, board)
        if card_data is None:
            return
        board_id = board.id
        _broadcast.record_board_event(board_id, event, card_data, actor_id=request.user.pk)

    @staticmethod
    def _lock_cell_entry(obj, change):
        """Re-take the cell-entry locks in the transaction that saves the card.

        In the admin this is a no-op: ``CardAdminForm.clean`` runs inside the
        same ``changeform_view`` transaction and already holds the card row
        and the same columns, decided on the same locked state. It is defense
        in depth for a save driven some other way (e.g. a form validated in a
        different transaction): it guarantees only that, when the stored cell
        differs from ``obj``'s, the card row and then its source and target
        column rows are held from before the card UPDATE until this
        transaction commits — which is what ``move_card``'s membership
        re-check (#1567) relies on. It does not re-run the limit check.
        """
        if not change:
            lock_card_cell_entry(card_id=None, column_ids={obj.column_id})
            return
        stored = lock_card_row(obj.pk)
        if stored is not None and (stored.column_id, stored.swimlane_id) != (
            obj.column_id, obj.swimlane_id,
        ):
            lock_card_cell_entry(card_id=None, column_ids={stored.column_id, obj.column_id})

    def delete_model(self, request, obj):
        board_id = obj.board_id
        card_uid = obj.uid
        # Lock the card before the collector reads its child rows, in one
        # transaction held across the delete (#1603; see lock_cards_for_cascade).
        with transaction.atomic():
            lock_cards_for_cascade(Card.objects.filter(pk=obj.pk))
            super().delete_model(request, obj)
            _broadcast.record_board_event(
                board_id, _broadcast.EVT_CARD_DELETED, {"card_uid": card_uid}, actor_id=request.user.pk
            )

    def delete_queryset(self, request, queryset):
        # One outer atomic: a bulk action is not wrapped by Django, so a lock
        # taken per object would not be held across the delete (#1603).
        with transaction.atomic():
            lock_cards_for_cascade(Card.objects.filter(pk__in=queryset.values("pk")))
            # Capture board_id + uid before delete — post-delete the instances are gone.
            deleted = [(c.board_id, c.uid) for c in queryset]
            super().delete_queryset(request, queryset)
            for board_id, card_uid in deleted:
                _broadcast.record_board_event(
                    board_id, _broadcast.EVT_CARD_DELETED, {"card_uid": card_uid}, actor_id=request.user.pk
                )


@admin.register(Column)
class ColumnAdmin(ContainerChangedAdminMixin, admin.ModelAdmin):
    """Admin for Column that broadcasts mutations (see CardAdmin for rationale)."""

    def save_model(self, request, obj, form, change):
        # Lock the (target) board row first, as ColumnViewSet.perform_create
        # does (#1602): a swimlane or board delete holds it across its
        # cascade, so no column can appear in the board — added here, or
        # re-pointed at it by an edit of its ``board`` — after the delete has
        # snapshotted the columns a card could enter through.
        # changeform_view's transaction holds the lock until the save commits.
        _lock_board_for_admin_save(obj.board_id, Column, obj.pk if change else None)
        super().save_model(request, obj, form, change)
        event = _broadcast.EVT_COLUMN_UPDATED if change else _broadcast.EVT_COLUMN_CREATED
        column_data = ColumnSerializer(obj).data
        board_id = obj.board_id
        _broadcast.record_board_event(board_id, event, column_data, actor_id=request.user.pk)

    def delete_model(self, request, obj):
        board_id = obj.board_id
        column_uid = obj.uid
        # Cards first, then the column row (#1603, #1602; see
        # lock_cards_for_cascade): the column lock keeps a card created or
        # moved into it from committing mid-cascade.
        with transaction.atomic():
            lock_cards_for_cascade(
                Card.objects.filter(column_id=obj.pk),
                entry_columns=Column.objects.filter(pk=obj.pk),
                boards=Board.objects.filter(pk=obj.board_id),
            )
            _require_present_for_admin(
                Board.objects.filter(pk=obj.board_id), Column.objects.filter(pk=obj.pk)
            )
            super().delete_model(request, obj)
            _broadcast.record_board_event(
                board_id, _broadcast.EVT_COLUMN_DELETED, {"column_uid": column_uid}, actor_id=request.user.pk
            )

    def delete_queryset(self, request, queryset):
        # One outer atomic across lock + delete (#1603).
        with transaction.atomic():
            lock_cards_for_cascade(
                Card.objects.filter(column__in=queryset.values("pk")),
                entry_columns=Column.objects.filter(pk__in=queryset.values("pk")),
                boards=Board.objects.filter(pk__in=queryset.values("board_id")),
            )
            deleted = [(c.board_id, c.uid) for c in queryset]
            super().delete_queryset(request, queryset)
            for board_id, column_uid in deleted:
                _broadcast.record_board_event(
                    board_id, _broadcast.EVT_COLUMN_DELETED, {"column_uid": column_uid}, actor_id=request.user.pk
                )


class _BoardEventAdminMixin:
    """Shared save/delete broadcast wiring for board-scoped admin models.

    Mutations made through the admin site bypass the REST viewset and its
    perform_create/perform_update/perform_destroy hooks (see CardAdmin above
    for the full rationale). CardAdmin and ColumnAdmin each hand-roll the same
    persist-then-broadcast shape; this mixin factors it out for the other
    board-scoped models the 1.2 pre-release broadcast-check audit flagged as
    silently bypassing it (#1241) — Swimlane, Label, BoardMembership. Each
    concrete admin class below only declares its event names and how to
    build a payload.

    Board is deliberately NOT one of these — its REST viewset dual-broadcasts
    to both the board channel and the board's group channel (a second
    audience the other three models don't have), so BoardAdmin implements its
    own save_model/delete_model/delete_queryset via
    _record_board_and_group_event() above instead of this single-channel
    mixin. Do not fold Board back into this mixin without also adding a
    group-channel branch generically — Swimlane/Label/BoardMembership's own
    REST viewsets never call broadcast_group_event, so a generic group
    broadcast here would be wrong for them.

    Subclasses set ``event_created``/``event_updated``/``event_deleted`` and
    implement ``_board_id``, ``_payload`` (used for both create and update),
    and ``_delete_payload``. ``_delete_payload`` is always called *before* the
    row is actually deleted — ``instance.delete()`` clears the instance's pk,
    the same reason CardAdmin/ColumnAdmin capture their uid up front rather
    than after ``super().delete_model()``.
    """

    event_created = None
    event_updated = None
    event_deleted = None

    def _board_id(self, obj):
        raise NotImplementedError

    def _payload(self, obj, request):
        raise NotImplementedError

    def _delete_payload(self, obj):
        raise NotImplementedError

    def _emit(self, board_id, event, payload, request):
        # `event` arrives as a plain parameter rather than a literal EVT_*
        # expression — the concrete constant lives on the subclass
        # (event_created/event_updated/event_deleted), and the
        # ws-event-reachability gate can only trace a registry constant
        # written directly in the calling function's own body, not through
        # an instance-attribute lookup. This one-line forwarder gets the same
        # pass-through treatment the gate already gives
        # _record_board_and_group_event() above; the events themselves are
        # independently declared/emitted/documented via each model's REST
        # viewset.
        _broadcast.record_board_event(board_id, event, payload, actor_id=request.user.pk)

    def save_model(self, request, obj, form, change):
        # Board row, then (on change) the row itself (#1602; see
        # _lock_board_for_admin_save).
        _lock_board_for_admin_save(
            self._board_id(obj), type(obj), obj.pk if change else None,
        )
        super().save_model(request, obj, form, change)
        event = self.event_updated if change else self.event_created
        self._emit(self._board_id(obj), event, self._payload(obj, request), request)

    def _cards_cascaded_by(self, queryset):
        """Cards a delete of ``queryset`` cascades to, or ``None`` when the
        model has no card cascade (Label, BoardMembership). Swimlane overrides
        it so its delete takes the card-row lock first (#1603)."""
        return None

    def _entry_columns_for(self, queryset):
        """Column rows whose lock keeps a card from entering what ``queryset``
        cascades to (#1602); only meaningful with ``_cards_cascaded_by``."""
        return None

    def _boards_for(self, queryset):
        """Board rows a cascading delete of ``queryset`` locks first (#1602)."""
        return None

    def _lock_cascade(self, queryset):
        cards = self._cards_cascaded_by(queryset)
        if cards is not None:
            lock_cards_for_cascade(
                cards, entry_columns=self._entry_columns_for(queryset),
                boards=self._boards_for(queryset),
            )

    def delete_model(self, request, obj):
        board_id = self._board_id(obj)
        payload = self._delete_payload(obj)
        with transaction.atomic():
            qs = type(obj).objects.filter(pk=obj.pk)
            if self._cards_cascaded_by(qs) is None:
                # No card cascade (label, membership): take the board row and
                # the row itself, so a delete queued behind another delete of
                # it answers 404 rather than recording a second event.
                _lock_board_for_admin_save(board_id, type(obj), obj.pk)
            else:
                self._lock_cascade(qs)
                _require_present_for_admin(Board.objects.filter(pk=board_id), qs)
            super().delete_model(request, obj)
            self._emit(board_id, self.event_deleted, payload, request)

    def delete_queryset(self, request, queryset):
        # One outer atomic across lock + delete: a bulk action is not wrapped
        # by Django, so per-object locks would not be held (#1603).
        with transaction.atomic():
            self._lock_cascade(queryset)
            # Capture identifiers before delete — post-delete each instance's pk is cleared.
            deleted = [(self._board_id(o), self._delete_payload(o)) for o in queryset]
            super().delete_queryset(request, queryset)
            for board_id, payload in deleted:
                self._emit(board_id, self.event_deleted, payload, request)


@admin.register(Swimlane)
class SwimlaneAdmin(ContainerChangedAdminMixin, _BoardEventAdminMixin, admin.ModelAdmin):
    """Admin for Swimlane that broadcasts mutations (see CardAdmin for rationale)."""

    event_created = _broadcast.EVT_SWIMLANE_CREATED
    event_updated = _broadcast.EVT_SWIMLANE_UPDATED
    event_deleted = _broadcast.EVT_SWIMLANE_DELETED

    def _board_id(self, obj):
        return obj.board_id

    def _payload(self, obj, request):
        # SwimlaneSerializer, never SwimlaneAdminSerializer: every swimlane
        # broadcast must go through the public serializer so contact_email
        # and notes (admin-only PII) never reach viewer-role WebSocket
        # subscribers — the same rule SwimlaneViewSet follows (see
        # SwimlaneSerializer's docstring in serializers.py).
        return SwimlaneSerializer(obj).data

    def _delete_payload(self, obj):
        return {"swimlane_uid": obj.uid}

    def _cards_cascaded_by(self, queryset):
        return Card.objects.filter(swimlane__in=queryset.values("pk"))

    def _entry_columns_for(self, queryset):
        # A card enters a swimlane through any column of its board, and every
        # such writer holds that column's row (#1602).
        return Column.objects.filter(board__in=queryset.values("board_id"))

    def _boards_for(self, queryset):
        return Board.objects.filter(pk__in=queryset.values("board_id"))


@admin.register(Label)
class LabelAdmin(_BoardEventAdminMixin, admin.ModelAdmin):
    """Admin for Label that broadcasts mutations (see CardAdmin for rationale)."""

    event_created = _broadcast.EVT_LABEL_CREATED
    event_updated = _broadcast.EVT_LABEL_UPDATED
    event_deleted = _broadcast.EVT_LABEL_DELETED

    def _board_id(self, obj):
        return obj.board_id

    def _payload(self, obj, request):
        return LabelSerializer(obj).data

    def _delete_payload(self, obj):
        return {"label_uid": obj.uid}


@admin.register(Board)
class BoardAdmin(ContainerChangedAdminMixin, admin.ModelAdmin):
    """Admin for Board that broadcasts mutations to both the board channel and
    the board's group channel.

    Board is the one board-scoped model in this file with a second broadcast
    audience: GroupDetail.tsx's dashboard/boards-list view listens for
    board.created/board.updated/board.deleted exclusively on the group socket
    (useGroupSocket), not the board socket — mirroring
    BoardViewSet.perform_create/perform_update/destroy's dual on_commit
    broadcast in boards/views/boards.py. A board created, renamed, or deleted
    via /admin/ must still reach that view without a manual reload, so this
    class does not use _BoardEventAdminMixin's single-channel pattern —
    Swimlane/Label/BoardMembership have no group-channel counterpart (their
    REST viewsets never call broadcast_group_event either) and stay on that
    mixin below.
    """

    def save_model(self, request, obj, form, change):
        if change:
            # Board row first (#1602; see _lock_board_for_admin_save).
            _lock_board_for_admin_save(obj.pk)
            if form is not None:
                # BoardAdmin.save_model re-reads fields that are not on the
                # form from the locked row (and form fields the administrator
                # did not change), so a concurrent change to them is not
                # reverted by the earlier read held in ``obj``. The share-link
                # fields are editable=False, so they are never on the form.
                stored = Board.objects.get(pk=obj.pk)
                changed = (
                    set(form.changed_data) if getattr(form, "is_bound", False) else None
                )
                for field in Board._meta.concrete_fields:
                    if field.name not in form.fields or (
                        changed is not None and field.name not in changed
                    ):
                        setattr(obj, field.attname, getattr(stored, field.attname))
        super().save_model(request, obj, form, change)
        event = _broadcast.EVT_BOARD_UPDATED if change else _broadcast.EVT_BOARD_CREATED
        # No request in context: BoardSerializer.get_group_detail() reads
        # request.query_params (a DRF Request attribute) to resolve
        # ?expand=group, which the plain Django admin HttpRequest does not
        # have — passing it through would raise AttributeError. Omitting
        # context falls back to is_starred=False / group_detail=None, both
        # of which are documented fallbacks on a bare instance, not a crash.
        board_data = BoardSerializer(obj).data
        _record_board_and_group_event(
            obj.id, obj.group_id, event, board_data, actor_id=request.user.pk
        )

    def delete_model(self, request, obj):
        board_id = obj.id
        board_uid = obj.uid
        group_id = obj.group_id
        # Cards first, then the board's columns, then the board (#1603,
        # #1602; see lock_cards_for_cascade).
        with transaction.atomic():
            lock_cards_for_cascade(
                Card.objects.filter(board_id=obj.pk),
                entry_columns=Column.objects.filter(board_id=obj.pk),
                boards=Board.objects.filter(pk=obj.pk),
            )
            _require_present_for_admin(Board.objects.filter(pk=obj.pk))
            super().delete_model(request, obj)
            _record_board_and_group_event(
                board_id, group_id, _broadcast.EVT_BOARD_DELETED, {"board_uid": board_uid},
                actor_id=request.user.pk,
            )

    def delete_queryset(self, request, queryset):
        # One outer atomic across lock + delete (#1603).
        with transaction.atomic():
            lock_cards_for_cascade(
                Card.objects.filter(board__in=queryset.values("pk")),
                entry_columns=Column.objects.filter(board__in=queryset.values("pk")),
                boards=Board.objects.filter(pk__in=queryset.values("pk")),
            )
            # Capture identifiers before delete — post-delete each instance's pk is cleared.
            deleted = [(b.id, b.uid, b.group_id) for b in queryset]
            super().delete_queryset(request, queryset)
            for board_id, board_uid, group_id in deleted:
                _record_board_and_group_event(
                    board_id, group_id, _broadcast.EVT_BOARD_DELETED, {"board_uid": board_uid},
                    actor_id=request.user.pk,
                )


@admin.register(BoardMembership)
class BoardMembershipAdmin(_BoardEventAdminMixin, admin.ModelAdmin):
    """Admin for BoardMembership that broadcasts mutations (see CardAdmin for rationale)."""

    event_created = _broadcast.EVT_MEMBER_ADDED
    event_updated = _broadcast.EVT_MEMBER_UPDATED
    event_deleted = _broadcast.EVT_MEMBER_REMOVED

    def _board_id(self, obj):
        return obj.board_id

    def _payload(self, obj, request):
        return BoardMembershipSerializer(obj, context={"board": obj.board, "request": request}).data

    def _delete_payload(self, obj):
        return {"user_id": obj.user_id}


def _refetched_card_data_or_none(card, request, board):
    """``_refetched_card_data``, or ``None`` when the card was deleted meanwhile.

    The helper raises a DRF ``NotFound`` (#1584), which the Django admin does
    not translate — it would surface as a 500 (#1585). In the admin the write
    the caller just made has already been applied and there is no response body
    to carry a 404, so a vanished card simply has nothing to broadcast: the
    ``card.deleted`` frame from whoever deleted it already told clients.
    """
    try:
        return _refetched_card_data(card, request, board)
    except NotFound:
        return None


class _CardChildBroadcastAdminMixin:
    """Shared broadcast wiring for admin models that hang off a Card.

    CardComment and CardChecklist have no WebSocket event of their own — the
    REST endpoints for comments and checklist items (see
    ``boards/views/cards.py``'s ``comments``/``checklist``/``checklist_item``
    actions) always re-broadcast the *card's* full ``card.updated`` payload
    rather than a comment/checklist-specific event, because the frontend
    renders both inline on the open card panel. This mixin matches that: any
    admin mutation re-broadcasts a freshly refetched ``card.updated`` for the
    parent card (see CardAdmin above for why admin needs this at all).
    """

    def save_model(self, request, obj, form, change):
        # Lock the parent card row for the rest of the transaction, as the REST
        # comment/checklist endpoints do (#1587). The child's card foreign key is
        # DEFERRABLE INITIALLY DEFERRED on PostgreSQL, so without the lock a card
        # delete committing before this transaction's COMMIT fails the FK check
        # there, as a 500. A card that is already gone answers 404 instead; the
        # admin's changeform transaction rolls the unsaved row back.
        with transaction.atomic():
            try:
                lock_card_for_write(obj.card_id)
            except CardNotFound:
                raise Http404("Card not found.") from None
            super().save_model(request, obj, form, change)
        card = Card.objects.select_related("board").filter(pk=obj.card_id).first()
        if card is not None:
            self._broadcast_card(request, card)

    @staticmethod
    def _broadcast_card(request, card):
        """Re-broadcast ``card.updated``, or nothing if the card has vanished."""
        card_data = _refetched_card_data_or_none(card, request, card.board)
        if card_data is not None:
            _broadcast.record_board_event(
                card.board_id, _broadcast.EVT_CARD_UPDATED, card_data, actor_id=request.user.pk
            )

    def delete_model(self, request, obj):
        # Capture the card before deleting obj. It is looked up by id rather
        # than through ``obj.card``, whose lazy FK read raises if the card was
        # deleted since the admin loaded ``obj`` (#1585); a vanished card has
        # nothing to broadcast. The refetch happens after the delete so the
        # broadcast reflects the item's removal, same ordering CardViewSet's
        # comment/checklist endpoints use.
        card = Card.objects.select_related("board").filter(pk=obj.card_id).first()
        super().delete_model(request, obj)
        if card is not None:
            self._broadcast_card(request, card)

    def delete_queryset(self, request, queryset):
        # Dedupe by card: a bulk delete can select several rows on the same
        # card, and each card only needs one post-delete broadcast reflecting
        # its final state, not one per deleted row. Cards are looked up by id so
        # a card deleted since the admin loaded the rows is skipped, not a 500.
        card_ids = {o.card_id for o in queryset}
        cards = list(Card.objects.select_related("board").filter(pk__in=card_ids))
        super().delete_queryset(request, queryset)
        # Refetch every card first, then record: a card deleted part-way
        # through is skipped, instead of leaving the earlier cards' events
        # emitted and the later ones' never emitted (#1585).
        refetched = [
            (card, _refetched_card_data_or_none(card, request, card.board))
            for card in cards
        ]
        for card, card_data in refetched:
            if card_data is None:
                continue
            _broadcast.record_board_event(
                card.board_id, _broadcast.EVT_CARD_UPDATED, card_data, actor_id=request.user.pk
            )


@admin.register(CardComment)
class CardCommentAdmin(_CardChildBroadcastAdminMixin, admin.ModelAdmin):
    """Admin for CardComment that broadcasts mutations (see CardAdmin for rationale)."""


@admin.register(CardChecklist)
class CardChecklistAdmin(_CardChildBroadcastAdminMixin, admin.ModelAdmin):
    """Admin for CardChecklist that broadcasts mutations (see CardAdmin for rationale)."""


# CardMovement and CardActivity are append-only audit-trail rows (no update
# path, and deletion is not part of any documented workflow) — the #1241
# audit flagged them as the same gap in principle but lower priority than the
# six models above. CardAttachment and Notification were not flagged by the
# audit. All four remain plain registrations; broadcasting admin mutations to
# them can follow the same _BoardEventAdminMixin/_CardChildBroadcastAdminMixin
# pattern if a future audit asks for it.
admin.site.register(CardMovement)
admin.site.register(CardActivity)
admin.site.register(CardAttachment)
admin.site.register(Notification)
