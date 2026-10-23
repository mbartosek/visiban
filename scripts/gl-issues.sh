#!/usr/bin/env bash
# List Visiban GitLab issues as a table (iid, state, milestone, closed_at, title).
#
# Usage: scripts/gl-issues.sh key=value [key=value ...]
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

if [ "$#" -eq 0 ]; then
  echo "usage: $0 key=value [key=value ...]" >&2
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
    r = subprocess.run(["glab", "api", path], capture_output=True, text=True)
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
    if k == "milestone":
        milestone = v
    params.append((k, v))
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
