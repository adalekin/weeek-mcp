# weeek-mcp

[![CI](https://github.com/adalekin/weeek-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/adalekin/weeek-mcp/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/weeek-mcp.svg)](https://pypi.org/project/weeek-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/adalekin/weeek-mcp/blob/main/LICENSE)

**Language:** English · [Русский](https://github.com/adalekin/weeek-mcp/blob/main/README.ru.md)

An [MCP](https://modelcontextprotocol.io) server for [Weeek](https://weeek.net): manage **tasks** through the public REST API and browse the **knowledge base** through Weeek's internal API, exposed as MCP **Resources** so you can search and select KB documents as content (not links) from your MCP client.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Tools](#tools)
- [Knowledge base in Claude context](#knowledge-base-in-claude-context)
- [Status & limitations](#status--limitations)
- [Development](#development)
- [License](#license)

## Features

### Tasks and boards

Public REST API — projects, boards, board columns, and the full task lifecycle: create, update, complete, move between columns, assign and unassign members.

### Knowledge base

Full CRUD. Weeek has no public KB API, so the server calls Weeek's **internal JSON API** (`api.weeek.net/ws/{id}/kb/...`) using cookies from a saved browser login. Documents are rendered to Markdown and published as MCP **Resources** (`weeek-kb://<id>`). Read/list/search/create/rename/delete go over the JSON API; **in-place body editing** speaks Weeek's collaborative protocol directly (Hocuspocus/Yjs), because bodies live in a shared document the REST API only serves a snapshot of. Content is converted between Markdown and Weeek's ProseMirror format automatically.

### Capability-aware

Task tools appear when an API token is set; KB tools and resources appear when login credentials or a cached session are present.

## Requirements

- Python 3.10+ (not needed for the [Claude Desktop bundle](#claude-desktop))
- A Weeek **API token** for task tools (Weeek → Settings → API).
- For the knowledge base: `weeek-mcp[kb]` plus the Chromium runtime (used for login only), and either login credentials or a session seeded once with `weeek-mcp-login`.

## Installation

```bash
pip install weeek-mcp              # task tools only
pip install "weeek-mcp[kb]"        # + knowledge base
playwright install chromium         # KB runtime
```

With [uv](https://docs.astral.sh/uv/) in your own project:

```bash
uv add "weeek-mcp[kb]"
```

## Configuration

Set environment variables (or copy `.env.example` to `.env`). Use just the task API,
just the knowledge base, or both.

| Variable | Purpose |
| --- | --- |
| `WEEEK_API_TOKEN` | Task API token. Required for task tools. |
| `WEEEK_EMAIL` / `WEEEK_PASSWORD` | First automated KB login. Optional (skip if 2FA/SSO — use `weeek-mcp-login`). |
| `WEEEK_WORKSPACE_ID` | KB workspace id. Optional — auto-detected via `/ws` when unset. |
| `WEEEK_STORAGE_STATE` | Where the browser session is cached (defaults under `~/.local/state`). |
| `WEEEK_HEADLESS` | `false` to watch the browser during login. |
| `WEEEK_KB_CACHE_TTL` | Seconds to cache the KB document list (default `300`). |
| `WEEEK_DEBUG_LOG` | `1`/`true` to write diagnostic timing/step logs to `~/.local/state/weeek-mcp/debug.log` (some MCP hosts discard stderr). Off by default. |

### Knowledge base first login

If your account has 2FA or a captcha, automated login won't work. Seed the session
once, interactively — it opens a browser, you sign in, then it caches the session for
headless reuse:

```bash
weeek-mcp-login
```

## Usage

Run the stdio server:

```bash
weeek-mcp
```

### Claude Desktop

The quickest route is the bundle. Download [`weeek-mcp.mcpb`](https://github.com/adalekin/weeek-mcp/releases/latest/download/weeek-mcp.mcpb) from the latest release and open it, or drag it onto Claude Desktop's Settings → Extensions page. Desktop asks for your API token and installs the extension. You don't need Python or uv for this: Desktop runs the server with its own uv, which fetches `weeek-mcp` from PyPI on first launch.

The knowledge base still needs a one-time sign-in from a terminal, and that part does use [uv](https://docs.astral.sh/uv/):

```bash
uvx --from "weeek-mcp[kb]" playwright install chromium
uvx --from "weeek-mcp[kb]" weeek-mcp-login
```

Then restart the extension (switch it off and on in Settings → Extensions). If you also put your Weeek email and password in the extension settings, the server signs in again by itself when the session expires. That won't work with 2FA or SSO; run `weeek-mcp-login` again instead.

To set it up by hand, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "weeek": {
      "command": "weeek-mcp",
      "env": {
        "WEEEK_API_TOKEN": "...",
        "WEEEK_STORAGE_STATE": "/absolute/path/to/storage_state.json"
      }
    }
  }
}
```

### Other MCP clients

`weeek-mcp` is a plain stdio server, so it works with any MCP client that launches
servers as a local subprocess: Cursor, Windsurf, Cline, Continue, Zed, VS Code
(Copilot agent mode), Gemini CLI, Goose, LibreChat, and others, plus your own agents
built on an MCP SDK. The command and environment variables are the same as above —
only the config format and its location differ.

Cursor (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project) uses the same shape
as Claude Desktop:

```json
{
  "mcpServers": {
    "weeek": {
      "command": "weeek-mcp",
      "env": { "WEEEK_API_TOKEN": "..." }
    }
  }
}
```

VS Code (`.vscode/mcp.json`) uses a `servers` key and an explicit `type`:

```json
{
  "servers": {
    "weeek": {
      "type": "stdio",
      "command": "weeek-mcp",
      "env": { "WEEEK_API_TOKEN": "..." }
    }
  }
}
```

> **On the knowledge base:** task tools and `weeek_kb_*` are ordinary MCP tools and
> work almost everywhere. Pulling documents in through an attachment menu relies on MCP
> **Resources**, which fewer clients surface. Where they aren't supported, read the KB
> with `weeek_kb_read`/`weeek_kb_search` — the content still lands in context. MCP and
> resources support moves fast per client; check the client's docs before relying on it.

## Tools

### Tasks

| Group | Tools |
| --- | --- |
| Reading & navigation | `weeek_whoami`, `weeek_list_members`, `weeek_list_projects`, `weeek_list_boards`, `weeek_list_board_columns`, `weeek_list_tasks`, `weeek_get_task` |
| Task lifecycle | `weeek_create_task`, `weeek_update_task`, `weeek_complete_task`, `weeek_uncomplete_task`, `weeek_delete_task`, `weeek_move_task` |
| Assignees, watchers, hierarchy | `weeek_set_assignees`, `weeek_remove_assignees`, `weeek_set_watchers`, `weeek_remove_watchers`, `weeek_set_task_parent`, `weeek_add_task_to_project`, `weeek_remove_task_from_project` |
| Time & attachments | `weeek_task_timer`, `weeek_manage_time_entry`, `weeek_upload_attachment`, `weeek_get_attachment` |
| Fields & comments | `weeek_list_custom_fields`, `weeek_list_task_comments`, `weeek_add_task_comment`, `weeek_update_task_comment`, `weeek_delete_task_comment` |

### Workspace admin

`weeek_manage_tags`, `weeek_manage_projects`, `weeek_manage_boards`, `weeek_manage_board_columns`, `weeek_manage_portfolios`, `weeek_manage_custom_fields`.

These take an `action` (create/update/delete/…) rather than one tool per operation — the CRUD is regular and the tool list stays readable. Custom fields live per board, per project or workspace-wide, so that tool takes a `scope` (`global`/`project`/`board`) plus `scope_id`.

### Knowledge base

| Group | Tools |
| --- | --- |
| Reading | `weeek_kb_search`, `weeek_kb_list`, `weeek_kb_read` |
| Writing | `weeek_kb_create`, `weeek_kb_update`, `weeek_kb_delete` |
| Formatting | `weeek_kb_table_widths`, `weeek_kb_icons` |

> `weeek_kb_update` with new content writes into the document's shared Yjs document over
> Weeek's collaborative websocket, because that — not REST — is where bodies are saved.
> No browser is involved and the document id is preserved.

### Behavior

**Priorities.** Take either Weeek's number or its label:

| Number | Label |
| --- | --- |
| `0` | low (Низкий) |
| `1` | medium (Средний) |
| `2` | high (Высокий) |
| `3` | hold (Замороженный) |

**Custom fields.** Set with `custom_fields`: on an existing task by field name or id (`{"Ссылка на фичу": "https://…"}`, `null` clears a field, a select takes the option name or id), on `weeek_create_task` by id only — `weeek_list_custom_fields` lists them. A field belongs to the projects it was added to, and Weeek stores nothing when you write to one it doesn't cover, so the write is verified and reported.

**Descriptions.** Editable on an existing task: `weeek_update_task` takes `description` as Markdown (empty string clears it). Weeek's REST API only accepts a description on create — `PUT /tm/tasks/{id}` has no such field — because descriptions sync through the same collaborative channel as KB document bodies, so this writes into that channel and needs the knowledge base session. `weeek_create_task` still takes its `description` as HTML, which is what that endpoint stores.

**Comments.** Read with `weeek_list_task_comments`, written with `weeek_add_task_comment` and rewritten in place with `weeek_update_task_comment` (all Markdown) — an edited comment beats posting a correction under the original. `weeek_delete_task_comment` removes one for good; Weeek keeps no trash for comments. Weeek's public API has no comments at all, so these go through its web API on the knowledge base session; no browser is launched, only the saved cookies.

**Tables.** One size to set: the pixel width of each column (minimum 90). New tables are fitted to the document's content column (~676px) instead of Weeek's 180px-per-column default, and existing widths are carried across a `weeek_kb_update` — a table that gains or loses a column is re-fitted. `weeek_kb_table_widths` sets them explicitly: `widths: [300, 200, 176]` for exact sizes, or `fit: true` to spread a table across the content column.

**Icons.** Pass `icon` to `weeek_kb_create`/`weeek_kb_update` as a single emoji (`🚀`) or as one of Weeek's built-in icon names (`weeek_kb_icons` lists them); an empty `icon` removes it. Listings report the icon a document currently has.

## Knowledge base in Claude context

Each KB document is published as an MCP **Resource** (`weeek-kb://<id>`). In Claude
Desktop you add them from the attachment (**+**) menu of the connected server — browse
the list or narrow it with `weeek_kb_search` — and the client pulls in the **document
content**, not a link.

> **Note on Project Context:** Claude Desktop surfaces MCP resources as attachments.
> Whether a selected resource persists inside a Project's *Context* panel (vs. a single
> conversation) depends on your Claude Desktop version. The content-not-a-link behavior
> works regardless.

## Status & limitations

- **Task tools** follow Weeek's published OpenAPI spec.
- **Knowledge base** uses Weeek's **internal, undocumented** API (`/ws/{id}/kb/...`). It is
  not covered by any stability guarantee and may change without notice; if KB calls start
  failing, the endpoints in [`weeek_mcp/kb/client.py`](https://github.com/adalekin/weeek-mcp/blob/main/weeek_mcp/kb/client.py) are the place
  to look. Login automation targets Weeek's two-step web form
  ([`weeek_mcp/kb/session.py`](https://github.com/adalekin/weeek-mcp/blob/main/weeek_mcp/kb/session.py)); accounts with 2FA/captcha/SSO
  should seed the session with `weeek-mcp-login` instead.
- Document content is ProseMirror/TipTap JSON, converted to/from Markdown by
  [`weeek_mcp/kb/prosemirror.py`](https://github.com/adalekin/weeek-mcp/blob/main/weeek_mcp/kb/prosemirror.py). Editing an existing body
  goes through Weeek's collaborative channel (there is no REST content-write):
  [`weeek_mcp/kb/collab.py`](https://github.com/adalekin/weeek-mcp/blob/main/weeek_mcp/kb/collab.py) speaks the Hocuspocus protocol,
  authenticates with a per-socket ticket, and replaces the `prosemirror` fragment of the
  document's Y.Doc. Authoring covers the common Markdown subset (headings, paragraphs, lists,
  bold/inline code, code blocks, quotes, rules); rich cases like nested lists and tables
  are simplified.
- Table column widths live on the `table_body` node, as a JSON string, and are written
  with the body rather than after it ([`weeek_mcp/kb/tables.py`](https://github.com/adalekin/weeek-mcp/blob/main/weeek_mcp/kb/tables.py)). Cell colors and per-column colors
  are stored alongside the widths but are not exposed as tools yet.

## Development

See [CONTRIBUTING.md](https://github.com/adalekin/weeek-mcp/blob/main/CONTRIBUTING.md) for setup, tests, and pull requests.

## License

This project is licensed under the MIT License — see [LICENSE](https://github.com/adalekin/weeek-mcp/blob/main/LICENSE).
