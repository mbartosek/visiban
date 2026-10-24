"""Public share link lifetime tied to the admin who enabled it (#1865).

``Board.share_enabled_by`` records the account that enabled the board's current
public share link. The link lasts only while that account keeps the standing
that let it enable sharing on that board: an active account whose effective
role there is board admin or site admin (the same check ``BoardViewSet.share``
applies). Standing is always re-derived from current state with
``get_board_role`` / ``get_board_roles``, so an account that is still an admin
another way (owner, a group admin role, all-content access) keeps the link.

Two halves, the same shape invite links use for their creator:

- ``share_enabler_has_standing`` is evaluated by the public view on every read,
  so a link stops answering the moment its enabler's standing lapses, whichever
  route (or race) caused it.
- ``revoke_lapsed_share_links`` clears the link on the board itself, called in
  the same transaction as the write that removed the standing, so the board's
  Sharing tab shows sharing as off and a later return of the standing does not
  bring the old link back.

Links whose ``share_enabled_by`` is NULL (enabled before the field existed)
are left alone by both.
"""

from django.contrib.auth import get_user_model

from ..models import Board, BoardMembership

#: Effective roles that may enable sharing, and so keep a link alive.
SHARE_ADMIN_ROLES = frozenset({BoardMembership.Role.ADMIN, "site_admin"})


def share_enabler_has_standing(user, board):
    """True while *user* may still hold the public share link on *board*.

    *board* should be loaded with ``GROUP_ANCESTOR_SELECT_RELATED`` so the
    group walk in ``get_board_role`` reads from the ORM cache.
    """
    from ..permissions import get_board_role

    if user is None or not user.is_active:
        return False
    return get_board_role(user, board) in SHARE_ADMIN_ROLES


def revoke_lapsed_share_links(*, enabler=None, board_ids=None, actor_id=None):
    """Clear the share links whose enabler no longer has standing on the board.

    Scope the check with *enabler* (a user or user pk: every board whose link
    that account enabled), *board_ids* (those boards, whoever enabled them), or
    both. Call it inside the transaction that made the access change, after the
    change is written, so the standing is judged on the new state and the
    revocation commits or rolls back with it. Returns the ids of the boards
    whose link was cleared, in pk order.

    Each clear is a conditional UPDATE that matches only while the board still
    carries the link that was judged (same enabler, same token), so a link an
    admin re-enabled meanwhile is never cleared by mistake. Each cleared board
    records ``board.updated`` (persisted, published on commit) so open board
    settings refresh, the same event the share action records.
    """
    from .. import broadcast as _broadcast
    from ..permissions import GROUP_ANCESTOR_SELECT_RELATED, get_board_roles
    from ..serializers import BoardSerializer

    if enabler is None and board_ids is None:
        raise ValueError("revoke_lapsed_share_links needs an enabler or board_ids")

    qs = Board.objects.filter(share_enabled_by__isnull=False, share_token__isnull=False)
    if enabler is not None:
        qs = qs.filter(share_enabled_by=getattr(enabler, "pk", enabler))
    if board_ids is not None:
        board_ids = sorted({bid for bid in board_ids if bid is not None})
        if not board_ids:
            return []
        qs = qs.filter(pk=board_ids[0]) if len(board_ids) == 1 else qs.filter(pk__in=board_ids)
    boards = list(qs.select_related(GROUP_ANCESTOR_SELECT_RELATED).order_by("pk"))
    if not boards:
        return []

    by_enabler = {}
    for board in boards:
        by_enabler.setdefault(board.share_enabled_by_id, []).append(board)
    # A fresh read of each enabler: the caller's instance may predate the
    # change it just wrote (a flag cleared through update(), for instance).
    users = get_user_model().objects.in_bulk(list(by_enabler))

    lapsed = []
    for user_id, user_boards in by_enabler.items():
        user = users.get(user_id)
        if user is None or not user.is_active:
            lapsed.extend(user_boards)
            continue
        roles = get_board_roles(user, user_boards)
        lapsed.extend(b for b in user_boards if roles.get(b.pk) not in SHARE_ADMIN_ROLES)

    revoked = []
    for board in sorted(lapsed, key=lambda b: b.pk):
        updated = Board.objects.filter(
            pk=board.pk,
            share_enabled_by_id=board.share_enabled_by_id,
            share_token=board.share_token,
        ).update(share_token=None, share_token_expires_at=None, share_enabled_by=None)
        if not updated:
            continue
        board.share_token = None
        board.share_token_expires_at = None
        board.share_enabled_by = None
        revoked.append(board.pk)
        _broadcast.record_board_event(
            board.pk, _broadcast.EVT_BOARD_UPDATED, BoardSerializer(board).data,
            actor_id=actor_id,
        )
    return revoked
