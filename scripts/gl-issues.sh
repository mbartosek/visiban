#!/usr/bin/env bash
# List Visiban GitLab issues as a table (iid, state, milestone, closed_at, title).
#
# Usage: scripts/gl-issues.sh key=value [key=value ...]
#        scripts/gl-issues.sh --self-test
#   e.g. scripts/gl-issues.sh milestone=1.2 state=opened
#        scripts/gl-issues.sh "search=prior audit" in=title state=all created_after=2026-10-16T00:00:00Z
# Each arg is one issues-API query parameter; values are URL-encoded here so multi-word
# and '&'-containing search terms survive. Used by .claude/skills/pre-release/SKILL.md.
#
# Why this exists instead of `glab issue list`: its text output omits state and milestone,
# and it silently returns nothing for an unmatched milestone title (#1901). This script
# fails closed: any glab error, non-array body, or unknown milestone title prints a
# "🔴 ... could not run" line and exits nonzero, so an empty result always means "no issues".
set -uo pipefail

self_test() {
  local tmp rc=0 out code py bash_bin
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/gl-issues-selftest-1901.XXXXXX")"
  mkdir "$tmp/bin"
  # Stub glab: STUB_FAIL=1 -> exit 1; milestones path -> $STUB_MS; otherwise $STUB_ISSUES.
  # Every requested path is appended to $STUB_LOG so URL-encoding can be asserted.
  cat > "$tmp/bin/glab" <<'STUB'
#!/bin/sh
echo "$2" >> "$STUB_LOG"
[ "$STUB_FAIL" = 1 ] && { echo "stub failure" >&2; exit 1; }
case "$2" in
  *milestones*) cat "$STUB_MS" ;;
  *) cat "$STUB_ISSUES" ;;
esac
STUB
  chmod +x "$tmp/bin/glab"
  py="$(command -v python3)"; bash_bin="$(command -v bash)"
  export STUB_LOG="$tmp/log" STUB_MS="$tmp/ms.json" STUB_ISSUES="$tmp/issues.json"
  echo '[{"title":"1.2"},{"title":"1.3"}]' > "$STUB_MS"
  echo '[{"iid":7,"state":"opened","milestone":{"title":"1.2"},"closed_at":null,"title":"hello"}]' > "$STUB_ISSUES"
  : > "$STUB_LOG"
  t() {  # name, expected rc, expected output substring, then args
    local name="$1" want="$2" frag="$3"; shift 3
    out="$(PATH="$tmp/bin:$PATH" "$bash_bin" "$0" "$@" 2>&1)"; code=$?
    if [ "$code" -ne "$want" ] || ! grep -qF -- "$frag" <<<"$out"; then
      echo "FAIL [$name]: rc=$code want=$want frag='$frag' out=$out" >&2; rc=1
    else echo "ok [$name]"; fi
  }
  t happy-path 0 "7 opened 1.2  hello" state=opened
  t happy-count 0 "checked 1 issue(s)" milestone=1.2 state=opened
  t unknown-milestone 1 "unknown milestone '9.9'" milestone=9.9
  t empty-value 1 "empty value for 'milestone'" milestone= state=opened
  t empty-date 1 "empty value for 'created_after'" created_after=
  t not-key-value 1 "is not key=value" bogus
  STUB_FAIL=1 t glab-fails 1 "could not run" state=opened
  echo '{"message":"401 Unauthorized"}' > "$STUB_ISSUES"
  t non-array 1 "not a JSON array" state=opened
  echo 'not json' > "$STUB_ISSUES"
  t non-json 1 "non-JSON response" state=opened
  echo '[]' > "$STUB_ISSUES"
  t empty-result-is-not-failure 0 "checked 0 issue(s)" state=opened
  # URL-encoding: '&' in a value must not split the parameter; spaces must be encoded.
  : > "$STUB_LOG"
  PATH="$tmp/bin:$PATH" "$bash_bin" "$0" "search=a&b" "search2=prior audit" >/dev/null 2>&1
  if grep -qF 'search=a%26b' "$STUB_LOG" && grep -qF 'search2=prior%20audit' "$STUB_LOG"; then
    echo "ok [url-encoding]"; else echo "FAIL [url-encoding]: $(cat "$STUB_LOG")" >&2; rc=1; fi
  # glab missing: only python3 and bash reachable.
  mkdir "$tmp/nobin"; ln -s "$py" "$tmp/nobin/python3"
  out="$(PATH="$tmp/nobin" "$bash_bin" "$0" state=opened 2>&1)"; code=$?
  if [ "$code" -eq 1 ] && grep -qF "could not run" <<<"$out" && ! grep -qF Traceback <<<"$out"; then
    echo "ok [missing-glab]"; else echo "FAIL [missing-glab]: rc=$code $out" >&2; rc=1; fi
  rm -rf "$tmp"
  [ "$rc" -eq 0 ] && echo "gl-issues self-test: all cases passed"
  return $rc
}

if [ "${1:-}" = "--self-test" ]; then
  self_test
  exit $?
fi

if [ "$#" -eq 0 ]; then
  echo "usage: $0 key=value [key=value ...] | --self-test" >&2
  exit 2
fi

python3 - "$@" <<'PY'
import json
import subprocess
import sys
import urllib.parse

PROJECT = "projects/visiban%2Fvisiban"
args = sys.argv[1:]


def fail(msg):
    print("🔴 pre-release issue query could not run: %s (%s)" % (msg, " ".join(args)))
    sys.exit(1)


def api(path):
    try:
        r = subprocess.run(["glab", "api", path], capture_output=True, text=True)
    except FileNotFoundError:
        fail("glab is not installed or not on PATH")
    if r.returncode != 0:
        fail("glab api exited %d: %s" % (r.returncode, r.stderr.strip() or r.stdout.strip()))
    try:
        return json.loads(r.stdout)
    except ValueError:
        fail("non-JSON response")


params = []
milestone = None
for a in args:
    if "=" not in a:
        fail("argument %r is not key=value" % a)
    k, v = a.split("=", 1)
    if not v:
        # An empty value (e.g. an unset $WORKING_RELEASE) would silently widen the query
        # to every milestone or all time; fail closed instead.
        fail("empty value for '%s'" % k)
    if k == "milestone":
        milestone = v
    params.append((k, v))
if "per_page" not in [k for k, _ in params]:
    params.append(("per_page", "100"))

if milestone:
    ms = api(PROJECT + "/milestones?state=all&per_page=100")
    if not isinstance(ms, list):
        fail("milestones response is not a JSON array")
    if milestone not in [m["title"] for m in ms]:
        fail("unknown milestone '%s'" % milestone)

d = api(PROJECT + "/issues?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote))
if not isinstance(d, list):
    fail("not a JSON array: %r" % (d,))
for i in d:
    print(i["iid"], i["state"], (i.get("milestone") or {}).get("title"),
          (i.get("closed_at") or "")[:10], i["title"][:70])
print("checked %d issue(s)" % len(d))
PY
