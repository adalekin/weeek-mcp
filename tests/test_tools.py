"""Tests for tool dispatch logic that doesn't need a live backend."""

import pytest

from weeek_mcp import tools


class FakeAPI:
    def __init__(self):
        self.calls = []

    async def create_task(self, body):
        self.calls.append(("create_task", body))
        return {"success": True}

    async def list_tasks(self, **filters):
        self.calls.append(("list_tasks", filters))
        return {"success": True, "tasks": []}


async def test_create_task_builds_locations():
    api = FakeAPI()
    await tools.handle_task_tool(
        "weeek_create_task",
        {"title": "T", "project_id": 5, "board_column_id": 10, "priority": 2},
        api,
    )
    _, body = api.calls[-1]
    assert body["title"] == "T"
    assert body["locations"] == [{"projectId": 5, "boardColumnId": 10}]
    assert body["priority"] == 2


async def test_create_task_allows_null_column():
    api = FakeAPI()
    await tools.handle_task_tool("weeek_create_task", {"title": "T", "project_id": 5}, api)
    _, body = api.calls[-1]
    assert body["locations"] == [{"projectId": 5, "boardColumnId": None}]


async def test_list_tasks_maps_snake_to_camel():
    api = FakeAPI()
    await tools.handle_task_tool("weeek_list_tasks", {"project_id": 5, "board_column_id": 3}, api)
    _, filters = api.calls[-1]
    assert filters["projectId"] == 5
    assert filters["boardColumnId"] == 3


async def test_unknown_tool_raises():
    with pytest.raises(ValueError):
        await tools.handle_task_tool("nope", {}, FakeAPI())


def test_kb_uri_roundtrip():
    assert tools.kb_doc_id_from_uri(tools.kb_uri("abc123")) == "abc123"
