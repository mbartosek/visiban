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
# Pin the clock so the fixtures' watermark (2026-10-01) never ages into the stale range.
fp() { python3 "$SCRIPT" --now 2026-10-14T00:00:00Z "$@"; }

fail=0; pass=0
check() { if [[ "$2" -eq 0 ]]; then pass=$((pass+1)); else echo "  FAIL: $1"; fail=$((fail+1)); fi; }
has() { grep -qF -- "$2" "$1" && echo 0 || echo 1; }
lacks() { grep -qF -- "$2" "$1" && echo 1 || echo 0; }

cp "$FIX/fastpath_table.md" "$TMP/table.md"
before="$(shasum "$TMP/table.md")"
run() { fp --state "$FIX/fastpath_state.json" --declined "$TMP/none.json" --table "$TMP/table.md" "$@"; }

rc=0; run --evidence-out "$TMP/ev.md" > "$TMP/p.diff" 2> "$TMP/err" || rc=$?
check "exits 0" "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
check "table file is never written" "$([ "$(shasum "$TMP/table.md")" = "$before" ] && echo 0 || echo 1)"
check "regression-check (11 zeros, fix) removed from the bugfix row" \
  "$(has "$TMP/p.diff" '+| Pure bugfix with identified root cause | `test-scaffold` (if no coverage)')"
check "already-filed issue (proposed) is still proposed in the table diff" "$(has "$TMP/ev.md" '| `regression-check` | `fix` | 11 |')"
check "plus-joined gate removed alone from the settings row" \
  "$(has "$TMP/p.diff" '+| Settings sub-page wiring (form/toggle on an existing serializer) | `ux-review` (visual diff) → `rbac-check` + `regression-check` →')"
check "non-canonical spacing, -> and an arrow inside a qualifier survive the edit" \
  "$(has "$TMP/p.diff" '+| Docs-only | `docs-writer` (if new page -> see docs/)  →  `/mr` |')"
check "below threshold (9) is not proposed" "$(lacks "$TMP/ev.md" '`completeness-check` | `fix`')"
check "load-bearing gate is not touched" "$(lacks "$TMP/ev.md" '`architect`')"
check "cluster-only gate is a hand-edit note, not a diff" "$(has "$TMP/ev.md" 'only via the pre-MR gate cluster in: Backend-only feature; New user-visible feature (hand edit)')"
check "unmapped shape is a note" "$(has "$TMP/ev.md" 'diff shape `other` maps to no table row')"
check "removing a gate from every row that runs it is a deletion note" "$(has "$TMP/ev.md" '| `ux-design` | `feat` | 11 |')"
check "deletion note names the /kaizen hard rule" "$(has "$TMP/ev.md" "a gate deletion, which /kaizen's hard rule reserves for a human")"
check "a gate deletion is never diffed (ux-design)" "$(lacks "$TMP/p.diff" '+| Frontend-only feature')"
check "a gate deletion is never diffed (voc -> voice-of-customer)" "$(lacks "$TMP/p.diff" '+| New user-visible feature')"
check "each narrowed gate gets a collateral-review checklist line" \
  "$(has "$TMP/ev.md" '- [ ] `security-review`: also review the Gate reference row and any prose restating when this gate fires')"
check "no checklist line for a gate with no diff" "$(lacks "$TMP/ev.md" '- [ ] `ux-design`')"
check "stderr carries a one-line summary" "$(has "$TMP/err" '7 candidate(s) over threshold, 3 diff edit(s), 5 evidence-only note(s)')"
check "a fresh state carries no stale warning" "$(lacks "$TMP/ev.md" 'Stale signal')"
check "evidence cites the streak MRs" "$(has "$TMP/ev.md" '!1101, !1102')"
check "evidence cites the watermark and threshold" "$(has "$TMP/ev.md" 'watermark !1200')"
check "evidence goes to the file, not stderr" "$(lacks "$TMP/err" 'Fast-path evidence')"

# The diff applies cleanly with patch and the result matches the expected table.
cp "$FIX/fastpath_table.md" "$TMP/applied.md"
check "diff applies with patch" "$(patch -s "$TMP/applied.md" < "$TMP/p.diff" >/dev/null 2>&1 && echo 0 || echo 1)"
check "applied table keeps the docs-only row" "$(has "$TMP/applied.md" '| Docs-only | `docs-writer` (if new page -> see docs/)  →  `/mr` |')"

# Without --evidence-out the section goes to stderr, keeping stdout a pure diff.
run > "$TMP/p2.diff" 2> "$TMP/err2"
check "stderr carries the evidence section by default" "$(has "$TMP/err2" '## Fast-path evidence')"
check "stdout is only the diff" "$(lacks "$TMP/p2.diff" 'Fast-path evidence')"

# Declined gates are not proposed.
echo '[{"id":"regression-check:fast-path-candidate","declined_on":"2026-10-01","note":"x"}]' > "$TMP/declined.json"
fp --state "$FIX/fastpath_state.json" --declined "$TMP/declined.json" --table "$TMP/table.md" > "$TMP/p3.diff" 2>/dev/null
check "declined gate is not proposed" "$(lacks "$TMP/p3.diff" '+| Pure bugfix')"

# Nothing at threshold -> empty diff, still exit 0.
python3 - "$FIX/fastpath_state.json" > "$TMP/quiet.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
for p in s["pairs"].values():
    p["streak"] = min(p["streak"], 3)
print(json.dumps(s))
PY
fp --state "$TMP/quiet.json" --declined "$TMP/none.json" --table "$TMP/table.md" > "$TMP/p4.diff" 2> "$TMP/err4"
check "no candidates -> empty diff" "$([ ! -s "$TMP/p4.diff" ] && echo 0 || echo 1)"
check "no candidates -> says so" "$(has "$TMP/err4" 'No table edit proposed.')"
check "no candidates -> summary says 0" "$(has "$TMP/err4" '0 candidate(s) over threshold, 0 diff edit(s), 0 evidence-only note(s)')"

# The threshold comes from the state file, not the module constant (10).
set_threshold() { python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); s["threshold"]=int(sys.argv[2]); print(json.dumps(s))' "$FIX/fastpath_state.json" "$1"; }
set_threshold 12 > "$TMP/hi.json"
fp --state "$TMP/hi.json" --declined "$TMP/none.json" --table "$TMP/table.md" > "$TMP/p5.diff" 2> "$TMP/err5"
check "state threshold 12 suppresses an 11-run streak" "$(lacks "$TMP/p5.diff" '+| Pure bugfix')"
check "state threshold 12 leaves only the 12-run pair" "$(has "$TMP/err5" '1 candidate(s) over threshold')"
set_threshold 9 > "$TMP/lo.json"
fp --state "$TMP/lo.json" --declined "$TMP/none.json" --table "$TMP/table.md" > /dev/null 2> "$TMP/err6"
check "state threshold 9 admits the 9-run streak" "$(has "$TMP/err6" '| `completeness-check` | `fix` | 9 |')"

# Error paths exit 2: a file with no table, an escaping path, evidence onto the table.
echo "no table here" > "$TMP/notable.md"
rc=0; fp --state "$FIX/fastpath_state.json" --table "$TMP/notable.md" >/dev/null 2>&1 || rc=$?
check "file without a table exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
rc=0; fp --state "$FIX/fastpath_state.json" --table /etc/passwd >/dev/null 2>&1 || rc=$?
check "--table outside allowed roots exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
rc=0; run --evidence-out "$TMP/table.md" >/dev/null 2>&1 || rc=$?
check "--evidence-out onto the table exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
ln "$TMP/table.md" "$TMP/hardlink.md"
rc=0; run --evidence-out "$TMP/hardlink.md" >/dev/null 2>&1 || rc=$?
check "--evidence-out onto a hard link of the table exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
rc=0; fp --state "$FIX/fastpath_state.json" --table "$TMP/table.md" --evidence-out /etc/x.md >/dev/null 2>"$TMP/err7" || rc=$?
check "escaping --evidence-out names the allowed roots and TMPDIR" \
  "$([ "$rc" -eq 2 ] && grep -q 'allowed roots:' "$TMP/err7" && grep -q 'TMPDIR' "$TMP/err7" && echo 0 || echo 1)"
check "table still untouched after the refused write" "$([ "$(shasum "$TMP/table.md")" = "$before" ] && echo 0 || echo 1)"

# Without --evidence-out the summary is still the LAST line of stderr.
check "summary is the last stderr line without --evidence-out" \
  "$(tail -n 1 "$TMP/err2" | grep -q 'diff edit(s)' && echo 0 || echo 1)"

# A missing or typo'd --state exits 2 instead of reading as "no candidates".
rc=0; fp --state "$TMP/typo.json" --table "$TMP/table.md" >/dev/null 2>"$TMP/err8" || rc=$?
check "missing --state exits 2" "$([ "$rc" -eq 2 ] && grep -q -- '--state file not found' "$TMP/err8" && echo 0 || echo 1)"

# A watermark older than STALE_DAYS (30) warns at the top of stderr and in the evidence.
rc=0; python3 "$SCRIPT" --now 2026-11-15T00:00:00Z --state "$FIX/fastpath_state.json" --declined "$TMP/none.json" \
  --table "$TMP/table.md" --evidence-out "$TMP/ev9.md" >/dev/null 2>"$TMP/err9" || rc=$?
check "stale state still exits 0" "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
check "stale warning is the first stderr line" "$(head -n 1 "$TMP/err9" | grep -q 'STALE SIGNAL' && echo 0 || echo 1)"
check "stale warning is in the evidence section" "$(has "$TMP/ev9.md" '> **Stale signal:** the state watermark (2026-10-01T00:00:00Z, !1200) is 45 days old')"

# An unexpected error (malformed state) exits 2 with a message, never a traceback.
echo '{"pairs": {"x|fix": {"runs": 1}}}' > "$TMP/bad.json"
rc=0; fp --state "$TMP/bad.json" --table "$TMP/table.md" >/dev/null 2>"$TMP/err10" || rc=$?
check "malformed state exits 2" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
check "malformed state prints no traceback" "$(lacks "$TMP/err10" 'Traceback')"

# Ambiguous qualifier and repeated items: refused as evidence notes, never diffed.
cp "$FIX/fastpath_table_ambiguous.md" "$TMP/amb.md"
fp --state "$FIX/fastpath_state_ambiguous.json" --declined "$TMP/none.json" --table "$TMP/amb.md" \
  --evidence-out "$TMP/ev11.md" > "$TMP/p11.diff" 2>/dev/null
check "ambiguous and repeated cells produce no diff" "$([ ! -s "$TMP/p11.diff" ] && echo 0 || echo 1)"
check "ambiguous qualifier is a refusal note" "$(has "$TMP/ev11.md" 'Pure bugfix with identified root cause (ambiguous text after the gate)')"
check "repeated item is a refusal note" "$(has "$TMP/ev11.md" 'Docs-only (named more than once)')"

# CRLF table: line endings carried through so patch applies and CRLF survives.
python3 -c 'import sys; d=open(sys.argv[1],newline="").read(); open(sys.argv[2],"w",newline="").write(d.replace("\n","\r\n"))' \
  "$FIX/fastpath_table.md" "$TMP/crlf.md"
cp "$TMP/crlf.md" "$TMP/crlf_applied.md"
fp --state "$FIX/fastpath_state.json" --declined "$TMP/none.json" --table "$TMP/crlf.md" > "$TMP/p12.diff" 2>/dev/null
check "CRLF diff applies with patch" "$(patch -s "$TMP/crlf_applied.md" < "$TMP/p12.diff" >/dev/null 2>&1 && echo 0 || echo 1)"
check "CRLF survives the patch" "$(python3 -c 'import sys; d=open(sys.argv[1],"rb").read(); sys.exit(0 if d.count(b"\n")==d.count(b"\r\n") and b"+|" not in d else 1)' "$TMP/crlf_applied.md" && echo 0 || echo 1)"
check "CRLF patch made the expected edit" "$(has "$TMP/crlf_applied.md" '| Pure bugfix with identified root cause | `test-scaffold` (if no coverage)')"

# Edited last line with no trailing newline: marker emitted, patch applies.
python3 -c 'import sys; L=open(sys.argv[1]).read().splitlines(); i=[n for n,l in enumerate(L) if l.startswith("| New user")][0]; open(sys.argv[2],"w").write("\n".join(L[:i])+"\n| Settings sub-page wiring (x) | `ux-review` → `security-review` + `regression-check` → `/mr` |")' \
  "$FIX/fastpath_table.md" "$TMP/nonl.md"
cp "$TMP/nonl.md" "$TMP/nonl_applied.md"
fp --state "$FIX/fastpath_state.json" --declined "$TMP/none.json" --table "$TMP/nonl.md" > "$TMP/p13.diff" 2>/dev/null
check "no-newline marker is emitted" "$(has "$TMP/p13.diff" '\ No newline at end of file')"
check "no-newline diff applies with patch" "$(patch -s "$TMP/nonl_applied.md" < "$TMP/p13.diff" >/dev/null 2>&1 && echo 0 || echo 1)"
check "patched last line has the edit and still no newline" \
  "$(python3 -c 'import sys; d=open(sys.argv[1]).read(); sys.exit(0 if d.endswith("`ux-review` → `regression-check` → `/mr` |") else 1)' "$TMP/nonl_applied.md" && echo 0 || echo 1)"

check "--self-test passes" "$(fp --self-test >/dev/null 2>&1 && echo 0 || echo 1)"

echo ""
if [[ "$fail" -eq 0 ]]; then echo "kaizen_fastpath_proposal.test.sh: all $pass checks passed"; else echo "kaizen_fastpath_proposal.test.sh: $fail failed, $pass passed"; exit 1; fi
