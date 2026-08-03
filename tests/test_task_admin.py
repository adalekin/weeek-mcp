"""Dispatch for the task subresources and the admin CRUD tools (no live backend)."""

import pytest

from weeek_mcp import tools


class RecordingAPI:
    """Records the HTTP calls the handlers make, the way WeeekAPI would issue them."""

    def __init__(self):
        self.calls = []

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs.get("json"), kwargs.get("params")))
        return {"success": True}

    def __getattr__(self, name):
        # Everything else routes through the real WeeekAPI methods bound to this double.
        from weeek_mcp.weeek_api import WeeekAPI

        attr = getattr(WeeekAPI, name)
        return attr.__get__(self, RecordingAPI)


async def call(tool, args):
    api = RecordingAPI()
    await tools.handle_task_tool(tool, args, api)
    return api.calls


# ------------------------------------------------------------------- task subresources
async def test_parent_keeps_a_null_so_a_subtask_can_be_detached():
    assert await call("weeek_set_task_parent", {"task_id": 5, "parent_id": None}) == [
        ("POST", "/tm/tasks/5/parent", {"parentId": None}, None)
    ]


async def test_parent_passes_sibling_placement():
    calls = await call("weeek_set_task_parent", {"task_id": 5, "parent_id": 9, "after": 7})
    assert calls[0][2] == {"parentId": 9, "after": 7}


async def test_moving_a_task_can_do_board_then_column():
    calls = await call("weeek_move_task", {"task_id": 5, "board_id": 2, "board_column_id": 3})
    assert [(c[0], c[1]) for c in calls] == [
        ("POST", "/tm/tasks/5/board"),
        ("POST", "/tm/tasks/5/board-column"),
    ]


async def test_moving_a_task_nowhere_is_an_error():
    with pytest.raises(ValueError, match="board_column_id"):
        await call("weeek_move_task", {"task_id": 5})


async def test_locations_add_and_remove():
    assert (await call("weeek_add_task_to_project", {"task_id": 5, "project_id": 2}))[0][2] == {"projectId": 2}
    assert (await call("weeek_remove_task_from_project", {"task_id": 5, "project_id": 2}))[0] == (
        "DELETE",
        "/tm/tasks/5/locations",
        {"projectId": 2},
        None,
    )


async def test_watchers_use_their_own_endpoint():
    calls = await call("weeek_set_watchers", {"task_id": 5, "watchers": ["u1"]})
    assert calls[0] == ("POST", "/tm/tasks/5/watchers", {"watchers": ["u1"]}, None)


@pytest.mark.parametrize(("action", "path"), [("start", "start-timer"), ("stop", "stop-timer")])
async def test_timer_actions(action, path):
    calls = await call("weeek_task_timer", {"task_id": 5, "action": action})
    assert calls[0][1] == f"/tm/tasks/5/{path}"


async def test_time_entry_create_sends_the_required_quartet():
    calls = await call(
        "weeek_manage_time_entry",
        {"action": "create", "task_id": 5, "user_id": "u1", "date": "2026-08-03", "duration": 30},
    )
    assert calls[0] == (
        "POST",
        "/tm/tasks/5/time-entries",
        {"userId": "u1", "date": "2026-08-03", "duration": 30, "isOvertime": False},
        None,
    )


async def test_time_entry_update_needs_an_entry_id():
    with pytest.raises(ValueError, match="entry_id"):
        await call(
            "weeek_manage_time_entry",
            {"action": "update", "task_id": 5, "user_id": "u1", "date": "2026-08-03", "duration": 30},
        )


async def test_uploading_a_missing_file_fails_before_the_request():
    with pytest.raises(ValueError, match="No such file"):
        await call("weeek_upload_attachment", {"task_id": 5, "paths": ["/nope/missing.png"]})


# ------------------------------------------------------------------------- admin CRUD
async def test_tags_list_and_create():
    assert (await call("weeek_manage_tags", {"action": "list"}))[0][:2] == ("GET", "/ws/tags")
    assert (await call("weeek_manage_tags", {"action": "create", "title": "срочно"}))[0][2] == {"title": "срочно"}


async def test_tag_update_requires_color_because_the_api_does():
    with pytest.raises(ValueError, match="color"):
        await call("weeek_manage_tags", {"action": "update", "tag_id": 1, "title": "x"})


@pytest.mark.parametrize(
    ("action", "method", "path"),
    [
        ("archive", "POST", "/tm/projects/3/archive"),
        ("unarchive", "POST", "/tm/projects/3/un-archive"),
        ("delete", "DELETE", "/tm/projects/3"),
    ],
)
async def test_project_actions(action, method, path):
    calls = await call("weeek_manage_projects", {"action": action, "project_id": 3})
    assert calls[0][:2] == (method, path)


async def test_creating_a_project_defaults_to_not_private():
    calls = await call("weeek_manage_projects", {"action": "create", "name": "P"})
    assert calls[0][2] == {"name": "P", "isPrivate": False}


async def test_board_move_keeps_null_meaning_top():
    calls = await call("weeek_manage_boards", {"action": "move", "board_id": 7, "upper_board_id": None})
    assert calls[0] == ("POST", "/tm/boards/7/move", {"upperBoardId": None}, None)


async def test_board_column_create_needs_a_board():
    with pytest.raises(ValueError, match="board_id"):
        await call("weeek_manage_board_columns", {"action": "create", "name": "Backlog"})


async def test_portfolio_list_drops_empty_filters():
    calls = await call("weeek_manage_portfolios", {"action": "list", "search": "инфра"})
    assert calls[0][3] == {"search": "инфра"}


# ---------------------------------------------------------------------- custom fields
@pytest.mark.parametrize(
    ("scope", "scope_id", "base"),
    [
        ("global", None, "/tm/custom-fields"),
        ("project", 2, "/tm/projects/2/custom-fields"),
        ("board", 7, "/tm/boards/7/custom-fields"),
    ],
)
async def test_custom_field_scopes_map_to_their_endpoints(scope, scope_id, base):
    calls = await call(
        "weeek_manage_custom_fields",
        {"action": "create", "scope": scope, "scope_id": scope_id, "name": "Макет", "type": "link"},
    )
    assert calls[0][:2] == ("POST", base)
    assert calls[0][2] == {"name": "Макет", "type": "link"}


async def test_a_scoped_field_needs_its_scope_id():
    with pytest.raises(ValueError, match="scope_id"):
        await call("weeek_manage_custom_fields", {"action": "create", "scope": "project", "type": "text"})


async def test_option_endpoints_nest_under_the_field():
    calls = await call(
        "weeek_manage_custom_fields",
        {
            "action": "update_option",
            "scope": "project",
            "scope_id": 2,
            "field_id": "f1",
            "option_id": "o1",
            "name": "Эпик",
            "color": "purple",
        },
    )
    assert calls[0][:2] == ("PUT", "/tm/projects/2/custom-fields/f1/options/o1")


async def test_transfer_to_task_manager_takes_no_body():
    calls = await call(
        "weeek_manage_custom_fields",
        {"action": "transfer", "scope": "project", "scope_id": 2, "field_id": "f1", "target": "global"},
    )
    assert calls[0][:3] == ("POST", "/tm/projects/2/custom-fields/f1/transfer-to-task-manager", None)


class StatefulAPI(RecordingAPI):
    """Answers the reads the handlers do before a PUT, so carry-over can be tested."""

    def __init__(self, project=None, time_entry=None):
        super().__init__()
        self._project = project or {}
        self._time_entry = time_entry or {}

    async def get_project(self, project_id):
        self.calls.append(("GET", f"/tm/projects/{project_id}", None, None))
        return {"success": True, "project": self._project}

    async def get_task(self, task_id):
        self.calls.append(("GET", f"/tm/tasks/{task_id}", None, None))
        return {"success": True, "task": {"timeEntries": [self._time_entry]}}


async def test_renaming_a_project_keeps_it_private():
    api = StatefulAPI(project={"id": 3, "isPrivate": True})
    await tools.handle_task_tool(
        "weeek_manage_projects",
        {"action": "update", "project_id": 3, "name": "Новое имя", "color": "#35AAFF"},
        api,
    )
    put = [c for c in api.calls if c[0] == "PUT"][0]
    assert put[2]["isPrivate"] is True


async def test_an_explicit_privacy_change_still_wins():
    api = StatefulAPI(project={"id": 3, "isPrivate": True})
    await tools.handle_task_tool(
        "weeek_manage_projects",
        {"action": "update", "project_id": 3, "name": "N", "color": "#fff", "is_private": False},
        api,
    )
    put = [c for c in api.calls if c[0] == "PUT"][0]
    assert put[2]["isPrivate"] is False
    assert not [c for c in api.calls if c[0] == "GET"]  # no needless read


async def test_editing_a_time_entry_keeps_its_overtime_flag():
    api = StatefulAPI(time_entry={"id": "e1", "duration": 30, "isOvertime": True})
    await tools.handle_task_tool(
        "weeek_manage_time_entry",
        {"action": "update", "task_id": 5, "entry_id": "e1", "user_id": "u", "date": "2026-08-04", "duration": 45},
        api,
    )
    put = [c for c in api.calls if c[0] == "PUT"][0]
    assert put[2]["isOvertime"] is True


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("weeek_manage_tags", {"action": "rename", "tag_id": 1, "title": "x", "color": "#fff"}),
        ("weeek_manage_projects", {"action": "rename", "project_id": 1, "name": "x", "color": "#fff"}),
        ("weeek_manage_boards", {"action": "rename", "board_id": 1, "name": "x"}),
        ("weeek_manage_board_columns", {"action": "rename", "board_column_id": 1, "name": "x"}),
        ("weeek_manage_portfolios", {"action": "rename", "portfolio_id": 1, "name": "x"}),
        ("weeek_manage_custom_fields", {"action": "reorder", "field_id": "f", "option_id": "o", "after": "z"}),
        (
            "weeek_manage_time_entry",
            {"action": "log", "task_id": 1, "user_id": "u", "date": "2026-08-04", "duration": 5},
        ),
    ],
)
async def test_an_unknown_action_never_runs_a_neighbouring_one(tool, args):
    # Every argument the neighbouring action needs is present, so only an explicit
    # check stops "rename" from silently performing an update.
    api = RecordingAPI()
    with pytest.raises(ValueError, match="does not know action"):
        await tools.handle_task_tool(tool, args, api)
    assert api.calls == []


async def test_move_option_without_a_neighbour_is_refused_before_the_request():
    api = RecordingAPI()
    with pytest.raises(ValueError, match="after or before"):
        await tools.handle_task_tool(
            "weeek_manage_custom_fields",
            {"action": "move_option", "field_id": "f", "option_id": "o"},
            api,
        )
    assert api.calls == []


async def test_transfer_to_a_board_needs_the_target_id():
    with pytest.raises(ValueError, match="target_id"):
        await call(
            "weeek_manage_custom_fields",
            {"action": "transfer", "scope": "global", "field_id": "f1", "target": "board"},
        )
