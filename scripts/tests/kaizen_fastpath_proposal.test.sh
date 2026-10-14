#!/usr/bin/env bash
# scripts/tests/kaizen_fastpath_proposal.test.sh
#
# Unit test for scripts/kaizen_fastpath_proposal.py (#1145): proposes fast-path
# table edits from the #1144 yield state. No network: fixture state + fixture
# table under scripts/tests/fixtures/kaizen/. Verifies the threshold, row
# mapping by diff shape, cluster-only and unmapped notes, the declined ledger,
# the evidence section, and that the table file is never written.
#
# Run: bash scripts/tests/kaizen_fastpath_proposal.test.sh
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/kaizen_fastpath_proposal.py"
FIX="$REPO_ROOT/scripts/tests/fixtures/kaizen"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0; pass=0
check() { if [[ "$2" -eq 0 ]]; then pass=$((pass+1)); else echo "  FAIL: $1"; fail=$((fail+1)); fi; }
has() { grep -qF -- "$2" "$1" && echo 0 || echo 1; }
lacks() { grep -qF -- "$2" "$1" && echo 1 || echo 0; }

cp "$FIX/fastpath_table.md" "$TMP/table.md"
before="$(shasum "$TMP/table.md")"
run() { python3 "$SCRIPT" --state "$FIX/fastpath_state.json" --declined "$TMP/none.json" --table "$TMP/table.md" "$@"; }

rc=0; run --evidence-out "$TMP/ev.md" > "$TMP/p.diff" 2> "$TMP/err" || rc=$?
check "exits 0" "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
check "table file is never written" "$([ "$(shasum "$TMP/table.md")" = "$before" ] && echo 0 || echo 1)"
check "ux-design (11 zeros, feat) removed from the frontend-only row" \
  "$(has "$TMP/p.diff" '+| Frontend-only feature (no API change) | `architect` → implement → `ux-review`')"
check "voc maps to the table's voice-of-customer and is removed" \
  "$(has "$TMP/p.diff" '+| New user-visible feature (full stack) | `threat-model`')"
check "already-filed issue (proposed) is still proposed in the table diff" "$(has "$TMP/ev.md" '| `ux-design` | `feat` | 11 |')"
check "dependency on chore is removed from the dependency-bump row" \
  "$(has "$TMP/p.diff" '+| Dependency bump (single package, no API change) | `changelog` → `/mr` |')"
check "below threshold (9) is not proposed" "$(lacks "$TMP/p.diff" '-| Pure bugfix')"
check "load-bearing gate is not touched" "$(lacks "$TMP/ev.md" '`architect`')"
check "cluster-only gate is a hand-edit note, not a diff" "$(has "$TMP/ev.md" 'only via the pre-MR gate cluster in: Backend-only feature; New user-visible feature (hand edit)')"
check "unmapped shape is a note" "$(has "$TMP/ev.md" 'diff shape `other` maps to no table row')"
check "evidence cites the streak MRs" "$(has "$TMP/ev.md" '!1101, !1102')"
check "evidence cites the watermark and threshold" "$(has "$TMP/ev.md" 'watermark !1200')"
check "evidence goes to the file, not stderr" "$([ ! -s "$TMP/err" ] && echo 0 || echo 1)"

# The diff applies cleanly with patch and the result matches the expected table.
cp "$FIX/fastpath_table.md" "$TMP/applied.md"
check "diff applies with patch" "$(patch -s "$TMP/applied.md" < "$TMP/p.diff" >/dev/null 2>&1 && echo 0 || echo 1)"
check "applied table keeps the docs-only row" "$(has "$TMP/applied.md" '| Docs-only | `docs-writer` (if new page)')"

# Without --evidence-out the section goes to stderr, keeping stdout a pure diff.
run > "$TMP/p2.diff" 2> "$TMP/err2"
check "stderr carries the evidence section by default" "$(has "$TMP/err2" '## Fast-path evidence')"
check "stdout is only the diff" "$(lacks "$TMP/p2.diff" 'Fast-path evidence')"

# Declined gates are not proposed.
echo '[{"id":"ux-design:fast-path-candidate","declined_on":"2026-10-01","note":"x"}]' > "$TMP/declined.json"
python3 "$SCRIPT" --state "$FIX/fastpath_state.json" --declined "$TMP/declined.json" --table "$TMP/table.md" > "$TMP/p3.diff" 2>/dev/null
check "declined gate is not proposed" "$(lacks "$TMP/p3.diff" '+| Frontend-only feature')"

# Nothing at threshold -> empty diff, still exit 0.
python3 - "$FIX/fastpath_state.json" > "$TMP/quiet.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
for p in s["pairs"].values():
    p["streak"] = min(p["streak"], 3)
print(json.dumps(s))
PY
python3 "$SCRIPT" --state "$TMP/quiet.json" --declined "$TMP/none.json" --table "$TMP/table.md" > "$TMP/p4.diff" 2> "$TMP/err4"
check "no candidates -> empty diff" "$([ ! -s "$TMP/p4.diff" ] && echo 0 || echo 1)"
check "no candidates -> says so" "$(has "$TMP/err4" 'No table edit proposed.')"

# Error paths exit 2: a file with no table, an escaping path, evidence onto the table.
echo "no table here" > "$TMP/notable.md"
rc=0; python3 "$SCRIPT" --state "$FIX/fastpath_state.json" --table "$TMP/notable.md" >/dev/null 2>&1 || rc=$?
check "file without a table exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
rc=0; python3 "$SCRIPT" --state "$FIX/fastpath_state.json" --table /etc/passwd >/dev/null 2>&1 || rc=$?
check "--table outside allowed roots exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
rc=0; run --evidence-out "$TMP/table.md" >/dev/null 2>&1 || rc=$?
check "--evidence-out onto the table exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
check "table still untouched after the refused write" "$([ "$(shasum "$TMP/table.md")" = "$before" ] && echo 0 || echo 1)"

check "--self-test passes" "$(python3 "$SCRIPT" --self-test >/dev/null 2>&1 && echo 0 || echo 1)"

echo ""
if [[ "$fail" -eq 0 ]]; then echo "kaizen_fastpath_proposal.test.sh: all $pass checks passed"; else echo "kaizen_fastpath_proposal.test.sh: $fail failed, $pass passed"; exit 1; fi
