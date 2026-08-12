"""Unit tests for the ProseMirror <-> Markdown/HTML converters (no network)."""

from weeek_mcp.kb.prosemirror import markdown_to_doc, markdown_to_html, to_markdown


def _doc(*content):
    return {"type": "doc", "content": list(content)}


def _text(s, *marks):
    node = {"type": "text", "text": s}
    if marks:
        node["marks"] = [{"type": m} for m in marks]
    return node


def test_heading_and_paragraph():
    doc = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("Title")]},
        {
            "type": "paragraph",
            "content": [
                _text(
                    "Hello ",
                ),
                _text("world", "bold"),
            ],
        },
    )
    assert to_markdown(doc) == "## Title\n\nHello **world**"


def test_bullet_list_items_grouped():
    doc = _doc(
        {"type": "list", "attrs": {"kind": "bullet"}, "content": [{"type": "paragraph", "content": [_text("one")]}]},
        {"type": "list", "attrs": {"kind": "bullet"}, "content": [{"type": "paragraph", "content": [_text("two")]}]},
    )
    assert to_markdown(doc) == "- one\n- two"


def test_nested_list_indented():
    doc = _doc(
        {
            "type": "list",
            "attrs": {"kind": "bullet"},
            "content": [
                {"type": "paragraph", "content": [_text("parent")]},
                {
                    "type": "list",
                    "attrs": {"kind": "bullet"},
                    "content": [{"type": "paragraph", "content": [_text("child")]}],
                },
            ],
        },
    )
    assert to_markdown(doc) == "- parent\n  - child"


def test_checkbox_and_ordered():
    doc = _doc(
        {
            "type": "list",
            "attrs": {"kind": "check", "checked": True},
            "content": [{"type": "paragraph", "content": [_text("done")]}],
        },
        {"type": "list", "attrs": {"kind": "number"}, "content": [{"type": "paragraph", "content": [_text("first")]}]},
    )
    assert to_markdown(doc) == "- [x] done\n1. first"


def test_code_quote_hr_image():
    doc = _doc(
        {"type": "code", "attrs": {"language": "py"}, "content": [_text("x = 1")]},
        {"type": "quote", "content": [{"type": "paragraph", "content": [_text("q")]}]},
        {"type": "horizontal-line"},
        {"type": "image", "attrs": {"link": "http://img/1"}},
    )
    assert to_markdown(doc) == "```py\nx = 1\n```\n\n> q\n\n---\n\n![](http://img/1)"


def test_table():
    doc = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "table_row",
                    "content": [
                        {"type": "table_cell", "content": [{"type": "paragraph", "content": [_text("a")]}]},
                        {"type": "table_cell", "content": [{"type": "paragraph", "content": [_text("b")]}]},
                    ],
                },
                {
                    "type": "table_row",
                    "content": [
                        {"type": "table_cell", "content": [{"type": "paragraph", "content": [_text("1")]}]},
                        {"type": "table_cell", "content": [{"type": "paragraph", "content": [_text("2")]}]},
                    ],
                },
            ],
        },
    )
    assert to_markdown(doc) == "| a | b |\n| --- | --- |\n| 1 | 2 |"


def test_empty_and_garbage():
    assert to_markdown(None) == ""
    assert to_markdown({"type": "doc", "content": []}) == ""


# ------------------------------------------------------------- Markdown -> doc


def test_markdown_to_doc_blocks():
    doc = markdown_to_doc("# Title\n\nHello **world**\n\n- a\n- b")
    types = [n["type"] for n in doc["content"]]
    assert types == ["heading", "paragraph", "list", "list"]
    assert doc["content"][0]["attrs"]["level"] == 1
    para = doc["content"][1]["content"]
    assert para[1] == {"type": "text", "text": "world", "marks": [{"type": "bold"}]}


def test_markdown_to_doc_code_and_hr():
    doc = markdown_to_doc("```py\nx = 1\n```\n\n---")
    assert doc["content"][0]["type"] == "code"
    assert doc["content"][0]["attrs"] == {"language": "py"}
    assert doc["content"][0]["content"][0]["text"] == "x = 1"
    assert doc["content"][1] == {"type": "horizontal-line"}


def test_markdown_to_doc_roundtrip():
    md = "## Heading\n\ntext\n\n- one\n- two"
    assert to_markdown(markdown_to_doc(md)) == md


# ------------------------------------------------------------- doc -> HTML (for paste)


def test_markdown_to_html():
    html = markdown_to_html("# T\n\np **b**\n\n- x\n- y")
    assert html == "<h1>T</h1><p>p <strong>b</strong></p><ul><li>x</li><li>y</li></ul>"


def test_markdown_to_html_ordered_and_code():
    html = markdown_to_html("1. a\n2. b\n\n```\ncode\n```")
    assert html == "<ol><li>a</li><li>b</li></ol><pre><code>code</code></pre>"


# ------------------------------------------------------------- extended formatting (write path)


def test_markdown_to_doc_inline_marks():
    doc = markdown_to_doc("a **b** *c* ~~d~~ `e` [f](http://x) g")
    nodes = doc["content"][0]["content"]
    marks_by_text = {n["text"]: [m["type"] for m in n.get("marks", [])] for n in nodes}
    assert marks_by_text["b"] == ["bold"]
    assert marks_by_text["c"] == ["italic"]
    assert marks_by_text["d"] == ["strike"]
    assert marks_by_text["e"] == ["inline-code"]
    assert marks_by_text["f"] == ["link"]
    link_node = next(n for n in nodes if n["text"] == "f")
    assert link_node["marks"][0]["attrs"]["href"] == "http://x"


def test_markdown_to_doc_uses_the_names_weeek_accepts():
    """Weeek stores a document with an unknown kind/mark and *then* errors on it.

    The names below are the ones its own editor writes (surveyed across the KB): a wrong
    ``kind`` answers 500 and a wrong mark answers 400, in both cases after the write.
    """
    doc = markdown_to_doc("1. first\n2. second\n\n- [x] done\n- plain\n\ntail `code`")
    kinds = [n["attrs"]["kind"] for n in doc["content"] if n["type"] == "list"]
    assert kinds == ["ordered", "ordered", "task", "bullet"]

    tail = doc["content"][-1]["content"]
    assert tail[-1]["marks"] == [{"type": "inline-code"}]

    # Round-trips back to the same Markdown, so reading is not left behind by the rename.
    assert to_markdown(doc) == "1. first\n1. second\n- [x] done\n- plain\n\ntail `code`"


def test_markdown_to_doc_nested_list():
    doc = markdown_to_doc("- a\n  - a.1\n    - a.1.1\n- b")
    content = doc["content"]
    assert [n["type"] for n in content] == ["list", "list"]
    a_children = content[0]["content"]
    assert a_children[0]["type"] == "paragraph"
    assert a_children[1]["type"] == "list"  # a.1 nested under a
    nested = a_children[1]["content"]
    assert nested[1]["type"] == "list"  # a.1.1 nested under a.1
    assert to_markdown(doc) == "- a\n  - a.1\n    - a.1.1\n- b"


def test_markdown_to_doc_table():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |"
    doc = markdown_to_doc(md)
    table = doc["content"][0]
    assert table["type"] == "table"
    # Weeek's editor drops a table whose body is not wrapped in table_html.
    assert [n["type"] for n in table["content"]] == ["table_html"]
    assert [n["type"] for n in table["content"][0]["content"]] == ["table_body"]
    assert to_markdown(doc) == md


def test_markdown_to_doc_image_block():
    doc = markdown_to_doc("![alt](http://img/1)")
    assert doc["content"][0] == {"type": "image", "attrs": {"link": "http://img/1"}}


def test_markdown_to_html_extended():
    html = markdown_to_html("*i* ~~s~~ [l](http://x)\n\n- a\n  - a.1\n\n| A | B |\n| --- | --- |\n| 1 | 2 |")
    assert "<em>i</em>" in html
    assert "<s>s</s>" in html
    assert '<a href="http://x">l</a>' in html
    assert "<ul><li>a<ul><li>a.1</li></ul></li></ul>" in html
    assert (
        "<table><tbody><tr><td><p>A</p></td><td><p>B</p></td></tr><tr><td><p>1</p></td><td><p>2</p></td></tr></tbody></table>"
        in html
    )
