import axios from "axios";

/** Server `code` values that mean a column limit refused the move. */
export const LIMIT_CODES = new Set(["wip_limit_exceeded", "wip_hard_blocked", "weight_limit_exceeded"]);

/** Why one move/update request failed, as a clause for "could not be X — {reason}".
 * The WIP/weight wording is used only when the server's `code` says so; a
 * network or permission failure must not be blamed on a column limit (#1733, #1761). */
/** True when the server's `code` says a column limit refused the request. */
export function isLimitFailure(err: unknown): boolean {
  if (!axios.isAxiosError(err)) return false;
  const code = (err.response?.data as { code?: unknown } | undefined)?.code;
  return typeof code === "string" && LIMIT_CODES.has(code);
}

export function failureReason(err: unknown, forbidden = "you do not have permission"): string {
  if (!axios.isAxiosError(err) || !err.response) return "a network error occurred";
  const data = err.response.data as { code?: unknown } | undefined;
  if (typeof data?.code === "string" && LIMIT_CODES.has(data.code)) return "a column limit was reached";
  if (data?.code === "maintenance_mode") return "maintenance mode is active";
  // Number-neutral wording: the same clause follows "1 of 1 card" and "2 of 5 cards".
  if (err.response.status === 403) return forbidden;
  if (err.response.status === 404) return "not found";
  if (err.response.status === 409) return "someone else changed a card";
  return "something went wrong";
}
