"""MCP tool definitions and dispatch.

Split into two groups:
  * task tools  -> Weeek public REST API (weeek_api.WeeekAPI)
  * kb tools    -> knowledge base search over Playwright (kb.client.WeeekKB)

Tool input schemas mirror the Weeek OpenAPI spec. The server module wires these
handlers to the low-level MCP Server.
"""

from __future__ import annotations

from typing import Any

import mcp.types as types

from .kb.client import WeeekKB
from .weeek_api import WeeekAPI

KB_URI_SCHEME = "weeek-kb"


def kb_uri(doc_id: str) -> str:
    return f"{KB_URI_SCHEME}://{doc_id}"


def kb_doc_id_from_uri(uri: str) -> str:
    return uri.split("://", 1)[-1].strip("/")


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
        description="List board columns (statuses). Optionally filter by board_id.",
        inputSchema={
            "type": "object",
            "properties": {"board_id": {"type": "integer"}},
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
                "priority": {"type": "integer", "enum": [0, 1, 2, 3]},
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
                "description": {"type": "string"},
                "type": {"type": "string", "enum": ["action", "meet", "call"]},
                "priority": {"type": "integer", "enum": [0, 1, 2, 3]},
                "day": {"type": "string", "description": "Y-m-d"},
                "user_id": {"type": "string", "description": "Assignee id"},
                "parent_id": {"type": "integer", "description": "Parent task id for a subtask"},
            },
            "required": ["title", "project_id"],
        },
    ),
    types.Tool(
        name="weeek_update_task",
        description="Update a task's fields (title, priority, type, dates, duration, tags).",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "title": {"type": "string"},
                "priority": {"type": "integer", "enum": [0, 1, 2, 3]},
                "type": {"type": "string", "enum": ["action", "meet", "call"]},
                "start_date": {"type": "string", "description": "Y-m-d"},
                "due_date": {"type": "string", "description": "Y-m-d"},
                "start_date_time": {"type": "string", "description": "ISO 8601"},
                "due_date_time": {"type": "string", "description": "ISO 8601"},
                "duration": {"type": "integer", "description": "Estimate in minutes"},
                "tags": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task_id"],
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
        description="Move a task to a board column (status).",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "board_column_id": {"type": "integer"},
            },
            "required": ["task_id", "board_column_id"],
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
            "creation. parent_id nests it under another document (folder)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content_markdown": {"type": "string"},
                "parent_id": {"type": "string", "description": "Parent document id, for nesting."},
            },
            "required": ["title"],
        },
    ),
    types.Tool(
        name="weeek_kb_update",
        description=(
            "Update a knowledge base document. Rename via title, and/or replace the body "
            "via content_markdown. Note: content replacement drives Weeek's editor in a "
            "headless browser (a few seconds) because bodies sync over a collaborative "
            "channel, not REST."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "title": {"type": "string"},
                "content_markdown": {"type": "string"},
            },
            "required": ["doc_id"],
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
async def handle_task_tool(name: str, args: dict[str, Any], api: WeeekAPI) -> Any:
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
            priority=args.get("priority"),
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
    if name == "weeek_create_task":
        body = {
            "title": args["title"],
            "description": args.get("description"),
            "day": args.get("day"),
            "type": args.get("type"),
            "priority": args.get("priority"),
            "userId": args.get("user_id"),
            "parentId": args.get("parent_id"),
            "locations": [
                {
                    "projectId": args["project_id"],
                    "boardColumnId": args.get("board_column_id"),
                }
            ],
        }
        return await api.create_task(body)
    if name == "weeek_update_task":
        body = {
            "title": args.get("title"),
            "priority": args.get("priority"),
            "type": args.get("type"),
            "startDate": args.get("start_date"),
            "dueDate": args.get("due_date"),
            "startDateTime": args.get("start_date_time"),
            "dueDateTime": args.get("due_date_time"),
            "duration": args.get("duration"),
            "tags": args.get("tags"),
        }
        return await api.update_task(args["task_id"], body)
    if name == "weeek_complete_task":
        return await api.complete_task(args["task_id"])
    if name == "weeek_uncomplete_task":
        return await api.uncomplete_task(args["task_id"])
    if name == "weeek_delete_task":
        return await api.delete_task(args["task_id"])
    if name == "weeek_move_task":
        return await api.move_task_to_column(args["task_id"], args["board_column_id"])
    if name == "weeek_set_assignees":
        return await api.add_assignees(args["task_id"], args["assignees"])
    if name == "weeek_remove_assignees":
        return await api.remove_assignees(args["task_id"], args["assignees"])
    raise ValueError(f"Unknown task tool: {name}")


async def handle_kb_tool(name: str, args: dict[str, Any], kb: WeeekKB) -> Any:
    if name == "weeek_kb_search":
        docs = await kb.search(args["query"])
        return [{"id": d.id, "title": d.title, "path": d.path, "uri": kb_uri(d.id)} for d in docs]
    if name == "weeek_kb_list":
        docs = await kb.list_documents(force=bool(args.get("force_refresh")))
        return [{"id": d.id, "title": d.title, "path": d.path, "uri": kb_uri(d.id)} for d in docs]
    if name == "weeek_kb_read":
        return await kb.read_document(args["doc_id"])
    if name == "weeek_kb_create":
        doc = await kb.create_document(
            args["title"],
            markdown=args.get("content_markdown"),
            parent_id=args.get("parent_id"),
        )
        return {"id": doc.id, "title": doc.title, "path": doc.path, "uri": kb_uri(doc.id)}
    if name == "weeek_kb_update":
        actions = []
        if args.get("title") is not None:
            await kb.rename_document(args["doc_id"], args["title"])
            actions.append("renamed")
        if args.get("content_markdown") is not None:
            await kb.update_content(args["doc_id"], args["content_markdown"])
            actions.append("content replaced")
        if not actions:
            raise ValueError("weeek_kb_update needs title and/or content_markdown.")
        return {"id": args["doc_id"], "updated": actions}
    if name == "weeek_kb_delete":
        await kb.delete_document(args["doc_id"], permanent=bool(args.get("permanent")))
        return {"id": args["doc_id"], "deleted": True, "permanent": bool(args.get("permanent"))}
    raise ValueError(f"Unknown kb tool: {name}")


TASK_TOOL_NAMES = {t.name for t in TASK_TOOLS}
KB_TOOL_NAMES = {t.name for t in KB_TOOLS}
