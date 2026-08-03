"""Priority labels and custom field resolution (no live backend)."""

import pytest

from weeek_mcp import tools

# Weeek addresses fields and select options by uuid; the create path checks that shape.
LINK = "a22f80bc-e1f3-4691-a24d-5c152ce69e34"
SELECT = "a22f8099-816e-475f-adc0-8cd091b0f5e4"
MOCKUP = "a14082a0-c001-4fa7-816d-d5b9d6bca533"
EPIC = "a22f80a9-13f5-402e-92a2-b2bb0ccd4ce3"

FIELDS = [
    {"id": LINK, "name": "Ссылка на фичу", "type": "link", "value": None},
    {"id": SELECT, "name": None, "type": "select", "options": [{"id": EPIC, "name": "Эпик"}], "value": None},
    {"id": MOCKUP, "name": "Макет", "type": "link", "value": None},
]


def _stored(values, task_id=7):
    """A task payload echoing back the values Weeek actually stored."""
    fields = [{**f, "value": values.get(f["id"])} for f in FIELDS]
    return {"success": True, "task": {"id": task_id, "customFields": fields}}


class FakeAPI:
    """Stands in for Weeek: writes land unless `drops` says the project rejects them."""

    def __init__(self, tasks=None, drops=()):
        self.calls = []
        self._tasks = tasks or []
        self._drops = set(drops)

    async def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return {"success": True, "task": {"id": task_id, "customFields": FIELDS}}

    async def update_task(self, task_id, body):
        self.calls.append(("update_task", task_id, body))
        written = {k: v for k, v in (body.get("customFields") or {}).items() if k not in self._drops}
        return _stored(written)

    async def create_task(self, body):
        self.calls.append(("create_task", body))
        written = {k: v for k, v in (body.get("customFields") or {}).items() if k not in self._drops}
        return _stored(written)

    async def list_tasks(self, **filters):
        self.calls.append(("list_tasks", filters))
        return {"success": True, "tasks": self._tasks}


# ----------------------------------------------------------------- priorities
@pytest.mark.parametrize(("given", "expected"), [("high", 2), ("HIGH", 2), (" hold ", 3), (2, 2), (None, None)])
def test_priority_accepts_labels_and_numbers(given, expected):
    assert tools._priority(given) == expected


def test_unknown_priority_lists_the_labels():
    with pytest.raises(ValueError, match="medium"):
        tools._priority("важный")


async def test_update_task_converts_the_priority_label():
    api = FakeAPI()
    await tools.handle_task_tool("weeek_update_task", {"task_id": 1, "priority": "high"}, api)
    _, _, body = api.calls[-1]
    assert body["priority"] == 2


# -------------------------------------------------------------- custom fields
def test_resolves_field_by_name_and_by_id():
    resolved = tools.resolve_custom_fields(FIELDS, {"Ссылка на фичу": "https://x", MOCKUP: "https://y"})
    assert resolved == {LINK: "https://x", MOCKUP: "https://y"}


def test_field_names_are_matched_loosely():
    assert tools.resolve_custom_fields(FIELDS, {"  макет ": "https://y"}) == {MOCKUP: "https://y"}


def test_none_clears_a_field():
    assert tools.resolve_custom_fields(FIELDS, {"Макет": None}) == {MOCKUP: None}


def test_select_option_name_becomes_its_id():
    assert tools.resolve_custom_fields(FIELDS, {SELECT: "Эпик"}) == {SELECT: EPIC}
    assert tools.resolve_custom_fields(FIELDS, {SELECT: EPIC}) == {SELECT: EPIC}


def test_unknown_option_lists_the_options():
    with pytest.raises(ValueError, match="Эпик"):
        tools.resolve_custom_fields(FIELDS, {SELECT: "Багфикс"})


def test_unknown_field_lists_names_and_unnamed_ids():
    with pytest.raises(ValueError) as exc:
        tools.resolve_custom_fields(FIELDS, {"Приоритет разработки": "x"})
    message = str(exc.value)
    assert "Ссылка на фичу" in message  # named fields
    assert SELECT in message  # unnamed ones can only be addressed by id


def test_unknown_field_on_a_task_without_any():
    with pytest.raises(ValueError, match="has none"):
        tools.resolve_custom_fields([], {"whatever": "x"})


async def test_update_task_resolves_against_the_tasks_own_fields():
    api = FakeAPI()
    await tools.handle_task_tool(
        "weeek_update_task",
        {"task_id": 42, "custom_fields": {"Ссылка на фичу": "https://x"}},
        api,
    )
    assert ("get_task", 42) in api.calls
    _, _, body = api.calls[-1]
    assert body["customFields"] == {LINK: "https://x"}


async def test_create_task_passes_custom_fields_through_by_id():
    api = FakeAPI()
    await tools.handle_task_tool(
        "weeek_create_task",
        {"title": "T", "project_id": 1, "custom_fields": {LINK: "https://x"}},
        api,
    )
    _, body = api.calls[-1]
    assert body["customFields"] == {LINK: "https://x"}


async def test_update_task_reports_a_field_the_project_does_not_use():
    api = FakeAPI(drops={MOCKUP})
    with pytest.raises(ValueError, match="Макет"):
        await tools.handle_task_tool(
            "weeek_update_task",
            {"task_id": 42, "custom_fields": {"Макет": "https://x"}},
            api,
        )


async def test_create_task_reports_a_dropped_field_and_names_the_new_task():
    api = FakeAPI(drops={LINK})
    with pytest.raises(ValueError) as exc:
        await tools.handle_task_tool(
            "weeek_create_task",
            {"title": "T", "project_id": 1, "custom_fields": {LINK: "https://x"}},
            api,
        )
    message = str(exc.value)
    assert LINK in message
    assert "Task 7 was created" in message  # so the caller fixes it instead of creating a duplicate
    assert "weeek_update_task" in message


async def test_create_task_rejects_field_names_without_blaming_the_project():
    api = FakeAPI()
    with pytest.raises(ValueError) as exc:
        await tools.handle_task_tool(
            "weeek_create_task",
            {"title": "T", "project_id": 1, "custom_fields": {"Ссылка на фичу": "https://x"}},
            api,
        )
    message = str(exc.value)
    assert "keyed by field id" in message
    assert "Task 7 was created" in message


def test_a_response_without_fields_is_not_read_as_a_lost_write():
    # Weeek echoes the stored fields today; a response that doesn't say anything
    # about them must not be reported as "nothing was saved".
    assert tools.dropped_custom_fields({"success": True, "task": {"id": 9}}, {LINK: "https://x"}) == []
    assert tools.dropped_custom_fields({"success": True, "task": None}, {LINK: "https://x"}) == []


async def test_create_task_survives_a_response_without_the_task():
    class Terse(FakeAPI):
        async def create_task(self, body):
            self.calls.append(("create_task", body))
            return {"success": True}

    await tools.handle_task_tool(
        "weeek_create_task",
        {"title": "T", "project_id": 1, "custom_fields": {LINK: "https://x"}},
        Terse(),
    )


async def test_clearing_a_field_is_not_mistaken_for_a_dropped_write():
    api = FakeAPI()
    await tools.handle_task_tool("weeek_update_task", {"task_id": 42, "custom_fields": {"Макет": None}}, api)
    _, _, body = api.calls[-1]
    assert body["customFields"] == {MOCKUP: None}


async def test_list_custom_fields_reads_them_off_a_task():
    api = FakeAPI(tasks=[{"id": 1, "customFields": FIELDS}])
    result = await tools.handle_task_tool("weeek_list_custom_fields", {"project_id": 3}, api)
    assert result[0] == {"id": LINK, "name": "Ссылка на фичу", "type": "link"}
    assert result[1]["options"] == [{"id": EPIC, "name": "Эпик"}]


async def test_list_custom_fields_on_an_empty_project():
    assert await tools.handle_task_tool("weeek_list_custom_fields", {"project_id": 3}, FakeAPI()) == []
