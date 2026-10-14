#!/usr/bin/env bash
# scripts/tests/registry-keep-regex.test.sh — pins which registry tags the GitLab
# container-registry cleanup policy's `name_regex_keep` protects (#1196, #1541).
#
# The policy is a GitLab project setting, not a file in this repo; the only
# record of it is docs/administration/container-image-retention.md. This test
# reads two specific `name_regex_keep` values from that page:
#
#   * LIVE   — the recorded-policy JSON block (the one carrying `next_run_at`)
#              directly under "## GitLab container registry";
#   * TARGET — the resulting-policy JSON block under "### #1541 update", which
#              must agree with that section's `glab api` PUT body and with the
#              plain-text copy shown to maintainers.
#
# Both are checked against the tag shapes the release pipeline actually pushes:
#
#   * release tags (vX.Y.Z, vX.Y.Z-alpha|beta|rc.N) and `latest` must be kept;
#   * the per-arch working tags `<tag>-amd64` / `<tag>-arm64` must NOT be kept,
#     so the ordinary keep_n/older_than sweep removes them (#1196). Removing the
#     TAG is safe: the release index references the platform manifest by digest;
#   * the target policy must keep cosign's `sha256-<64 hex>.sig` / `.att` tags,
#     which carry the signatures and SBOM attestations of those same platform
#     digests (#1153, #1541), and nothing looser than that exact shape;
#   * the live policy must NOT keep them yet (#1541 is not applied). When #1541
#     is applied and the live block is updated, this case goes red on purpose:
#     flip it in the same change as the other #1541-stale passages.
#
# A negative control re-runs the checks on a copy of the page whose live block
# holds the target regex and asserts the "not kept yet" case goes red, so the
# test cannot keep passing after the live policy changes.
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
DOC = open(doc_path, encoding="utf-8").read()
CI = open(ci_path, encoding="utf-8").read()

KEEP_RE = r'"name_regex_keep":\s*"((?:[^"\\]|\\.)*)"'
JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def section(doc, heading):
    """Text of a markdown section from its heading to the next heading of the
    same or higher level ('' if absent)."""
    m = re.search(r"^" + re.escape(heading) + r"[ \t]*$", doc, re.M)
    if not m:
        return ""
    level = len(heading) - len(heading.lstrip("#"))
    nxt = re.compile(r"^#{1,%d} " % level, re.M).search(doc, m.end())
    return doc[m.end():nxt.start() if nxt else len(doc)]


def unescape(raw):
    return json.loads('"' + raw + '"')


def live_block(doc):
    """The recorded live-policy JSON block: first ```json block in the GitLab
    section, before its first ### subsection, that carries next_run_at."""
    top = section(doc, "## GitLab container registry")
    top = re.split(r"^### ", top, maxsplit=1, flags=re.M)[0]
    for b in JSON_BLOCK.findall(top):
        if '"next_run_at"' in b:
            return b
    return None


def compile_gitlab(pattern):
    # \z (end of string) -> \Z (Python end of string), escape-aware.
    return re.compile(re.sub(r"(?<!\\)((?:\\\\)*)\\z", r"\1\\Z", pattern))


HEX = "0123456789abcdef" * 4  # 64 lowercase hex chars
RELEASES = ["v1.2.0", "v1.10.3", "v1.2.0-alpha.4", "v1.2.0-beta.1",
            "v1.2.0-rc.3", "v2.0.0-rc.12"]
PER_ARCH = [f"{t}-{a}" for t in RELEASES for a in ("amd64", "arm64")]
COSIGN = [f"sha256-{HEX}.sig", f"sha256-{HEX}.att"]
NEVER = [
    "main", "a1b2c3d4", "1.2", "v1.2", "v1.2.0-dev", "latest-amd64",
    "v1.2.0-amd64-arm64", "v1.2.0\n", "xv1.2.0", "latest\n",
    f"sha256-{HEX}", f"sha256-{HEX}.sbom", f"sha256-{HEX[:-1]}.sig",
    f"sha256-{HEX}0.sig", f"sha256-{HEX.upper()}.sig", f"sha256-{HEX}.sig-amd64",
    f"x-sha256-{HEX}.sig",
]


def evaluate(doc, ci):
    """Run every check; return (passed, [failure descriptions])."""
    results = []

    def check(desc, ok):
        results.append((desc, bool(ok)))

    lb = live_block(doc)
    live_vals = re.findall(KEEP_RE, lb) if lb else []
    check("live recorded-policy block found with exactly one name_regex_keep",
          len(live_vals) == 1)

    s1541 = section(doc, "### #1541 update: cosign `.sig`/`.att` tags added to the keep-regex")
    t_json = [v for b in JSON_BLOCK.findall(s1541) for v in re.findall(KEEP_RE, b)]
    t_put = re.findall(KEEP_RE, re.sub(r"```json\n.*?\n```", "", s1541, flags=re.S))
    check("#1541 section has exactly one resulting-policy name_regex_keep",
          len(t_json) == 1)
    if len(live_vals) != 1 or len(t_json) != 1:
        return sum(ok for _, ok in results), [d for d, ok in results if not ok]
    live, target = unescape(live_vals[0]), unescape(t_json[0])
    check("#1541 PUT body regex matches its resulting-policy regex",
          [unescape(v) for v in t_put] == [target])
    text_blocks = re.findall(r"```text\n(.*?)\n```", doc, re.S)
    check("plain-text target regex in the doc matches the #1541 one",
          target in [b.strip() for b in text_blocks])

    LIVE, TARGET = compile_gitlab(live), compile_gitlab(target)

    def kept(rx, tag):
        full = rx.fullmatch(tag) is not None
        # GitLab anchors the whole name; the patterns carry their own ^...\z
        # too. Both readings must agree, or the doc's regex is ambiguous.
        check(f"{rx.pattern!r}: fullmatch and search agree on {tag!r}",
              full == (rx.search(tag) is not None))
        return full

    # The suffix scheme the sample tags assume must still be the one CI uses.
    check(".gitlab-ci.yml amd64 leg pushes <tag>-amd64", "${CI_COMMIT_TAG}-amd64" in ci)
    check(".gitlab-ci.yml arm64 legs push <tag>-arm64", "${TAG}-arm64" in ci)
    check(".gitlab-ci.yml manifest jobs assemble from the <tag>-ARCH template",
          '--template "${REG}/${IMG}:${TAG}-ARCH"' in ci
          and "--platforms linux/amd64,linux/arm64" in ci)

    for name, rx in (("live", LIVE), ("target", TARGET)):
        for t in RELEASES + ["latest"]:
            check(f"{name}: release/latest tag {t!r} is kept", kept(rx, t))
        for t in PER_ARCH:
            check(f"{name}: per-arch working tag {t!r} is NOT kept (#1196)",
                  not kept(rx, t))
        for t in NEVER:
            check(f"{name}: {t!r} is NOT kept", not kept(rx, t))

    for t in COSIGN:
        check(f"target: cosign tag {t!r} is kept (#1541)", kept(TARGET, t))
        check(f"live: cosign tag {t!r} is not kept yet (#1541 pending)",
              not kept(LIVE, t))

    return sum(ok for _, ok in results), [d for d, ok in results if not ok]


passed, failures = evaluate(DOC, CI)
for f in failures:
    print(f"  FAIL: {f}")

# Negative control: simulate #1541 being applied to the live block without
# updating this test. The "not kept yet" case must go red.
ctl_passed = ctl_failed = 0
lb = live_block(DOC)
s1541 = section(DOC, "### #1541 update: cosign `.sig`/`.att` tags added to the keep-regex")
t_raw = [v for b in JSON_BLOCK.findall(s1541) for v in re.findall(KEEP_RE, b)]
if lb and t_raw:
    mutated_block = re.sub(KEEP_RE, lambda m: f'"name_regex_keep": "{t_raw[0]}"', lb)
    _, ctl = evaluate(DOC.replace(lb, mutated_block, 1), CI)
    if any("not kept yet" in f for f in ctl):
        ctl_passed += 1
    else:
        ctl_failed += 1
        print("  FAIL: negative control: live block holding the #1541 target "
              "regex did not turn the 'not kept yet' case red")
    # And a live regex that loses its \z anchor must let per-arch tags through.
    unanchored = re.sub(KEEP_RE, lambda m: '"name_regex_keep": "v.*"', lb)
    _, ctl = evaluate(DOC.replace(lb, unanchored, 1), CI)
    if any("per-arch working tag" in f for f in ctl):
        ctl_passed += 1
    else:
        ctl_failed += 1
        print("  FAIL: negative control: an unanchored live regex was not caught")
else:
    ctl_failed += 1
    print("  FAIL: negative control could not locate the live or #1541 block")

total_failed = len(failures) + ctl_failed
print(f"registry-keep-regex: {passed + ctl_passed} passed, {total_failed} failed")
sys.exit(1 if total_failed else 0)
PY
