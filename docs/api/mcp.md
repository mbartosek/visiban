# MCP Server

Visiban can expose its boards to AI agents over the [Model Context Protocol](https://spec.modelcontextprotocol.io) (MCP). An MCP-compatible client — Claude, Copilot, or a custom agent — connects to `/mcp`, authenticates with a Personal Access Token, and calls tools that read and write your Visiban data.

The feature is flag-gated: `/mcp` returns `404 Not Found` unless the server is started with `MCP_SERVER_ENABLED=true`.

!!! note
    This is the Phase 1 (OSS core) surface: board discovery, full card CRUD, and the `board://`/`card://` resources below. A full setup guide is tracked separately.

---

## Stability and versioning

> **Added in 1.2**

The MCP surface is a public contract from 1.2 and follows the same compatibility rules as the REST API from 1.0 onward. It is **not** experimental.

- **Tool names** (`list_boards`, `list_columns`, `list_swimlanes`, `list_cards`, `create_card`, `move_card`, `update_card`, `archive_card`) and **resource URIs** (`board://{board_id}`, `card://{card_id}`) are never removed or renamed in a minor or patch release.
- **Argument schemas** change additively only: new arguments are optional with a sensible default, and an existing argument is never removed, renamed, made required, or given a different type.
- **Results and error payloads** only gain fields; existing fields keep their name and type. The error `code` values listed under [Tool errors](#tool-errors) are never removed.
- A tool or resource that must go away is marked deprecated here for at least one minor release, and removal needs a major version bump.
- New tools, resources, optional arguments, and result fields can appear in any minor release, so clients should ignore fields they do not recognize.
- The configuration names `MCP_SERVER_ENABLED`, `MCP_ALLOWED_HOSTS`, `MCP_THROTTLE_*_RATE`, and the `/mcp` path follow the environment-variable rules: they are not renamed, and a rename keeps the old name as a deprecated alias for at least one minor release.

---

## Enabling the server

Set the environment variable on the backend and restart it:

```bash
MCP_SERVER_ENABLED=true
```

=== "Docker Compose"
    ```yaml
    services:
      backend:
        environment:
          MCP_SERVER_ENABLED: "true"
    ```

=== "Helm"
    ```yaml
    backend:
      extraEnv:
        - name: MCP_SERVER_ENABLED
          value: "true"
    ```

When the flag is off, the app is not installed, the `/mcp` route does not exist, and the MCP SDK is never imported — an install that does not use MCP exposes no additional surface.

---

## Transport

| | |
|---|---|
| Endpoint | `/mcp` |
| Transport | Streamable HTTP (MCP spec rev `2025-03-26`) |
| Methods | `POST` |
| Session mode | Stateless |

The server runs **stateless**: every request is self-contained and no session state is pinned to a single process. This means a deployment running several backend replicas needs no sticky-session configuration.

If you terminate TLS behind a reverse proxy, the `/mcp` location must disable response buffering — the transport holds a long-lived Server-Sent Events response open, and a buffering proxy will withhold event frames. The bundled Nginx templates and the Helm chart already include a correct `/mcp` block.

### DNS rebinding protection

The transport validates the `Host` and `Origin` headers of every request against the server's `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` settings. This stops a malicious web page from using DNS rebinding to reach the MCP server from a victim's browser. A request whose `Host` is not allowed is rejected with `421 Misdirected Request`. If you reach the server on a hostname the backend does not know about, add it to `ALLOWED_HOSTS`.

!!! warning "If `ALLOWED_HOSTS` is `*`"
    A wildcard cannot serve as a rebinding allowlist — the two settings guard different threats, and inheriting the wildcard would silently switch this protection off. Set `MCP_ALLOWED_HOSTS` to the hostnames clients actually reach `/mcp` on:

    ```bash
    MCP_ALLOWED_HOSTS=visiban.example.com,visiban.internal
    ```

    Until you do, the backend logs a warning at startup and only `Origin` checking (from `CORS_ALLOWED_ORIGINS`) applies. `MCP_ALLOWED_HOSTS` takes precedence over `ALLOWED_HOSTS` whenever it is set.

---

## Authentication

Every request to `/mcp` must carry a Visiban [Personal Access Token](../features/personal-access-tokens.md) in an `Authorization` header using the **`Bearer`** scheme:

```
Authorization: Bearer vbn_0123456789abcdef0123456789abcdef01234567
```

!!! warning "`Bearer` here, `Token` everywhere else"
    The REST API uses the `Token` scheme (see [Authentication](authentication.md)). `/mcp` uses `Bearer` because the MCP specification mandates it. The two schemes are not interchangeable: the REST API rejects `Bearer`, and `/mcp` rejects `Token`.

    Both resolve the **same** Personal Access Token records, so a token that is revoked, expired, or dropped by a password change stops working for MCP at the same moment it stops working for the REST API.

Create a token from **Profile → Access Tokens**, or via the API:

```bash
curl -s -X POST http://localhost:8000/api/v1/auth/tokens/ \
  -H "Authorization: Token <your-session-token>" \
  -H "Content-Type: application/json" \
  -d '{"name": "claude-desktop", "scopes": ["mcp:read"]}'
```

The plaintext token is shown **once**, at creation. Only a SHA-256 hash is stored.

### The `mcp:read` scope is required

> **Changed in 1.2**

`/mcp` accepts a token only if it carries the **`mcp:read`** [scope](authentication.md#scopes). This is not implied by anything else:

- a token scoped `read` and `write` for the REST API is **refused** at `/mcp`
- a token created **before 1.2** (no scopes recorded) is **refused** at `/mcp`, even though it still works across the whole REST API
- `mcp:write` does not imply `mcp:read`

The reason is consent, not capability. A PAT is a credential a user is instructed to paste into an agent, and an agent is a fundamentally different kind of consumer from a shell script. Requiring a scope that can only have been chosen deliberately means no pre-existing token quietly becomes an agent credential because MCP was switched on.

Scopes never widen access: an `mcp:read` token still sees exactly the boards its owner can see, with the same roles.

A token intended for both an agent and a script needs both sets of scopes, e.g. `["read", "mcp:read"]`.

### Write tools additionally require `mcp:write`

> **Added in 1.2**

`mcp:read` opens a session and is enough to call every read tool below. Calling any **write** tool (`create_card`, `move_card`, `update_card`, `archive_card`) additionally requires the **`mcp:write`** scope on the same token — non-hierarchically, exactly like every other scope pair: `mcp:read` does not grant `mcp:write`, and `mcp:write` alone does not even open a session (a token needs `mcp:read` to reach `/mcp` at all). A write-capable agent credential therefore needs **both**: `["mcp:read", "mcp:write"]`.

Unlike a missing `mcp:read` (rejected at the transport, before any tool runs), a missing `mcp:write` is reported by the tool itself as a normal result — `{"error": {"code": "missing_scope", "detail": "..."}}` — not a `401`. The session is valid; only that one call was declined.

### Errors

| Status | Meaning |
|---|---|
| `401 Unauthorized` | The `Authorization` header is missing, is not the `Bearer` scheme, or the token is unknown, expired, revoked, or belongs to a deactivated account. |
| `401 Unauthorized` | The token is valid but lacks the `mcp:read` scope, or predates scopes entirely. The message names the scope required. |
| `403 Forbidden` | The token's account has a pending forced change (`must_change_password` or `must_change_username`). Same message and code as the REST API (`error.data.code` is `must_change_username` for the username case); change the password or choose a username in the web app, then retry. |
| `404 Not Found` | `MCP_SERVER_ENABLED` is not set. |
| `421 Misdirected Request` | The `Host` header is not in `ALLOWED_HOSTS`. |

Authentication is enforced at the transport layer, before any MCP protocol frame is parsed — an unauthenticated caller cannot reach the protocol machinery at all. The `401` body is a JSON-RPC error envelope so that spec-compliant clients surface a useful message.

---

## Rate limiting

> **Added in 1.2**

DRF's request throttles (`DEFAULT_THROTTLE_RATES`) never apply to `/mcp` — the MCP server is mounted in-process at the ASGI level and calls tool functions directly, not through a DRF view. `/mcp` instead has its own per-token limiter, keyed on the presented Personal Access Token (never on IP, and never on the raw token value), so limits hold regardless of which network or proxy a caller connects through, and hold across every backend replica because they share the same cache backend the REST throttles already use.

Two limits stack on every call:

| Bucket | Applies to | Default | Setting |
|---|---|---|---|
| Baseline (read) | Every tool call and every resource read | `300/min` per token | `MCP_THROTTLE_READ_RATE` |
| Compute | `list_cards` and `board://{board_id}` only, **in addition to** the baseline limit above | `30/min` per token | `MCP_THROTTLE_COMPUTE_RATE` |

A call to `list_cards` or `board://{board_id}` spends a unit from **both** buckets; every other tool/resource spends only the baseline unit. There is no `DEBUG`-time bypass — unlike the REST throttle rates, these apply the same way in local development, since a runaway loop in an agent under development is exactly the case the compute bucket exists to catch.

This applies uniformly to every token, including a site admin's — there is no admin exemption from the throttle, unlike maintenance mode.

Both settings accept the same `<count>/<period>` syntax as DRF's throttle rates (`m`/`min`, `h`/`hour`, `s`/`sec`, `d`/`day` — e.g. `"300/min"`, `"20/hour"`). An operator who never sets either variable still gets the defaults above, not an unlimited server. A rate string that fails to parse raises `ImproperlyConfigured` at startup rather than silently disabling the limit.

```bash
MCP_THROTTLE_READ_RATE=300/min
MCP_THROTTLE_COMPUTE_RATE=30/min
```

### Failed-authentication limit (per IP)

> **Added in 1.2** (#1679)

The per-token limits above only apply once a token has resolved, so they cannot slow a caller who keeps presenting invalid tokens. `/mcp` therefore also applies an approximate per-IP limit to **failed** token lookups:

| Bucket | Applies to | Default | Setting |
|---|---|---|---|
| Pre-auth failures | Requests whose Bearer token is unknown, revoked, expired, or otherwise fails lookup (HTTP `401`) | `30/min` per client IP | `MCP_THROTTLE_PREAUTH_RATE` |

Only failed lookups spend this budget. A valid token that merely lacks the `mcp:read` scope is not counted, and neither are successful requests, but the check runs before the lookup for every request. Once an IP has used its budget, further requests from it, **valid ones included**, are answered `429` (a `Retry-After` header and a JSON-RPC error with `data.code: "throttled"` and `data.retry_after`) until the window slides. Valid agents that share an IP with a client that keeps failing (an agent looping on a revoked token, or a hostile host behind the same NAT) are therefore blocked too. This is a best-effort brake on guessing, not a hard cap: the counter is approximate, and concurrent or multi-replica failures can exceed the budget or lose counts. The client IP is resolved with the same `NUM_PROXIES` logic as the REST throttles; set `NUM_PROXIES` to match your proxy chain or all clients behind a proxy will share one bucket. The setting is optional, uses the same `<count>/<period>` syntax as the others, and an unparseable value raises `ImproperlyConfigured`. Like the per-token buckets, the limiter fails closed: if the cache backend is unavailable the request errors rather than skipping the check.

### Throttled tool calls

A tool call over its limit returns a structured error as an ordinary result, exactly like every other tool-level error documented above — never a `429` or a transport-level failure:

```json
{
  "error": {
    "code": "throttled",
    "detail": "Rate limit exceeded for this token (mcp_read). Retry after 12 second(s).",
    "retry_after": 12
  }
}
```

`retry_after` is a whole number of seconds; an agent should wait at least that long before retrying the same call.

### Throttled resource reads

Resources have no structured-error return path (see [Resource errors](#resource-errors) below), so a throttled `board://`/`card://` read surfaces the same way any other resource failure does: as a JSON-RPC-level error whose message includes the retry hint, e.g. `Rate limit exceeded for this token (mcp_compute). Retry after 4 second(s).` — rather than as a `{"error": {...}}` payload in the resource's content.

---

## Connecting a client

Point any MCP client at the endpoint with the token as a Bearer credential. For example, with the reference inspector:

```bash
npx @modelcontextprotocol/inspector
```

Then connect to `http://localhost:8000/mcp` using **Streamable HTTP** transport, adding the `Authorization: Bearer vbn_...` header. The inspector will list the server and its tools.

---

## Tools

### `list_boards`

Returns every board the authenticated user can access.

**Arguments:** none.

**Returns:** an array of objects.

| Field | Type | Description |
|---|---|---|
| `id` | integer | Board primary key, the same identifier the REST API uses. |
| `uid` | string | Stable public short id. |
| `name` | string | Board name. |
| `description` | string | Board description; empty string when unset. |
| `role` | string | The user's **effective** role on the board — see below. |
| `column_count` | integer | Number of columns. |
| `swimlane_count` | integer | Number of swimlanes. |
| `card_count` | integer | Number of **non-archived** cards. |
| `created_at` | string | ISO 8601 timestamp. |
| `updated_at` | string | ISO 8601 timestamp. |

**Example**

```json
[
  {
    "id": 12,
    "uid": "5930ca070e34672b",
    "name": "Platform Roadmap",
    "description": "Q3 delivery",
    "role": "admin",
    "column_count": 5,
    "swimlane_count": 3,
    "card_count": 42,
    "created_at": "2026-09-14T05:13:39.725480+00:00",
    "updated_at": "2026-09-14T05:13:39.725490+00:00"
  }
]
```

#### Which boards are returned

The tool applies exactly the same access rule as `GET /api/v1/boards/`, so an agent sees the same boards the user sees in the web UI. A board is included when the user owns it, holds a membership on it, inherits access through a group, or is a site admin with access to all content.

#### The `role` field

`role` is the user's *effective* role, not a raw membership row. It is one of:

| Value | Meaning |
|---|---|
| `admin` | Board admin, or the board's owner. |
| `member` | Can create, edit, move, and delete cards. |
| `collaborator` | Read-only on cards; may comment and attach. |
| `viewer` | Read-only. |
| `site_admin` | A site administrator with access to all content. Not a board membership — it is not revocable per board. |

Group-inherited access reports the role inherited from the group.

---

### Tool errors

> **Added in 1.2**

A tool that can fail for a reason other than "your token can't reach `/mcp` at all" (a bad id, a role or scope that may not write, a WIP limit) returns a structured error as an ordinary result — never a `500`, and never an unstructured exception message. The result carries a top-level `error` key instead of the tool's normal payload:

```json
{
  "error": {
    "code": "wip_limit_exceeded",
    "column_name": "In Progress",
    "current_count": 3,
    "wip_limit": 3
  }
}
```

Every error has a `code` your agent can branch on. The common ones:

| `code` | Meaning |
|---|---|
| `board_not_found` | The board id does not exist, or exists but you have no role on it (identical either way — Visiban never confirms a board id you cannot see). |
| `card_not_found` | Same, for a card id. |
| `permission_denied` | Your board role is `collaborator` or `viewer`; card writes require `admin` or `member`. |
| `missing_scope` | Your token lacks the `mcp:write` scope required for this tool — see above. |
| `maintenance_mode` | The instance is in [maintenance mode](admin.md#maintenance-mode) and your token does not belong to a site admin. Only ever returned by the write tools below (`create_card`, `move_card`, `update_card`, `archive_card`) — the read tools (`list_boards`, `list_columns`, `list_swimlanes`, `list_cards`) keep working regardless. Added in 1.2. |
| `throttled` | Your token has exceeded its per-token rate limit. Carries a `retry_after` (seconds) — see [Rate limiting](#rate-limiting). Added in 1.2. |
| `validation_error` | A field failed validation — includes an `errors` object keyed by field name, e.g. an `assignee` username or label name that does not resolve to an assignable board member/label. Every unresolved `assignee` returns the same message, whether the username does not exist, belongs to someone with no role on the board, or belongs to a `viewer` (who cannot be assigned). |
| `wip_limit_exceeded` / `wip_hard_blocked` | The target column is at its WIP limit. Returned by `move_card` and, since 1.2 (#1428), `create_card`. Neither tool overrides either — there is no `force` option over MCP. |
| `weight_limit_exceeded` | The target column is at its weight limit (`move_card`, and `create_card` since 1.2). |
| `version_conflict` | `move_card` only. The card kept changing under concurrent writes while the server retried locking it. `move_card` takes no `version` argument, so this is the only cause. Re-read the card and retry. Added in 1.2 (#1522). |

### User fields

Users are identified by username and display name, matching the REST API. Every user-bearing field in tool and resource output — a card's `assignee`, a movement's `moved_by`, an activity's `actor`, and a comment's `author` — is either `null` or a `{username, display_name}` object, the same `username`/`display_name` pair the REST API embeds for board members. `display_name` is an empty string when the user has not set one.

The `assignee` argument of `list_cards`, `create_card`, and `update_card` takes a username, matched case-insensitively.

*Changed in 1.2 (before GA, #1671): the 1.2 pre-releases reported these fields as email address strings and took the `create_card`/`update_card` assignee as `assignee_email`; that argument is now `assignee`. The `list_cards` `assignee` filter kept its name but now matches a username instead of an email address, so an email value returns no cards rather than an error. A call that still passes `assignee_email` (any value, including an empty string) is rejected with a `validation_error` whose `errors` is `` {"assignee_email": ["This argument was replaced by `assignee` (a username)."]} ``, and nothing is written.*

### `list_columns`

Lists a board's columns, ordered by position. All board roles may call it.

**Arguments:** `board_id` (integer, required).

**Returns:** an array of `{id, name, position, color, wip_limit, card_count}` — `card_count` excludes archived cards. `wip_limit` is checked by both `move_card` and `create_card` (since 1.2, #1428); only a board import can leave `card_count` above it (see [Field Enforcement](../architecture/field-enforcement.md)).

### `list_swimlanes`

Lists a board's swimlanes, ordered by position. All board roles may call it.

**Arguments:** `board_id` (integer, required).

**Returns:** an array of `{id, name, position, color, card_count, is_collapsed}`, plus `contact_email` for `admin`/`site_admin` callers only — a `collaborator`/`viewer` response simply omits that key, matching the same admin-only visibility the web UI already applies to swimlane contact info.

### `list_cards`

Lists a board's cards, ordered by column then swimlane then position. All board roles may call it. Returns at most 200 cards, with no signal when more match and no way to page past them (tracked in #1762). The REST `GET /api/v1/boards/{board_id}/cards/` has the same 200-row default but reports the total in `X-Total-Count` / `X-Truncated` and pages with `limit`/`offset`; use it, or `GET /api/v1/boards/{board_id}/full/`, when a board may hold more than 200 matching cards.

**Arguments:** `board_id` (integer, required); `column_id`, `swimlane_id` (integers), `assignee` (username, case-insensitive), `priority`, `label` (name) — all optional filters, ANDed together; `include_archived` (boolean, default `false`).

**Returns:** an array of `{id, title, description, priority, assignee, labels, column, swimlane, due_date, position, created_at, updated_at}`. `assignee` is a `{username, display_name}` object or `null` (see [User fields](#user-fields)); `labels` is an array of names; `column`/`swimlane` are `{id, name}` objects so a result can be fed straight into `move_card`.

### `create_card`

Creates a card, appended to the end of its column/swimlane cell (or inserted at `position` if given). Requires `admin` or `member` board role and the `mcp:write` scope.

**Arguments:** `board_id`, `column_id`, `swimlane_id`, `title` (required); `description`, `priority` (default `"medium"`), `assignee` (username of any board member whose role is not `viewer`), `labels` (array of names), `due_date` (`YYYY-MM-DD`), `position` — all optional.

**Returns:** the created card, same shape as one row of `list_cards`. Writes a `CardMovement` record with no "from" column (matching how the REST API records card creation) and broadcasts `card.created` on the board's WebSocket channel.

Enforces the column's WIP and weight limits exactly as `move_card` does, with no override: a create into a full column returns `wip_limit_exceeded`/`wip_hard_blocked`/`weight_limit_exceeded` and writes nothing. *Changed in 1.2 (#1428): creates were not checked before.*

### `move_card`

Moves a card to a new column and/or swimlane and/or position. Requires `admin` or `member` board role and the `mcp:write` scope.

**Arguments:** `card_id` (required); `to_column_id`, `to_swimlane_id` — at least one required, the other defaults to the card's current value; `position` (default `0`).

**Returns:** `{"card": <card>, "movement": <movement-or-null>}`. `movement` is `null` for a pure position reorder within the same cell (no column/swimlane change); otherwise it carries `{id, from_column, to_column, from_swimlane, to_swimlane, moved_at, moved_by, imported_from_username}`. `imported_from_username` is `null` except on a row created by a board import with history (since 1.2), where it holds the username the import file named and `moved_by` is the user who imported it. Enforces the board's WIP/weight limits exactly as the REST move endpoint does, with no override — a blocked move returns `wip_limit_exceeded`/`wip_hard_blocked`/`weight_limit_exceeded` rather than moving the card.

### `update_card`

Updates one or more fields on a card. Requires `admin` or `member` board role and the `mcp:write` scope.

**Arguments:** `card_id` (required); any of `title`, `description`, `priority`, `assignee` (username), `labels`, `due_date` — omitted fields are left unchanged. `labels: []` clears every label; `assignee: ""` unassigns the card. Cannot change a card's column or swimlane — use `move_card`.

**Returns:** the updated card, same shape as `create_card`. Records a `CardActivity` audit entry per field that actually changed.

### `archive_card`

Soft-deletes a card. Idempotent — archiving an already-archived card succeeds and returns its existing `archived_at`. Requires `admin` or `member` board role and the `mcp:write` scope.

**Arguments:** `card_id` (required).

**Returns:** `{"card_id": <id>, "archived_at": <ISO 8601 timestamp>}`.

---

## Resources

> **Added in 1.2**

A **resource** is MCP's second kind of server capability, alongside a tool. Where a tool is invoked with `tools/call` and takes arguments an agent chooses per call, a resource is addressed by a URI and read with `resources/read` — it exists so an agent can load a whole board's or card's context in a single round trip, instead of chaining several `list_*` tool calls together. Visiban registers two resource templates: `board://{board_id}` and `card://{card_id}`.

Like every other endpoint under `/mcp`, reading a resource requires the `mcp:read` scope. Neither resource needs `mcp:write` — both are read-only.

`/mcp` also now sends `Access-Control-*` headers — including a response to preflight `OPTIONS` requests — for origins listed in `CORS_ALLOWED_ORIGINS`, the same setting already used by the REST API and the WebSocket, so a browser-based MCP client can reach the server directly.

### `board://{board_id}`

A full read-only board snapshot: metadata, columns, swimlanes, active cards, and labels — equivalent to calling `list_columns` + `list_swimlanes` + `list_cards` and merging the results, but resolves and authorizes the board once instead of three times. All board roles may read it. Mime type `application/json`.

**Returns:**

| Field | Type | Description |
|---|---|---|
| `id` | integer | Board primary key. |
| `name` | string | Board name. |
| `description` | string | Board description; empty string when unset. |
| `created_at` | string | ISO 8601 timestamp. |
| `updated_at` | string | ISO 8601 timestamp. |
| `columns` | array | Same shape as `list_columns`'s return above. |
| `swimlanes` | array | Same shape as `list_swimlanes`'s return above, including the admin/site_admin-only `contact_email` rule. |
| `cards` | array | Same shape as `list_cards`'s return above, unfiltered and excluding archived cards. Subject to the same 200-card cap as `list_cards`, with no truncation signal (#1762). |
| `labels` | array | `{id, name, color}` objects, one per label defined on the board. |

!!! note
    `board://{board_id}` deliberately omits every web-session-only field the REST board detail response carries — `share_token`, `capabilities`, `is_starred`, `members`, `current_user_role`. These have no meaning for an AI agent reading board context, and a share token in particular is a bearer credential that must never appear in agent-facing output.

### `card://{card_id}`

Card detail plus full audit history. Requires board membership — any role, including `collaborator`/`viewer`. Mime type `application/json`.

**Returns:** the same fields as one row of `list_cards`, plus:

| Field | Type | Description |
|---|---|---|
| `archived_at` | string or `null` | ISO 8601 timestamp, or `null` if the card is not archived. |
| `movements` | array | `{id, from_column, to_column, from_swimlane, to_swimlane, moved_at, moved_by, imported_from_username}` objects — same shape as `move_card`'s `movement` above. |
| `checklist_items` | array | `{id, text, is_checked, position}` objects. |
| `activities` | array | `{id, event_type, from_value, to_value, actor, created_at, imported_from_username}` objects, newest first. `imported_from_username` works as on `movements`. |
| `comments` | array | `{id, body, author, created_at, updated_at}` objects, oldest first. |

### Resource errors

Resources fail differently from tools. A tool that cannot complete returns a structured `{"error": {"code": ...}}` result (see [Tool errors](#tool-errors) above); a resource read has no equivalent channel. A failed read — a bad id, or an id on a board the caller cannot access — instead surfaces as a plain JSON-RPC-level error, with a message like `No Board matches the given query.` or `No Card matches the given query.`

That message is deliberately identical whether the id does not exist or exists on a board the caller cannot see — the same IDOR-prevention reasoning the `board_not_found`/`card_not_found` tool error codes already document above. There is no way to distinguish the two cases from the response, by design.

The same "raise, don't return" rule applies to a throttled resource read — see [Throttled resource reads](#throttled-resource-reads) above.

---

## Roadmap

Phase 1 covers connectivity, authentication, board discovery, full card CRUD, and the `board://`/`card://` resources (this release). A full setup guide is tracked separately. Analytics tools and OAuth 2.1 are planned for the Enterprise edition.
