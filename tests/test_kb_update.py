"""Replacing a document body: the write counts only once Weeek serves it back.

Weeek's editor confirms nothing — a body that never reached the server reads
exactly like one that did — so ``update_content`` used to wait five seconds
blind and report success either way. What replaced that wait is polling the
server itself; these tests cover the polling, what counts as written, and the
error raised when the server never takes the edit.
"""

import json

import pytest

from weeek_mcp.kb import client as kb_client
from weeek_mcp.kb import session as session_mod
from weeek_mcp.kb.client import KBError, WeeekKB
from weeek_mcp.kb.prosemirror import markdown_to_doc
from weeek_mcp.kb.session import KBNotSettledError, _wait_until_settled

OLD = "# Карта\n\nстарый текст\n"
NEW = "# Карта\n\nновый текст\n"


class FakeServer:
    """Weeek as the internal API shows it: the body changes only when the sync lands."""

    def __init__(self, markdown):
        self.doc = markdown_to_doc(markdown)

    def receives(self, markdown):
        self.doc = markdown_to_doc(markdown)


@pytest.fixture
def kb(monkeypatch, tmp_path):
    server = FakeServer(OLD)
    state = tmp_path / "storage_state.json"
    state.write_text("{}")

    class Cfg:
        storage_state_path = state

    instance = WeeekKB.__new__(WeeekKB)
    instance._cfg = Cfg()
    monkeypatch.setattr(WeeekKB, "_workspace", lambda self: _done("923663"))
    monkeypatch.setattr(WeeekKB, "_document_content", lambda self, doc_id: _done(server.doc))
    return instance, server


async def _done(value):
    return value


async def test_the_write_is_not_reported_before_weeek_serves_it(kb, monkeypatch):
    instance, server = kb
    answers = []

    async def fake_replace(cfg, ws, article_id, html, columns_plan=None, settled=None):
        answers.append(await settled(None))  # nothing synced yet
        server.receives(NEW)
        answers.append(await settled(None))  # sync landed

    monkeypatch.setattr(kb_client, "replace_article_content", fake_replace)
    await instance.update_content("24", NEW)

    assert answers == [False, True]


async def test_column_widths_are_part_of_what_counts_as_written(kb, monkeypatch):
    instance, server = kb
    answers = []

    async def fake_replace(cfg, ws, article_id, html, columns_plan=None, settled=None):
        server.receives(NEW)
        answers.append(await settled([[169, 169]]))  # body there, widths not

    monkeypatch.setattr(kb_client, "replace_article_content", fake_replace)
    await instance.update_content("24", NEW)

    assert answers == [False]


async def test_a_held_document_reaches_the_caller_as_a_kb_error(kb, monkeypatch):
    """server.py turns KBError into a tool error; a bare RuntimeError would slip past it."""
    instance, _server = kb

    async def fake_replace(cfg, ws, article_id, html, columns_plan=None, settled=None):
        raise KBNotSettledError("Nothing was saved.")

    monkeypatch.setattr(kb_client, "replace_article_content", fake_replace)

    with pytest.raises(KBError, match="Nothing was saved"):
        await instance.update_content("24", NEW)


# ----------------------------------------------------------------- the wait itself


class FakePage:
    """Stands in for the Playwright page, recording how long it was asked to wait."""

    def __init__(self):
        self.waits: list[int] = []

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


def _log(_message: str) -> None:
    pass


async def test_the_page_is_held_open_until_the_server_answers_yes():
    answers = iter([False, False, True])

    async def settled(_widths):
        return next(answers)

    page = FakePage()
    await _wait_until_settled(page, settled, None, "document body", _log)

    assert page.waits == [session_mod._SETTLE_POLL_MS] * 2


async def test_waiting_forever_is_not_an_option(monkeypatch):
    """A document another session holds fails loudly instead of returning success."""
    monkeypatch.setattr(session_mod, "_SETTLE_TIMEOUT", 0.05)
    monkeypatch.setattr(session_mod, "_SETTLE_POLL_MS", 1)

    async def never(_widths):
        return False

    with pytest.raises(KBNotSettledError) as caught:
        await _wait_until_settled(FakePage(), never, None, "document body", _log)

    message = str(caught.value)
    assert "Nothing was saved" in message
    # The caller has to know not to hammer it: a repeat can empty the document.
    assert "do not retry blindly" in message
    # And how long the wait ran, so the failure is a diagnosis and not a shrug.
    assert "polls" in message


# ------------------------------------------------------- widths ride the same channel

TABLE = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"


def _with_widths(markdown, widths):
    """The document as Weeek stores it once a table has been sized."""
    doc = markdown_to_doc(markdown)

    def walk(node):
        if node.get("type") == "table_body":
            node.setdefault("attrs", {})["columns"] = json.dumps(
                [{"id": str(i), "width": w, "color": "", "backgroundColor": ""} for i, w in enumerate(widths)]
            )
        for child in node.get("content") or []:
            if isinstance(child, dict):
                walk(child)

    walk(doc)
    return doc


async def test_widths_are_not_reported_before_weeek_serves_them(monkeypatch, tmp_path):
    """Closing the page before the sync lands throws the widths away, silently.

    On a short document the sync wins the race and the old code looked fine; on a
    long one it lost every time, and the caller was told the table simply refused
    the change.
    """
    state = tmp_path / "storage_state.json"
    state.write_text("{}")

    class Cfg:
        storage_state_path = state

    instance = WeeekKB.__new__(WeeekKB)
    instance._cfg = Cfg()
    served = {"doc": _with_widths(TABLE, [338, 338])}
    monkeypatch.setattr(WeeekKB, "_workspace", lambda self: _done("923663"))
    monkeypatch.setattr(WeeekKB, "_document_content", lambda self, doc_id: _done(served["doc"]))

    answers = []

    async def fake_size(cfg, ws, article_id, plan, settled=None):
        answers.append(await settled([[104, 572]]))  # sync has not landed yet
        served["doc"] = _with_widths(TABLE, [104, 572])
        answers.append(await settled([[104, 572]]))  # now it has
        return {"tables": 1, "changed": 1, "expected": [[104, 572]], "available": 676}

    monkeypatch.setattr(kb_client, "set_table_columns", fake_size)
    result = await instance.set_table_widths("24", table_index=0, widths=[104, 572])

    assert answers == [False, True]
    assert result["widths"] == [[104, 572]]
