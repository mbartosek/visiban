/**
 * True only for an absolute `http:` / `https:` URL (#1087).
 *
 * Use this at every site that binds an externally-sourced URL (provider JSON,
 * imports, any value an API returns that a third party influenced) to an `href`
 * or `window.open`. An `href` is an XSS sink: `javascript:`, `data:` and
 * `vbscript:` URLs run or render attacker content. Fail closed: when this
 * returns false, render text and do not open anything.
 *
 * Strictness choices:
 * - Non-strings, empty strings, and relative/garbage values are rejected.
 * - Any ASCII control character or surrounding whitespace is rejected. The WHATWG
 *   URL parser silently strips leading/trailing C0 controls and spaces and removes
 *   tab/CR/LF inside the value, so "\tjava\nscript:..." would otherwise be
 *   normalized by the browser into something different from what we validated.
 * - Embedded credentials (userinfo) are rejected.
 * - A URL that parses to a non-http(s) scheme (any letter case) is rejected.
 *
 * For card external refs, which also enforce a length cap and no credentials,
 * use `isHttpUrl` from `externalRef.ts`.
 */
export function isSafeHttpUrl(value: unknown): value is string {
  if (typeof value !== "string" || value === "") return false;
  // eslint-disable-next-line no-control-regex -- deliberate: reject C0 controls and DEL
  if (/[\u0000- \u007f]/.test(value)) return false;
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
  // Credentials in a link are a phishing lookalike ("https://github.com@evil.com").
  return parsed.hostname !== "" && !parsed.username && !parsed.password;
}
