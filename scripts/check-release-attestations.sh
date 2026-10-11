#!/usr/bin/env bash
# scripts/check-release-attestations.sh — fail when a recent release's images
# no longer carry their cosign signatures and CycloneDX SBOM attestations
# (#1542, follow-up from #1153).
#
# `check-release-images.sh` (#1074) proves the image exists. It does not look
# at the `sha256-<digest>.sig` / `.att` tags cosign stores in the SAME
# repository, so a registry cleanup sweep (the GitLab registry's current
# policy can delete them ~90 days after a release, #1541) or a manual delete
# removes every signature and SBOM while that job and the release pipeline
# stay green. Nothing else notices. This is the same failure shape as #1074
# one layer up, and the same answer: look, on a schedule.
#
# For each of the newest ATTEST_CHECK_COUNT `v*` tags, on BOTH registries, for
# BOTH images:
#   1. Read the manifest list and resolve each required platform digest
#      (linux/amd64, linux/arm64). No index, or a platform missing from it, is
#      a failure.
#   2. `cosign verify` the index digest and each platform digest.
#   3. `cosign verify-attestation --type cyclonedx` each platform digest, and
#      require at least one attestation back whose in-toto subject is that
#      digest. "Verified, returned nothing" is a failure, never a pass.
# Only releases cut AFTER image signing shipped (#1153) are checked: a tag is
# in scope when the .gitlab-ci.yml at that tag defines every job in
# ATTEST_SIGNING_JOBS, i.e. that release's own pipeline signed and attested its
# images. Older releases were never signed, so they have nothing that could
# have been swept, and this gate is about signatures SURVIVING (#1606: the
# first nightly checked three pre-#1153 alphas and reported 60 "failures" for
# signatures that had never existed). A pre-signing release that a Maintainer
# backfills is brought back into scope by listing it in ATTEST_BACKFILLED_TAGS.
# A tag that lacks the signing jobs but DESCENDS (git ancestry, not version
# order) from a release that had them is a failure (signing was dropped from
# the release pipeline), never a skip; so is a tag that does not resolve to a
# commit, and a shallow clone, where ancestry cannot be trusted.
# If no release has been cut since signing shipped, the job says so and exits
# 0; the self-test pins that the current .gitlab-ci.yml still defines the
# signing jobs, so a rename cannot turn that notice into a permanent pass.
#
# Identity is the documented tag-only regexp
# (docs/administration/image-verification.md). A tag listed in
# ATTEST_BACKFILLED_TAGS (releases signed later by the manual
# `image-attest-backfill` job on main) uses the documented main-or-tag regexp
# instead. The wider regexp is never a silent fallback for any other tag: that
# would let a backfill signature quietly stand in for a missing release one.
#
# Every failure is collected and printed naming registry/image/tag/digest, then
# the script exits non-zero. There is no `|| true` anywhere: this gate's whole
# job is to make absence read red, and some GitLab components exit 0 on
# findings (see the repo's CI notes), so the exit status is computed here.
#
# ## Overrides
#
# ATTEST_CHECK_COUNT      how many of the newest `v*` tags to check (default 3).
# RELEASE_REGISTRIES      space-separated registry image-path prefixes (default
#                         "$CI_REGISTRY_IMAGE ghcr.io/visiban/visiban"; an
#                         explicit empty value is exit 2).
# RELEASE_IMAGES          image names under each registry (default
#                         "backend frontend").
# ATTEST_ARCHES           required platform architectures (default
#                         "amd64 arm64").
# ATTEST_BACKFILLED_TAGS  space-separated tags documented as backfilled from
#                         main. Empty by default; a maintainer adds a tag here
#                         (in .gitlab-ci.yml, naming the issue) after running
#                         `image-attest-backfill` for it.
# ATTEST_ACCEPTED_GAPS    space-separated exact "<registry>/<image>:<tag>"
#                         references reported SKIP instead of FAIL. Empty by
#                         default; for documented, permanent exceptions only
#                         (for example a pre-#1153 release that was never
#                         signed and will not be backfilled).
# ATTEST_SIGNING_JOBS     CI job names whose presence in a tag's
#                         .gitlab-ci.yml marks it as cut after image signing
#                         shipped (default "backend-image-attest
#                         frontend-image-attest").
# ATTEST_TAGS             space-separated tags to check instead of resolving
#                         them from git (used by --self-test and by a
#                         maintainer running one release on demand). Bypasses
#                         the signing-era filter: an explicit tag is checked.
# CRANE, COSIGN           the crane / cosign commands (default `crane`,
#                         `cosign`); overridable so --self-test can drive the
#                         decision logic with stubs and no registry.
#
# Usage:
#   scripts/check-release-attestations.sh
#   scripts/check-release-attestations.sh --self-test
#
# Exit codes: 0 = every checked digest verified; 1 = at least one missing or
# failing signature/attestation; 2 = invocation error (no registry, no tag).
#
# Tools: bash, git, jq, crane, cosign. Portable to bash 3.2 (no associative
# arrays).

set -euo pipefail

ISSUER="https://gitlab.com"
# Keep these two byte-identical to docs/administration/image-verification.md;
# --self-test fails if the doc drifts from them.
IDENTITY_TAG='^https://gitlab\.com/visiban/visiban//\.gitlab-ci\.yml@refs/tags/v[0-9][^/]*$'
IDENTITY_BACKFILL='^https://gitlab\.com/visiban/visiban//\.gitlab-ci\.yml@refs/(tags/v[0-9][^/]*|heads/main)$'

ATTEST_CHECK_COUNT="${ATTEST_CHECK_COUNT:-3}"
RELEASE_IMAGES="${RELEASE_IMAGES:-backend frontend}"
ATTEST_ARCHES="${ATTEST_ARCHES:-amd64 arm64}"
ATTEST_BACKFILLED_TAGS="${ATTEST_BACKFILLED_TAGS:-}"
ATTEST_ACCEPTED_GAPS="${ATTEST_ACCEPTED_GAPS:-}"
ATTEST_TAGS="${ATTEST_TAGS:-}"
ATTEST_SIGNING_JOBS="${ATTEST_SIGNING_JOBS:-backend-image-attest frontend-image-attest}"
CRANE="${CRANE:-crane}"
COSIGN="${COSIGN:-cosign}"

FAILURES=0
CHECKED_ATTESTATIONS=0

fail() { echo "  FAIL    $*" >&2; FAILURES=$((FAILURES + 1)); }

# Same version-sort rules as check-release-images.sh: without the suffix
# config git sorts v1.1.0-rc.3 AFTER v1.1.0.
all_release_tags() {
  git -c versionsort.suffix=-alpha -c versionsort.suffix=-beta -c versionsort.suffix=-rc \
    for-each-ref --sort=-v:refname \
    --format='%(refname:short)' 'refs/tags/v*' 2>/dev/null
}

# signing_era <tag> → 0 when the .gitlab-ci.yml at <tag> defines every job in
# ATTEST_SIGNING_JOBS (that release's pipeline signed its images), 1 when it
# does not (or has no .gitlab-ci.yml), 2 when the tag's commit is not in the
# clone. 2 is never treated as "pre-signing": an unreadable tag is a failure.
# Here-strings, not `printf | grep -q`: grep -q exits on the first match and
# the SIGPIPE'd printf would fail the pipeline under pipefail.
signing_era() {
  local t="$1" cfg j
  git cat-file -e "${t}^{commit}" 2>/dev/null || return 2
  cfg="$(git show "${t}:.gitlab-ci.yml" 2>/dev/null)" || return 1
  for j in $ATTEST_SIGNING_JOBS; do
    grep -qx "${j}:" <<<"$cfg" || return 1
  done
  return 0
}

# resolve_release_tags → sets RESOLVED_TAGS (the newest ATTEST_CHECK_COUNT
# signing-era or backfilled tags), UNSIGNED_AFTER (tags without the signing
# jobs that descend from a signing-era release), PRE_SIGNING (tags without the
# signing jobs that descend from none), UNREADABLE and ANY_TAG. Called
# directly, not in $(…), so it can set globals.
#
# "After signing" is decided by commit ancestry, never by version order: a
# release whose pipeline dropped the signing jobs and a later one that
# restored them (alpha.5 signed, alpha.6 not, alpha.7 signed), or a patch tag
# on an older release line branched from a signed release (v1.2.1 sorts
# below a signed v1.3.0), both have unsigned tags BELOW a signed one in
# version order, and neither is "pre-signing". Only tags whose .gitlab-ci.yml
# really defines the jobs anchor the ancestry: a backfilled tag predates
# signing, so its descendants are not "cut after signing" because of it.
# The scan is full (no early stop) so every unsigned tag is classified.
resolve_release_tags() {
  local t rc picked=0 anchors="" unsigned="" a after
  RESOLVED_TAGS=""; UNSIGNED_AFTER=""; PRE_SIGNING=""; UNREADABLE=""; ANY_TAG=0
  for t in $(all_release_tags); do
    ANY_TAG=1
    rc=0
    signing_era "$t" || rc=$?
    case "$rc" in
      0) anchors="${anchors:+$anchors }$t" ;;
      2) UNREADABLE="${UNREADABLE:+$UNREADABLE }$t"; continue ;;
    esac
    if [ "$rc" -eq 0 ] || in_list "$t" "$ATTEST_BACKFILLED_TAGS"; then
      if [ "$picked" -lt "$ATTEST_CHECK_COUNT" ]; then
        RESOLVED_TAGS="${RESOLVED_TAGS:+$RESOLVED_TAGS }$t"
        picked=$((picked + 1))
      fi
    else
      unsigned="${unsigned:+$unsigned }$t"
    fi
  done
  for t in $unsigned; do
    after=0
    for a in $anchors; do
      if git merge-base --is-ancestor "$a" "$t" 2>/dev/null; then after=1; break; fi
    done
    if [ "$after" -eq 1 ]; then
      UNSIGNED_AFTER="${UNSIGNED_AFTER:+$UNSIGNED_AFTER }$t"
    else
      PRE_SIGNING="${PRE_SIGNING:+$PRE_SIGNING }$t"
    fi
  done
  return 0
}

default_registries() {
  if [ -n "${CI_REGISTRY_IMAGE:-}" ]; then
    printf '%s ghcr.io/visiban/visiban' "$CI_REGISTRY_IMAGE"
  else
    printf 'ghcr.io/visiban/visiban'
  fi
}

in_list() { # in_list <needle> <space-separated haystack> — exact match only
  local n="$1" e
  for e in $2; do [ "$e" = "$n" ] && return 0; done
  return 1
}

# platform_digest <manifest-json> <arch> → digest, or empty unless exactly one.
platform_digest() {
  printf '%s' "$1" | jq -r --arg a "$2" \
    '[.manifests[]? | select(.platform.os=="linux" and .platform.architecture==$a) | .digest]
     | if length==1 then .[0] else "" end'
}

verify_sig() { # verify_sig <ref> <identity-regexp>
  "$COSIGN" verify "$1" \
    --certificate-identity-regexp "$2" \
    --certificate-oidc-issuer "$ISSUER" >/dev/null 2>&1
}

# verify_att <repo-ref-with-digest> <digest> <identity-regexp>
# Succeeds only if cosign verified AND returned at least one attestation whose
# in-toto subject is <digest>.
verify_att() {
  local ref="$1" digest="$2" idre="$3" out n
  out="$("$COSIGN" verify-attestation --type cyclonedx \
    --certificate-identity-regexp "$idre" \
    --certificate-oidc-issuer "$ISSUER" "$ref" 2>/dev/null)" || return 1
  n="$(printf '%s\n' "$out" | jq -r --arg d "${digest#sha256:}" \
    'select(.payload != null) | .payload | @base64d | fromjson
     | select([.subject[]?.digest.sha256] | index($d)) | 1' 2>/dev/null | wc -l | tr -d ' ')"
  [ "${n:-0}" -gt 0 ] || return 2
  CHECKED_ATTESTATIONS=$((CHECKED_ATTESTATIONS + n))
  return 0
}

check_image() { # check_image <registry> <image> <tag> <identity-regexp>
  local reg="$1" img="$2" tag="$3" idre="$4"
  local repo="${reg}/${img}" ref manifest arch digest rc
  ref="${repo}:${tag}"

  if in_list "$ref" "$ATTEST_ACCEPTED_GAPS"; then
    echo "  SKIP    ${ref} (ATTEST_ACCEPTED_GAPS)"
    return 0
  fi

  if ! manifest="$("$CRANE" manifest "$ref" 2>/dev/null)" || [ -z "$manifest" ]; then
    fail "${ref}: manifest not readable (image missing, or registry error) — run check-release-images first"
    return 0
  fi

  # The index itself: signed with `cosign sign --recursive`.
  digest="$("$CRANE" digest "$ref" 2>/dev/null)" || digest=""
  if [ -z "$digest" ]; then
    fail "${ref}: cannot resolve the index digest"
  elif verify_sig "${repo}@${digest}" "$idre"; then
    echo "  ok      ${repo}@${digest} (index) signature"
  else
    fail "${repo}@${digest} (index of ${tag}): no verifying signature for identity ${idre}"
  fi

  for arch in $ATTEST_ARCHES; do
    digest="$(platform_digest "$manifest" "$arch")"
    case "$digest" in
      sha256:*) ;;
      *) fail "${ref}: index has no single linux/${arch} manifest"; continue ;;
    esac
    if verify_sig "${repo}@${digest}" "$idre"; then
      echo "  ok      ${repo}@${digest} (${arch}) signature"
    else
      fail "${repo}@${digest} (${arch} of ${tag}): no verifying signature for identity ${idre}"
    fi
    rc=0
    verify_att "${repo}@${digest}" "$digest" "$idre" || rc=$?
    case "$rc" in
      0) echo "  ok      ${repo}@${digest} (${arch}) SBOM attestation" ;;
      2) fail "${repo}@${digest} (${arch} of ${tag}): cosign verified but returned ZERO cyclonedx attestations for this digest" ;;
      *) fail "${repo}@${digest} (${arch} of ${tag}): no verifying cyclonedx attestation for identity ${idre}" ;;
    esac
  done
}

# run_checks → prints a report, returns 0/1/2. Never exits (self-test calls it).
run_checks() {
  local registries tags tag reg img idre
  FAILURES=0
  CHECKED_ATTESTATIONS=0

  if [ "${RELEASE_REGISTRIES+set}" = "set" ]; then
    registries="$RELEASE_REGISTRIES"
  else
    registries="$(default_registries)"
  fi
  if [ -z "$registries" ]; then
    echo "ERROR: no registry configured — set RELEASE_REGISTRIES, or CI_REGISTRY_IMAGE." >&2
    return 2
  fi

  if [ -n "$ATTEST_TAGS" ]; then
    tags="$ATTEST_TAGS"
  else
    resolve_release_tags
    if [ "$ANY_TAG" -eq 0 ]; then
      echo "ERROR: no v* release tag found to check — a gate that checks nothing must not pass." >&2
      return 2
    fi
    # Ancestry decides "pre-signing" vs "signing dropped": a shallow clone can
    # make a descendant look unrelated, so refuse rather than skip (#1606).
    if [ "$(git rev-parse --is-shallow-repository 2>/dev/null)" = "true" ]; then
      fail "shallow clone: commit ancestry is unreliable, so unsigned tags cannot be told apart from pre-signing ones (set GIT_DEPTH 0)"
    fi
    for tag in $UNREADABLE; do
      fail "${tag}: does not resolve to a commit in the clone — cannot tell whether it was signed"
    done
    for tag in $UNSIGNED_AFTER; do
      fail "${tag}: descends from a signed release, but its .gitlab-ci.yml does not define every job in [${ATTEST_SIGNING_JOBS}] — the release pipeline did not sign it"
    done
    if [ -n "$PRE_SIGNING" ]; then
      echo "-- not checked, descended from no signed release (cut before image signing, #1153): ${PRE_SIGNING}"
    fi
    tags="$RESOLVED_TAGS"
    if [ -z "$tags" ]; then
      if [ "$FAILURES" -gt 0 ]; then
        echo "check-release-attestations: ${FAILURES} failure(s)." >&2
        return 1
      fi
      echo "check-release-attestations: no release has been cut since image signing shipped (#1153);"
      echo "nothing to verify yet. The next v* tag pipeline signs its images and this job checks them."
      return 0
    fi
  fi

  for tag in $tags; do
    if in_list "$tag" "$ATTEST_BACKFILLED_TAGS"; then
      idre="$IDENTITY_BACKFILL"
      echo "== ${tag} (documented backfill: main-or-tag identity)"
    else
      idre="$IDENTITY_TAG"
      echo "== ${tag}"
    fi
    for reg in $registries; do
      for img in $RELEASE_IMAGES; do
        check_image "$reg" "$img" "$tag" "$idre"
      done
    done
  done

  if [ "$FAILURES" -eq 0 ] && [ "$CHECKED_ATTESTATIONS" -eq 0 ]; then
    echo "ERROR: zero attestations were verified across ${tags} — every image skipped or nothing was checked." >&2
    return 1
  fi
  if [ "$FAILURES" -gt 0 ]; then
    echo >&2
    echo "check-release-attestations: ${FAILURES} failure(s). A missing .sig/.att tag usually means a registry" >&2
    echo "cleanup sweep or manual delete (#1541); see docs/development/ci-gates.md for the runbook." >&2
    return 1
  fi
  echo "check-release-attestations: ${CHECKED_ATTESTATIONS} attestation(s) verified"
  return 0
}

# ─── self-test ───────────────────────────────────────────────────────────────
self_test() {
  local tmp pass=0 fail_n=0 rc out here doc
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" EXIT

  check() { # check <desc> <0|1>
    if [ "$2" -eq 0 ]; then echo "  PASS  $1"; pass=$((pass + 1)); else echo "  FAIL  $1"; fail_n=$((fail_n + 1)); fi
  }

  # Stub registry: manifests/digests from fixtures, behavior from marker files.
  local AMD="sha256:aaaa" ARM="sha256:bbbb" IDX="sha256:cccc"
  # shellcheck disable=SC2016  # the generated stub must keep a literal $1
  write_crane() {
    {
      echo '#!/usr/bin/env bash'
      echo "[ -e '$tmp/no-image' ] && exit 1"
      echo 'case "$1" in'
      if [ -e "$tmp/no-arm" ]; then
        echo "  manifest) echo '{\"manifests\":[{\"digest\":\"$AMD\",\"platform\":{\"os\":\"linux\",\"architecture\":\"amd64\"}}]}' ;;"
      else
        echo "  manifest) echo '{\"manifests\":[{\"digest\":\"$AMD\",\"platform\":{\"os\":\"linux\",\"architecture\":\"amd64\"}},{\"digest\":\"$ARM\",\"platform\":{\"os\":\"linux\",\"architecture\":\"arm64\"}}]}' ;;"
      fi
      if [ -e "$tmp/no-digest" ]; then echo "  digest) exit 1 ;;"; else echo "  digest) echo '$IDX' ;;"; fi
      echo 'esac'
    } > "$tmp/crane"
    chmod +x "$tmp/crane"
  }
  # Stub cosign: `verify` fails if no-sig exists; `verify-attestation` fails
  # on no-att, prints nothing on empty-att, wrong subject on bad-subject,
  # and logs the identity regexp it was given to identity.log.
  cat > "$tmp/cosign" <<STUB
#!/usr/bin/env bash
sub="\$1"; shift
idre=""; ref=""; issuer=""; typ=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    --certificate-identity-regexp) idre="\$2"; shift 2 ;;
    --certificate-oidc-issuer) issuer="\$2"; shift 2 ;;
    --type) typ="\$2"; shift 2 ;;
    *) ref="\$1"; shift ;;
  esac
done
echo "\$idre" >> "$tmp/identity.log"
# The documented issuer, and the SBOM predicate type, are part of the contract.
[ "\$issuer" = "https://gitlab.com" ] || { echo "stub: wrong issuer '\$issuer'" >&2; exit 1; }
case "\$sub" in
  verify) [ -e "$tmp/no-sig" ] && exit 1
          if [ -e "$tmp/no-platform-sig" ]; then case "\$ref" in *@$AMD|*@$ARM) exit 1 ;; esac; fi
          exit 0 ;;
  verify-attestation)
    [ "\$typ" = "cyclonedx" ] || { echo "stub: wrong --type '\$typ'" >&2; exit 1; }
    [ -e "$tmp/no-att" ] && exit 1
    [ -e "$tmp/empty-att" ] && exit 0
    d="\${ref#*@sha256:}"
    [ -e "$tmp/bad-subject" ] && d="ffff"
    payload="\$(printf '{"subject":[{"digest":{"sha256":"%s"}}]}' "\$d" | base64 | tr -d '\n')"
    printf '{"payload":"%s"}\n' "\$payload" ;;
esac
STUB
  chmod +x "$tmp/cosign"

  reset() { rm -f "$tmp"/no-digest "$tmp"/no-platform-sig "$tmp"/no-image "$tmp"/no-arm "$tmp"/no-sig "$tmp"/no-att "$tmp"/empty-att "$tmp"/bad-subject "$tmp"/identity.log; write_crane; }
  run() { rc=0; out="$(run_checks 2>&1)" || rc=$?; }

  CRANE="$tmp/crane"; COSIGN="$tmp/cosign"
  RELEASE_REGISTRIES="reg.example.com/v/v ghcr.example/v/v"
  RELEASE_IMAGES="backend frontend"
  ATTEST_TAGS="v1.2.0"
  ATTEST_BACKFILLED_TAGS=""
  ATTEST_ACCEPTED_GAPS=""

  echo "case: everything signed and attested"
  reset; run
  check "exits 0" "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
  check "counts 8 attestations (2 registries x 2 images x 2 arches)" \
    "$(echo "$out" | grep -q '8 attestation(s) verified' && echo 0 || echo 1)"
  check "uses the tag-only identity" \
    "$(grep -qF "$IDENTITY_TAG" "$tmp/identity.log" && ! grep -qF "$IDENTITY_BACKFILL" "$tmp/identity.log" && echo 0 || echo 1)"

  echo "case: zero attestations returned by cosign"
  reset; : > "$tmp/empty-att"; run
  check "exits 1 (never a silent pass)" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"
  check "names ZERO attestations and the digest" \
    "$(echo "$out" | grep -q "reg.example.com/v/v/backend@$AMD (amd64 of v1.2.0).*ZERO" && echo 0 || echo 1)"

  echo "case: attestation for the wrong subject"
  reset; : > "$tmp/bad-subject"; run
  check "exits 1" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"

  echo "case: attestation tag deleted"
  reset; : > "$tmp/no-att"; run
  check "exits 1" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"
  check "names the registry, image and digest" \
    "$(echo "$out" | grep -q "ghcr.example/v/v/frontend@$ARM (arm64 of v1.2.0)" && echo 0 || echo 1)"

  echo "case: signature tag deleted"
  reset; : > "$tmp/no-sig"; run
  check "exits 1" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"
  check "reports the index signature too" \
    "$(echo "$out" | grep -q "(index of v1.2.0): no verifying signature" && echo 0 || echo 1)"

  echo "case: release image missing"
  reset; : > "$tmp/no-image"; run
  check "exits 1" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"
  check "names the missing reference" \
    "$(echo "$out" | grep -q 'reg.example.com/v/v/backend:v1.2.0: manifest not readable' && echo 0 || echo 1)"

  echo "case: platform missing from the index"
  reset; : > "$tmp/no-arm"; write_crane; run
  check "exits 1 and names linux/arm64" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'no single linux/arm64 manifest' && echo 0 || echo 1)"

  echo "case: backfilled tag uses the main-or-tag identity, others do not"
  reset; ATTEST_BACKFILLED_TAGS="v1.2.0"; run
  check "exits 0" "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
  check "used the backfill identity, not the tag-only one" \
    "$(grep -qF "$IDENTITY_BACKFILL" "$tmp/identity.log" && ! grep -qF "$IDENTITY_TAG" "$tmp/identity.log" && echo 0 || echo 1)"
  ATTEST_BACKFILLED_TAGS="v9.9.9"; reset; run
  check "an unrelated backfilled tag does not widen v1.2.0" \
    "$(grep -qF "$IDENTITY_TAG" "$tmp/identity.log" && ! grep -qF "$IDENTITY_BACKFILL" "$tmp/identity.log" && echo 0 || echo 1)"
  ATTEST_BACKFILLED_TAGS=""

  echo "case: everything accepted as a gap leaves nothing verified"
  reset
  ATTEST_ACCEPTED_GAPS="reg.example.com/v/v/backend:v1.2.0 reg.example.com/v/v/frontend:v1.2.0 ghcr.example/v/v/backend:v1.2.0 ghcr.example/v/v/frontend:v1.2.0"
  run
  check "exits 1 (zero attestations verified)" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"
  ATTEST_ACCEPTED_GAPS=""

  echo "case: nothing to check"
  reset; ATTEST_TAGS=""
  rc=0
  ( cd "$tmp" && git init -q . && run_checks >/dev/null 2>&1 ) || rc=$?
  check "exits 2 when no v* tag resolves" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
  ATTEST_TAGS="v1.2.0"
  RELEASE_REGISTRIES=""; run
  check "exits 2 when no registry is configured" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"

  RELEASE_REGISTRIES="reg.example.com/v/v ghcr.example/v/v"  # restore after the empty-registry case
  echo "case: only a platform-digest signature is missing"
  reset; : > "$tmp/no-platform-sig"; run
  check "exits 1 even though the index signature verifies" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q "(amd64 of v1.2.0): no verifying signature" && ! echo "$out" | grep -q '(index of' && echo 0 || echo 1)"

  echo "case: index digest cannot be resolved"
  reset; : > "$tmp/no-digest"; write_crane; run
  check "exits 1 and says so" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'cannot resolve the index digest' && echo 0 || echo 1)"

  # Test repos: commit_ci <dir> <signed|unsigned> commits a .gitlab-ci.yml that
  # does or does not define the image signing jobs (#1606). The unsigned one
  # defines frontend-image-attest for real and names backend-image-attest only
  # in a comment and as an indented key, so a match that is not anchored to a
  # whole top-level line would wrongly count it as signed.
  commit_ci() {
    if [ "$2" = signed ]; then
      printf 'backend-manifest:\n  stage: deploy\nbackend-image-attest:\n  stage: deploy\nfrontend-image-attest:\n  stage: deploy\n' > "$1/.gitlab-ci.yml"
    else
      printf 'backend-manifest:\n  stage: deploy\n# backend-image-attest: (comment, not a job)\nfrontend-image-attest:\n  stage: deploy\n  backend-image-attest:\n    nested: true\n' > "$1/.gitlab-ci.yml"
    fi
    ( cd "$1" && git add .gitlab-ci.yml \
      && git -c user.name=t -c user.email=t@example.com commit -q --allow-empty -m "$2" )
  }
  in_repo() { # in_repo <dir> — run_checks over that repo's tags, sets rc/out
    rc=0
    out="$( cd "$1" && ATTEST_TAGS="" && ATTEST_CHECK_COUNT=3 && run_checks 2>&1 )" || rc=$?
  }

  echo "case: ATTEST_CHECK_COUNT bounds the releases checked"
  reset
  mkdir "$tmp/repo"
  ( cd "$tmp/repo" && git init -q . )
  commit_ci "$tmp/repo" signed
  ( cd "$tmp/repo" && git tag v1.0.0 && git tag v1.1.0 && git tag v1.2.0 )
  rc=0
  out="$( cd "$tmp/repo" && ATTEST_TAGS="" && ATTEST_CHECK_COUNT=2 && run_checks 2>&1 )" || rc=$?
  check "exactly 2 releases are checked, the newest 2" \
    "$([ "$(echo "$out" | grep -c '^== ')" -eq 2 ] && echo "$out" | grep -q '^== v1.2.0' && echo "$out" | grep -q '^== v1.1.0' && ! echo "$out" | grep -q '^== v1.0.0' && echo 0 || echo 1)"

  # #1606: the first nightly checked the three newest tags, all cut before
  # image signing existed, and reported every digest as unsigned.
  echo "case: releases cut before image signing are not checked (#1606)"
  reset
  mkdir "$tmp/era"
  ( cd "$tmp/era" && git init -q . )
  commit_ci "$tmp/era" unsigned
  ( cd "$tmp/era" && git tag v1.1.0 && git tag v1.2.0-alpha.3 && git tag v1.2.0-alpha.4 )
  in_repo "$tmp/era"
  check "no signing-era release yet: exits 0 with a notice, verifies nothing" \
    "$([ "$rc" -eq 0 ] && echo "$out" | grep -q 'no release has been cut since image signing' && [ ! -e "$tmp/identity.log" ] && echo 0 || echo 1)"
  check "names the pre-signing releases it skipped" \
    "$(echo "$out" | grep -q 'not checked, descended from no signed release (cut before image signing, #1153): v1.2.0-alpha.4 v1.2.0-alpha.3 v1.1.0$' && echo 0 || echo 1)"
  commit_ci "$tmp/era" signed
  ( cd "$tmp/era" && git tag v1.2.0-alpha.5 )
  reset; in_repo "$tmp/era"
  check "only the signing-era release is checked, and it passes" \
    "$([ "$rc" -eq 0 ] && [ "$(echo "$out" | grep -c '^== ')" -eq 1 ] && echo "$out" | grep -q '^== v1.2.0-alpha.5$' && echo 0 || echo 1)"
  ATTEST_BACKFILLED_TAGS="v1.2.0-alpha.3"; reset; in_repo "$tmp/era"
  check "a backfilled pre-signing release is in scope, and does not make other pre-signing tags fail" \
    "$([ "$rc" -eq 0 ] && echo "$out" | grep -q '^== v1.2.0-alpha.3 (documented backfill' && ! echo "$out" | grep -q 'descends from a signed release' && echo 0 || echo 1)"
  ATTEST_BACKFILLED_TAGS=""
  : > "$tmp/no-sig"; in_repo "$tmp/era"
  check "a signing-era release with a missing signature still fails" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q '(index of v1.2.0-alpha.5): no verifying signature' && echo 0 || echo 1)"
  commit_ci "$tmp/era" unsigned
  ( cd "$tmp/era" && git tag v1.2.0-beta.1 )
  reset; in_repo "$tmp/era"
  check "a release cut AFTER signing without the signing jobs fails, never skips" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'v1.2.0-beta.1: descends from a signed release' && echo 0 || echo 1)"
  # Signing dropped (beta.1) and then restored (beta.2): beta.1 now sorts
  # BELOW a signed tag, but it is still a release cut after signing.
  commit_ci "$tmp/era" signed
  ( cd "$tmp/era" && git tag v1.2.0-beta.2 )
  reset; in_repo "$tmp/era"
  check "signing dropped then restored: the gap still fails, not 'pre-signing'" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'v1.2.0-beta.1: descends from a signed release' && ! echo "$out" | grep -q 'not checked.*v1.2.0-beta.1' && echo 0 || echo 1)"

  echo "case: a patch on an older release line, cut after signing (#1606)"
  reset
  mkdir "$tmp/line"
  ( cd "$tmp/line" && git init -q . )
  commit_ci "$tmp/line" signed
  ( cd "$tmp/line" && git tag v1.2.0 )
  commit_ci "$tmp/line" signed
  ( cd "$tmp/line" && git tag v1.3.0 && git checkout -q -b release-1.2 v1.2.0 )
  commit_ci "$tmp/line" unsigned
  ( cd "$tmp/line" && git tag v1.2.1 )
  in_repo "$tmp/line"
  check "v1.2.1 sorts below signed v1.3.0 but descends from signed v1.2.0: fails" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'v1.2.1: descends from a signed release' && echo 0 || echo 1)"

  echo "case: a tag that does not resolve to a commit"
  reset
  ( cd "$tmp/line" && git checkout -q release-1.2 && git tag v1.2.2 "$(git hash-object -w .gitlab-ci.yml)" )
  in_repo "$tmp/line"
  check "fails and names it, never treats it as pre-signing" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'v1.2.2: does not resolve to a commit' && ! echo "$out" | grep -q 'not checked.*v1.2.2' && echo 0 || echo 1)"

  echo "case: a shallow clone"
  reset
  git clone -q --depth 1 "file://$tmp/era" "$tmp/shallow" 2>/dev/null
  in_repo "$tmp/shallow"
  check "fails: ancestry cannot be trusted (GIT_DEPTH 0)" \
    "$([ "$rc" -eq 1 ] && echo "$out" | grep -q 'shallow clone: commit ancestry is unreliable' && echo 0 || echo 1)"

  check "the repo's own .gitlab-ci.yml still defines every ATTEST_SIGNING_JOBS job" \
    "$(cfg="$(cat "$here/../.gitlab-ci.yml")"; r=0; for j in $ATTEST_SIGNING_JOBS; do grep -qx "${j}:" <<<"$cfg" || r=1; done; echo "$r")"

  echo "case: the real entry point's exit status (subprocess, stubs on PATH)"
  mkdir "$tmp/bin"; ln -s "$tmp/crane" "$tmp/bin/crane"; ln -s "$tmp/cosign" "$tmp/bin/cosign"
  entry() { # entry <env assignments...> — runs the script itself, not run_checks
    rc=0
    out="$(cd "$tmp/repo" && env -u CRANE -u COSIGN PATH="$tmp/bin:$PATH" "$@" bash "$here/check-release-attestations.sh" 2>&1)" || rc=$?
  }
  reset; entry RELEASE_REGISTRIES="reg.example.com/v/v" ATTEST_TAGS="v1.2.0"
  check "exits 0 when everything verifies" "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
  : > "$tmp/no-att"; entry RELEASE_REGISTRIES="reg.example.com/v/v" ATTEST_TAGS="v1.2.0"
  check "exits 1 on a verify failure" "$([ "$rc" -eq 1 ] && echo 0 || echo 1)"
  reset; entry RELEASE_REGISTRIES="" ATTEST_TAGS="v1.2.0"
  check "exits 2 with no registry" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"
  mkdir "$tmp/empty" && ( cd "$tmp/empty" && git init -q . )
  rc=0
  out="$(cd "$tmp/empty" && env -u ATTEST_TAGS -u CRANE -u COSIGN PATH="$tmp/bin:$PATH" RELEASE_REGISTRIES="reg.example.com/v/v" bash "$here/check-release-attestations.sh" 2>&1)" || rc=$?
  check "exits 2 with no tag" "$([ "$rc" -eq 2 ] && echo 0 || echo 1)"

  echo "case: regexps match the documented ones"
  doc="$here/../docs/administration/image-verification.md"
  check "tag-only identity is in image-verification.md" "$(grep -qF "IDENTITY='$IDENTITY_TAG'" "$doc" && echo 0 || echo 1)"
  check "backfill identity is in image-verification.md" "$(grep -qF "IDENTITY='$IDENTITY_BACKFILL'" "$doc" && echo 0 || echo 1)"

  echo
  if [ "$fail_n" -gt 0 ]; then
    echo "check-release-attestations.sh --self-test: $pass passed, $fail_n FAILED" >&2
    return 1
  fi
  echo "check-release-attestations.sh --self-test: $pass passed"
}

main() {
  if [ "${1:-}" = "--self-test" ]; then
    self_test
    exit $?
  fi
  local rc=0
  run_checks || rc=$?
  exit "$rc"
}

main "$@"
