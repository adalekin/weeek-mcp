"""Column widths of knowledge base tables.

Weeek stores the only size a table has — the pixel width of each column — on the
``table_body`` node, as a JSON *string*::

    "columns": "[{\\"id\\":\\"<uuid>\\",\\"width\\":180,\\"color\\":\\"\\",\\"backgroundColor\\":\\"\\"}]"

The order of the entries is the order of the columns. The editor's own resizer
never goes below 90px, so neither do we.

None of this survives a paste: the ``table_body`` spec parses from a bare
``tbody`` with no ``getAttrs``, so widths are dropped whenever a body is
replaced. This module holds the plumbing to read them beforehand and hand a plan
to the browser step that puts them back.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

MIN_WIDTH = 90  # the editor's own floor, enforced in its mousemove handler
DEFAULT_COLUMN_WIDTH = 180  # what the editor gives a column it was never told about
FALLBACK_CONTENT_WIDTH = 676  # the KB content column is a fixed 680px, less 2px padding either side


@dataclass(frozen=True)
class TableInfo:
    """One table, in document order."""

    columns: int
    widths: list[int] | None  # None when the table has never been sized


def _column_count(body: dict) -> int:
    for row in body.get("content") or []:
        if row.get("type") != "table_row":
            continue
        return sum(int((c.get("attrs") or {}).get("colspan") or 1) for c in row.get("content") or [])
    return 0


def _parse_columns(body: dict) -> list[int] | None:
    raw = (body.get("attrs") or {}).get("columns")
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    widths = [e.get("width") for e in entries if isinstance(e, dict)]
    return [int(w) for w in widths if isinstance(w, int | float)] or None


def fit_widths(columns: int, available: int = FALLBACK_CONTENT_WIDTH) -> list[int]:
    """Spread ``available`` pixels over ``columns`` columns, remainder on the last."""
    if columns <= 0:
        return []
    each = max(MIN_WIDTH, available // columns)
    widths = [each] * columns
    widths[-1] = max(MIN_WIDTH, available - each * (columns - 1))
    return widths


def size_new_tables(doc: dict) -> dict:
    """Give every unsized table in a document its fitted widths, in place.

    Used on create, where the content is stored as JSON and no browser is
    involved: without this the editor falls back to 180px per column the first
    time the document is opened, which leaves most tables narrower than the text
    beside them.
    """

    def walk(node: dict) -> None:
        if node.get("type") == "table_body":
            attrs = node.setdefault("attrs", {})
            if not attrs.get("columns"):
                attrs["columns"] = json.dumps(
                    [
                        {"id": str(uuid.uuid4()), "width": w, "color": "", "backgroundColor": ""}
                        for w in fit_widths(_column_count(node))
                    ]
                )
        for child in node.get("content") or []:
            if isinstance(child, dict):
                walk(child)

    if isinstance(doc, dict):
        walk(doc)
    return doc


def read_tables(doc: dict | None) -> list[TableInfo]:
    """Describe every table in a ProseMirror document, in document order."""
    found: list[TableInfo] = []

    def walk(node: dict) -> None:
        if node.get("type") == "table_body":
            found.append(TableInfo(columns=_column_count(node), widths=_parse_columns(node)))
        for child in node.get("content") or []:
            if isinstance(child, dict):
                walk(child)

    if isinstance(doc, dict):
        walk(doc)
    return found


def carry_over_plan(before: list[TableInfo], after: list[TableInfo]) -> list[dict | None]:
    """Plan for restoring widths onto tables that have just been re-pasted.

    A table keeps its widths when it still has the same number of columns; one
    whose shape changed — or that had no widths to begin with — is fitted to the
    content column instead, which beats the editor's 180px-per-column default.
    """
    plan: list[dict | None] = []
    for i, table in enumerate(after):
        old = before[i] if i < len(before) else None
        if old is not None and old.widths and old.columns == table.columns:
            plan.append({"mode": "widths", "widths": old.widths})
        else:
            plan.append({"mode": "fit"})
    return plan


def widths_plan(
    tables: list[TableInfo], index: int | None, widths: list[int | None] | None, *, fit: bool
) -> list[dict | None]:
    """Plan for an explicit width change: ``widths`` (None = leave alone) or ``fit``.

    ``index`` of None means every table in the document, which only makes sense
    for ``fit`` — an explicit list of widths belongs to one table's column count.
    """
    if not tables:
        raise ValueError("This document has no tables.")
    if fit == bool(widths):
        raise ValueError("Pass either widths or fit=true, not both.")

    if index is None:
        if not fit:
            raise ValueError("table_index is required when passing explicit widths.")
        return [{"mode": "fit"} for _ in tables]

    if not 0 <= index < len(tables):
        raise ValueError(f"table_index {index} is out of range: the document has {len(tables)} table(s).")

    if fit:
        spec: dict = {"mode": "fit"}
    else:
        assert widths is not None
        expected = tables[index].columns
        if len(widths) != expected:
            raise ValueError(f"Table {index} has {expected} column(s), got {len(widths)} width(s).")
        for w in widths:
            if w is not None and w < MIN_WIDTH:
                raise ValueError(f"Column widths start at {MIN_WIDTH}px (got {w}).")
        spec = {"mode": "widths", "widths": widths}

    return [spec if i == index else None for i in range(len(tables))]
