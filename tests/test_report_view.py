"""What each block of a report shows: key figures, charts, tables and texts, worked out from rows made by hand.

The views are what the page, the Markdown and the JSON of a report are all written from, so they are where a number is
formatted, a long query is cut and a column that a block names and its query lacks is found. Every test makes a run with
the rows of its queries written out, so each number can be checked by hand. No lake and no DuckDB are needed.
"""

import datetime as dt
from decimal import Decimal as D

import pyarrow as pa
import pytest

from local_data_platform.report.formats import format_value
from local_data_platform.report.spec import DEFAULT_MAX_POINTS, MAX_SERIES
from local_data_platform.report.svg import chart_svg
from local_data_platform.report.view import Chart, Grid, Kpi, Words, resolve
from tests.report_support import decimals, make_run


def view_of(block, rows, **keys):
    """The one view of a report that has one block, which shows the query ``q``."""
    run = make_run([block], {"q": rows}, **keys)
    assert run.ok, run.problems
    assert len(run.views) == 1
    return run.views[0]


def problems_of(block, rows):
    run = make_run([block], {"q": rows})
    assert run.views == []
    return run.problems


# --------------------------------------------------------------------------- a key figure


def test_a_key_figure_shows_the_first_row_in_its_format_and_compares_with_a_column():
    rows = pa.table({"revenue": decimals("1234.50", "9"), "previous": decimals("1100.00", "9")})
    block = {"type": "kpi", "title": "Revenue", "query": "q", "value": "revenue", "compare": "previous",
             "format": "currency:USD"}

    tile = view_of(block, rows)

    assert isinstance(tile, Kpi)
    assert (tile.title, tile.value, tile.compare, tile.change, tile.direction) == (
        "Revenue", "$1,234.50", "$1,100.00", "+12.2%", "up")
    assert (tile.raw, tile.raw_compare, tile.format, tile.result, tile.notes) == (D("1234.50"), D("1100.00"),
                                                                                  "currency:USD", "q", ())
    assert tile.block.index == 0


@pytest.mark.parametrize(("value", "other", "words", "direction"), [
    ("90.00", "100.00", "-10.0%", "down"), ("100.00", "100.00", "0.0%", "flat"), ("100.04", "100.00", "0.0%", "flat"),
    ("150.00", "100.00", "+50.0%", "up"), ("-50.00", "-100.00", "+50.0%", "up"),
])
def test_a_key_figure_that_moved_says_how_far_and_which_way(value, other, words, direction):
    rows = pa.table({"v": decimals(value), "c": decimals(other)})

    tile = view_of({"type": "kpi", "query": "q", "value": "v", "compare": "c"}, rows)

    assert (tile.change, tile.direction) == (words, direction)


def test_a_key_figure_with_no_column_to_compare_with_says_nothing_of_change():
    tile = view_of({"type": "kpi", "query": "q", "value": "orders"}, pa.table({"orders": [87]}))

    assert (tile.title, tile.value, tile.compare, tile.change, tile.direction) == ("orders", "87", None, None, None)


def test_a_key_figure_cannot_say_how_much_it_grew_from_nothing_but_still_says_what_it_was():
    rows = pa.table({"v": [5], "c": [0]})

    tile = view_of({"type": "kpi", "query": "q", "value": "v", "compare": "c", "format": "integer"}, rows)

    assert (tile.value, tile.compare, tile.change, tile.direction) == ("5", "0", None, None)


def test_a_key_figure_whose_comparison_is_missing_in_the_row_has_no_comparison():
    rows = pa.table({"v": [5], "c": pa.array([None], pa.int64())})

    tile = view_of({"type": "kpi", "query": "q", "value": "v", "compare": "c"}, rows)

    assert (tile.compare, tile.change, tile.raw_compare) == (None, None, None)


def test_a_key_figure_of_a_query_with_no_row_is_a_dash():
    rows = pa.table({"v": pa.array([], pa.int64()), "c": pa.array([], pa.int64())})

    tile = view_of({"type": "kpi", "query": "q", "value": "v", "compare": "c", "format": "integer"}, rows)

    assert (tile.value, tile.compare, tile.change) == ("-", None, None)


def test_a_key_figure_that_is_text_is_written_as_it_is_whatever_the_format():
    tile = view_of({"type": "kpi", "query": "q", "value": "v", "format": "integer"}, pa.table({"v": ["Pune"]}))

    assert (tile.value, tile.compare, tile.change, tile.direction) == ("Pune", None, None, None)


def test_the_comparison_of_a_key_figure_has_to_be_numbers():
    rows = pa.table({"v": [1], "c": ["Delhi"]})

    assert problems_of({"type": "kpi", "query": "q", "value": "v", "compare": "c"}, rows) == [
        "metadata.blocks[0].compare: the column 'c' of the query 'q' is a string, and it has to hold numbers"]
    assert problems_of({"type": "kpi", "query": "q", "value": "w"}, rows) == [
        "metadata.blocks[0].value: the query 'q' has no column 'w'. Its columns are ['v', 'c']"]


def test_a_key_figure_uses_the_first_row_only():
    tile = view_of({"type": "kpi", "query": "q", "value": "v"}, pa.table({"v": [1, 2, 3]}))

    assert tile.value == "1"


def test_a_key_figure_says_when_the_row_cap_cut_the_rows_of_its_query():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1, 2, 3]})},
                   truncated=("q",), row_cap=3)

    assert run.views[0].notes == ("The query has more rows than the row cap of 3, so this has the first 3.",)


def test_a_key_figure_of_a_metric_over_periods_is_the_last_period_against_the_one_before():
    rows = pa.table({"day": [dt.date(2026, 7, 30), dt.date(2026, 7, 31)], "revenue": decimals("100.00", "112.50")})
    block = {"type": "kpi", "metric": "revenue", "grain": "day"}

    tile = make_run([block], {"blocks[0]": rows}, formats={"blocks[0]": {"revenue": "number:2"}}).views[0]

    assert (tile.title, tile.value, tile.compare, tile.change, tile.direction, tile.result) == (
        "revenue", "112.50", "100.00", "+12.5%", "up", "blocks[0]")
    assert tile.format == "number:2"


def test_a_key_figure_of_a_metric_with_one_period_or_none_has_nothing_to_compare_with():
    rows = pa.table({"day": [dt.date(2026, 7, 31)], "revenue": decimals("112.50")})
    block = {"type": "kpi", "metric": "revenue", "grain": "day", "title": "Revenue"}

    tile = make_run([block], {"blocks[0]": rows}, formats={"blocks[0]": {"revenue": "number:2"}}).views[0]
    empty = make_run([block], {"blocks[0]": rows.slice(0, 0)}).views[0]

    assert (tile.title, tile.value, tile.compare, tile.change) == ("Revenue", "112.50", None, None)
    assert (empty.value, empty.compare) == ("-", None)


def test_a_key_figure_of_a_metric_with_no_grain_has_no_period_before_it_and_shows_the_last_row():
    rows = pa.table({"revenue": decimals("100.00", "112.50")})

    tile = make_run([{"type": "kpi", "metric": "revenue"}], {"blocks[0]": rows},
                    formats={"blocks[0]": {"revenue": "number:2"}}).views[0]

    assert (tile.value, tile.compare, tile.change, tile.raw_compare) == ("112.50", None, None, None)


def test_a_key_figure_of_a_metric_with_no_grain_is_its_one_row_and_the_block_may_set_the_format():
    rows = pa.table({"revenue": decimals("1234.50")})
    block = {"type": "kpi", "metric": "revenue", "format": "currency:EUR"}

    tile = make_run([block], {"blocks[0]": rows}, formats={"blocks[0]": {"revenue": "number:2"}}).views[0]

    assert (tile.value, tile.compare, tile.format) == ("\u20ac1,234.50", None, "currency:EUR")


# --------------------------------------------------------------------------- a bar or a line


def test_a_chart_has_a_category_for_each_row_and_one_series_named_after_its_numbers():
    rows = pa.table({"city": ["Pune", "Delhi"], "revenue": decimals("520.25", "410.00")})

    chart = view_of({"type": "bar", "query": "q", "x": "city", "y": "revenue", "format": "currency:USD"}, rows)

    assert isinstance(chart, Chart) and chart.kind == "bar"
    assert chart.categories == ("Pune", "Delhi") and [item.name for item in chart.series] == ["revenue"]
    assert chart.series[0].values == (520.25, 410.0) and chart.series[0].texts == ("$520.25", "$410.00")
    assert chart.series[0].raw == (D("520.25"), D("410.00"))
    assert chart.headers == ("city", "revenue")
    assert chart.table == (("Pune", "$520.25"), ("Delhi", "$410.00"))
    assert (chart.title, chart.x, chart.y, chart.series_column, chart.rows, chart.total_rows, chart.notes,
            chart.result) == ("revenue by city", "city", "revenue", None, 2, 2, (), "q")


def test_a_chart_is_called_by_its_title_or_by_its_numbers_and_its_axis():
    rows = pa.table({"city": ["Pune"], "k": ["a"], "n": [1]})

    assert view_of({"type": "line", "query": "q", "x": "city", "y": "n"}, rows).title == "n by city"
    keyed = {"type": "line", "query": "q", "x": "city", "y": "n", "series": "k"}
    assert view_of(keyed, rows).title == "n by city and k"
    assert view_of({"type": "line", "query": "q", "x": "city", "y": "n", "title": "Orders"}, rows).title == "Orders"


def test_a_series_column_splits_a_chart_into_series_in_the_order_they_first_appear():
    rows = pa.table({"day": ["mon", "mon", "tue", "wed"], "city": ["Pune", "Delhi", "Delhi", "Pune"],
                     "n": [1, 2, 3, 4]})

    chart = view_of({"type": "line", "query": "q", "x": "day", "y": "n", "series": "city"}, rows)

    assert chart.categories == ("mon", "tue", "wed")
    assert [(item.name, item.values) for item in chart.series] == [("Pune", (1.0, None, 4.0)),
                                                                   ("Delhi", (2.0, 3.0, None))]
    assert chart.headers == ("day", "Pune", "Delhi")
    assert chart.table == (("mon", "1", "2"), ("tue", "-", "3"), ("wed", "4", "-"))


def test_a_chart_of_one_series_column_with_one_value_still_names_the_series_in_its_table():
    rows = pa.table({"day": ["mon", "tue"], "city": ["Pune", "Pune"], "n": [1, 2]})

    chart = view_of({"type": "bar", "query": "q", "x": "day", "y": "n", "series": "city"}, rows)

    assert chart.headers == ("day", "Pune")


def test_a_chart_along_dates_puts_them_in_order_and_writes_them_as_dates():
    rows = pa.table({"day": [dt.date(2026, 8, 2), dt.date(2026, 7, 31), dt.date(2026, 8, 1)], "n": [3, 1, 2]})

    chart = view_of({"type": "line", "query": "q", "x": "day", "y": "n"}, rows)

    assert chart.categories == ("2026-07-31", "2026-08-01", "2026-08-02")
    assert chart.series[0].values == (1.0, 2.0, 3.0)


def test_a_chart_along_times_puts_them_in_order_too():
    rows = pa.table({"at": pa.array([dt.datetime(2026, 8, 1, 12), dt.datetime(2026, 8, 1, 3)], pa.timestamp("us")),
                     "n": [2, 1]})

    chart = view_of({"type": "line", "query": "q", "x": "at", "y": "n"}, rows)

    assert chart.categories == ("2026-08-01 03:00:00", "2026-08-01 12:00:00") and chart.series[0].values == (1.0, 2.0)


def test_a_chart_along_names_keeps_the_order_of_the_query_which_is_the_order_by_of_its_author():
    rows = pa.table({"city": ["Pune", "Delhi", "Agra"], "n": [1, 2, 3]})

    assert view_of({"type": "bar", "query": "q", "x": "city", "y": "n"}, rows).categories == ("Pune", "Delhi", "Agra")


def test_rows_that_have_the_same_place_along_the_axis_are_added_up_and_the_chart_says_so():
    rows = pa.table({"city": ["Pune", "Pune", "Delhi"], "n": [1, 2, 5]})

    chart = view_of({"type": "bar", "query": "q", "x": "city", "y": "n"}, rows)

    assert chart.categories == ("Pune", "Delhi") and chart.series[0].values == (3.0, 5.0)
    assert chart.notes == ("Some rows have the same value along the axis, so their numbers are added up.",)


def test_a_missing_place_along_a_dated_axis_keeps_the_order_of_the_query_and_does_not_fail():
    rows = pa.table({"day": pa.array([dt.date(2026, 8, 2), None, dt.date(2026, 7, 31)], pa.date32()), "n": [3, 2, 1]})

    chart = view_of({"type": "line", "query": "q", "x": "day", "y": "n"}, rows)

    assert chart.categories == ("2026-08-02", "-", "2026-07-31") and chart.series[0].values == (3.0, 2.0, 1.0)


def test_a_missing_number_does_not_erase_the_number_of_a_row_that_has_the_same_place():
    rows = pa.table({"x": ["a", "a", "b", "b", "c", "c"], "n": pa.array([5, None, None, 7, None, None], pa.int64())})

    chart = view_of({"type": "bar", "query": "q", "x": "x", "y": "n"}, rows)

    assert chart.series[0].values == (5.0, 7.0, None) and chart.series[0].texts == ("5", "7", "-")
    assert chart.notes == ()  # nothing was added up, so there is nothing to say


def test_a_missing_number_is_a_gap_and_never_zero():
    rows = pa.table({"city": ["Pune", "Delhi"], "n": pa.array([4, None], pa.int64())})

    chart = view_of({"type": "bar", "query": "q", "x": "city", "y": "n"}, rows)

    assert chart.series[0].values == (4.0, None) and chart.series[0].texts == ("4", "-")
    assert chart.series[0].raw == (4, None)


def test_a_chart_draws_the_first_500_rows_of_its_query_and_says_how_many_it_left_out():
    rows = pa.table({"n": list(range(600)), "v": [1] * 600})

    chart = view_of({"type": "line", "query": "q", "x": "n", "y": "v"}, rows)

    assert DEFAULT_MAX_POINTS == 500
    assert (chart.rows, chart.total_rows, len(chart.categories)) == (500, 600, 500)
    assert chart.categories[-1] == "499"
    assert chart.notes == ("The chart draws the first 500 of 600 rows. 100 rows are left out.",)


def test_the_counts_in_the_notes_have_thousands_separators_like_the_numbers_on_the_page():
    rows = pa.table({"n": list(range(1200)), "v": [1] * 1200})
    blocks = [{"type": "line", "query": "q", "x": "n", "y": "v", "max_points": 1000},
              {"type": "table", "query": "q", "max_rows": 10}, {"type": "kpi", "query": "q", "value": "v"}]

    chart, grid, tile = make_run(blocks, {"q": rows}, truncated=("q",), row_cap=1200).views

    capped = "The query has more rows than the row cap of 1,200, so this has the first 1,200."
    assert chart.notes == (capped, "The chart draws the first 1,000 of 1,200 rows. 200 rows are left out.")
    assert grid.notes == (capped, "The table shows the first 10 of 1,200 rows. 1,190 rows are left out.")
    assert tile.notes == (capped,)


def test_a_chart_may_set_how_many_rows_it_draws_and_a_single_row_left_out_is_said_in_the_singular():
    rows = pa.table({"n": [1, 2, 3, 4], "v": [1, 1, 1, 1]})

    chart = view_of({"type": "bar", "query": "q", "x": "n", "y": "v", "max_points": 3}, rows)

    assert (chart.rows, chart.total_rows) == (3, 4)
    assert chart.notes == ("The chart draws the first 3 of 4 rows. 1 row is left out.",)


def test_a_chart_of_exactly_as_many_rows_as_it_may_draw_leaves_nothing_out():
    rows = pa.table({"n": [1, 2, 3], "v": [1, 1, 1]})

    chart = view_of({"type": "bar", "query": "q", "x": "n", "y": "v", "max_points": 3}, rows)

    assert chart.notes == () and chart.rows == chart.total_rows == 3


def test_a_chart_draws_at_most_eight_series_and_says_how_many_it_left_out():
    count = MAX_SERIES + 2
    rows = pa.table({"x": ["a"] * count, "k": [f"k{n}" for n in range(count)], "v": list(range(count))})

    chart = view_of({"type": "bar", "query": "q", "x": "x", "y": "v", "series": "k"}, rows)

    assert MAX_SERIES == 8
    assert [item.name for item in chart.series] == [f"k{n}" for n in range(8)]
    assert chart.notes == ("The chart draws the first 8 of 10 series. 2 series are left out.",)
    one_more = view_of({"type": "bar", "query": "q", "x": "x", "y": "v", "series": "k"},
                       pa.table({"x": ["a"] * 9, "k": [f"k{n}" for n in range(9)], "v": list(range(9))}))
    assert one_more.notes == ("The chart draws the first 8 of 9 series. 1 series is left out.",)


def test_a_chart_with_exactly_eight_series_leaves_none_out():
    rows = pa.table({"x": ["a"] * 8, "k": [f"k{n}" for n in range(8)], "v": list(range(8))})

    assert view_of({"type": "bar", "query": "q", "x": "x", "y": "v", "series": "k"}, rows).notes == ()


def test_the_numbers_of_a_chart_are_written_by_one_format_so_the_column_agrees_with_itself():
    rows = pa.table({"x": ["a", "b"], "v": [1234.0, 0.5]})

    chart = view_of({"type": "bar", "query": "q", "x": "x", "y": "v", "format": "number"}, rows)

    assert chart.series[0].texts == ("1,234.00", "0.50")


def test_a_chart_of_a_metric_takes_its_format_from_the_metric_unless_the_block_sets_one():
    rows = pa.table({"city": ["Pune", "Delhi"], "revenue": decimals("520.25", "410.00")})
    formats = {"blocks[0]": {"revenue": "number:2"}}
    block = {"type": "bar", "metric": "revenue", "by": ["city"]}

    chart = make_run([block], {"blocks[0]": rows}, formats=formats).views[0]
    own = make_run([{**block, "format": "currency:USD"}], {"blocks[0]": rows}, formats=formats).views[0]

    assert (chart.x, chart.y, chart.series_column, chart.format) == ("city", "revenue", None, "number:2")
    assert chart.series[0].texts == ("520.25", "410.00") and own.series[0].texts == ("$520.25", "$410.00")
    assert chart.title == "revenue by city"


def test_the_columns_of_a_chart_of_a_metric_come_from_its_grain_and_its_dimensions():
    by_day = pa.table({"day": [dt.date(2026, 7, 30), dt.date(2026, 7, 31)] * 2, "city": ["Pune"] * 2 + ["Delhi"] * 2,
                       "revenue": decimals("1", "2", "3", "4")})
    by_two = pa.table({"city": ["Pune", "Pune", "Delhi"], "cuisine": ["Thai", "Italian", "Thai"],
                       "revenue": decimals("1", "2", "3")})

    line = make_run([{"type": "line", "metric": "revenue", "grain": "day", "by": "city"}], {"blocks[0]": by_day}
                    ).views[0]
    bars = make_run([{"type": "bar", "metric": "revenue", "by": ["city", "cuisine"]}], {"blocks[0]": by_two}).views[0]
    periods = make_run([{"type": "bar", "metric": "revenue", "grain": "day"}],
                       {"blocks[0]": by_day.select(["day", "revenue"]).slice(0, 2)}).views[0]

    assert (line.x, line.y, line.series_column) == ("day", "revenue", "city")
    assert [item.name for item in line.series] == ["Pune", "Delhi"]
    assert (bars.x, bars.series_column) == ("city", "cuisine") and [item.name for item in bars.series] == [
        "Thai", "Italian"]
    assert (periods.x, periods.series_column) == ("day", None)


def test_a_chart_of_a_query_with_no_rows_is_empty_and_not_an_error():
    rows = pa.table({"x": pa.array([], pa.string()), "v": pa.array([], pa.int64())})

    chart = view_of({"type": "bar", "query": "q", "x": "x", "y": "v"}, rows)

    assert chart.categories == () and chart.series[0].values == () and chart.table == () and chart.rows == 0
    assert "<svg" in str(chart_svg(chart, "c1"))


def test_a_chart_says_when_the_row_cap_cut_its_query():
    run = make_run([{"type": "bar", "query": "q", "x": "n", "y": "n"}], {"q": pa.table({"n": [1, 2]})},
                   truncated=("q",), row_cap=2)

    assert run.views[0].notes == ("The query has more rows than the row cap of 2, so this has the first 2.",)


def test_a_missing_category_is_a_dash_and_a_number_axis_is_written_as_it_is():
    rows = pa.table({"x": pa.array([None, 7], pa.int64()), "v": [1, 2]})

    chart = view_of({"type": "bar", "query": "q", "x": "x", "y": "v"}, rows)

    assert chart.categories == ("-", "7")


def test_a_chart_is_a_problem_when_a_column_it_names_is_not_in_its_query_and_the_problem_lists_the_columns():
    rows = pa.table({"city": ["Pune"], "n": [1]})

    assert problems_of({"type": "bar", "query": "q", "x": "town", "y": "n"}, rows) == [
        "metadata.blocks[0].x: the query 'q' has no column 'town'. Its columns are ['city', 'n']"]
    assert problems_of({"type": "bar", "query": "q", "x": "city", "y": "m", "series": "k"}, rows) == [
        "metadata.blocks[0].y: the query 'q' has no column 'm'. Its columns are ['city', 'n']",
        "metadata.blocks[0].series: the query 'q' has no column 'k'. Its columns are ['city', 'n']"]


def test_a_chart_is_a_problem_when_its_numbers_are_not_numbers():
    rows = pa.table({"city": ["Pune"], "n": ["many"]})

    assert problems_of({"type": "line", "query": "q", "x": "city", "y": "n"}, rows) == [
        "metadata.blocks[0].y: the column 'n' of the query 'q' is a string, and it has to hold numbers"]


@pytest.mark.parametrize("arrow", [pa.int8(), pa.uint32(), pa.int64(), pa.float32(), pa.float64(), pa.decimal128(10, 2),
                                   pa.null()])
def test_the_numbers_of_a_chart_may_be_any_integer_float_or_decimal_type_and_an_empty_column(arrow):
    rows = pa.table({"x": ["a"], "n": pa.array([None], arrow)})

    assert isinstance(view_of({"type": "bar", "query": "q", "x": "x", "y": "n"}, rows), Chart)


@pytest.mark.parametrize("arrow", [pa.bool_(), pa.string(), pa.date32(), pa.timestamp("us")])
def test_the_numbers_of_a_chart_may_not_be_booleans_text_or_dates(arrow):
    rows = pa.table({"x": ["a"], "n": pa.array([None], arrow)})

    found = problems_of({"type": "bar", "query": "q", "x": "x", "y": "n"}, rows)

    assert len(found) == 1 and found[0].endswith("and it has to hold numbers")


# --------------------------------------------------------------------------- a table


def test_a_table_shows_every_column_in_the_order_of_its_query_with_each_cell_written_out():
    rows = pa.table({"cuisine": ["Thai", "Italian"], "n": [3, 1], "share": [0.75, 0.25]})

    grid = view_of({"type": "table", "title": "Top cuisines", "query": "q", "formats": {"share": "percent:0"}}, rows)

    assert isinstance(grid, Grid)
    assert grid.headers == ("cuisine", "n", "share") and grid.title == "Top cuisines"
    assert grid.rows == (("Thai", "3", "75%"), ("Italian", "1", "25%"))
    assert grid.raw == (("Thai", 3, 0.75), ("Italian", 1, 0.25))
    assert grid.numeric == (1, 2) and (grid.total_rows, grid.result, grid.notes) == (2, "q", ())


def test_a_table_may_name_the_columns_to_show_and_their_order():
    rows = pa.table({"a": [1], "b": ["x"], "c": [2.5]})

    grid = view_of({"type": "table", "query": "q", "columns": ["c", "a"]}, rows)

    assert grid.headers == ("c", "a") and grid.rows == (("2.5", "1"),) and grid.raw == ((2.5, 1),)
    assert grid.title is None


def test_a_table_writes_each_column_by_its_own_format():
    rows = pa.table({"orders": [1234, 5], "revenue": decimals("1234.50", "5.00")})

    grid = view_of({"type": "table", "query": "q", "formats": {"orders": "integer", "revenue": "currency:USD"}}, rows)

    assert grid.rows == (("1,234", "$1,234.50"), ("5", "$5.00"))


def test_a_column_that_has_numbers_and_gaps_is_aligned_as_numbers_and_text_is_not():
    rows = pa.table({"a": pa.array([1, None], pa.int64()), "b": ["x", "y"], "c": pa.array([None, None], pa.int64()),
                     "d": [True, False], "e": [dt.date(2026, 8, 1)] * 2, "f": pa.array([1.5, None], pa.float64())})

    grid = view_of({"type": "table", "query": "q"}, rows)

    assert grid.numeric == (0, 5)
    assert grid.rows[1] == ("-", "y", "-", "false", "2026-08-01", "-")


def test_a_column_with_a_format_is_numbers_even_when_a_row_is_text():
    rows = pa.table({"a": ["12", "x"]})

    assert view_of({"type": "table", "query": "q", "formats": {"a": "integer"}}, rows).numeric == (0,)


def test_a_table_shows_the_first_100_rows_and_says_how_many_it_left_out():
    rows = pa.table({"n": list(range(150))})

    grid = view_of({"type": "table", "query": "q"}, rows)

    assert len(grid.rows) == 100 and grid.total_rows == 150 and grid.rows[-1] == ("99",)
    assert grid.notes == ("The table shows the first 100 of 150 rows. 50 rows are left out.",)


def test_a_table_may_set_how_many_rows_it_shows_and_one_row_left_out_is_said_in_the_singular():
    rows = pa.table({"n": [1, 2]})

    grid = view_of({"type": "table", "query": "q", "max_rows": 1}, rows)

    assert grid.rows == (("1",),)
    assert grid.notes == ("The table shows the first 1 of 2 rows. 1 row is left out.",)
    assert view_of({"type": "table", "query": "q", "max_rows": 2}, rows).notes == ()


def test_a_table_says_when_the_row_cap_cut_its_query_and_when_it_cut_its_own_rows():
    run = make_run([{"type": "table", "query": "q", "max_rows": 2}], {"q": pa.table({"n": [1, 2, 3]})},
                   truncated=("q",), row_cap=3)

    assert run.views[0].notes == (
        "The query has more rows than the row cap of 3, so this has the first 3.",
        "The table shows the first 2 of 3 rows. 1 row is left out.")


def test_a_table_is_a_problem_when_a_column_or_a_format_names_a_column_its_query_lacks():
    rows = pa.table({"a": [1], "b": [2]})

    assert problems_of({"type": "table", "query": "q", "columns": ["a", "zzz"]}, rows) == [
        "metadata.blocks[0].columns[1]: the query 'q' has no column 'zzz'. Its columns are ['a', 'b']"]
    assert problems_of({"type": "table", "query": "q", "formats": {"zzz": "integer", "b": "integer"}}, rows) == [
        "metadata.blocks[0].formats.zzz: the query 'q' has no column 'zzz'. Its columns are ['a', 'b']"]


def test_a_table_of_no_rows_has_its_headers_and_nothing_else():
    grid = view_of({"type": "table", "query": "q"}, pa.table({"a": pa.array([], pa.int64())}))

    assert grid.headers == ("a",) and grid.rows == () and grid.numeric == () and grid.notes == ()


# --------------------------------------------------------------------------- a text


def test_a_text_fills_its_placeholders_from_the_first_row_of_a_query_as_they_are():
    rows = pa.table({"orders": [1234], "revenue": decimals("520.25"), "city": ["R&D <b>"]})
    block = {"type": "text", "title": "Note",
             "text": "{q.orders} orders and {q.revenue} in {q.city}. Use {{braces}} for a brace."}

    words = view_of(block, rows)

    assert isinstance(words, Words) and words.title == "Note"
    assert words.paragraphs == ("1234 orders and 520.25 in R&D <b>. Use {braces} for a brace.",)


def test_a_text_is_split_into_paragraphs_at_blank_lines_and_its_spaces_are_made_single():
    block = {"type": "text", "text": "  One   line\nand a second.\n\n\n  Another   paragraph.  \n"}

    words = view_of(block, pa.table({"a": [1]}))

    assert words.paragraphs == ("One line and a second.", "Another paragraph.")
    assert words.title is None


def test_a_blank_line_that_has_spaces_in_it_still_ends_a_paragraph():
    words = view_of({"type": "text", "text": "One.\n   \t \nTwo."}, pa.table({"a": [1]}))

    assert words.paragraphs == ("One.", "Two.")


def test_a_placeholder_of_a_query_with_no_row_is_a_dash():
    block = {"type": "text", "text": "There were {q.n} orders."}

    assert view_of(block, pa.table({"n": pa.array([], pa.int64())})).paragraphs == ("There were - orders.",)


def test_a_placeholder_of_a_missing_value_is_a_dash():
    block = {"type": "text", "text": "There were {q.n} orders."}

    assert view_of(block, pa.table({"n": pa.array([None], pa.int64())})).paragraphs == ("There were - orders.",)


def test_a_placeholder_of_a_query_that_did_not_run_is_a_dash_and_the_query_has_the_problem():
    run = make_run([{"type": "text", "text": "There were {q.n} orders."}], {"q": pa.table({"n": [1]})})
    run.queries["q"].problem = "metadata.queries.q: it failed"

    views, found = resolve(run)

    assert found == [] and [type(item).__name__ for item in views] == ["Words"]
    assert views[0].paragraphs == ("There were - orders.",)


def test_a_placeholder_for_a_column_the_query_lacks_is_a_problem_that_is_told_once():
    block = {"type": "text", "text": "{q.zzz} and {q.zzz} and {q.a} and {q.yyy}"}

    assert problems_of(block, pa.table({"a": [1]})) == [
        "metadata.blocks[0].text: the query 'q' has no column 'zzz'. Its columns are ['a']",
        "metadata.blocks[0].text: the query 'q' has no column 'yyy'. Its columns are ['a']"]


# --------------------------------------------------------------------------- all of them


def test_the_views_are_in_the_order_of_the_blocks_and_a_block_whose_query_did_not_run_is_left_out():
    blocks = [{"type": "kpi", "query": "a", "value": "n"}, {"type": "table", "query": "b"},
              {"type": "text", "text": "Hello."}, {"type": "bar", "query": "a", "x": "n", "y": "n"}]
    run = make_run(blocks, {"a": pa.table({"n": [1]}), "b": pa.table({"m": [2]})})
    assert [type(item).__name__ for item in run.views] == ["Kpi", "Grid", "Words", "Chart"]
    assert [item.block.index for item in run.views] == [0, 1, 2, 3]

    run.queries["b"].problem = "metadata.queries.b: it failed"

    views, found = resolve(run)

    assert [item.block.index for item in views] == [0, 2, 3] and found == []


@pytest.mark.parametrize(("fmt", "top"), [
    (None, 400.0), ("integer", 400.0), ("number", 400.0), ("number:3", 400.0), ("percent", 0.5), ("percent:0", 0.5),
    ("currency:USD", 400.0), ("currency:JPY", 400.0), ("currency:KWD", 400.0),
])
def test_one_number_is_written_the_same_in_a_key_figure_on_a_chart_axis_and_in_a_table(fmt, top):
    rows = pa.table({"x": ["a", "b"], "v": [top, 0.0]})
    keyed = {"format": fmt} if fmt else {}
    blocks = [{"type": "kpi", "query": "q", "value": "v", **keyed},
              {"type": "bar", "query": "q", "x": "x", "y": "v", **keyed},
              {"type": "table", "query": "q", "columns": ["v"], "formats": {"v": fmt} if fmt else {}}]

    kpi, chart, grid = make_run(blocks, {"q": rows}).views

    text = format_value(top, fmt)
    assert kpi.value == text and chart.series[0].texts[0] == text and grid.rows[0][0] == text
    drawing = str(chart_svg(chart, "c1"))
    assert f'class="tick-y">{text}</text>' in drawing
    assert f'class="value-label">{text}</text>' in drawing
