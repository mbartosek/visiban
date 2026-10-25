# Ask Your Own Agent About the Demo Data

> **Added in 1.2**

This walkthrough points your own AI agent at a **local** Visiban instance loaded with the demo data, using a read-only token, so you can ask it questions about a realistic board. It takes about ten minutes. For the full MCP reference, see [MCP Server](mcp-server.md).

!!! note "Why this runs locally and not on try.visiban.com"
    The hosted demo does not expose MCP and does not let visitors create tokens. This is by design: publishing a credential on a public site is a security decision we chose not to make. The steps below give you the same experience on your own machine, where the token is yours alone.

## 1. Start a local instance

```bash
git clone https://gitlab.com/visiban/visiban.git && cd visiban
cp .env.example .env
```

Edit `.env`: set `DJANGO_SECRET_KEY`, and uncomment the MCP line so it reads:

```bash
MCP_SERVER_ENABLED=true
```

Then start the stack (see [Installation](../getting-started/installation.md) for details):

```bash
docker compose up --build
```

The backend is now at `http://localhost:8000`, so the MCP endpoint is `http://localhost:8000/mcp`. The defaults in `.env.example` (`ALLOWED_HOSTS=localhost,127.0.0.1`) already accept it.

## 2. Load the demo data

In a second terminal:

```bash
docker compose exec backend python manage.py seed_demo_data
```

This creates the "Visiban Demo Board" (five columns, ten swimlanes, about 80 cards, with movement history, checklists, and comments). `DEBUG=true` in `.env.example` satisfies the command's production guard. See [Demo Data](../administration/demo-data.md) for what the seed creates.

## 3. Sign in and mint a read-only token

The seeded `demo1` to `demo5` accounts have unusable passwords, so sign in as the site admin, which can read every board.

1. Get the one-time admin password: `docker compose exec backend cat /tmp/visiban_admin_password` (if the file is missing, see [First Boot](../getting-started/first-boot.md)).
2. Open `http://localhost:5173`, sign in as `admin`, and choose a new password when asked. Until you do, `/mcp` answers `403`.
3. Open your avatar menu, then **Settings**, then **Access Tokens**, then **New token**.
4. Name it `demo-agent` and select **only** the `mcp:read` scope. Leave `mcp:write`, `read`, and `write` unselected.
5. Click **Create token** and copy the value now. It is shown once.

A token with only `mcp:read` can list and read boards, columns, swimlanes, cards, and card history. Calling a write tool (`create_card`, `move_card`, `update_card`, `archive_card`) fails with a structured error. Scopes are not hierarchical; see [Personal Access Tokens](personal-access-tokens.md#scopes).

## 4. Connect your agent

**Claude Code:**

```bash
claude mcp add --transport http visiban http://localhost:8000/mcp \
  --header "Authorization: Bearer <your-token>"
```

**Claude Desktop** (or any client that reads a JSON config), in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "visiban": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

Restart the client. To check the server before blaming the client, use the [MCP Inspector](mcp-server.md#verifying-with-mcp-inspector).

## 5. Ask about the board

Try questions like these:

- "Which boards can you see, and how many cards are on the Visiban Demo Board?"
- "What is sitting in the Backlog column, highest priority first?"
- "Which cards have been in progress the longest?"
- "Show me the movement history of the card about <topic> and tell me who moved it last."
- "Summarize the comments on the cards assigned to demo2."

Then try to break the read-only promise, to see the guardrail: "Create a card called Test in Backlog." The agent should report a permission error, and the board does not change.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `404` from `/mcp` | `MCP_SERVER_ENABLED` is not `true`; restart the backend after editing `.env` |
| `401` | Token mistyped, or it lacks `mcp:read` |
| `403` | The account still has to change its password in the web app |
| `seed_demo_data` refuses to run | `DEBUG` is not `true` in `.env`; see the [production guard](../administration/demo-data.md#production-guard) |

When you are done, delete the token from **Access Tokens** and run `docker compose down -v` to discard the local data.
