"""Unit tests for table column-width planning (no network, no browser)."""

import json

import pytest

from weeek_mcp.kb.tables import (
    TableInfo,
    carry_over_plan,
    fit_widths,
    read_tables,
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
    with pytest.raises(ValueError, match="either widths or fit"):
        widths_plan(tables, 0, None, fit=False)


def test_a_document_without_tables_is_an_error():
    with pytest.raises(ValueError, match="no tables"):
        widths_plan([], 0, None, fit=True)
