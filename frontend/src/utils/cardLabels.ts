import type { Card, Label } from "../types";

/**
 * Drop labels that are not on the board from a card (#1888). The card renderer
 * does not filter, and `label.deleted` carries no per-card event, so a card
 * response that was in flight (a move/PATCH result, an out-of-order
 * `card.updated`, a rollback snapshot) could otherwise put a just-deleted
 * label back. Card labels are always board labels, so anything absent from
 * `boardLabels` is stale. Returns the same object when nothing is dropped.
 */
export function dropUnknownLabels(card: Card, boardLabels: Label[]): Card {
  if (card.labels.every((l) => boardLabels.some((b) => b.id === l.id))) return card;
  return { ...card, labels: card.labels.filter((l) => boardLabels.some((b) => b.id === l.id)) };
}
