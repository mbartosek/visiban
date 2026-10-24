#!/bin/sh
# Upgrade pip to the hash-pinned floor and fail if it is older (#1927).
#
#   sh scripts/ensure-pip.sh              # install pinned pip, then assert version
#   sh scripts/ensure-pip.sh --self-test  # network-free check of the version gate
#
# Why: pip < 26.2.2 always uses its own vendored certifi, so the runtime
# `certifi` floor does not protect pip's TLS to PyPI against the eMudhra emSign
# CA incident (2026-10-20). python:3.12-slim still ships pip 26.2.1.
# REMOVABLE once the python:3.12-slim base image ships pip >= 26.2.2: delete
# this script, scripts/pip-requirements.txt, and the `.pip-floor` steps in
# .gitlab-ci.yml. (The Docker builders get pip from backend/requirements-build.lock.)
set -eu

PIP_MIN="26.2.2"

# Prints "ok" when $1 >= $2 (dotted numeric versions), else "old".
version_ge() {
  python3 - "$1" "$2" <<'PY'
import re, sys
def key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])
print("ok" if key(sys.argv[1]) >= key(sys.argv[2]) else "old")
PY
}

if [ "${1:-}" = "--self-test" ]; then
  # A gate must prove it can fail (#1093).
  [ "$(version_ge 26.2.1 "$PIP_MIN")" = "old" ] || { echo "self-test FAIL: 26.2.1 accepted" >&2; exit 1; }
  [ "$(version_ge 26.2.2 "$PIP_MIN")" = "ok" ] || { echo "self-test FAIL: 26.2.2 rejected" >&2; exit 1; }
  [ "$(version_ge 26.10 "$PIP_MIN")" = "ok" ] || { echo "self-test FAIL: 26.10 rejected" >&2; exit 1; }
  [ "$(version_ge 25.9.9 "$PIP_MIN")" = "old" ] || { echo "self-test FAIL: 25.9.9 accepted" >&2; exit 1; }
  echo "self-test OK"
  exit 0
fi

python3 -m pip install --quiet --disable-pip-version-check --require-hashes \
  -r "$(dirname "$0")/pip-requirements.txt"

have="$(python3 -m pip --version | awk '{print $2}')"
if [ "$(version_ge "$have" "$PIP_MIN")" != "ok" ]; then
  echo "pip ${have} is older than ${PIP_MIN} (eMudhra emSign CA incident, #1927)" >&2
  exit 1
fi
echo "pip ${have} >= ${PIP_MIN}"
