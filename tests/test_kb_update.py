"""Replacing a document body: the write counts only once Weeek serves it back.

The body goes into the document's Y.Doc over Weeek's collaborative channel, and
that channel confirms nothing on its own — a body that never reached the server
reads exactly like one that did. So the write is followed by polling Weeek's own
snapshot; these tests cover the polling, what counts as written, and the error
raised when the server never takes the edit.
"""

import json

import pytest

from weeek_mcp.kb import collab
from weeek_mcp.kb.client import KBError, WeeekKB
from weeek_mcp.kb.prosemirror import markdown_to_doc

OLD = "# Карта\n\nстарый текст\n"
NEW = "# Карта\n\nновый текст\n"
TABLE = "| a | b |\n| --- | --- |\n| 1 | 2 |\n"


class FakeServer:
    """Weeek as the internal API shows it: the body changes only when the sync lands."""

    def __init__(self, markdown):
        self.doc = markdown_to_doc(markdown)

    def receives(self, markdown):
        self.doc = markdown_to_doc(markdown)


async def _done(value):
    return value


@pytest.fixture
def kb(monkeypatch, tmp_path):
    server = FakeServer(OLD)
    state = tmp_path / "storage_state.json"
    state.write_text("{}")

    class Cfg:
        storage_state_path = state
        collab_base = "wss://collab.test"

    instance = WeeekKB.__new__(WeeekKB)
    instance._cfg = Cfg()
    monkeypatch.setattr(WeeekKB, "_workspace", lambda self: _done("923663"))
    monkeypatch.setattr(WeeekKB, "_document_content", lambda self, doc_id: _done(server.doc))
    monkeypatch.setattr(WeeekKB, "_collab", lambda self, kind, item_id: _done(("wss://collab.test/x", "doc", "token")))
    return instance, server


async def test_the_write_is_not_reported_before_weeek_serves_it(kb, monkeypatch):
    instance, server = kb
    answers = []

    async def fake_apply(url, name, token, change, settled=None):
        answers.append(await settled())  # nothing synced yet
        server.receives(NEW)
        answers.append(await settled())  # sync landed

    monkeypatch.setattr(collab, "apply", fake_apply)
    await instance.update_content("24", NEW)

    assert answers == [False, True]


async def test_a_document_the_server_never_takes_reaches_the_caller_as_a_kb_error(kb, monkeypatch):
    """server.py turns KBError into a tool error; a bare RuntimeError would slip past it."""
    instance, _server = kb

    async def fake_apply(url, name, token, change, settled=None):
        raise collab.CollabError("Nothing was saved.")

    monkeypatch.setattr(collab, "apply", fake_apply)

    with pytest.raises(KBError, match="Nothing was saved"):
        await instance.update_content("24", NEW)


async def test_the_body_is_written_into_the_shared_document(kb, monkeypatch):
    """What ``change`` does to the fragment is the whole write — check it end to end."""
    from pycrdt import Doc, XmlFragment

    instance, server = kb
    fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)

    async def fake_apply(url, name, token, change, settled=None):
        change(fragment)
        server.receives(NEW)

    monkeypatch.setattr(collab, "apply", fake_apply)
    await instance.update_content("24", NEW)

    assert "новый текст" in str(fragment)


# ------------------------------------------------------- widths ride the same channel


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


@pytest.fixture
def sized(monkeypatch, tmp_path):
    """A client whose one document holds a single 2x2 table, sized 338/338."""
    state = tmp_path / "storage_state.json"
    state.write_text("{}")

    class Cfg:
        storage_state_path = state
        collab_base = "wss://collab.test"

    instance = WeeekKB.__new__(WeeekKB)
    instance._cfg = Cfg()
    served = {"doc": _with_widths(TABLE, [338, 338])}
    monkeypatch.setattr(WeeekKB, "_workspace", lambda self: _done("923663"))
    monkeypatch.setattr(WeeekKB, "_document_content", lambda self, doc_id: _done(served["doc"]))
    monkeypatch.setattr(WeeekKB, "_collab", lambda self, kind, item_id: _done(("wss://collab.test/x", "doc", "token")))
    return instance, served


async def test_widths_are_written_and_waited_for(sized, monkeypatch):
    instance, served = sized
    seen = {}

    async def fake_apply(url, name, token, change, settled=None):
        change(_fragment_with_table([338, 338]))
        served["doc"] = _with_widths(TABLE, [104, 572])  # the sync lands
        seen["settled"] = await settled()

    monkeypatch.setattr(collab, "apply", fake_apply)
    result = await instance.set_table_widths("24", table_index=0, widths=[104, 572])

    assert seen["settled"] is True, "the widths path has to wait for the server"
    assert result["widths"] == [[104, 572]]


async def test_widths_that_never_arrive_are_still_reported(sized, monkeypatch):
    """Not waiting is not the same as not checking: the read-back still has to fail."""
    instance, _served = sized

    async def fake_apply(url, name, token, change, settled=None):
        change(_fragment_with_table([338, 338]))

    monkeypatch.setattr(collab, "apply", fake_apply)

    with pytest.raises(KBError, match="Weeek now reports"):
        await instance.set_table_widths("24", table_index=0, widths=[104, 572])


def _fragment_with_table(widths):
    """A live fragment holding one 2-column table, sized as given."""
    from pycrdt import Doc, XmlFragment

    fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)
    collab.write_body(fragment, _with_widths(TABLE, widths))
    return fragment


async def test_explicit_widths_ride_with_the_body(kb, monkeypatch):
    """Widths given with the body replace the carry-over, one entry per table."""
    instance, _server = kb
    captured = {}

    async def fake_apply(url, name, token, change, settled=None):
        from pycrdt import Doc, XmlFragment

        fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)
        change(fragment)
        body = collab.table_bodies(fragment)[0]
        captured["widths"] = [entry["width"] for entry in json.loads(body.attributes.get("columns"))]

    monkeypatch.setattr(collab, "apply", fake_apply)
    with pytest.raises(KBError, match=r"table\(s\) \[0\] kept their old widths"):
        await instance.update_content("24", NEW + "\n" + TABLE, table_widths=[[104, 572]])

    assert captured["widths"] == [104, 572]


async def test_a_width_list_per_table_is_required(kb, monkeypatch):
    """A miscounted list would silently size the wrong table, so it is refused."""
    instance, _server = kb

    async def fake_apply(url, name, token, change, settled=None):
        raise AssertionError("must not reach the server")

    monkeypatch.setattr(collab, "apply", fake_apply)

    with pytest.raises(KBError, match="1 table"):
        await instance.update_content("24", NEW + "\n" + TABLE, table_widths=[[104, 572], [90, 90]])

    with pytest.raises(KBError, match="2 column"):
        await instance.update_content("24", NEW + "\n" + TABLE, table_widths=[[104, 572, 90]])
