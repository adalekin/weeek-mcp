"""Weeek's collaborative channel, spoken directly.

Document bodies are not writable over REST: they live in a Yjs document held by
a Hocuspocus server, and REST only ever serves a snapshot of it. Until now the
way in was to drive Weeek's own editor in a headless browser, which works right
up until a document has enough history to matter — the roadmap document carries
3.8MB of it, takes ~20s to open, and the edit never reaches the server.

Speaking the protocol removes the browser from the write path entirely: connect,
authenticate, sync, replace the body, send the update. A second on a document
where the browser needed a minute and then failed.

The wire format is Hocuspocus's: every message is ``varString(documentName)``
followed by ``varUint(messageType)`` and the type's own payload. Type 0 carries
y-protocols/sync (step1 = state vector, step2 = update, 2 = update), type 2 is
authentication, type 8 is the server reporting that a sync finished.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

# The Y.Doc field y-prosemirror binds the editor to.
FRAGMENT = "prosemirror"

_MSG_SYNC = 0
_MSG_AUTH = 2
_MSG_SYNC_STATUS = 8
_SYNC_STEP1 = 0
_SYNC_STEP2 = 1
_SYNC_UPDATE = 2

_SYNC_TIMEOUT = 45.0  # a megabyte-scale history takes a few seconds to arrive; this is the ceiling
_SETTLE_POLL = 0.5  # how often the caller is asked whether the server kept the write
_SETTLE_TIMEOUT = 20.0


class CollabError(RuntimeError):
    """The collaborative channel refused the document, the write, or both."""


def _var_uint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _var_bytes(payload: bytes) -> bytes:
    return _var_uint(len(payload)) + payload


def _var_str(text: str) -> bytes:
    return _var_bytes(text.encode())


class _Reader:
    """Just enough of lib0's decoder to read what the server sends."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._at = 0

    def uint(self) -> int:
        value = shift = 0
        while True:
            byte = self._payload[self._at]
            self._at += 1
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7

    def buffer(self) -> bytes:
        size = self.uint()
        out = self._payload[self._at : self._at + size]
        self._at += size
        return out

    def string(self) -> str:
        return self.buffer().decode()


def document_name(workspace_id: str, kind: str, item_id: str) -> str:
    """Hocuspocus's name for one editor's document — ``kind`` is "article" or "task"."""
    return f"editor-{workspace_id}-{kind}-{item_id}"


def endpoint(collab_base: str, workspace_id: str, kind: str, item_id: str) -> str:
    return f"{collab_base}/editor/{workspace_id}/{kind}/{item_id}"


def _inline(text: XmlText, nodes: list[dict]) -> None:
    """Fill one XmlText with a paragraph's inline content, marks and all.

    Marks are formatting attributes on the text itself — ``{"bold": {...}}`` —
    which is how y-prosemirror stores them and how Weeek's own editor writes
    them. The text has to be integrated into the document already, so this runs
    after the tree is in place.
    """
    at = 0
    for node in nodes:
        if node.get("type") != "text":
            continue
        body = node.get("text") or ""
        if not body:
            continue
        marks = {mark["type"]: (mark.get("attrs") or {}) for mark in node.get("marks") or [] if mark.get("type")}
        text.insert(at, body, marks or None)
        # In UTF-8 bytes, which is how yrs indexes text — counting characters
        # puts every run after the first Cyrillic word in the wrong place. And
        # count what went in rather than asking the text how long it is: a
        # formatted XmlText stringifies to markup, not to its own characters.
        at += len(body.encode())


def _element(node: dict, pending: list[tuple[XmlText, list[dict]]]) -> XmlElement | None:
    """One ProseMirror node as an XmlElement, children and all.

    Inline runs are collected into ``pending`` rather than written here: their
    XmlText has to be integrated into the Y.Doc before it will take text.
    """
    node_type = node.get("type")
    if not node_type or node_type == "text":
        return None

    children: list[XmlElement | XmlText] = []
    inline: list[dict] = []
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text":
            inline.append(child)
            continue
        if inline:
            text = XmlText()
            children.append(text)
            pending.append((text, inline))
            inline = []
        element = _element(child, pending)
        if element is not None:
            children.append(element)
    if inline or not children:
        # A node that holds only text still needs its XmlText, and an empty
        # paragraph needs one too — that is what an empty line is.
        text = XmlText()
        children.append(text)
        pending.append((text, inline))

    return XmlElement(node_type, dict(node.get("attrs") or {}), children)


def write_body(fragment: XmlFragment, doc: dict) -> None:
    """Replace everything in ``fragment`` with the ProseMirror document ``doc``."""
    pending: list[tuple[XmlText, list[dict]]] = []
    elements = [
        element
        for node in doc.get("content") or []
        if isinstance(node, dict) and (element := _element(node, pending)) is not None
    ]
    if not elements:
        raise CollabError("Refusing to write an empty body: the document would be left blank.")

    del fragment.children[:]
    for element in elements:
        fragment.children.append(element)
    for text, nodes in pending:
        _inline(text, nodes)


def table_bodies(fragment: XmlFragment) -> list[XmlElement]:
    """Every ``table_body`` in the fragment, in document order."""
    found: list[XmlElement] = []

    def walk(node: Any) -> None:
        if not isinstance(node, XmlElement):
            return
        if node.tag == "table_body":
            found.append(node)
        for child in node.children:
            walk(child)

    for child in fragment.children:
        walk(child)
    return found


def measure_body(body: XmlElement) -> tuple[int, str | None]:
    """A live ``table_body``'s column count and its ``columns`` attribute, verbatim."""
    columns = 0
    for row in body.children:
        if not isinstance(row, XmlElement) or row.tag != "table_row":
            continue
        columns = sum(
            int(float(cell.attributes.get("colspan") or 1))
            for cell in row.children
            if isinstance(cell, XmlElement) and cell.tag == "table_cell"
        )
        break
    raw = body.attributes.get("columns")
    return columns, raw if isinstance(raw, str) else None


@asynccontextmanager
async def open_document(url: str, name: str, token: str):
    """Connect, authenticate, and sync — yields the shared Y.Doc, live.

    The connection stays open for as long as the caller holds it, which is what
    lets a write be confirmed before the socket goes away.
    """
    import websockets

    doc: Doc = Doc()
    try:
        socket = await websockets.connect(url, max_size=None, open_timeout=_SYNC_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - any connect failure reads the same to the caller
        raise CollabError(f"Could not reach Weeek's collaborative server: {exc}") from exc

    async def send(kind: int, payload: bytes) -> None:
        await socket.send(_var_str(name) + _var_uint(kind) + payload)

    try:
        await send(_MSG_AUTH, _var_uint(0) + _var_str(token))
        await send(_MSG_SYNC, _var_uint(_SYNC_STEP1) + _var_bytes(doc.get_state()))

        deadline = time.monotonic() + _SYNC_TIMEOUT
        permission: str | None = None
        synced = False
        while not synced:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CollabError(f"The document did not finish syncing within {_SYNC_TIMEOUT:.0f}s.")
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise CollabError(f"The document did not finish syncing within {_SYNC_TIMEOUT:.0f}s.") from exc
            reader = _Reader(raw if isinstance(raw, bytes) else raw.encode())
            reader.string()  # document name, echoed back
            kind = reader.uint()
            if kind == _MSG_SYNC:
                step = reader.uint()
                if step == _SYNC_STEP1:
                    await send(_MSG_SYNC, _var_uint(_SYNC_STEP2) + _var_bytes(doc.get_update(reader.buffer())))
                elif step in (_SYNC_STEP2, _SYNC_UPDATE):
                    doc.apply_update(reader.buffer())
            elif kind == _MSG_AUTH:
                reader.uint()
                permission = reader.string()
                if permission == "readonly":
                    raise CollabError("Weeek granted read-only access to this document — nothing was written.")
            elif kind == _MSG_SYNC_STATUS:
                synced = bool(reader.uint())

        yield doc, send
    finally:
        await socket.close()


async def confirm(settled, timeout: float = _SETTLE_TIMEOUT) -> bool:
    """Poll the caller's check until the server serves what was written."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if bool(await settled()):
            return True
        await asyncio.sleep(_SETTLE_POLL)
    return bool(await settled())


async def apply(url: str, name: str, token: str, change, settled=None) -> None:
    """Run ``change`` against the live document and hand the result to the server.

    ``change`` gets the ``prosemirror`` fragment and mutates it. ``settled`` — the
    caller's read-back check — decides when the write is real; without it the
    update is sent and the connection closed as soon as the socket accepts it.
    """
    async with open_document(url, name, token) as (doc, send):
        fragment = doc.get(FRAGMENT, type=XmlFragment)
        before = doc.get_state()
        change(fragment)
        update = doc.get_update(before)
        await send(_MSG_SYNC, _var_uint(_SYNC_UPDATE) + _var_bytes(update))
        if settled is not None and not await confirm(settled):
            raise CollabError(
                f"The change reached Weeek's collaborative server but the document still reads as it did "
                f"after {_SETTLE_TIMEOUT:.0f}s. Nothing was saved — re-read the document before retrying."
            )
