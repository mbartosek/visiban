#!/usr/bin/env bash
# scripts/tests/check-api-compat.test.sh
#
# CLI-contract test for scripts/check-api-compat.py (#1667). The gate's own
# `--self-test` covers the detection logic; this covers what CI depends on:
# exit codes for a removal, a type change, a required-ness change, an addition,
# an allow-listed removal, a stale/malformed allow-list, and a removed env var,
# all through the real command line against synthetic baselines.
#
# Run: bash scripts/tests/check-api-compat.test.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GATE="$REPO_ROOT/scripts/check-api-compat.py"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "SKIP: python3 not installed"; exit 0; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export TMPDIR="$TMP"

fail=0
pass=0
check() { # check "<description>" <condition-exit-code>
  if [[ "$2" -eq 0 ]]; then pass=$((pass + 1)); else echo "  FAIL: $1"; fail=$((fail + 1)); fi
}
run_gate() {
  set +e
  OUT="$("$PYTHON" "$GATE" "$@" 2>&1)"
  RC=$?
  set -e
}

# mk_schema <file> <props-json> <required-json> [<request-body-props-json> <request-required-json>]
mk_schema() {
  "$PYTHON" - "$@" <<'PY'
import json, sys
out, props, req = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
op = {"responses": {"200": {"content": {"application/json": {"schema": {
    "type": "object", "properties": props, "required": req}}}}}}
if len(sys.argv) > 4:
    op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "properties": json.loads(sys.argv[4]), "required": json.loads(sys.argv[5])}}}}
json.dump({"paths": {"/api/v1/x/": {"post" if len(sys.argv) > 4 else "get": op}}}, open(out, "w"))
PY
}

printf 'ALPHA\nBETA\n' > "$TMP/env_base.txt"
printf 'ALPHA = env("ALPHA")\nBETA = env.bool("BETA", default=False)\n' > "$TMP/settings_ok.py"
printf 'ALPHA = env("ALPHA")\n' > "$TMP/settings_missing.py"
echo '{"allow": []}' > "$TMP/allow_empty.json"

mk_schema "$TMP/base.json" '{"id":{"type":"integer"},"name":{"type":"string"}}' '["id","name"]'

gate() { # gate <head-schema> <settings> <allowlist>
  run_gate --baseline-schema "$TMP/base.json" --baseline-env "$TMP/env_base.txt" \
    --schema "$1" --settings "$2" --allowlist "$3"
}

# Unchanged contract passes.
gate "$TMP/base.json" "$TMP/settings_ok.py" "$TMP/allow_empty.json"
check "unchanged contract exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"

# Addition passes.
mk_schema "$TMP/add.json" '{"id":{"type":"integer"},"name":{"type":"string"},"extra":{"type":"string"}}' '["id","name"]'
gate "$TMP/add.json" "$TMP/settings_ok.py" "$TMP/allow_empty.json"
check "added response field exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"

# Removal fails and names the field.
mk_schema "$TMP/rm.json" '{"id":{"type":"integer"}}' '["id"]'
gate "$TMP/rm.json" "$TMP/settings_ok.py" "$TMP/allow_empty.json"
check "removed field exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "removed field is named" "$(grep -q 'REMOVED_RESPONSE_FIELD.*\.name' <<<"$OUT" && echo 0 || echo 1)"

# Type change fails.
mk_schema "$TMP/type.json" '{"id":{"type":"string"},"name":{"type":"string"}}' '["id","name"]'
gate "$TMP/type.json" "$TMP/settings_ok.py" "$TMP/allow_empty.json"
check "type change exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "type change is named" "$(grep -q 'TYPE_CHANGED.*integer->string' <<<"$OUT" && echo 0 || echo 1)"

# Required-ness: a request field becoming required fails.
mk_schema "$TMP/req_base.json" '{}' '[]' '{"a":{"type":"string"}}' '[]'
mk_schema "$TMP/req_head.json" '{}' '[]' '{"a":{"type":"string"}}' '["a"]'
run_gate --baseline-schema "$TMP/req_base.json" --baseline-env "$TMP/env_base.txt" \
  --schema "$TMP/req_head.json" --settings "$TMP/settings_ok.py" --allowlist "$TMP/allow_empty.json"
check "request field became required exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "required-ness finding is named" "$(grep -q 'REQUEST_FIELD_BECAME_REQUIRED' <<<"$OUT" && echo 0 || echo 1)"

# Allow-listed removal passes; the same entry goes stale once the field is back.
cat > "$TMP/allow_rm.json" <<'JSON'
{"allow": [{"key": "REMOVED_RESPONSE_FIELD GET /api/v1/x/ response 200 application/json .name",
            "issue": "#1667", "reason": "test"}]}
JSON
gate "$TMP/rm.json" "$TMP/settings_ok.py" "$TMP/allow_rm.json"
check "allow-listed removal exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"
gate "$TMP/base.json" "$TMP/settings_ok.py" "$TMP/allow_rm.json"
check "stale allow-list entry exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "stale entry is reported" "$(grep -q STALE <<<"$OUT" && echo 0 || echo 1)"

# An allow-list entry without an issue reference is rejected outright.
cat > "$TMP/allow_noissue.json" <<'JSON'
{"allow": [{"key": "REMOVED_RESPONSE_FIELD GET /api/v1/x/ response 200 application/json .name", "reason": "test"}]}
JSON
gate "$TMP/rm.json" "$TMP/settings_ok.py" "$TMP/allow_noissue.json"
check "allow-list entry without issue exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"

# Removed env var fails; an added one passes.
gate "$TMP/base.json" "$TMP/settings_missing.py" "$TMP/allow_empty.json"
check "removed env var exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "removed env var is named" "$(grep -q 'REMOVED_ENV_VAR BETA' <<<"$OUT" && echo 0 || echo 1)"

# Fail closed on a missing baseline.
run_gate --baseline-schema "$TMP/nope.json" --baseline-env "$TMP/env_base.txt" --schema "$TMP/base.json"
check "missing baseline does not exit 0" "$([[ $RC -ne 0 ]] && echo 0 || echo 1)"

# Built-in self-test.
run_gate --self-test
check "--self-test exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"

echo "check-api-compat.test.sh: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
