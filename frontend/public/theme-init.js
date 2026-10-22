// Theme initialization — runs before React mounts to prevent flash of
// incorrect theme. Reads the user's stored preference and applies the
// "dark" class + data-theme attribute to <html> immediately so the first
// paint matches the theme. Must stay in sync with ThemeContext.tsx.
(function () {
  var s = null;
  try {
    // New key first; fall back to the pre-#1855 name until ThemeContext has
    // migrated it (remove the fallback after 1.2 GA).
    s = localStorage.getItem("user:prefs:theme") || localStorage.getItem("visiban-theme");
  } catch (e) {}
  var prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  var isDark = s === "dark" || ((!s || s === "system") && prefersDark);
  var root = document.documentElement;
  if (isDark) {
    root.classList.add("dark");
  }
  root.setAttribute("data-theme", isDark ? "dark" : "light");
})();
