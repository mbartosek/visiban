#!/usr/bin/env bash
# scripts/tests/wt-forge-timeout.test.sh
#
# Regression test for #1803: every glab/gh call in scripts/wt must be bounded by
# WT_FORGE_TIMEOUT. The bug: `wt prune` removed a worktree, then hung forever in
# clear_lock's unbounded `glab issue update`, and Ctrl-C left status::wip set.
#
# `glab` is stubbed on PATH as a script that sleeps far longer than the bound.
# A timeout must (a) return within the bound, (b) warn naming the issue, (c)
# leave prune's worktree removal intact, and (d) never read as "merged".
#
# Run: bash scripts/tests/wt-forge-timeout.test.sh

# The case-3 fragment is sourced from scripts/wt and reads DEFAULT_BRANCH/warn.
# shellcheck disable=SC1090,SC2034,SC2329
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WT="$REPO_ROOT/scripts/wt"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
check() { # check <desc> <rc>
  if [[ "$2" -ne 0 ]]; then echo "  FAIL: $1"; fail=$((fail + 1)); fi
}

mkdir -p "$TMP/bin"
cat > "$TMP/bin/glab" <<'STUB'
#!/usr/bin/env bash
sleep 30
STUB
chmod +x "$TMP/bin/glab"
export PATH="$TMP/bin:$PATH"
export WT_FORGE_TIMEOUT=1

D="$TMP/repo"
mkdir -p "$D"
( cd "$D"
  git init -q -b main
  git config user.email t@t.co; git config user.name t
  echo base > README.md; git add -A; git commit -qm init
  git init -q --bare "$D.gitlab-origin.git"
  git remote add origin "$D.gitlab-origin.git"
  git push -q origin main; git fetch -q origin )

now() { date +%s; }

echo "Case 1: release (clear_lock) returns within the bound and names the issue"
start="$(now)"
set +e
OUT="$( cd "$D" && bash "$WT" release 1803 2>&1 )"; rc=$?
set -e
elapsed=$(( $(now) - start ))
check "release exits 0 (warning, not failure)" "$rc"
check "release returned within the bound (took ${elapsed}s)" "$(( elapsed < 10 ? 0 : 1 ))"
grep -q "timed out" <<<"$OUT"; check "warns about the timeout" "$?"
grep -q "scripts/wt release 1803" <<<"$OUT"; check "warning tells the user how to retry" "$?"

echo "Case 2: prune removes the worktree even when the lock release stalls"
P="$D.wt.feat-1803-x"
git -C "$D" worktree add -q -b feat/1803-x "$P" main 2>/dev/null
echo a > "$P/a.txt"; git -C "$P" add a.txt; git -C "$P" commit -qm a
git -C "$P" push -q -u origin feat/1803-x 2>/dev/null
git -C "$D" merge -q --no-ff feat/1803-x -m merge
git -C "$D" push -q origin main
git -C "$D" push -q origin --delete feat/1803-x 2>/dev/null
start="$(now)"
set +e
OUT="$( cd "$D" && bash "$WT" prune 2>&1 )"; rc=$?
set -e
elapsed=$(( $(now) - start ))
check "prune exits 0" "$rc"
check "prune returned within the bound (took ${elapsed}s)" "$(( elapsed < 15 ? 0 : 1 ))"
check "worktree stays removed" "$([[ ! -d "$P" ]] && echo 0 || echo 1)"
grep -q "release of #1803 timed out" <<<"$OUT"; check "prune warns naming the issue" "$?"

echo "Case 3: a stalled MR lookup is 'can't answer', never 'merged'"
set +e
( cd "$D" && source <(sed -n '/^FORGE_TIMEOUT_RC=/,/^}/p;/^merged_mr_has_sha()/,/^}/p' "$WT") \
  && DEFAULT_BRANCH=main && REPO_ROOT="$D" && warn() { :; } \
  && merged_mr_has_sha feat/1803-x deadbeef ) >/dev/null 2>&1
rc=$?
set -e
check "timed-out merged_mr_has_sha returns nonzero" "$(( rc != 0 ? 0 : 1 ))"

if (( fail > 0 )); then echo "FAILED: $fail"; exit 1; fi
echo "OK"
