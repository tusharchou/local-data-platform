"""Bar charts and line charts drawn as inline SVG, with axes, ticks and gridlines, and no dependency.

A chart is one ``<svg>`` element that scales to the width of its box. It has a ``<title>`` and a ``<desc>`` that name it
for a screen reader, and each bar and each point has a ``<title>`` that says its category, its series and its value, so
a reader who points at one sees the number. The page repeats every value in a table under the chart.

Colours are not written here. Every element has a class, such as ``bar s1``, and the stylesheet of
:mod:`local_data_platform.html` gives the classes their colours in the light and the dark theme, so the page keeps its
promise of a Content-Security-Policy that allows no style attribute. The markup is made with
:func:`local_data_platform.html.h`, so every label is escaped.

The numbers along the value axis are "nice": the ends and the step are 1, 2 or 5 times a power of ten, and the same
text of a format that a table has writes them. A bar chart always starts at zero. A line chart starts at zero when its
smallest value is not much above it, and at the smallest value otherwise.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from local_data_platform import html
from local_data_platform.report.formats import format_values

if TYPE_CHECKING:
    from local_data_platform.report.view import Chart

WIDTH = 640
HEIGHT = 340
TOP = 14
RIGHT = 16
BOTTOM = 52
MIN_LEFT = 40
CHARACTER = 7.2
"""How wide a character of a tick label is, in the units of the drawing, which sets the room for the value axis."""
LABEL_WIDTH = 78
"""The room that a label under the category axis needs, in the units of the drawing."""
LABEL_CHARACTERS = 14
"""How many characters of a category a label shows. The title of each bar has the whole text."""
LINE_MARKERS = 40
"""A line chart of more categories than this has no markers, only the line."""
VALUE_LABELS = 12
"""A bar chart of one series and no more categories than this writes the value over each bar."""
TICKS = 5


def _nice(value: float, rounding: bool) -> float:
    exponent = math.floor(math.log10(value))
    fraction = value / 10 ** exponent
    if rounding:
        nice = 1 if fraction < 1.5 else 2 if fraction < 3 else 5 if fraction < 7 else 10
    else:
        nice = 1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
    return nice * 10 ** exponent


def nice_scale(low: float, high: float, ticks: int = TICKS) -> tuple[float, float, float]:
    """The ends and the step of a value axis that holds ``low`` to ``high``, as 1, 2 or 5 times a power of ten.

    Args:
        low: The smallest value to draw.
        high: The largest value to draw.
        ticks: About how many ticks the axis should have.

    Returns:
        ``(start, stop, step)``, where ``start`` is at most ``low`` and ``stop`` at least ``high``. A range of one value
        gets a range around it.
    """
    if high < low:
        low, high = high, low
    if high == low:
        pad = abs(high) * 0.1 or 1.0
        low, high = low - pad, high + pad
    span = _nice(high - low, False)
    step = _nice(span / max(ticks - 1, 1), True)
    start = math.floor(round(low / step, 9)) * step
    stop = math.ceil(round(high / step, 9)) * step
    return round(start, 10), round(stop, 10), round(step, 10)


def tick_values(start: float, stop: float, step: float) -> list[float]:
    """The values of the ticks of an axis, from ``start`` to ``stop``."""
    count = int(round((stop - start) / step))
    return [round(start + number * step, 10) for number in range(count + 1)]


def _n(value: float) -> str:
    """A coordinate as short, fixed text, so that the same chart is the same bytes every time."""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _clip(text: str, width: int = LABEL_CHARACTERS) -> str:
    return text if len(text) <= width else text[:max(width - 3, 1)] + "..."


def _range(chart: Chart) -> tuple[float, float]:
    values = [value for series in chart.series for value in series.values if value is not None]
    low, high = (min(values), max(values)) if values else (0.0, 1.0)
    if low == high == 0:  # nothing but zeros has a scale of its own, from nothing to one
        return 0.0, 1.0
    if chart.kind == "bar":
        return min(low, 0.0), max(high, 0.0)
    if low >= 0 and low <= 0.4 * high:  # a line that starts near zero starts at zero
        return 0.0, high
    pad = (high - low) * 0.05
    return low - pad, high + pad


def _describe(chart: Chart) -> str:
    kind = "Bar chart" if chart.kind == "bar" else "Line chart"
    more = f" in {len(chart.series)} series" if len(chart.series) > 1 else ""
    return (f"{kind} of {chart.y} by {chart.x}, {len(chart.categories)} "
            f"{'value' if len(chart.categories) == 1 else 'values'}{more} along the axis. The numbers are in the table "
            "under the chart.")


def chart_svg(chart: Chart, uid: str) -> html.Raw:
    """The chart as one inline ``<svg>`` element.

    Args:
        chart: A bar chart or a line chart, from :func:`local_data_platform.report.view.resolve`.
        uid: An id that is not used elsewhere on the page, which names the parts that the title and the description of
            the chart are linked by.
    """
    low, high = _range(chart)
    start, stop, step = nice_scale(low, high)
    ticks = tick_values(start, stop, step)
    labels = format_values(ticks, chart.format)
    left = max(MIN_LEFT, math.ceil(8 + CHARACTER * max(len(label) for label in labels)))
    plot_left, plot_right = left, WIDTH - RIGHT
    plot_top, plot_bottom = TOP, HEIGHT - BOTTOM
    width, height = plot_right - plot_left, plot_bottom - plot_top

    def y_of(value: float) -> float:
        return plot_bottom - (value - start) / (stop - start) * height

    count = len(chart.categories)
    band = width / max(count, 1)
    zero = y_of(0.0) if start <= 0 <= stop else plot_bottom
    parts: list[html.Raw] = [html.h("title", chart.title, id=f"{uid}-t"),
                             html.h("desc", _describe(chart), id=f"{uid}-d")]
    for tick, label in zip(ticks, labels):
        parts.append(html.h("line", x1=_n(plot_left), x2=_n(plot_right), y1=_n(y_of(tick)), y2=_n(y_of(tick)),
                            class_="grid"))
        parts.append(html.h("text", label, x=_n(plot_left - 6), y=_n(y_of(tick) + 4), class_="tick-y"))
    parts.append(html.h("line", x1=_n(plot_left), x2=_n(plot_right), y1=_n(zero), y2=_n(zero), class_="axis"))
    every = max(1, math.ceil(count / max(1, int(width // LABEL_WIDTH))))
    for place, category in enumerate(chart.categories):
        if place % every == 0:
            parts.append(html.h("text", _clip(category), x=_n(plot_left + (place + 0.5) * band), y=_n(plot_bottom + 18),
                                class_="tick-x"))
    if chart.kind == "bar":
        parts += _bars(chart, plot_left, band, zero, y_of)
    else:
        parts += _lines(chart, plot_left, band, y_of)
    return html.h("svg", html.Raw("\n"), html.join(*parts), html.Raw("\n"), viewbox=f"0 0 {WIDTH} {HEIGHT}",
                  class_=f"chart {chart.kind}", role="img", aria_labelledby=f"{uid}-t {uid}-d", focusable="false")


def _point_title(chart: Chart, place: int, series_place: int) -> str:
    series = chart.series[series_place]
    name = f"{series.name}, " if len(chart.series) > 1 else ""
    return f"{chart.categories[place]}, {name}{series.texts[place]}"


def _bars(chart: Chart, plot_left: float, band: float, zero: float, y_of) -> list[html.Raw]:
    shown = len(chart.series)
    group = band * 0.8
    each = group / shown
    drawn = max(each - (1 if each > 3 else 0), 0.5)
    labelled = shown == 1 and len(chart.categories) <= VALUE_LABELS
    out: list[html.Raw] = []
    for series_place, series in enumerate(chart.series):
        for place, value in enumerate(series.values):
            if value is None:
                continue
            x = plot_left + place * band + (band - group) / 2 + series_place * each
            top, bottom = sorted((y_of(value), zero))
            out.append(html.h("rect", html.h("title", _point_title(chart, place, series_place)), x=_n(x), y=_n(top),
                              width=_n(drawn), height=_n(bottom - top), class_=f"bar s{series_place + 1}"))
            if labelled:
                where = top - 4 if value >= 0 else bottom + 12
                out.append(html.h("text", series.texts[place], x=_n(x + each / 2), y=_n(where), class_="value-label"))
    return out


def _lines(chart: Chart, plot_left: float, band: float, y_of) -> list[html.Raw]:
    markers = len(chart.categories) <= LINE_MARKERS
    out: list[html.Raw] = []
    for series_place, series in enumerate(chart.series):
        klass = f"s{series_place + 1}"
        runs: list[list[tuple[int, float, float]]] = []
        run: list[tuple[int, float, float]] = []
        for place, value in enumerate(series.values):
            if value is None:
                if run:
                    runs.append(run)
                run = []
            else:
                run.append((place, plot_left + (place + 0.5) * band, y_of(value)))
        if run:
            runs.append(run)
        joined = {place for points in runs if len(points) > 1 for place, _x, _y in points}
        for points in runs:
            if len(points) > 1:
                out.append(html.h("polyline", points=" ".join(f"{_n(x)},{_n(y)}" for _place, x, y in points),
                                  class_=f"line {klass}"))
        for points in runs:
            for place, x, y in points:
                if markers or place not in joined:  # a point with no line to it is always drawn
                    out.append(html.h("circle", html.h("title", _point_title(chart, place, series_place)), cx=_n(x),
                                      cy=_n(y), r="3.5", class_=f"dot {klass}"))
    return out


def legend(series: Sequence[str]) -> html.Raw:
    """The legend of a chart of several series, a list with a swatch of the colour of each. It is plain markup."""
    return html.h("ul", html.join(*[html.h("li", html.h("span", "", class_=f"swatch s{place + 1}"), " ", name)
                                    for place, name in enumerate(series)]), class_="legend")


__all__ = ["chart_svg", "legend", "nice_scale", "tick_values"]
