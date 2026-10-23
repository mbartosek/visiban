# API compatibility gate

Visiban 1.0+ treats the REST schema and the operator-facing environment variable names as a
public contract (see "Backward compatibility" in `CLAUDE.md`). The `api-compat-check` CI job
(#1667) diffs both against the last GA tag on every MR and on `main`, so a removal can no
longer slip through on a green pipeline.

It is a sibling of `backend-schema-validate` (is the document well formed?) and
`backend-schema-fuzz` (does the API match the document?). This job asks a third question: does
today's contract still contain everything the last release promised?

## What it checks

**OpenAPI** — the schema `manage.py spectacular` emits for the branch is compared with
`scripts/compat-baseline/v1.1.0/openapi.json`. `$ref` and `allOf` are resolved on both sides,
so a renamed component is not a finding; only what a client can observe is.

| Finding | Meaning |
|---|---|
| `REMOVED_OPERATION` | A path + method present in the baseline is gone |
| `REMOVED_RESPONSE_STATUS` | A 2xx status the baseline documented is gone |
| `REMOVED_RESPONSE_FIELD` / `REMOVED_REQUEST_FIELD` | A property was removed |
| `TYPE_CHANGED` | A property, parameter, or body changed type |
| `RESPONSE_FIELD_BECAME_OPTIONAL` | A response property is no longer always present |
| `RESPONSE_BECAME_NULLABLE` | A response property may now be `null` |
| `REQUEST_FIELD_BECAME_REQUIRED`, `NEW_REQUIRED_REQUEST_FIELD`, `NEW_REQUIRED_REQUEST_BODY` | Existing clients now fail validation |
| `REQUEST_STOPPED_ACCEPTING_NULL`, `REQUEST_ENUM_VALUE_REMOVED` | A request value that used to be accepted is not |
| `REMOVED_PARAMETER`, `PARAMETER_BECAME_REQUIRED`, `NEW_REQUIRED_PARAMETER`, `PARAMETER_TYPE_CHANGED` | The same, for query/header/path parameters |
| `REMOVED_REQUEST_MEDIA_TYPE`, `REMOVED_RESPONSE_MEDIA_TYPE` | A content type is no longer offered |

Additions (new endpoints, new optional fields, new optional parameters) always pass. Error
statuses (4xx/5xx) are not part of the checked contract. A baseline property that is a bare
read-only `string` is treated as untyped: it is drf-spectacular's fallback for an unannotated
`SerializerMethodField`, so correcting it to its real type is not a break.

**Settings** — every env var name that `backend/visiban/settings.py` reads at the GA tag
(`env(...)`, `env.<type>(...)`, `_secret_env(...)`, `os.environ`/`os.getenv`, and the legacy
name passed to `_warn_deprecated_env_alias`) must still be read at HEAD. A deprecated alias
that is kept counts as present, which is exactly the rename policy in `CLAUDE.md`. The
baseline is `scripts/compat-baseline/v1.1.0/settings-env.txt`.

## Component names are not part of the contract

The gate resolves `$ref` and `allOf`, so a renamed component is never a finding. A generated
client does turn a component name into a type name, so a rename is still worth a decision, made
once and recorded:

- **Pin the old name** (`@extend_schema_serializer(component_name=...)`) only when a plain
  rename is the whole change, as #1825 did for dj-rest-auth's password request bodies.
- **Otherwise keep the new name** and list the old-to-new mapping in the upgrade guide. #1834
  did this for `/auth/user/` (`User` to `CurrentUser`) and for the dj-rest-auth response
  components: pinning `User` back would have collided with `UserSerializer`'s component, which
  `POST /auth/choose-username/` returns, so one of the two would have needed a rename anyway.

Decide before the baseline roll: the roll freezes whatever names the schema has that day.

Decided in #1874, ahead of the first baseline roll that contains them (none of the names
below exist in the v1.1.0 baseline, so nothing shipped was renamed):

- **No `*RequestRequest` components.** `COMPONENT_SPLIT_REQUEST` already appends `Request`
  to a request component, so an `inline_serializer(name="...")` on a `request=` must not end
  in `Request`. Use `name="CardMove"` to get `CardMoveRequest`. This applied to the card
  move, checklist/column/custom-field/swimlane/swimlane-custom-field reorder, board share,
  add-or-update-member, and personal-access-token create bodies. The share body's expiry enum
  is now `ShareBoardExpiresInDaysEnum`.
- **List endpoints get `_list` operationIds.** `api_v1_admin_action_log_list`,
  `api_v1_admin_board_invite_links_list` and `api_v1_boards_events_list` are set explicitly
  with `operation_id=` because the generator names a non-`ViewSet` `APIView` GET, and the
  `events` action, `_retrieve`.
- A 1.1 name that shipped keeps its name; only 1.2-new names were changed.

`backend/boards/tests/test_schema_generated_names_1874.py` fails if a `*RequestRequest`
component or one of those `_retrieve` operationIds reappears.

## Allow-listing a deliberate break

A break that is intentional (a planned major bump, or a removal of an item already deprecated
for a full release) goes in `scripts/compat-baseline/allowlist.json`. Every entry needs an
`issue` (`#1234`) and a `reason`, and exactly one target:

```json
{"allow": [
  {"key": "REMOVED_RESPONSE_FIELD GET /api/v1/boards/ response 200 application/json .results[] .legacy_name",
   "issue": "#1700", "reason": "deprecated in 1.2, removal scheduled for 2.0"},
  {"operation": "POST /api/v1/old/", "issue": "#1701", "reason": "endpoint retired after the 410 period"}
]}
```

- `key` is the exact text the gate prints (`*` is a wildcard).
- `operation` (`"METHOD /path/"`) covers every finding under that one operation.

An entry that no longer matches any finding is **stale and fails the job**, so the list can
only shrink toward the truth. Entries without an issue reference fail the job outright.

Prefer `key` entries. An `operation` entry silently covers every future finding under that
operation, so use it only when the whole operation is deliberately retired (the five
`#1080` catch-all entries are an example). The self-test pins the opposite behavior for
`key` entries: a *new* finding under an operation that already has entries still fails.

## Experimental operations

An operation documented as experimental (no stability promise until it graduates) must not
freeze when the baseline is rolled. List it in `scripts/compat-baseline/experimental.json`
(#1875), which is separate from the allow-list: the allow-list excuses one specific break,
this keeps a whole operation out of the contract.

```json
{"experimental": [
  {"path_pattern": "/api/v1/git-lens/*", "issue": "#1875", "reason": "documented experimental in docs/api/git-lens.md"}
]}
```

- `path_pattern` is an `fnmatch` pattern against the schema path (`*` is a wildcard).
- Every entry needs an `issue` and a `reason`; the gate fails without them.
- Matching operations are skipped on both sides of the diff, and the gate prints each skipped
  `METHOD /path/` in the CI log so the exemption is visible.
- An entry that matches no operation in the baseline or at HEAD is **dead and fails the job**.
- `--normalize` honors the list, so a baseline regenerated at a GA tag never contains these
  operations (see "Regenerating the baseline"). Components are compared only through the
  operations that reference them, so a component shared with a non-experimental operation is
  still checked through that operation.
- **When to add an entry:** only for a surface the public docs already call experimental, with
  a tracking issue. Do not use it to get a pipeline green.
- **Leaving experimental:** remove the entry **before** the baseline roll that should freeze the
  operation, and update the API doc's experimental notice in the same MR. Removing it after the
  roll leaves the operation permanently unfrozen.

The only entries today are the Git Lens REST endpoints. The `lens_connection.*` WebSocket
events and the `git_lens_enabled` user field are deliberately frozen (#1666) and are not listed.

### Frozen snapshot of the v1.1.0 baseline drift

The v1.1.0 schema was generated before the schema-hygiene work (#1135-#1138, #1209, #1080) and
documented fallback or wrong serializers for a number of operations (for example a card
serializer standing in for the request body of `checklist/` and `comments/`). Diffing against
it yields findings that look like documentation corrections rather than runtime changes.

A read-only comparison of the v1.1.0 and current runtime code verified these classes as
**documentation-only baseline errors, not breaks**:

- `card_count` / `member_count` on `GET /boards/{id}/full/`: the v1.1.0 `BoardFullSerializer`
  never had them; the v1.1.0 schema showed the viewset's default `BoardSerializer`.
- `assignee` / `created_by` nullable on cards: v1.1.0 already returned `null` for the
  `SET_NULL` foreign keys; only the schema (`allow_null=True`) changed.
- `POST /boards/import/` `file`: v1.1.0 was already multipart-only and answered 400
  "No file provided." without it.
- `object` -> `array` list responses (activities, attachments, movements, saved-filters, and
  the group boards / descendant-boards / invite-links / labels / subgroups actions): these
  hand-written `@action`s returned bare arrays in both versions; only the schema's
  `pagination_class` was fixed.
- 2xx status changes: an AST scan found none that differ at runtime.

The same comparison found no serializer field and no URL route removed between v1.1.0 and
`main` (the `dj_rest_auth` `include()` became explicit routes, and five declarations were
rewritten as same-name `SerializerMethodField`s).

Those findings are therefore frozen as exact per-finding `key` entries (generated once from
the gate's own output with an empty allow-list, sorted for stable diffs). The snapshot is
closed: anything not already in it, including a further change to an operation that has
entries, fails the job. Never add to it to get a pipeline green; a new finding is a real break
or needs its own issue-referenced entry. The temporary `#1788`, `#1783` and `#1792` entries described
below are such entries; none of them is part of the burn-down count above.
The exception is an operation covered by an `operation` entry (the `#1783` ones): every finding
under it passes, including a new one. That is why those entries must be deleted at the v1.2.0
roll.

**Burn-down result** (#1695). Every one of the 51 operations with entries was checked against
the v1.1.0 *view code* (`git show v1.1.0:backend/...`), not the v1.1.0 schema, and each entry's
`reason` now records what v1.1.0 actually read and returned. The outcome:

- **556 entries under `#1695` are documentation-only.** The v1.1.0 schema showed a fallback
  serializer for hand-written `@action`s and `APIView`s (a card serializer for `move/`,
  `checklist/`, `comments/`, `attachments/`, `status/`, `timeline/`, `archived/`; a board
  serializer for `share/`, `members/`, `movements/`, `export-history/`; a group serializer for
  the group `labels/`, `members/`, `star/`, `invite-links/`, `boards/` actions), echoed the
  request serializer as the response for the dj-rest-auth subclasses (`login/`,
  `password/reset/`, `password/reset/confirm/`, `registration/`), and documented `200` where
  the view returned `201` (or `204` where `DELETE share/` returned `200`). None of the listed
  fields or statuses was ever on the wire.
- **4 entries under `#1302` are a real, deliberate runtime change**, already announced in
  `CHANGELOG.md`: `columns/reorder/` and `swimlanes/reorder/` used to treat a missing `order` as
  a `200` no-op and now answer `400` unless `order` is the board's full ID list. The
  upgrade-guide note for this is tracked in #1782.
- **3 `REMOVED_REQUEST_MEDIA_TYPE` entries on `POST /auth/password/change/` were a HEAD
  schema defect, not a removal** (#1781, fixed). `VisibanPasswordChangeSerializer.__init__`
  called `has_usable_password()` on `AnonymousUser` during schema generation, so HEAD's schema
  lost the request body. The fix replaced them with three
  `NEW_REQUIRED_REQUEST_FIELD ... .old_password` entries under `#1257`, the deliberate security
  break.

Until the baseline is rolled at the next GA, these entries stay in the file because the drift lives in the frozen baseline itself: deleting
an entry whose finding still fires turns the job red, whether the finding is drift or a real
break. Entries are only ever deleted (when one goes stale) or re-justified, never added.
The exceptions are the #1781 replacement above and the temporary #1788 entries below.
The temporary #1783 entries below are a third exception, and the #1792 entry a fourth.

Four temporary `key` entries under `#1788` cover `NEW_REQUIRED_REQUEST_BODY` on `POST swimlanes/`,
`PUT swimlanes/{id}/`, and `PATCH swimlanes/{id}/set-collapsed/` and `set_collapsed/`. The v1.1.0
baseline lacks those bodies because `SwimlaneViewSet` crashed during schema generation; #1788
fixes the schema, and the endpoints always required the body. Delete all four entries when the
baseline is rolled at the v1.2.0 GA.

Sixteen temporary `operation` entries under `#1783` cover hand-written actions and `APIView`s
whose schema showed a fallback serializer, no body, or a wrong status (for example `star/`,
`move-group/`, `saved-filters/`, card `archive/` and `unarchive/`, `summary/`, `analytics/`,
`export/`, the group `members/{user_id}/`, `transfer-ownership/` and `board-defaults/` actions,
`auth/change-password/`, `auth/choose-username/` and `auth/registration/resend-email/`).
The v1.1.0 baseline holds those wrong shapes, so each correction reads as a removal; no runtime
behavior changed. Delete all sixteen when the baseline is rolled at the v1.2.0 GA.

One temporary `key` entry under `#1792` covers `REQUEST_BODY_BECAME_REQUIRED` on `POST boards/import/`: the
baseline published the multipart upload body as optional, but the view has always answered 400 without
`file`. Delete it when the baseline is rolled at the v1.2.0 GA.

The GA roll does not fix the schema by itself. It freezes the schema HEAD generates at that tag
as the new baseline, so an operation that still documents a fallback serializer at the tag
carries that error into the next baseline. #1783 corrected the cases its sweep confirmed. The
board import request bodies were corrected in #1792; hand-written `GET`s that
publish no response body are tracked in #1793; any found later needs the same fix before the following roll.

## Running it locally

```bash
cd backend && source .venv/bin/activate   # the same env the backend tests use
cd .. && python3 scripts/check-api-compat.py --self-test
python3 scripts/check-api-compat.py
bash scripts/tests/check-api-compat.test.sh
```

## Regenerating the baseline (at each GA tag)

Do this once, when a new GA tag is cut, from a clean checkout of the tag. Run it in a
temporary worktree outside the repo and delete it afterward.

```bash
TAG=v1.2.0
VENV_PY="$PWD/backend/.venv/bin/python"   # run from the repository root
git worktree add --detach /tmp/visiban-$TAG $TAG
( cd /tmp/visiban-$TAG/backend \
  && DATABASE_URL=postgres://u:p@localhost:5432/d REDIS_URL=redis://localhost:6379/0 \
     DJANGO_SECRET_KEY=x DEBUG=True ALLOWED_HOSTS=localhost GIT_LENS_ENABLED=True \
     "$VENV_PY" manage.py spectacular --format openapi-json --file /tmp/$TAG.json )
mkdir -p scripts/compat-baseline/$TAG
python3 scripts/check-api-compat.py --normalize /tmp/$TAG.json scripts/compat-baseline/$TAG/openapi.json
python3 scripts/check-api-compat.py --dump-env /tmp/visiban-$TAG/backend/visiban/settings.py \
  > scripts/compat-baseline/$TAG/settings-env.txt
git worktree remove /tmp/visiban-$TAG
```

Then, as a manual step on the
release checklist, point `BASELINE_SCHEMA` / `BASELINE_ENV` in `scripts/check-api-compat.py`
(lines 52-53) at the new
directory, delete every allow-list entry the new baseline no longer needs, and delete the old
baseline directory. `--normalize` strips prose, examples, and `x-` extensions so the fixture
stays small and docs edits never read as contract changes, and drops every operation listed in
`experimental.json`. Before the roll, remove the entry of any operation that should now freeze.

Every allow-list entry is a diff against the v1.1.0 baseline. Once the baseline is the v1.2.0
schema and HEAD matches it, none of those findings fire, so all entries go stale, and a stale
entry fails the job. After regenerating, run `python3 scripts/check-api-compat.py`, delete every
entry it reports as stale (expected: the whole list, leaving `{"allow": []}`), and re-run until
it prints OK with no stale entries. As a safety net for the temporary entries (those whose
`reason` starts with `temporary:`, citing #1783, #1788, #1789 or #1792), this lists any left
behind; after deleting, it must print nothing:

```bash
python3 -c "import json;[print(e['issue'],e.get('operation') or e['key']) for e in json.load(open('scripts/compat-baseline/allowlist.json'))['allow'] if e['reason'].startswith('temporary:') or e['issue'] in ('#1783','#1788','#1789','#1792')]"
```

A temporary entry left behind fails the roll MR loudly. Then, in this file, delete every sentence
or paragraph that describes temporary entries or counts exceptions to the "never added" rule
(including any added for #1789 or #1792), keeping the existing "never added" sentence as is, and
revise the prose that describes the v1.1.0 baseline drift and allow-list burn-down (the
"Frozen snapshot of the v1.1.0 baseline drift" section, the `#1080` catch-all mentions, the
`v1.1.0/` paths, and the paragraph above) to the new baseline.

## Not covered

- Serializer fields that never reach the schema (the schema is the source of truth here;
  `serializer-ts-parity` covers frontend interfaces).
- WebSocket events: see [WebSocket event contract](websocket-event-contract.md).
- Settings *values* and defaults; only names are compared.
