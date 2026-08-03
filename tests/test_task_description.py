"""Task description updates, which go through the editor rather than REST."""

import pytest

from weeek_mcp import tools


class FakeAPI:
    """Weeek as seen over REST: the description only changes when the editor writes it."""

    def __init__(self, description="<p>исходный текст</p>"):
        self.calls = []
        self.description = description

    async def update_task(self, task_id, body):
        self.calls.append(("update_task", task_id, body))
        return {"success": True, "task": {"id": task_id, "description": self.description}}

    async def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return {"success": True, "task": {"id": task_id, "description": self.description}}


class FakeKB:
    config = "cfg"

    async def workspace(self):
        return "923663"


class FakeEditor:
    """Stands in for the headless editor; syncs its writes into the APIs watching it."""

    def __init__(self):
        self.writes = []
        self.watchers: list[FakeAPI] = []

    def watching(self, api: FakeAPI) -> FakeAPI:
        self.watchers.append(api)
        return api

    async def __call__(self, cfg, workspace_id, task_id, html):
        self.writes.append((cfg, workspace_id, task_id, html))
        for api in self.watchers:
            api.description = html or "<p></p>"


@pytest.fixture
def editor(monkeypatch):
    fake = FakeEditor()
    monkeypatch.setattr(tools, "replace_task_description", fake)
    return fake


async def test_description_goes_through_the_editor_not_the_rest_body(editor):
    api = editor.watching(FakeAPI())
    await tools.handle_task_tool(
        "weeek_update_task",
        {"task_id": 42, "description": "новый **текст**"},
        api,
        FakeKB(),
    )
    _, _, body = api.calls[0]
    assert "description" not in body  # REST ignores it, so it must not look like it worked
    assert editor.writes == [("cfg", "923663", "42", "<p>новый <strong>текст</strong></p>")]


async def test_markdown_special_characters_are_escaped_before_pasting(editor):
    # Pasted raw, "a < b & c" would be parsed as markup by the browser and lost.
    api = editor.watching(FakeAPI())
    await tools.handle_task_tool(
        "weeek_update_task", {"task_id": 42, "description": "Цена < 100 & больше"}, api, FakeKB()
    )
    assert editor.writes[-1][3] == "<p>Цена &lt; 100 &amp; больше</p>"


async def test_an_empty_description_clears_it(editor):
    api = editor.watching(FakeAPI())
    await tools.handle_task_tool("weeek_update_task", {"task_id": 42, "description": ""}, api, FakeKB())
    assert editor.writes[-1][3] == ""


async def test_the_returned_task_is_re_read_after_the_edit(editor):
    api = editor.watching(FakeAPI())
    result = await tools.handle_task_tool("weeek_update_task", {"task_id": 42, "description": ""}, api, FakeKB())
    assert ("get_task", 42) in api.calls
    assert result["task"]["description"] == "<p></p>"


async def test_an_edit_that_did_not_land_is_reported(editor):
    api = FakeAPI()  # not watching: the editor write never reaches the server
    with pytest.raises(ValueError, match="not stored"):
        await tools.handle_task_tool("weeek_update_task", {"task_id": 42, "description": "<p>новый</p>"}, api, FakeKB())


def test_markup_differences_alone_do_not_count_as_failure():
    tools.check_description_applied({"task": {"description": "<p>привет</p>"}}, "привет")
    tools.check_description_applied({"task": {"description": "<p></p>"}}, "")
    tools.check_description_applied({"task": {"description": None}}, "что угодно")


def test_entities_are_not_a_difference():
    # We paste escaped markup; Weeek hands it back with the user's own < and & raw,
    # which makes the stored value invalid HTML. Both must still compare equal.
    tools.check_description_applied(
        {"task": {"description": "<p>Цена < 100 & больше</p>"}}, "<p>Цена &lt; 100 &amp; больше</p>"
    )
    tools.check_description_applied({"task": {"description": "<p>a &amp; b</p>"}}, "<p>a &amp; b</p>")


async def test_without_a_session_the_error_names_the_way_out(editor):
    with pytest.raises(ValueError, match="weeek-mcp-login"):
        await tools.handle_task_tool("weeek_update_task", {"task_id": 42, "description": ""}, FakeAPI(), None)
    assert editor.writes == []


async def test_other_updates_still_work_without_a_session(editor):
    api = FakeAPI()
    await tools.handle_task_tool("weeek_update_task", {"task_id": 42, "title": "T"}, api, None)
    assert api.calls[0][2]["title"] == "T"
    assert editor.writes == []
