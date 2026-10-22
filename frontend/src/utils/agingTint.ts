/**
 * Card aging: a graduated severity ramp driven by how long a card has been
 * idle, measured in multiples of the board's `staleness_threshold_days` (#1841).
 *
 * | Stage      | Ratio (idle / threshold)        | Hue    |
 * |------------|---------------------------------|--------|
 * | `none`     | below the warning point          | —      |
 * | `warning`  | warning point ≤ ratio < 1.0      | yellow |
 * | `stale`    | 1.0 ≤ ratio < 2.0                | orange |
 * | `stale2x`  | 2.0 ≤ ratio < 3.0                | red    |
 * | `stale3x`  | ratio ≥ 3.0                      | maroon + ring |
 *
 * Every lower bound is inclusive: a card exactly at an edge is in the higher
 * stage. The multiples are fixed on purpose (no board setting) until the fixed
 * ramp proves insufficient.
 *
 * The ratio is not capped, so a card idle 15 days and one idle 150 days no
 * longer look identical — the whole reason for the ramp. There is no green
 * stage: color means "needs attention", never "good".
 *
 * Color is not the only signal (WCAG 1.4.1): the card's badge and its
 * hover/keyboard-focus tooltip state the idle days, and the 3x stage adds an
 * inset ring. The body-opacity step (100 → 90 → 85) is a weak supplementary
 * cue only; 1–2x vs 2–3x share opacity-85 and differ by hue and text.
 */

export type AgingStage = "none" | "warning" | "stale" | "stale2x" | "stale3x";

const DAY_MS = 86_400_000;

/**
 * The fraction of the threshold at which the warning stage begins, or `null`
 * when the warning stage is off.
 *
 * `stale_warning_pct` is the percentage of the threshold a card has *used*
 * before the warning appears (settings copy and model help_text). Earlier code
 * computed `1 - pct / 100`, which agreed with the contract only at the default
 * 50% (#1841). `0` turns the warning stage off: read literally it would tint
 * every card, including one moved a second ago, which contradicts "fresh cards
 * stay uncolored". `100` leaves the warning stage empty because it coincides
 * with the stale edge.
 */
export function warningFraction(warningPct: number): number | null {
  if (!(warningPct > 0)) return null;
  return Math.min(100, warningPct) / 100;
}

/** Maps an uncapped idle ratio onto a stage. Shared by the card tint and the analytics heatmap. */
export function agingStage(ratio: number, warningPct: number): AgingStage {
  if (ratio >= 3) return "stale3x";
  if (ratio >= 2) return "stale2x";
  if (ratio >= 1) return "stale";
  const warn = warningFraction(warningPct);
  if (warn !== null && ratio >= warn) return "warning";
  return "none";
}

const STAGE_CLASSES: Record<AgingStage, { overlayClass: string | null; bodyOpacityClass: string }> = {
  none: { overlayClass: null, bodyOpacityClass: "" },
  warning: { overlayClass: "absolute inset-0 rounded-md pointer-events-none bg-warning-emphasis/10", bodyOpacityClass: "opacity-90" },
  stale: { overlayClass: "absolute inset-0 rounded-md pointer-events-none bg-warning/15", bodyOpacityClass: "opacity-85" },
  stale2x: { overlayClass: "absolute inset-0 rounded-md pointer-events-none bg-danger/15", bodyOpacityClass: "opacity-85" },
  stale3x: {
    overlayClass: "absolute inset-0 rounded-md pointer-events-none bg-stale-severe/20 ring-2 ring-inset ring-stale-severe",
    bodyOpacityClass: "opacity-85",
  },
};

export interface AgingTint {
  stage: AgingStage;
  /** Idle time divided by the threshold, uncapped. 0 when the card has no movement. */
  ratio: number;
  /** Whole days since the last movement. */
  idleDays: number;
  overlayClass: string | null;
  bodyOpacityClass: string;
}

export function agingTint(
  lastMovedAt: string | null,
  thresholdDays: number,
  warningPct: number,
  now: number = Date.now()
): AgingTint {
  if (!lastMovedAt) return { stage: "none", ratio: 0, idleDays: 0, ...STAGE_CLASSES.none };
  const days = Math.max(1, thresholdDays); // prevent division by zero
  const idleMs = Math.max(0, now - new Date(lastMovedAt).getTime());
  const ratio = idleMs / (days * DAY_MS);
  const stage = agingStage(ratio, warningPct);
  return { stage, ratio, idleDays: Math.floor(idleMs / DAY_MS), ...STAGE_CLASSES[stage] };
}

function isStaleStage(stage: AgingStage): boolean {
  return stage === "stale" || stage === "stale2x" || stage === "stale3x";
}

/**
 * Hover text for the aging tint. States the idle days (WCAG 1.4.1: the tint is
 * never the only signal) and, past the threshold, how many multiples of it.
 *
 * "Stale" is only claimed when the server agrees (`serverStale`, from
 * `card.is_stale`): the tint is client-computed from `last_moved_at` and the
 * two can disagree by a day at the edge, so a client-only stale tint reads
 * "Idle 15d · stale at 14d" instead of asserting a verdict the badge does not
 * show. The ramp reports age, not that the card is stuck.
 */
export function formatAgingTooltip(
  aging: AgingTint,
  thresholdDays: number,
  warningPct: number,
  serverStale: boolean,
  blockerCount: number
): string | null {
  if (aging.stage === "none") return null;
  const thresh = Math.max(1, thresholdDays);
  const parts: string[] = [];
  if (aging.stage === "warning") {
    const warn = warningFraction(warningPct) ?? 0;
    parts.push(`Idle ${aging.idleDays}d`, `warning at ${Math.round(thresh * warn)}d`, `stale at ${thresh}d`);
  } else if (serverStale) {
    // Floor to one decimal so a card at 2.96x never reads "3.0×" while still in the 2x stage.
    const multiple = (Math.floor(aging.ratio * 10) / 10).toFixed(1);
    parts.push("Stale", `${aging.idleDays}d`, `${multiple}× the ${thresh}d threshold`);
  } else {
    parts.push(`Idle ${aging.idleDays}d`, `stale at ${thresh}d`);
  }
  if (isStaleStage(aging.stage) && blockerCount > 0) {
    parts.push(blockerCount === 1 ? "blocked by 1 card" : `blocked by ${blockerCount} cards`);
  }
  return parts.join(" · ");
}

/**
 * Text tone for the card-face "Stale" urgency badge. The badge sits on the
 * card's aging tint, so it uses the on-tint token of the stage's hue (#1550)
 * rather than plain `text-warning`, which falls below 4.5:1 on a tint.
 */
export function staleBadgeToneClass(stage: AgingStage): string {
  return stage === "stale2x" || stage === "stale3x" ? "text-danger-on-tint" : "text-warning-on-tint";
}

/** Returns the number of whole days since lastMovedAt. Returns 0 if null. */
export function idleDays(lastMovedAt: string | null): number {
  if (!lastMovedAt) return 0;
  return Math.floor((Date.now() - new Date(lastMovedAt).getTime()) / DAY_MS);
}
