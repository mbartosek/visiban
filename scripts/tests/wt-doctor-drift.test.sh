#!/usr/bin/env bash
# scripts/tests/wt-doctor-drift.test.sh
#
# `wt doctor` must warn when the dev venv's installed versions differ from the
# `==` pins in backend/requirements.txt (#1715), naming the package and both
# versions, and stay quiet when there is no drift. Hermetic: a fake interpreter
# stands in for backend/.venv/bin/python, so the real venv is never consulted.
#
# Run: bash scripts/tests/wt-doctor-drift.test.sh

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
mkdir -p "$D/backend"
git -C "$D" init -q -b main
git -C "$D" config user.email t@t.co
git -C "$D" config user.name t
cat > "$D/backend/requirements.txt" <<'REQ'
# comment line
Django>=5.2.15,<6
mcp==2.3.0
psycopg[binary]==3.3.6   # trailing comment
Some_Pkg==1.0
pywin32==306 ; sys_platform == "win32"
-r other.txt
missing-pkg==9.9.9
REQ
echo base > "$D/README.md"
git -C "$D" add -A
git -C "$D" commit -qm init
# wt resolves the default branch from origin/HEAD, so the repo needs an origin.
git init -q --bare "$TMP/origin.git"
git -C "$D" remote add origin "$TMP/origin.git"
git -C "$D" push -q origin main
git -C "$D" fetch -q origin
git -C "$D" remote set-head origin main

# Fake interpreter: answers `-m pip list --format=json` from $FAKE_PIP_JSON.
FAKE="$TMP/fake-python"
cat > "$FAKE" <<'PY'
#!/usr/bin/env bash
cat "$FAKE_PIP_JSON"
PY
chmod +x "$FAKE"
export WT_VENV_PYTHON="$FAKE"

run_doctor() { (cd "$D" && FAKE_PIP_JSON="$1" bash "$WT" doctor 2>&1); }

# Drift: old mcp, old psycopg, missing-pkg absent; Some_Pkg matches via PEP 503 normalization.
cat > "$TMP/drift.json" <<'J'
[{"name":"mcp","version":"1.30.0"},{"name":"psycopg","version":"3.2.0"},{"name":"some-pkg","version":"1.0"},{"name":"Django","version":"5.2.15"}]
J
OUT="$(run_doctor "$TMP/drift.json")"
grep -q "mcp: requires 2.3.0, installed 1.30.0" <<<"$OUT"; check "mcp drift named with both versions" $?
grep -q "psycopg: requires 3.3.6, installed 3.2.0" <<<"$OUT"; check "psycopg drift (extras pin) reported" $?
grep -q "missing-pkg: requires 9.9.9, installed NOT INSTALLED" <<<"$OUT"; check "missing package reported" $?
! grep -q "Some_Pkg" <<<"$OUT"; check "normalized-name match is not drift" $?
! grep -q "pywin32" <<<"$OUT"; check "marker line skipped" $?
! grep -q "Django" <<<"$OUT"; check "range spec not checked" $?
grep -q "pip install -r backend/requirements.txt" <<<"$OUT"; check "fix hint printed" $?

# Negative control: everything in sync -> no drift warning.
cat > "$TMP/ok.json" <<'J'
[{"name":"mcp","version":"2.3.0"},{"name":"psycopg","version":"3.3.6"},{"name":"Some-Pkg","version":"1.0"},{"name":"missing_pkg","version":"9.9.9"}]
J
OUT="$(run_doctor "$TMP/ok.json")"
! grep -q "venv drift" <<<"$OUT"; check "no drift warning when in sync" $?
grep -q "backend venv matches requirements.txt pins" <<<"$OUT"; check "in-sync confirmation printed" $?

if [[ "$fail" -eq 0 ]]; then echo "wt-doctor-drift: all checks passed"; else echo "wt-doctor-drift: $fail failed"; echo "$OUT"; exit 1; fi
