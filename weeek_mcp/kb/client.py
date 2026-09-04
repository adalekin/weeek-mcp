"""Knowledge base client over Weeek's internal JSON API.

Weeek has no public KB API, but the web app talks to a stable internal API at
``api.weeek.net`` authenticated by session cookies. We fetch KB data with httpx
using cookies from a saved Playwright login (see ``session.py``); the browser is
only launched to (re)acquire cookies when they are missing or expired.

Endpoints (base ``{internal_api_base}/ws/{workspace_id}``):
  GET /kb/articles/search?search=&offset=0&limit=&isTrashed=0  -> flat article list
  GET /kb/articles/{id}                                        -> article + content
  POST /kb/articles/{id}/avatar {objectType, objectId}         -> set the document icon
  DELETE /kb/articles/{id}/avatar                              -> clear the icon
  GET /tm/tasks/{id}/comments                                  -> task comments
  POST /tm/tasks/{id}/comments {content}                       -> add one
  PUT /tm/tasks/{id}/comments/{commentId} {content}            -> rewrite one
  DELETE /tm/tasks/{id}/comments/{commentId}                   -> remove one

Task comments live here rather than in the client for the public REST API because the public
API has no route for them at all (``/tm/tasks/{id}/comments`` and every neighbouring spelling
answer 404 there).

Reference data for icons lives outside the workspace tree, at
``{internal_api_base}/app/avatars`` (colors, emoji groups, built-in icons).

Note: ``parentId`` in the create/update article body is silently ignored by the
API (confirmed by network capture of the web app) — nesting is a separate write,
``PATCH /kb/hierarchy`` with ``{targetId, placeId, direction: "into"}``. The
document icon behaves the same way: avatar fields in the article body are
swallowed, and only the ``/avatar`` subresource actually writes it.

Public surface (stable for callers):
    await kb.list_documents()      -> list[KBDocument]
    await kb.read_document(doc_id) -> str  (markdown)
    await kb.search(query)         -> list[KBDocument]
    await kb.set_icon(doc_id, "🚀")-> str | None  (label of the icon now set)
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from ..config import Config
from ..logging_util import make_logger
from . import collab
from .prosemirror import markdown_to_doc, to_markdown
from .session import KBAuthError, automated_login, load_cookies_into
from .tables import (
    MeasuredTable,
    carry_over_plan,
    ceiling,
    read_tables,
    resolve_columns,
    size_new_tables,
    widths_plan,
    write_columns,
)

_PAGE_LIMIT = 200


class KBError(RuntimeError):
    pass


@dataclass(frozen=True)
class KBDocument:
    id: str
    title: str
    path: str  # breadcrumb trail, e.g. "Технологии / Провайдеры"
    icon: str | None = None  # emoji character or built-in icon name, when set
    crumbs: tuple[str, ...] = ()  # breadcrumb names, self last; folders are crumbs[:-1]


_VARIATION_SELECTOR = "\ufe0f"


def _emoji_key(text: str) -> str:
    """Codepoint key for an emoji, e.g. "🚀" -> "1f680".

    The variation selector is dropped so that "☄️" and "☄" resolve to the same
    catalog entry — Weeek stores some emoji with it and some without.
    """
    return " ".join(f"{ord(c):04x}" for c in text if c != _VARIATION_SELECTOR)


def _emoji_key_from_catalog(unicode_spec: str) -> str:
    """Same key, from the catalog's notation: "U+2604 U+FE0F" -> "2604"."""
    points = [int(p[2:], 16) for p in unicode_spec.split() if p.upper().startswith("U+")]
    return " ".join(f"{p:04x}" for p in points if p != 0xFE0F)


def _emoji_char(unicode_spec: str) -> str:
    points = [int(p[2:], 16) for p in unicode_spec.split() if p.upper().startswith("U+")]
    return "".join(chr(p) for p in points)


_UNSAFE = re.compile(r'[/\\:*?"<>|]+')


def _safe_name(name: str) -> str:
    """Filesystem-safe file/folder name derived from a document title."""
    cleaned = _UNSAFE.sub("-", name).strip().strip(".")
    return (cleaned or "untitled")[:120]


# Our front matter stamp. It sits in the first lines, so a short read is enough
# to tell our own file from one the user dropped into the folder by hand.
_EXPORT_STAMP = re.compile(r"^weeek_id: \d+$", re.M)
_STAMP_PROBE = 400


def _prune_export(root: Path, kept: set[Path]) -> list[str]:
    """Drop exported files the current export no longer accounts for.

    A renamed or moved document writes itself under a new name and leaves the old
    file behind; a deleted one leaves its file forever. Both make the folder drift
    away from the knowledge base it is supposed to mirror.

    Only files carrying our own front matter are candidates. Anything else in the
    folder belongs to the user and is left alone, and so is everything under a
    hidden folder: a snapshot of an earlier export kept in ``.backup/`` is full of
    our own front matter, and sweeping it would be exactly the data loss this
    function exists to avoid.
    """
    removed: list[str] = []
    for path in sorted(root.rglob("*.md")):
        if path in kept or not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue  # hidden: the user's, not part of the mirror
        try:
            head = path.read_text(errors="replace")[:_STAMP_PROBE]
        except OSError:
            continue
        if not _EXPORT_STAMP.search(head):
            continue  # not ours to delete
        path.unlink()
        removed.append(str(path))

    # Deepest first, so a folder emptied by the loop above can go too.
    for folder in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if any(part.startswith(".") for part in folder.relative_to(root).parts):
            continue
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    return removed


def _breadcrumb_parts(article: dict) -> list[str]:
    """Breadcrumb names in order, self last. Empty crumbs are dropped."""
    crumbs = article.get("breadcrumbs") or []
    return [c["name"] for c in crumbs if isinstance(c, dict) and c.get("name")]


def _breadcrumb(article: dict) -> str:
    return " / ".join(_breadcrumb_parts(article))


class WeeekKB:
    def __init__(self, config: Config):
        self._cfg = config
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._ws: str | None = config.workspace_id
        self._cache: list[KBDocument] | None = None
        self._cache_ts: float = 0.0
        self._icons: dict[str, str] = {}  # lowercased icon name -> id
        self._emoji: dict[str, str] = {}  # codepoint key -> id
        self._labels: dict[str, str] = {}  # avatar id -> emoji character / icon name
        self._catalog_loaded = False
        self._log = make_logger(config.log_path, "weeek-mcp/kb")

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
        t0 = time.monotonic()
        self._log("session refresh: starting automated login")
        try:
            await automated_login(self._cfg)
        except KBAuthError as exc:
            self._log(f"session refresh: failed after {time.monotonic() - t0:.1f}s: {exc}")
            raise KBError(str(exc)) from exc
        self._log(f"session refresh: done in {time.monotonic() - t0:.1f}s")
        # Rebuild the client so fresh cookies are loaded.
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._ensure_client()

    async def _get(self, path: str, *, params: dict | None = None, _retry: bool = True):
        client = self._ensure_client()
        t0 = time.monotonic()
        try:
            resp = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            self._log(f"GET {path} failed after {time.monotonic() - t0:.1f}s: {exc}")
            raise KBError(f"Request to {path} failed: {exc}") from exc

        if resp.status_code in (401, 403) and _retry:
            self._log(f"GET {path} got {resp.status_code}, refreshing session and retrying")
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

    @property
    def config(self) -> Config:
        return self._cfg

    async def workspace(self) -> str:
        """The workspace these calls run against (auto-detected when not configured)."""
        return await self._workspace()

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
        await self._load_catalog_quietly()  # to name the icons the articles carry
        return [
            KBDocument(
                id=str(a["id"]),
                title=a.get("name") or str(a["id"]),
                path=_breadcrumb(a),
                icon=self._icon_label(a.get("avatar")),
                crumbs=tuple(_breadcrumb_parts(a)),
            )
            for a in articles
        ]

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

    async def _patch(self, path: str, payload: dict, *, _retry: bool = True):
        client = self._ensure_client()
        resp = await client.patch(path, json=payload)
        if resp.status_code in (401, 403) and _retry:
            await self._refresh_session()
            return await self._patch(path, payload, _retry=False)
        if resp.status_code >= 400:
            raise KBError(f"Internal API {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    async def _delete(self, path: str, *, _retry: bool = True):
        client = self._ensure_client()
        resp = await client.delete(path)
        if resp.status_code in (401, 403) and _retry:
            await self._refresh_session()
            return await self._delete(path, _retry=False)
        if resp.status_code >= 400:
            raise KBError(f"Internal API {resp.status_code} for {path}: {resp.text[:200]}")
        return resp.json()

    def _invalidate_cache(self) -> None:
        self._cache = None

    async def _set_parent(self, doc_id: str, parent_id: str | int) -> None:
        """Nest a document under another one.

        ``parentId`` in the article create/update body is silently ignored by the
        API — this is the only endpoint that actually reparents a document.
        """
        ws = await self._workspace()
        await self._patch(
            f"/ws/{ws}/kb/hierarchy",
            {"targetId": int(doc_id), "placeId": int(parent_id), "direction": "into"},
        )

    # ------------------------------------------------------------- icons
    async def _load_catalog(self) -> None:
        """Fetch and index Weeek's avatar catalog (built-in icons + emoji).

        Static reference data shared by the whole app, so one fetch per process.
        """
        if self._catalog_loaded:
            return
        data = (await self._get("/app/avatars")).get("data") or {}
        for icon in data.get("icons") or []:
            name = icon.get("name") or ""
            self._icons.setdefault(name.casefold(), icon["id"])
            self._labels[icon["id"]] = name
        for group in data.get("emojiGroups") or []:
            for emoji in group.get("emojis") or []:
                spec = emoji.get("unicode") or ""
                if not spec:
                    continue
                self._emoji.setdefault(_emoji_key_from_catalog(spec), emoji["id"])
                self._labels[emoji["id"]] = _emoji_char(spec)
        self._catalog_loaded = True

    async def _load_catalog_quietly(self) -> None:
        """Catalog load for read paths — a hiccup here must not break listing.

        Broad on purpose: reference data for icon names is not worth failing a
        document listing over, whatever went wrong fetching or parsing it.
        """
        try:
            await self._load_catalog()
        except Exception as exc:  # noqa: BLE001
            self._log(f"icon catalog unavailable: {exc}")

    def _icon_label(self, avatar: dict | None) -> str | None:
        """The icon of an article as an emoji character or a built-in icon name."""
        if not isinstance(avatar, dict) or avatar.get("objectType") not in ("icon", "emoji"):
            return None
        return self._labels.get(avatar.get("objectId") or "")

    async def icon_names(self) -> list[str]:
        """Names of the built-in icons, as accepted by ``set_icon``."""
        await self._load_catalog()
        return sorted((self._labels[i] for i in self._icons.values()), key=str.casefold)

    async def _resolve_icon(self, icon: str) -> dict:
        await self._load_catalog()
        spec = icon.strip()
        emoji_id = self._emoji.get(_emoji_key(spec))
        if emoji_id:
            return {"objectType": "emoji", "objectId": emoji_id}
        icon_id = self._icons.get(spec.casefold())
        if icon_id:
            return {"objectType": "icon", "objectId": icon_id}
        raise KBError(
            f"Unknown icon {icon!r}. Pass a single emoji character (e.g. 🚀) or one of: "
            + ", ".join(await self.icon_names())
        )

    async def _write_icon(self, doc_id: str, avatar: dict) -> str | None:
        ws = await self._workspace()
        await self._post(f"/ws/{ws}/kb/articles/{doc_id}/avatar", avatar)
        self._invalidate_cache()
        return self._labels.get(avatar["objectId"])

    async def set_icon(self, doc_id: str, icon: str | None) -> str | None:
        """Set a document's icon to an emoji or a built-in icon; empty clears it.

        Returns the label now shown for the document (None once cleared).
        """
        if not (icon or "").strip():
            ws = await self._workspace()
            await self._delete(f"/ws/{ws}/kb/articles/{doc_id}/avatar")
            self._invalidate_cache()
            return None
        return await self._write_icon(doc_id, await self._resolve_icon(icon or ""))

    async def create_document(
        self,
        title: str,
        *,
        markdown: str | None = None,
        parent_id: str | int | None = None,
        icon: str | None = None,
    ) -> KBDocument:
        ws = await self._workspace()
        # Resolve the icon first: an unknown one must fail before a document exists,
        # not leave a stray document behind for the caller to notice and clean up.
        avatar = await self._resolve_icon(icon) if icon else None

        body: dict = {"name": title, "content": size_new_tables(markdown_to_doc(markdown)) if markdown else {}}
        data = await self._post(f"/ws/{ws}/kb/articles", body)
        art = data.get("article") or {}
        doc_id = str(art.get("id"))

        label = await self._write_icon(doc_id, avatar) if avatar is not None else None

        if parent_id is not None:
            await self._set_parent(doc_id, parent_id)
            # Re-fetch: the create response has no breadcrumbs, and now they've changed.
            fresh = await self._get(f"/ws/{ws}/kb/articles/{doc_id}")
            art = fresh.get("article") or art

        self._invalidate_cache()
        return KBDocument(
            id=doc_id,
            title=art.get("name") or title,
            path=_breadcrumb(art),
            icon=label,
            crumbs=tuple(_breadcrumb_parts(art)),
        )

    async def rename_document(self, doc_id: str, title: str) -> None:
        ws = await self._workspace()
        await self._put(f"/ws/{ws}/kb/articles/{doc_id}", {"name": title})
        self._invalidate_cache()

    async def move_document(self, doc_id: str, parent_id: str | int) -> None:
        """Nest an existing document under another one (or move it elsewhere)."""
        await self._set_parent(doc_id, parent_id)
        self._invalidate_cache()

    async def _document_content(self, doc_id: str) -> dict:
        """The raw ProseMirror document behind an article."""
        ws = await self._workspace()
        data = await self._get(f"/ws/{ws}/kb/articles/{doc_id}")
        article = data.get("article")
        if not article:
            raise KBError(f"Document {doc_id!r} not found.")
        return (article.get("content") or {}).get("data") or {}

    async def _collab(self, kind: str, item_id: str) -> tuple[str, str, str]:
        """Where an editor's collaborative channel is, and the ticket to enter it.

        ``kind`` is "article" (a KB document) or "task" (a task description) —
        the same protocol, two collections. The token is minted per socket;
        Weeek takes whatever socket id it is given, so ours identifies this
        client rather than a Pusher connection.
        """
        ws = await self._workspace()
        collection = "kb/articles" if kind == "article" else "tm/tasks"
        data = await self._post(
            f"/ws/{ws}/{collection}/{item_id}/content/token", {"socketId": f"weeek-mcp.{uuid.uuid4().hex[:12]}"}
        )
        token = data.get("token")
        if not token:
            raise KBError(f"Weeek did not issue an editing ticket for {kind} {item_id!r}.")
        return (
            collab.endpoint(self._cfg.collab_base, ws, kind, str(item_id)),
            collab.document_name(ws, kind, str(item_id)),
            str(token),
        )

    async def update_task_description(self, task_id: int | str, markdown: str) -> None:
        """Replace a task's description — the same collaborative channel a body uses.

        ``PUT /tm/tasks/{id}`` has no description field (only create does): like
        KB bodies, descriptions sync through the editor's Y.Doc.
        """
        url, name, token = await self._collab("task", str(task_id))
        # An empty description is a real request — clear the field — so it is
        # written as the empty paragraph the editor holds, not refused as a
        # body that would blank a document by accident.
        doc = markdown_to_doc(markdown) if markdown.strip() else {"type": "doc", "content": [{"type": "paragraph"}]}
        try:
            await collab.apply(url, name, token, lambda fragment: collab.write_body(fragment, doc))
        except collab.CollabError as exc:
            raise KBError(str(exc)) from exc

    async def update_content(
        self,
        doc_id: str,
        markdown: str,
        table_widths: list[list[int] | None] | None = None,
        width: str = "text",
    ) -> None:
        """Replace a document's body in place, over the collaborative channel.

        The body is written straight into the document's Y.Doc — the same place
        the editor writes — because REST only serves a snapshot of it and drops
        any content handed to it.

        Column widths are not part of the Markdown, so they are read from the old
        body and carried onto the new one; tables whose shape changed, and new
        ones, are fitted to ``width`` instead — "text" for the column of text,
        "page" to overhang it and span the document area.
        """
        before = read_tables(await self._document_content(doc_id))
        doc = markdown_to_doc(markdown)
        after = read_tables(doc)
        plan = carry_over_plan(before, after) if after else None
        if table_widths is not None and plan is not None:
            # Explicit widths beat carrying the old ones over. They ride with the
            # body because that is the write this document actually takes: setting
            # them afterwards on an existing table is what Weeek refuses to sync.
            if len(table_widths) != len(plan):
                raise KBError(
                    f"The new body has {len(plan)} table(s) but {len(table_widths)} width list(s) "
                    "were given. Pass one entry per table, or null to keep what a table had."
                )
            for i, want in enumerate(table_widths):
                if want is None:
                    continue
                if len(want) != after[i].columns:
                    raise KBError(f"Table {i} has {after[i].columns} column(s), got {len(want)} width(s).")
                plan[i] = {"mode": "widths", "widths": list(want)}
        if plan is not None:
            measured = [MeasuredTable(columns=t.columns, raw_columns=None) for t in after]
            write_columns(doc, resolve_columns(measured, plan, ceiling(width)))
        wanted = to_markdown(doc)

        async def settled() -> bool:
            """Whether Weeek itself now serves the body that was written.

            Only the body. Widths are checked after: a width that did not take is
            worth saying so without pretending the body was lost.
            """
            return to_markdown(await self._document_content(doc_id)) == wanted

        url, name, token = await self._collab("article", str(doc_id))
        try:
            await collab.apply(url, name, token, lambda fragment: collab.write_body(fragment, doc), settled=settled)
        except collab.CollabError as exc:
            raise KBError(str(exc)) from exc
        if table_widths is not None:
            stored = [t.widths for t in read_tables(await self._document_content(doc_id))]
            missed = [
                i
                for i, want in enumerate(table_widths)
                if want is not None and (i >= len(stored) or stored[i] != list(want))
            ]
            if missed:
                raise KBError(
                    f"The body was written, but table(s) {missed} kept their old widths. "
                    "The body is safe — only the sizing did not take."
                )

    async def set_table_widths(
        self,
        doc_id: str,
        *,
        table_index: int | None = None,
        widths: list[int | None] | None = None,
        fit: bool = False,
        width: str = "text",
    ) -> dict:
        """Set column widths on a document's tables, leaving their content alone.

        Written straight into the document's Y.Doc, then read back and compared
        with what was asked for: a dropped sync has to surface as an error rather
        than as a success with nothing changed.
        """
        tables = read_tables(await self._document_content(doc_id))
        plan = widths_plan(tables, table_index, widths, fit=fit)
        expected: list[list[int] | None] = []

        def size(fragment) -> None:
            bodies = [collab.measure_body(body) for body in collab.table_bodies(fragment)]
            measured = [MeasuredTable(columns=columns, raw_columns=raw) for columns, raw in bodies]
            resolved = resolve_columns(measured, plan, ceiling(width))
            expected.extend([[e["width"] for e in entries] if entries else None for entries in resolved])
            for body, entries in zip(collab.table_bodies(fragment), resolved, strict=True):
                if entries is not None:
                    body.attributes["columns"] = json.dumps(entries)

        async def settled() -> bool:
            current = [t.widths for t in read_tables(await self._document_content(doc_id))]
            return all(want is None or (i < len(current) and current[i] == want) for i, want in enumerate(expected))

        url, name, token = await self._collab("article", str(doc_id))
        try:
            await collab.apply(url, name, token, size, settled=settled)
        except collab.CollabError as exc:
            raise KBError(str(exc)) from exc

        stored = [t.widths for t in read_tables(await self._document_content(doc_id))]
        for i, want in enumerate(expected):
            if want is not None and (i >= len(stored) or stored[i] != want):
                raise KBError(
                    f"Table {i} was set to {want} but Weeek now reports "
                    f"{stored[i] if i < len(stored) else 'no such table'}."
                )
        return {
            "tables": len(expected),
            "changed": sum(1 for want in expected if want is not None),
            "fitted_to": {"width": width, "pixels": ceiling(width)},
            "widths": stored,
        }

    async def export_documents(self, target_dir: str, *, query: str = "") -> dict:
        """Write knowledge base documents to a local folder as Markdown files.

        Mirrors the KB tree as subfolders and adds YAML front matter with the
        document id and path. Intended for folder-based integrations (e.g. adding
        the folder to a Claude Desktop project's Context), which take file content
        rather than links.

        The folder is a mirror, not an overlay: a full export also drops files it
        no longer accounts for, so a renamed, moved or deleted document does not
        leave a stale copy behind. Only files carrying our own front matter are
        touched. A filtered export (``query``) writes a subset and prunes nothing.

        ``target_dir`` must be absolute. The server runs with its own checkout as
        the working directory, so a relative path lands inside the repository.
        """
        root = Path(target_dir).expanduser()
        if not root.is_absolute():
            raise KBError(
                f"target_dir must be an absolute path, got {target_dir!r}. "
                "A relative path resolves against the server's working directory, "
                "which is the repository checkout, and mkdir(parents=True) would "
                "silently create the tree there."
            )

        docs = await self.search(query) if query.strip() else await self.list_documents(force=True)
        root.mkdir(parents=True, exist_ok=True)

        written: list[str] = []
        kept: set[Path] = set()
        for d in docs:
            body = await self.read_document(d.id)
            parts = list(d.crumbs)
            if parts and parts[-1] == d.title:
                parts = parts[:-1]  # last crumb is the document itself
            folder = root.joinpath(*[_safe_name(p) for p in parts]) if parts else root
            folder.mkdir(parents=True, exist_ok=True)

            path = folder / f"{_safe_name(d.title)}.md"
            if path.exists() and f"weeek_id: {d.id}\n" not in path.read_text():
                path = folder / f"{_safe_name(d.title)}-{d.id}.md"  # title collision

            front = f"---\ntitle: {d.title}\nweeek_id: {d.id}\nweeek_path: {d.path}\n---\n\n"
            path.write_text(front + body)
            written.append(str(path))
            kept.add(path)

        # A filtered export is a subset by design, so it is not evidence that the
        # rest is stale.
        removed = _prune_export(root, kept) if not query.strip() else []

        return {
            "exported": len(written),
            "removed": len(removed),
            "directory": str(root),
            "files": written,
            "pruned": removed,
        }

    async def delete_document(self, doc_id: str, *, permanent: bool = False) -> None:
        ws = await self._workspace()
        await self._delete(f"/ws/{ws}/kb/articles/{doc_id}/trash")  # move to trash
        if permanent:
            await self._delete(f"/ws/{ws}/kb/articles/{doc_id}")
        self._invalidate_cache()

    # ------------------------------------------------------------- task comments
    async def list_task_comments(self, task_id: int) -> list[dict]:
        ws = await self._workspace()
        data = await self._get(f"/ws/{ws}/tm/tasks/{task_id}/comments")
        return data.get("comments") or []

    async def add_task_comment(self, task_id: int, markdown: str) -> dict:
        """Post a comment written as Markdown.

        The body is ``{"content": <ProseMirror doc>}`` and the server stores that doc under
        ``content.data``, filling in ``version`` and ``mentions`` itself (established by posting
        to a live task and reading the response back). Sending the wrapper Weeek returns instead,
        ``{"content": {"data": ...}}``, answers 500 after writing a comment nested one level too
        deep, so the doc goes in bare.
        """
        ws = await self._workspace()
        data = await self._post(
            f"/ws/{ws}/tm/tasks/{task_id}/comments",
            {"content": markdown_to_doc(markdown)},
        )
        return cast(dict, data.get("comment") or data)

    async def update_task_comment(self, task_id: int, comment_id: int, markdown: str) -> dict:
        """Rewrite a comment in place, so the thread keeps one entry instead of gaining a second."""
        ws = await self._workspace()
        data = await self._put(
            f"/ws/{ws}/tm/tasks/{task_id}/comments/{comment_id}",
            {"content": markdown_to_doc(markdown)},
        )
        return cast(dict, data.get("comment") or data)

    async def delete_task_comment(self, task_id: int, comment_id: int) -> None:
        """Remove a comment. Weeek has no trash for these — it is gone."""
        ws = await self._workspace()
        await self._delete(f"/ws/{ws}/tm/tasks/{task_id}/comments/{comment_id}")
