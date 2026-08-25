"""Unit tests for table column-width planning (no network, no browser)."""

import json

import pytest

from weeek_mcp.kb.tables import (
    PAGE_WIDTH,
    MeasuredTable,
    TableInfo,
    TableShapeError,
    carry_over_plan,
    ceiling,
    fit_widths,
    read_tables,
    resolve_columns,
    size_new_tables,
    widths_plan,
)


def _columns(*widths):
    return json.dumps([{"id": f"c{i}", "width": w, "color": "", "backgroundColor": ""} for i, w in enumerate(widths)])


def _table(*, columns=None, cells=2, colspans=None):
    spans = colspans or [1] * cells
    body = {
        "type": "table_body",
        "attrs": {"meta": {"id": "b"}},
        "content": [
            {
                "type": "table_row",
                "content": [{"type": "table_cell", "attrs": {"colspan": s, "rowspan": 1}} for s in spans],
            }
        ],
    }
    if columns is not None:
        body["attrs"]["columns"] = columns
    return {"type": "table", "content": [{"type": "table_html", "content": [body]}]}


def _doc(*content):
    return {"type": "doc", "content": list(content)}


def test_reads_widths_and_column_count():
    tables = read_tables(_doc(_table(columns=_columns(180, 306))))
    assert tables == [TableInfo(columns=2, widths=[180, 306])]


def test_unsized_table_reports_no_widths():
    assert read_tables(_doc(_table(cells=3))) == [TableInfo(columns=3, widths=None)]


def test_column_count_follows_colspan():
    assert read_tables(_doc(_table(colspans=[2, 1])))[0].columns == 3


def test_tables_are_listed_in_document_order():
    doc = _doc(_table(columns=_columns(100, 100)), {"type": "paragraph"}, _table(cells=3))
    assert [t.columns for t in read_tables(doc)] == [2, 3]


def test_read_tables_tolerates_empty_document():
    assert read_tables(None) == []
    assert read_tables({}) == []


def test_fit_widths_fill_the_content_column():
    assert sum(fit_widths(3, 676)) == 676
    assert fit_widths(3, 676) == [225, 225, 226]


def test_a_table_can_be_fitted_to_either_ceiling():
    """Weeek gives a table two widths to sit in, and which one is a decision."""
    assert ceiling("text") == 676
    assert ceiling("page") == PAGE_WIDTH == 1040
    assert sum(fit_widths(4, ceiling("page"))) == 1040


def test_an_unknown_ceiling_is_refused_by_name():
    with pytest.raises(ValueError, match="text, page"):
        ceiling("full-bleed")


def test_the_wide_ceiling_reaches_resolve_columns():
    """The choice has to survive the planning step, not just the constant."""
    resolved = resolve_columns([MeasuredTable(columns=4, raw_columns=None)], [{"mode": "fit"}], ceiling("page"))
    assert sum(entry["width"] for entry in resolved[0]) == 1040


def test_fit_widths_respect_the_minimum_when_columns_do_not_fit():
    assert fit_widths(10, 676) == [90] * 10


def test_new_tables_are_sized_on_create():
    doc = size_new_tables(_doc(_table(cells=3)))
    assert read_tables(doc) == [TableInfo(columns=3, widths=[225, 225, 226])]


def test_existing_widths_are_left_alone():
    doc = size_new_tables(_doc(_table(columns=_columns(120, 400))))
    assert read_tables(doc) == [TableInfo(columns=2, widths=[120, 400])]


def test_carry_over_keeps_widths_when_the_shape_is_unchanged():
    before = [TableInfo(columns=2, widths=[180, 306])]
    after = [TableInfo(columns=2, widths=None)]
    assert carry_over_plan(before, after) == [{"mode": "widths", "widths": [180, 306]}]


def test_carry_over_fits_a_table_whose_shape_changed():
    before = [TableInfo(columns=2, widths=[180, 306])]
    after = [TableInfo(columns=3, widths=None)]
    assert carry_over_plan(before, after) == [{"mode": "fit"}]


def test_carry_over_fits_tables_that_are_new_or_never_sized():
    before = [TableInfo(columns=2, widths=None)]
    after = [TableInfo(columns=2, widths=None), TableInfo(columns=4, widths=None)]
    assert carry_over_plan(before, after) == [{"mode": "fit"}, {"mode": "fit"}]


def test_explicit_widths_target_one_table():
    tables = [TableInfo(columns=2, widths=None), TableInfo(columns=3, widths=None)]
    assert widths_plan(tables, 1, [200, 200, 276], fit=False) == [
        None,
        {"mode": "widths", "widths": [200, 200, 276]},
    ]


def test_fit_without_an_index_covers_every_table():
    tables = [TableInfo(columns=2, widths=None), TableInfo(columns=3, widths=None)]
    assert widths_plan(tables, None, None, fit=True) == [{"mode": "fit"}, {"mode": "fit"}]


def test_widths_must_match_the_column_count():
    with pytest.raises(ValueError, match="2 column"):
        widths_plan([TableInfo(columns=2, widths=None)], 0, [100], fit=False)


def test_widths_below_the_editor_minimum_are_rejected():
    with pytest.raises(ValueError, match="90px"):
        widths_plan([TableInfo(columns=2, widths=None)], 0, [80, 200], fit=False)


def test_explicit_widths_need_a_table_index():
    tables = [TableInfo(columns=2, widths=None), TableInfo(columns=2, widths=None)]
    with pytest.raises(ValueError, match="table_index is required"):
        widths_plan(tables, None, [100, 100], fit=False)


def test_index_out_of_range_is_rejected():
    with pytest.raises(ValueError, match="out of range"):
        widths_plan([TableInfo(columns=2, widths=None)], 3, None, fit=True)


def test_widths_and_fit_are_mutually_exclusive():
    tables = [TableInfo(columns=2, widths=None)]
    with pytest.raises(ValueError, match="either widths or fit"):
        widths_plan(tables, 0, [100, 100], fit=True)


def test_one_of_widths_or_fit_is_required():
    tables = [TableInfo(columns=2, widths=None)]
    with pytest.raises(ValueError, match="Pass widths"):
        widths_plan(tables, 0, None, fit=False)


def test_a_document_without_tables_is_an_error():
    with pytest.raises(ValueError, match="no tables"):
        widths_plan([], 0, None, fit=True)


# ------------------------------------------------------------- resolving a plan into attributes


def test_resolve_fit_fills_the_measured_width():
    columns = resolve_columns([MeasuredTable(columns=2, raw_columns=None)], [{"mode": "fit"}], 700)
    assert [c["width"] for c in columns[0]] == [350, 350]
    assert all(c["id"] for c in columns[0])


def test_resolve_keeps_ids_and_colors_of_existing_columns():
    raw = json.dumps(
        [
            {"id": "keep-me", "width": 120, "color": "#111", "backgroundColor": "PaleBlue"},
            {"id": "and-me", "width": 120, "color": "", "backgroundColor": ""},
        ]
    )
    columns = resolve_columns(
        [MeasuredTable(columns=2, raw_columns=raw)], [{"mode": "widths", "widths": [300, 200]}], 676
    )
    assert [(c["id"], c["width"], c["color"]) for c in columns[0]] == [
        ("keep-me", 300, "#111"),
        ("and-me", 200, ""),
    ]
    assert columns[0][0]["backgroundColor"] == "PaleBlue"


def test_resolve_leaves_null_widths_at_their_current_size():
    raw = json.dumps([{"id": "a", "width": 120}, {"id": "b", "width": 400}])
    columns = resolve_columns(
        [MeasuredTable(columns=2, raw_columns=raw)], [{"mode": "widths", "widths": [None, 300]}], 676
    )
    assert [c["width"] for c in columns[0]] == [120, 300]


def test_resolve_falls_back_to_the_editor_default_for_unknown_columns():
    columns = resolve_columns(
        [MeasuredTable(columns=2, raw_columns=None)], [{"mode": "widths", "widths": [None, 300]}], 676
    )
    assert [c["width"] for c in columns[0]] == [180, 300]


def test_resolve_skips_tables_the_plan_does_not_name():
    columns = resolve_columns(
        [MeasuredTable(columns=2, raw_columns=None), MeasuredTable(columns=3, raw_columns=None)],
        [None, {"mode": "fit"}],
        600,
    )
    assert columns[0] is None
    assert [c["width"] for c in columns[1]] == [200, 200, 200]


def test_resolve_refuses_when_the_document_gained_a_table():
    with pytest.raises(TableShapeError, match="2 table"):
        resolve_columns(
            [MeasuredTable(columns=2, raw_columns=None), MeasuredTable(columns=2, raw_columns=None)],
            [{"mode": "fit"}],
            676,
        )


def test_resolve_refuses_when_a_table_changed_shape():
    with pytest.raises(TableShapeError, match="3 column"):
        resolve_columns([MeasuredTable(columns=3, raw_columns=None)], [{"mode": "widths", "widths": [100, 100]}], 676)


def test_resolve_never_goes_below_the_editor_minimum():
    columns = resolve_columns([MeasuredTable(columns=2, raw_columns=None)], [{"mode": "fit"}], 100)
    assert [c["width"] for c in columns[0]] == [90, 90]
