import { staleBadgeToneClass, type AgingStage } from "../../utils/agingTint";

/**
 * Non-color stage cue for a stale-tinted card (#1843, WCAG 1.4.1).
 *
 * The `Stale · Nd` badge only renders on non-compact cards at comfortable or
 * standard density, and only when no higher-priority urgency (overdue,
 * due-soon) preempts it. Everywhere else — every compact card, dense cards,
 * preempted badges — the aging tint was the only at-a-glance signal, so
 * stale vs 2× vs 3× differed by hue alone. CardItem renders this glyph
 * exactly when the card has a stale-stage tint and the badge is not visible.
 *
 * Shape, not text: a compact card is ~77px of content wide (two-column grid
 * in a 220px cell), so `41d` would eat half the title. Three ascending bars
 * with the solid count equal to the stage multiple (1×, 2×, 3×) tell the
 * stages apart by shape; the bars above the stage are ghosted. The day count
 * lives in the accessible name and in the card's own aging tooltip.
 *
 * It sits inline at the start of the title `<p>` so it shares the title's
 * line box and never changes card height. Do not wrap it in `inline-flex`:
 * that moves the line's baseline onto the SVG and shifts the title.
 *
 * The aria-label states age and multiple, never "Stale": the stage is
 * client-computed and the server's `is_stale` can disagree by a day at the
 * edge (see formatAgingTooltip). No `title` attribute — the card's tooltip
 * already opens on hover and keyboard focus with the same information.
 * Because it leads the title, the card button's content-derived name starts
 * with "Idle N days, …" — accepted: the leading position is a hard visual
 * constraint (the bulk-select checkbox covers the trailing edge).
 */

const SOLID_BARS: Partial<Record<AgingStage, number>> = { stale: 1, stale2x: 2, stale3x: 3 };

// x / y / height of each bar in a 12×12 viewBox, shortest first.
const BARS = [
  { x: 0, y: 7, h: 5 },
  { x: 4.5, y: 3.5, h: 8.5 },
  { x: 9, y: 0, h: 12 },
] as const;

function cueLabel(stage: AgingStage, idleDays: number): string {
  const days = idleDays === 1 ? "1 day" : `${idleDays} days`;
  // "times", not "×": screen readers read "×" as "multiplication sign".
  if (stage === "stale3x") return `Idle ${days}, at least 3 times the stale threshold`;
  if (stage === "stale2x") return `Idle ${days}, at least 2 times the stale threshold`;
  return `Idle ${days}, past the stale threshold`;
}

interface AgingCueProps {
  stage: AgingStage;
  idleDays: number;
}

export default function AgingCue({ stage, idleDays }: AgingCueProps) {
  const solid = SOLID_BARS[stage];
  if (solid === undefined) return null;
  return (
    <svg
      role="img"
      aria-label={cueLabel(stage, idleDays)}
      data-aging-cue={stage}
      viewBox="0 0 12 12"
      // Same on-tint tone as the badge (#1550): measured >= 4.55:1 on every
      // stale stage in both themes, above the 3:1 a non-text graphic needs.
      // -0.125em, not align-baseline: a 12px glyph standing on the baseline
      // leaves under 1px of headroom in a text-xs leading-snug line, so a
      // different font stack could grow the line box. Dropping the feet
      // 0.125em keeps ~1.5px of headroom at text-xs and more at text-sm.
      className={`inline-block w-3 h-3 mr-1 align-[-0.125em] ${staleBadgeToneClass(stage)}`}
    >
      {BARS.map((bar, i) => (
        <rect
          key={bar.x}
          x={bar.x}
          y={bar.y}
          width={3}
          height={bar.h}
          rx={0.5}
          fill="currentColor"
          // Ghost bars are decorative; the count of solid bars carries the stage.
          opacity={i < solid ? undefined : 0.3}
        />
      ))}
    </svg>
  );
}
