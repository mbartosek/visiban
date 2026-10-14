#!/usr/bin/env bash
# scripts/tests/registry-keep-regex.test.sh — pins which registry tags the GitLab
# container-registry cleanup policy's `name_regex_keep` protects (#1196, #1541).
#
# The policy is a GitLab project setting, not a file in this repo; the only
# record of it is docs/administration/container-image-retention.md. This test
# reads every `name_regex_keep` value recorded there (the live policy and the
# #1541 target policy) and checks it against the tag shapes the release
# pipeline actually pushes:
#
#   * release tags (vX.Y.Z, vX.Y.Z-alpha|beta|rc.N) and `latest` must be kept;
#   * the per-arch working tags `<tag>-amd64` / `<tag>-arm64` must NOT be kept,
#     so the ordinary keep_n/older_than sweep removes them (#1196). Removing the
#     TAG is safe: the release index references the platform manifest by digest;
#   * the target policy must keep cosign's `sha256-<64 hex>.sig` / `.att` tags,
#     which carry the signatures and SBOM attestations of those same platform
#     digests (#1153, #1541), and nothing looser than that exact shape.
#
# It also checks that .gitlab-ci.yml still names the per-arch tags with the
# `-amd64` / `-arm64` suffix, so the sample tags below cannot silently stop
# matching what the pipeline pushes.
#
# GitLab matches name_regex_keep against the whole tag name (`\z` = end of
# string); Python's re.fullmatch with `\z` rewritten to `\Z` has the same
# semantics for these patterns.
#
# Run: bash scripts/tests/registry-keep-regex.test.sh   (needs python3)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOC="${KEEP_REGEX_DOC:-$REPO_ROOT/docs/administration/container-image-retention.md}"
CI_FILE="${KEEP_REGEX_CI_FILE:-$REPO_ROOT/.gitlab-ci.yml}"

python3 -I - "$DOC" "$CI_FILE" <<'PY'
import json
import re
import sys

doc_path, ci_path = sys.argv[1], sys.argv[2]
doc = open(doc_path, encoding="utf-8").read()
ci = open(ci_path, encoding="utf-8").read()

passed = 0
failed = 0


def check(desc, ok):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {desc}")


# Every "name_regex_keep": "<json string>" in the doc (JSON policy blocks and
# the glab api PUT bodies), JSON-unescaped to the actual regex text.
raw = re.findall(r'"name_regex_keep":\s*"((?:[^"\\]|\\.)*)"', doc)
regexes = sorted({json.loads('"' + r + '"') for r in raw})
live = [r for r in regexes if "sig" not in r]
target = [r for r in regexes if "sig" in r]
check(f"doc records exactly one live and one #1541 target keep-regex (found {regexes})",
      len(live) == 1 and len(target) == 1)
if not (live and target):
    print(f"registry-keep-regex: {passed} passed, {failed} failed")
    sys.exit(1)
live, target = live[0], target[0]

# The plain-text copy of the target regex shown to maintainers must agree.
text_blocks = re.findall(r"```text\n(.*?)\n```", doc, re.S)
check("plain-text target regex in the doc matches the JSON one",
      target in [b.strip() for b in text_blocks])


def compile_gitlab(pattern):
    # \z (RE2/Ruby end of string) -> \Z (Python end of string), escape-aware.
    py = re.sub(r"(?<!\\)((?:\\\\)*)\\z", r"\1\\Z", pattern)
    return re.compile(py)


LIVE = compile_gitlab(live)
TARGET = compile_gitlab(target)


def kept(rx, tag):
    full = rx.fullmatch(tag) is not None
    # GitLab anchors the whole name; the patterns carry their own ^...\z too.
    # Both readings must agree, or the doc's regex is ambiguous.
    check(f"{rx.pattern!r}: fullmatch and search agree on {tag!r}",
          full == (rx.search(tag) is not None))
    return full


# The suffix scheme the sample tags below assume must still be the one CI uses.
check(".gitlab-ci.yml amd64 leg pushes <tag>-amd64",
      "${CI_COMMIT_TAG}-amd64" in ci)
check(".gitlab-ci.yml arm64 legs push <tag>-arm64",
      "${TAG}-arm64" in ci)
check(".gitlab-ci.yml manifest jobs assemble from the <tag>-ARCH template",
      '--template "${REG}/${IMG}:${TAG}-ARCH"' in ci
      and "--platforms linux/amd64,linux/arm64" in ci)

HEX = "0123456789abcdef" * 4  # 64 lowercase hex chars
releases = ["v1.2.0", "v1.10.3", "v1.2.0-alpha.4", "v1.2.0-beta.1",
            "v1.2.0-rc.3", "v2.0.0-rc.12"]
per_arch = [f"{t}-{a}" for t in releases for a in ("amd64", "arm64")]
cosign = [f"sha256-{HEX}.sig", f"sha256-{HEX}.att"]
never = [
    "main", "a1b2c3d4", "1.2", "v1.2", "v1.2.0-dev", "latest-amd64",
    "v1.2.0-amd64-arm64", "v1.2.0\n", "xv1.2.0", "latest\n",
    f"sha256-{HEX}", f"sha256-{HEX}.sbom", f"sha256-{HEX[:-1]}.sig",
    f"sha256-{HEX}0.sig", f"sha256-{HEX.upper()}.sig", f"sha256-{HEX}.sig-amd64",
    f"x-sha256-{HEX}.sig",
]

for name, rx in (("live", LIVE), ("target", TARGET)):
    for t in releases + ["latest"]:
        check(f"{name}: release/latest tag {t!r} is kept", kept(rx, t))
    for t in per_arch:
        check(f"{name}: per-arch working tag {t!r} is NOT kept (#1196)",
              not kept(rx, t))
    for t in never:
        check(f"{name}: {t!r} is NOT kept", not kept(rx, t))

for t in cosign:
    check(f"target: cosign tag {t!r} is kept (#1541)", kept(TARGET, t))
    # Documented gap until #1541 is applied — flip this when the doc records
    # the target as the live policy.
    check(f"live: cosign tag {t!r} is not kept yet (#1541 pending)",
          not kept(LIVE, t))

print(f"registry-keep-regex: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
PY
