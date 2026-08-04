"""Convert between Weeek knowledge base documents (ProseMirror/TipTap JSON) and Markdown.

Node types observed in Weeek KB: doc, heading, paragraph, list (nested), text, code,
quote, horizontal-line, image, line-break, table/table_body/table_row/table_cell/table_html.
Mark types: bold, italic, strike, code, link.

Both directions (read: doc -> markdown, write: markdown -> doc / -> HTML) support
the same feature set: headings, nested bullet/numbered/checkbox lists, blockquotes,
fenced code, horizontal rules, pipe tables, images, and inline bold/italic/strike/
code/links.
"""

from __future__ import annotations

import html as _html
import re
from typing import Any

_MARK_WRAP = {
    "bold": "**",
    "italic": "*",
    "strike": "~~",
    "strikethrough": "~~",
    "code": "`",
    "inline-code": "`",
}


def to_markdown(doc: Any) -> str:
    """Render a ProseMirror ``doc`` node (or its ``content`` list) to Markdown."""
    if isinstance(doc, dict):
        nodes = doc.get("content") or []
    elif isinstance(doc, list):
        nodes = doc
    else:
        return ""
    return _blocks(nodes).strip()


# --------------------------------------------------------------------------- inline
def _inline(nodes: Any) -> str:
    out: list[str] = []
    for n in nodes or []:
        t = n.get("type")
        if t == "text":
            text = n.get("text", "")
            for m in n.get("marks", []) or []:
                mt = m.get("type")
                if mt == "link":
                    attrs = m.get("attrs") or {}
                    href = attrs.get("href") or attrs.get("link") or ""
                    text = f"[{text}]({href})"
                elif mt in _MARK_WRAP:
                    w = _MARK_WRAP[mt]
                    text = f"{w}{text}{w}"
            out.append(text)
        elif t == "line-break":
            out.append("  \n")
        elif t == "image":
            out.append(f"![]({(n.get('attrs') or {}).get('link', '')})")
        else:
            out.append(_inline(n.get("content")))
    return "".join(out)


def _plain_text(nodes: Any) -> str:
    out: list[str] = []
    for n in nodes or []:
        if n.get("type") == "text":
            out.append(n.get("text", ""))
        elif n.get("type") == "line-break":
            out.append("\n")
        else:
            out.append(_plain_text(n.get("content")))
    return "".join(out)


# --------------------------------------------------------------------------- blocks
def _blocks(nodes: Any) -> str:
    blocks: list[str] = []
    list_buf: list[str] = []

    def flush() -> None:
        if list_buf:
            blocks.append("\n".join(list_buf))
            list_buf.clear()

    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        if n.get("type") == "list":
            list_buf.append(_list_item(n, 0))
        else:
            flush()
            b = _block(n)
            if b.strip():
                blocks.append(b)
    flush()
    return "\n\n".join(blocks)


def _block(node: dict) -> str:
    t = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []

    if t == "heading":
        level = attrs.get("level") or 1
        return "#" * max(1, min(6, int(level))) + " " + _inline(content)
    if t == "paragraph":
        return _inline(content)
    if t == "quote":
        inner = _blocks(content)
        return "\n".join(("> " + line).rstrip() for line in inner.splitlines() or [""])
    if t == "code":
        lang = attrs.get("language") or attrs.get("lang") or ""
        return f"```{lang}\n{_plain_text(content)}\n```"
    if t == "horizontal-line":
        return "---"
    if t == "image":
        return f"![]({attrs.get('link', '')})"
    if t == "list":
        return _list_item(node, 0)
    if t in ("table", "table_body", "table_row", "table_cell"):
        return _table(node)
    if t == "table_html":
        return _plain_text(content)
    # Unknown block: recurse into children so nothing is silently dropped.
    return _blocks(content)


def _list_item(node: dict, depth: int) -> str:
    attrs = node.get("attrs") or {}
    kind = attrs.get("kind", "bullet")
    if kind in ("check", "todo", "checkbox"):
        marker = "- [x] " if attrs.get("checked") else "- [ ] "
    elif kind in ("number", "ordered"):
        marker = "1. "
    else:
        marker = "- "

    indent = "  " * depth
    text_parts: list[str] = []
    nested: list[str] = []
    for c in node.get("content") or []:
        if c.get("type") == "list":
            nested.append(_list_item(c, depth + 1))
        elif c.get("type") == "paragraph":
            text_parts.append(_inline(c.get("content")))
        else:
            text_parts.append(_block(c))

    line = indent + marker + " ".join(p for p in text_parts if p.strip()).strip()
    if nested:
        return line + "\n" + "\n".join(nested)
    return line


def _table(node: dict) -> str:
    rows: list[dict] = []

    def find_rows(n: dict) -> None:
        for c in n.get("content") or []:
            if c.get("type") == "table_row":
                rows.append(c)
            else:
                find_rows(c)

    find_rows(node)
    if not rows:
        return ""

    lines: list[str] = []
    for i, row in enumerate(rows):
        cells = [_inline_cell(c) for c in (row.get("content") or []) if c.get("type") == "table_cell"]
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("| " + " | ".join("---" for _ in cells) + " |")
    return "\n".join(lines)


def _inline_cell(cell: dict) -> str:
    # Flatten a cell's block content to a single line for Markdown tables.
    text = _blocks(cell.get("content") or [])
    return text.replace("\n", " ").replace("|", "\\|").strip()


# ======================================================================= Markdown -> ProseMirror
# Supports the block/inline subset Weeek's editor uses: headings, paragraphs,
# nested bullet/numbered/checkbox lists, fenced code, blockquotes, horizontal
# rules, pipe tables, images, and inline **bold**, *italic*/_italic_, ~~strike~~,
# `code`, [links](url).

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^( *)[-*]\s+(.*)$")
_CHECK_RE = re.compile(r"^( *)[-*]\s+\[( |x|X)\]\s+(.*)$")
_NUMBER_RE = re.compile(r"^( *)\d+[.)]\s+(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")

_INLINE_TOKEN_RE = re.compile(
    r"(\*\*.+?\*\*"  # bold
    r"|~~.+?~~"  # strike
    r"|`[^`]+?`"  # code
    r"|!\[[^\]]*\]\([^)]+?\)"  # image
    r"|\[[^\]]+?\]\([^)]+?\)"  # link
    r"|\*[^*\s][^*]*?\*"  # italic (*...*)
    r"|_[^_\s][^_]*?_)"  # italic (_..._)
)
_LINK_RE = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)$")


def _inline_nodes(text: str) -> list[dict]:
    nodes: list[dict] = []
    for part in _INLINE_TOKEN_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "bold"}]})
        elif part.startswith("~~") and part.endswith("~~") and len(part) > 4:
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strike"}]})
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif part.startswith("!["):
            m = _IMAGE_RE.match(part)
            if m:
                nodes.append({"type": "image", "attrs": {"link": m.group(2)}})
            else:
                nodes.append({"type": "text", "text": part})
        elif part.startswith("["):
            m = _LINK_RE.match(part)
            if m:
                nodes.append(
                    {"type": "text", "text": m.group(1), "marks": [{"type": "link", "attrs": {"href": m.group(2)}}]}
                )
            else:
                nodes.append({"type": "text", "text": part})
        elif (part.startswith("*") and part.endswith("*") and len(part) > 2) or (
            part.startswith("_") and part.endswith("_") and len(part) > 2
        ):
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "italic"}]})
        else:
            nodes.append({"type": "text", "text": part})
    return nodes


def _split_table_row(line: str) -> list[str]:
    m = _TABLE_ROW_RE.match(line)
    inner = m.group(1) if m else line
    return [cell.strip() for cell in inner.split("|")]


def _para(text: str) -> dict:
    return {"type": "paragraph", "content": _inline_nodes(text)}


def markdown_to_doc(md: str) -> dict:
    """Convert a Markdown string to a Weeek ProseMirror ``doc`` node."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    content: list[dict] = []
    i = 0
    para_buf: list[str] = []
    list_node_at_depth: dict[int, dict] = {}

    def flush_para() -> None:
        if para_buf:
            content.append(_para(" ".join(para_buf).strip()))
            para_buf.clear()

    def add_list_item(depth: int, node: dict) -> None:
        parent = list_node_at_depth.get(depth - 1) if depth > 0 else None
        container = parent["content"] if parent is not None else content
        container.append(node)
        list_node_at_depth[depth] = node
        for k in [k for k in list_node_at_depth if k > depth]:
            del list_node_at_depth[k]

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # fenced code block
        if stripped.startswith("```"):
            flush_para()
            lang = stripped[3:].strip()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            node: dict[str, Any] = {"type": "code", "content": [{"type": "text", "text": "\n".join(code)}]}
            if lang:
                node["attrs"] = {"language": lang}
            content.append(node)
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        m = _HEADING_RE.match(stripped)
        if m:
            flush_para()
            content.append(
                {"type": "heading", "attrs": {"level": len(m.group(1))}, "content": _inline_nodes(m.group(2))}
            )
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            flush_para()
            content.append({"type": "horizontal-line"})
            i += 1
            continue

        if stripped.startswith(">"):
            flush_para()
            content.append({"type": "quote", "content": [_para(stripped[1:].strip())]})
            i += 1
            continue

        mc = _CHECK_RE.match(line)
        if mc:
            flush_para()
            depth = len(mc.group(1)) // 2
            node = {
                "type": "list",
                "attrs": {"kind": "check", "checked": mc.group(2).lower() == "x"},
                "content": [_para(mc.group(3))],
            }
            add_list_item(depth, node)
            i += 1
            continue

        mb = _BULLET_RE.match(line)
        if mb:
            flush_para()
            depth = len(mb.group(1)) // 2
            node = {"type": "list", "attrs": {"kind": "bullet"}, "content": [_para(mb.group(2))]}
            add_list_item(depth, node)
            i += 1
            continue

        mn = _NUMBER_RE.match(line)
        if mn:
            flush_para()
            depth = len(mn.group(1)) // 2
            node = {"type": "list", "attrs": {"kind": "number"}, "content": [_para(mn.group(2))]}
            add_list_item(depth, node)
            i += 1
            continue

        if (
            _TABLE_ROW_RE.match(stripped)
            and i + 1 < len(lines)
            and _TABLE_ROW_RE.match(lines[i + 1].strip())
            and _TABLE_SEP_RE.match(lines[i + 1].strip())
        ):
            flush_para()
            header = _split_table_row(stripped)
            i += 2  # skip header + separator
            rows = [header]
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i].strip()):
                rows.append(_split_table_row(lines[i].strip()))
                i += 1
            table_rows = [
                {
                    "type": "table_row",
                    "content": [{"type": "table_cell", "content": [_para(cell)]} for cell in row],
                }
                for row in rows
            ]
            # The table_html wrapper is not decorative: a table whose body is not
            # wrapped in it is dropped wholesale the first time Weeek's editor
            # opens the document, taking the rows with it.
            content.append(
                {
                    "type": "table",
                    "content": [{"type": "table_html", "content": [{"type": "table_body", "content": table_rows}]}],
                }
            )
            continue

        mi = _IMAGE_RE.match(stripped)
        if mi:
            flush_para()
            content.append({"type": "image", "attrs": {"link": mi.group(2)}})
            i += 1
            continue

        para_buf.append(stripped)
        i += 1

    flush_para()
    return {"type": "doc", "content": content}


# ======================================================================= doc -> HTML
# Used to paste content into Weeek's editor (ProseMirror parses HTML into its schema).

_MARK_TAG = {"bold": "strong", "italic": "em", "strike": "s", "code": "code", "inline-code": "code"}


def markdown_to_html(md: str) -> str:
    return doc_to_html(markdown_to_doc(md))


def doc_to_html(doc: Any) -> str:
    nodes = doc.get("content") if isinstance(doc, dict) else doc
    return _html_blocks(nodes or [])


def _esc(s: str) -> str:
    return _html.escape(s, quote=False)


def _html_inline(nodes: Any) -> str:
    out: list[str] = []
    for n in nodes or []:
        t = n.get("type")
        if t == "text":
            text = _esc(n.get("text", ""))
            for m in n.get("marks", []) or []:
                mt = m.get("type")
                if mt == "link":
                    href = _esc((m.get("attrs") or {}).get("href") or (m.get("attrs") or {}).get("link") or "")
                    text = f'<a href="{href}">{text}</a>'
                elif mt in _MARK_TAG:
                    tag = _MARK_TAG[mt]
                    text = f"<{tag}>{text}</{tag}>"
            out.append(text)
        elif t == "line-break":
            out.append("<br>")
        elif t == "image":
            out.append(f'<img src="{_esc((n.get("attrs") or {}).get("link", ""))}">')
        else:
            out.append(_html_inline(n.get("content")))
    return "".join(out)


def _html_blocks(nodes: Any) -> str:
    parts: list[str] = []
    i = 0
    nodes = list(nodes or [])
    while i < len(nodes):
        n = nodes[i]
        if not isinstance(n, dict):
            i += 1
            continue
        if n.get("type") == "list":
            # group consecutive list items of the same ordered/unordered kind
            kind = (n.get("attrs") or {}).get("kind", "bullet")
            ordered = kind in ("number", "ordered")
            tag = "ol" if ordered else "ul"
            items = []
            while i < len(nodes) and nodes[i].get("type") == "list":
                a = nodes[i].get("attrs") or {}
                is_ord = a.get("kind") in ("number", "ordered")
                if is_ord != ordered:
                    break
                items.append(f"<li>{_html_list_item(nodes[i])}</li>")
                i += 1
            parts.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        parts.append(_html_block(n))
        i += 1
    return "".join(parts)


def _html_list_item(list_node: dict) -> str:
    # A list item's own text, plus any nested "list" children rendered as a sub-list.
    text = ""
    nested: list[dict] = []
    for c in list_node.get("content") or []:
        if c.get("type") == "paragraph":
            text = _html_inline(c.get("content"))
        elif c.get("type") == "list":
            nested.append(c)
        else:
            nested_html = _html_block(c)
            text = text + nested_html if text else nested_html
    return text + (_html_blocks(nested) if nested else "")


def _html_block(node: dict) -> str:
    t = node.get("type")
    attrs = node.get("attrs") or {}
    content = node.get("content") or []
    if t == "heading":
        lvl = max(1, min(6, int(attrs.get("level") or 1)))
        return f"<h{lvl}>{_html_inline(content)}</h{lvl}>"
    if t == "paragraph":
        return f"<p>{_html_inline(content)}</p>"
    if t == "quote":
        return f"<blockquote>{_html_blocks(content)}</blockquote>"
    if t == "code":
        return f"<pre><code>{_esc(_plain_text(content))}</code></pre>"
    if t == "horizontal-line":
        return "<hr>"
    if t == "image":
        return f'<img src="{_esc(attrs.get("link", ""))}">'
    if t in ("table", "table_body", "table_row", "table_cell"):
        return _html_table(node)
    if t == "table_html":
        return _html_blocks(content)
    return f"<p>{_html_inline(content)}</p>"


def _html_table(node: dict) -> str:
    rows: list[dict] = []

    def find_rows(n: dict) -> None:
        for c in n.get("content") or []:
            if c.get("type") == "table_row":
                rows.append(c)
            else:
                find_rows(c)

    find_rows(node)
    if not rows:
        return ""
    row_html = []
    for row in rows:
        cells = [c for c in (row.get("content") or []) if c.get("type") == "table_cell"]
        row_html.append("<tr>" + "".join(f"<td>{_html_blocks(c.get('content') or [])}</td>" for c in cells) + "</tr>")
    return "<table><tbody>" + "".join(row_html) + "</tbody></table>"
