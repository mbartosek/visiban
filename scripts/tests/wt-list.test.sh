#!/usr/bin/env bash
# scripts/tests/wt-list.test.sh
#
# `wt list` must show every worktree that `wt_count` counts (#1485): detached
# HEAD worktrees were skipped (the listing showed 6 rows under "8/10"), and a
# path containing a space was truncated at the first space.
#
# Run: bash scripts/tests/wt-list.test.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WT="$REPO_ROOT/scripts/wt"

TMP="$(cd -P "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP"' EXIT

fail=0
check() { # check <desc> <rc>
  if [[ "$2" -ne 0 ]]; then echo "  FAIL: $1"; fail=$((fail + 1)); fi
}

D="$TMP/repo"
mkdir -p "$D"
git -C "$D" init -q -b main
git -C "$D" config user.email t@t.co
git -C "$D" config user.name t
echo base > "$D/README.md"
git -C "$D" add -A
git -C "$D" commit -qm init
# wt resolves the default branch from origin/HEAD, so the repo needs an origin.
git init -q --bare "$TMP/origin.git"
git -C "$D" remote add origin "$TMP/origin.git"
git -C "$D" push -q origin main
git -C "$D" fetch -q origin
git -C "$D" remote set-head origin main

git -C "$D" worktree add -q -b feat/1-branch "$TMP/wt-branch" main
git -C "$D" worktree add -q --detach "$TMP/wt-detached" main
git -C "$D" worktree add -q -b feat/2-spaced "$TMP/with space/wt-spaced" main

OUT="$(cd "$D" && bash "$WT" list 2>&1)"
SHA="$(git -C "$D" rev-parse --short=7 HEAD)"

grep -q "Active worktrees (3/" <<<"$OUT"; check "count header is 3" $?
grep -q "feat/1-branch .*$TMP/wt-branch" <<<"$OUT"; check "branch worktree listed" $?
grep -q "(detached $SHA) .*$TMP/wt-detached" <<<"$OUT"; check "detached worktree listed" $?
grep -qF "$TMP/with space/wt-spaced" <<<"$OUT"; check "path with a space is not truncated" $?
rows="$(grep -c "$TMP" <<<"$OUT")"
[[ "$rows" -eq 3 ]]; check "row count ($rows) equals the header count" $?

if [[ "$fail" -eq 0 ]]; then echo "wt-list: all checks passed"; else echo "wt-list: $fail failed"; echo "$OUT"; exit 1; fi
