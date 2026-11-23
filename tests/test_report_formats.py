"""How a report writes a value and draws a chart: the formats, the axis and the inline SVG.

These are the pure parts of ``ldp report build``. A format is one function for a key figure, the axis of a chart and a
table, so the tests write down what each format gives for numbers that can be worked out by hand. The charts are checked
as markup, which is what the page holds, and as the promises that the page makes about it, which are a title for a
screen reader, every value written out, no colour in an attribute and nothing that is not escaped.
"""

import datetime as dt
import math
import re
from decimal import Decimal as D

import pyarrow as pa
import pytest

from local_data_platform import html
from local_data_platform.report import formats, svg
from local_data_platform.report.formats import (
    change,
    change_text,
    format_value,
    format_values,
    is_number,
    metric_format,
    parse_format,
    plain,
)
from local_data_platform.report.spec import FORMAT_PATTERN, MAX_SERIES
from local_data_platform.report.svg import chart_svg, legend, nice_scale, tick_values
from tests.report_support import decimals, make_run

# --------------------------------------------------------------------------- the formats


@pytest.mark.parametrize("text", ["integer", "number", "number:0", "number:2", "percent", "percent:3", "currency:USD",
                                  "currency:JPY"])
def test_every_format_that_the_spec_accepts_parses(text):
    assert parse_format(text).kind == text.partition(":")[0]


@pytest.mark.parametrize("text", ["", "int", "number:", "number:x", "percent:-1", "currency", "currency:usd",
                                  "currency:US", "integer:2", " number", "number ", None, 3])
def test_text_that_is_not_a_format_is_refused_with_the_choices(text):
    with pytest.raises(ValueError, match="integer, number, number:N, percent, percent:N or currency:CODE"):
        parse_format(text)


def test_the_parser_and_the_spec_agree_on_what_a_format_is():
    for text in ["integer", "number", "number:12", "percent", "percent:0", "currency:EUR", "currency:ABC",
                 "number:100", "currency:AB", "currency:ABCD", "Number", "percent:1.5"]:
        assert bool(re.fullmatch(FORMAT_PATTERN, text)) == _parses(text), text


def _parses(text):
    try:
        parse_format(text)
    except ValueError:
        return False
    return True


def test_a_format_of_none_writes_a_value_as_it_is():
    assert format_values([1234, 0.5, D("20.00"), "x", None], None) == ["1234", "0.5", "20.00", "x", "-"]


@pytest.mark.parametrize(("value", "fmt", "text"), [
    (1234.5, "integer", "1,235"),
    (-1234.5, "integer", "-1,235"),
    (2.5, "integer", "3"),
    (-2.5, "integer", "-3"),
    (0.49, "integer", "0"),
    (1234.5, "number:2", "1,234.50"),
    (1234.5, "number:0", "1,235"),
    (0.125, "number:2", "0.13"),
    (D("1.005"), "number:2", "1.01"),
    (-0.001, "number:2", "0.00"),
    (10 ** 30, "integer", "1,000,000,000,000,000,000,000,000,000,000"),
    (0.1234, "percent", "12.3%"),
    (0.1234, "percent:0", "12%"),
    (0.1234, "percent:2", "12.34%"),
    (-0.05, "percent", "-5.0%"),
    (1.5, "percent:2", "150.00%"),
    (0, "percent", "0.0%"),
    (1234.5, "currency:USD", "$1,234.50"),
    (-1234.5, "currency:USD", "-$1,234.50"),
    (1234.5, "currency:EUR", "\u20ac1,234.50"),
    (1234.5, "currency:JPY", "\u00a51,235"),
    (1234.5678, "currency:KWD", "KWD 1,234.568"),
    (1234.5, "currency:CHF", "CHF 1,234.50"),
    (1234.5, "currency:VND", "\u20ab1,235"),
    (1234.5, "currency:BRL", "R$1,234.50"),
    (1234.5, "currency:CAD", "CA$1,234.50"),
    (1234.5, "currency:AUD", "A$1,234.50"),
    (1234.5, "currency:MXN", "MX$1,234.50"),
    (1234.5, "currency:INR", "\u20b91,234.50"),
])
def test_a_format_writes_a_number_as_a_person_reads_it(value, fmt, text):
    assert format_value(value, fmt) == text


def test_a_number_with_no_digits_named_takes_none_when_the_whole_column_is_whole_and_two_otherwise():
    assert format_values([1234, 1234.0, D("5.00")], "number") == ["1,234", "1,234", "5"]
    assert format_values([1234.5, 2, D("3.10")], "number") == ["1,234.50", "2.00", "3.10"]
    assert format_value(1234, "number") == "1,234"
    assert format_value(1234.5, "number") == "1,234.50"


def test_a_value_that_is_not_a_number_is_written_as_it_is_whatever_the_format():
    nan, inf = float("nan"), float("inf")
    assert format_values(["abc", True, nan, -inf, None], "integer") == ["abc", "true", "nan", "-inf", "-"]
    assert format_values([dt.date(2026, 8, 1)], "currency:USD") == ["2026-08-01"]
    assert format_values([nan, 1234.5], "number") == ["nan", "1,234.50"]


def test_a_value_is_a_number_only_if_it_is_a_finite_int_float_or_decimal():
    assert [is_number(item) for item in (1, 1.5, D("1.5"), 0, -0.0)] == [True] * 5
    assert [is_number(item) for item in (True, False, None, "1", float("nan"), float("inf"), D("NaN"), D("Infinity"),
                                         [1], dt.date(2026, 8, 1))] == [False] * 10


@pytest.mark.parametrize(("value", "text"), [
    (None, "-"), (True, "true"), (False, "false"), (1.5, "1.5"), (1.0, "1"), (0.0, "0"), (D("20.00"), "20.00"),
    (D("1E+3"), "1000"), (1234567, "1234567"), (1.23456789, "1.234568"), (float("nan"), "nan"), ("text", "text"),
    (dt.datetime(2026, 8, 1, 3, 0), "2026-08-01 03:00:00"), (dt.date(2026, 8, 1), "2026-08-01"),
    (dt.datetime(2026, 8, 1, 3, 0, 5, 123456), "2026-08-01 03:00:05"),
])
def test_a_value_with_no_format_is_written_as_it_is(value, text):
    assert plain(value) == text


def test_the_formats_of_a_metric_are_the_formats_that_write_it():
    assert [metric_format(name) for name in ("number", "percent", "currency")] == ["number", "percent:1", "number:2"]
    assert metric_format(None) is None and metric_format("") is None and metric_format("weight") is None
    for text in formats.METRIC_FORMATS.values():
        parse_format(text)


def test_the_symbols_of_the_currencies_are_written_with_escapes_so_that_the_source_is_plain_text():
    source = formats.__file__
    with open(source, encoding="utf-8") as handle:
        assert handle.read().isascii()
    assert formats.SYMBOLS["EUR"] == "\u20ac" and formats.SYMBOLS["GBP"] == "\u00a3"
    assert set(formats.ZERO_DECIMALS) >= {"JPY", "KRW", "VND"} and set(formats.THREE_DECIMALS) >= {"KWD", "BHD"}


def test_a_change_is_the_fraction_that_a_number_moved_from_the_one_to_compare_with():
    assert change(110, 100) == pytest.approx(0.1) and change(90, 100) == pytest.approx(-0.1)
    assert change(-50, -100) == pytest.approx(0.5)  # from minus 100 up to minus 50 is up by half of 100
    assert change(D("1.1"), D("1.0")) == pytest.approx(0.1)


@pytest.mark.parametrize(("value", "compare"), [
    (1, 0), (0, 0), (1, None), (None, 1), ("a", 1), (1, "a"), (True, 1), (float("nan"), 1), (1, float("inf"))])
def test_there_is_no_change_from_nothing_or_from_what_is_not_a_number(value, compare):
    assert change(value, compare) is None


@pytest.mark.parametrize(("fraction", "words"), [
    (0.1223, ("+12.2%", "up")), (-0.1, ("-10.0%", "down")), (0.0004, ("0.0%", "flat")), (-0.0004, ("0.0%", "flat")),
    (0.0, ("0.0%", "flat")), (0.00049, ("0.0%", "flat")), (0.0005, ("+0.1%", "up")), (-0.0005, ("-0.1%", "down")),
    (12.5, ("+1,250.0%", "up")), (None, None),
])
def test_a_change_is_written_with_its_sign_and_a_direction_and_rounding_to_zero_is_flat(fraction, words):
    assert change_text(fraction) == words


# --------------------------------------------------------------------------- the axis


@pytest.mark.parametrize(("low", "high", "scale"), [
    (0, 100, (0, 100, 20)), (0, 97, (0, 100, 20)), (0, 7, (0, 8, 2)), (1234, 5678, (1000, 6000, 1000)),
    (-5, 12, (-5, 15, 5)), (0, 1, (0.0, 1.0, 0.2)), (0.0, 0.0031, (0.0, 0.004, 0.001)), (-40, -10, (-40, -10, 10)),
    (3, 3, (2.6, 3.4, 0.2)), (0, 0, (-1.0, 1.0, 0.5)), (10, 5, (5, 10, 1)),
])
def test_the_axis_starts_and_ends_on_a_round_number_and_steps_by_one_two_or_five(low, high, scale):
    assert nice_scale(low, high) == scale


def test_the_axis_always_holds_the_values_and_steps_by_1_2_or_5_times_a_power_of_ten():
    for low in (-1e6, -250, -3, -0.07, 0, 0.003, 4, 99, 1234):
        for span in (0, 1e-4, 0.02, 0.9, 3, 17, 250, 12345, 3e7):
            high = low + span
            start, stop, step = nice_scale(low, high)
            assert start <= low + 1e-9 * max(1, abs(low)) and stop >= high - 1e-9 * max(1, abs(high)), (low, high)
            mantissa = step / 10 ** math.floor(math.log10(step))
            assert round(mantissa, 6) in (1, 2, 5), (low, high, step)
            assert 2 <= len(tick_values(start, stop, step)) <= 12, (low, high)


def test_the_ticks_of_an_axis_have_no_noise_of_floating_point():
    assert tick_values(0, 100, 20) == [0, 20, 40, 60, 80, 100]
    assert tick_values(0.0, 1.0, 0.2) == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    assert tick_values(-0.3, 0.3, 0.1) == [-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3]


# --------------------------------------------------------------------------- the charts


def chart_of(rows, kind="bar", fmt=None, **block):
    """The chart that a block of a report draws from a query of two or three columns, with its run."""
    spec = {"type": kind, "query": "q", "x": "x", "y": "y", **({"format": fmt} if fmt else {}), **block}
    run = make_run([spec], {"q": rows})
    assert run.ok, run.problems
    return run.views[0]


def numbers(xs, ys, kind="bar", fmt=None):
    return chart_of(pa.table({"x": xs, "y": pa.array(ys, pa.float64())}), kind, fmt)


def test_a_bar_chart_is_one_svg_with_a_title_a_description_and_a_title_on_every_bar():
    chart = chart_of(pa.table({"x": ["Pune", "Delhi", "Mumbai"], "y": decimals("520.25", "410.00", "304.25")}),
                     fmt="currency:USD")

    drawing = str(chart_svg(chart, "c1"))

    assert drawing.startswith("<svg ") and drawing.endswith("</svg>") and drawing.count("<svg") == 1
    assert 'role="img"' in drawing and 'aria-labelledby="c1-t c1-d"' in drawing and 'focusable="false"' in drawing
    assert '<title id="c1-t">y by x</title>' in drawing
    assert ('<desc id="c1-d">Bar chart of y by x, 3 values along the axis. The numbers are in the table under the '
            'chart.</desc>') in drawing
    assert drawing.count('<rect ') == 3 and drawing.count("<title") == 4
    assert "<title>Pune, $520.25</title>" in drawing and "<title>Mumbai, $304.25</title>" in drawing
    assert 'viewbox="0 0 640 340"' in drawing and 'class="chart bar"' in drawing


def test_the_axis_of_a_chart_is_written_by_the_format_of_its_numbers():
    chart = chart_of(pa.table({"x": ["Pune", "Delhi"], "y": decimals("520.25", "410.00")}), fmt="currency:USD")
    drawing = str(chart_svg(chart, "c1"))

    labels = re.findall(r'<text [^>]*class="tick-y">([^<]*)</text>', drawing)

    assert labels == ["$0.00", "$200.00", "$400.00", "$600.00"]
    assert 'class="value-label">$520.25</text>' in drawing and 'class="value-label">$410.00</text>' in drawing


def test_a_bar_chart_starts_at_zero_and_a_negative_bar_hangs_from_the_zero_line():
    drawing = str(chart_svg(numbers(["a", "b", "c"], [1000.0, 1100.0, -50.0]), "c1"))

    labels = re.findall(r'class="tick-y">([^<]*)</text>', drawing)
    zero = float(re.search(r'<text x="\S+" y="(\S+)" class="tick-y">0</text>', drawing).group(1)) - 4
    bars = [(float(top), float(height)) for top, height in
            re.findall(r'<rect x="\S+" y="(\S+)" width="\S+" height="(\S+)" class="bar s1">', drawing)]

    assert labels == ["-500", "0", "500", "1000", "1500"]  # numbers with no format are written as they are
    assert [round(top + height, 1) for top, height in bars[:2]] == [round(zero, 1)] * 2  # they stand on the line
    assert bars[2][0] == pytest.approx(zero, abs=0.01)  # and a negative bar hangs from it
    assert bars[2][1] == pytest.approx(50 / 2000 * (svg.HEIGHT - svg.BOTTOM - svg.TOP), abs=0.01)


def test_a_bar_chart_of_nothing_but_zeros_has_a_scale_from_zero_to_one():
    drawing = str(chart_svg(numbers(["a", "b"], [0.0, 0.0]), "c1"))

    assert re.findall(r'class="tick-y">([^<]*)</text>', drawing) == ["0", "0.2", "0.4", "0.6", "0.8", "1"]


def test_a_line_chart_starts_at_zero_only_when_its_smallest_value_is_near_it():
    near = str(chart_svg(numbers(["a", "b", "c"], [10.0, 40.0, 25.0], "line"), "c1"))
    far = str(chart_svg(numbers(["a", "b", "c"], [1000.0, 1040.0, 1025.0], "line"), "c1"))

    assert re.findall(r'class="tick-y">([^<]*)</text>', near)[0] == "0"
    assert float(re.findall(r'class="tick-y">([^<]*)</text>', far)[0].replace(",", "")) > 900


def test_every_label_of_a_chart_is_escaped():
    chart = chart_of(pa.table({"x": ["<b>x</b>", 'a"b', "R&D"], "y": [1.0, 2.0, 3.0]}),
                     title="<script>alert(1)</script>")

    drawing = str(chart_svg(chart, "c1"))

    assert "<b>" not in drawing and "<script" not in drawing
    assert "&lt;b&gt;x&lt;/b&gt;" in drawing and "R&amp;D" in drawing
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in drawing


def test_a_long_category_is_clipped_in_its_label_and_whole_in_the_title_of_its_bar():
    name = "Pune Maharashtra India"
    drawing = str(chart_svg(numbers([name, "b"], [3.0, 4.0]), "c1"))

    assert f'class="tick-x">{name[:11]}...</text>' in drawing
    assert f"<title>{name}, 3</title>" in drawing and name not in re.sub(r"<title>[^<]*</title>", "", drawing)


def test_a_bar_chart_of_few_bars_and_one_series_writes_the_value_over_each_bar():
    few = str(chart_svg(numbers(list("abcdefghijkl"), [float(n) for n in range(1, 13)]), "c1"))
    many = str(chart_svg(numbers(list("abcdefghijklm"), [float(n) for n in range(1, 14)]), "c1"))

    assert few.count('class="value-label"') == 12 and many.count('class="value-label"') == 0


def test_the_labels_under_the_axis_are_thinned_so_that_they_do_not_run_into_each_other():
    drawing = str(chart_svg(numbers([f"day {n:02d}" for n in range(30)], [float(n) for n in range(30)], "line"), "c1"))

    labels = re.findall(r'class="tick-x">([^<]*)</text>', drawing)

    assert labels == ["day 00", "day 05", "day 10", "day 15", "day 20", "day 25"]  # at most 7 fit, so every 5th


def test_a_line_chart_draws_a_line_and_a_marker_on_each_point_while_it_has_few():
    drawing = str(chart_svg(numbers(["a", "b", "c"], [1.0, 3.0, 2.0], "line"), "c1"))

    assert drawing.count("<polyline") == 1 and drawing.count("<circle") == 3
    assert 'class="line s1"' in drawing and 'class="dot s1"' in drawing
    assert "<title>b, 3</title>" in drawing


def test_a_line_chart_of_more_than_40_points_has_the_line_and_no_markers():
    def drawn(count):
        chart = numbers([f"d{n}" for n in range(count)], [float(n % 7) for n in range(count)], "line")
        return str(chart_svg(chart, "c1"))

    assert drawn(41).count("<polyline") == 1 and drawn(41).count("<circle") == 0
    assert drawn(40).count("<polyline") == 1 and drawn(40).count("<circle") == 40


def test_a_gap_in_a_line_breaks_it_and_a_point_with_no_line_to_it_is_always_drawn():
    count = svg.LINE_MARKERS + 10
    values = [float(n) for n in range(count)]
    values[10] = None
    values[12] = None  # the point at 11 has no neighbour left
    chart = numbers([f"d{n}" for n in range(count)], values, "line")

    drawing = str(chart_svg(chart, "c1"))

    assert drawing.count("<polyline") == 2
    assert drawing.count("<circle") == 1 and "<title>d11, 11</title>" in drawing


def test_a_line_chart_of_one_point_is_one_marker_and_no_line():
    drawing = str(chart_svg(numbers(["only"], [5.0], "line"), "c1"))

    ticks = [float(item) for item in re.findall(r'class="tick-y">([^<]*)</text>', drawing)]
    assert drawing.count("<polyline") == 0 and drawing.count("<circle") == 1
    assert ticks[0] < 5 < ticks[-1]  # the scale is around the one value, which is the only place it can be read


def test_a_chart_with_no_value_to_draw_is_still_a_chart_with_a_scale():
    rows = pa.table({"x": ["a", "b"], "y": pa.array([None, None], pa.float64())})
    for kind in ("bar", "line"):
        drawing = str(chart_svg(chart_of(rows, kind), "c1"))

        assert drawing.count("<rect") == 0 and drawing.count("<circle") == 0 and drawing.count("<polyline") == 0
        assert "<title id=\"c1-t\">" in drawing and "tick-y" in drawing


def test_the_series_of_a_chart_have_a_colour_class_each_and_no_colour_in_an_attribute():
    rows = pa.table({"x": ["a", "b"] * MAX_SERIES,
                     "k": [f"k{n}" for n in range(MAX_SERIES) for _ in range(2)],
                     "y": [float(n) for n in range(2 * MAX_SERIES)]})
    for kind in ("bar", "line"):
        drawing = str(chart_svg(chart_of(rows, kind, series="k"), "c1"))

        for number in range(1, MAX_SERIES + 1):
            assert f's{number}"' in drawing
        assert "style=" not in drawing and "fill=" not in drawing and "stroke=" not in drawing
        assert "url(" not in drawing and "http" not in drawing and "xmlns" not in drawing


def test_every_series_colour_class_and_token_is_in_the_stylesheet_for_both_themes():
    light, dark = html.CSS.split("@media (prefers-color-scheme:dark)")[:2]

    for number in range(1, MAX_SERIES + 1):
        assert f"--s{number}:#" in light and f"--s{number}:#" in dark.split("*{box-sizing")[0]
        assert f".s{number}{{--c:var(--s{number})}}" in html.CSS


def test_the_bars_of_several_series_sit_side_by_side_inside_their_band_and_are_named_in_their_titles():
    rows = pa.table({"x": ["a", "a", "b", "b"], "k": ["east", "west", "east", "west"], "y": [1.0, 2.0, 3.0, 4.0]})

    drawing = str(chart_svg(chart_of(rows, series="k"), "c1"))

    where = {title: float(x) for x, title in re.findall(r'<rect x="(\S+)"[^>]*><title>([^<]*)</title>', drawing)}
    assert drawing.count("<rect") == 4
    assert where["a, east, 1"] < where["a, west, 2"] < where["b, east, 3"] < where["b, west, 4"]
    assert "<title>a, east, 1</title>" in drawing and "<title>b, west, 4</title>" in drawing
    assert drawing.count('class="value-label"') == 0
    assert "2 series" in drawing


def test_the_drawing_of_a_chart_is_the_same_text_every_time_and_has_short_coordinates():
    chart = numbers([f"d{n}" for n in range(9)], [1 / 3 * n for n in range(9)], "line")

    first, second = str(chart_svg(chart, "c1")), str(chart_svg(chart, "c1"))

    assert first == second
    assert not re.search(r"\d\.\d{3,}", re.sub(r"<title>[^<]*</title>", "", first))


def test_a_chart_of_one_value_says_value_and_not_values():
    drawing = str(chart_svg(numbers(["a"], [3.0]), "c1"))

    assert "1 value along the axis" in drawing and "1 values" not in drawing


def test_the_legend_has_a_swatch_in_the_class_of_each_series_and_escapes_the_names():
    markup = str(legend(["east", "<i>west</i>", "R&D"]))

    assert markup.count("<li>") == 3 and 'class="legend"' in markup
    assert 'class="swatch s1"' in markup and 'class="swatch s2"' in markup and 'class="swatch s3"' in markup
    assert "<i>" not in markup and "&lt;i&gt;west&lt;/i&gt;" in markup and "R&amp;D" in markup


# --------------------------------------------------------------------------- what the numbers of a drawing are


@pytest.mark.parametrize(("low", "high", "ticks", "scale"), [
    (0, 5, 4, (0, 6, 2)), (0, 5, 8, (0, 5, 1)), (0, 100, 2, (0, 100, 100)), (0, 100, 3, (0, 100, 50)),
    (0, 100, 11, (0, 100, 10)),
])
def test_the_axis_has_about_as_many_ticks_as_asked_for_and_a_tick_less_intervals(low, high, ticks, scale):
    assert nice_scale(low, high, ticks) == scale


def test_a_coordinate_is_at_most_two_decimals_and_never_a_negative_zero():
    assert [svg._n(value) for value in (0, 0.0, -0.001, -0.004, 12.3456, 100.0, -2.5, 1e-9)] == [
        "0", "0", "0", "0", "12.35", "100", "-2.5", "0"]


def test_a_label_of_14_characters_is_whole_and_one_of_15_is_cut_to_11_and_three_dots():
    drawing = str(chart_svg(numbers(["a" * 14, "b" * 15], [1.0, 2.0]), "c1"))

    assert re.findall(r'class="tick-x">([^<]*)</text>', drawing) == ["a" * 14, "b" * 11 + "..."]


def test_a_line_chart_of_negative_numbers_has_an_axis_that_holds_them():
    drawing = str(chart_svg(numbers(["a", "b", "c"], [-30.0, -10.0, -20.0], "line"), "c1"))

    assert re.findall(r'class="tick-y">([^<]*)</text>', drawing) == ["-40", "-30", "-20", "-10", "0"]


def test_bars_are_a_pixel_narrower_than_their_room_when_there_is_room_and_fill_it_when_there_is_none():
    rows = pa.table({"x": [f"c{n}" for n in range(40) for _ in range(8)],
                     "k": [f"s{m}" for _ in range(40) for m in range(8)], "y": [1.0] * 320})
    few = pa.table({"x": ["a", "a", "b", "b"], "k": ["e", "w", "e", "w"], "y": [1.0, 2.0, 3.0, 4.0]})

    crowded = str(chart_svg(chart_of(rows, series="k"), "c1"))
    roomy = str(chart_svg(chart_of(few, series="k"), "c1"))

    assert set(re.findall(r'<rect x="\S+" y="\S+" width="(\S+)"', crowded)) == {"1.46"}
    assert set(re.findall(r'<rect x="\S+" y="\S+" width="(\S+)"', roomy)) == {"115.8"}


def test_the_value_of_a_bar_is_written_over_it_and_under_a_bar_that_hangs_down_and_over_a_bar_of_nothing():
    drawing = str(chart_svg(numbers(["neg", "zero", "pos"], [-5.0, 0.0, 5.0]), "c1"))

    bars = {title: (float(top), float(height)) for top, height, title in
            re.findall(r'<rect x="\S+" y="(\S+)" width="\S+" height="(\S+)" class="bar s1"><title>([^<]*)', drawing)}
    labels = {text: float(y) for y, text in re.findall(r'<text x="\S+" y="(\S+)" class="value-label">([^<]*)', drawing)}

    assert labels["5"] == pytest.approx(bars["pos, 5"][0] - 4, abs=0.01)  # over the bar
    assert labels["-5"] == pytest.approx(sum(bars["neg, -5"]) + 12, abs=0.01)  # under the bar that hangs down
    assert bars["zero, 0"][1] == 0 and labels["0"] == pytest.approx(bars["zero, 0"][0] - 4, abs=0.01)
