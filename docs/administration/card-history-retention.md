# Card History Retention

> **Added in 1.2** (#1797)

Since 1.2, deleting a card keeps its movement history (#1469). This page covers what that history keeps, who can see it, how long it lasts, and how a superuser erases it on request with the `purge_card_history` management command.

## What a deleted card leaves behind

Each movement record of a deleted card keeps:

- the card's UID, and the card's title **as it was at the time of that move** (`card_uid`, `card_title`);
- the movement's notes, the columns and swimlanes involved, who moved it and when.

While the card exists, its history is shown under its current title. Once it is deleted, each movement is shown under the title recorded with it, so a card that was renamed before deletion appears in the board's **History** view under each of its earlier titles. This is intended: it is what the card was called when each move happened.

**Who can see it:** every member of the board, through the board's **History** view, and Django admin users with permission to view card movements (read-only).

**How long it lasts:** until the board is deleted, or until a superuser purges it with the command below. Nothing expires it on a schedule.

See [Card History](../features/card-history.md#deleted-cards-keep-their-history) for how deleted-card history appears to board members and how it affects metrics and exports.

### Other places a deleted card's title can remain

`purge_card_history` erases movement records only. A deleted card's title can also remain in:

- **The board [change feed](../api/events.md)** (`GET /api/v1/boards/{id}/events/`). Each event stores its broadcast payload, which can include the card's title. Events expire after the retention window that the [`prune_board_events`](scheduled-jobs.md) job enforces (30 days by default).
- **Board exports** downloaded before the card was deleted, and any copies made of them.
- **Database backups** taken before the purge.

## Purging retained history

> **Added in 1.2** (#1797)

```bash
# Dry run (the default): report how many records would be deleted
docker compose exec backend python manage.py purge_card_history \
    --actor admin --card 3f9a0c1d2e4b5a69

# Delete them
docker compose exec backend python manage.py purge_card_history \
    --actor admin --card 3f9a0c1d2e4b5a69 --yes
```

On Kubernetes, run the same command with `kubectl exec` in a backend pod.

| Option | Meaning |
|---|---|
| `--actor USERNAME` | Required. An active **superuser**; any other account is refused. Recorded as the actor of the action-log entry. |
| `--card CARD_UID` | Only the history of the card with this UID. |
| `--board BOARD` | Only history on this board, given as its numeric ID or its UID. |
| `--since DATE` | Only movements at or after this moment. |
| `--before DATE` | Only movements strictly before this moment. |
| `--include-live-cards` | Also delete matching history of cards that still exist. |
| `--yes` | Delete. Without it the command only reports what would be deleted. |

Dates are `YYYY-MM-DD` (midnight UTC at the start of that day) or an ISO 8601 datetime (UTC when it has no offset). `--since 2026-03-01 --before 2026-04-01` covers exactly March.

How the command behaves:

- **Selectors combine.** `--card`, `--board`, `--since` and `--before` all apply together. At least one is required; there is no form that purges everything.
- **Deleted cards only, by default.** Without `--include-live-cards`, only history of cards that no longer exist is matched, and naming a card that still exists with `--card` is refused. A live card's history feeds its own timeline and the board's metrics, so erasing it is a separate, explicit choice.
- **Dry run by default.** The dry run prints counts of records, cards and boards, never titles. Re-run with `--yes` to delete.
- **Recorded.** A purge that deletes anything writes one `card_history.purged` entry to the [admin action log](../api/admin.md#action-log) with source `cli`, the `--actor` account, the selector used and the number of records deleted. The entry never contains a purged title or note. A run that matches nothing writes no entry.
- **All or nothing.** The records are deleted in the same transaction that writes the log entry. If the entry cannot be written, nothing is deleted.

Finding the card UID: a deleted card's UID is shown in the Django admin's **Card movements** list (`/admin/boards/cardmovement/`), which can be searched by the recorded title.

!!! warning "This breaks the append-only history on purpose"
    Movement records are otherwise never edited or deleted outside a board delete. A purge removes them for good, and Board History, the card's audit trail and any later review cannot recover them. The action-log entry is the only record that the purge happened.

!!! note "`--actor` is accountability, not authentication"
    Anyone who can run `manage.py` already has direct access to the database and can name any account. Shell access is the real control; `--actor` records which superuser authorized the purge, so the action log can answer "who erased this?".
