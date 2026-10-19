/**
 * Parser for `extension_panels` from `GET /boards/{id}/summary/` (#1770).
 *
 * The array is produced by enterprise `ANALYTICS_EXTENSIONS` callables, so it
 * is untrusted: entries may be null, non-objects, missing fields or duplicated.
 * The parser returns normalized primitives only and skips (never truncates) any
 * panel that violates the contract in docs/api/boards.md "Extension panel
 * payload". Unknown fields are ignored at every level so optional fields can be
 * added later. A `null` optional field (description, hint, align) is treated as
 * absent, because Python producers often send None.
 */

export type PanelCell = string | number | null;

export interface StatsItem {
  label: string;
  value: PanelCell;
  hint?: string;
}

export interface TableColumn {
  key: string;
  label: string;
  align: "left" | "right";
}

interface PanelBase {
  id: string;
  title: string;
  description?: string;
}

export interface StatsPanel extends PanelBase {
  type: "stats";
  items: StatsItem[];
}

export interface TablePanel extends PanelBase {
  type: "table";
  columns: TableColumn[];
  rows: Record<string, PanelCell>[];
}

export type ExtensionPanel = StatsPanel | TablePanel;

export const LIMITS = {
  maxPanels: 20,
  idMax: 100,
  titleMax: 120,
  descriptionMax: 500,
  statsItemsMax: 24,
  labelMax: 80,
  valueMax: 200,
  hintMax: 200,
  columnsMax: 12,
  keyMax: 64,
  rowsMax: 200,
} as const;

const warned = new Set<string>();

/** Test helper: clears the warn-once memory. */
export function resetExtensionPanelWarnings(): void {
  warned.clear();
}

function warnSkipped(id: string | null, index: number, reason: string): void {
  const dedupKey = `${id ?? "#" + index}:${reason}`;
  if (warned.has(dedupKey)) return;
  warned.add(dedupKey);
  // Never log the title or data: they are untrusted and may be sensitive.
  console.warn(`[extension_panels] skipped panel ${id ?? "#" + index}: ${reason}`);
}

class Skip extends Error {}

function fail(reason: string): never {
  throw new Skip(reason);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function str(v: unknown, min: number, max: number, what: string): string {
  if (typeof v !== "string" || v.length < min || v.length > max) fail(`invalid ${what}`);
  return v;
}

function cell(v: unknown, what: string): PanelCell {
  if (v === null) return null;
  if (typeof v === "string") {
    if (v.length > LIMITS.valueMax) fail(`${what} too long`);
    return v;
  }
  if (typeof v === "number" && Number.isFinite(v)) return v;
  return fail(`invalid ${what}`);
}

function parseStats(base: PanelBase, data: Record<string, unknown>): StatsPanel {
  const raw = data.items;
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > LIMITS.statsItemsMax) {
    fail("invalid items");
  }
  const items = raw.map((it): StatsItem => {
    if (!isRecord(it)) fail("invalid item");
    const item: StatsItem = {
      label: str(it.label, 1, LIMITS.labelMax, "item label"),
      value: cell(it.value, "item value"),
    };
    if (it.hint !== undefined && it.hint !== null) item.hint = str(it.hint, 0, LIMITS.hintMax, "item hint");
    return item;
  });
  return { ...base, type: "stats", items };
}

function parseTable(base: PanelBase, data: Record<string, unknown>): TablePanel {
  const rawCols = data.columns;
  if (!Array.isArray(rawCols) || rawCols.length < 1 || rawCols.length > LIMITS.columnsMax) {
    fail("invalid columns");
  }
  const seen = new Set<string>();
  const columns = rawCols.map((c): TableColumn => {
    if (!isRecord(c)) fail("invalid column");
    const key = str(c.key, 1, LIMITS.keyMax, "column key");
    // Keys index plain row objects; these names would hit the prototype chain.
    if (key === "__proto__" || key === "constructor" || key === "prototype") fail("reserved column key");
    if (seen.has(key)) fail("duplicate column key");
    seen.add(key);
    const label = str(c.label, 1, LIMITS.labelMax, "column label");
    if (c.align !== undefined && c.align !== null && c.align !== "left" && c.align !== "right") fail("invalid align");
    return { key, label, align: c.align === "right" ? "right" : "left" };
  });
  const rawRows = data.rows;
  if (!Array.isArray(rawRows) || rawRows.length > LIMITS.rowsMax) fail("invalid rows");
  const rows = rawRows.map((r) => {
    if (!isRecord(r)) fail("invalid row");
    const out: Record<string, PanelCell> = {};
    for (const col of columns) {
      out[col.key] = Object.prototype.hasOwnProperty.call(r, col.key)
        ? cell(r[col.key], "cell")
        : null;
    }
    return out;
  });
  return { ...base, type: "table", columns, rows };
}

function parseOne(entry: unknown, seenIds: Set<string>): ExtensionPanel {
  if (!isRecord(entry)) fail("not an object");
  const id = str(entry.id, 1, LIMITS.idMax, "id");
  if (seenIds.has(id)) fail("duplicate id");
  
  const title = str(entry.title, 1, LIMITS.titleMax, "title");
  const base: PanelBase = { id, title };
  if (entry.description !== undefined && entry.description !== null) {
    base.description = str(entry.description, 0, LIMITS.descriptionMax, "description");
  }
  if (entry.type !== "stats" && entry.type !== "table") fail("unsupported type");
  if (!isRecord(entry.data)) fail("invalid data");
  const panel =
    entry.type === "stats" ? parseStats(base, entry.data) : parseTable(base, entry.data);
  seenIds.add(id);
  return panel;
}

export function parseExtensionPanels(raw: unknown): ExtensionPanel[] {
  if (!Array.isArray(raw)) return [];
  const out: ExtensionPanel[] = [];
  const seenIds = new Set<string>();
  for (let index = 0; index < raw.length; index++) {
    const entry: unknown = raw[index];
    if (out.length >= LIMITS.maxPanels) {
      // Stop after the cap: do not parse or log the remainder one by one.
      warnSkipped(null, index, `over panel limit (${raw.length - index} dropped)`);
      break;
    }
    const id =
      isRecord(entry) && typeof entry.id === "string" && entry.id.length >= 1 && entry.id.length <= LIMITS.idMax
        ? entry.id
        : null;
    try {
      out.push(parseOne(entry, seenIds));
    } catch (e) {
      if (!(e instanceof Skip)) throw e;
      warnSkipped(id, index, e.message);
    }
  }
  return out;
}
