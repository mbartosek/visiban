import type { SheetPlacement, SheetTransition } from "../../hooks/useCardSheetPlacement";

/**
 * Card detail sheet transitions (#1914), driven by the Web Animations API.
 *
 * Every transition is visual only and never delays a state change: closing
 * unmounts the dialog at once (focus return and "dialog is gone" checks are
 * unaffected) and a swap mounts the new card at once. What fades out is a
 * "ghost": an inert, `aria-hidden`, id- and role-stripped clone of the
 * departing sheet appended to `document.body` at its last position. It never
 * takes pointer events or focus and removes itself when its animation ends.
 *
 * Feature-detected: without `Element.animate` (jsdom, very old browsers)
 * nothing animates and no ghost is ever made.
 */

const SWAP_EASE = "cubic-bezier(.2,.8,.2,1)";
/** Above the sheet (z-50) so the departing content cross-fades over the new one; below the z-[60] popovers. */
const GHOST_Z_INDEX = "55";

/** Attributes that would give the clone a second id, a second dialog, a tab stop or a form control. */
const STRIPPED_ATTRIBUTES = [
  "id", "role", "aria-modal", "aria-labelledby", "aria-describedby", "aria-controls", "aria-live",
  "tabindex", "autofocus", "name", "form", "for", "data-testid", "href",
];

export function canAnimate(el: Element | null | undefined): el is HTMLElement {
  return !!el && typeof (el as HTMLElement).animate === "function";
}

export function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Clones `panel` into a ghost at `rect` (its current position by default), or returns null when nothing can animate. */
export function makeGhost(panel: HTMLElement, rect: DOMRect = panel.getBoundingClientRect()): HTMLElement | null {
  if (!canAnimate(panel)) return null;
  const ghost = panel.cloneNode(true) as HTMLElement;
  for (const el of [ghost, ...Array.from(ghost.querySelectorAll("*"))]) {
    for (const attr of STRIPPED_ATTRIBUTES) el.removeAttribute(attr);
  }
  ghost.setAttribute("aria-hidden", "true");
  ghost.setAttribute("inert", "");
  ghost.setAttribute("data-card-sheet-ghost", "");
  Object.assign(ghost.style, {
    position: "fixed",
    left: `${rect.left}px`,
    top: `${rect.top}px`,
    width: `${rect.width}px`,
    height: `${rect.height}px`,
    right: "auto",
    bottom: "auto",
    margin: "0",
    pointerEvents: "none",
    zIndex: GHOST_Z_INDEX,
  });
  document.body.appendChild(ghost);
  return ghost;
}

function fadeOutAndRemove(ghost: HTMLElement, keyframes: Keyframe[], options: KeyframeAnimationOptions) {
  const anim = ghost.animate(keyframes, { fill: "forwards", ...options });
  const remove = () => ghost.remove();
  anim.onfinish = remove;
  anim.oncancel = remove;
}

/** Which way the sheet is "away from the card": an anchored-left sheet moves left, everything else right. */
function awayDirection(placement: SheetPlacement | null): 1 | -1 {
  return placement?.mode === "anchored" && placement.side === "left" ? -1 : 1;
}

/** Close: 140ms ease-in, fade plus an 8px move away from the card (reduced motion: a 120ms fade). */
export function playClose(ghost: HTMLElement, placement: SheetPlacement | null) {
  if (prefersReducedMotion()) {
    fadeOutAndRemove(ghost, [{ opacity: 1 }, { opacity: 0 }], { duration: 120 });
    return;
  }
  const dx = 8 * awayDirection(placement);
  fadeOutAndRemove(ghost, [
    { opacity: 1, transform: "none" },
    { opacity: 0, transform: `translateX(${dx}px)` },
  ], { duration: 140, easing: "ease-in" });
}

/**
 * Plays `kind` on the sheet `panel`, which is already at its new position.
 * `from` is the previous rect (the departing card's on a swap). `ghost`, when
 * given, holds the previous content: it cross-fades out on a slide and fades
 * out in place on a fade. On a fade with no ghost (same card placed
 * elsewhere), one is cloned from the panel at `from`.
 */
export function playTransition(kind: SheetTransition, panel: HTMLElement, opts: {
  placement: SheetPlacement;
  from?: DOMRect | null;
  ghost?: HTMLElement | null;
}) {
  if (kind === "none" || !canAnimate(panel)) {
    opts.ghost?.remove();
    return;
  }
  const reduced = prefersReducedMotion();
  const { placement, from = null } = opts;
  let ghost = opts.ghost ?? null;

  if (kind === "open") {
    ghost?.remove();
    if (reduced) {
      panel.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 120 });
      return;
    }
    // Opens with a 12px move toward the card, so it starts 12px away from it.
    const dx = 12 * awayDirection(placement);
    panel.animate([
      { opacity: 0, transform: `translateX(${dx}px)` },
      { opacity: 1, transform: "none" },
    ], { duration: 180, easing: "ease-out" });
    return;
  }

  // Reduced motion: every swap or reposition is a 120ms fade out, then a
  // 120ms fade in at the new position. No slide.
  if (kind === "fade" || reduced) {
    if (!ghost && from) ghost = makeGhost(panel, from);
    const out = reduced ? 120 : 90;
    const inn = reduced ? 120 : 140;
    if (ghost) fadeOutAndRemove(ghost, [{ opacity: 1 }, { opacity: 0 }], { duration: out });
    panel.animate([{ opacity: 0 }, { opacity: 1 }], { duration: inn, delay: out, fill: "backwards" });
    return;
  }

  // Same-side slide: 220ms. The sheet moves from the old position; a ghost
  // of the old content moves with it and fades out over the first 80ms while
  // the new content fades in over 120ms, so the header text updates at the
  // start of the fade-in.
  const now = panel.getBoundingClientRect();
  const dx = from ? from.left - now.left : 0;
  const timing = { duration: 220, easing: SWAP_EASE };
  panel.animate([{ transform: `translateX(${dx}px)` }, { transform: "none" }], timing);
  if (ghost) {
    fadeOutAndRemove(ghost, [
      { opacity: 1, transform: "none" },
      { opacity: 0, offset: 80 / 220 },
      { opacity: 0, transform: `translateX(${-dx}px)` },
    ], timing);
    for (const child of Array.from(panel.children)) {
      child.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 120, delay: 80, fill: "backwards" });
    }
  }
}

// ── Swap hand-off ──────────────────────────────────────────────────────────
// Panels replace, never stack: a swap unmounts one CardDetail and mounts the
// next in the same commit (`key={selectedCard.id}`). The departing sheet
// leaves its ghost and placement here; the arriving one claims them in its
// layout effect, which runs before the next frame. Unclaimed by then, it was
// a close, and the ghost plays the close transition.

interface Departure {
  cardId: number;
  placement: SheetPlacement;
  rect: DOMRect;
  ghost: HTMLElement;
  frame: number;
}

let departure: Departure | null = null;

/** Called by a sheet as it unmounts. */
export function departSheet(cardId: number, placement: SheetPlacement, panel: HTMLElement) {
  dropDeparture();
  const rect = panel.getBoundingClientRect();
  const ghost = makeGhost(panel, rect);
  if (!ghost) return;
  const frame = requestAnimationFrame(() => {
    if (departure?.ghost !== ghost) return;
    departure = null;
    playClose(ghost, placement);
  });
  departure = { cardId, placement, rect, ghost, frame };
}

/**
 * Takes the departing sheet for a swap. The same card id is not a swap but
 * React StrictMode's simulated unmount and remount in development: its ghost
 * is dropped silently and nothing is returned.
 */
export function claimDeparture(cardId: number): Omit<Departure, "frame" | "cardId"> | null {
  const d = departure;
  if (!d) return null;
  departure = null;
  cancelAnimationFrame(d.frame);
  if (d.cardId === cardId) {
    d.ghost.remove();
    return null;
  }
  return { placement: d.placement, rect: d.rect, ghost: d.ghost };
}

/**
 * Drops a pending departure of `cardId` itself: React StrictMode's simulated
 * unmount of a sheet that stays mounted. A different card's departure is a
 * swap and is left for `claimDeparture`.
 */
export function dropOwnDeparture(cardId: number) {
  if (departure?.cardId === cardId) dropDeparture();
}

/** Drops a pending departure without animating it. */
export function dropDeparture() {
  if (!departure) return;
  cancelAnimationFrame(departure.frame);
  departure.ghost.remove();
  departure = null;
}
