"""Async client for the Weeek public REST API (task manager domain).

Base URL and endpoint shapes were derived from the official OpenAPI spec published
at developers.weeek.net. All endpoints require a Bearer token.
"""

from __future__ import annotations

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

    # ------------------------------------------------------------------ projects
    async def list_projects(self) -> Any:
        return await self._request("GET", "/tm/projects")

    async def get_project(self, project_id: int) -> Any:
        return await self._request("GET", f"/tm/projects/{project_id}")

    # ------------------------------------------------------------------ boards
    async def list_boards(self, project_id: int) -> Any:
        return await self._request("GET", "/tm/boards", params={"projectId": project_id})

    async def list_board_columns(self, board_id: int | None = None) -> Any:
        return await self._request("GET", "/tm/board-columns", params=_clean({"boardId": board_id}))

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

    async def add_assignees(self, task_id: int, assignees: list[str]) -> Any:
        return await self._request("POST", f"/tm/tasks/{task_id}/assignees", json={"assignees": assignees})

    async def remove_assignees(self, task_id: int, assignees: list[str]) -> Any:
        return await self._request(
            "DELETE",
            f"/tm/tasks/{task_id}/assignees",
            json={"assignees": assignees},
        )
