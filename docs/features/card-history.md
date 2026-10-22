# Card History

Every card keeps a full audit trail of movements and field changes. **Why it matters:** you can see who moved a card and when — the record incident retros and audits need.

## Movement history

A `CardMovement` record captures every column or swimlane change. Each record includes:

- From column / to column (FK, nullable after deletion)
- From swimlane / to swimlane (FK, nullable after deletion)
- From column UID / to column UID — stable even after the column is deleted or renamed
- From swimlane UID / to swimlane UID — stable even after the swimlane is deleted or renamed
- Denormalized names at the time of the move (from_column_name, to_column_name, etc.)
- Who moved it (`moved_by`)
- When (`moved_at`) — indexed for query performance
- Optional notes
- **Movement type** (`movement_type`) — one of `move` (standard drag-and-drop or API move), `archived` (card was archived), or `unarchived` (card was unarchived from the Archived panel)

Pure position reorders within the same cell don't create a movement record.

`archived` and `unarchived` events appear in the card's own timeline and in the board-level History view.

Find the movement timeline in the card detail panel's **Activity** tab.

## Deleted cards keep their history

> **Changed in 1.2** (#1469)

Movement records are append-only: once written, they are not edited, and they are removed only with their board or by a superuser's `purge_card_history` run (since 1.2; see [Card History Retention](../administration/card-history-retention.md)).

- **Deleting a card keeps its movements.** This covers deleting the card itself and deleting the column or swimlane it is in. Each record keeps the card's UID and title (`card_uid`, `card_title`) as of when it was recorded (for movements from before 1.2 and for imported ones, as of the upgrade or import), so the board's **History** view still lists it under the original card. A deleted card no longer appears under the **Swimlane** filter, which filters by the card's current swimlane.
- **Each movement keeps the title recorded with it.** While a card exists, its history is listed under its current title. Once it is deleted, each movement is listed under the title the card had at that move, so a card renamed before it was deleted appears under its earlier titles too.
- **Deleting the board removes all of its movements**, including those of cards deleted earlier. Apart from that, since 1.2 only a superuser can remove them, with the `purge_card_history` command (see [Card History Retention](../administration/card-history-retention.md)).
- **Metrics are unchanged.** The Summary and Analytics views and the stale-card check count only cards that still exist, as before. A deleted card's movements do not count toward throughput, cycle time or dwell time.
- **Exports don't include them yet.** JSON and CSV board export write movements per card, so a deleted card's history is not exported (#1796).

In the Django admin (`/admin/`), movement records can be viewed but not added, edited or deleted; earlier releases allowed edits and deletes there. The admin list shows, and searches, the card UID and the card's title **as recorded at the time of each move**, not a renamed card's current title. Moving a card to another board in the admin keeps its movement history attached to the card's current board.

A deleted card's title and any movement notes stay visible to the board's members until the board is deleted or a superuser purges them on request (since 1.2, #1797). [Card History Retention](../administration/card-history-retention.md) covers the purge command and the other places a deleted card's title can remain.

Before 1.2, deleting a card also deleted its movement history.

## Imported history

> **Changed in 1.2** (#1675)

A board created by a [JSON import](board.md#import) with **Card history** included brings the file's movements and activity entries with it. Each imported entry:

- keeps the time the file gives it, so the timeline reads in its original order;
- is recorded under the user who ran the import (`moved_by` / `actor`);
- keeps the username the file named as plain text in `imported_from_username`. It is shown next to the importer's name — "by Kelly · imported, originally alice" in the card's **Activity** tab, and "imported, originally alice" under the mover in the board's **History** view. When the file named nobody, the entry just says "imported".

The source name is never matched to an account here: imported entries are shown under the importer's name. Entries recorded on the board after the import have `imported_from_username: null` and show no note. History imported by earlier versions also carries `null`, since the original name was not kept.

Imported comments work the same way (since 1.2, #1702): a comment created by a JSON or [Trello import](trello-import.md#comment-authors) is posted under the importer, keeps its original time, and keeps the source's author name in `imported_from_username`, shown after the time in the card's comment list ("imported, originally alice"). Comments posted on the board carry `null`.

Exports carry the field too: JSON export writes `imported_from_username` on every movement, activity, and comment, and re-importing such a file keeps the original source name (not the name of whoever imported it first). CSV export appends the note to the mover in **Movement History**, for example `…|Kelly (imported, originally alice)`.

## Activity log

The activity log also tracks field changes:

| Event | Recorded |
|---|---|
| Title change | Old and new title |
| Priority change | Old and new priority |
| Weight change | Old and new weight |
| Assignee change | Old and new assignee name |
| Description change | Flagged (content not stored) |
| Label change | Labels added (+) and removed (-) |
| Comment added | Flagged |
| Attachment added / deleted | Flagged |
| Checklist item added / checked / unchecked / deleted | Item text |
| Due date change | Old and new date (or "none" if cleared) |

!!! note
    Due date change tracking was added in 1.0.

## Viewing history

Open a card and click **Activity** in the side panel. The timeline shows movements and field changes in reverse chronological order. Each entry shows:

- What changed (from → to)
- Who made the change
- When it happened (relative time, e.g. "3 days ago")

Movement entries keep the full column and swimlane names, even after those columns or swimlanes are renamed or deleted. A deleted column's name shows in italic red with a **Deleted —** prefix and a hover tooltip, so you can tell live columns from removed ones at a glance.

### Duration between entries

Each movement entry shows how long the card stayed in that column before the next move — "Spent 3h here" or "Spent 2d here" on the right side of the entry. Durations come from the gap between consecutive movement timestamps, shown in the most readable unit (minutes, hours, or days). The last entry has no duration label, because the card is still there.

### Checklist event grouping

Sequential checklist events of the same type collapse into one grouped entry instead of one row per item — for example, "Added checklist items: "item a", "item b", "item c"". This keeps the timeline compact when bulk checklist edits would otherwise dominate it.

The UID fields on each movement record (`from_column_uid`, `to_column_uid`, `from_swimlane_uid`, `to_swimlane_uid`) are also preserved permanently. Use them, not the name or ID fields, to correlate movement history in an external system. See [Stable UIDs](stable-uids.md).

## How dwell time is calculated

Dwell time is the gap between two consecutive `CardMovement` records. The Analytics view uses these gaps to compute median dwell time per stage and flag outliers. See [Analytics](analytics.md).

## Stale card detection

A card is stale if its most recent `CardMovement` is older than the board's `staleness_threshold_days` (default: 7 days) — it gets an amber tint overlay with reduced opacity in the board view. See [Notifications](notifications.md).

## API access

Movement history and the activity log are accessible via the API:

```
GET /api/v1/boards/{board_id}/cards/{card_id}/movements/
GET /api/v1/boards/{board_id}/cards/{card_id}/activities/
```

Movement records include `card_uid`, `card_title`, `from_column_name`, `to_column_name`, `from_swimlane_name`, `to_swimlane_name`, `from_column_uid`, `to_column_uid`, `from_swimlane_uid`, `to_swimlane_uid`, `moved_by`, `moved_at`, and `movement_type`. The column and swimlane name and UID fields are denormalized — captured at write time, accurate regardless of future renames or deletions. `card_uid` and `card_title` show the card's current values while the card exists, and the values kept on the record once it is deleted. See [Stable UIDs](stable-uids.md) for the full field reference and integration examples.

## Board-level movement history

> **Added in 1.0**

The board toolbar's **History** tab shows movements across every card on the board, alongside the per-card timeline. See [Board & Cards — History view](board.md#history-view) for the UI description.

### API

```
GET /api/v1/boards/{id}/movements/
```

| Parameter | Type | Description |
|---|---|---|
| `swimlane_id` | integer | Filter by the card's current swimlane |
| `to_column_id` | integer | Filter by destination column |
| `moved_by_id` | integer | Filter by the user who performed the move |
| `moved_after` | ISO date | Lower bound (inclusive) |
| `moved_before` | ISO date | Upper bound (inclusive) |
| `exclude_type` | comma-separated string | Exclude movement types — e.g. `archived,unarchived` |
| `offset` | integer | Pagination offset (default `0`) |

Page size is fixed at 50. With no date range, the endpoint returns the full movement history. The API returns all movement types, including archive/unarchive, unless you pass `exclude_type=archived,unarchived`. The History view UI doesn't send that parameter, so it shows everything.
