# Nightly load test

`nightly-load-test` is a nightly, schedule-only CI job (`.gitlab-ci.yml`) that measures real
wall-clock p50/p95/p99 latency against a large seeded fixture and fails the pipeline if any
covered endpoint exceeds its committed budget.

**Why it matters:** a query-count guard is not a latency guard. `perf-check` (static N+1
review) and `perf-bench` (query counts on demand) both measure query *count* — a regression
from a missing index, a data-volume change, or a query-plan flip can leave the query count
and query text identical while making the same queries slower. Neither of those gates can
see that class of regression; this job can.

!!! info "Where this came from"
    Filed as #1082 from a TruePPM audit finding: a real p95 regression (`GET /tasks/` at
    3.3x baseline) was "fixed" by a query-count guard that its own description said could not
    detect that class of regression — the fix never touched the actual root cause, and the
    regression sat inside a budget loose enough to hide it. See the issue for the full
    postmortem.

## What it covers

| Endpoint | Path | Volume sensitivity |
|---|---|---|
| Board list | `GET /api/v1/boards/` | Run as the board owner (`demo1`) so the access query spans the 20-member board. Per-board member and card counts are independent correlated subqueries and the access filter no longer joins memberships (#1723). Budget 55ms ([#1767](https://gitlab.com/visiban/visiban/-/issues/1767)) |
| Board fetch | `GET /api/v1/boards/{id}/full/` | Serializes every card on the board, unpaginated — the single most volume-sensitive read in the product |
| Card search | `GET /api/v1/cards/?search=...` | Exercises the `pg_trgm` GIN indexes added in migration `0030_card_trigram_search_indexes` |
| Card timeline | `GET /api/v1/boards/{board_id}/cards/{id}/timeline/` | Exercises the composite indexes added in migration `0045_add_activity_comment_composite_indexes` |
| Notifications list | `GET /api/v1/notifications/` | The highest-QPS authenticated endpoint in the product — the frontend polls it every 15-30s |
| Notifications unread count | `GET /api/v1/notifications/unread-count/` | Same polling cadence as the list endpoint |

`perf-check` and `perf-bench` are unchanged and still run where they already did — this job is
additive, not a replacement. `perf-check`'s own description
([`.claude/agents/perf-check.md`](https://gitlab.com/visiban/visiban/-/blob/main/.claude/agents/perf-check.md))
now states this division of labor explicitly: a query-count guard does not satisfy a latency
budget.

## The fixture

The job seeds a dedicated, larger board — never the real demo board — with:

```bash
cd backend
python manage.py seed_demo_data --force --scale 20 --with-notifications
```

- `--scale 20` replicates the demo board's 10-swimlane layout 20 times: **200 swimlanes,
  ~2,400 cards** (11-13 cards per swimlane, same distribution `seed_demo_data` always uses —
  see its docstring). The board is named **"Visiban Load Test Board"**, distinct from
  `"Visiban Demo Board"`, so this job can never collide with or overwrite the
  manual `seed-demo-data` job's data, in CI or on the shared demo environment.
- **20 members** on that board (the 5 demo users plus 15 `loadtest*` filler accounts with
  unusable passwords, `--scale > 1` only). Members x cards is the shape that made the board
  list's count annotations expensive (two multi-valued joins multiplying, #1723), and a
  5-member fixture could not expose it; with ~2,400 cards it now exercises that endpoint.
  `GET /api/v1/boards/` computes `member_count` / `card_count` / `archived_card_count` as
  independent correlated subqueries, and the access filter uses an `EXISTS` membership predicate
  rather than a join, so the members x cards row blow-up is gone. The job times that endpoint as
  the `board_list` scenario, as the board owner (`demo1`, via `--owner-token-file`) so the
  20 memberships are on the measured path; every other scenario still runs as `demo2`. It has
  a committed budget in `backend/nightly-load-test-baseline.json` (added by
  [#1767](https://gitlab.com/visiban/visiban/-/issues/1767)).
- `--with-notifications` seeds 60 unread `Notification` rows for the `demo2` user — more than
  the 50-row cap `NotificationListView` / `NotificationUnreadCountView` apply, so the job
  exercises the same "more unread than the cap" path a busy real inbox hits.
- **A fixture size with no stated card/swimlane count is meaningless** — a p95 number is only
  interpretable next to the data volume it was measured against. `nightly-load-test-results.json`
  (the job's artifact) always records the fixture's actual card and swimlane count alongside
  the timings, and `backend/nightly-load-test-baseline.json` records what the committed budgets
  were derived against.

The large fixture is regenerated fresh in an ephemeral CI database on every run and is never
committed anywhere.

## Auth

The job reuses `manage.py provision_fuzz_token` (already used by `backend-schema-fuzz`) to mint
a scoped, non-admin Personal Access Token for `demo2` and writes it to a CI-only file
(`VISIBAN_LOAD_TEST_TOKEN_FILE`, mode `0600`, never logged). No credential is hardcoded anywhere
in the job or the script — this follows the same "no hardcoded credentials" rule as every other
backend job in `.gitlab-ci.yml`.

The job passes `--set-random-password`, the same flag the schema-fuzz jobs use
([#1607](https://gitlab.com/visiban/visiban/-/issues/1607)). `demo2` is seeded with no usable
password, and a password change revokes every token. The flag gives `demo2` a random password
that is never recorded, so the password-change routes require a current password and no request
can revoke the job's token mid-run. The flag is off by default, and the command is for throwaway
CI databases like this job's only.

## Running it

```bash
python3 scripts/nightly_load_test.py \
    --base-url http://localhost:8000 \
    --token-file /tmp/visiban_load_test_token \
    --owner-token-file /tmp/visiban_load_test_owner_token \
    --board-name "Visiban Load Test Board" \
    --budget-file backend/nightly-load-test-baseline.json \
    --iterations 40 --warmup 5 \
    --output nightly-load-test-results.json
```

The CI job provisions two tokens: `--token-file` (`demo2`, a plain member) for every scenario,
and `--owner-token-file` (`demo1`, the board owner) for `board_list` alone. `--owner-token-file`
has no default and the script refuses to run without it: `board_list`'s 55ms budget was derived
as the owner, whose access query spans all 20 memberships, so measuring it as `demo2` would
compare a cheaper path against that budget and hide a regression. The other flags shown are the
script's defaults.

For each endpoint: 5 warmup requests (discarded), then 40 timed requests. Percentiles use
nearest-rank (no interpolation), so every reported number was an actually-observed latency, not
a synthetic average of two samples. The job fails (`sys.exit(1)`) if any endpoint's measured p95
exceeds its budget, and always writes the full per-endpoint results as a CI artifact
(`nightly-load-test-results.json`, kept 90 days) regardless of pass/fail.

The Django `runserver` output is not written to the job log (a noisy run could hit GitLab's
16 MB log cap). It goes to the `nightly-load-test-server.log` artifact, which shares the results
file's 90-day expiry and is restricted to developers. On failure the job log prints the last
200 lines of it.

Like every bespoke gate script in this repo, it ships a `--self-test` mode
(`python scripts/nightly_load_test.py --self-test`) that proves the percentile calculation and
budget-comparison logic still fire on a known-bad and known-good input, run in CI immediately
before the real invocation — see [CI gate self-tests](ci-gates.md). It also fails if any scenario
the script measures has no `budget_p95_ms` in the committed baseline, so a new scenario cannot
silently stay measure-only. The `nightly-load-test-selftest` job runs it on merge requests that
touch the script or the baseline.

## The schedule

Configure once in GitLab: **CI/CD → Schedules → New schedule**

- Target branch: `main`
- Cron: `0 0 * * *`, timezone `America/New_York` (daily at midnight Eastern, 04:00 UTC during daylight time and 05:00 UTC in winter; schedule 4463581)
- Variables: `LOAD_TEST_SCHEDULE=true`

The job can also be run manually at any time (advisory — `allow_failure: true` on the manual
path only; the scheduled run is blocking) via "Run pipeline" or the job's manual play button.

!!! warning "This step is easy to skip silently — #1213"
    This is GitLab project configuration, not a file in this repo, so nothing in a diff or a
    review catches it being missed. It *was* missed: the schedule above was never created
    after this job merged, and the gap sat undetected until #1213 found it — by which point a
    real ~40% p95 regression on `GET /boards/{id}/full/` had already shipped past it. The
    `schedule-config-check` job (`scripts/check-pipeline-schedules.sh`) now asserts this
    schedule (and every other `$CI_PIPELINE_SOURCE == "schedule" && $VAR == "true"` rule in
    `.gitlab-ci.yml`) has an active GitLab schedule actually setting its variable, on every
    scheduled pipeline run — see [CI gate self-tests](ci-gates.md).

## Budget derivation — the rule

**Budgets are committed to the repo, in `backend/nightly-load-test-baseline.json`, and every
`budget_p95_ms` must trace to a `measured_p95_ms` via a stated formula — never a round number
picked because it looked reasonable.** TruePPM's postmortem is explicit about why: its 2000ms
budget was loose enough that a 2x regression (683-851ms baseline &rarr; 1330ms) sat inside it
without ever firing.

The formula this repo uses:

```
budget_p95_ms = ceil(measured_p95_ms * 1.4 / 5) * 5
```

- **1.4x** is the midpoint of the 1.3-1.5x range #1082 specifies — tight enough that a
  meaningful regression still trips the budget, loose enough to absorb ordinary CI-runner
  noise between runs.
- Rounding up to the nearest **5ms** gives the comparison a small amount of headroom for
  measurement jitter without the number itself being arbitrary — it is still fully determined
  by the measured baseline, just not reported to sub-millisecond precision.

!!! warning "Budgets are only valid on NUC runners"
    Budgets are absolute milliseconds, so they only hold on the hardware they were measured
    on. The current baseline (job 17304593759, #1767) ran on `runner-04-NUC`; the one before it (job 16796708105, #1233) ran on `gitlab-runner-nuc-2-visiban`, and the job is
    pinned with `tags: [nuc]` (see [CI Runners](../maintainers/ci-runners.md)). Before the
    pin, the 2026-10-08 scheduled run landed on `gitlab-runner-dell-01-visiban` and failed
    `card_timeline`, `notifications_list` and `notifications_unread` by 1.3-1.8x with no
    relevant code change, while every NUC run before it had passed. When re-deriving, take
    the artifact from a NUC run, and re-derive the whole file if the runner class ever
    changes.

### Re-deriving the baseline (do this at every release, and whenever the fixture size changes)

1. Trigger the `nightly-load-test` job (wait for the schedule, or run it manually).
2. Download its `nightly-load-test-results.json` artifact.
3. For each endpoint, recompute `budget_p95_ms` from that run's `p95_ms` using the formula
   above.
4. Update `backend/nightly-load-test-baseline.json`'s `measured_p95_ms`, `measured_p99_ms`, and
   `budget_p95_ms` fields, its top-level `measured_at`, and its `fixture` block if the fixture
   size changed. Recompute the whole file in one pass — never hand-adjust a single
   `budget_p95_ms` without also updating the `measured_p95_ms` it supposedly came from.
5. Flip `status` from `"provisional"` to `"ci-derived"` the first time this is done from a real
   scheduled pipeline run (see the note below).

!!! note "Re-derived from the first real CI run — #1233"
    The baseline committed alongside #1082 was measured locally (a development machine, not a
    GitLab CI runner) and stayed `"provisional"` until a real scheduled run existed to re-derive
    it from — see #1213. That run happened 2026-09-29T03:00 UTC; `backend/nightly-load-test-baseline.json`
    is now `"ci-derived"` from its artifact (job 16796708105), following the steps above. The very
    next scheduled run (2026-09-30T03:00 UTC) failed against the *old* provisional budgets on
    `board_full`/`card_timeline`/`notifications_unread` — its measured numbers pass comfortably
    against the re-derived budgets, confirming the stale provisional file was the cause, not a new
    regression. `board_full` is the most CPU-bound of the six endpoints (per-card DRF
    serialization across ~2,388 cards, #1212) and shows the widest run-to-run variance on a
    shared CI runner as a result — expect it to move more between runs than the lighter
    endpoints.

!!! note "Re-derived after the 20-member fixture and `board_list` — #1767"
    The seed fixture grew to 20 board members and the `board_list` scenario was added (#1723), so
    the whole file was re-derived on 2026-10-20 from the first scheduled run after both landed
    (job 17304593759, pipeline 2965970261, started about 04:08 UTC, on `runner-04-NUC`, tag `nuc`). `board_list`
    is new and got its first budget (p95 37.2ms, budget 55ms), measured as the board owner so
    the 20 memberships are on the path. The other budgets were recomputed from this run, not
    hand-adjusted, and all moved up: `board_full` 1430 to 1695ms, `card_search` 120 to 155ms,
    `card_timeline` 55 to 60ms, `notifications_list` 50 to 60ms, `notifications_unread` 40 to
    45ms. Those increases are **not** attributable to the fixture change: they predate #1723
    (`board_full` p95 was 981-1042ms on `gitlab-runner-nuc-2-visiban` on Oct 1-4 versus
    1172-1375ms on Oct 9-19) and are tracked in
    [#1800](https://gitlab.com/visiban/visiban/-/issues/1800). The new `board_full` budget leaves
    under 3% headroom over the worst past run (1650.7ms, the Oct 12 failure against the old
    1430ms budget). [#1800](https://gitlab.com/visiban/visiban/-/issues/1800) traced the
    `board_full` rise to the psycopg 3 migration and backported the fix. See
    [The October 2026 `board_full` drift](#the-october-2026-board_full-drift-1800) below. The previous derivation ran
    on `gitlab-runner-nuc-2-visiban`; both are NUC-class
    (`nuc`-tagged) runners, so the file was re-derived as a whole rather than patched.

## The October 2026 `board_full` drift — #1800

**Verdict: a real regression, fixed in code.** The cause was the psycopg2 to psycopg 3 move
([#1252](https://gitlab.com/visiban/visiban/-/issues/1252), merged 2026-10-04 at 10:47 EDT).
It was not a runner or environment change.

### What the scheduled runs show

The 22 completed scheduled runs between 2026-09-29 and 2026-10-20 did **not** all land on one
runner. They alternated between `gitlab-runner-nuc-2-visiban` and `Runner-03-NUC`. One ran
on `gitlab-runner-dell-01-visiban` (Oct 8, before the `nuc` pin) and one on `runner-04-NUC`
(Oct 20). Card count was 2388 throughout. `board_full` p50 is less noisy than p95, and the
step is visible in it on **both** NUC runners:

| Runner | Before (Sep 29 to Oct 4) | After (Oct 5 to Oct 19) |
|---|---|---|
| `gitlab-runner-nuc-2-visiban` | p50 910-995ms (4 runs) | p50 1074-1200ms (6 runs) |
| `Runner-03-NUC` | p50 944ms (Oct 3)\* | p50 1050-1326ms (8 runs) |

\* The Sep 30 `Runner-03-NUC` run (p50 1180ms, p95 1335ms) is the only slow run before
Oct 5. Its commit predates every change below, so it was runner noise.

The first slow run is the Oct 5 run on `724fbdd4b`. The last fast run is the Oct 4 run on
`7dee839ca`. The job ran the same `python:3.12-slim` image and job definition on both
commits, and neither the load-test script nor `seed_demo_data` changed. Between them, only two packages changed in `backend/requirements.lock`:
`psycopg2-binary` 2.9.11 became `psycopg[binary]` 3.3.6 (#1252), and `mcp` 1.30.0 became
2.3.0 (#1253). The MCP SDK is not on the request path (`MCP_SERVER_ENABLED` is off in the
job). The `board_full` serializer change in the same window (#1077) only adds schema
`help_text`.

The 2026-10-12 failure (job 17100891040, p95 1650.7ms against the then-1430ms budget) was a
tail outlier on top of that step. Its p50 was 1304ms. The next two runs, on a commit with
no change to the board serializer, view or requirements, measured p95 1228ms and 1155ms.

### Root cause, measured

Django 5.2 parses every `timestamptz` value through
`django.db.backends.postgresql.psycopg_any.BaseTzLoader`. That class subclasses psycopg's
**pure-Python** `TimestamptzLoader`, so each datetime is parsed by a regex in Python, even
with `psycopg[binary]` installed. The C loader that package ships is never used. This is
Django ticket [#36960](https://code.djangoproject.com/ticket/36960), fixed upstream only in
Django 6.1 (commit `994fa77`). psycopg2 parsed these values in C. One `board_full` response
on the load-test board loads about 44,000 `timestamptz` values (cards, movements and the
other prefetched rows).

The cause was measured locally on a scale-20 fixture against PostgreSQL 17. It was not
inferred from the dates.

- **Same SQL, same payload.** Under both drivers the endpoint runs 17 queries and returns
  byte-identical JSON. A query-count guard cannot see this regression.
- **The driver is the whole step.** A 2x2 run crossed pre-drift code (`7dee839ca`) and
  current code with each driver: 4 interleaved rounds of 40 requests each, `DEBUG=True` as
  in CI. Changing the driver added about 11-12% to the mean on either code base:
  622ms to 696ms on the old code, 630ms to 700ms on the current code. Changing the code
  under psycopg2 added about 1% (622ms to 630ms). p95 rose more than the mean (about 667ms
  to 839ms on the old code), which matches the ~25% p95 rise CI saw.
- **The loader is where the time goes.** In a profile of 5 requests, psycopg 3 spent about
  44,000 calls per request in `psycopg/types/datetime.py` `_load_iso`. Per value, loading
  cost about 1.5µs under psycopg 3, against 0.47µs under psycopg2.

### The fix

`backend/visiban/db_psycopg.py` backports Django 6.1's change onto 5.2's `BaseTzLoader`. It
wraps the loader psycopg registers by default (the C one with `psycopg[binary]`) and then
re-applies the connection timezone exactly as Django did before. `boards.apps.BoardsConfig.ready()`
applies it. Results are byte-identical to psycopg2's for the whole `board_full` response.
With the backport in place:

- per-value loading drops from about 1.5µs to about 0.69µs (psycopg2: 0.47µs);
- the C core of psycopg 3 costs the same as psycopg2's on this endpoint. What is left is
  the Python wrapper and `.replace(tzinfo=...)`, which upstream Django 6.1 also keeps;
- `boards/tests/test_db_psycopg.py` pins it. Values must match Django's original loader, and
  a real query on PostgreSQL must never call the Python ISO parser. That test fails without
  the backport. It also checks that the backport turns itself off on a Django that already
  has the upstream fix. **Delete the module once the `Django` pin reaches 6.1.**

The backport removes most of the driver cost, not all of it. Expect `board_full` to settle a
few percent above its pre-Oct-5 level rather than exactly back at it. The committed budget
(1695ms, from a run before this fix) is therefore looser than the endpoint now needs. Tighten
it at the next re-derivation by following the procedure above, from a run that includes this
fix. Never hand-edit it.

### What #1800 did not explain

- `card_search` (p50 about 80ms to 84-90ms over the same period, and 100.7ms on
  `runner-04-NUC`) and `notifications_unread` show no driver effect locally: psycopg2,
  psycopg 3 and psycopg 3 with the backport measured within noise of each other. Their
  smaller rise is not the timestamptz loader. Locally, `card_search` was also about 14%
  slower on current code than on `7dee839ca` with the same 8 queries. That comparison spans
  two separately seeded databases and was not taken further here.
- The job installs `backend/requirements.txt`, not the hash-pinned `requirements.lock`, and
  its `python:3.12-slim` and `postgres:17-alpine` images are floating tags. Transitive
  dependency and base-image versions can therefore change between two runs on the same
  commit. That did not cause this drift (the lock diff above is the full set of pinned
  changes, and the step lines up with #1252's merge). When attributing a future drift, check
  for it.

## Failure notifications

The scheduled run is blocking (not `allow_failure`), so a budget violation fails the pipeline.
GitLab emails every project maintainer who has "Failed pipeline" notifications enabled
(**Profile → Notifications → Global notification level → "Watch"**, or per-project under
**Project → Notification settings**) — the same mechanism the nightly CVE scan
(`backend-dep-scan` / `frontend-dep-scan`, on the separate Nightly schedule) already relies
on. The load test keeps its own schedule rather than joining that one because its latency
budgets are noisy on a busy runner — see [Scheduled pipelines](ci-gates.md#scheduled-pipelines).
