"""Attaching files to a document: the upload, the block, and keeping the block afterwards.

A file in a Weeek document is a block in the body, and Markdown has no such block.
So the risk sits past the upload: the next ``weeek_kb_update`` rebuilds the body
from Markdown, and a file block that does not come back out of it is gone.
"""

import pytest
from pycrdt import Doc, XmlFragment

from weeek_mcp.kb import collab
from weeek_mcp.kb.client import KBError, WeeekKB
from weeek_mcp.kb.prosemirror import markdown_to_doc, restore_files, to_markdown

LINK = "https://api.weeek.net/ws/923663/files/a2e07b47-3c9a-484c-b1dd-b47b3b334102"
FILE = {
    "type": "file",
    "attrs": {"meta": None, "id": "a2e07b47", "type": "file", "link": LINK, "name": "отчёт.pdf", "size": 2048},
}


def _doc(*content):
    return {"type": "doc", "content": list(content)}


def test_a_file_block_reads_as_a_link_on_its_own_line():
    doc = _doc({"type": "paragraph", "content": [{"type": "text", "text": "до"}]}, FILE)

    assert to_markdown(doc) == f"до\n\n[отчёт.pdf]({LINK})"


def test_the_block_comes_back_from_the_markdown_it_was_read_as():
    old = _doc(FILE)
    new = restore_files(markdown_to_doc("# Заголовок\n\n" + to_markdown(old) + "\n"), old)

    assert new["content"][1] == {
        "type": "file",
        "attrs": {"id": "a2e07b47", "type": "file", "link": LINK, "name": "отчёт.pdf", "size": 2048},
    }


def test_an_ordinary_link_stays_a_link():
    """Only a link to a file the old body held is a file block; anything else is text."""
    new = restore_files(markdown_to_doc("[сайт](https://example.com)\n\nсм. [отчёт](" + LINK + ") тут\n"), _doc(FILE))

    assert [block["type"] for block in new["content"]] == ["paragraph", "paragraph"]


def test_appending_leaves_the_body_in_place():
    fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)
    collab.write_body(fragment, markdown_to_doc("первый абзац\n"))
    collab.append_body(fragment, [{"type": "file", "attrs": {k: v for k, v in FILE["attrs"].items() if v is not None}}])

    first, added = fragment.children
    assert "первый абзац" in str(first)
    assert added.tag == "file"
    assert added.attributes.get("link") == LINK
    assert added.attributes.get("name") == "отчёт.pdf"


async def _done(value):
    return value


@pytest.fixture
def kb(monkeypatch, tmp_path):
    state = tmp_path / "storage_state.json"
    state.write_text("{}", encoding="utf-8")

    class Cfg:
        storage_state_path = state
        collab_base = "wss://collab.test"

    instance = WeeekKB.__new__(WeeekKB)
    instance._cfg = Cfg()
    served = {"doc": markdown_to_doc("первый абзац\n")}
    uploads = []

    async def fake_upload(self, path, file):
        uploads.append((path, file.name))
        return {
            "id": f"id-{file.name}",
            "name": file.name,
            "size": file.stat().st_size,
            "previewLink": LINK + file.name,
        }

    monkeypatch.setattr(WeeekKB, "_workspace", lambda self: _done("923663"))
    monkeypatch.setattr(WeeekKB, "_document_content", lambda self, doc_id: _done(served["doc"]))
    monkeypatch.setattr(WeeekKB, "_collab", lambda self, kind, item_id: _done(("wss://collab.test/x", "doc", "token")))
    monkeypatch.setattr(WeeekKB, "_upload", fake_upload)
    return instance, served, uploads


async def test_files_are_uploaded_then_added_as_blocks(kb, monkeypatch, tmp_path):
    instance, served, uploads = kb
    pdf = tmp_path / "отчёт.pdf"
    pdf.write_bytes(b"%PDF")
    png = tmp_path / "схема.png"
    png.write_bytes(b"\x89PNG")
    written = []

    async def fake_apply(url, name, token, change, settled=None):
        fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)
        collab.write_body(fragment, served["doc"])
        assert await settled() is False  # nothing synced yet
        change(fragment)
        written.extend(fragment.children)
        served["doc"] = {"type": "doc", "content": [{"type": c.tag, "attrs": dict(c.attributes)} for c in written]}
        assert await settled() is True

    monkeypatch.setattr(collab, "apply", fake_apply)
    attached = await instance.attach_files("24", [str(pdf), str(png)])

    assert uploads == [
        ("/ws/923663/kb/articles/24/attachments", "отчёт.pdf"),
        ("/ws/923663/kb/articles/24/attachments", "схема.png"),
    ]
    assert [c.tag for c in written] == ["paragraph", "file", "image"]
    assert written[1].attributes.get("type") == "file"
    assert written[1].attributes.get("size") == 4
    assert [item["name"] for item in attached] == ["отчёт.pdf", "схема.png"]


async def test_a_missing_file_stops_everything_before_the_first_upload(kb, tmp_path):
    instance, _served, uploads = kb
    real = tmp_path / "есть.txt"
    real.write_text("x", encoding="utf-8")

    with pytest.raises(KBError, match="нет.txt"):
        await instance.attach_files("24", [str(real), str(tmp_path / "нет.txt")])

    assert uploads == []


async def test_a_relative_path_is_refused(kb):
    """It would resolve against the server's checkout and upload whatever sits there."""
    instance, _served, uploads = kb

    with pytest.raises(KBError, match="absolute"):
        await instance.attach_files("24", ["README.md"])

    assert uploads == []


def test_an_image_keeps_the_id_of_its_upload():
    image = {"type": "image", "attrs": {"meta": None, "id": "a2e07c2f", "link": LINK, "externalSource": False}}
    new = restore_files(markdown_to_doc(to_markdown(_doc(image)) + "\n"), _doc(image))

    assert new["content"] == [{"type": "image", "attrs": {"id": "a2e07c2f", "link": LINK}}]


async def test_replacing_the_body_keeps_the_attachment(kb, monkeypatch):
    instance, served, _uploads = kb
    served["doc"] = _doc({"type": "paragraph", "content": [{"type": "text", "text": "старый текст"}]}, FILE)
    fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)

    async def fake_apply(url, name, token, change, settled=None):
        change(fragment)

    monkeypatch.setattr(collab, "apply", fake_apply)
    await instance.update_content("24", f"новый текст\n\n[отчёт.pdf]({LINK})\n")

    assert [c.tag for c in fragment.children] == ["paragraph", "file"]
    assert fragment.children[1].attributes.get("name") == "отчёт.pdf"
