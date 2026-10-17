/**
 * Attribution text for movement and activity rows (#1675).
 *
 * A row created by a board import with history belongs to the user who ran
 * the import; `imported_from_username` keeps the username the import file
 * named (`""` when it named nobody, `null` for a row that was not imported).
 * The source name is shown as plain text, never resolved to a local account,
 * so a reader can tell an imported row from one recorded on this board.
 */

/** "imported, originally alice" / "imported", or null for a row that was not imported. */
export function importedNote(importedFrom: string | null | undefined): string | null {
  if (importedFrom === null || importedFrom === undefined) return null;
  return importedFrom ? `imported, originally ${importedFrom}` : "imported";
}

/**
 * The "by …" line of a timeline row: `by Kelly`, or for an imported row
 * `by Kelly · imported, originally alice`. Null when there is nothing to show.
 */
export function historyAttribution(
  actor: string | null,
  importedFrom: string | null | undefined,
): string | null {
  const note = importedNote(importedFrom);
  const by = actor ? `by ${actor}` : null;
  if (!note) return by;
  return by ? `${by} · ${note}` : note;
}
