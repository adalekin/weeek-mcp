"""Async client for the Weeek public REST API (task manager domain).

Base URL and endpoint shapes were derived from the official OpenAPI spec published
at developers.weeek.net. All endpoints require a Bearer token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class WeeekAPIError(RuntimeError):
    """Raised when the Weeek API returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Weeek API error {status_code}: {body}")


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is None (Weeek treats absent vs null differently)."""
    return {k: v for k, v in params.items() if v is not None}


class WeeekAPI:
    def __init__(self, token: str, base_url: str, *, timeout: float = 30.0):
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise WeeekAPIError(resp.status_code, body)
        if resp.status_code == 204 or not resp.content:
            return {"success": True}
        return resp.json()

    # ------------------------------------------------------------------ workspace
    async def whoami(self) -> Any:
        return await self._request("GET", "/user/me")

    async def list_members(self) -> Any:
        return await self._request("GET", "/ws/members")

    # ------------------------------------------------------------------ tags
    async def list_tags(self) -> Any:
        return await self._request("GET", "/ws/tags")

    async def create_tag(self, title: str) -> Any:
        return await self._request("POST", "/ws/tags", json={"title": title})

    async def update_tag(self, tag_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/ws/tags/{tag_id}", json=body)

    async def delete_tag(self, tag_id: int) -> Any:
        return await self._request("DELETE", f"/ws/tags/{tag_id}")

    # ------------------------------------------------------------------ projects
    async def list_projects(self) -> Any:
        return await self._request("GET", "/tm/projects")

    async def get_project(self, project_id: int) -> Any:
        return await self._request("GET", f"/tm/projects/{project_id}")

    async def create_project(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/tm/projects", json=_clean(body))

    async def update_project(self, project_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/tm/projects/{project_id}", json=_clean(body))

    async def delete_project(self, project_id: int) -> Any:
        return await self._request("DELETE", f"/tm/projects/{project_id}")

    async def archive_project(self, project_id: int, *, archived: bool = True) -> Any:
        action = "archive" if archived else "un-archive"
        return await self._request("POST", f"/tm/projects/{project_id}/{action}")

    # ------------------------------------------------------------------ portfolios
    async def list_portfolios(self, **filters: Any) -> Any:
        return await self._request("GET", "/tm/portfolios", params=_clean(filters))

    async def create_portfolio(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/tm/portfolios", json=_clean(body))

    async def get_portfolio(self, portfolio_id: int) -> Any:
        return await self._request("GET", f"/tm/portfolios/{portfolio_id}")

    async def update_portfolio(self, portfolio_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/tm/portfolios/{portfolio_id}", json=body)

    async def delete_portfolio(self, portfolio_id: int) -> Any:
        return await self._request("DELETE", f"/tm/portfolios/{portfolio_id}")

    # ------------------------------------------------------------------ boards
    async def list_boards(self, project_id: int) -> Any:
        return await self._request("GET", "/tm/boards", params={"projectId": project_id})

    async def create_board(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/tm/boards", json=body)

    async def update_board(self, board_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/tm/boards/{board_id}", json=body)

    async def delete_board(self, board_id: int) -> Any:
        return await self._request("DELETE", f"/tm/boards/{board_id}")

    async def move_board(self, board_id: int, upper_board_id: int | None) -> Any:
        # null means "to the top", so this body is sent as-is rather than cleaned.
        return await self._request("POST", f"/tm/boards/{board_id}/move", json={"upperBoardId": upper_board_id})

    async def list_board_columns(self, board_id: int | None = None) -> Any:
        return await self._request("GET", "/tm/board-columns", params=_clean({"boardId": board_id}))

    async def create_board_column(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/tm/board-columns", json=body)

    async def update_board_column(self, column_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/tm/board-columns/{column_id}", json=body)

    async def delete_board_column(self, column_id: int) -> Any:
        return await self._request("DELETE", f"/tm/board-columns/{column_id}")

    async def move_board_column(self, column_id: int, upper_column_id: int | None) -> Any:
        return await self._request(
            "POST", f"/tm/board-columns/{column_id}/move", json={"upperBoardColumnId": upper_column_id}
        )

    # ------------------------------------------------------------------ tasks
    async def list_tasks(self, **filters: Any) -> Any:
        return await self._request("GET", "/tm/tasks", params=_clean(filters))

    async def get_task(self, task_id: int) -> Any:
        return await self._request("GET", f"/tm/tasks/{task_id}")

    async def create_task(self, body: dict[str, Any]) -> Any:
        return await self._request("POST", "/tm/tasks", json=_clean(body))

    async def update_task(self, task_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/tm/tasks/{task_id}", json=_clean(body))

    async def delete_task(self, task_id: int) -> Any:
        return await self._request("DELETE", f"/tm/tasks/{task_id}")

    async def complete_task(self, task_id: int) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/complete")

    async def uncomplete_task(self, task_id: int) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/un-complete")

    async def move_task_to_column(self, task_id: int, board_column_id: int) -> Any:
        return await self._request(
            "POST",
            f"/tm/tasks/{task_id}/board-column",
            json={"boardColumnId": board_column_id},
        )

    async def move_task_to_board(self, task_id: int, board_id: int) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/board", json={"boardId": board_id})

    async def add_assignees(self, task_id: int, assignees: list[str]) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/assignees", json={"assignees": assignees})

    async def remove_assignees(self, task_id: int, assignees: list[str]) -> Any:
        return await self._request(
            "DELETE",
            f"/tm/tasks/{task_id}/assignees",
            json={"assignees": assignees},
        )

    async def set_task_parent(self, task_id: int, body: dict[str, Any]) -> Any:
        # parentId: null detaches a subtask, so the body keeps its nulls.
        return await self._request("POST", f"/tm/tasks/{task_id}/parent", json=body)

    async def add_task_location(self, task_id: int, body: dict[str, Any]) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/locations", json=_clean(body))

    async def remove_task_location(self, task_id: int, project_id: int) -> Any:
        return await self._request("DELETE", f"/tm/tasks/{task_id}/locations", json={"projectId": project_id})

    async def add_watchers(self, task_id: int, watchers: list[str]) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/watchers", json={"watchers": watchers})

    async def remove_watchers(self, task_id: int, watchers: list[str]) -> Any:
        return await self._request("DELETE", f"/tm/tasks/{task_id}/watchers", json={"watchers": watchers})

    async def task_timer(self, task_id: int, *, running: bool) -> Any:
        action = "start-timer" if running else "stop-timer"
        return await self._request("POST", f"/tm/tasks/{task_id}/{action}")

    async def create_time_entry(self, task_id: int, body: dict[str, Any]) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/time-entries", json=body)

    async def update_time_entry(self, task_id: int, entry_id: int, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"/tm/tasks/{task_id}/time-entries/{entry_id}", json=body)

    async def delete_time_entry(self, task_id: int, entry_id: int) -> Any:
        return await self._request("DELETE", f"/tm/tasks/{task_id}/time-entries/{entry_id}")

    async def upload_attachments(self, task_id: int, paths: list[str]) -> Any:
        files = [("files[]", (Path(p).name, Path(p).read_bytes())) for p in paths]
        return await self._request("POST", f"/tm/tasks/{task_id}/attachments", files=files)

    async def get_attachment(self, file_id: str) -> Any:
        return await self._request("GET", f"/ws/attachments/{file_id}")

    # ------------------------------------------------------------------ custom fields
    def _custom_field_base(self, scope: str, scope_id: int | None) -> str:
        """Custom fields exist per board, per project, or workspace-wide ("global")."""
        if scope == "board":
            return f"/tm/boards/{scope_id}/custom-fields"
        if scope == "project":
            return f"/tm/projects/{scope_id}/custom-fields"
        return "/tm/custom-fields"

    async def list_global_custom_fields(self) -> Any:
        return await self._request("GET", "/tm/custom-fields")

    async def create_custom_field(self, scope: str, scope_id: int | None, body: dict[str, Any]) -> Any:
        return await self._request("POST", self._custom_field_base(scope, scope_id), json=_clean(body))

    async def update_custom_field(self, scope: str, scope_id: int | None, field_id: str, body: dict[str, Any]) -> Any:
        return await self._request("PUT", f"{self._custom_field_base(scope, scope_id)}/{field_id}", json=_clean(body))

    async def delete_custom_field(self, scope: str, scope_id: int | None, field_id: str) -> Any:
        return await self._request("DELETE", f"{self._custom_field_base(scope, scope_id)}/{field_id}")

    async def transfer_custom_field(
        self, scope: str, scope_id: int | None, field_id: str, target: str, target_id: int | None
    ) -> Any:
        base = f"{self._custom_field_base(scope, scope_id)}/{field_id}"
        if target == "board":
            return await self._request("POST", f"{base}/transfer-to-board", json={"boardId": target_id})
        if target == "project":
            return await self._request("POST", f"{base}/transfer-to-project", json={"projectId": target_id})
        return await self._request("POST", f"{base}/transfer-to-task-manager")

    async def create_custom_field_option(
        self, scope: str, scope_id: int | None, field_id: str, body: dict[str, Any]
    ) -> Any:
        return await self._request("POST", f"{self._custom_field_base(scope, scope_id)}/{field_id}/options", json=body)

    async def update_custom_field_option(
        self, scope: str, scope_id: int | None, field_id: str, option_id: str, body: dict[str, Any]
    ) -> Any:
        return await self._request(
            "PUT", f"{self._custom_field_base(scope, scope_id)}/{field_id}/options/{option_id}", json=body
        )

    async def delete_custom_field_option(self, scope: str, scope_id: int | None, field_id: str, option_id: str) -> Any:
        return await self._request(
            "DELETE", f"{self._custom_field_base(scope, scope_id)}/{field_id}/options/{option_id}"
        )

    async def move_custom_field_option(
        self, scope: str, scope_id: int | None, field_id: str, option_id: str, body: dict[str, Any]
    ) -> Any:
        return await self._request(
            "POST", f"{self._custom_field_base(scope, scope_id)}/{field_id}/options/{option_id}/move", json=_clean(body)
        )
