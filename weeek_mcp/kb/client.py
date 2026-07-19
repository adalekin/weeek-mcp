"""Knowledge base client over Weeek's internal JSON API.

Weeek has no public KB API, but the web app talks to a stable internal API at
``api.weeek.net`` authenticated by session cookies. We fetch KB data with httpx
using cookies from a saved Playwright login (see ``session.py``); the browser is
only launched to (re)acquire cookies when they are missing or expired.

Endpoints (base ``{internal_api_base}/ws/{workspace_id}``):
  GET /kb/articles/search?search=&offset=0&limit=&isTrashed=0  -> flat article list
  GET /kb/articles/{id}                                        -> article + content

Public surface (stable for callers):
    await kb.list_documents()      -> list[KBDocument]
    await kb.read_document(doc_id) -> str  (markdown)
    await kb.search(query)         -> list[KBDocument]
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from ..config import Config
from .prosemirror import markdown_to_doc, markdown_to_html, to_markdown
from .session import KBAuthError, automated_login, load_cookies_into, replace_article_content

_PAGE_LIMIT = 200


class KBError(RuntimeError):
    pass


@dataclass(frozen=True)
class KBDocument:
    id: str
    title: str
    path: str  # breadcrumb trail, e.g. "Технологии / Провайдеры"


def _breadcrumb(article: dict) -> str:
    crumbs = article.get("breadcrumbs") or []
    names = [c.get("name", "") for c in crumbs if isinstance(c, dict)]
    return " / ".join(n for n in names if n)


class WeeekKB:
    def __init__(self, config: Config):
        self._cfg = config
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._ws: str | None = config.workspace_id
        self._cache: list[KBDocument] | None = None
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------- http/session
    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._cfg.internal_api_base,
                headers={
                    "Accept": "application/json",
                    "Origin": self._cfg.app_base,
                    "Referer": self._cfg.app_base + "/",
                },
                timeout=30.0,
            )
            load_cookies_into(self._client, self._cfg)
        return self._client

    async def _refresh_session(self) -> None:
        try:
            await automated_login(self._cfg)
        except KBAuthError as exc:
            raise KBError(str(exc)) from exc
        # Rebuild the client so fresh cookies are loaded.
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._ensure_client()

    async def _get(self, path: str, *, params: dict | None = None, _retry: bool = True):
        client = self._ensure_client()
        try:
            resp = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise KBError(f"Request to {path} failed: {exc}") from exc

        if resp.status_code in (401, 403) and _retry:
            await self._refresh_session()
            return await self._get(path, params=params, _retry=False)
        if resp.status_code >= 400:
            raise KBError(f"Internal API {resp.status_code} for {path}: {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise KBError(f"Internal API returned success=false for {path}")
        return data

    async def _workspace(self) -> str:
        if self._ws:
            return self._ws
        data = await self._get("/ws")
        workspaces = data.get("workspaces") or []
        if not workspaces:
            raise KBError("No workspaces available for this session.")
        self._ws = str(workspaces[0]["id"])
        return self._ws

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- documents
    async def _search_articles(self, query: str) -> list[KBDocument]:
        ws = await self._workspace()
        data = await self._get(
            f"/ws/{ws}/kb/articles/search",
            params={"search": query, "offset": 0, "limit": _PAGE_LIMIT, "isTrashed": 0},
        )
        articles = data.get("articles") or []
        return [KBDocument(id=str(a["id"]), title=a.get("name") or str(a["id"]), path=_breadcrumb(a)) for a in articles]

    async def list_documents(self, *, force: bool = False) -> list[KBDocument]:
        now = time.monotonic()
        if not force and self._cache is not None and now - self._cache_ts < self._cfg.kb_cache_ttl:
            return self._cache
        async with self._lock:
            docs = await self._search_articles("")
            self._cache = docs
            self._cache_ts = now
            return docs

    async def search(self, query: str) -> list[KBDocument]:
        if not query.strip():
            return await self.list_documents()
        return await self._search_articles(query.strip())

    async def read_document(self, doc_id: str) -> str:
        ws = await self._workspace()
        data = await self._get(f"/ws/{ws}/kb/articles/{doc_id}")
        article = data.get("article")
        if not article:
            raise KBError(f"Document {doc_id!r} not found.")
        title = article.get("name") or str(doc_id)
        body = to_markdown((article.get("content") or {}).get("data"))
        crumb = _breadcrumb(article)
        header = f"# {title}"
        parts = [header]
        if crumb and crumb != title:
            parts.append(f"*{crumb}*")
        if body:
            parts.append(body)
        return "\n\n".join(parts)

    # ------------------------------------------------------------- writes
    async def _post(self, path: str, payload: dict, *, _retry: bool = True):
        client = self._ensure_client()
        resp = await client.post(path, json=payload)
        if resp.status_code in (401, 403) and _retry:
            await self._refresh_session()
            return await self._post(path, payload, _retry=False)
        if resp.status_code >= 400:
            raise KBError(f"Internal API {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    async def _put(self, path: str, payload: dict, *, _retry: bool = True):
        client = self._ensure_client()
        resp = await client.put(path, json=payload)
        if resp.status_code in (401, 403) and _retry:
            await self._refresh_session()
            return await self._put(path, payload, _retry=False)
        if resp.status_code >= 400:
            raise KBError(f"Internal API {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    async def _delete(self, path: str):
        client = self._ensure_client()
        resp = await client.delete(path)
        if resp.status_code >= 400:
            raise KBError(f"Internal API {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    def _invalidate_cache(self) -> None:
        self._cache = None

    async def create_document(
        self, title: str, *, markdown: str | None = None, parent_id: str | int | None = None
    ) -> KBDocument:
        ws = await self._workspace()
        body: dict = {"name": title, "content": markdown_to_doc(markdown) if markdown else {}}
        if parent_id is not None:
            body["parentId"] = int(parent_id)
        data = await self._post(f"/ws/{ws}/kb/articles", body)
        art = data.get("article") or {}
        self._invalidate_cache()
        return KBDocument(id=str(art.get("id")), title=art.get("name") or title, path=_breadcrumb(art))

    async def rename_document(self, doc_id: str, title: str) -> None:
        ws = await self._workspace()
        await self._put(f"/ws/{ws}/kb/articles/{doc_id}", {"name": title})
        self._invalidate_cache()

    async def update_content(self, doc_id: str, markdown: str) -> None:
        """Replace a document's body in place via Weeek's editor (collaborative sync)."""
        ws = await self._workspace()
        # Ensure we have a live session before launching the browser editor.
        if not self._cfg.storage_state_path.exists():
            await self._refresh_session()
        await replace_article_content(self._cfg, ws, str(doc_id), markdown_to_html(markdown))

    async def delete_document(self, doc_id: str, *, permanent: bool = False) -> None:
        ws = await self._workspace()
        await self._delete(f"/ws/{ws}/kb/articles/{doc_id}/trash")  # move to trash
        if permanent:
            await self._delete(f"/ws/{ws}/kb/articles/{doc_id}")
        self._invalidate_cache()
