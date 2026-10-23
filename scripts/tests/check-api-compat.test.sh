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
echo '{"experimental": []}' > "$TMP/exp_empty.json"

mk_schema "$TMP/base.json" '{"id":{"type":"integer"},"name":{"type":"string"}}' '["id","name"]'

gate() { # gate <head-schema> <settings> <allowlist>
  run_gate --baseline-schema "$TMP/base.json" --baseline-env "$TMP/env_base.txt" \
    --schema "$1" --settings "$2" --allowlist "$3" --experimental "$TMP/exp_empty.json"
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
  --schema "$TMP/req_head.json" --settings "$TMP/settings_ok.py" --allowlist "$TMP/allow_empty.json" \
  --experimental "$TMP/exp_empty.json"
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

# Experimental operations (#1875). mk_exp builds a schema whose only operation
# lives under /api/v1/exp/; a break there passes when the path is excluded.
mk_exp() { # mk_exp <file> <props-json> <required-json>
  "$PYTHON" - "$@" <<'PY'
import json, sys
out, props, req = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3])
ok = {"responses": {"200": {"content": {"application/json": {"schema": {
    "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}}}}}}
exp = {"responses": {"200": {"content": {"application/json": {"schema": {
    "type": "object", "properties": props, "required": req}}}}}}
json.dump({"paths": {"/api/v1/x/": {"get": ok}, "/api/v1/exp/a/": {"get": exp}}}, open(out, "w"))
PY
}
mk_exp "$TMP/exp_base.json" '{"id":{"type":"integer"},"name":{"type":"string"}}' '["id","name"]'
mk_exp "$TMP/exp_broken.json" '{"id":{"type":"string"}}' '["id"]'
echo '{"experimental": [{"path_pattern": "/api/v1/exp/*", "issue": "#1875", "reason": "test"}]}' > "$TMP/exp_ok.json"
echo '{"experimental": [{"path_pattern": "/api/v1/nope/*", "issue": "#1875", "reason": "test"}]}' > "$TMP/exp_dead.json"
echo '{"experimental": [{"path_pattern": "/api/v1/exp/*", "reason": "test"}]}' > "$TMP/exp_noissue.json"
exp_gate() { # exp_gate <head-schema> <experimental-file>
  run_gate --baseline-schema "$TMP/exp_base.json" --baseline-env "$TMP/env_base.txt" \
    --schema "$1" --settings "$TMP/settings_ok.py" --allowlist "$TMP/allow_empty.json" --experimental "$2"
}
exp_gate "$TMP/exp_broken.json" "$TMP/exp_ok.json"
check "break in experimental op exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"
check "skipped experimental op is printed" "$(grep -q 'GET /api/v1/exp/a/' <<<"$OUT" && echo 0 || echo 1)"
exp_gate "$TMP/exp_broken.json" "$TMP/allow_empty.json"
check "same break without exclusion exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
# A break in a NON-excluded op still fails while the experimental one is excluded.
"$PYTHON" - "$TMP/exp_base.json" "$TMP/exp_other_broken.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
d["paths"]["/api/v1/x/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"] = {}
d["paths"]["/api/v1/x/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["required"] = []
json.dump(d, open(sys.argv[2], "w"))
PY
exp_gate "$TMP/exp_other_broken.json" "$TMP/exp_ok.json"
check "break in non-excluded op still exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "non-excluded break is named" "$(grep -q 'REMOVED_RESPONSE_FIELD.*/api/v1/x/' <<<"$OUT" && echo 0 || echo 1)"
exp_gate "$TMP/exp_base.json" "$TMP/exp_dead.json"
check "dead experimental entry exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
check "dead experimental entry is reported" "$(grep -q DEAD <<<"$OUT" && echo 0 || echo 1)"
exp_gate "$TMP/exp_base.json" "$TMP/exp_noissue.json"
check "experimental entry without issue exits 1" "$([[ $RC -eq 1 ]] && echo 0 || echo 1)"
# --normalize strips experimental ops so a rolled baseline never freezes them.
run_gate --normalize "$TMP/exp_base.json" "$TMP/exp_norm.json" --experimental "$TMP/exp_ok.json"
check "--normalize exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"
check "--normalize drops experimental path" "$(grep -q '/api/v1/exp/' "$TMP/exp_norm.json" && echo 1 || echo 0)"
check "--normalize keeps other paths" "$(grep -q '/api/v1/x/' "$TMP/exp_norm.json" && echo 0 || echo 1)"

# Built-in self-test.
run_gate --self-test
check "--self-test exits 0" "$([[ $RC -eq 0 ]] && echo 0 || echo 1)"

echo "check-api-compat.test.sh: $pass passed, $fail failed"
[[ $fail -eq 0 ]]
