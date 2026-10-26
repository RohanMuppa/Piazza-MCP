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
| `list_resources` | Files and links on a class's Resources page, grouped by section |
| `download_resource` | Save one Resources file to a local folder |
| `get_daily_digest` | Cross-class summary of all activity in the last N hours |

All tools are read-only on Piazza. Nothing is ever posted, edited, or marked read. `download_resource` is the only tool that writes, and only to the local `dest_dir` you pass it: it never overwrites an existing file unless `overwrite` is true, and a failed or oversized (over 200 MB) download leaves no partial file behind. Set `PIAZZA_DOWNLOAD_ROOT` to confine downloads to one folder tree (recommended, since the agent chooses `dest_dir`).

### Resources

Piazza has no RPC method for the Resources page. `list_resources` loads the class profile page (`https://piazza.com/<school>/<term>/<course>/resources`, built from your user status) and parses the `RESOURCES` list Piazza embeds in it. `download_resource` fetches `/class_profile/get_resource/<network_id>/<resource_id>`. Files are named after the resource title, with an extension added from the file type when the title has none (`Homework_1_PDF` becomes `Homework_1_PDF.pdf`). An expired session (HTTP 401/403, or Piazza's login page served in place of the file) triggers one re-login retry. This scrapes page HTML, so it is more likely than the RPC tools to break if Piazza changes its site.

To grab a whole class: call `list_resources`, then `download_resource` for each `resource_id` with the same absolute `dest_dir`.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and a Piazza-native email/password login (if your account is SSO-only, set a password via Piazza account settings first).

```bash
export PIAZZA_EMAIL="you@school.edu"
export PIAZZA_PASSWORD="..."
export PIAZZA_DOWNLOAD_ROOT="$HOME/Documents"   # optional, limits download_resource
```

On macOS you can leave `PIAZZA_PASSWORD` unset and keep the password in the Keychain instead, so no config file holds it. When only `PIAZZA_EMAIL` is set, the server reads the password stored under service `piazza-mcp` for that email:

```bash
security add-generic-password -U -s piazza-mcp -a you@school.edu -w   # prompts for the password
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

## CLI

The same package installs a `piazza` command for jobs that need no model in the loop, such as syncing a class's Resources into a folder on a schedule. It reads the same `PIAZZA_EMAIL`, `PIAZZA_PASSWORD`, and optional `PIAZZA_DOWNLOAD_ROOT` variables.

```bash
uv tool install git+https://github.com/RohanMuppa/Piazza-MCP   # puts `piazza` on your PATH

piazza classes                                    # network ids
piazza resources list <network_id>
piazza resources download <network_id> --dest ~/school/ece362          # every file
piazza resources download <network_id> --dest ~/school/ece362 --section homework
piazza resources download <network_id> <resource_id> --dest . --overwrite
```

`download` with no resource ids fetches every file, skips links and files that already exist (so re-running only picks up new ones), keeps going past individual failures, and exits 1 if any file failed. Relative `--dest` paths resolve against the current directory. `--section` takes a section name exactly as `resources list` prints it (unsectioned resources are `general`); an unknown section is an error rather than a silent no-op. Resources whose titles would save to the same file (`Notes`, `notes`, `Notes.pdf`) get their resource id appended (`Notes-<id>.pdf`), and the name is the same on every run so re-syncs still recognize them.

## Development

```bash
uv sync
uv run pytest                                   # unit tests, no network
npx @modelcontextprotocol/inspector uv run piazza-mcp   # interactive tool inspector
```

Layout: `src/piazza_mcp/client.py` owns all Piazza access (lazy login, 60s TTL cache, one re-login retry on expired sessions), `formatting.py` renders Piazza JSON to compact markdown, `downloads.py` writes Resources files to disk (shared by both front ends), `server.py` defines the FastMCP tools, and `cli.py` is the `piazza` command.
