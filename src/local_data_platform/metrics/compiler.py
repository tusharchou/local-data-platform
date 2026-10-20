"""Compiling a metric query into DuckDB SQL.

A query asks for some metrics of one semantic model, grouped by some dimensions and a grain of time, for some filters
and a range of days. The result is one ``SELECT`` over one gold table. What a person wrote, which is a filter value or a
date, never goes into the SQL text. It is bound as a parameter, so a value cannot change what the query does. What goes
into the text are names and expressions that the metrics file defines and that were checked, and the five grains.

A ratio is the sum of one measure over the sum of another, and never the average of the ratios of the rows. A query for
more than one semantic model is refused, because it would need a join, and this version reads one table.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.metrics.load import Definitions, unknown_name
from local_data_platform.metrics.model import GRAINS, Dimension, Metric, SemanticModel
from local_data_platform.metrics.sql import aggregate, names, only_expression, quote, ratio, time_bucket

_INTEGERS = frozenset({"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                       "UBIGINT", "UHUGEINT"})
_FLOATS = frozenset({"FLOAT", "DOUBLE", "REAL"})
_TEXTS = frozenset({"VARCHAR", "CHAR", "BPCHAR", "TEXT", "STRING"})
_TIMESTAMPS = frozenset({"TIMESTAMP", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS"})
_WHOLE = re.compile(r"[+-]?[0-9]+")
_DECIMAL = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")
_FLOAT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MOMENT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}([ T][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,6})?)?)?")


@dataclass(frozen=True)
class Column:
    """One column of the result of a query.

    Attributes:
        name: The name of the column, which is the grain, the dimension or the metric.
        kind: ``time`` for the grain, ``dimension`` or ``metric``.
        format: How a person reads the value of a metric, ``number``, ``percent`` or ``currency``. ``None`` otherwise.
        description: What the column means, or ``None``.
    """

    name: str
    kind: str
    format: str | None = None
    description: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The column as JSON-serialisable primitives."""
        return {"name": self.name, "kind": self.kind, "format": self.format, "description": self.description}


@dataclass(frozen=True)
class CompiledQuery:
    """A metric query as SQL, and what it asks for.

    Attributes:
        sql: The ``SELECT``, with ``$1``, ``$2`` and so on for the values that a person gave.
        parameters: The values of those, in order, already of the type of their column.
        model: The semantic model that the metrics are of.
        table: The gold table, ``<namespace>.<table>``.
        metrics: The metrics, in the order they were asked for.
        columns: The columns of the result, in order.
        grain: The grain of time, or ``None``.
    """

    sql: str
    parameters: tuple[Any, ...]
    model: SemanticModel
    table: str
    metrics: tuple[Metric, ...]
    columns: tuple[Column, ...]
    grain: str | None = None

    def explain(self) -> str:
        """The SQL, with a comment for each parameter that says what is bound to it, which is what ``--sql`` prints."""
        lines = [self.sql]
        lines += [f"-- ${number} = {_show(value)}" for number, value in enumerate(self.parameters, start=1)]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """The query as JSON-serialisable primitives."""
        return {"sql": self.sql, "parameters": [_json(value) for value in self.parameters],
                "semantic_model": self.model.name, "table": self.table, "grain": self.grain,
                "metrics": [item.name for item in self.metrics], "columns": [item.to_dict() for item in self.columns]}


def _show(value: Any) -> str:
    """A bound value as a comment shows it, on one line whatever the text holds."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    return str(value)


def _json(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def split_names(value: str | Sequence[str] | None, what: str) -> list[str]:
    """Names given as ``a,b`` or as a list of names, in order and once each.

    Raises:
        ConfigError: If a name is empty or is not text.
    """
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    found: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise ConfigError(f"{what} has to be names, got {item!r}")
        for part in item.split(","):
            name = part.strip()
            if not name:
                raise ConfigError(f"{what} has an empty name in {item!r}")
            if name not in found:
                found.append(name)
    return found


def select_metrics(definitions: Definitions, wanted: str | Sequence[str]) -> tuple[SemanticModel, list[Metric]]:
    """The metrics that were asked for, and the one semantic model they are of.

    Raises:
        ConfigError: If none was asked for, a name is not a metric, or the metrics are of more than one semantic model.
    """
    asked = split_names(wanted, "the metrics")
    if not asked:
        raise ConfigError(f"ask for at least one metric. The metrics are {[item.name for item in definitions.metrics]}")
    metrics = [definitions.metric(name) for name in asked]
    models: dict[str, list[str]] = {}
    for metric in metrics:
        models.setdefault(metric.model, []).append(metric.name)
    if len(models) > 1:
        where = "; ".join(f"{names(found)} on {name} ({definitions.model(name).table})"
                          for name, found in models.items())
        raise ConfigError(f"the metrics are on more than one semantic model, {where}. A query reads one table, so "
                          "ask for them in separate queries")
    return definitions.model(metrics[0].model), metrics


# --------------------------------------------------------------------------- values


def _kind(type_name: str) -> str:
    base = str(type_name).upper().split("(")[0].strip()
    if base in _TEXTS:
        return "text"
    if base in _INTEGERS:
        return "integer"
    if base == "DECIMAL":
        return "decimal"
    if base in _FLOATS:
        return "float"
    if base == "BOOLEAN":
        return "boolean"
    if base == "DATE":
        return "date"
    if base in _TIMESTAMPS:
        return "timestamp"
    return "other"


def parse_day(text: Any, flag: str) -> dt.date:
    """A date written ``YYYY-MM-DD``.

    Raises:
        ConfigError: If ``text`` is anything else.
    """
    if isinstance(text, dt.datetime):
        raise ConfigError(f"{flag} has to be a date such as 2026-01-31, got {text!r}")
    if isinstance(text, dt.date):
        return text
    if isinstance(text, str) and _DAY.fullmatch(text.strip()):
        try:
            return dt.date.fromisoformat(text.strip())
        except ValueError:
            pass
    raise ConfigError(f"{flag} has to be a date such as 2026-01-31, got {text!r}")


def coerce(text: str, type_name: str, dimension: str) -> Any:
    """A filter value, written as text, as a value of the type of its column.

    Args:
        text: The value as a person wrote it.
        type_name: The DuckDB type of the column.
        dimension: The name of the dimension, for the message.

    Raises:
        ConfigError: If the value does not fit the type, or the type cannot be filtered on.
    """
    kind = _kind(type_name)
    value = text.strip() if kind != "text" else text
    bad = f"the dimension {dimension!r} is a {type_name}, so a filter value has to be"
    if kind == "text":
        return value
    if kind == "integer":
        if _WHOLE.fullmatch(value):
            return int(value)
        raise ConfigError(f"{bad} a whole number, got {text!r}")
    if kind == "decimal":
        if _DECIMAL.fullmatch(value):
            return Decimal(value)
        raise ConfigError(f"{bad} a number, got {text!r}")
    if kind == "float":
        if _FLOAT.fullmatch(value):
            return float(value)
        raise ConfigError(f"{bad} a number, got {text!r}")
    if kind == "boolean":
        if value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ConfigError(f"{bad} true or false, got {text!r}")
    if kind == "date":
        return parse_day(value, f"the filter value of {dimension!r}")
    if kind == "timestamp":
        if _MOMENT.fullmatch(value):
            try:
                return dt.datetime.fromisoformat(value)
            except ValueError:
                pass
        raise ConfigError(f"{bad} a date or a time such as 2026-01-31 or 2026-01-31 18:30, got {text!r}")
    raise ConfigError(f"the dimension {dimension!r} is a {type_name}, and a filter works on text, numbers, "
                      "booleans, dates and timestamps")


def parse_filters(items: Mapping[str, Any] | Sequence[str] | None) -> list[tuple[str, list[str]]]:
    """Filters as ``(dimension, values)`` in the order they were given. A dimension that is given twice has two values.

    Args:
        items: ``{"city": "Pune"}``, ``{"city": ["Pune", "Delhi"]}``, or a list of ``"city=Pune"`` as ``--filter``
            gives them. The value is everything after the first ``=``.

    Raises:
        ConfigError: If an item is not ``dimension=value``.
    """
    found: dict[str, list[str]] = {}
    if items is None:
        return []
    if isinstance(items, Mapping):
        for name, value in items.items():
            values = [value] if isinstance(value, (str, int, float, bool, dt.date)) else list(value)
            found.setdefault(str(name), []).extend(str(item) for item in values)
        return list(found.items())
    for item in ([items] if isinstance(items, str) else items):
        name, equals, value = str(item).partition("=")
        if not equals or not name.strip():
            raise ConfigError(f"a filter is written dimension=value, got {item!r}")
        found.setdefault(name.strip(), []).append(value)
    return list(found.items())


# --------------------------------------------------------------------------- compiling


def _resolve_dimensions(model: SemanticModel, wanted: str | Sequence[str] | None, what: str) -> list[Dimension]:
    found = []
    for name in split_names(wanted, what):
        dimension = model.dimension(name)
        if dimension is None:
            known = [item.name for item in model.dimensions]
            raise ConfigError(f"{unknown_name('dimension', name, known)}. They are the dimensions of {model.name}")
        found.append(dimension)
    return found


def _check_grain(model: SemanticModel, metrics: Sequence[Metric], grain: str | None, asked_time: bool) -> None:
    """Refuse a grain, or no grain, that the time dimension of the model does not allow."""
    named = names([item.name for item in metrics])
    if model.time_dimension is None:
        if grain is not None or asked_time:
            raise ConfigError(f"{model.name} has no time dimension, so a grain or a range of days does not apply to "
                              f"{named}")
        return
    allowed = list(model.grains)
    if grain is None:
        if "year" not in allowed:
            raise ConfigError(f"{named} cannot be added up over the whole period, because the time dimension of "
                              f"{model.name} does not allow the year grain, and adding up the years is the same step. "
                              f"Ask for one of the grains {allowed} with --grain")
        return
    if grain not in GRAINS:
        raise ConfigError(f"unknown grain {grain!r}. The grains are {list(GRAINS)}")
    if grain not in allowed:
        raise ConfigError(f"{named} cannot be reported by {grain}. The time dimension of {model.name} allows the "
                          f"grains {allowed}")


def compile_query(definitions: Definitions, metrics: str | Sequence[str], *, table: str, types: Mapping[str, str],
                  by: str | Sequence[str] | None = None, grain: str | None = None,
                  filters: Mapping[str, Any] | Sequence[str] | None = None, start: str | dt.date | None = None,
                  end: str | dt.date | None = None, table_sql: str | None = None) -> CompiledQuery:
    """Compile a metric query into SQL.

    Args:
        definitions: The metrics of a folder.
        metrics: The metrics to compute, as ``revenue,avg_ticket`` or a list. They are of one semantic model.
        table: The gold table of that model, as ``<namespace>.<table>``.
        types: The DuckDB type of each column of the table, by lower case column name.
        by: The dimensions to group by.
        grain: The grain of time to group by, one that the time dimension of the model allows. Without one the
            period is added up into one row, which the model allows when it allows the year grain.
        filters: ``dimension=value`` items, or ``{dimension: value or values}``. Values for one dimension are
            alternatives, and the dimensions are all required.
        start: The first day to include, ``YYYY-MM-DD``.
        end: The last day to include, ``YYYY-MM-DD``.
        table_sql: How the table is named in the SQL. By default ``"<namespace>"."<table>"``.

    Returns:
        The :class:`CompiledQuery`. Nothing has been run.

    Raises:
        ConfigError: If a name is not known, the metrics are of more than one semantic model, a grain or a value is not
            allowed, or a measure expression is not a plain expression. The message lists what is valid.
    """
    model, selected = select_metrics(definitions, metrics)
    dimensions = _resolve_dimensions(model, by, "--by")
    first = parse_day(start, "--from") if start is not None else None
    last = parse_day(end, "--to") if end is not None else None
    if first is not None and last is not None and first > last:
        raise ConfigError(f"--from {first} is after --to {last}")
    _check_grain(model, selected, grain, first is not None or last is not None)
    asked = parse_filters(filters)
    for name, _ in asked:
        if model.dimension(name) is None:
            known = [item.name for item in model.dimensions]
            raise ConfigError(f"{unknown_name('dimension', name, known)}. They are the dimensions of {model.name}, and "
                              "a filter works on a dimension")

    def column_type(column: str, owner: str) -> str:
        if column.lower() not in types:
            raise ConfigError(f"the column {column!r} of {owner} is not in {table}. The columns are "
                              f"{list(types)}")
        return types[column.lower()]

    def time_kind() -> str:
        time = model.time_dimension
        found = column_type(time.column, "the time dimension")
        kind = _kind(found)
        if kind not in ("date", "timestamp"):
            raise ConfigError(f"the column {time.column!r} of the time dimension of {model.name} is a {found}, and a "
                              "time dimension has to be a DATE or a TIMESTAMP")
        return kind

    parameters: list[Any] = []

    def bind(value: Any) -> str:
        parameters.append(value)
        return f"${len(parameters)}"

    selects: list[str] = []
    columns: list[Column] = []
    if grain is not None:
        time_kind()
        selects.append(f"{time_bucket(model.time_dimension.column, grain)} AS {quote(grain)}")
        columns.append(Column(grain, "time", description=f"the start of the {grain}"))
    for dimension in dimensions:
        column_type(dimension.column, f"the dimension {dimension.name!r}")
        selects.append(quote(dimension.column) if dimension.column == dimension.name
                       else f"{quote(dimension.column)} AS {quote(dimension.name)}")
        columns.append(Column(dimension.name, "dimension", description=dimension.description))
    grouped = len(selects)
    needed: dict[str, str] = {}
    for metric in selected:
        for name in metric.measures:
            measure = model.measure(name)
            if measure is None:
                raise ConfigError(f"the metric {metric.name!r} names the measure {name!r}, which is not in "
                                  f"{model.name}")
            if measure.expr is not None and name not in needed:
                only_expression(measure.expr)
            needed[name] = aggregate(measure.agg, measure.expr)
        if metric.type == "simple":
            value = needed[metric.measure]
        else:
            value = ratio(needed[metric.numerator], needed[metric.denominator])
        selects.append(f"{value} AS {quote(metric.name)}")
        columns.append(Column(metric.name, "metric", format=metric.format, description=metric.description))

    conditions: list[str] = []
    for name, values in asked:
        dimension = model.dimension(name)
        kind = column_type(dimension.column, f"the dimension {dimension.name!r}")
        bound = [bind(coerce(value, kind, dimension.name)) for value in values]
        conditions.append(f"{quote(dimension.column)} = {bound[0]}" if len(bound) == 1
                          else f"{quote(dimension.column)} IN ({', '.join(bound)})")
    if first is not None or last is not None:
        kind = time_kind()
        column = quote(model.time_dimension.column)
        if first is not None:
            conditions.append(f"{column} >= {bind(first if kind == 'date' else dt.datetime.combine(first, dt.time()))}")
        if last is not None:
            if kind == "date":
                conditions.append(f"{column} <= {bind(last)}")
            else:
                conditions.append(f"{column} < {bind(dt.datetime.combine(last + dt.timedelta(days=1), dt.time()))}")

    target = table_sql or ".".join(quote(part) for part in table.rsplit(".", 1))
    lines = ["SELECT", ",\n".join(f"  {item}" for item in selects), f"FROM {target}"]
    if conditions:
        lines.append("WHERE " + " AND ".join(conditions))
    if grouped:
        positions = ", ".join(str(number) for number in range(1, grouped + 1))
        lines += [f"GROUP BY {positions}", f"ORDER BY {positions}"]
    return CompiledQuery(sql="\n".join(lines), parameters=tuple(parameters), model=model, table=table,
                         metrics=tuple(selected), columns=tuple(columns), grain=grain)


__all__ = ["Column", "CompiledQuery", "coerce", "compile_query", "parse_day", "parse_filters", "select_metrics",
           "split_names"]
