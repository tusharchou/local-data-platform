"""What each block of a report shows, worked out from the rows of its query.

:func:`resolve` takes a :class:`~local_data_platform.report.engine.ReportRun` and gives one view for each block that can
be shown, with the values already written by the formats of :mod:`local_data_platform.report.formats`. The writers of
the HTML page, the Markdown and the JSON draw the views, so the three agree. A block that cannot be shown, because its
query has no column that it names or a column that has to hold numbers does not, is a problem that has the JSON path of
the key, and ``ldp report check`` finds it without building anything.

A ``kpi`` shows the first row of its query, or the last period of a metric, and compares it with the column ``compare``
or with the period before. A ``bar`` or a ``line`` draws the first ``max_points`` rows of its query, with at most
:data:`~local_data_platform.report.spec.MAX_SERIES` series, and says what it left out. A ``table`` shows the first
``max_rows`` rows, and a ``text`` fills its ``{query.column}`` placeholders from the first row of a query. Whatever a
block left out is in its ``notes``.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from local_data_platform.report.formats import change, change_text, format_values, is_number, plain
from local_data_platform.report.spec import MAX_SERIES, Block, fill_placeholders

if TYPE_CHECKING:
    from local_data_platform.report.engine import QueryRun, ReportRun


@dataclass(frozen=True)
class Kpi:
    """One number, with how it compares.

    Attributes:
        block: The block.
        title: What the number is called.
        value: The value as it is written, a ``-`` when the query has no row.
        raw: The value as it was computed.
        compare: The value to compare with, as it is written, or ``None``.
        raw_compare: The value to compare with, as it was computed.
        change: The change from the comparison as a percentage with its sign, such as ``+12.3%``, or ``None``.
        direction: ``up``, ``down`` or ``flat`` for the change, or ``None``.
        format: The format that wrote the number, or ``None``.
        result: The key of the rows in the run.
        notes: What it left out.
    """

    block: Block
    title: str
    value: str
    raw: Any
    compare: str | None
    raw_compare: Any
    change: str | None
    direction: str | None
    format: str | None
    result: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Series:
    """One series of a chart: its name and a value for each category.

    Attributes:
        name: The name, which is the value of the series column, or the name of the column that holds the numbers.
        values: The numbers to draw, ``None`` where the series has no value for a category.
        texts: The same values as they are written.
        raw: The same values as they were computed.
    """

    name: str
    values: tuple[float | None, ...]
    texts: tuple[str, ...]
    raw: tuple[Any, ...]


@dataclass(frozen=True)
class Chart:
    """A bar chart or a line chart.

    Attributes:
        block: The block.
        kind: ``bar`` or ``line``.
        title: What the chart is called.
        x: The column along the axis.
        y: The column that holds the numbers.
        series_column: The column that splits the chart into series, or ``None``.
        categories: The values along the axis, as they are written.
        series: The series, at most :data:`~local_data_platform.report.spec.MAX_SERIES`.
        format: The format that wrote the numbers, or ``None``.
        result: The key of the rows in the run.
        rows: How many rows of the query it draws.
        total_rows: How many rows the query returned.
        notes: What it left out.
    """

    block: Block
    kind: str
    title: str
    x: str
    y: str
    series_column: str | None
    categories: tuple[str, ...]
    series: tuple[Series, ...]
    format: str | None
    result: str
    rows: int
    total_rows: int
    notes: tuple[str, ...] = ()

    @property
    def headers(self) -> tuple[str, ...]:
        """The headings of the table that repeats the values of the chart."""
        if self.series_column is None and len(self.series) == 1:
            return (self.x, self.y)
        return (self.x, *(item.name for item in self.series))

    @property
    def table(self) -> tuple[tuple[str, ...], ...]:
        """The rows of the table that repeats the values of the chart."""
        return tuple((category, *(item.texts[place] for item in self.series))
                     for place, category in enumerate(self.categories))


@dataclass(frozen=True)
class Grid:
    """The rows of a table block.

    Attributes:
        block: The block.
        title: What the table is called, or ``None``.
        headers: The columns that it shows.
        rows: The rows that it shows, with each cell as it is written.
        raw: The same rows as they were computed.
        numeric: The positions of the columns that hold numbers, which are right-aligned.
        result: The key of the rows in the run.
        total_rows: How many rows the query returned.
        notes: What it left out.
    """

    block: Block
    title: str | None
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    raw: tuple[tuple[Any, ...], ...]
    numeric: tuple[int, ...]
    result: str
    total_rows: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Words:
    """The paragraphs of a text block, with their placeholders filled.

    Attributes:
        block: The block.
        title: What the text is called, or ``None``.
        paragraphs: The paragraphs.
    """

    block: Block
    title: str | None
    paragraphs: tuple[str, ...]


View = Kpi | Chart | Grid | Words


def _is_numeric_type(arrow_type: Any) -> bool:
    import pyarrow as pa

    return bool(pa.types.is_integer(arrow_type) or pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type)
                or pa.types.is_null(arrow_type))


def _number(value: Any) -> float | None:
    """The number to draw for a value, or ``None`` for a gap, which a value that is missing or not finite is."""
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _capped(run: ReportRun, item: QueryRun) -> list[str]:
    """The note for a query that the row cap cut, which every block that shows it repeats."""
    if not item.truncated:
        return []
    return [f"The query has more rows than the row cap of {run.row_cap:,}, so this has the first {item.rows:,}."]


def _left_out(count: int, one: str = "row", many: str = "rows") -> str:
    return f"{count:,} {one if count == 1 else many} {'is' if count == 1 else 'are'} left out."


def _check_columns(problems: list[str], where: str, item: QueryRun, query: str,
                   names: Sequence[tuple[str, str | None]], numbers: Sequence[tuple[str, str | None]] = ()) -> bool:
    """Whether every column that a block names is in its query, and the ones that hold numbers do. Adds the problems."""
    schema = item.table.schema
    ok = True
    for key, name in names:
        if name is not None and name not in schema.names:
            problems.append(f"{where}.{key}: the query {query!r} has no column {name!r}. Its columns are "
                            f"{list(schema.names)}")
            ok = False
    for key, name in numbers:
        if name is not None and name in schema.names and not _is_numeric_type(schema.field(name).type):
            problems.append(f"{where}.{key}: the column {name!r} of the query {query!r} is a "
                            f"{schema.field(name).type}, "
                            "and it has to hold numbers")
            ok = False
    return ok


def _kpi(run: ReportRun, block: Block, item: QueryRun, problems: list[str]) -> Kpi | None:
    where = f"metadata.blocks[{block.index}]"
    rows = item.table.to_pylist()
    if block.metric is not None:
        column, title = block.metric, block.title or block.metric
        fmt = block.format or item.formats.get(column)
        if block.grain and len(rows) > 1:
            current, previous = rows[-1], rows[-2]
        else:
            current, previous = (rows[-1] if rows else {}), {}
        value, other = current.get(column), previous.get(column)
    else:
        column, title = block.value, block.title or block.value
        if not _check_columns(problems, where, item, block.query, [("value", block.value), ("compare", block.compare)],
                              [("compare", block.compare)]):
            return None
        fmt = block.format
        current = rows[0] if rows else {}
        value, other = current.get(block.value), (current.get(block.compare) if block.compare else None)
    texts = format_values([value], fmt)
    written = None
    spelled = change_text(change(value, other))
    if other is not None:
        written = format_values([other], fmt)[0]
    return Kpi(block=block, title=title, value=texts[0], raw=value, compare=written, raw_compare=other,
               change=spelled[0] if spelled else None, direction=spelled[1] if spelled else None, format=fmt,
               result=block.result, notes=tuple(_capped(run, item)))


def _chart_columns(block: Block) -> tuple[str, str, str | None]:
    """The column along the axis, the column of the numbers and the column that splits the chart into series."""
    if block.metric is None:
        return block.x, block.y, block.series
    if block.grain:
        return block.grain, block.metric, (block.by[0] if block.by else None)
    return block.by[0], block.metric, (block.by[1] if len(block.by) > 1 else None)


def _chart(run: ReportRun, block: Block, item: QueryRun, problems: list[str]) -> Chart | None:
    where = f"metadata.blocks[{block.index}]"
    x, y, split = _chart_columns(block)
    if block.metric is None and not _check_columns(
            problems, where, item, block.query, [("x", x), ("y", y), ("series", split)], [("y", y)]):
        return None
    rows = item.table.to_pylist()
    shown = rows[:block.max_points]
    notes = _capped(run, item)
    if len(rows) > len(shown):
        notes.append(f"The chart draws the first {len(shown):,} of {len(rows):,} rows. "
                     f"{_left_out(len(rows) - len(shown))}")
    xs = [row[x] for row in shown]
    if all(isinstance(value, dt.date) for value in xs):  # a datetime is a date too, and times go in order
        order = sorted(range(len(shown)), key=lambda place: xs[place])
        shown = [shown[place] for place in order]
    categories: list[Any] = []
    for row in shown:
        if row[x] not in categories:
            categories.append(row[x])
    names: list[Any] = [] if split else [y]  # a chart with no series column has the one series, even without rows
    for row in shown:
        name = row[split] if split else y
        if name not in names:
            names.append(name)
    if len(names) > MAX_SERIES:
        notes.append(f"The chart draws the first {MAX_SERIES} of {len(names)} series. "
                     f"{_left_out(len(names) - MAX_SERIES, 'series', 'series')}")
        names = names[:MAX_SERIES]
    cells: dict[tuple[Any, Any], Any] = {}
    summed = False
    for row in shown:
        name = row[split] if split else y
        if name not in names:
            continue
        key = (row[x], name)
        value, before = row[y], cells.get((row[x], name))
        if before is not None and value is not None:
            summed = True
            value = before + value
        elif value is None:
            value = before  # a missing number does not erase the one that was there
        cells[key] = value
    if summed:
        notes.append("Some rows have the same value along the axis, so their numbers are added up.")
    fmt = block.format or item.formats.get(y)
    raws = [[cells.get((category, name)) for category in categories] for name in names]
    flat = format_values([value for series in raws for value in series], fmt)
    series = []
    for place, name in enumerate(names):
        texts = tuple(flat[place * len(categories):(place + 1) * len(categories)])
        series.append(Series(name=plain(name), values=tuple(_number(value) for value in raws[place]), texts=texts,
                             raw=tuple(raws[place])))
    title = block.title or (f"{y} by {x}" if split is None else f"{y} by {x} and {split}")
    return Chart(block=block, kind=block.type, title=title, x=x, y=y, series_column=split,
                 categories=tuple(plain(category) for category in categories), series=tuple(series), format=fmt,
                 result=block.result, rows=len(shown), total_rows=len(rows), notes=tuple(notes))


def _grid(run: ReportRun, block: Block, item: QueryRun, problems: list[str]) -> Grid | None:
    where = f"metadata.blocks[{block.index}]"
    available = list(item.table.schema.names)
    columns = list(block.columns) if block.columns is not None else available
    bad = False
    for place, name in enumerate(block.columns or ()):
        if name not in available:
            problems.append(f"{where}.columns[{place}]: the query {block.query!r} has no column {name!r}. Its columns "
                            f"are {available}")
            bad = True
    for name in block.formats:
        if name not in available:
            problems.append(f"{where}.formats.{name}: the query {block.query!r} has no column {name!r}. Its columns "
                            f"are {available}")
            bad = True
    if bad:
        return None
    rows = item.table.to_pylist()
    shown = rows[:block.max_rows]
    notes = _capped(run, item)
    if len(rows) > len(shown):
        notes.append(f"The table shows the first {len(shown):,} of {len(rows):,} rows. "
                     f"{_left_out(len(rows) - len(shown))}")
    written: list[list[str]] = []
    numeric: list[int] = []
    cells = []
    for place, column in enumerate(columns):
        fmt = block.formats.get(column)
        values = [row[column] for row in shown]
        cells.append(format_values(values, fmt))
        holds_numbers = (all(value is None or is_number(value) for value in values)
                         and any(value is not None for value in values))
        if fmt is not None or holds_numbers:  # a format is a number format, so a column that has one is numbers
            numeric.append(place)
    for line in range(len(shown)):
        written.append([cells[place][line] for place in range(len(columns))])
    return Grid(block=block, title=block.title, headers=tuple(columns), rows=tuple(tuple(row) for row in written),
                raw=tuple(tuple(row[column] for column in columns) for row in shown), numeric=tuple(numeric),
                result=block.result, total_rows=len(rows), notes=tuple(notes))


def _words(run: ReportRun, block: Block, problems: list[str]) -> Words | None:
    where = f"metadata.blocks[{block.index}].text"
    missing: list[str] = []

    def lookup(query: str, column: str) -> str:
        item = run.queries.get(query)
        if item is None or not item.ok:
            return "-"
        if column not in item.table.schema.names:
            missing.append(f"{where}: the query {query!r} has no column {column!r}. Its columns are "
                           f"{list(item.table.schema.names)}")
            return "-"
        rows = item.table.slice(0, 1).to_pylist()
        return plain(rows[0][column]) if rows else "-"

    text = fill_placeholders(block.text or "", lookup)
    for message in dict.fromkeys(missing):
        problems.append(message)
    if missing:
        return None
    paragraphs = tuple(" ".join(part.split()) for part in re.split(r"\n\s*\n", text.strip()) if part.strip())
    return Words(block=block, title=block.title, paragraphs=paragraphs)


def resolve(run: ReportRun) -> tuple[list[View], list[str]]:
    """The views of the blocks of a run, in order, and the problems of the blocks that cannot be shown.

    A block whose query did not run is left out, because the run already has that problem.

    Returns:
        ``(views, problems)``. Each problem starts with the JSON path of the key of the block that is wrong.
    """
    views: list[View] = []
    problems: list[str] = []
    for block in run.report.blocks:
        if block.type == "text":
            view = _words(run, block, problems)
        else:
            item = run.queries.get(block.result)
            if item is None or not item.ok:
                continue
            view = {"kpi": _kpi, "bar": _chart, "line": _chart, "table": _grid}[block.type](run, block, item, problems)
        if view is not None:
            views.append(view)
    return views, problems


__all__ = ["Chart", "Grid", "Kpi", "Series", "View", "Words", "resolve"]
