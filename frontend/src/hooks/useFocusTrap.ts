import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href]:not([disabled]),button:not([disabled]),textarea:not([disabled]),' +
  'input:not([type="hidden"]):not([disabled]),select:not([disabled]),' +
  '[tabindex]:not([tabindex="-1"])';

// Active traps, innermost last. A dialog opened from inside another trapped
// surface (a confirm inside CardDetail) lives in the outer container's DOM, so
// both document handlers would otherwise fire on one Tab and fight over focus.
// Only the top of the stack acts.
const trapStack: symbol[] = [];

/**
 * Traps keyboard focus inside `containerRef` while `active` is true.
 * Tab wraps from last focusable element to first; Shift+Tab wraps in reverse.
 *
 * Also restores focus to the element that had it when the trap activated once
 * the trap deactivates or unmounts (#1732). The opener is captured during
 * render, not in an effect: a child's `autoFocus` (and the surfaces' own
 * focus-on-open effects) have already moved focus by the time effects run.
 * Focus is restored only when it would otherwise be lost (dropped to `body`), so it never steals focus the user moved elsewhere.
 *
 * Rule (frontend/CLAUDE.md): anything with `aria-modal` uses ModalWrapper or
 * this hook.
 */
export function useFocusTrap(
  containerRef: React.RefObject<HTMLElement | null>,
  active: boolean,
) {
  const openerRef = useRef<HTMLElement | null>(null);
  const wasActiveRef = useRef(false);
  if (active && !wasActiveRef.current) {
    openerRef.current = document.activeElement as HTMLElement | null;
  }
  wasActiveRef.current = active;

  useEffect(() => {
    if (!active) return;

    const id = Symbol("focus-trap");
    trapStack.push(id);

    const handler = (e: KeyboardEvent) => {
      if (e.key !== "Tab") return;
      if (trapStack[trapStack.length - 1] !== id) return;
      const container = containerRef.current;
      if (!container) return;

      const focusable = Array.from(
        container.querySelectorAll<HTMLElement>(FOCUSABLE),
      ).filter((el) => !el.closest("[aria-hidden='true']"));

      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (e.shiftKey) {
        if (document.activeElement === first || document.activeElement === container) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last || document.activeElement === container) {
          e.preventDefault();
          first.focus();
        }
      }
    };

    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
      const i = trapStack.indexOf(id);
      if (i !== -1) trapStack.splice(i, 1);

      const opener = openerRef.current;
      openerRef.current = null;
      if (!opener || !opener.isConnected || opener === document.body) return;
      const current = document.activeElement;
      // Focus is "lost" once the focused node unmounts: the browser drops it to body.
      if (!current || current === document.body) opener.focus();
    };
  }, [active, containerRef]);
}
