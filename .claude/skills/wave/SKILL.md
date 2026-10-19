---
name: wave
model: opus
disable-model-invocation: true
argument-hint: "[milestone] [label ...]"
description: >
  Pick up a batch of milestone issues and land them in parallel — one git
  worktree and one delegated agent per issue, each finishing with its own MR.
  Asks which milestone and which labels to pull from; never infers either.
  Enforces token-discipline (Sonnet by default, a hard wave cap, per-agent
  commit verification). User-invoked only — it spawns many agents and spends
  real money, so it must never start on a model's own initiative.
---

# Wave

Land several milestone issues in one wave. Each issue gets its own worktree, its
own delegated agent, and its own MR. You are the orchestrator: you choose the
issues, brief the agents, verify what they actually produced, and report. You do
**not** implement the issues yourself.

This skill is for issues that sit in a milestone. A one-off chore with no issue uses the
same mechanics by hand (worktree via `scripts/wt`, one delegated agent, the Step 5
contract); there is nothing here to select.

> **Renamed from `batch`.** A project skill named `batch` hides Claude Code's bundled
> `/batch`: a project skill replaces a bundled skill of the same name. Observed in
> Blueprint #101 on Claude Code 2.1.300, where Blueprint's `batch` won and the built-in
> was unreachable. Do not reintroduce a project skill under a bundled name.

## Why this skill is shaped the way it is

`CLAUDE.md`'s own cost-discipline section puts this project's active-session spend
at roughly 110M tokens/day, and warns that "a cache miss on a 1M-token working
context costs several dollars." A batch multiplies that risk by however many
agents run at once: `turn count × context size × fan-out`. A fan-out of five
agents that each wander into unnecessary exploration costs the same as landing
twenty-five focused ones. Every rule below attacks one of those three factors —
turn count, context size, or fan-out — not because a specific incident measured
it here, but because the arithmetic is the same regardless of project.

The only measurement behind these rules is upstream, not Visiban's: freehand batch work
in TruePPM (TruePPM #3659, from trueppm-suite) cost **~122M tokens per merged MR**, with
**95.9% of it cache reads**. No Visiban measurement exists from after this skill was
adopted, so treat that figure as the rationale for the shape, not as a Visiban baseline.

Do not treat these as style preferences. A wave that ignores them is not merely
slower — it is the difference between a wave that costs a few million tokens and
one that costs an order of magnitude more for the same landed MRs.

## Step 1 — Ask. Never infer.

Ask both questions with `AskUserQuestion` in a **single call** (two questions, one
round trip). Do not guess either answer, and do not skip the ask because the
answer looks obvious.

1. **Which milestone?** Offer the active dated milestones from
   `glab api "projects/:id/milestones?state=active"`, nearest due date first.
2. **Which labels to pull from?** Multi-select. Offer the domain labels actually
   present on that milestone's open issues — read them, do not offer a
   hardcoded list. If the project starts using `release::` commitment labels
   (committed/reserve/stretch), offer those too.

Also ask **wave size** if the user has not said one, defaulting to **5**. See the
cap rule below for why the number is small.

If the user names milestone and labels in their invocation (`/wave 1.2
lens`), take them and skip the ask.

## Step 2 — Select the issues

```bash
glab issue list --milestone <M> --label <L> --per-page 100
```

`--per-page 100` is a first page, not a result set. A milestone with more open
issues than that truncates silently here, and the issues you never see are
indistinguishable from issues that do not exist. When the count comes back at
exactly the page size, re-read it with
`glab api --paginate "projects/:id/issues?milestone=<M>&labels=<L>&state=opened&per_page=100"`
— which emits one JSON array **per page, concatenated**, so decode it with a
`raw_decode` loop rather than `json.load`.

Then filter, in this order:

1. **Drop anything already claimed** — an issue carrying the check-out label
   (`status::wip` by default, `WT_LOCK_LABEL`), an issue number that already has an open
   MR, or a worktree in `scripts/wt list`. `scripts/wt new` refuses a checked-out issue
   (see `scripts/wt help`), and `scripts/check-issue-collision.sh` backstops it at push
   time — but check first so you are not selecting work you cannot start. To exclude
   claimed issues up front, add `--not-label status::wip` to the list command. Read the
   check-out comment (`🔒 checked out …`) before assuming a label is stale: it names the
   branch and worktree holding the issue. A label with no live worktree behind it is a
   stale lock — ask the user before taking it over; never `--force` it on your own.
2. **Drop anything blocked** on an unmerged branch or an unanswered 🔴 question
   from a prior `architect`/`ux-design` review.
3. **Prefer issues with an identified root cause.** An issue whose body already
   names the file and the mechanism is one an agent can finish in a bounded
   number of turns. A vague issue is where an agent starts exploring and the
   turn count runs away.
4. **Prefer independent issues.** Two agents touching the same file collide on
   the merged tree even when both are green alone — check the paths each issue
   implies before pairing them in one wave (`backend/boards/views.py`,
   `frontend/src/components/**`, `changelog.d/*`, and `frontend/CLAUDE.md`'s
   numbered rule list are the recurring hotspots in this repo).
5. **Respect any `release::` ordering** if the milestone uses those labels —
   committed before reserve before stretch, unless the user said otherwise.

Report the selected list to the user before spawning anything. One line per
issue: number, title, chosen model, chosen effort, and why (the escalation criterion met, or
"default").

## Step 3 — One worktree per issue

Never let two agents share a checkout, and never create the worktrees from
inside an agent.

```bash
scripts/wt new <issue>      # branch + worktree off latest origin/main, claims status::wip
```

`wt new` also **checks the issue out**: it applies the `status::wip` label and posts a
check-out comment, so a second `/wave` run or a parallel session sees the issue as taken.
If it refuses an already-checked-out issue, drop that issue from the wave and say so in
the report — `--force` is the user's call, not yours. The label is released by
`scripts/wt remove` and `scripts/wt prune`; `scripts/wt release <issue>` clears it by hand.

- The WIP cap is 10 (`WT_CAP`, default in `scripts/wt`). If the wave would
  exceed it, run `scripts/wt prune` first (it reaps worktrees whose MRs already merged),
  then either shrink the wave or raise it deliberately with
  `WT_CAP=<n> scripts/wt new <issue>` — and say so in your report. Do **not**
  delete another session's worktree to make room; a worktree you did not
  create belongs to someone else's session.
- Each worktree's `.envrc` sets `COMPOSE_PROJECT_NAME` to reuse the main
  checkout's running Docker Compose stack (for anything that genuinely needs
  Postgres/Valkey, e.g. WebSocket/channels work), but `wt new` also seeds a
  worktree-local `backend/.env` pointing at an **isolated SQLite database** —
  each worktree is already its own directory, so `pytest`/`manage.py test`
  runs never collide across agents, no port arithmetic needed. Tell each
  agent's brief not to overwrite that seeded `backend/.env` unless the issue
  specifically needs the shared Postgres stack.
- Frontend e2e is isolated too: `wt new` writes a deterministic `WT_E2E_PORT`
  into `.envrc`, which `playwright.config.ts` reads — without it, a second
  agent's Playwright run would either fight over port 5173 or (worse) silently
  attach to a sibling worktree's already-running dev server via
  `reuseExistingServer`. `source .envrc` (or direnv) before running e2e.
- `git stash` is **not** worktree-scoped. Tell every agent to use
  `scripts/wt stash`, never bare `git stash` — see `scripts/wt help`.

## Step 4 — Delegate, on the cheapest model that can do the job

One agent per issue, all spawned **in a single message** so they run
concurrently, each as `Agent(subagent_type: "general-purpose", model: …, effort: …)`. The
implementer needs `Edit`, `Bash`, and `Agent` (for its pre-MR gate batch); no read-only
gate agent has all three. Agents run under this session's permission mode.

### Model choice

**Default to Sonnet.** Most issues in this project's fast-path table (bugfixes,
single-endpoint features, settings sub-page wiring) do not need Opus.

Escalate to Opus only when the issue meets one of these, and **say which one** in
your report:

- The root cause is unknown and must be found, not just fixed.
- The fix spans both `backend/` and `frontend/` with a contract change (a
  serializer field, a WebSocket event shape) that both sides must agree on.
- It changes the git-lens pipeline-derivation logic, board WebSocket broadcast
  semantics, or another area with subtle invariants.
- It requires a design judgment the issue does not settle — a new interaction
  pattern, an OSS/Enterprise boundary call, or a backward-compatibility
  question under `CLAUDE.md`'s API contract rules.

"This issue is important" is not escalation criteria. Neither is "it touches
security" — a one-line permission-class fix with a named root cause is Sonnet
work; `rbac-check` and `security-review` catch what Sonnet misses.

**Read-only gates always run on Sonnet.** `regression-check`, `rbac-check`,
`perf-check`, `security-review`, `broadcast-check`, `migration-check` read a diff and
report findings. None needs Opus. Pass `model: "sonnet"` on the Agent call when you spawn
them: an `Agent` call without `model` uses the agent file's `model:` if it has one and
otherwise inherits this session's Opus, and in this repo `regression-check` and
`security-review` (and `architect`, `ux-design`) carry no `model:` in their
`.claude/agents/` frontmatter at all, so an omitted `model` runs them on Opus. Where an
agent file does set a model (`rbac-check`, `perf-check`, `broadcast-check`,
`migration-check`, `completeness-check` set `sonnet`), the Agent tool's `model` parameter
overrides it, so passing it is harmless. **`completeness-check` is the exception:** it
is read-only too, but it follows the escalation criteria above (Opus when the
branch meets one, Sonnet otherwise) — see `.claude/agents/completeness-check.md`.

### Every spawn names its model and effort — never omit either

This skill runs as `model: opus`. An `Agent` call **without** `model` uses the agent
file's `model:` if it has one and otherwise inherits the orchestrator's. The implementer
is `general-purpose`, which has none, so an omitted `model` silently runs it on Opus and
defeats the Sonnet default. An omitted `effort` likewise falls back. Pass both on every
spawn this wave makes — implementers, gates, and `completeness-check` — from this table.

Caveat: `CLAUDE_CODE_SUBAGENT_MODEL` sets a default only when `model` is omitted; its
`_FORCE` variant, and `CLAUDE_CODE_EFFORT_LEVEL` for effort, instead remove the parameter
from the Agent tool, so you cannot pass it. If either is set, say so in the report rather
than calling the omission a defect.

| Agent | Model | Effort |
|---|---|---|
| Implementer, default | `sonnet` | `medium` |
| Implementer, escalated (a criterion above, named in the report) | `opus` | `high` |
| Read-only gates (`regression-check`, `rbac-check`, `perf-check`, `security-review`, `broadcast-check`, `migration-check`) | `sonnet` | `medium` |
| `completeness-check` round 1 | `sonnet`, or `opus` on its own criteria | `high` |
| `completeness-check` round 2 | `opus` | `high` |
| `completeness-check/fix-diff` re-check | `sonnet`, or `opus` if round 1 escalated | `medium` |

Effort scales the reasoning spent per turn, not the context re-read on every turn, so it
is not where the cost concentrates. It goes up where the job is reasoning (an escalated
implementer, and `completeness-check`, which hunts for what the diff does *not* contain)
and stays `medium` where it is pattern-matching. Do not use `low` for an implementer: an
under-thought fix that fails its gates costs more turns than it saved.

Show both columns everywhere you list agents — the Step 2 selection, every status table
while the wave runs (`# | Issue | Model | Effort | Status | MR`), and the Step 7 report —
using the values you actually passed. For gates the implementer spawned, use the values it
reported (Step 5.4). If an agent was spawned without one, write `inherited` and treat the
omission as a defect in this run, unless an override variable above removed the parameter.

### The brief

Each agent's prompt must be **self-contained**. An agent that has to go
rediscover context spends its budget on cache reads of files you could have
named. Include:

- The issue number, title, the **full issue body**, and **its comments**
  (`glab issue view N --comments`) — paste them, do not make the agent fetch them. Scope
  corrections live in comments; an agent briefed from the body alone builds a requirement
  that may already have been superseded, and `completeness-check` then reports the branch
  as missing it.
- Its worktree path, and the instruction to `cd` there and `source .envrc` first.
- The specific files or symbols to start from, if the issue names them.
- The exact gates that apply to this diff, from `CLAUDE.md`'s fast-path table —
  and which are `n/a`, so the agent does not run the whole battery. The implementer spawns
  only the pre-MR batch, as one parallel batch, with `model: "sonnet"` and
  `effort: "medium"` on each call (see the table above), and tells you which values it
  passed. It writes tests and docs itself.
- The scoped test command — the affected test file, not the whole suite
  (`cd backend && pytest <path> -q`, or `cd frontend && npx vitest run <file>`,
  or `cd frontend && npx playwright test e2e/<spec>.spec.ts`).
- The completion contract from Step 5.
- **Frontend issues:** also run `cd frontend && npx tsc -p tsconfig.e2e.json --noEmit`
  (it catches e2e fixture drift that `tsconfig.app.json` does not) and require Vitest
  diff-coverage of at least 80% on the changed lines. Playwright does not count toward it.
- **Confidential issues:** check the issue's `confidential` flag with
  `glab api projects/:id/issues/<n>` — the flag, not the body. Branches mirror to public
  GitHub, so a confidential issue must not produce a public branch or MR that discloses
  it (#1489 leaked into public !1225). If it is confidential, ask the user before any
  push.
- **MR bodies:** `glab mr create --description` replaces the default MR template and drops
  its Apache 2.0 checkbox. Brief the agent to append the template's Checklist
  (`.gitlab/merge_request_templates/Default.md`) and leave the Apache box **unchecked**
  for the user.
- **Shared scratchpad:** parallel agents share one scratchpad directory, so every backup
  or temp file name must include the issue number. An unnumbered `foo.bak` from one agent
  gets restored by another (it clobbered #1290 with #1289's file).

Tell each agent explicitly:

- **Do not re-delegate.** A subagent must execute, not spawn more agents.
- **Do not run the full test suite.** Scoped test commands only (see above).
- **Batch independent tool calls** into one message.
- **Stop and report** if blocked after two attempts at the same failure, rather
  than looping.
- **Kill stray full-suite runs** (`pytest`, `vitest` without a file argument) before
  handing back.

Gate agents you spawn yourself get the same discipline in reverse: **never `git checkout`
in an implementer's worktree.** A gate agent briefed with a SHA can detach the
implementer's HEAD. Brief gate agents to read the diff (`git diff`, `git show`) in place,
and run `git branch --show-current` in the worktree before re-briefing an implementer to
confirm it is still on its branch.

## Step 5 — Completion contract

Every agent finishes by, in order:

1. Tests and docs in the **same commit** as the code change, per `CLAUDE.md`.
2. A changelog fragment at `changelog.d/<issue>.<type>.md`, unless the change is
   exempt (chore/ci/docs, matching the `changelog-check` job's exemptions).
3. Local scoped checks green: `cd frontend && npm run lint -- src/ --max-warnings 15`
   and `npx tsc -p tsconfig.app.json --noEmit` for frontend changes;
   `cd backend && ruff check . && python manage.py makemigrations --check --dry-run`
   for backend changes touching `models.py`. Log each long check to a **file** rather
   than piping it through `tail` — the pipe swallows the diagnostic, and an OOM kill then
   reads as a bare exit code.
4. **Commit and stop — do not push yet.** Report the commit SHA, the pre-MR
   gate ledger, and the model and effort it passed to each gate agent, to the orchestrator. The orchestrator then spawns
   `Agent(subagent_type: "completeness-check")` (`.claude/agents/completeness-check.md`)
   on the unpushed branch — a fresh agent that did not write it, model per its own
   escalation criteria — and every BLOCKER and GAP is fixed on the branch or
   deferred to an **open** issue. The orchestrator re-briefs the implementer (via
   `SendMessage`) to fix them, in a **new commit** (never an amend), so the fix diff
   stays separable from the audited branch.
   A deferred *security* finding goes to an open issue labeled `security::deferred` and a milestone set for planning
   (`docs/maintainers/security-deferred-label.md`); the pre-tag and `/pre-release` check scans all open `security::deferred` issues regardless of milestone.

   **Then decide which second pass runs — at most one, per the "After round 1"
   table in `.claude/agents/completeness-check.md`.** If round 1 reported a
   `class-missed` or `collateral` **BLOCKER**, or **4 or more** BLOCKERS + GAPS in
   total, run **round 2** instead of the fix-diff re-check: a fresh **Opus**
   `Agent(subagent_type: "completeness-check")` auditing the **whole branch** at HEAD
   (fix commits included), briefed exactly as round 1 was and **not shown round 1's
   findings**. Compare the two lists and record `overlap k/M`. Hard stop after round
   2: if it still reports a BLOCKER, do not start round 3 — put the choice to the
   user. Otherwise (round 2 not triggered), decide whether the fix diff needs its own
   re-check — once, narrowly. Spawn a fresh `Agent(subagent_type: "completeness-check")` scoped to the fix
   commit(s) only (`git diff <audited-sha>..HEAD`), not the whole branch, when
   **either** holds:
   - the first check returned any **BLOCKER**; or
   - a fix commit changes **executable behavior** — application code, CI or chart
     logic, a gate or check script, or a migration — rather than only docs, tests,
     comments, or changelog text.

   Model (fix-diff re-check only; round 2 is always Opus): Sonnet, unless the branch met the escalation criteria above. **One pass
   only, never a loop**: fix what it finds and stop — a re-check of a re-check is the
   whack-a-mole loop `/pre-release` warns about. Record it as its own ledger line
   (see Step 5.7). A fix round that touched only docs, tests, comments, or
   changelog text gets no re-check; say so in the report rather than skipping it
   silently. Only after this does the orchestrator tell the implementer to push.
5. **Check for a stale base, immediately before push — not at worktree
   creation.** `scripts/wt new` branches off latest `origin/main`, but the gate
   sequence above (especially an Opus completeness-check on an escalated issue)
   can take long enough that another issue in the *same wave* merges to main
   first. A branch that was fresh when created can be stale by the time its
   gates are done, and nothing before this step re-checks the base — the
   implementer's scoped tests only ever ran against the base it started from.

   Run `git fetch origin && git log HEAD..origin/main --oneline` in the
   worktree. If it prints anything:
   - Rebase onto `origin/main`.
   - Re-run whichever of this branch's own tests/gates touch the surface the
     new upstream commits changed — schema/parity checks if they touched
     serializers or `settings.py`, `migration-check` if `models.py` changed
     upstream, the affected test files if they touched a shared component —
     not just a blind re-run of the implementer's original scoped tests.
   - A clean `git merge-tree` is not sufficient evidence by itself: a
     textually-clean merge can still combine two independently-correct schema
     states into a wrong one. Confirm the *tests* pass post-rebase, not just
     that the rebase applied cleanly.
6. **Push, then open the MR itself**, reproducing the `mr` skill's format by running
   `glab mr create` directly. `/mr` is `disable-model-invocation` — an agent
   cannot call it and must not try. `.claude/skills/mr/SKILL.md` is the
   canonical format for both paths.
7. Include `Closes #NNN` in the MR description, the `completeness-check`
   `## Requirements` table, and a `## Gates` section with one
   `gate: <name> — <N> findings` line per gate run (including
   `completeness-check — <N> findings (model: <sonnet|opus>; causes: <tally>)`, plus
   `completeness-check/fix-diff — <N> findings (model: <sonnet|opus>)` when Step 5.4
   triggered the re-check, or — when Step 5.4 triggered round 2 instead —
   `completeness-check — <N> findings (round 2; opus; causes: <tally>; overlap <k>/<M>)`
   with `completeness-check/fix-diff — n/a`). `0 findings` is a real
   outcome; never omit a zero, and never conflate `n/a` with `skipped`. A fix round
   that did not trigger the re-check records `completeness-check/fix-diff — n/a`;
   one that triggered it and the user declined records `skipped`. If Step 5
   found a stale base, note the rebase and what was re-verified in the MR's
   `## Notes` section.
8. **Never merge.** Hand back the MR URL and stop.

The agent reports back twice: after step 4 (commit SHA and the pre-MR gate
ledger, unpushed), and at the end (MR URL, the full gate ledger, the commit SHA,
and anything it deliberately left undone).

## Step 6 — Verify before you believe it

**An agent reporting "done" is not evidence.** Check every agent's actual output:

```bash
cd ../visiban-wt/<leaf> && git log origin/main..HEAD --oneline    # are there commits?
git status --porcelain                                            # anything uncommitted?
```

Then confirm the MR is real and points at the right code:

```bash
glab api "projects/:id/merge_requests/<iid>" \
  | jq -r '"sha=\(.sha) pipeline=\(.head_pipeline.status) closes=\(.description|test("Closes #"))"'
```

`HEAD == MR sha == pipeline sha`. A worktree is not private — another session can
commit and push from it mid-edit — so verify rather than assume the agent's
summary describes what landed.

Read the MR description's `## Gates` section too. It must carry a `completeness-check`
line (plus the round 2 or `completeness-check/fix-diff` line when one applied); its
absence means the audit was skipped, and the branch goes back to Step 5.4 rather than
being reported done.

An agent that produced no commit costs the same as one that shipped. Re-brief it
with what was missing, or take the issue over yourself; do not report it as done.

## Step 7 — Report

One table: issue, model, effort, MR, pipeline status, commits, gate findings, with a row
per `completeness-check` pass under its issue carrying that pass's model and effort. Then
state plainly:

- Which issues did **not** land, and why.
- Whether you raised the WIP cap, and whether any worktrees need cleanup
  (`scripts/wt remove <issue>` or `scripts/wt prune`).
- The wave's shape: how many agents, how many on Opus, and at what effort.

Do not merge anything. Do not start another wave without being asked.

## Cost rules, condensed

- **Sonnet unless escalation criteria are met**; read-only gates always Sonnet
  (except `completeness-check`, which follows the same escalation criteria).
- **Name `model` and `effort` on every spawn.** Omitted, `general-purpose` agents run on
  this skill's Opus.
- **Cap the wave** (default 5, WIP cap 10). A wave much larger than that is past
  the point where the issues are genuinely independent.
- **Verify commits, don't trust summaries** — an empty agent is pure loss, and a
  wandering one costs many times a focused one.
- **Self-contained briefs.** Paste the issue body; name the files. Every fact the
  agent has to rediscover is re-read on every subsequent turn of that agent.
- **Scoped tests only.** Never the full suite inside an agent.
- **Pre-MR gates as one parallel batch**, never serially — see `CLAUDE.md`'s
  "Pre-MR gate batch" section. Then `completeness-check` once, serially, **before
  push** — it audits the branch the batch's fixes produced. Re-check the **fix diff
  only**, once, when the first check found a BLOCKER or a fix changed executable
  behavior — never the whole branch twice, never a loop.
- **Apply only the gates the diff earns** — `CLAUDE.md`'s fast-path table is
  authoritative. A bugfix with a known root cause does not need `architect`.
- **No re-delegation** from inside an agent.
- **Two-strike rule**: an agent stuck on the same failure twice stops and
  reports.
- **Long-running work goes to the background**, not to a polling loop.
- **Wait on background work silently** — no filler tool calls while a task is pending.

## Rules

- **Never merge.** Not after a green pipeline, not for a docs-only branch.
- **Never infer a commitment label.** If the milestone uses `release::` labels
  and an issue's tier is ambiguous, ask which applies rather than guessing.
- **Never commit to `main`**, and always branch from latest `origin/main` —
  `scripts/wt new` handles this.
- **Never bare `git stash`** in a worktree. `scripts/wt stash`.
- If the user says skip a gate, skip it and record it as `skipped` with their
  reason — not as `n/a`.
