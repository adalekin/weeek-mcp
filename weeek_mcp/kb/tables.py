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


class TableShapeError(RuntimeError):
    """The document's tables are not what the plan was built for — do not write."""


@dataclass(frozen=True)
class TableInfo:
    """One table, in document order."""

    columns: int
    widths: list[int] | None  # None when the table has never been sized


@dataclass(frozen=True)
class MeasuredTable:
    """One table as the editor currently holds it, reported by the browser step."""

    columns: int
    raw_columns: str | None  # the table_body `columns` attribute, verbatim


def _column_count(body: dict) -> int:
    for row in body.get("content") or []:
        if row.get("type") != "table_row":
            continue
        return sum(int((c.get("attrs") or {}).get("colspan") or 1) for c in row.get("content") or [])
    return 0


def _parse_entries(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def _parse_columns(body: dict) -> list[int] | None:
    widths = [e.get("width") for e in _parse_entries((body.get("attrs") or {}).get("columns"))]
    return [int(w) for w in widths if isinstance(w, int | float)] or None


def fit_widths(columns: int, available: int = FALLBACK_CONTENT_WIDTH) -> list[int]:
    """Spread ``available`` pixels over ``columns`` columns, remainder on the last."""
    if columns <= 0:
        return []
    each = max(MIN_WIDTH, available // columns)
    widths = [each] * columns
    widths[-1] = max(MIN_WIDTH, available - each * (columns - 1))
    return widths


def _column_entry(old: dict | None, width: int | None) -> dict:
    """One entry of the ``columns`` attribute, keeping whatever the old one carried.

    Colors are part of the same attribute and belong to the user, so an entry is
    rebuilt around them rather than replaced.
    """
    old = old or {}
    target = width if width is not None else old.get("width") or DEFAULT_COLUMN_WIDTH
    return {
        "id": old.get("id") or str(uuid.uuid4()),
        "width": max(MIN_WIDTH, round(target)),
        "color": old.get("color") or "",
        "backgroundColor": old.get("backgroundColor") or "",
    }


def resolve_columns(measured: list[MeasuredTable], plan: list[dict | None], available: int) -> list[list[dict] | None]:
    """Turn a plan into the exact ``columns`` attribute for each table.

    All the arithmetic lives here rather than in the browser, so that what runs
    against the live document is the same code the tests cover. ``measured``
    comes from the editor itself: if it disagrees with the plan the document
    changed under us (it is a collaborative editor), and writing anything would
    land on the wrong table.
    """
    if len(measured) != len(plan):
        raise TableShapeError(
            f"The document now holds {len(measured)} table(s) but the change was prepared for "
            f"{len(plan)} — someone edited it in the meantime. Nothing was written."
        )

    resolved: list[list[dict] | None] = []
    for i, (table, spec) in enumerate(zip(measured, plan, strict=True)):
        if spec is None or not table.columns:
            resolved.append(None)
            continue
        if spec["mode"] == "fit":
            widths: list[int | None] = list(fit_widths(table.columns, available))
        else:
            widths = list(spec["widths"])
            if len(widths) != table.columns:
                raise TableShapeError(
                    f"Table {i} now has {table.columns} column(s), not {len(widths)} — "
                    "someone edited it in the meantime. Nothing was written."
                )
        previous = _parse_entries(table.raw_columns)
        resolved.append(
            [_column_entry(previous[k] if k < len(previous) else None, widths[k]) for k in range(table.columns)]
        )
    return resolved


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
                attrs["columns"] = json.dumps([_column_entry(None, w) for w in fit_widths(_column_count(node))])
        for child in node.get("content") or []:
            if isinstance(child, dict):
                walk(child)

    if isinstance(doc, dict):
        walk(doc)
    return doc


def write_columns(doc: dict, resolved: list[list[dict] | None]) -> dict:
    """Put resolved ``columns`` entries onto a document's tables, in place.

    The entries come from ``resolve_columns``; this only serializes them onto the
    matching ``table_body``, in document order. A table the plan left alone keeps
    whatever it already carried.
    """
    bodies: list[dict] = []

    def walk(node: dict) -> None:
        if node.get("type") == "table_body":
            bodies.append(node)
        for child in node.get("content") or []:
            if isinstance(child, dict):
                walk(child)

    walk(doc)
    if len(bodies) != len(resolved):
        raise TableShapeError(
            f"The body being written has {len(bodies)} table(s) but {len(resolved)} were sized. Nothing was written."
        )
    for body, entries in zip(bodies, resolved, strict=True):
        if entries is not None:
            body.setdefault("attrs", {})["columns"] = json.dumps(entries)
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
    if fit and widths:
        raise ValueError("Pass either widths or fit=true, not both.")
    if not fit and not widths:
        raise ValueError("Pass widths (a pixel value per column) or fit=true.")

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
