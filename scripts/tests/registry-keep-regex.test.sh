#!/usr/bin/env bash
# scripts/tests/registry-keep-regex.test.sh — pins which registry tags the GitLab
# container-registry cleanup policy's `name_regex_keep` protects (#1196, #1541).
#
# The policy is a GitLab project setting, not a file in this repo; the only
# record of it is docs/administration/container-image-retention.md. This test
# reads these regexes from that page and nothing else:
#
#   * LIVE  — the recorded-policy JSON block (the one carrying `next_run_at`)
#             directly under "## GitLab container registry", before its first
#             `###` subsection;
#   * #1541 — the PUT body in "### #1541 update"; it set the live policy, so it
#             must equal LIVE;
#   * #1190 — the PUT body in "### #1190 update" (historical);
#   * `v.*` — the unanchored form named in "### History" (historical).
#
# LIVE is checked against the tag shapes the release pipeline actually pushes:
#
#   * release tags (vX.Y.Z, vX.Y.Z-alpha|beta|rc.N) and `latest` are kept;
#   * the per-arch working tags `<tag>-amd64` / `<tag>-arm64` are NOT kept, so
#     the ordinary keep_n/older_than sweep removes them (#1196). Removing the
#     TAG is safe: the release index references the platform manifest by digest;
#   * cosign's `sha256-<64 hex>.sig` / `.att` tags, which carry the signatures
#     and SBOM attestations of those digests (#1153), ARE kept (#1541), and
#     nothing looser than that exact shape.
#
# The historical regexes are checked for why each was replaced: #1190 kept
# releases and `latest` but not `.sig`/`.att`; `v.*` also kept per-arch tags.
#
# Negative controls re-run the checks on mutated copies of the page and assert
# they go red: a live block without the `.sig`/`.att` alternative, an
# unanchored live block, and a #1190 PUT body loosened to `v.*`.
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
H_GITLAB = "## GitLab container registry"
H_1190 = "### #1190 update: `latest` added to the keep-regex"
H_1541 = "### #1541 update: cosign `.sig`/`.att` tags added to the keep-regex"
H_HISTORY = "### History: the semver-anchored form predates #1190"


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
    top = section(doc, H_GITLAB)
    top = re.split(r"^### ", top, maxsplit=1, flags=re.M)[0]
    for b in JSON_BLOCK.findall(top):
        if '"next_run_at"' in b:
            return b
    return None


def put_body_regexes(doc, heading):
    """name_regex_keep values in a section's bash (PUT) blocks only."""
    sec = section(doc, heading)
    bodies = re.findall(r"```bash\n(.*?)\n```", sec, re.S)
    return [unescape(v) for b in bodies for v in re.findall(KEEP_RE, b)]


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

    def done():
        return sum(ok for _, ok in results), [d for d, ok in results if not ok]

    def kept(rx, tag):
        full = rx.fullmatch(tag) is not None
        # GitLab anchors the whole name; the patterns carry their own ^...\z
        # too. Both readings must agree, or the doc's regex is ambiguous.
        check(f"{rx.pattern!r}: fullmatch and search agree on {tag!r}",
              full == (rx.search(tag) is not None))
        return full

    lb = live_block(doc)
    live_vals = re.findall(KEEP_RE, lb) if lb else []
    check("live recorded-policy block found with exactly one name_regex_keep",
          len(live_vals) == 1)
    if len(live_vals) != 1:
        return done()
    live = unescape(live_vals[0])
    LIVE = compile_gitlab(live)

    # The suffix scheme the sample tags assume must still be the one CI uses.
    check(".gitlab-ci.yml amd64 leg pushes <tag>-amd64", "${CI_COMMIT_TAG}-amd64" in ci)
    check(".gitlab-ci.yml arm64 legs push <tag>-arm64", "${TAG}-arm64" in ci)
    check(".gitlab-ci.yml manifest jobs assemble from the <tag>-ARCH template",
          '--template "${REG}/${IMG}:${TAG}-ARCH"' in ci
          and "--platforms linux/amd64,linux/arm64" in ci)

    for t in RELEASES + ["latest"]:
        check(f"live: release/latest tag {t!r} is kept", kept(LIVE, t))
    for t in PER_ARCH:
        check(f"live: per-arch working tag {t!r} is NOT kept (#1196)",
              not kept(LIVE, t))
    for t in NEVER:
        check(f"live: {t!r} is NOT kept", not kept(LIVE, t))
    for t in COSIGN:
        check(f"live: cosign tag {t!r} is kept (#1541)", kept(LIVE, t))

    # Historical regexes, and why each was replaced.
    put1541 = put_body_regexes(doc, H_1541)
    check("#1541 PUT body equals the live regex (it set the live policy)",
          put1541 == [live])
    put1190 = put_body_regexes(doc, H_1190)
    check("#1190 section has exactly one PUT body regex", len(put1190) == 1)
    if len(put1190) == 1:
        R1190 = compile_gitlab(put1190[0])
        for t in RELEASES + ["latest"]:
            check(f"#1190: release/latest tag {t!r} was kept", kept(R1190, t))
        for t in PER_ARCH:
            check(f"#1190: per-arch tag {t!r} was NOT kept", not kept(R1190, t))
        for t in COSIGN:
            check(f"#1190: cosign tag {t!r} was NOT kept (why #1541 replaced it)",
                  not kept(R1190, t))
    hist = section(doc, H_HISTORY)
    check("History section still names the unanchored `v.*` form",
          "`v.*`" in hist)
    VSTAR = compile_gitlab("v.*")
    for t in PER_ARCH[:2]:
        check(f"`v.*`: per-arch tag {t!r} was kept (why it was anchored)",
              VSTAR.fullmatch(t) is not None)

    return done()


passed, failures = evaluate(DOC, CI)
for f in failures:
    print(f"  FAIL: {f}")


def control(desc, mutated, marker):
    """Re-run on a mutated page; the control passes if some failure mentions
    `marker`."""
    if mutated == DOC:
        print(f"  FAIL: negative control could not apply mutation: {desc}")
        return 0, 1
    _, ctl = evaluate(mutated, CI)
    if any(marker in f for f in ctl):
        return 1, 0
    print(f"  FAIL: negative control did not go red: {desc}")
    return 0, 1


ctl_passed = ctl_failed = 0
lb = live_block(DOC) or ""
put1190_raw = []
for b in re.findall(r"```bash\n(.*?)\n```", section(DOC, H_1190), re.S):
    put1190_raw += re.findall(KEEP_RE, b)
controls = []
if lb and put1190_raw:
    controls = [
        ("live block without the .sig/.att alternative",
         DOC.replace(lb, re.sub(KEEP_RE, lambda m: f'"name_regex_keep": "{put1190_raw[0]}"', lb), 1),
         "live: cosign tag"),
        ("unanchored live block",
         DOC.replace(lb, re.sub(KEEP_RE, lambda m: '"name_regex_keep": "v.*"', lb), 1),
         "live: per-arch working tag"),
        ("#1190 PUT body loosened to v.*",
         DOC.replace(f'"name_regex_keep": "{put1190_raw[0]}"', '"name_regex_keep": "v.*"', 1)
         if DOC.count(f'"name_regex_keep": "{put1190_raw[0]}"') == 1 else DOC,
         "#1190: per-arch tag"),
    ]
else:
    ctl_failed += 1
    print("  FAIL: negative controls could not locate the live or #1190 block")
for desc, mutated, marker in controls:
    p, f = control(desc, mutated, marker)
    ctl_passed += p
    ctl_failed += f

total_failed = len(failures) + ctl_failed
print(f"registry-keep-regex: {passed + ctl_passed} passed, {total_failed} failed")
sys.exit(1 if total_failed else 0)
PY
