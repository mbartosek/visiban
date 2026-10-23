/**
 * One-time localStorage key migration (#1855).
 *
 * Three preferences predate the `user:prefs:{name}` naming rule. This moves a
 * value from its legacy key to the conforming key: read old, write new, remove
 * old. If the new key already exists it wins and the old key is only removed,
 * so a stale legacy value can never overwrite a choice made after the rename.
 *
 * Storage can throw (private mode, blocked site data, quota); a failed
 * migration must never crash the app, so every access is swallowed. The shim
 * is idempotent and can be removed under #1859 once the legacy keys have aged
 * out (also drop the legacy read in public/theme-init.js).
 */
export function migrateLegacyKey(oldKey: string, newKey: string): void {
  try {
    const oldValue = localStorage.getItem(oldKey);
    if (oldValue === null) return;
    if (localStorage.getItem(newKey) === null) {
      localStorage.setItem(newKey, oldValue);
    }
    localStorage.removeItem(oldKey);
  } catch {
    // Storage unavailable — fall through to defaults.
  }
}
