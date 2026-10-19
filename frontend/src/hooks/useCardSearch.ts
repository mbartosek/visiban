import { useEffect, useState } from "react";
import { searchAllCards } from "../api/cards";

interface UseCardSearchResult {
  searchMatchIds: Set<number> | null;
  isSearching: boolean;
  /** Non-null only when the page ceiling was hit and some matches are not applied (#1763). */
  truncation: { shown: number; total: number } | null;
}

/**
 * Debounced server-side card search hook.
 *
 * Returns null searchMatchIds when query is empty (no filter applied) or on
 * error (silent fallback — show all cards). A non-null Set means the server
 * returned results and only those card IDs should pass the search filter.
 *
 * Every page of matches is fetched (the endpoint caps one response at 200);
 * if the page ceiling is hit, `truncation` reports it so the UI can say so.
 *
 * Stale in-flight requests are aborted via AbortController so the hook never
 * applies results from a superseded query.
 */
export function useCardSearch(
  boardId: number,
  query: string,
  debounceMs = 300,
): UseCardSearchResult {
  const [searchMatchIds, setSearchMatchIds] = useState<Set<number> | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [truncation, setTruncation] = useState<{ shown: number; total: number } | null>(null);

  useEffect(() => {
    // Empty query — clear any previous results immediately, no request needed.
    if (!query.trim()) {
      setSearchMatchIds(null);
      setTruncation(null);
      setIsSearching(false);
      return;
    }

    const controller = new AbortController();

    const timer = setTimeout(() => {
      setIsSearching(true);

      searchAllCards(boardId, query, controller.signal)
        .then(({ cards, truncated, total }) => {
          setSearchMatchIds(new Set(cards.map((c) => c.id)));
          setTruncation(truncated ? { shown: cards.length, total } : null);
          setIsSearching(false);
        })
        .catch((err) => {
          // Ignore abort errors from stale requests — they are expected and not errors.
          if (err?.name === "CanceledError" || err?.name === "AbortError" || controller.signal.aborted) {
            return;
          }
          // Silent fallback: treat any other error as no filter (show all cards).
          setSearchMatchIds(null);
          setTruncation(null);
          setIsSearching(false);
        });
    }, debounceMs);

    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [boardId, query, debounceMs]);

  return { searchMatchIds, isSearching, truncation };
}
