"""The collaborative channel: the wire codec, and the ProseMirror mapping.

The mapping is the part with teeth. Weeek's editor reads the Y.Doc directly, so
a node written in the wrong shape is not a rendering glitch — a table that lands
without its ``table_html`` wrapper is dropped wholesale the next time the
document is opened, and nothing gets it back. Every document these tests build
is therefore read back and compared against the ProseMirror JSON it came from.
"""

import json

from pycrdt import Doc, XmlElement, XmlFragment, XmlText

from weeek_mcp.kb import collab
from weeek_mcp.kb.prosemirror import markdown_to_doc, to_markdown


def _fragment(doc: dict) -> XmlFragment:
    fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)
    collab.write_body(fragment, doc)
    return fragment


def _node(item) -> dict:
    """One Y.Xml node back as ProseMirror JSON, so a write can be compared to its source."""
    if isinstance(item, XmlText):
        content = []
        for text, attrs in item.diff():
            node: dict = {"type": "text", "text": text}
            if attrs:
                node["marks"] = [
                    {"type": name, **({"attrs": values} if values else {})} for name, values in attrs.items()
                ]
            content.append(node)
        return {"inline": content}

    attrs = {}
    for key in ("level", "kind", "checked", "collapsed", "colspan", "rowspan", "columns", "link", "href", "language"):
        value = item.attributes.get(key)
        if value is not None:
            attrs[key] = int(value) if isinstance(value, float) and value.is_integer() else value

    content: list = []
    for child in item.children:
        rendered = _node(child)
        content.extend(rendered["inline"]) if "inline" in rendered else content.append(rendered)

    node = {"type": item.tag}
    if attrs:
        node["attrs"] = attrs
    if content:
        node["content"] = content
    return node


def _roundtrip(markdown: str) -> dict:
    """What Weeek would hold after ``markdown`` is written, as ProseMirror JSON."""
    fragment = _fragment(markdown_to_doc(markdown))
    return {"type": "doc", "content": [_node(child) for child in fragment.children]}


def test_the_wire_codec_reads_back_what_it_writes():
    payload = collab._var_str("editor-923663-article-24") + collab._var_uint(2) + collab._var_bytes(b"\x01\x02")
    reader = collab._Reader(payload)

    assert reader.string() == "editor-923663-article-24"
    assert reader.uint() == 2
    assert reader.buffer() == b"\x01\x02"


def test_a_heading_keeps_its_level():
    fragment = _fragment(markdown_to_doc("## Заголовок\n"))
    heading = fragment.children[0]

    assert heading.tag == "heading"
    assert heading.attributes.get("level") == 2
    assert str(heading) == '<heading level="2">Заголовок</heading>'


def test_marks_are_written_as_text_formatting():
    """Weeek stores marks as attributes on the text run, not as wrapper nodes."""
    fragment = _fragment(markdown_to_doc("обычный **жирный** и [ссылка](https://example.com)\n"))
    runs = fragment.children[0].children[0].diff()

    assert [text for text, _ in runs] == ["обычный ", "жирный", " и ", "ссылка"]
    assert runs[1][1] == {"bold": {}}
    assert runs[3][1] == {"link": {"href": "https://example.com"}}


def test_runs_stay_in_order_past_cyrillic_and_emoji():
    """yrs indexes text in UTF-8 bytes: counting characters interleaves the runs."""
    fragment = _fragment(markdown_to_doc("Дорожная **карта** 🟡 и *сроки*\n"))
    runs = fragment.children[0].children[0].diff()

    assert [text for text, _ in runs] == ["Дорожная ", "карта", " 🟡 и ", "сроки"]


def test_a_table_keeps_the_wrapper_the_editor_needs():
    """table > table_html > table_body: without table_html the editor drops the table."""
    fragment = _fragment(markdown_to_doc("| a | b |\n| --- | --- |\n| 1 | 2 |\n"))
    table = fragment.children[0]
    html = table.children[0]
    body = html.children[0]

    assert (table.tag, html.tag, body.tag) == ("table", "table_html", "table_body")
    assert [row.tag for row in body.children] == ["table_row", "table_row"]
    assert collab.measure_body(body) == (2, None)


def test_column_widths_travel_as_the_json_string_weeek_stores():
    doc = markdown_to_doc("| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    from weeek_mcp.kb.tables import write_columns

    write_columns(doc, [[{"id": "x", "width": 104, "color": "", "backgroundColor": ""}]])
    body = collab.table_bodies(_fragment(doc))[0]
    columns, raw = collab.measure_body(body)

    assert columns == 2
    assert json.loads(raw)[0]["width"] == 104


def test_a_document_survives_the_round_trip():
    """Everything the Markdown converter can produce, written and read back unchanged."""
    markdown = (
        "# Заголовок\n\n"
        "Абзац с **жирным**, *курсивом*, ~~зачёркнутым~~, `кодом` и [ссылкой](https://example.com).\n\n"
        "- пункт\n"
        "- ещё пункт\n\n"
        "1. раз\n"
        "2. два\n\n"
        "> цитата\n\n"
        "```python\nprint(1)\n```\n\n"
        "---\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n"
    )
    written = _roundtrip(markdown)

    assert to_markdown(written) == to_markdown(markdown_to_doc(markdown))


def test_an_empty_body_is_refused():
    """A converter that produced nothing must not be the way a document gets blanked."""
    fragment = Doc().get(collab.FRAGMENT, type=XmlFragment)
    fragment.children.append(XmlElement("paragraph", {}, [XmlText("что-то важное")]))

    try:
        collab.write_body(fragment, {"type": "doc", "content": []})
    except collab.CollabError as exc:
        assert "empty" in str(exc)
    else:  # pragma: no cover - the assertion below reports it
        raise AssertionError("an empty body should not be written")

    assert "что-то важное" in str(fragment)
