import httpx
import pytest
import respx

from weeek_mcp.weeek_api import WeeekAPI, WeeekAPIError

BASE = "https://api.weeek.net/public/v1"


@pytest.fixture
async def api():
    client = WeeekAPI("token", BASE)
    yield client
    await client.aclose()


@respx.mock
async def test_list_tasks_drops_none_params(api):
    route = respx.get(f"{BASE}/tm/tasks").mock(return_value=httpx.Response(200, json={"success": True, "tasks": []}))
    await api.list_tasks(projectId=5, boardId=None, search="hi")

    assert route.called
    sent = route.calls.last.request.url
    assert "projectId=5" in str(sent)
    assert "search=hi" in str(sent)
    assert "boardId" not in str(sent)  # None must be dropped


@respx.mock
async def test_create_task_posts_body(api):
    route = respx.post(f"{BASE}/tm/tasks").mock(
        return_value=httpx.Response(200, json={"success": True, "task": {"id": 1}})
    )
    body = {
        "title": "Do it",
        "description": None,
        "locations": [{"projectId": 5, "boardColumnId": 10}],
    }
    await api.create_task(body)

    import json

    payload = json.loads(route.calls.last.request.content)
    assert payload["title"] == "Do it"
    assert payload["locations"] == [{"projectId": 5, "boardColumnId": 10}]
    assert "description" not in payload  # None must be dropped


@respx.mock
async def test_error_raises(api):
    respx.get(f"{BASE}/tm/tasks/999").mock(return_value=httpx.Response(404, json={"message": "not found"}))
    with pytest.raises(WeeekAPIError) as exc:
        await api.get_task(999)
    assert exc.value.status_code == 404


@respx.mock
async def test_complete_task_no_content(api):
    respx.post(f"{BASE}/tm/tasks/1/complete").mock(return_value=httpx.Response(204))
    result = await api.complete_task(1)
    assert result == {"success": True}


@respx.mock
async def test_a_refusal_without_an_http_error_still_raises(api):
    # A plan limit answers 200 with success:false and creates nothing.
    respx.post(f"{BASE}/tm/projects").mock(return_value=httpx.Response(200, json={"success": False, "reason": "limit"}))
    with pytest.raises(WeeekAPIError) as exc:
        await api.create_project({"name": "P", "isPrivate": True})
    assert exc.value.body["reason"] == "limit"
