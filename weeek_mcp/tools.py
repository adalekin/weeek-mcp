"""MCP tool definitions and dispatch.

Split into two groups:
  * task tools  -> Weeek public REST API (weeek_api.WeeekAPI)
  * kb tools    -> knowledge base search over Playwright (kb.client.WeeekKB)

Tool input schemas mirror the Weeek OpenAPI spec, with two deliberate departures
for callers that work from labels rather than ids: priority also accepts its UI
label, and custom field values may be keyed by field name. The server module
wires these handlers to the low-level MCP Server.

Weeek answers several writes it did not perform with ``success: true`` (unknown
custom field ids, avatar fields in an article body, ``parentId`` on a document).
The rule here: validate against reference data before the write when there is
any — and when there is none, compare the write's response against what was
asked for, so a dropped value surfaces as an error instead of a silent no-op.

Its PUT endpoints replace rather than patch: a field they require but the caller
did not mention has to be carried over from the current value, never defaulted.
Defaulting turns "rename this project" into "rename it and make it public".
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

import mcp.types as types

from .kb.client import KBDocument, WeeekKB
from .kb.prosemirror import markdown_to_html, to_markdown
from .kb.session import replace_task_description
from .weeek_api import WeeekAPI

KB_URI_SCHEME = "weeek-kb"


def kb_uri(doc_id: str) -> str:
    return f"{KB_URI_SCHEME}://{doc_id}"


def kb_doc_id_from_uri(uri: str) -> str:
    return uri.split("://", 1)[-1].strip("/")


# --------------------------------------------------------------------------- priorities
# Weeek stores task priority as 0..3; these are the labels its own UI shows.
PRIORITIES = {"low": 0, "medium": 1, "high": 2, "hold": 3}
PRIORITY_DESCRIPTION = (
    "Priority as a number or a label: 0 low (Низкий), 1 medium (Средний), 2 high (Высокий), 3 hold (Замороженный)."
)
PRIORITY_SCHEMA = {
    "type": ["integer", "string"],
    "enum": [0, 1, 2, 3, *PRIORITIES],
    "description": PRIORITY_DESCRIPTION,
}


def _priority(value: Any) -> Any:
    """Accept either Weeek's numeric priority or one of its labels."""
    if not isinstance(value, str):
        return value
    try:
        return PRIORITIES[value.strip().casefold()]
    except KeyError:
        raise ValueError(f"Unknown priority {value!r}. Use 0-3 or one of: {', '.join(PRIORITIES)}.") from None


# --------------------------------------------------------------------------- custom fields
async def task_custom_fields(api: WeeekAPI, task_id: int) -> list[dict[str, Any]]:
    task = (await api.get_task(task_id)).get("task") or {}
    return task.get("customFields") or []


async def project_custom_fields(api: WeeekAPI, project_id: int) -> list[dict[str, Any]]:
    """Custom fields of a project, read off one of its tasks.

    The public API has no schema endpoint for them — ``/tm/custom-fields`` and a
    project's own ``customFields`` both come back empty — while every task carries
    the full field list. An empty project therefore has nothing to read.
    """
    tasks = (await api.list_tasks(projectId=project_id, perPage=1)).get("tasks") or []
    return (tasks[0].get("customFields") or []) if tasks else []


def _unknown_field_message(key: Any, fields: list[dict[str, Any]]) -> str:
    if not fields:
        return f"Unknown custom field {key!r}: this task has none."
    named = [f["name"] for f in fields if f.get("name")]
    unnamed = [f["id"] for f in fields if not f.get("name")]
    parts = [f"Unknown custom field {key!r}."]
    if named:
        parts.append("Available: " + ", ".join(named) + ".")
    if unnamed:
        parts.append("Unnamed fields are addressed by id: " + ", ".join(unnamed) + ".")
    return " ".join(parts)


def _custom_field_value(field: dict[str, Any], value: Any) -> Any:
    """Map chosen option names onto option ids, which is what Weeek stores.

    Per Weeek's spec a ``select`` takes one option id and a ``multiselect`` takes a
    list of them, so a list is resolved element by element.
    """
    options = field.get("options") or []
    if not options or value is None:
        return value
    if isinstance(value, list):
        return [_option_id(field, options, v) for v in value]
    if not isinstance(value, str):
        return value
    return _option_id(field, options, value)


def _option_id(field: dict[str, Any], options: list[dict[str, Any]], value: Any) -> Any:
    if not isinstance(value, str):
        return value
    for option in options:
        if option.get("id") == value:
            return value
    for option in options:
        if (option.get("name") or "").strip().casefold() == value.strip().casefold():
            return option["id"]
    label = field.get("name") or field.get("id")
    raise ValueError(
        f"Custom field {label!r} has no option {value!r}. Available: "
        + ", ".join(o.get("name") or o.get("id", "") for o in options)
    )


def dropped_custom_fields(
    response: Any, requested: dict[str, Any], fields: list[dict[str, Any]] | None = None
) -> list[str]:
    """Values Weeek did not store, by name where we know it.

    A task lists every custom field of the workspace, but a field only applies to
    the projects it was added to — writing to one of the others comes back
    ``success: true`` with the value silently missing.

    The check reads the fields the write response echoes back. A response that
    lists none of them says nothing about what was stored, so it is treated as
    "nothing to check" rather than as a task that lost every value.
    """
    task = (response or {}).get("task") or {}
    stored = task.get("customFields") or []
    if not stored:
        return []
    applied = {f.get("id"): f.get("value") for f in stored}
    labels = {f.get("id"): str(f.get("name") or f.get("id")) for f in fields or []}
    return [
        labels.get(fid, str(fid))
        for fid, value in requested.items()
        if not _clears_field(value) and applied.get(fid) is None
    ]


def _clears_field(value: Any) -> bool:
    """Values that ask for an empty field — Weeek stores them all as null.

    Compared by equality rather than truthiness so that 0 and False, which are real
    values for number and boolean fields, are not mistaken for a clear.
    """
    return value is None or value == "" or value == []


_FIELD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _report_created_task_fields(created: Any, requested: dict[str, Any]) -> None:
    """Check a freshly created task's custom fields, naming the task in any complaint."""
    task_id = ((created or {}).get("task") or {}).get("id")
    not_ids = [key for key in requested if not _FIELD_ID.match(str(key))]
    if not_ids:
        raise ValueError(
            f"Task {task_id} was created, but on create custom_fields must be keyed by field id: "
            + ", ".join(repr(k) for k in not_ids)
            + ". Set those values with weeek_update_task, which also accepts field names."
        )
    dropped = dropped_custom_fields(created, requested)
    if dropped:
        raise ValueError(
            f"Task {task_id} was created, but Weeek did not store "
            + ", ".join(repr(d) for d in dropped)
            + " — a custom field only applies to the projects it was added to. Fix the value with "
            "weeek_update_task rather than creating the task again."
        )


def resolve_custom_fields(fields: list[dict[str, Any]], values: dict[str, Any]) -> dict[str, Any]:
    """Turn {field name or id: value} into the {field id: value} body Weeek expects.

    Weeek accepts unknown field ids with ``success: true`` and writes nothing, so
    every key is checked against the task's own fields before the write. A value
    of None clears the field.
    """
    by_id = {f.get("id"): f for f in fields}
    by_name: dict[str, dict[str, Any]] = {}
    for field in fields:
        name = field.get("name")
        if name:
            by_name.setdefault(name.strip().casefold(), field)

    resolved: dict[str, Any] = {}
    for key, value in values.items():
        match = by_id.get(key) or by_name.get(str(key).strip().casefold())
        if match is None:
            raise ValueError(_unknown_field_message(key, fields))
        resolved[match["id"]] = _custom_field_value(match, value)
    return resolved


# --------------------------------------------------------------------------- schemas
TASK_TOOLS: list[types.Tool] = [
    types.Tool(
        name="weeek_whoami",
        description="Return the current user (id, name) for the API token. Useful to get your userId for assignments.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="weeek_list_members",
        description="List workspace members (id, name, email) — use their ids as assignees.",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="weeek_list_projects",
        description="List all task-manager projects (id, name).",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="weeek_list_boards",
        description="List boards of a project.",
        inputSchema={
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    ),
    types.Tool(
        name="weeek_list_board_columns",
        description="List board columns (statuses) for a board. board_id is required by the Weeek API.",
        inputSchema={
            "type": "object",
            "properties": {"board_id": {"type": "integer"}},
            "required": ["board_id"],
        },
    ),
    types.Tool(
        name="weeek_list_tasks",
        description="List tasks with optional filters (project, board, column, assignee, completion, tags, text search).",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "board_id": {"type": "integer"},
                "board_column_id": {"type": "integer"},
                "user_id": {"type": "string", "description": "Assignee id"},
                "completed": {"type": "boolean"},
                "type": {"type": "string", "enum": ["action", "meet", "call"]},
                "priority": PRIORITY_SCHEMA,
                "tags": {"type": "array", "items": {"type": "integer"}},
                "search": {"type": "string"},
                "day": {"type": "string", "description": "Y-m-d"},
                "start_date": {"type": "string", "description": "Y-m-d"},
                "end_date": {"type": "string", "description": "Y-m-d"},
                "per_page": {"type": "integer"},
                "offset": {"type": "integer"},
            },
        },
    ),
    types.Tool(
        name="weeek_get_task",
        description="Get one task by id.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_create_task",
        description=(
            "Create a task. Requires a project_id (and normally a board_column_id, "
            "which you get from weeek_list_board_columns). Dates are set separately via "
            "weeek_update_task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project_id": {"type": "integer"},
                "board_column_id": {
                    "type": ["integer", "null"],
                    "description": "Target column; null puts the task in the board default.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "Task description as HTML, which is what Weeek's create endpoint "
                        "stores. weeek_update_task takes Markdown instead, because it writes "
                        "through the editor rather than REST."
                    ),
                },
                "type": {"type": "string", "enum": ["action", "meet", "call"]},
                "priority": PRIORITY_SCHEMA,
                "custom_fields": {
                    "type": "object",
                    "description": (
                        "Custom field values keyed by field id (names only work on an "
                        "existing task, via weeek_update_task) — get the ids from "
                        "weeek_list_custom_fields. For a select field pass the option id. "
                        "A field that does not belong to this project is reported as an error."
                    ),
                },
                "day": {"type": "string", "description": "Y-m-d"},
                "user_id": {"type": "string", "description": "Assignee id"},
                "parent_id": {"type": "integer", "description": "Parent task id for a subtask"},
            },
            "required": ["title", "project_id"],
        },
    ),
    types.Tool(
        name="weeek_update_task",
        description=(
            "Update a task's fields (title, priority, type, dates, duration, tags), its "
            "custom field values, and its description."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "title": {"type": "string"},
                "description": {
                    "type": "string",
                    "description": (
                        "Replaces the description, as Markdown (same feature set as "
                        "weeek_kb_update); an empty string clears it. Weeek's REST API "
                        "ignores the description on update, so this drives its editor in a "
                        "headless browser (a few seconds) and needs the knowledge base session."
                    ),
                },
                "priority": PRIORITY_SCHEMA,
                "type": {"type": "string", "enum": ["action", "meet", "call"]},
                "start_date": {"type": "string", "description": "Y-m-d"},
                "due_date": {"type": "string", "description": "Y-m-d"},
                "start_date_time": {"type": "string", "description": "ISO 8601"},
                "due_date_time": {"type": "string", "description": "ISO 8601"},
                "duration": {"type": "integer", "description": "Estimate in minutes"},
                "tags": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "Tag ids — weeek_manage_tags lists them with their names.",
                },
                "custom_fields": {
                    "type": "object",
                    "description": (
                        "Custom field values keyed by field name or field id, e.g. "
                        '{"Ссылка на фичу": "https://..."}. For a select field pass the '
                        "option name or its id, for a multiselect a list of them; pass null "
                        "to clear a field. Names are matched against this task's own fields, "
                        "and a field that does not belong to this task's project is reported "
                        "as an error."
                    ),
                },
            },
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_list_custom_fields",
        description=(
            "List the task custom fields visible in a project (id, name, type, select "
            "options). Weeek's public API exposes no schema endpoint for them, so this "
            "reads the fields off one of the project's tasks — a project with no tasks yet "
            "returns nothing. Tasks list every field of the workspace, so some of them may "
            "belong to other projects; writing to one of those is reported as an error."
        ),
        inputSchema={
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    ),
    types.Tool(
        name="weeek_complete_task",
        description="Mark a task complete.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_uncomplete_task",
        description="Mark a completed task as not complete.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_delete_task",
        description="Delete a task by id.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_move_task",
        description=(
            "Move a task to a board column (status) and/or to another board. Give at least "
            "one of board_column_id, board_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "board_column_id": {"type": "integer"},
                "board_id": {"type": "integer", "description": "Target board; applied before the column."},
            },
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_set_assignees",
        description="Add assignees to a task (member ids from weeek_list_members).",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "assignees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "assignees"],
        },
    ),
    types.Tool(
        name="weeek_remove_assignees",
        description="Remove assignees from a task.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "assignees": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "assignees"],
        },
    ),
    types.Tool(
        name="weeek_set_task_parent",
        description=(
            "Nest a task under another one, or detach it with parent_id null. after/before "
            "place it among its new siblings."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "parent_id": {"type": ["integer", "null"], "description": "New parent; null makes it top-level."},
                "after": {"type": ["integer", "null"], "description": "Sibling task id to sit after."},
                "before": {"type": ["integer", "null"], "description": "Sibling task id to sit before."},
            },
            "required": ["task_id", "parent_id"],
        },
    ),
    types.Tool(
        name="weeek_add_task_to_project",
        description=("Put a task into a project (a task can live in several). Optionally target a board column there."),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "project_id": {"type": "integer"},
                "board_column_id": {"type": ["integer", "null"]},
            },
            "required": ["task_id", "project_id"],
        },
    ),
    types.Tool(
        name="weeek_remove_task_from_project",
        description="Remove a task from one of the projects it belongs to.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}, "project_id": {"type": "integer"}},
            "required": ["task_id", "project_id"],
        },
    ),
    types.Tool(
        name="weeek_set_watchers",
        description="Add watchers (subscribers) to a task — member ids from weeek_list_members.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "watchers": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "watchers"],
        },
    ),
    types.Tool(
        name="weeek_remove_watchers",
        description="Remove watchers from a task.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "watchers": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "watchers"],
        },
    ),
    types.Tool(
        name="weeek_task_timer",
        description="Start or stop the running timer on a task.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "action": {"type": "string", "enum": ["start", "stop"]},
            },
            "required": ["task_id", "action"],
        },
    ),
    types.Tool(
        name="weeek_manage_time_entry",
        description=(
            "Log time on a task, or edit/delete a logged entry. create and update need "
            "user_id, date, duration; delete needs entry_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "delete"]},
                "task_id": {"type": "integer"},
                "entry_id": {"type": "integer", "description": "Required for update and delete."},
                "user_id": {"type": "string", "description": "Whose time this is."},
                "date": {"type": "string", "description": "Y-m-d"},
                "duration": {"type": "integer", "description": "Minutes"},
                "is_overtime": {"type": "boolean", "default": False},
            },
            "required": ["action", "task_id"],
        },
    ),
    types.Tool(
        name="weeek_upload_attachment",
        description="Attach local files to a task. Paths must exist on this machine.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Absolute file paths."},
            },
            "required": ["task_id", "paths"],
        },
    ),
    types.Tool(
        name="weeek_get_attachment",
        description="Get one attachment's metadata and download URL by file id.",
        inputSchema={
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        },
    ),
    types.Tool(
        name="weeek_manage_tags",
        description=(
            "Workspace tags: list them (with ids to use in weeek_update_task), create, rename/recolor, or delete one."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                "tag_id": {"type": "integer", "description": "Required for update and delete."},
                "title": {"type": "string", "description": "Required for create and update."},
                "color": {"type": "string", "description": "Hex color; required by the API on update."},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="weeek_manage_projects",
        description=("Create, update, delete, archive or unarchive a project. Use weeek_list_projects to read them."),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "delete", "archive", "unarchive"]},
                "project_id": {"type": "integer", "description": "Required for everything but create."},
                "name": {"type": "string", "description": "Required for create and update."},
                "is_private": {"type": "boolean", "description": "Required by the API on create and update."},
                "description": {"type": "string"},
                "portfolio_id": {"type": "integer", "description": "Create only."},
                "color": {
                    "type": "string",
                    "description": "Hex color, e.g. #35AAFF. Required on update — the API rejects it missing.",
                },
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="weeek_manage_boards",
        description="Create, rename, delete or reorder a board. Use weeek_list_boards to read them.",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "delete", "move"]},
                "board_id": {"type": "integer", "description": "Required for everything but create."},
                "project_id": {"type": "integer", "description": "Required for create."},
                "name": {"type": "string", "description": "Required for create and update."},
                "upper_board_id": {
                    "type": ["integer", "null"],
                    "description": "move: the board to sit below; null moves it to the top.",
                },
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="weeek_manage_board_columns",
        description=(
            "Create, rename, delete or reorder a board column (status). Use weeek_list_board_columns to read them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "delete", "move"]},
                "board_column_id": {"type": "integer", "description": "Required for everything but create."},
                "board_id": {"type": "integer", "description": "Required for create."},
                "name": {"type": "string", "description": "Required for create and update."},
                "upper_board_column_id": {
                    "type": ["integer", "null"],
                    "description": "move: the column to sit after; null moves it first.",
                },
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="weeek_manage_portfolios",
        description="List, create, rename or delete portfolios (the folders projects live in).",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                "portfolio_id": {"type": "integer", "description": "Required for get, update and delete."},
                "name": {"type": "string", "description": "Required for create and update."},
                "parent_id": {"type": ["integer", "null"], "description": "Nest a portfolio under another one."},
                "search": {"type": "string", "description": "list only."},
                "limit": {"type": "integer", "description": "list only."},
                "offset": {"type": "integer", "description": "list only."},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="weeek_manage_custom_fields",
        description=(
            "Create, update, delete, move or transfer custom fields and their select "
            "options. A field belongs to one board, one project, or the whole task manager "
            "(scope global) — set scope and scope_id accordingly. weeek_list_custom_fields "
            "reads the fields a project's tasks actually show."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_global",
                        "create",
                        "update",
                        "delete",
                        "transfer",
                        "create_option",
                        "update_option",
                        "delete_option",
                        "move_option",
                    ],
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project", "board"],
                    "default": "global",
                    "description": "Where the field lives.",
                },
                "scope_id": {"type": "integer", "description": "Project or board id; omit for global."},
                "field_id": {"type": "string", "description": "Required for everything but create/list_global."},
                "option_id": {"type": "string", "description": "Required for the *_option actions except create."},
                "name": {"type": "string", "description": "Field or option name."},
                "type": {
                    "type": "string",
                    "enum": [
                        "text",
                        "boolean",
                        "datetime",
                        "select",
                        "multiselect",
                        "member",
                        "contact",
                        "link",
                        "approval",
                        "number",
                    ],
                    "description": "Required when creating a field.",
                },
                "color": {
                    "type": "string",
                    "enum": [
                        "blue",
                        "light_blue",
                        "dark_purple",
                        "purple",
                        "dark_pink",
                        "pink",
                        "light_pink",
                        "red",
                        "turquoise",
                        "green",
                        "light_green",
                        "dark_yellow",
                        "yellow",
                    ],
                    "description": "Required when creating or updating an option.",
                },
                "config": {"type": "object", "description": "Field type settings, when the type takes any."},
                "target": {
                    "type": "string",
                    "enum": ["global", "project", "board"],
                    "description": "transfer: where the field should end up.",
                },
                "target_id": {"type": "integer", "description": "transfer: target project or board id."},
                "after": {"type": "string", "description": "move_option: option id to sit after."},
                "before": {"type": "string", "description": "move_option: option id to sit before."},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="weeek_list_task_comments",
        description="List a task's comments, oldest first, with their author and text.",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    ),
    types.Tool(
        name="weeek_add_task_comment",
        description=(
            "Comment on a task. The text is Markdown (paragraphs, lists, bold/italic/code, links) "
            "and posts as you. Weeek's public API has no comments, so this drives its web API "
            "through the browser session — it needs WEEEK_EMAIL/WEEEK_PASSWORD or a cached login."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "text": {"type": "string", "description": "Comment body as Markdown."},
            },
            "required": ["task_id", "text"],
        },
    ),
]

KB_TOOLS: list[types.Tool] = [
    types.Tool(
        name="weeek_kb_search",
        description=(
            "Search knowledge base documents by title. Returns matches with their "
            "resource URIs — attach a match to the conversation/Project Context to pull "
            "in its full content (not a link)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    ),
    types.Tool(
        name="weeek_kb_list",
        description="List all knowledge base documents (id, title, resource URI).",
        inputSchema={
            "type": "object",
            "properties": {
                "force_refresh": {
                    "type": "boolean",
                    "description": "Bypass the cache and re-read the document tree.",
                }
            },
        },
    ),
    types.Tool(
        name="weeek_kb_read",
        description="Read a knowledge base document's content by id (returns markdown text).",
        inputSchema={
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
    ),
    types.Tool(
        name="weeek_kb_create",
        description=(
            "Create a knowledge base document. Optional Markdown content is stored on "
            "creation, supporting: headings, nested bullet/numbered/checkbox lists, "
            "blockquotes, fenced code, horizontal rules, pipe tables, images "
            "(![alt](url)), and inline **bold**, *italic*, ~~strike~~, `code`, "
            "[links](url). parent_id nests it under another document (folder). "
            "Tables are created spanning the document's content column, with the "
            "width split evenly between the columns; weeek_kb_table_widths changes that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content_markdown": {
                    "type": "string",
                    "description": (
                        "Markdown body: headings, nested lists, blockquotes, code fences, "
                        "hr, pipe tables, images, and inline bold/italic/strike/code/links."
                    ),
                },
                "parent_id": {"type": "string", "description": "Parent document id, for nesting."},
                "icon": {
                    "type": "string",
                    "description": "Document icon: a single emoji (e.g. 🚀) or a built-in icon name from weeek_kb_icons.",
                },
            },
            "required": ["title"],
        },
    ),
    types.Tool(
        name="weeek_kb_update",
        description=(
            "Update a knowledge base document. Rename via title, change its icon, and/or "
            "replace the body via content_markdown — same Markdown feature set as "
            "weeek_kb_create (headings, nested lists, tables, images, "
            "bold/italic/strike/code/links). Note: content replacement drives Weeek's "
            "editor in a headless browser (a few seconds) because bodies sync over a "
            "collaborative channel, not REST. Table column widths are carried across the "
            "replacement; a table that gained or lost a column is re-spread across the "
            "content column instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "title": {"type": "string"},
                "content_markdown": {
                    "type": "string",
                    "description": (
                        "Replaces the full body. Same Markdown feature set as weeek_kb_create: "
                        "headings, nested lists, blockquotes, code fences, hr, pipe tables, "
                        "images, and inline bold/italic/strike/code/links."
                    ),
                },
                "icon": {
                    "type": ["string", "null"],
                    "description": (
                        "New document icon: a single emoji (e.g. 🚀) or one of the built-in "
                        "icon names from weeek_kb_icons. Pass null or an empty string to "
                        "remove the current icon."
                    ),
                },
            },
            "required": ["doc_id"],
        },
    ),
    types.Tool(
        name="weeek_kb_table_widths",
        description=(
            "Resize the columns of a table in a knowledge base document. Weeek stores "
            "column widths in pixels (minimum 90) and nothing else — there is no row "
            "height or table width to set. Pass widths for exact sizes, or fit=true to "
            "spread the table across the document's content column (~676px), which is "
            "the usual intent for a table that looks too narrow. Tables are addressed "
            "by their order in the document, starting at 0; omitting table_index with "
            "fit=true resizes every table. Drives Weeek's editor in a headless browser "
            "(a few seconds), leaving the document's content untouched."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "table_index": {
                    "type": "integer",
                    "description": (
                        "Which table to resize, in document order (0 = first). Required "
                        "with widths; omit with fit=true to resize every table."
                    ),
                },
                "widths": {
                    "type": "array",
                    "items": {"type": ["integer", "null"]},
                    "description": (
                        "One width in pixels per column, in column order; minimum 90. "
                        "null leaves that column as it is. The list length must match "
                        "the table's column count."
                    ),
                },
                "fit": {
                    "type": "boolean",
                    "description": "Spread the columns evenly across the content column instead of giving widths.",
                },
            },
            "required": ["doc_id"],
        },
    ),
    types.Tool(
        name="weeek_kb_icons",
        description=(
            "List the built-in icon names accepted by the icon argument of "
            "weeek_kb_create/weeek_kb_update. Any single emoji works too, so call this "
            "only when you specifically want one of Weeek's own icons."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="weeek_kb_move",
        description="Nest an existing knowledge base document under another one (or move it elsewhere in the tree).",
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "parent_id": {"type": "string", "description": "New parent document id."},
            },
            "required": ["doc_id", "parent_id"],
        },
    ),
    types.Tool(
        name="weeek_kb_export",
        description=(
            "Export knowledge base documents to a local folder as Markdown files, "
            "mirroring the KB tree. Use this to feed folder-based integrations such as a "
            "Claude Desktop project's Context, which accepts folders rather than MCP "
            "resources. Re-run to refresh."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_dir": {"type": "string", "description": "Destination folder path."},
                "query": {
                    "type": "string",
                    "description": "Optional search filter; omit to export everything.",
                },
            },
            "required": ["target_dir"],
        },
    ),
    types.Tool(
        name="weeek_kb_delete",
        description="Delete a knowledge base document. Moves it to trash; set permanent to also delete it.",
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "permanent": {"type": "boolean"},
            },
            "required": ["doc_id"],
        },
    ),
]

ALL_TOOLS = TASK_TOOLS + KB_TOOLS


# --------------------------------------------------------------------------- handlers
_TAGS = re.compile(r"<[^>]+>")


def _text_of(markup: str) -> str:
    """Tag- and whitespace-free text, for comparing what we asked for with what stuck.

    Unescape first, then strip tags: Weeek hands descriptions back with the user's
    own ``<`` and ``&`` unescaped inside the markup, so text like ``a < b`` makes it
    invalid HTML. Normalizing both sides the same way keeps such a description
    comparable instead of reading as a failed edit.
    """
    return "".join(_TAGS.sub(" ", html.unescape(markup)).split())


def check_description_applied(task_response: Any, requested: str) -> None:
    """Confirm the edit reached the server — it syncs over a websocket, not the REST call."""
    stored = ((task_response or {}).get("task") or {}).get("description")
    if stored is None:
        return  # nothing echoed back to compare against
    if _text_of(stored) != _text_of(requested):
        raise ValueError(
            "The description was not stored as requested — Weeek now has "
            f"{_text_of(stored)[:80]!r}. The editor syncs over a websocket, so a slow "
            "connection can drop the edit; retry the same call."
        )


async def _write_description(kb: WeeekKB | None, task_id: int, markdown: str) -> str:
    """Set a task's description through Weeek's editor — REST cannot write it.

    Returns the HTML handed to the editor, which is what the result has to match.
    Markdown goes through the KB converter so that text like ``a < b & c`` is
    escaped: pasted raw, the browser would parse it as markup and drop it.
    """
    if kb is None:
        raise ValueError(
            "Updating a description needs the knowledge base session (Playwright): Weeek's "
            "REST API ignores the description on update. Set WEEEK_EMAIL/WEEEK_PASSWORD or "
            "run `weeek-mcp-login`, or recreate the task with weeek_create_task, which can "
            "set a description."
        )
    body = markdown_to_html(markdown) if markdown.strip() else ""
    await replace_task_description(kb.config, await kb.workspace(), str(task_id), body)
    return body


def _require_kb(kb: WeeekKB | None, what: str) -> WeeekKB:
    """Comments live only in Weeek's web API, which needs the browser session."""
    if kb is None:
        raise ValueError(
            f"{what} needs the browser session: Weeek's public API has no comments endpoint. "
            "Set WEEEK_EMAIL/WEEEK_PASSWORD or run `weeek-mcp-login`."
        )
    return kb


def _comment_text(comment: dict) -> str:
    return to_markdown((comment.get("content") or {}).get("data"))


def _comments_digest(comments: list[dict]) -> list[dict]:
    return [
        {
            "id": c.get("id"),
            "author": (c.get("user") or {}).get("name"),
            "sent_at": c.get("sentAt"),
            "text": _comment_text(c),
        }
        for c in comments
    ]


async def handle_task_tool(name: str, args: dict[str, Any], api: WeeekAPI, kb: WeeekKB | None = None) -> Any:
    if name == "weeek_whoami":
        return await api.whoami()
    if name == "weeek_list_members":
        return await api.list_members()
    if name == "weeek_list_projects":
        return await api.list_projects()
    if name == "weeek_list_boards":
        return await api.list_boards(args["project_id"])
    if name == "weeek_list_board_columns":
        return await api.list_board_columns(args.get("board_id"))
    if name == "weeek_list_tasks":
        return await api.list_tasks(
            projectId=args.get("project_id"),
            boardId=args.get("board_id"),
            boardColumnId=args.get("board_column_id"),
            userId=args.get("user_id"),
            completed=args.get("completed"),
            type=args.get("type"),
            priority=_priority(args.get("priority")),
            tags=args.get("tags"),
            search=args.get("search"),
            day=args.get("day"),
            startDate=args.get("start_date"),
            endDate=args.get("end_date"),
            perPage=args.get("per_page"),
            offset=args.get("offset"),
        )
    if name == "weeek_get_task":
        return await api.get_task(args["task_id"])
    if name == "weeek_list_task_comments":
        return _comments_digest(await _require_kb(kb, "Reading comments").list_task_comments(args["task_id"]))
    if name == "weeek_add_task_comment":
        comment = await _require_kb(kb, "Commenting").add_task_comment(args["task_id"], args["text"])
        return {"id": comment.get("id"), "text": _comment_text(comment)}
    if name == "weeek_list_custom_fields":
        fields = await project_custom_fields(api, args["project_id"])
        return [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "type": f.get("type"),
                **(
                    {"options": [{"id": o.get("id"), "name": o.get("name")} for o in f["options"]]}
                    if f.get("options")
                    else {}
                ),
            }
            for f in fields
        ]
    if name == "weeek_create_task":
        body = {
            "title": args["title"],
            "description": args.get("description"),
            "day": args.get("day"),
            "type": args.get("type"),
            "priority": _priority(args.get("priority")),
            "customFields": args.get("custom_fields"),
            "userId": args.get("user_id"),
            "parentId": args.get("parent_id"),
            "locations": [
                {
                    "projectId": args["project_id"],
                    "boardColumnId": args.get("board_column_id"),
                }
            ],
        }
        created = await api.create_task(body)
        if args.get("custom_fields"):
            # The task already exists at this point, so every complaint has to say so —
            # otherwise the obvious retry is to create it a second time.
            _report_created_task_fields(created, args["custom_fields"])
        return created
    if name == "weeek_update_task":
        body = {
            "title": args.get("title"),
            "priority": _priority(args.get("priority")),
            "type": args.get("type"),
            "startDate": args.get("start_date"),
            "dueDate": args.get("due_date"),
            "startDateTime": args.get("start_date_time"),
            "dueDateTime": args.get("due_date_time"),
            "duration": args.get("duration"),
            "tags": args.get("tags"),
        }
        task_fields: list[dict[str, Any]] = []
        requested: dict[str, Any] = {}
        if args.get("custom_fields"):
            task_fields = await task_custom_fields(api, args["task_id"])
            requested = resolve_custom_fields(task_fields, args["custom_fields"])
            body["customFields"] = requested

        updated = await api.update_task(args["task_id"], body)

        dropped = dropped_custom_fields(updated, requested, task_fields) if requested else []
        if dropped:
            raise ValueError(
                "Weeek did not store "
                + ", ".join(repr(d) for d in dropped)
                + " — a custom field only applies to the projects it was added to. Any other "
                "values in the same call were saved."
            )
        if args.get("description") is not None:
            written = await _write_description(kb, args["task_id"], args["description"])
            updated = await api.get_task(args["task_id"])  # the REST response predates the edit
            check_description_applied(updated, written)
        return updated
    if name == "weeek_complete_task":
        return await api.complete_task(args["task_id"])
    if name == "weeek_uncomplete_task":
        return await api.uncomplete_task(args["task_id"])
    if name == "weeek_delete_task":
        return await api.delete_task(args["task_id"])
    if name == "weeek_move_task":
        moves = []
        if args.get("board_id") is not None:
            moves.append(await api.move_task_to_board(args["task_id"], args["board_id"]))
        if args.get("board_column_id") is not None:
            moves.append(await api.move_task_to_column(args["task_id"], args["board_column_id"]))
        if not moves:
            raise ValueError("weeek_move_task needs board_column_id and/or board_id.")
        return moves[-1]
    if name == "weeek_set_assignees":
        return await api.add_assignees(args["task_id"], args["assignees"])
    if name == "weeek_remove_assignees":
        return await api.remove_assignees(args["task_id"], args["assignees"])
    if name == "weeek_set_task_parent":
        body = {"parentId": args["parent_id"]}
        for key, field in (("after", "after"), ("before", "before")):
            if args.get(key) is not None:
                body[field] = args[key]
        return await api.set_task_parent(args["task_id"], body)
    if name == "weeek_add_task_to_project":
        return await api.add_task_location(
            args["task_id"],
            {"projectId": args["project_id"], "boardColumnId": args.get("board_column_id")},
        )
    if name == "weeek_remove_task_from_project":
        return await api.remove_task_location(args["task_id"], args["project_id"])
    if name == "weeek_set_watchers":
        return await api.add_watchers(args["task_id"], args["watchers"])
    if name == "weeek_remove_watchers":
        return await api.remove_watchers(args["task_id"], args["watchers"])
    if name == "weeek_task_timer":
        return await api.task_timer(args["task_id"], running=args["action"] == "start")
    if name == "weeek_manage_time_entry":
        return await _handle_time_entry(args, api)
    if name == "weeek_upload_attachment":
        missing = [p for p in args["paths"] if not Path(p).expanduser().is_file()]
        if missing:
            raise ValueError("No such file(s): " + ", ".join(missing))
        return await api.upload_attachments(args["task_id"], [str(Path(p).expanduser()) for p in args["paths"]])
    if name == "weeek_get_attachment":
        return await api.get_attachment(args["file_id"])
    if name == "weeek_manage_tags":
        return await _handle_tags(args, api)
    if name == "weeek_manage_projects":
        return await _handle_projects(args, api)
    if name == "weeek_manage_boards":
        return await _handle_boards(args, api)
    if name == "weeek_manage_board_columns":
        return await _handle_board_columns(args, api)
    if name == "weeek_manage_portfolios":
        return await _handle_portfolios(args, api)
    if name == "weeek_manage_custom_fields":
        return await _handle_custom_fields(args, api)
    raise ValueError(f"Unknown task tool: {name}")


def _need(args: dict[str, Any], tool: str, action: str, *keys: str) -> None:
    missing = [k for k in keys if args.get(k) is None]
    if missing:
        raise ValueError(f"{tool} {action!r} needs {', '.join(missing)}.")


def _unknown_action(tool: str, action: Any, *known: str) -> ValueError:
    """An action nobody handled must not fall through to a neighbouring one."""
    return ValueError(f"{tool} does not know action {action!r}. Use one of: {', '.join(known)}.")


async def _handle_time_entry(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool = "weeek_manage_time_entry"
    action, task_id = args["action"], args["task_id"]
    if action == "delete":
        _need(args, tool, action, "entry_id")
        return await api.delete_time_entry(task_id, args["entry_id"])
    if action not in ("create", "update"):
        raise _unknown_action(tool, action, "create", "update", "delete")

    _need(args, tool, action, "user_id", "date", "duration")
    body = {
        "userId": args["user_id"],
        "date": args["date"],
        "duration": args["duration"],
        "isOvertime": bool(args.get("is_overtime")),
    }
    if action == "create":
        return await api.create_time_entry(task_id, body)

    _need(args, tool, action, "entry_id")
    if args.get("is_overtime") is None:
        # The API requires the flag, so an update that omits it would reset it.
        body["isOvertime"] = await _current_overtime(api, task_id, args["entry_id"])
    return await api.update_time_entry(task_id, args["entry_id"], body)


async def _current_overtime(api: WeeekAPI, task_id: int, entry_id: Any) -> bool:
    task = (await api.get_task(task_id)).get("task") or {}
    for entry in task.get("timeEntries") or []:
        if str(entry.get("id")) == str(entry_id):
            return bool(entry.get("isOvertime"))
    return False


async def _handle_tags(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool, action = "weeek_manage_tags", args["action"]
    if action == "list":
        return await api.list_tags()
    if action == "create":
        _need(args, tool, action, "title")
        return await api.create_tag(args["title"])
    if action not in ("update", "delete"):
        raise _unknown_action(tool, action, "list", "create", "update", "delete")

    _need(args, tool, action, "tag_id")
    if action == "delete":
        return await api.delete_tag(args["tag_id"])
    _need(args, tool, action, "title", "color")
    return await api.update_tag(args["tag_id"], {"title": args["title"], "color": args["color"]})


async def _handle_projects(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool, action = "weeek_manage_projects", args["action"]
    if action == "create":
        _need(args, tool, action, "name")
        return await api.create_project(
            {
                "name": args["name"],
                "isPrivate": bool(args.get("is_private")),
                "description": args.get("description"),
                "portfolioId": args.get("portfolio_id"),
            }
        )
    if action not in ("update", "delete", "archive", "unarchive"):
        raise _unknown_action(tool, action, "create", "update", "delete", "archive", "unarchive")

    _need(args, tool, action, "project_id")
    if action == "delete":
        return await api.delete_project(args["project_id"])
    if action in ("archive", "unarchive"):
        return await api.archive_project(args["project_id"], archived=action == "archive")
    # color is optional in Weeek's spec but rejected as missing by the API (422).
    _need(args, tool, action, "name", "color")
    is_private = args.get("is_private")
    if is_private is None:
        # The API requires the flag, so renaming a private project would publish it.
        current = (await api.get_project(args["project_id"])).get("project") or {}
        is_private = bool(current.get("isPrivate"))
    return await api.update_project(
        args["project_id"],
        {"name": args["name"], "isPrivate": bool(is_private), "color": args["color"]},
    )


async def _handle_boards(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool, action = "weeek_manage_boards", args["action"]
    if action == "create":
        _need(args, tool, action, "name", "project_id")
        return await api.create_board({"name": args["name"], "projectId": args["project_id"]})
    if action not in ("update", "delete", "move"):
        raise _unknown_action(tool, action, "create", "update", "delete", "move")

    _need(args, tool, action, "board_id")
    if action == "delete":
        return await api.delete_board(args["board_id"])
    if action == "move":
        return await api.move_board(args["board_id"], args.get("upper_board_id"))
    _need(args, tool, action, "name")
    return await api.update_board(args["board_id"], {"name": args["name"]})


async def _handle_board_columns(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool, action = "weeek_manage_board_columns", args["action"]
    if action == "create":
        _need(args, tool, action, "name", "board_id")
        return await api.create_board_column({"name": args["name"], "boardId": args["board_id"]})
    if action not in ("update", "delete", "move"):
        raise _unknown_action(tool, action, "create", "update", "delete", "move")

    _need(args, tool, action, "board_column_id")
    if action == "delete":
        return await api.delete_board_column(args["board_column_id"])
    if action == "move":
        return await api.move_board_column(args["board_column_id"], args.get("upper_board_column_id"))
    _need(args, tool, action, "name")
    return await api.update_board_column(args["board_column_id"], {"name": args["name"]})


async def _handle_portfolios(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool, action = "weeek_manage_portfolios", args["action"]
    if action == "list":
        return await api.list_portfolios(
            search=args.get("search"),
            parentId=args.get("parent_id"),
            limit=args.get("limit"),
            offset=args.get("offset"),
        )
    if action == "create":
        _need(args, tool, action, "name")
        return await api.create_portfolio({"name": args["name"], "parentId": args.get("parent_id")})
    if action not in ("get", "update", "delete"):
        raise _unknown_action(tool, action, "list", "get", "create", "update", "delete")

    _need(args, tool, action, "portfolio_id")
    if action == "get":
        return await api.get_portfolio(args["portfolio_id"])
    if action == "delete":
        return await api.delete_portfolio(args["portfolio_id"])
    _need(args, tool, action, "name")
    return await api.update_portfolio(args["portfolio_id"], {"name": args["name"]})


_CUSTOM_FIELD_ACTIONS = (
    "list_global",
    "create",
    "update",
    "delete",
    "transfer",
    "create_option",
    "update_option",
    "delete_option",
    "move_option",
)


async def _handle_custom_fields(args: dict[str, Any], api: WeeekAPI) -> Any:
    tool, action = "weeek_manage_custom_fields", args["action"]
    if action not in _CUSTOM_FIELD_ACTIONS:
        raise _unknown_action(tool, action, *_CUSTOM_FIELD_ACTIONS)
    if action == "list_global":
        return await api.list_global_custom_fields()

    scope = args.get("scope", "global")
    scope_id = args.get("scope_id")
    if scope != "global" and scope_id is None:
        raise ValueError(f"{tool} scope {scope!r} needs scope_id.")

    if action == "create":
        _need(args, tool, action, "type")
        return await api.create_custom_field(
            scope, scope_id, {"name": args.get("name"), "type": args["type"], "config": args.get("config")}
        )

    _need(args, tool, action, "field_id")
    field_id = args["field_id"]
    if action == "update":
        return await api.update_custom_field(
            scope, scope_id, field_id, {"name": args.get("name"), "config": args.get("config")}
        )
    if action == "delete":
        return await api.delete_custom_field(scope, scope_id, field_id)
    if action == "transfer":
        _need(args, tool, action, "target")
        target = args["target"]
        if target != "global" and args.get("target_id") is None:
            raise ValueError(f"{tool} 'transfer' needs target_id for a project or board target.")
        return await api.transfer_custom_field(scope, scope_id, field_id, target, args.get("target_id"))
    if action == "create_option":
        _need(args, tool, action, "name", "color")
        return await api.create_custom_field_option(
            scope, scope_id, field_id, {"name": args["name"], "color": args["color"]}
        )

    _need(args, tool, action, "option_id")
    option_id = args["option_id"]
    if action == "update_option":
        _need(args, tool, action, "name", "color")
        return await api.update_custom_field_option(
            scope, scope_id, field_id, option_id, {"name": args["name"], "color": args["color"]}
        )
    if action == "delete_option":
        return await api.delete_custom_field_option(scope, scope_id, field_id, option_id)
    # The API takes after or before, and rejects a body with neither (422).
    if args.get("after") is None and args.get("before") is None:
        raise ValueError(f"{tool} 'move_option' needs after or before — the option id to sit next to.")
    return await api.move_custom_field_option(
        scope, scope_id, field_id, option_id, {"after": args.get("after"), "before": args.get("before")}
    )


def _doc_payload(doc: KBDocument) -> dict[str, Any]:
    # icon is always present so its absence reads as "no icon", not as a field we forgot.
    return {"id": doc.id, "title": doc.title, "path": doc.path, "uri": kb_uri(doc.id), "icon": doc.icon}


async def handle_kb_tool(name: str, args: dict[str, Any], kb: WeeekKB) -> Any:
    if name == "weeek_kb_search":
        return [_doc_payload(d) for d in await kb.search(args["query"])]
    if name == "weeek_kb_list":
        docs = await kb.list_documents(force=bool(args.get("force_refresh")))
        return [_doc_payload(d) for d in docs]
    if name == "weeek_kb_read":
        return await kb.read_document(args["doc_id"])
    if name == "weeek_kb_icons":
        return await kb.icon_names()
    if name == "weeek_kb_create":
        doc = await kb.create_document(
            args["title"],
            markdown=args.get("content_markdown"),
            parent_id=args.get("parent_id"),
            icon=args.get("icon"),
        )
        return _doc_payload(doc)
    if name == "weeek_kb_update":
        actions = []
        if args.get("title") is not None:
            await kb.rename_document(args["doc_id"], args["title"])
            actions.append("renamed")
        if args.get("content_markdown") is not None:
            await kb.update_content(args["doc_id"], args["content_markdown"])
            actions.append("content replaced")
        if "icon" in args:
            label = await kb.set_icon(args["doc_id"], args["icon"])
            actions.append(f"icon set to {label}" if label else "icon cleared")
        if not actions:
            raise ValueError("weeek_kb_update needs title, content_markdown and/or icon.")
        return {"id": args["doc_id"], "updated": actions}
    if name == "weeek_kb_table_widths":
        result = await kb.set_table_widths(
            args["doc_id"],
            table_index=args.get("table_index"),
            widths=args.get("widths"),
            fit=bool(args.get("fit")),
        )
        return {"id": args["doc_id"], **result}
    if name == "weeek_kb_move":
        await kb.move_document(args["doc_id"], args["parent_id"])
        return {"id": args["doc_id"], "parent_id": args["parent_id"], "moved": True}
    if name == "weeek_kb_export":
        result = await kb.export_documents(args["target_dir"], query=args.get("query", ""))
        # Keep the response compact: counts and directory, not every path.
        return {"exported": result["exported"], "directory": result["directory"]}
    if name == "weeek_kb_delete":
        await kb.delete_document(args["doc_id"], permanent=bool(args.get("permanent")))
        return {"id": args["doc_id"], "deleted": True, "permanent": bool(args.get("permanent"))}
    raise ValueError(f"Unknown kb tool: {name}")


TASK_TOOL_NAMES = {t.name for t in TASK_TOOLS}
KB_TOOL_NAMES = {t.name for t in KB_TOOLS}
