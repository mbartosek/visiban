#!/usr/bin/env bash
# scripts/tests/check-suppression-issues.test.sh
#
# Unit test for scripts/check-suppression-issues.sh — the SUPPRESSED-UNTIL(#N)
# marker gate (#1092). The gate's whole job is to fail when a suppression
# cites an issue that has since closed; a regression here would let it go
# quietly green forever, exactly the failure mode the gate exists to prevent
# (see #1090, where 0% coverage on git_lens went unnoticed for months).
#
# This test drives the script's own CHECK_SUPPRESSION_MOCK_CLOSED test hook
# (the same one --self-test uses) against small fixture trees, so it needs no
# network access and depends on no real GitLab issue existing or closing.
#
# #1694 added a second kind of case below (see "REAL FETCH PATH"): the mock
# hook never exercised check-suppression-issues.sh's actual GitLab API call,
# which is exactly how the JOB-TOKEN-401 defect went unnoticed — those cases
# drive gitlab_issue_states through a fake `curl` placed first on PATH
# instead, with no mock hook involved, proving the real request shape:
# unique-iid batching, a closed issue still blocking, an open issue still
# passing, and JOB-TOKEN never sent even when CI_JOB_TOKEN is set.
#
# Run: bash scripts/tests/check-suppression-issues.test.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATE="$REPO_ROOT/scripts/check-suppression-issues.sh"
LIB="$REPO_ROOT/scripts/lib/gitlab-issues.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail=0
pass=0
check() { # check "<description>" <0-if-true>
  local desc="$1" rc="$2"
  if [[ "$rc" -eq 0 ]]; then
    pass=$((pass + 1))
  else
    echo "  FAIL: $desc"
    fail=$((fail + 1))
  fi
}

# --- Fixture trees ----------------------------------------------------------
mkdir -p "$TMP/closed-hit" "$TMP/open-hit" "$TMP/plain-reason" "$TMP/mixed" "$TMP/backend-style"

cat > "$TMP/closed-hit/Component.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- SUPPRESSED-UNTIL(#4242): fixture only
EOF

cat > "$TMP/open-hit/Component.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- SUPPRESSED-UNTIL(#5150): fixture only
EOF

cat > "$TMP/plain-reason/Component.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- deps intentionally narrowed, permanent
EOF

cat > "$TMP/mixed/a.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- SUPPRESSED-UNTIL(#4242): fixture only
EOF
cat > "$TMP/mixed/b.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- SUPPRESSED-UNTIL(#5150): fixture only
EOF

cat > "$TMP/backend-style/test_thing.py" <<'EOF'
    @pytest.mark.skip(reason="SUPPRESSED-UNTIL(#4242): fixture only, arm64 runners unavailable")
    def test_arm64_only_path(self):
        pass
EOF

# The gate always scans SCAN_PATHS ("frontend/src backend"), not an arbitrary
# directory, so to test it against small fixture trees instead of the whole
# real repo we source its function definitions (run_scan, resolve_issue_state)
# and call run_scan directly. The gate is bash; the only wrinkle is its
# trailing `main "$@"` line, which would run on source too and scan the real
# repo as a side effect — so we source a scratch copy with that last line
# stripped instead of the gate file itself.
#
# The sourced file also defines and immediately runs its own top-level
# `REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$REPO_ROOT"`
# (outside any function) AND, since #1694, a `source
# "${REPO_ROOT}/scripts/lib/gitlab-issues.sh"` that depends on that
# REPO_ROOT being right — unlike `$0`, `${BASH_SOURCE[0]}` DOES follow a
# sourced file, so sourcing a bare copy dropped directly into $TMP computes
# a REPO_ROOT that is wrong *and consequential*: the inner `source` fails
# immediately, before we would ever get a chance to restore anything. Fixed
# by mirroring the real scripts/ layout under $TMP (copy the library to the
# same relative path) so that inner source resolves correctly either way,
# then overwriting our own $REPO_ROOT/cwd back to the real repo root
# afterward regardless.
OWN_REPO_ROOT="$REPO_ROOT"
mkdir -p "$TMP/scripts/lib"
cp "$LIB" "$TMP/scripts/lib/gitlab-issues.sh"
GATE_LIB="$TMP/scripts/gate_lib.sh"
sed '$ d' "$GATE" > "$GATE_LIB"   # drop the trailing `main "$@"` line
# shellcheck source=/dev/null
source "$GATE_LIB"
REPO_ROOT="$OWN_REPO_ROOT"
cd "$REPO_ROOT"

# run_case <mock-closed-csv> <dir> — runs run_scan against a fixture
# directory with a mocked closed-issue list, capturing combined output +
# exit code into $CASE_OUT / $CASE_RC.
run_case() {
  local mock_closed="$1" dir="$2"
  set +e
  CASE_OUT="$(CHECK_SUPPRESSION_MOCK_CLOSED="$mock_closed" run_scan "$dir" 2>&1)"
  CASE_RC=$?
  set -e
}

# --- A marker citing a closed issue blocks, naming file/line/issue ---------
run_case "4242" "$TMP/closed-hit"
check "closed-issue marker blocks (exit 1)" "$([ "$CASE_RC" -eq 1 ] && echo 0 || echo 1)"
check "failure names the file" "$(echo "$CASE_OUT" | grep -q "Component.tsx" && echo 0 || echo 1)"
check "failure names the issue number" "$(echo "$CASE_OUT" | grep -q "#4242" && echo 0 || echo 1)"
check "failure says CLOSED" "$(echo "$CASE_OUT" | grep -q "CLOSED" && echo 0 || echo 1)"

# --- A marker citing an open issue passes -----------------------------------
run_case "4242" "$TMP/open-hit"
check "open-issue marker passes (exit 0)" "$([ "$CASE_RC" -eq 0 ] && echo 0 || echo 1)"

# --- A plain '-- reason' suppression with no marker is left alone ----------
run_case "4242" "$TMP/plain-reason"
check "plain-reason suppression (no marker) is never flagged, even with matching mock-closed list" \
  "$([ "$CASE_RC" -eq 0 ] && echo 0 || echo 1)"

# --- Mixed tree: one closed hit among files blocks and reports both --------
run_case "4242" "$TMP/mixed"
check "mixed tree blocks when any marker cites a closed issue" "$([ "$CASE_RC" -eq 1 ] && echo 0 || echo 1)"
check "mixed tree reports the closed citation (a.tsx)" "$(echo "$CASE_OUT" | grep -q "a.tsx" && echo 0 || echo 1)"

# --- Backend pytest-style marker (not just eslint-disable) is also caught --
run_case "4242" "$TMP/backend-style"
check "pytest.mark.skip reason= marker is detected the same way as eslint-disable" \
  "$([ "$CASE_RC" -eq 1 ] && echo 0 || echo 1)"

# --- Clean tree (no markers at all) passes ----------------------------------
mkdir -p "$TMP/clean"
cat > "$TMP/clean/nothing.tsx" <<'EOF'
export const x = 1;
EOF
run_case "4242" "$TMP/clean"
check "a tree with no SUPPRESSED-UNTIL marker at all passes" "$([ "$CASE_RC" -eq 0 ] && echo 0 || echo 1)"

# --- A missing scan path blocks instead of silently reporting clean --------
# Regression coverage: `grep -r` on a nonexistent path errors on stderr
# (discarded) and `|| true` would otherwise swallow that, so a future rename
# of frontend/src or backend must not turn this gate into a silent no-op.
run_case "4242" "$TMP/this-path-does-not-exist"
check "a missing scan path blocks (exit 1), not a false-green clean scan" \
  "$([ "$CASE_RC" -eq 1 ] && echo 0 || echo 1)"
check "a missing scan path names itself in the failure" \
  "$(echo "$CASE_OUT" | grep -q "does not exist" && echo 0 || echo 1)"

# --- The gate's own --self-test mode passes as a subprocess (no network) ---
set +e
SELFTEST_OUT="$(bash "$GATE" --self-test 2>&1)"
SELFTEST_RC=$?
set -e
check "the gate's own --self-test exits 0" "$([ "$SELFTEST_RC" -eq 0 ] && echo 0 || echo 1)"
check "the gate's own --self-test reports OK" "$(echo "$SELFTEST_OUT" | grep -q "self-test: OK" && echo 0 || echo 1)"

# === REAL FETCH PATH (#1694) ================================================
#
# Everything above drives CHECK_SUPPRESSION_MOCK_CLOSED, which never touches
# gitlab_issue_states or curl at all — exactly the blind spot that let the
# JOB-TOKEN/401 defect ship unnoticed. These cases instead put a fake `curl`
# first on PATH and run the gate's *actual* fetch path end to end, with
# CHECK_SUPPRESSION_MOCK_CLOSED deliberately unset.
install_fake_curl() {
  local dir="$1" body="$2"
  mkdir -p "$dir"
  {
    echo '#!/usr/bin/env bash'
    # shellcheck disable=SC2016
    echo 'echo "$*" >> "$CALL_LOG"'
    echo "$body"
  } > "$dir/curl"
  chmod +x "$dir/curl"
}

FAKE_CURL_DIR="$TMP/fake-curl-real-path"
# shellcheck disable=SC2016
install_fake_curl "$FAKE_CURL_DIR" '
for a in "$@"; do
  case "$a" in
    JOB-TOKEN:*) echo "JOB-TOKEN SENT: $a" >&2; exit 99 ;;
  esac
done
printf '"'"'[{"iid":6060,"state":"opened"},{"iid":6161,"state":"closed"}]'"'"'
printf '"'"'\nHTTPSTATUS:200'"'"'
'

mkdir -p "$TMP/real-fetch/open" "$TMP/real-fetch/closed"
cat > "$TMP/real-fetch/open/a.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- SUPPRESSED-UNTIL(#6060): fixture only, real-fetch-path case
EOF
cat > "$TMP/real-fetch/closed/b.tsx" <<'EOF'
// eslint-disable-next-line react-hooks/exhaustive-deps -- SUPPRESSED-UNTIL(#6161): fixture only, real-fetch-path case
EOF

REAL_CALL_LOG="$TMP/real-fetch-calls.log"
: > "$REAL_CALL_LOG"
set +e
REAL_OUT="$(
  unset CHECK_SUPPRESSION_MOCK_CLOSED
  export CALL_LOG="$REAL_CALL_LOG"
  export CI_JOB_TOKEN="definitely-set-but-must-never-be-sent"
  export CI_PROJECT_ID="visiban%2Fvisiban"
  unset GITLAB_API_TOKEN ISSUES_API_TOKEN
  PATH="$FAKE_CURL_DIR:$PATH" run_scan "$TMP/real-fetch" 2>&1
)"
REAL_RC=$?
set -e

check "real fetch path: closed issue blocks through the real curl call (exit 1)" \
  "$([ "$REAL_RC" -eq 1 ] && echo 0 || echo 1)"
check "real fetch path: failure names the closed file" \
  "$(echo "$REAL_OUT" | grep -q "b.tsx" && echo 0 || echo 1)"
check "real fetch path: failure names the closed issue and says CLOSED" \
  "$(echo "$REAL_OUT" | grep -q "#6161" && echo "$REAL_OUT" | grep -q "CLOSED" && echo 0 || echo 1)"
check "real fetch path: the open issue's file is never reported as a finding" \
  "$(echo "$REAL_OUT" | grep -q "a.tsx" && echo 1 || echo 0)"
check "real fetch path: both cited iids reached curl in one batched call" \
  "$(grep -q 'iids\[\]=6060' "$REAL_CALL_LOG" && grep -q 'iids\[\]=6161' "$REAL_CALL_LOG" && echo 0 || echo 1)"
check "real fetch path: exactly one HTTP call made (batched, not per-marker)" \
  "$([ "$(grep -c 'per_page=100' "$REAL_CALL_LOG")" -eq 1 ] && echo 0 || echo 1)"
check "real fetch path: JOB-TOKEN was never sent even though CI_JOB_TOKEN was set" \
  "$(grep -q 'JOB-TOKEN' "$REAL_CALL_LOG" && echo 1 || echo 0)"

echo ""
if [[ "$fail" -eq 0 ]]; then
  echo "check-suppression-issues.test.sh: all $pass checks passed"
  exit 0
else
  echo "check-suppression-issues.test.sh: $fail failed, $pass passed"
  exit 1
fi
