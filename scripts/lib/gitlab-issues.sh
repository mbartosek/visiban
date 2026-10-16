#!/usr/bin/env bash
# scripts/lib/gitlab-issues.sh — shared helper: resolve a batch of GitLab
# issue IIDs to their live state via the REST API.
#
# Ported from trueppm/trueppm#4452 (the same defect class was independently
# found in macrodream/blueprint#123; visiban#1694 is this repo's instance,
# on scripts/check-suppression-issues.sh).
#
# The defect this exists to fix: `GET /projects/:id/issues/:iid` answers
# **401 to any `JOB-TOKEN` header**, while the identical request with no
# auth header at all answers 200 on a public project (verified 2026-10-16
# against visiban%2Fvisiban: anonymous -> 200, `JOB-TOKEN: x` -> 401).
# `CI_JOB_TOKEN` is always set inside a CI job, so a gate that sends it never
# sees a real response in CI; `curl -sf` swallows the 401 into an empty
# body, which reads as "the tracker is unreachable" and a hygiene gate's own
# documented fail-open policy silently absorbs what should have been a hard
# finding. This library:
#
#   - never sends JOB-TOKEN — GITLAB_API_TOKEN, then ISSUES_API_TOKEN, then
#     an anonymous request (see "Token resolution" below);
#   - resolves every cited issue in as few batched `iids[]=` requests as fit
#     under GITLAB_ISSUES_BATCH_SIZE (default 100, the API's own `per_page`
#     ceiling) instead of one request per marker site — cuts both request
#     count and the 429 exposure that comes with an unauthenticated,
#     per-IP-budgeted call for every site;
#   - retries a transient failure (network error, connection refused, HTTP
#     429/5xx) up to 5 times, capped at 60s total, honoring any
#     `Retry-After` response header the way curl itself already does for
#     429/503 (left unset so curl's own handling applies — see
#     `_gitlab_issues_curl` below);
#   - when a configured token is rejected (401), warns once and falls back
#     to an anonymous request for that same batch instead of reporting the
#     whole lookup inconclusive, since an anonymous GET already sees every
#     non-confidential issue on this public project.
#
# Source this file, don't execute it:
#   # shellcheck source=scripts/lib/gitlab-issues.sh
#   source "$(cd "$(dirname "$0")" && pwd)/lib/gitlab-issues.sh"
#   gitlab_issue_states 123 456      # -> "123<TAB>opened" / "456<TAB>closed" lines
#
# gitlab_issue_states <iid> [<iid>...]
#   Prints one "<iid><TAB><state>" line (state: "opened" or "closed", the
#   API's own literal values) for every iid the tracker actually returned
#   in a successful response. An iid passed in but never printed back means
#   the tracker did not return it — it does not exist, or it is confidential
#   and invisible to the token (or anonymous request) in use. Callers MUST
#   treat an iid with no corresponding output line as a distinct,
#   reportable "missing" state — never as "open", and never folded into
#   "the lookup failed".
#
#   Returns 1 and stops emitting further output the moment one batch's
#   lookup is genuinely inconclusive: every retry exhausted, a non-2xx
#   status that isn't a token-rejecting 401 (which triggers the anonymous
#   fallback instead, see above), or a body that isn't the JSON array the
#   issues list endpoint promises. Callers are expected to fail OPEN (warn,
#   non-blocking) on that return code — this library has no opinion on
#   blocking policy, only on what the tracker said. Any "<iid><TAB><state>"
#   lines already printed for earlier, successful batches before the
#   failing one remain valid and are not retracted.
#
# Token resolution (never JOB-TOKEN — see header): GITLAB_API_TOKEN if set,
# else ISSUES_API_TOKEN, else an anonymous request with no auth header at
# all. The anonymous path is not a last resort: this project is public, so
# it already sees every non-confidential issue, and a CI job that never
# configured either token variable still gets a working (if narrower)
# lookup instead of unconditionally failing open.
#
# Env overrides:
#   CI_API_V4_URL             API base URL (default: https://gitlab.com/api/v4)
#   CI_PROJECT_ID             project path or numeric id (default: visiban%2Fvisiban)
#   GITLAB_ISSUES_BATCH_SIZE  iids per request (default: 100, the API's own
#                             per_page ceiling)

GITLAB_ISSUES_BATCH_SIZE="${GITLAB_ISSUES_BATCH_SIZE:-100}"

# _gitlab_issues_curl <url> [curl-arg...] — one GET with the shared retry
# policy. Prints "<http_status>\n<body>" and returns 0 as long as the
# transport itself eventually succeeded. curl only exits non-zero here on a
# true transport error surviving every retry (DNS, timeout, connection
# refused) — never on an HTTP error status, since `-f` is deliberately not
# used: callers need to see 401 (to trigger the anonymous fallback) and 429
# (already retried by curl itself below) rather than have curl swallow them
# into an empty, indistinguishable body the way the pre-#1694 script did.
#
# `--retry 5 --retry-max-time 60` covers 429/5xx/network per visiban#1694;
# `--retry-connrefused` extends that to a refused connection, which curl's
# default retry set otherwise treats as fatal. `--retry-delay` is
# deliberately never set so curl honors a `Retry-After` response header on
# 429/503 itself instead of our own fixed backoff overriding it.
_gitlab_issues_curl() {
  local url="$1"
  shift
  local out
  out="$(curl -sS --retry 5 --retry-max-time 60 --retry-connrefused \
    -w $'\nHTTPSTATUS:%{http_code}' "$@" "$url" 2>/dev/null)" || return 1
  printf '%s\n%s' "${out##*HTTPSTATUS:}" "${out%HTTPSTATUS:*}"
}

# _gitlab_issues_fetch_batch <url> — the auth-then-anonymous-fallback dance
# for one batch URL. Prints the raw JSON response body on success; returns 1
# if nothing conclusive came back even after falling back to anonymous.
_gitlab_issues_fetch_batch() {
  local url="$1" auth_header="" token_label="" result status body

  if [ -n "${GITLAB_API_TOKEN:-}" ]; then
    auth_header="PRIVATE-TOKEN: ${GITLAB_API_TOKEN}"
    token_label="GITLAB_API_TOKEN"
  elif [ -n "${ISSUES_API_TOKEN:-}" ]; then
    auth_header="PRIVATE-TOKEN: ${ISSUES_API_TOKEN}"
    token_label="ISSUES_API_TOKEN"
  fi

  if [ -n "$auth_header" ]; then
    result="$(_gitlab_issues_curl "$url" -H "$auth_header")" || return 1
    status="${result%%$'\n'*}"
    body="${result#*$'\n'}"
    if [ "$status" = "200" ]; then
      printf '%s' "$body"
      return 0
    fi
    if [ "$status" = "401" ]; then
      echo "gitlab-issues: WARNING — ${token_label} was rejected (401); falling back to an anonymous request for this public project." >&2
    else
      echo "gitlab-issues: WARNING — authenticated issue lookup returned HTTP ${status}; falling back to an anonymous request." >&2
    fi
  fi

  result="$(_gitlab_issues_curl "$url")" || return 1
  status="${result%%$'\n'*}"
  body="${result#*$'\n'}"
  if [ "$status" = "200" ]; then
    printf '%s' "$body"
    return 0
  fi
  return 1
}

gitlab_issue_states() {
  local ids=("$@")
  [ "${#ids[@]}" -gt 0 ] || return 0

  local api_base project
  api_base="${CI_API_V4_URL:-https://gitlab.com/api/v4}"
  project="${CI_PROJECT_ID:-visiban%2Fvisiban}"

  local batch_size="${GITLAB_ISSUES_BATCH_SIZE}"
  local total="${#ids[@]}" offset=0

  while [ "$offset" -lt "$total" ]; do
    local batch=("${ids[@]:offset:batch_size}")
    local url="${api_base}/projects/${project}/issues?per_page=100"
    local id
    for id in "${batch[@]}"; do
      url="${url}&iids[]=${id}"
    done

    local body
    body="$(_gitlab_issues_fetch_batch "$url")" || return 1

    printf '%s' "$body" | jq -e 'type == "array"' >/dev/null 2>&1 || return 1
    printf '%s' "$body" | jq -r '.[] | "\(.iid)\t\(.state)"'

    offset=$((offset + batch_size))
  done
}
