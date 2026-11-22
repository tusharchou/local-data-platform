"""Report specs: what a report file says, checked without reading any table.

A report file is a spec of the kind ``Report``. It names the tables a report reads, the queries it runs and the blocks
it shows, so that a report is reviewed, versioned and checked like any other ``ldp`` file::

    {
      "apiVersion": "ldp/v1",
      "kind": "Report",
      "identifier": "daily_sales",
      "who": "owner@example.com",
      "what": "Orders, revenue and the average ticket of one day",
      "metadata": {
        "title": "Daily sales",
        "timezone": "Asia/Kolkata",
        "tables": {"config": ["../config"], "allow": ["gold.*"]},
        "queries": {
          "orders": {"sql": "SELECT sum(orders) AS n FROM gold_daily_sales WHERE order_date >= $window_start"},
          "by_restaurant": {"sql_file": "queries.sql"}
        },
        "blocks": [
          {"type": "kpi", "metric": "revenue", "grain": "day"},
          {"type": "kpi", "query": "orders", "value": "n"},
          {"type": "bar", "title": "Revenue by restaurant", "query": "by_restaurant", "x": "restaurant", "y": "revenue",
           "format": "currency:INR"}
        ]
      }
    }

``tables`` takes what ``ldp mcp`` takes, which is ``config`` files or folders, ``catalog`` spec files and ``allow``
patterns, and the paths are relative to the folder of the file. A query is inline ``sql`` or a ``sql_file`` that has a
``-- name: <name>`` line for it, as ``examples/robot_episodes/queries.sql`` does. A ``kpi``, ``bar`` or ``line`` block
shows a query, or computes a ``metric`` that a metrics file of the ``config`` folders defines. The queries take the
named parameters ``$window_start`` and ``$window_end``, which are the window as wall-clock times in the report's
``timezone``, and the ``parameters`` that the file declares. Nothing is ever pasted into the SQL.

This module checks the shape of the file, and that the blocks refer to queries that exist. It reads no table and no
file, so it needs only the standard library, and :mod:`local_data_platform.spec` hands the ``Report`` kind to it.
:func:`read_report` reads the SQL files too, and :mod:`local_data_platform.report.engine` runs the queries.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.orchestration.windows import get_timezone
from local_data_platform.paths import resolve_path
from local_data_platform.spec import (
    API_VERSION,
    _check_secrets,
    _check_string,
    _Problems,
    kind_of,
    kind_of_file,
    validate_spec,
)

KIND = "Report"
"""The ``kind`` of a report file."""

REPORT_KEYS = ("apiVersion", "kind", "identifier", "who", "what", "where", "when", "how", "metadata")
"""The top-level keys of a report file."""

METADATA_KEYS = ("title", "tables", "queries", "blocks", "parameters", "timezone", "row_cap", "timeout_s", "freshness",
                 "delivery", "after", "observability")
"""The keys of ``metadata``. ``tables``, ``queries`` and ``blocks`` are required."""

TABLES_KEYS = ("config", "catalog", "allow")
QUERY_KEYS = ("sql", "sql_file", "description")
PARAMETER_KEYS = ("type", "default", "description")
FRESHNESS_KEYS = ("max_age_hours", "on_stale")
DESTINATION_KEYS = {
    "slack": ("type", "webhook_env", "retries"),
    "webhook": ("type", "url_env", "headers_env", "retries"),
    "file": ("type", "path", "public_url"),
}

BLOCK_TYPES = ("kpi", "bar", "line", "table", "text")
"""What a block shows: a number, bars, a line, rows, or some words."""

BLOCK_KEYS = {
    "kpi": ("type", "title", "query", "value", "compare", "metric", "by", "grain", "filters", "format"),
    "bar": ("type", "title", "query", "x", "y", "series", "metric", "by", "grain", "filters", "format"),
    "line": ("type", "title", "query", "x", "y", "series", "metric", "by", "grain", "filters", "format"),
    "table": ("type", "title", "query", "columns", "formats", "max_rows"),
    "text": ("type", "title", "text"),
}

QUERY_FORM_KEYS = {"kpi": ("value", "compare"), "bar": ("x", "y", "series"), "line": ("x", "y", "series")}
METRIC_FORM_KEYS = ("grain", "filters", "by")

PARAMETER_TYPES = ("string", "integer", "number", "boolean", "date")
"""What a declared parameter may be."""

ON_STALE = ("warn", "block")
DESTINATION_TYPES = tuple(DESTINATION_KEYS)

BUILTIN_PARAMETERS = ("window_start", "window_end")
"""The parameters that every query may use when a window is given. A report cannot declare them."""

GRAINS = ("day", "week", "month", "quarter", "year")
"""The grains of time a metric block may ask for, as in :mod:`local_data_platform.metrics.model`."""

DEFAULT_TIMEZONE = "UTC"
DEFAULT_ROW_CAP = 10_000
"""How many rows a query may return. A report shows the first rows of a long result and says how many it left out."""
MAX_ROW_CAP = 1_000_000
DEFAULT_TIMEOUT_S = 30.0
MAX_TIMEOUT_S = 3600.0
MAX_BLOCK_ROWS = 10_000
MAX_SQL_CHARS = 100_000

NAME_PATTERN = r"[a-z][a-z0-9_]{0,62}"
"""A name: lower case letters, digits and underscores, starting with a letter, at most 63 characters."""

FORMAT_PATTERN = r"integer|(number|percent)(:[0-6])?|currency:[A-Z]{3}"
"""How a number is written: ``integer``, ``number`` or ``percent`` with 0 to 6 decimals, or ``currency:`` and a code."""

_NAME = re.compile(NAME_PATTERN)
_FORMAT = re.compile(FORMAT_PATTERN)
_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SECTION = re.compile(r"--[ \t]*name:[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*")
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{[^{}]*\}|[{}]")
_REFERENCE = re.compile(r"([a-z][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)")
_INTEGER = re.compile(r"[+-]?[0-9]+")
_NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


# --------------------------------------------------------------------------- the definitions


@dataclass(frozen=True)
class Parameter:
    """A value that a query may use, and that the person who runs the report may change.

    Attributes:
        name: What the SQL calls it, as ``$name``.
        type: ``string``, ``integer``, ``number``, ``boolean`` or ``date``.
        default: The value when none is given, or ``None`` for a parameter that has to be given.
        description: What it is for, or ``None``.
    """

    name: str
    type: str
    default: Any = None
    description: str | None = None

    @property
    def required(self) -> bool:
        """Whether a value has to be given, because there is no default."""
        return self.default is None


@dataclass(frozen=True)
class Query:
    """One named query.

    Attributes:
        name: The name that blocks refer to it by.
        sql: The SQL. It is ``None`` until :func:`read_report` has read the file that holds it.
        sql_file: The file that holds the SQL, as the report wrote it, or ``None`` for inline SQL.
        description: What it answers, or ``None``.
    """

    name: str
    sql: str | None = None
    sql_file: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Block:
    """One thing a report shows.

    Which attributes apply depends on the ``type``, as :func:`validate_report` checks. A ``kpi``, ``bar`` or ``line``
    has either a ``query`` and the columns to show, or a ``metric``. A ``table`` has a ``query``, and a ``text`` has its
    words.

    Attributes:
        index: The position in ``blocks``, which names the block in problems and in ``ReportRun.queries``.
        type: ``kpi``, ``bar``, ``line``, ``table`` or ``text``.
        title: What the block is called, or ``None``.
        query: The query it shows.
        value: The column of a ``kpi`` that holds the number.
        compare: The column of a ``kpi`` that holds the number to compare with.
        x: The column of the categories or the times of a ``bar`` or ``line``.
        y: The column of its values.
        series: The column that splits it into series.
        metric: The metric that it computes instead of a query.
        grain: The grain of time of a metric.
        by: The dimensions of a metric to group by.
        filters: The ``dimension=value`` filters of a metric.
        format: How its numbers are written.
        columns: The columns of a ``table`` to show, in order, or ``None`` for all.
        formats: How the columns of a ``table`` are written, by column.
        max_rows: The rows of a ``table`` to show.
        text: The words of a ``text``, with ``{query.column}`` placeholders.
    """

    index: int
    type: str
    title: str | None = None
    query: str | None = None
    value: str | None = None
    compare: str | None = None
    x: str | None = None
    y: str | None = None
    series: str | None = None
    metric: str | None = None
    grain: str | None = None
    by: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    format: str | None = None
    columns: tuple[str, ...] | None = None
    formats: Mapping[str, str] = field(default_factory=dict)
    max_rows: int = 100
    text: str | None = None

    @property
    def result(self) -> str:
        """The key of the rows of this block in a run: the name of its query, or ``blocks[N]`` for a metric."""
        return self.query if self.query is not None else f"blocks[{self.index}]"


@dataclass(frozen=True)
class TableSources:
    """Where the tables of a report come from, which is what ``ldp mcp`` takes.

    Attributes:
        config: Dataset configs, or folders of them, as the report wrote them.
        catalog: Catalog spec files.
        allow: The patterns of the tables that may be read, or ``None`` for every table that is found.
    """

    config: tuple[str, ...] = ()
    catalog: tuple[str, ...] = ()
    allow: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Freshness:
    """How a report treats stale data.

    Attributes:
        max_age_hours: How old the data of a table may be, or ``None`` to use what the SLA of the table says.
        on_stale: ``warn`` shows a banner and ``block`` holds the report back.
    """

    max_age_hours: float | None = None
    on_stale: str = "warn"


@dataclass(frozen=True)
class Destination:
    """A place that a report is sent to.

    Attributes:
        name: What the report calls it.
        type: ``slack``, ``webhook`` or ``file``.
        options: The rest of what the report says, such as ``webhook_env`` or ``path``.
    """

    name: str
    type: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Report:
    """What a report file says, once :func:`validate_report` found no problem in it.

    Attributes:
        identifier: The id of the report.
        title: What it is called. The identifier when the file has no title.
        who: Who owns it, or ``None``.
        what: What it is for, or ``None``.
        where: Where it goes, or ``None``.
        when: How often it is made, or ``None``.
        how: How it is made, or ``None``.
        tables: Where its tables come from.
        queries: Its queries, in the order of the file.
        blocks: Its blocks, in the order of the file.
        parameters: The parameters that its queries may use.
        timezone: The time zone that its queries run in, an IANA name.
        row_cap: How many rows a query may return.
        timeout_s: How many seconds a query may run.
        freshness: How it treats stale data, or ``None``.
        delivery: Where it is sent.
        after: The pipelines that have to have run before the report is sent.
        observability: The ``observability`` block as the file wrote it, or ``None``.
        file: The file it was read from, or ``None``.
        base_dir: The folder that its paths are relative to.
    """

    identifier: str
    title: str
    who: str | None
    what: str | None
    where: str | None
    when: str | None
    how: str | None
    tables: TableSources
    queries: tuple[Query, ...]
    blocks: tuple[Block, ...]
    parameters: tuple[Parameter, ...] = ()
    timezone: str = DEFAULT_TIMEZONE
    row_cap: int = DEFAULT_ROW_CAP
    timeout_s: float = DEFAULT_TIMEOUT_S
    freshness: Freshness | None = None
    delivery: tuple[Destination, ...] = ()
    after: tuple[str, ...] = ()
    observability: Mapping[str, Any] | None = None
    file: Path | None = None
    base_dir: Path | None = None

    def query(self, name: str) -> Query | None:
        """The query called ``name``, or ``None``."""
        return next((item for item in self.queries if item.name == name), None)

    def parameter(self, name: str) -> Parameter | None:
        """The declared parameter called ``name``, or ``None``."""
        return next((item for item in self.parameters if item.name == name), None)

    @property
    def metric_blocks(self) -> tuple[Block, ...]:
        """The blocks that compute a metric instead of showing a query."""
        return tuple(item for item in self.blocks if item.metric is not None)


# --------------------------------------------------------------------------- checking a file


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) != float("inf")  # not NaN, and not infinite


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unknown_keys(problems: _Problems, where: str, block: Mapping[str, Any], allowed: Sequence[str],
                  noun: str = "") -> None:
    unknown = sorted(set(map(str, block)) - set(allowed))
    if unknown:
        problems.add(where, f"has unknown keys {unknown}; allowed{noun}: {list(allowed)}")


def _optional_text(problems: _Problems, where: str, block: Mapping[str, Any], key: str) -> None:
    if key in block and not _is_text(block[key]):
        problems.add(f"{where}.{key}", f"must be a non-empty string, got {block[key]!r}")


def _name_problem(value: Any) -> str | None:
    if not _NAME.fullmatch(value if isinstance(value, str) else ""):
        return (f"must be lower case letters, digits and underscores, starting with a letter, and at most 63 "
                f"characters, got {value!r}")
    return None


def _check_format(problems: _Problems, where: str, value: Any) -> None:
    if not isinstance(value, str) or not _FORMAT.fullmatch(value):
        problems.add(where, "must be integer, number, number:N or percent:N with N from 0 to 6, or currency:CODE with "
                            "a "
                            f"three letter code such as currency:USD, got {value!r}")


def _check_column(problems: _Problems, where: str, value: Any, *, required: bool = True) -> None:
    if value is None:
        if required:
            problems.add(where, "is required and must be the name of a column of the query")
    elif not isinstance(value, str) or not _COLUMN.fullmatch(value):
        problems.add(where, f"must be the name of a column of the query, got {value!r}")


def _check_tables(problems: _Problems, value: Any) -> None:
    where = "metadata.tables"
    if not isinstance(value, Mapping):
        problems.add(where, f"is required and must be an object with config, catalog and allow, as ldp mcp takes them, "
                            f"got {type(value).__name__}")
        return
    _unknown_keys(problems, where, value, TABLES_KEYS)
    for key in ("config", "catalog"):
        if key not in value:
            continue
        items = value[key]
        if not isinstance(items, list) or not items:
            problems.add(f"{where}.{key}", f"must be a list of at least one path, got {items!r}")
            continue
        for place, item in enumerate(items):
            if not _is_text(item):
                problems.add(f"{where}.{key}[{place}]", f"must be a path, got {item!r}")
    if not value.get("config") and not value.get("catalog"):
        problems.add(where, "needs config, which lists dataset configs or folders of them, or catalog, which lists "
                            "catalog spec files, so that there are tables to read")
    if "allow" in value:
        patterns = value["allow"]
        if not isinstance(patterns, list) or not patterns:
            problems.add(f"{where}.allow", f"must be a list of at least one table pattern such as 'gold.*', got "
                                           f"{patterns!r}")
        else:
            for place, item in enumerate(patterns):
                if not _is_text(item) or "," in item:
                    problems.add(f"{where}.allow[{place}]", f"must be one table pattern, without a comma, got {item!r}")


def _check_queries(problems: _Problems, value: Any) -> list[str]:
    """Check ``queries`` and return the names."""
    where = "metadata.queries"
    if not isinstance(value, Mapping) or not value:
        problems.add(where, "is required and must be an object that maps a name to a query")
        return []
    names: list[str] = []
    for name, query in value.items():
        here = f"{where}.{name}"
        problem = _name_problem(name)
        if problem:
            problems.add(here, f"the name {problem}")
        else:
            names.append(name)
        if not isinstance(query, Mapping):
            problems.add(here, f"must be an object with sql or sql_file, got {type(query).__name__}")
            continue
        _unknown_keys(problems, here, query, QUERY_KEYS)
        has_sql, has_file = "sql" in query, "sql_file" in query
        if has_sql == has_file:
            problems.add(here, "needs exactly one of sql, which is the SQL itself, and sql_file, which is a file that "
                               "has a '-- name: <name>' line for the query")
        if has_sql:
            sql = query["sql"]
            if not _is_text(sql):
                problems.add(f"{here}.sql", f"must be SQL text, got {sql!r}")
            elif len(sql) > MAX_SQL_CHARS:
                problems.add(f"{here}.sql", f"is {len(sql)} characters long, and the limit is {MAX_SQL_CHARS}")
        if has_file and not _is_text(query["sql_file"]):
            problems.add(f"{here}.sql_file", f"must be the path of a file of SQL, got {query['sql_file']!r}")
        _optional_text(problems, here, query, "description")
    return names


def _check_parameters(problems: _Problems, value: Any) -> list[str]:
    """Check ``parameters`` and return the names."""
    where = "metadata.parameters"
    names: list[str] = []
    if not isinstance(value, Mapping):
        problems.add(where, f"must be an object that maps a name to a parameter, got {type(value).__name__}")
        return names
    for name, parameter in value.items():
        here = f"{where}.{name}"
        problem = _name_problem(name)
        if problem:
            problems.add(here, f"the name {problem}")
            continue
        if name in BUILTIN_PARAMETERS:
            problems.add(here, f"is a name that the window uses. {list(BUILTIN_PARAMETERS)} are set from --window and "
                               "cannot be declared")
            continue
        names.append(name)
        if not isinstance(parameter, Mapping):
            problems.add(here, f"must be an object with a type, got {type(parameter).__name__}")
            continue
        _unknown_keys(problems, here, parameter, PARAMETER_KEYS)
        kind = parameter.get("type")
        if kind not in PARAMETER_TYPES:
            problems.add(f"{here}.type", f"is required and must be one of {list(PARAMETER_TYPES)}"
                         + (f", got {kind!r}" if kind is not None else ""))
        elif "default" in parameter:
            reason = _value_problem(kind, parameter["default"])
            if reason:
                problems.add(f"{here}.default", reason)
        _optional_text(problems, here, parameter, "description")
    return names


def _value_problem(kind: str, value: Any) -> str | None:
    """Why ``value`` is not a ``kind``, or ``None`` when it is."""
    if kind == "string":
        return None if isinstance(value, str) else f"must be a string, got {value!r}"
    if kind == "integer":
        return None if _whole(value) else f"must be a whole number, got {value!r}"
    if kind == "number":
        return None if _is_number(value) else f"must be a number, got {value!r}"
    if kind == "boolean":
        return None if isinstance(value, bool) else f"must be true or false, got {value!r}"
    if kind == "date":
        if isinstance(value, str) and _DAY.fullmatch(value) and _real_day(value):
            return None
        return f"must be a day written YYYY-MM-DD, got {value!r}"
    return f"has an unknown type {kind!r}"


def _real_day(text: str) -> bool:
    import datetime as dt

    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _check_names(problems: _Problems, where: str, value: Any, noun: str) -> None:
    """Check a list of names, such as the dimensions of a metric block."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        problems.add(where, f"must be the name of a {noun}, or a list of them, got {value!r}")
        return
    for place, item in enumerate(items):
        if not _is_text(item) or "," in item:
            problems.add(f"{where}[{place}]" if isinstance(value, list) else where,
                         f"must be the name of a {noun}, got {item!r}")


def _check_filters(problems: _Problems, where: str, value: Any) -> None:
    if not isinstance(value, list) or not value:
        problems.add(where, f"must be a list of dimension=value filters such as ['city=Pune'], got {value!r}")
        return
    for place, item in enumerate(value):
        if not isinstance(item, str) or "=" not in item or not item.split("=", 1)[0].strip():
            problems.add(f"{where}[{place}]", f"must be written dimension=value, got {item!r}")


def text_references(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """The ``{query.column}`` placeholders of the words of a ``text`` block, and what is wrong with its braces.

    ``{{`` and ``}}`` write a brace. Any other brace has to open and close a placeholder of a query and a column.

    Returns:
        The ``(query, column)`` pairs in order, and the problems, each a sentence about one brace.
    """
    found: list[tuple[str, str]] = []
    problems: list[str] = []
    for match in _PLACEHOLDER.finditer(text):
        token = match.group(0)
        if token in ("{{", "}}"):
            continue
        if token in ("{", "}"):
            problems.append(f"has a lone {token!r}. Write {token * 2!r} for a brace, or {{query.column}} for a value")
            continue
        reference = _REFERENCE.fullmatch(token[1:-1])
        if reference is None:
            problems.append(f"has the placeholder {token}, which is not {{query.column}}")
        else:
            found.append((reference.group(1), reference.group(2)))
    return found, problems


def _check_block(problems: _Problems, place: int, block: Any, queries: Sequence[str]) -> None:
    where = f"metadata.blocks[{place}]"
    if not isinstance(block, Mapping):
        problems.add(where, f"must be an object with a type, got {type(block).__name__}")
        return
    kind = block.get("type")
    if kind not in BLOCK_TYPES:
        problems.add(f"{where}.type", f"is required and must be one of {list(BLOCK_TYPES)}"
                     + (f", got {kind!r}" if kind is not None else ""))
        return
    _unknown_keys(problems, where, block, BLOCK_KEYS[kind], f" for a {kind}")
    _optional_text(problems, where, block, "title")
    if kind == "text":
        text = block.get("text")
        if not _is_text(text):
            problems.add(f"{where}.text", "is required and must be the words to show")
            return
        references, errors = text_references(text)
        for message in errors:
            problems.add(f"{where}.text", message)
        for name, _column in references:
            if name not in queries:
                problems.add(f"{where}.text", f"has the placeholder {{{name}.{_column}}}, and {name!r} is not a query. "
                                              f"The queries are {list(queries)}")
        return
    named, metric = block.get("query"), block.get("metric")
    if kind == "table":
        if metric is not None:
            problems.add(f"{where}.metric", "is for a kpi, a bar or a line. A table shows a query")
        _check_query_name(problems, f"{where}.query", named, queries)
        if "columns" in block:
            columns = block["columns"]
            if not isinstance(columns, list) or not columns:
                problems.add(f"{where}.columns", f"must be a list of at least one column, got {columns!r}")
            else:
                for index, item in enumerate(columns):
                    _check_column(problems, f"{where}.columns[{index}]", item)
                if len(set(map(str, columns))) != len(columns):
                    problems.add(f"{where}.columns", "lists a column twice")
        if "formats" in block:
            formats = block["formats"]
            if not isinstance(formats, Mapping):
                problems.add(f"{where}.formats", f"must be an object that maps a column to a format, got {formats!r}")
            else:
                for column, item in formats.items():
                    _check_column(problems, f"{where}.formats.{column}", column)
                    _check_format(problems, f"{where}.formats.{column}", item)
        if "max_rows" in block and not (_whole(block["max_rows"]) and 1 <= block["max_rows"] <= MAX_BLOCK_ROWS):
            problems.add(f"{where}.max_rows", f"must be a whole number from 1 to {MAX_BLOCK_ROWS}, got "
                                              f"{block['max_rows']!r}")
        return
    # kpi, bar and line either show a query or compute a metric
    if (named is None) == (metric is None):
        problems.add(where, "needs exactly one of query, which shows a query, and metric, which computes a metric of "
                            "the metrics files")
        return
    if "format" in block:
        _check_format(problems, f"{where}.format", block["format"])
    if named is not None:
        _check_query_name(problems, f"{where}.query", named, queries)
        for key in METRIC_FORM_KEYS:
            if key in block:
                problems.add(f"{where}.{key}", "is for a metric. A block that shows a query takes its numbers from the "
                                               "query")
        for key in ("value", "compare") if kind == "kpi" else ("x", "y", "series"):
            _check_column(problems, f"{where}.{key}", block.get(key), required=key in ("value", "x", "y"))
        return
    if not _is_text(metric) or "," in metric:
        problems.add(f"{where}.metric", f"must be the name of one metric, got {metric!r}")
    for key in QUERY_FORM_KEYS[kind]:
        if key in block:
            problems.add(f"{where}.{key}", "is for a block that shows a query. A metric is shown by its grain and its "
                                           "dimensions")
    if "grain" in block and block["grain"] not in GRAINS:
        problems.add(f"{where}.grain", f"must be one of {list(GRAINS)}, got {block['grain']!r}")
    if "by" in block:
        if kind == "kpi":
            problems.add(f"{where}.by", "does not apply to a kpi, which shows one number")
        else:
            _check_names(problems, f"{where}.by", block["by"], "dimension")
    if "filters" in block:
        _check_filters(problems, f"{where}.filters", block["filters"])
    if kind == "bar" and "by" not in block and "grain" not in block:
        problems.add(where, "is a bar of a metric, which needs by, the dimension of the bars, or grain, the periods of "
                            "the bars")
    if kind == "line" and "grain" not in block:
        problems.add(where, "is a line of a metric, which needs grain, the periods along the line")


def _check_query_name(problems: _Problems, where: str, value: Any, queries: Sequence[str]) -> None:
    if not isinstance(value, str) or not value:
        problems.add(where, "is required and must be the name of a query")
    elif value not in queries:
        problems.add(where, f"unknown query {value!r}; the queries are {list(queries)}")


def _check_blocks(problems: _Problems, value: Any, queries: Sequence[str]) -> None:
    if not isinstance(value, list) or not value:
        problems.add("metadata.blocks", "is required and must be a list of at least one block")
        return
    for place, block in enumerate(value):
        _check_block(problems, place, block, queries)


def _check_freshness(problems: _Problems, value: Any) -> None:
    where = "metadata.freshness"
    if not isinstance(value, Mapping):
        problems.add(where, f"must be an object with max_age_hours and on_stale, got {type(value).__name__}")
        return
    _unknown_keys(problems, where, value, FRESHNESS_KEYS)
    if "max_age_hours" in value and not (_is_number(value["max_age_hours"]) and value["max_age_hours"] > 0):
        problems.add(f"{where}.max_age_hours", f"must be a number of hours above 0, got {value['max_age_hours']!r}")
    if "on_stale" in value and value["on_stale"] not in ON_STALE:
        problems.add(f"{where}.on_stale", f"must be one of {list(ON_STALE)}, got {value['on_stale']!r}")


def _check_delivery(problems: _Problems, value: Any) -> None:
    where = "metadata.delivery"
    if not isinstance(value, Mapping) or not value:
        problems.add(where, "must be an object that maps the name of a destination to the destination")
        return
    for name, destination in value.items():
        here = f"{where}.{name}"
        problem = _name_problem(name)
        if problem:
            problems.add(here, f"the name {problem}")
        if not isinstance(destination, Mapping):
            problems.add(here, f"must be an object with a type, got {type(destination).__name__}")
            continue
        kind = destination.get("type")
        if kind not in DESTINATION_TYPES:
            problems.add(f"{here}.type", f"is required and must be one of {list(DESTINATION_TYPES)}"
                         + (f", got {kind!r}" if kind is not None else ""))
            continue
        _unknown_keys(problems, here, destination, DESTINATION_KEYS[kind], f" for a {kind} destination")
        required = {"slack": ("webhook_env",), "webhook": ("url_env",), "file": ("path",)}[kind]
        for key in required:
            if key not in destination:
                hint = (" and names the environment variable that holds the address, never the address"
                        if key.endswith("_env") else "")
                problems.add(f"{here}.{key}", "is required" + hint)
        for key in ("webhook_env", "url_env", "headers_env"):
            if key in destination and not (isinstance(destination[key], str) and _ENV_NAME.fullmatch(destination[key])):
                problems.add(f"{here}.{key}", "must be the name of an environment variable such as SLACK_WEBHOOK_URL, "
                                              "and not the address or the token itself")
        for key in ("path", "public_url"):
            if key in destination and not _is_text(destination[key]):
                problems.add(f"{here}.{key}", f"must be a non-empty string, got {destination[key]!r}")
        if "retries" in destination and not (_whole(destination["retries"]) and 0 <= destination["retries"] <= 10):
            problems.add(f"{here}.retries", f"must be a whole number from 0 to 10, got {destination['retries']!r}")


def _check_after(problems: _Problems, value: Any) -> None:
    where = "metadata.after"
    if not isinstance(value, list) or not value:
        problems.add(where, "must be a list of the identifiers of at least one pipeline")
        return
    for place, item in enumerate(value):
        if not _is_text(item):
            problems.add(f"{where}[{place}]", f"must be the identifier of a pipeline, got {item!r}")
        elif item in value[:place]:
            problems.add(f"{where}[{place}]", f"{item!r} is listed twice")


def validate_report(spec: Mapping[str, Any]) -> list[ConfigError]:
    """The problems of a report file, without reading any table or file.

    It checks the keys, the identifier, the tables, that each query is inline SQL or names a file, the parameters, that
    each block has what its type needs and refers to queries that exist, the time zone, the limits, the freshness rule,
    where the report is sent and what it waits for, and that no secret is written into the file. Whether the SQL runs,
    and whether the tables, the files and the metrics exist, is checked by ``ldp report check``.

    Args:
        spec: The file as a mapping.

    Returns:
        One :class:`~local_data_platform.exceptions.ConfigError` per problem, each message starting with the JSON path
        of the offending value. Empty when valid.
    """
    problems = _Problems()
    _unknown_keys(problems, "", spec, REPORT_KEYS)
    if "apiVersion" in spec and spec["apiVersion"] != API_VERSION:
        problems.add("apiVersion", f"must be {API_VERSION!r}, got {spec['apiVersion']!r}")
    _check_string(problems, spec, "identifier", "", required=True)
    for key in ("who", "what", "where", "when", "how"):
        if key in spec and not isinstance(spec[key], str):
            problems.add(key, f"must be a string, got {type(spec[key]).__name__}")
    metadata = spec.get("metadata")
    if not isinstance(metadata, Mapping):
        problems.add("metadata", "is required and must be an object with tables, queries and blocks")
    else:
        _unknown_keys(problems, "metadata", metadata, METADATA_KEYS)
        _optional_text(problems, "metadata", metadata, "title")
        _check_tables(problems, metadata.get("tables"))
        queries = _check_queries(problems, metadata.get("queries"))
        _check_blocks(problems, metadata.get("blocks"), queries)
        if "parameters" in metadata:
            _check_parameters(problems, metadata["parameters"])
        if "timezone" in metadata:
            try:
                if not _is_text(metadata["timezone"]):
                    raise ConfigError(f"must be an IANA name such as 'Asia/Kolkata' or 'UTC', got "
                                      f"{metadata['timezone']!r}")
                get_timezone(metadata["timezone"])
            except ConfigError as error:
                problems.add("metadata.timezone", str(error))
        if "row_cap" in metadata and not (_whole(metadata["row_cap"]) and 1 <= metadata["row_cap"] <= MAX_ROW_CAP):
            problems.add("metadata.row_cap", f"must be a whole number of rows from 1 to {MAX_ROW_CAP}, got "
                                             f"{metadata['row_cap']!r}")
        timeout = metadata.get("timeout_s")
        if "timeout_s" in metadata and not (_is_number(timeout) and 0 < timeout <= MAX_TIMEOUT_S):
            problems.add("metadata.timeout_s", f"must be a number of seconds above 0 and at most {MAX_TIMEOUT_S:g}, "
                                               f"got {metadata['timeout_s']!r}")
        if "freshness" in metadata:
            _check_freshness(problems, metadata["freshness"])
        if "delivery" in metadata:
            _check_delivery(problems, metadata["delivery"])
        if "after" in metadata:
            _check_after(problems, metadata["after"])
        if "observability" in metadata:
            from local_data_platform.events import sink_specs

            try:
                sink_specs(metadata)
            except ConfigError as error:
                problems.add("", str(error))
    _check_secrets(problems, spec, "")
    return problems.errors


def describe_report(spec: Mapping[str, Any]) -> dict[str, Any]:
    """What ``ldp plan`` reports about a valid report file: its queries, blocks, parameters and time zone."""
    metadata = spec["metadata"]
    return {"queries": list(metadata["queries"]),
            "blocks": [f"{block['type']} {block.get('metric') or block.get('query') or ''}".strip()
                       for block in metadata["blocks"]],
            "parameters": [*BUILTIN_PARAMETERS, *metadata.get("parameters", {})],
            "timezone": metadata.get("timezone", DEFAULT_TIMEZONE)}


# --------------------------------------------------------------------------- reading a file


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _names(value: Any) -> tuple[str, ...]:
    items = [value] if isinstance(value, str) else (value or [])
    return tuple(str(item).strip() for item in items)


def build(spec: Mapping[str, Any], *, file: Path | None = None, base_dir: Path | None = None) -> Report:
    """The :class:`Report` of a file that :func:`validate_report` found no problem in.

    The SQL of a query that names a ``sql_file`` is not read here, so its ``sql`` is ``None``.

    Args:
        spec: The file as a mapping.
        file: The file it was read from.
        base_dir: The folder that its paths are relative to. The folder of ``file`` by default.
    """
    metadata = spec["metadata"]
    base = base_dir if base_dir is not None else (file.parent if file is not None else None)
    tables = metadata["tables"]
    queries = tuple(Query(name=name, sql=_text(item.get("sql")), sql_file=_text(item.get("sql_file")),
                          description=_text(item.get("description"))) for name, item in metadata["queries"].items())
    blocks = []
    for index, item in enumerate(metadata["blocks"]):
        formats = item.get("formats") or {}
        blocks.append(Block(
            index=index, type=item["type"], title=_text(item.get("title")), query=item.get("query"),
            value=item.get("value"), compare=item.get("compare"), x=item.get("x"), y=item.get("y"),
            series=item.get("series"), metric=_text(item.get("metric")), grain=item.get("grain"),
            by=_names(item.get("by")), filters=tuple(item.get("filters") or ()), format=item.get("format"),
            columns=tuple(item["columns"]) if "columns" in item else None, formats=dict(formats),
            max_rows=item.get("max_rows", 100), text=item.get("text")))
    parameters = tuple(Parameter(name=name, type=item["type"], default=item.get("default"),
                                 description=_text(item.get("description")))
                       for name, item in (metadata.get("parameters") or {}).items())
    freshness = None
    if "freshness" in metadata:
        item = metadata["freshness"]
        hours = item.get("max_age_hours")
        freshness = Freshness(max_age_hours=float(hours) if hours is not None else None,
                              on_stale=item.get("on_stale", "warn"))
    delivery = tuple(Destination(name=name, type=item["type"], options={k: v for k, v in item.items() if k != "type"})
                     for name, item in (metadata.get("delivery") or {}).items())
    allow = tables.get("allow")
    identifier = str(spec["identifier"]).strip()
    return Report(
        identifier=identifier, title=_text(metadata.get("title")) or identifier, who=_text(spec.get("who")),
        what=_text(spec.get("what")), where=_text(spec.get("where")), when=_text(spec.get("when")),
        how=_text(spec.get("how")),
        tables=TableSources(config=tuple(tables.get("config") or ()), catalog=tuple(tables.get("catalog") or ()),
                            allow=tuple(allow) if allow else None),
        queries=queries, blocks=tuple(blocks), parameters=parameters,
        timezone=str(metadata.get("timezone") or DEFAULT_TIMEZONE), row_cap=metadata.get("row_cap", DEFAULT_ROW_CAP),
        timeout_s=float(metadata.get("timeout_s", DEFAULT_TIMEOUT_S)), freshness=freshness, delivery=delivery,
        after=tuple(metadata.get("after") or ()), observability=metadata.get("observability"), file=file,
        base_dir=base)


def named_queries(text: str) -> dict[str, str]:
    """The queries of a file of SQL: each starts at a ``-- name: <name>`` line and ends at the next one.

    The comment lines under the name belong to the query, and so does anything before the closing ``;``.

    Raises:
        ConfigError: If two queries have the same name.
    """
    queries: dict[str, str] = {}
    name: str | None = None
    lines: list[str] = []
    for line in [*text.splitlines(), "-- name: __end__"]:
        match = _SECTION.fullmatch(line.strip())
        if match:
            if name is not None:
                queries[name] = "\n".join(lines).strip().rstrip(";").strip()
            name, lines = match.group(1), []
            if name != "__end__" and name in queries:
                raise ConfigError(f"the query {name!r} is named twice")
        elif name is not None:
            lines.append(line)
    return queries


def report_files(path: str | Path) -> list[Path]:
    """The report files at ``path``: the ``*.json`` files of a folder whose kind is ``Report``, or the file itself.

    Raises:
        ConfigError: If ``path`` does not exist.
    """
    resolved = resolve_path(path)
    if resolved.is_dir():
        return sorted(item for item in resolved.glob("*.json") if item.is_file() and kind_of_file(item) == KIND)
    if resolved.is_file():
        return [resolved]
    raise ConfigError(f"report path not found: {resolved}")


def read_report(path: str | Path) -> tuple[Report | None, list[str]]:
    """Read a report file, with the SQL of its files, and say what is wrong with it, without raising for the file.

    Args:
        path: A report file.

    Returns:
        The report, or ``None`` when there are problems, and the problems, each starting with a JSON path. A file that
        is not JSON, is not a report, or is not there has one problem, which says so.
    """
    file = resolve_path(path)
    if not file.is_file():
        return None, [f"report file not found: {file}"]
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, [f"{file.name} is not valid JSON: {error}"]
    if not isinstance(data, Mapping):
        return None, [f"{file.name} must be a JSON object, got {type(data).__name__}"]
    try:
        kind = kind_of(data)
    except ConfigError as error:
        return None, [f"kind: {error}"]
    if kind != KIND:
        return None, [f"kind: {file.name} is a {kind}, and a report has \"kind\": \"{KIND}\""]
    errors = [str(error) for error in validate_spec(data)]
    if errors:
        return None, errors
    report = build(data, file=file)
    queries: list[Query] = []
    problems: list[str] = []
    cache: dict[Path, dict[str, str] | str] = {}
    for query in report.queries:
        if query.sql_file is None:
            queries.append(query)
            continue
        location = resolve_path(query.sql_file, report.base_dir)
        if location not in cache:
            try:
                cache[location] = named_queries(location.read_text(encoding="utf-8"))
            except OSError as error:
                cache[location] = f"the file {location} cannot be read: {error.strerror or error}"
            except ConfigError as error:
                cache[location] = f"the file {location} is not usable. {error}"
        found = cache[location]
        where = f"metadata.queries.{query.name}.sql_file"
        if isinstance(found, str):
            problems.append(f"{where}: {found}")
        elif query.name not in found:
            problems.append(f"{where}: the file {location} has no '-- name: {query.name}' line. It has "
                            f"{list(found) or 'no named query'}")
        elif not found[query.name]:
            problems.append(f"{where}: the query {query.name!r} of {location} is empty")
        elif len(found[query.name]) > MAX_SQL_CHARS:
            problems.append(f"{where}: the query {query.name!r} of {location} is {len(found[query.name])} characters "
                            f"long, and the limit is {MAX_SQL_CHARS}")
        else:
            queries.append(Query(name=query.name, sql=found[query.name], sql_file=query.sql_file,
                                 description=query.description))
    if problems:
        return None, problems
    return replace(report, queries=tuple(queries)), []


def load_report(path: str | Path) -> Report:
    """Read a report file.

    Raises:
        ConfigError: If the file is not a valid report, or an SQL file is missing. The message lists every problem.
    """
    report, problems = read_report(path)
    if report is not None:
        return report
    name = Path(str(path)).name
    if len(problems) == 1:
        raise ConfigError(f"{name}: {problems[0]}")
    raise ConfigError("\n".join([f"{name} has {len(problems)} problems:", *[f"  - {item}" for item in problems]]))


# --------------------------------------------------------------------------- the JSON Schema


def report_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a report file."""
    string = {"type": "string"}
    text = {"type": "string", "minLength": 1}
    name = {"type": "string", "pattern": f"^{NAME_PATTERN}$"}
    column = {"type": "string", "pattern": f"^{_COLUMN.pattern}$"}
    number_format = {"type": "string", "pattern": f"^({FORMAT_PATTERN})$",
                     "examples": ["integer", "number:2", "percent:1", "currency:USD"],
                     "description": "How a number is written."}
    env_name = {"type": "string", "pattern": f"^{_ENV_NAME.pattern}$",
                "description": "The name of an environment variable. The address or the token is never written here."}
    paths = {"type": "array", "minItems": 1, "items": text}
    filters = {"type": "array", "minItems": 1, "items": text}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local-data-platform.readthedocs.io/schemas/ldp-v1-report.json",
        "title": f"local-data-platform report ({API_VERSION})",
        "type": "object",
        "required": ["kind", "identifier", "metadata"],
        "additionalProperties": False,
        "properties": {
            "apiVersion": {"const": API_VERSION, "default": API_VERSION},
            "kind": {"const": KIND, "description": "A report says so, because a file without a kind is a Pipeline."},
            "identifier": {**text, "description": "The id of the report."},
            "who": {**string, "description": "Who owns the report."},
            "what": {**string, "description": "What the report is for."},
            "where": {**string, "description": "Where the report goes."},
            "when": {**string, "description": "How often it is made."},
            "how": {**string, "description": "How it is made."},
            "metadata": {
                "type": "object", "required": ["tables", "queries", "blocks"], "additionalProperties": False,
                "properties": {
                    "title": {**text, "description": "What the report is called. The identifier by default."},
                    "tables": {
                        "type": "object", "additionalProperties": False,
                        "anyOf": [{"required": ["config"]}, {"required": ["catalog"]}],
                        "description": "The tables the queries read, found as ldp mcp finds them. Paths are relative "
                                       "to the folder of the file.",
                        "properties": {
                            "config": {**paths, "description": "Dataset configs, or folders of them. The metrics files "
                                                               "of the folders define the metrics of the blocks."},
                            "catalog": {**paths, "description": "Catalog spec files. Every table of the catalog is "
                                                                "found."},
                            "allow": {"type": "array", "minItems": 1, "items": {**text, "pattern": "^[^,]+$"},
                                      "examples": [["gold.*"]],
                                      "description": "The tables that may be read, as patterns such as gold.* or "
                                                     "gold.daily_sales. Every table that is found by default."}}},
                    "queries": {
                        "type": "object", "minProperties": 1, "propertyNames": {"pattern": f"^{NAME_PATTERN}$"},
                        "additionalProperties": {"$ref": "#/$defs/query"},
                        "description": "The queries, by name. A block shows one by its name."},
                    "blocks": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/block"},
                               "description": "What the report shows, in order."},
                    "parameters": {
                        "type": "object", "propertyNames": {"pattern": f"^{NAME_PATTERN}$", "not": {
                            "enum": list(BUILTIN_PARAMETERS)}},
                        "additionalProperties": {"$ref": "#/$defs/parameter"},
                        "description": "Values that a query may use as $name, and that --param can change."},
                    "timezone": {**text, "default": DEFAULT_TIMEZONE, "examples": ["Asia/Kolkata", "UTC"],
                                 "description": "The IANA time zone that the queries run in. A day is a day of this "
                                                "zone, whatever the zone of the machine."},
                    "row_cap": {"type": "integer", "minimum": 1, "maximum": MAX_ROW_CAP, "default": DEFAULT_ROW_CAP,
                                "description": "How many rows a query may return."},
                    "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_TIMEOUT_S,
                                  "default": DEFAULT_TIMEOUT_S, "description": "How many seconds a query may run."},
                    "freshness": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "max_age_hours": {"type": "number", "exclusiveMinimum": 0},
                            "on_stale": {"enum": list(ON_STALE), "default": "warn"}},
                        "description": "What to do with data that is stale."},
                    "delivery": {
                        "type": "object", "minProperties": 1, "propertyNames": {"pattern": f"^{NAME_PATTERN}$"},
                        "additionalProperties": {"$ref": "#/$defs/destination"},
                        "description": "The places the report is sent to, by name."},
                    "after": {"type": "array", "minItems": 1, "uniqueItems": True, "items": text,
                              "description": "The identifiers of the pipelines that have to have run for the window "
                                             "before the report is sent."},
                    "observability": {"type": "object", "description": "Where the events of a build go, as for a "
                                                                       "pipeline."},
                },
            },
        },
        "$defs": {
            "query": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "sql": {"type": "string", "minLength": 1, "maxLength": MAX_SQL_CHARS,
                            "description": "One read-only SELECT or WITH query, with $window_start and $window_end."},
                    "sql_file": {**text, "description": "A file of SQL with a '-- name: <name>' line for the query."},
                    "description": text},
                "oneOf": [{"required": ["sql"]}, {"required": ["sql_file"]}],
            },
            "parameter": {
                "type": "object", "required": ["type"], "additionalProperties": False,
                "properties": {
                    "type": {"enum": list(PARAMETER_TYPES)},
                    "default": {"description": "The value when none is given. A parameter without one has to be "
                                               "given."},
                    "description": text},
            },
            "destination": {
                "type": "object", "required": ["type"],
                "oneOf": [
                    {"properties": {"type": {"const": "slack"}, "webhook_env": env_name,
                                    "retries": {"type": "integer", "minimum": 0, "maximum": 10}},
                     "required": ["webhook_env"], "additionalProperties": False},
                    {"properties": {"type": {"const": "webhook"}, "url_env": env_name, "headers_env": env_name,
                                    "retries": {"type": "integer", "minimum": 0, "maximum": 10}},
                     "required": ["url_env"], "additionalProperties": False},
                    {"properties": {"type": {"const": "file"}, "path": text, "public_url": text},
                     "required": ["path"], "additionalProperties": False},
                ],
            },
            "block": {
                "type": "object", "required": ["type"],
                "properties": {"type": {"enum": list(BLOCK_TYPES)}, "title": text},
                "oneOf": [
                    {"properties": {"type": {"const": "kpi"}, "title": text, "query": name, "value": column,
                                    "compare": column, "metric": text, "grain": {"enum": list(GRAINS)},
                                    "filters": filters, "format": number_format},
                     "required": ["type"], "additionalProperties": False,
                     "oneOf": [{"required": ["query", "value"]}, {"required": ["metric"]}]},
                    {"properties": {"type": {"const": "bar"}, "title": text, "query": name, "x": column, "y": column,
                                    "series": column, "metric": text, "by": {"oneOf": [text, paths]},
                                    "grain": {"enum": list(GRAINS)},
                                    "filters": filters, "format": number_format},
                     "required": ["type"], "additionalProperties": False,
                     "oneOf": [{"required": ["query", "x", "y"]}, {"required": ["metric"], "anyOf": [
                         {"required": ["by"]}, {"required": ["grain"]}]}]},
                    {"properties": {"type": {"const": "line"}, "title": text, "query": name, "x": column, "y": column,
                                    "series": column, "metric": text, "by": {"oneOf": [text, paths]},
                                    "grain": {"enum": list(GRAINS)},
                                    "filters": filters, "format": number_format},
                     "required": ["type"], "additionalProperties": False,
                     "oneOf": [{"required": ["query", "x", "y"]}, {"required": ["metric", "grain"]}]},
                    {"properties": {"type": {"const": "table"}, "title": text, "query": name,
                                    "columns": {"type": "array", "minItems": 1, "uniqueItems": True, "items": column},
                                    "formats": {"type": "object", "propertyNames": column,
                                                "additionalProperties": number_format},
                                    "max_rows": {"type": "integer", "minimum": 1, "maximum": MAX_BLOCK_ROWS,
                                                 "default": 100}},
                     "required": ["type", "query"], "additionalProperties": False},
                    {"properties": {"type": {"const": "text"}, "title": text,
                                    "text": {**text, "description": "Plain paragraphs. {query.column} is filled from "
                                                                    "the first row of the query, and {{ and }} write "
                                                                    "braces."}},
                     "required": ["type", "text"], "additionalProperties": False},
                ],
            },
        },
    }


__all__ = [
    "BLOCK_KEYS",
    "BLOCK_TYPES",
    "BUILTIN_PARAMETERS",
    "Block",
    "DEFAULT_ROW_CAP",
    "DEFAULT_TIMEOUT_S",
    "DEFAULT_TIMEZONE",
    "Destination",
    "FORMAT_PATTERN",
    "Freshness",
    "GRAINS",
    "KIND",
    "METADATA_KEYS",
    "NAME_PATTERN",
    "PARAMETER_TYPES",
    "Parameter",
    "Query",
    "REPORT_KEYS",
    "Report",
    "TableSources",
    "build",
    "describe_report",
    "load_report",
    "named_queries",
    "read_report",
    "report_files",
    "report_schema",
    "text_references",
    "validate_report",
]
