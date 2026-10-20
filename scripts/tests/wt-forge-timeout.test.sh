#!/usr/bin/env bash
# scripts/tests/wt-forge-timeout.test.sh
#
# Regression test for #1803: every glab/gh call in scripts/wt must be bounded by
# WT_FORGE_TIMEOUT. The bug: `wt prune` removed a worktree, then hung forever in
# clear_lock's unbounded `glab issue update`, and Ctrl-C left status::wip set.
#
# `glab` is stubbed on PATH. Its modes (GLAB_MODE) all ignore ALRM and TERM like a
# real Go binary (ignored dispositions survive exec), so only a KILL escalation
# bounds them:
#   stall        sleep far past the bound
#   partial      print a matching MR (GLAB_TIP) THEN stall (timeout with partial output)
#   grandchild   stall, with a TERM-ignoring grandchild holding stdout open
#   ok           succeed immediately and touch $GLAB_MARK
#
# A timeout must (a) return within the bound, (b) warn naming the issue, (c) leave
# prune's worktree removal intact without printing "released", (d) make
# `wt release` fail, and (e) never read as "merged".
#
# Every case runs and reports; nothing here depends on `set -e`.
#
# Run: bash scripts/tests/wt-forge-timeout.test.sh

# shellcheck disable=SC2034  # DEFAULT_BRANCH is read by the eval'd merged_mr_has_sha
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WT="${WT_UNDER_TEST:-$REPO_ROOT/scripts/wt}"  # override to run against another copy

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
ok_() { :; }
bad() { echo "  FAIL: $1"; fail=$((fail + 1)); }
expect() { # expect <desc> <condition-rc>   (0 = pass)
  if [[ "$2" -eq 0 ]]; then ok_; else bad "$1"; fi
}
has() { # has <desc> <haystack> <fixed-string>
  if [[ "$2" == *"$3"* ]]; then ok_; else bad "$1 (missing: $3)"; fi
}
lacks() { # lacks <desc> <haystack> <fixed-string>
  if [[ "$2" != *"$3"* ]]; then ok_; else bad "$1 (found: $3)"; fi
}

mkdir -p "$TMP/bin"
cat > "$TMP/bin/glab" <<'STUB'
#!/usr/bin/env bash
trap '' ALRM TERM
case "${GLAB_MODE:-stall}" in
  ok)         [[ -n "${GLAB_MARK:-}" ]] && touch "$GLAB_MARK"; exit 0 ;;
  partial)    if [[ "${1:-}" == "api" ]]; then printf '[{"sha":"%s"}]' "$GLAB_TIP"; fi
              exec sleep 30 ;;
  grandchild) sleep 30 & exec sleep 30 ;;
  *)          exec sleep 30 ;;
esac
STUB
chmod +x "$TMP/bin/glab"
export PATH="$TMP/bin:$PATH"
export WT_FORGE_TIMEOUT=1

D="$TMP/repo"
mkdir -p "$D"
( cd "$D" &&
  git init -q -b main &&
  git config user.email t@t.co && git config user.name t &&
  echo base > README.md && git add -A && git commit -qm init &&
  git init -q --bare "$D.gitlab-origin.git" &&
  git remote add origin "$D.gitlab-origin.git" &&
  git push -q origin main && git fetch -q origin )

now() { date +%s; }
BOUND=10   # generous vs the 1s limit: only a hang (30s+) can exceed it

# mk_wt <branch> — a pushed worktree on <branch> with one commit; sets P.
mk_wt() {
  P="$D.wt.${1//\//-}"
  git -C "$D" worktree add -q -b "$1" "$P" main 2>/dev/null
  echo "$1" > "$P/f.txt"; git -C "$P" add f.txt; git -C "$P" commit -qm "$1"
  git -C "$P" push -q -u origin "$1" 2>/dev/null
}

echo "Case 1: release returns within the bound, names the issue, and FAILS"
start="$(now)"
OUT="$( cd "$D" && GLAB_MODE=stall bash "$WT" release 1803 2>&1 )"; rc=$?
elapsed=$(( $(now) - start ))
expect "release exits non-zero on timeout (rc=$rc)" "$(( rc != 0 ? 0 : 1 ))"
expect "release returned within the bound (took ${elapsed}s)" "$(( elapsed < BOUND ? 0 : 1 ))"
has "warns about the timeout" "$OUT" "timed out"
has "warning tells the user how to retry" "$OUT" "scripts/wt release 1803"
lacks "no success line after a timeout" "$OUT" "released check-out"

echo "Case 2: prune removes the worktree, exits 0, claims no release"
mk_wt feat/1803-x
git -C "$D" merge -q --no-ff feat/1803-x -m merge
git -C "$D" push -q origin main
git -C "$D" push -q origin --delete feat/1803-x 2>/dev/null
start="$(now)"
OUT="$( cd "$D" && GLAB_MODE=stall bash "$WT" prune 2>&1 )"; rc=$?
elapsed=$(( $(now) - start ))
expect "prune exits 0 (rc=$rc)" "$rc"
expect "prune returned within the bound (took ${elapsed}s)" "$(( elapsed < BOUND ? 0 : 1 ))"
expect "worktree stays removed" "$([[ ! -d "$P" ]] && echo 0 || echo 1)"
has "prune warns naming the issue" "$OUT" "release of #1803 timed out"
lacks "prune prints no release success line" "$OUT" "released check-out"

echo "Case 3: prune reaches the MR lookup; a timeout with partial output is 'not merged'"
# Branch commits are NOT in main (no ancestor / patch-id path), so prune must ask
# merged_mr_has_sha. The stub prints a MR whose sha matches the tip, THEN stalls.
mk_wt feat/1803-y
TIP="$(git -C "$D" rev-parse feat/1803-y)"
git -C "$D" push -q origin --delete feat/1803-y 2>/dev/null
start="$(now)"
OUT="$( cd "$D" && GLAB_MODE=partial GLAB_TIP="$TIP" bash "$WT" prune 2>&1 )"; rc=$?
elapsed=$(( $(now) - start ))
expect "prune exits 0 (rc=$rc)" "$rc"
expect "prune returned within the bound (took ${elapsed}s)" "$(( elapsed < BOUND ? 0 : 1 ))"
expect "unmerged worktree is KEPT" "$([[ -d "$P" ]] && echo 0 || echo 1)"
has "the MR lookup really timed out" "$OUT" "glab api timed out"
has "kept with the not-merged warning" "$OUT" "NOT in origin/main"
lacks "never reported as merged" "$OUT" "merged MR head matches"

echo "Case 4: merged_mr_has_sha directly (functions really loaded)"
eval "$(sed -n '/^FORGE_TIMEOUT_RC=/,/^FORGE_PERL_WARNED=/p;/^forge_run()/,/^}/p;/^merged_mr_has_sha()/,/^}/p' "$WT")"
declare -F merged_mr_has_sha >/dev/null; expect "merged_mr_has_sha is defined" "$?"
declare -F forge_run >/dev/null; expect "forge_run is defined" "$?"
DEFAULT_BRANCH=main
warn() { echo "W: $*" >&2; }
start="$(now)"
GLAB_MODE=partial GLAB_TIP="$TIP" merged_mr_has_sha feat/1803-y "$TIP" 2>"$TMP/w4"; rc=$?
elapsed=$(( $(now) - start ))
expect "partial-output timeout returns nonzero (rc=$rc)" "$(( rc != 0 ? 0 : 1 ))"
expect "returned within the bound (took ${elapsed}s)" "$(( elapsed < BOUND ? 0 : 1 ))"
has "timeout path ran" "$(cat "$TMP/w4")" "timed out"

echo "Case 5: a grandchild holding stdout cannot extend the bound"
start="$(now)"
OUT="$(GLAB_MODE=grandchild forge_run glab issue view 1 2>&1)"; rc=$?
elapsed=$(( $(now) - start ))
expect "timeout rc is 142 (rc=$rc)" "$(( rc == 142 ? 0 : 1 ))"
expect "\$( ) call returned within the bound (took ${elapsed}s)" "$(( elapsed < BOUND ? 0 : 1 ))"

echo "Case 6: invalid WT_FORGE_TIMEOUT falls back to 20 with a warning and still runs the CLI"
for v in 0 abc -5; do
  rm -f "$TMP/mark"
  OUT="$( cd "$D" && WT_FORGE_TIMEOUT="$v" GLAB_MODE=ok GLAB_MARK="$TMP/mark" bash "$WT" release 1803 2>&1 )"; rc=$?
  expect "[$v] release succeeds (rc=$rc)" "$rc"
  has "[$v] warns about the value" "$OUT" "not a positive integer"
  expect "[$v] glab actually ran" "$([[ -e "$TMP/mark" ]] && echo 0 || echo 1)"
  has "[$v] release reported" "$OUT" "released check-out of #1803"
done

if (( fail > 0 )); then echo "FAILED: $fail"; exit 1; fi
echo "OK"
