"""Weeek MCP server (stdio).

Exposes:
  * task-management tools backed by the Weeek public REST API
  * knowledge base tools + MCP Resources backed by Playwright

Capabilities are advertised based on configuration: task tools require an API
token; knowledge base tools/resources require login credentials or a cached
session. This keeps the tool list clean for whatever the user has set up.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

from . import __version__
from .config import Config
from .kb.client import KBError, WeeekKB
from .tools import (
    ALL_TOOLS,
    KB_TOOL_NAMES,
    KB_TOOLS,
    TASK_TOOL_NAMES,
    TASK_TOOLS,
    handle_kb_tool,
    handle_task_tool,
    kb_doc_id_from_uri,
    kb_uri,
)
from .weeek_api import WeeekAPI, WeeekAPIError


def _log(msg: str) -> None:
    print(f"[weeek-mcp] {msg}", file=sys.stderr, flush=True)


class WeeekServer:
    def __init__(self, config: Config):
        self.cfg = config
        self.server: Server = Server("weeek-mcp", version=__version__)
        self._api: WeeekAPI | None = None
        self._kb: WeeekKB | None = None
        self.kb_available = config.has_kb_credentials or config.storage_state_path.exists()
        self._register()

    # ------------------------------------------------------------- lazy deps
    def _get_api(self) -> WeeekAPI:
        if not self.cfg.has_api:
            raise ValueError("Task tools require WEEEK_API_TOKEN. Set it in the environment.")
        if self._api is None:
            self._api = WeeekAPI(self.cfg.api_token or "", self.cfg.api_base)
        return self._api

    def _get_kb(self) -> WeeekKB:
        if self._kb is None:
            self._kb = WeeekKB(self.cfg)
        return self._kb

    # ------------------------------------------------------------- handlers
    def _register(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[types.Tool]:
            tools: list[types.Tool] = []
            if self.cfg.has_api:
                tools += TASK_TOOLS
            if self.kb_available:
                tools += KB_TOOLS
            return tools or ALL_TOOLS  # advertise everything if nothing is configured yet

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.ContentBlock]:
            args = arguments or {}
            try:
                if name in TASK_TOOL_NAMES:
                    result = await handle_task_tool(name, args, self._get_api())
                elif name in KB_TOOL_NAMES:
                    result = await handle_kb_tool(name, args, self._get_kb())
                else:
                    raise ValueError(f"Unknown tool: {name}")
            except WeeekAPIError as exc:
                raise ValueError(f"Weeek API error {exc.status_code}: {exc.body}") from exc
            except KBError as exc:
                raise ValueError(f"Knowledge base error: {exc}") from exc

            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
            return [types.TextContent(type="text", text=text)]

        @self.server.list_resources()
        async def list_resources() -> list[types.Resource]:
            if not self.kb_available:
                return []
            try:
                docs = await self._get_kb().list_documents()
            except Exception as exc:  # noqa: BLE001 — never let KB break the session
                _log(f"list_resources failed: {exc}")
                return []
            return [
                types.Resource(
                    uri=AnyUrl(kb_uri(d.id)),
                    name=d.title,
                    description=d.path or f"Weeek knowledge base document ({d.id})",
                    mimeType="text/markdown",
                )
                for d in docs
            ]

        @self.server.read_resource()
        async def read_resource(uri: Any) -> str:
            doc_id = kb_doc_id_from_uri(str(uri))
            try:
                return await self._get_kb().read_document(doc_id)
            except KBError as exc:
                raise ValueError(f"Knowledge base error: {exc}") from exc

    # ------------------------------------------------------------- run
    async def run(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            try:
                await self.server.run(
                    read_stream,
                    write_stream,
                    self.server.create_initialization_options(),
                )
            finally:
                if self._api is not None:
                    await self._api.aclose()
                if self._kb is not None:
                    await self._kb.aclose()


async def _amain() -> None:
    cfg = Config.from_env()
    if not cfg.has_api and not (cfg.has_kb_credentials or cfg.storage_state_path.exists()):
        _log("Warning: neither WEEEK_API_TOKEN nor KB credentials/session found. Tools will error until configured.")
    await WeeekServer(cfg).run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
