# Piazza MCP

Read-only MCP server for browsing Piazza course forums. Built for use with the Hermes agent, but works with any MCP client (Claude Code, Claude Desktop, Cursor).

Piazza has no official API. This server uses [piazza-api](https://github.com/hfaran/piazza-api), which drives Piazza's internal JSON-RPC endpoint with your login session. It can break without notice and is not affiliated with Piazza Technologies Inc. Use at your own risk.

## Tools

| Tool | Purpose |
| --- | --- |
| `list_classes` | Enrolled classes with their network ids |
| `get_folders` | Folder names for one class |
| `get_feed` | Browse recent posts (folder filter, unread filter, pagination) |
| `search_posts` | Keyword search within one class |
| `get_post` | Full thread: question, answers, endorsements, followups |
| `read_attachment` | Extract text from PDF or text files linked in posts |
| `get_daily_digest` | Cross-class summary of all activity in the last N hours |

All tools are read-only. Nothing is ever posted, edited, or marked read.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and a Piazza-native email/password login (if your account is SSO-only, set a password via Piazza account settings first).

```bash
export PIAZZA_EMAIL="you@school.edu"
export PIAZZA_PASSWORD="..."
```

### Hermes agent

Add to the `mcp_servers` section of your Hermes config on the machine running Hermes, then `/reload-mcp`:

```yaml
mcp_servers:
  piazza:
    command: "uvx"
    args: ["--from", "git+https://github.com/RohanMuppa/Piazza-MCP", "piazza-mcp"]
    env:
      PIAZZA_EMAIL: "you@school.edu"
      PIAZZA_PASSWORD: "..."
```

If the repo is cloned locally instead, use:

```yaml
    command: "uv"
    args: ["run", "--directory", "/path/to/Piazza-MCP", "piazza-mcp"]
```

Tools appear with the `mcp_piazza_` prefix.

### Daily review

The server cannot push on a schedule; scheduling lives in the agent. Give Hermes a daily scheduled task with a prompt like:

> Call mcp_piazza_get_daily_digest with hours=24. Summarize what needs my attention: unanswered questions in my classes, new instructor answers, and anything about exams or deadlines. Use mcp_piazza_get_post to read anything important before summarizing.

### Claude Code

This repo ships a project-scope `.mcp.json` that runs the server via `uv run piazza-mcp` and reads `PIAZZA_EMAIL` / `PIAZZA_PASSWORD` from your shell. Or register manually:

```bash
claude mcp add --scope user --env PIAZZA_EMAIL=you@school.edu --env PIAZZA_PASSWORD=... piazza -- uv run --directory /path/to/Piazza-MCP piazza-mcp
```

## Development

```bash
uv sync
uv run pytest                                   # unit tests, no network
npx @modelcontextprotocol/inspector uv run piazza-mcp   # interactive tool inspector
```

Layout: `src/piazza_mcp/client.py` owns all Piazza access (lazy login, 60s TTL cache, one re-login retry on expired sessions), `formatting.py` renders Piazza JSON to compact markdown, `server.py` defines the FastMCP tools.
