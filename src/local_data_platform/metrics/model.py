"""Metric definitions: what a metrics file says, checked without reading any table.

A metrics file is a spec of the kind ``Metrics``. It defines semantic models and the metrics that are computed from
them, with the terms of MetricFlow, so an exporter can follow later::

    {
      "apiVersion": "ldp/v1",
      "kind": "Metrics",
      "identifier": "restaurant_metrics",
      "who": "analytics@example.com",
      "semantic_models": [
        {
          "name": "daily_sales",
          "table": "gold.gold_daily_sales",
          "time_dimension": {"column": "order_date", "grains": ["day", "week", "month", "quarter", "year"]},
          "dimensions": ["cuisine", {"name": "city", "description": "The city of the restaurant."}],
          "measures": [
            {"name": "revenue_sum", "agg": "sum", "expr": "net_revenue"},
            {"name": "order_count", "agg": "sum", "expr": "orders"}
          ]
        }
      ],
      "metrics": [
        {"name": "revenue", "type": "simple", "measure": "revenue_sum", "format": "currency",
         "description": "Money taken, after discounts and before tax."},
        {"name": "avg_ticket", "type": "ratio", "numerator": "revenue_sum", "denominator": "order_count",
         "format": "currency", "description": "Revenue divided by orders."}
      ]
    }

A semantic model names one gold table. Its ``time_dimension`` is a column and the grains that its numbers may be
reported at, from ``day``, ``week``, ``month``, ``quarter`` and ``year``. Its ``dimensions`` are columns to group and
filter by, and its ``measures`` are an aggregate, which is ``sum``, ``count``, ``count_distinct``, ``avg``, ``min`` or
``max``, over a SQL expression of the columns of the table. A metric is ``simple``, one measure, or a ``ratio``, one
measure over another. It has a ``description``, an ``owner`` that is its own or the ``who`` of the file, and a
``format`` of ``number``, ``percent`` or ``currency``, which says how a person reads the value.

This module checks the shape of the file and the references of the metrics to the measures. It reads no table, so it
needs only the standard library, and :mod:`local_data_platform.spec` hands the ``Metrics`` kind to it. What the names
and the expressions mean for the columns of the gold tables is checked by :mod:`local_data_platform.metrics.check`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.spec import API_VERSION, _check_secrets, _check_string, _Problems

KIND = "Metrics"
"""The ``kind`` of a metrics file."""

GRAINS = ("day", "week", "month", "quarter", "year")
"""The grains a time dimension may allow, finest first."""

AGGREGATIONS = ("sum", "count", "count_distinct", "avg", "min", "max")
"""What a measure may do to its expression."""

METRIC_TYPES = ("simple", "ratio")
"""The kinds of metric: one measure, or one measure over another."""

FORMATS = ("number", "percent", "currency")
"""How a person reads the value of a metric."""

DEFAULT_FORMAT = "number"

METRICS_KEYS = ("apiVersion", "kind", "identifier", "who", "what", "where", "when", "how", "semantic_models",
                "metrics")
"""The top-level keys of a metrics file."""

MODEL_KEYS = ("name", "description", "table", "time_dimension", "dimensions", "measures")
TIME_KEYS = ("column", "grains")
DIMENSION_KEYS = ("name", "column", "description")
MEASURE_KEYS = ("name", "agg", "expr", "description")
METRIC_KEYS = ("name", "type", "description", "owner", "format", "measure", "numerator", "denominator")

NAME_PATTERN = r"[a-z][a-z0-9_]{0,62}"
"""A name: lower case letters, digits and underscores, starting with a letter, at most 63 characters."""

_NAME = re.compile(NAME_PATTERN)
_MAX_EXPR = 2000


# --------------------------------------------------------------------------- the definitions


@dataclass(frozen=True)
class Dimension:
    """A column to group and filter by.

    Attributes:
        name: What a person calls it, in ``--by`` and ``--filter``.
        column: The column of the table. It is the name unless the file says otherwise.
        description: What it holds, or ``None``.
    """

    name: str
    column: str
    description: str | None = None


@dataclass(frozen=True)
class TimeDimension:
    """The date column of a semantic model and the grains its numbers may be reported at.

    Attributes:
        column: The column, which is a date or a timestamp.
        grains: The allowed grains, in the order the file lists them.
    """

    column: str
    grains: tuple[str, ...]


@dataclass(frozen=True)
class Measure:
    """An aggregate over an expression of the columns of the table.

    Attributes:
        name: The name that metrics refer to it by. It is unique in the file.
        agg: ``sum``, ``count``, ``count_distinct``, ``avg``, ``min`` or ``max``.
        expr: The SQL expression, or ``None`` for ``count`` of rows.
        description: What it counts or adds up, or ``None``.
    """

    name: str
    agg: str
    expr: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SemanticModel:
    """One gold table with what can be asked of it.

    Attributes:
        name: The name of the model.
        table: The gold table, ``<namespace>.<table>`` or its bare name when only one gold table has it.
        description: What the table is for, or ``None``.
        time_dimension: Its date column and grains, or ``None`` for a table that is not a series.
        dimensions: The columns to group and filter by.
        measures: The aggregates that metrics are made of.
        file: The metrics file that defines it.
    """

    name: str
    table: str
    description: str | None
    time_dimension: TimeDimension | None
    dimensions: tuple[Dimension, ...]
    measures: tuple[Measure, ...]
    file: str = ""

    def dimension(self, name: str) -> Dimension | None:
        """The dimension called ``name``, or ``None``."""
        return next((item for item in self.dimensions if item.name == name), None)

    def measure(self, name: str) -> Measure | None:
        """The measure called ``name``, or ``None``."""
        return next((item for item in self.measures if item.name == name), None)

    @property
    def grains(self) -> tuple[str, ...]:
        """The grains the model allows, empty when it has no time dimension."""
        return self.time_dimension.grains if self.time_dimension is not None else ()


@dataclass(frozen=True)
class Metric:
    """A number with a meaning, an owner and a format.

    Attributes:
        name: The name to ask for it by. It is unique across the metrics files of a folder.
        type: ``simple`` for one measure, ``ratio`` for one measure over another.
        description: What it means.
        owner: Who answers for it, which is the ``owner`` of the metric or else the ``who`` of the file.
        format: ``number``, ``percent`` or ``currency``.
        model: The name of the semantic model that its measures are in.
        measure: The measure of a ``simple`` metric.
        numerator: The measure on top of a ``ratio``.
        denominator: The measure underneath it.
        file: The metrics file that defines it.
    """

    name: str
    type: str
    description: str
    owner: str
    format: str
    model: str
    measure: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    file: str = ""

    @property
    def measures(self) -> tuple[str, ...]:
        """The names of the measures it is made of."""
        if self.type == "simple":
            return (self.measure,) if self.measure else ()
        return tuple(name for name in (self.numerator, self.denominator) if name)


# --------------------------------------------------------------------------- checking a file


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unknown_keys(problems: _Problems, where: str, block: Mapping[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(set(map(str, block)) - set(allowed))
    if unknown:
        problems.add(where, f"has unknown keys {unknown}; allowed: {list(allowed)}")


def _optional_text(problems: _Problems, where: str, block: Mapping[str, Any], key: str) -> None:
    if key in block and not _is_text(block[key]):
        problems.add(f"{where}.{key}", f"must be a non-empty string, got {block[key]!r}")


def _check_name(problems: _Problems, where: str, value: Any, taken: list[str], noun: str, *,
                grains_reserved: bool = False) -> str | None:
    """Check one name, add it to ``taken`` and return it. Returns ``None`` when it is not a usable name."""
    if not isinstance(value, str) or not value:
        problems.add(where, "is required and must be a non-empty string")
        return None
    if not _NAME.fullmatch(value):
        problems.add(where, f"must be lower case letters, digits and underscores, starting with a letter, and at most "
                            f"63 characters, got {value!r}")
        return None
    if grains_reserved and value in GRAINS:
        problems.add(where, f"{value!r} is the name of a grain, and the column for --grain {value} would clash with "
                            f"it. Give the {noun} another name")
        return None
    if value in taken:
        problems.add(where, f"{value!r} is the name of an earlier {noun}")
        return None
    taken.append(value)
    return value


def _check_time_dimension(problems: _Problems, where: str, value: Any) -> str | None:
    """Check ``time_dimension`` and return its column, or ``None``."""
    if not isinstance(value, Mapping):
        problems.add(where, f"must be an object with a column and its grains, got {type(value).__name__}")
        return None
    _unknown_keys(problems, where, value, TIME_KEYS)
    column = value.get("column")
    if not _is_text(column):
        problems.add(f"{where}.column", "is required and must be the name of a date or timestamp column")
        column = None
    grains = value.get("grains")
    if not isinstance(grains, list) or not grains:
        problems.add(f"{where}.grains", f"is required and must be a list of at least one of {list(GRAINS)}")
    else:
        for place, grain in enumerate(grains):
            if grain not in GRAINS:
                problems.add(f"{where}.grains[{place}]", f"must be one of {list(GRAINS)}, got {grain!r}")
            elif grain in grains[:place]:
                problems.add(f"{where}.grains[{place}]", f"{grain!r} is listed twice")
    return column.strip() if isinstance(column, str) else None


def _check_dimensions(problems: _Problems, where: str, value: Any, time_column: str | None) -> list[str]:
    """Check ``dimensions`` and return the names."""
    names: list[str] = []
    if not isinstance(value, list):
        problems.add(where, f"must be a list of dimensions, got {type(value).__name__}")
        return names
    for place, item in enumerate(value):
        here = f"{where}[{place}]"
        if isinstance(item, str):
            _check_name(problems, here, item, names, "dimension", grains_reserved=True)
            column = item
        elif isinstance(item, Mapping):
            _unknown_keys(problems, here, item, DIMENSION_KEYS)
            _check_name(problems, f"{here}.name", item.get("name"), names, "dimension", grains_reserved=True)
            _optional_text(problems, here, item, "column")
            _optional_text(problems, here, item, "description")
            column = item.get("column") or item.get("name")
        else:
            problems.add(here, f"must be a column name, or an object with a name, got {type(item).__name__}")
            continue
        if time_column and isinstance(column, str) and column.strip().lower() == time_column.lower():
            problems.add(here, f"is the column of the time dimension, {time_column!r}. Group by it with --grain")
    return names


def _check_measures(problems: _Problems, where: str, value: Any, model: str | None,
                    measures: dict[str, str | None]) -> bool:
    """Check ``measures``, and record each name with its model in ``measures``, which is unique in the file.

    Returns:
        Whether every measure was an object that has a name, so that the metrics can be checked against the names.
    """
    if not isinstance(value, list) or not value:
        problems.add(where, "is required and must be a list of at least one measure")
        return False
    own: list[str] = []
    readable = True
    for place, item in enumerate(value):
        here = f"{where}[{place}]"
        if not isinstance(item, Mapping):
            problems.add(here, f"must be an object with a name, an agg and an expr, got {type(item).__name__}")
            readable = False
            continue
        _unknown_keys(problems, here, item, MEASURE_KEYS)
        name = item.get("name")
        if isinstance(name, str) and name in measures and name not in own:
            problems.add(f"{here}.name", f"{name!r} is already a measure of the semantic model {measures[name]!r}. "
                                         "A measure name is unique in the file, because a metric refers to it by name")
            own.append(name)
        else:
            added = _check_name(problems, f"{here}.name", name, own, "measure")
            if added is not None:
                measures[added] = model
            else:
                readable = False
        agg = item.get("agg")
        if agg not in AGGREGATIONS:
            problems.add(f"{here}.agg", f"is required and must be one of {list(AGGREGATIONS)}"
                         + (f", got {agg!r}" if agg is not None else ""))
        expr = item.get("expr")
        if expr is None:
            if agg in AGGREGATIONS and agg != "count":
                problems.add(f"{here}.expr", f"is required for {agg!r}. Only a count of rows has no expression")
        elif not _is_text(expr):
            problems.add(f"{here}.expr", f"must be a SQL expression of the columns of the table, got {expr!r}")
        elif len(expr) > _MAX_EXPR:
            problems.add(f"{here}.expr", f"is {len(expr)} characters long, and the limit is {_MAX_EXPR}")
        _optional_text(problems, here, item, "description")
    return readable


def _check_models(problems: _Problems, value: Any) -> tuple[dict[str, str | None], dict[str, list[str]], bool]:
    """Check ``semantic_models``.

    Returns ``{measure: model}``, ``{model: dimension names}``, and whether every model could be read for its measures,
    which is what the references of the metrics can be checked against.
    """
    measures: dict[str, str | None] = {}
    dimensions: dict[str, list[str]] = {}
    if not isinstance(value, list) or not value:
        problems.add("semantic_models", "is required and must be a list of at least one semantic model")
        return measures, dimensions, False
    names: list[str] = []
    complete = True
    for place, model in enumerate(value):
        here = f"semantic_models[{place}]"
        if not isinstance(model, Mapping):
            problems.add(here, f"must be an object with a name, a table and measures, got {type(model).__name__}")
            complete = False
            continue
        _unknown_keys(problems, here, model, MODEL_KEYS)
        name = _check_name(problems, f"{here}.name", model.get("name"), names, "semantic model")
        _check_string(problems, model, "table", here)
        _optional_text(problems, here, model, "description")
        time_column = None
        if "time_dimension" in model:
            time_column = _check_time_dimension(problems, f"{here}.time_dimension", model["time_dimension"])
        found = _check_dimensions(problems, f"{here}.dimensions", model.get("dimensions", []), time_column)
        if name is not None:
            dimensions[name] = found
        if not _check_measures(problems, f"{here}.measures", model.get("measures"), name, measures):
            complete = False
    return measures, dimensions, complete


def _check_metric_measure(problems: _Problems, where: str, value: Any, measures: Mapping[str, str | None],
                          references: bool) -> None:
    if not isinstance(value, str) or not value.strip():
        problems.add(where, "is required and must be the name of a measure")
    elif references and value not in measures:
        problems.add(where, f"unknown measure {value!r}; the measures are {sorted(measures)}")


def _check_metrics(problems: _Problems, value: Any, measures: Mapping[str, str | None],
                   dimensions: Mapping[str, list[str]], owner: Any, references: bool) -> None:
    if not isinstance(value, list) or not value:
        problems.add("metrics", "is required and must be a list of at least one metric")
        return
    names: list[str] = []
    for place, metric in enumerate(value):
        here = f"metrics[{place}]"
        if not isinstance(metric, Mapping):
            problems.add(here, f"must be an object with a name, a type and a description, got "
                               f"{type(metric).__name__}")
            continue
        _unknown_keys(problems, here, metric, METRIC_KEYS)
        _check_name(problems, f"{here}.name", metric.get("name"), names, "metric", grains_reserved=True)
        kind = metric.get("type")
        if kind not in METRIC_TYPES:
            problems.add(f"{here}.type", f"is required and must be one of {list(METRIC_TYPES)}"
                         + (f", got {kind!r}" if kind is not None else ""))
        if not _is_text(metric.get("description")):
            problems.add(f"{here}.description", "is required, and says what the metric means")
        if not _is_text(metric.get("owner")) and not _is_text(owner) and (owner is None or isinstance(owner, str)):
            problems.add(f"{here}.owner", "is required, unless the file has a who that owns every metric")
        elif "owner" in metric and not _is_text(metric["owner"]):
            problems.add(f"{here}.owner", f"must be a non-empty string, got {metric['owner']!r}")
        if "format" in metric and metric["format"] not in FORMATS:
            problems.add(f"{here}.format", f"must be one of {list(FORMATS)}, got {metric['format']!r}")
        used: list[str] = []
        if kind == "simple":
            for key in ("numerator", "denominator"):
                if key in metric:
                    problems.add(f"{here}.{key}", "is for a ratio. A simple metric has one measure")
            _check_metric_measure(problems, f"{here}.measure", metric.get("measure"), measures, references)
            used = [metric.get("measure")]
        elif kind == "ratio":
            if "measure" in metric:
                problems.add(f"{here}.measure", "is for a simple metric. A ratio has a numerator and a denominator")
            for key in ("numerator", "denominator"):
                _check_metric_measure(problems, f"{here}.{key}", metric.get(key), measures, references)
            used = [metric.get("numerator"), metric.get("denominator")]
        models = sorted({measures[item] for item in used if isinstance(item, str) and measures.get(item)})
        if len(models) > 1:
            problems.add(here, f"is made of measures of {len(models)} semantic models, {models}. A metric reads one "
                               "table, so its measures have to be of one semantic model")
        elif len(models) == 1 and metric.get("name") in dimensions.get(models[0], []):
            problems.add(f"{here}.name", f"is also a dimension of the semantic model {models[0]!r}, and a result "
                                         "would have two columns of that name")


def validate_metrics(spec: Mapping[str, Any]) -> list[ConfigError]:
    """The problems of a metrics file, without reading any table.

    It checks the keys, the identifier, the names, the grains and aggregations, that each metric has a description and
    an owner, and that each metric refers to measures that exist, of one semantic model. Whether the tables, columns and
    expressions exist is checked by :func:`local_data_platform.metrics.check.validate_folder`.

    Args:
        spec: The file as a mapping.

    Returns:
        One :class:`~local_data_platform.exceptions.ConfigError` per problem, each message starting with the JSON path
        of the offending value. Empty when valid.
    """
    problems = _Problems()
    _unknown_keys(problems, "", spec, METRICS_KEYS)
    if "apiVersion" in spec and spec["apiVersion"] != API_VERSION:
        problems.add("apiVersion", f"must be {API_VERSION!r}, got {spec['apiVersion']!r}")
    _check_string(problems, spec, "identifier", "", required=True)
    for key in ("who", "what", "where", "when", "how"):
        if key in spec and not isinstance(spec[key], str):
            problems.add(key, f"must be a string, got {type(spec[key]).__name__}")
    measures, dimensions, complete = _check_models(problems, spec.get("semantic_models"))
    _check_metrics(problems, spec.get("metrics"), measures, dimensions, spec.get("who"), complete)
    _check_secrets(problems, spec, "")
    return problems.errors


def describe_metrics(spec: Mapping[str, Any]) -> dict[str, Any]:
    """What ``ldp plan`` reports about a valid metrics file: the names of its semantic models and metrics."""
    return {"semantic_models": [item["name"] for item in spec["semantic_models"]],
            "metrics": [item["name"] for item in spec["metrics"]]}


# --------------------------------------------------------------------------- reading a file


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def build(spec: Mapping[str, Any], file: str = "") -> tuple[tuple[SemanticModel, ...], tuple[Metric, ...]]:
    """The semantic models and metrics of a metrics file that :func:`validate_metrics` found no problem in.

    Args:
        spec: The file as a mapping.
        file: The name of the file, which each model and metric remembers.
    """
    models = []
    owner_of: dict[str, str] = {}
    for item in spec["semantic_models"]:
        time = item.get("time_dimension")
        dimensions = tuple(
            Dimension(name=entry, column=entry) if isinstance(entry, str)
            else Dimension(name=entry["name"], column=_text(entry.get("column")) or entry["name"],
                           description=_text(entry.get("description")))
            for entry in item.get("dimensions", []))
        measures = tuple(Measure(name=entry["name"], agg=entry["agg"], expr=_text(entry.get("expr")),
                                 description=_text(entry.get("description"))) for entry in item["measures"])
        for entry in measures:
            owner_of[entry.name] = item["name"]
        models.append(SemanticModel(
            name=item["name"], table=item["table"].strip(), description=_text(item.get("description")),
            time_dimension=TimeDimension(column=time["column"].strip(), grains=tuple(time["grains"]))
            if time is not None else None, dimensions=dimensions, measures=measures, file=file))
    default_owner = _text(spec.get("who"))
    metrics = []
    for item in spec["metrics"]:
        used = [item.get(key) for key in ("measure", "numerator", "denominator") if item.get(key)]
        metrics.append(Metric(
            name=item["name"], type=item["type"], description=item["description"].strip(),
            owner=_text(item.get("owner")) or default_owner or "", format=item.get("format", DEFAULT_FORMAT),
            model=owner_of[used[0]], measure=item.get("measure"), numerator=item.get("numerator"),
            denominator=item.get("denominator"), file=file))
    return tuple(models), tuple(metrics)


# --------------------------------------------------------------------------- the JSON Schema


def metrics_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) of a metrics file."""
    string = {"type": "string"}
    text = {"type": "string", "minLength": 1}
    name = {"type": "string", "pattern": f"^{NAME_PATTERN}$"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local-data-platform.readthedocs.io/schemas/ldp-v1-metrics.json",
        "title": f"local-data-platform metrics ({API_VERSION})",
        "type": "object",
        "required": ["kind", "identifier", "semantic_models", "metrics"],
        "additionalProperties": False,
        "properties": {
            "apiVersion": {"const": API_VERSION, "default": API_VERSION},
            "kind": {"const": KIND, "description": "A metrics file says so, because a file without a kind is a "
                                                   "Pipeline."},
            "identifier": {**text, "description": "The id of the file."},
            "who": {**string, "description": "Who owns the metrics that do not name an owner of their own."},
            "what": {**string, "description": "What the metrics are for."},
            "where": {**string, "description": "Where the numbers come from."},
            "when": {**string, "description": "How often they change."},
            "how": {**string, "description": "How they are made."},
            "semantic_models": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/semantic_model"},
                                "description": "The gold tables that metrics are asked of."},
            "metrics": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/metric"},
                        "description": "The numbers, each with a meaning, an owner and a format."},
        },
        "$defs": {
            "semantic_model": {
                "type": "object", "required": ["name", "table", "measures"], "additionalProperties": False,
                "properties": {
                    "name": {**name, "description": "The name of the model."},
                    "description": {**text, "description": "What the table is for."},
                    "table": {**text, "examples": ["gold.gold_daily_sales"],
                              "description": "A gold table, as <namespace>.<table>, or its bare name when only one "
                                             "gold table has it."},
                    "time_dimension": {
                        "type": "object", "required": ["column", "grains"], "additionalProperties": False,
                        "description": "The date column and the grains that the numbers may be reported at.",
                        "properties": {
                            "column": {**text, "description": "A DATE or TIMESTAMP column."},
                            "grains": {"type": "array", "minItems": 1, "uniqueItems": True,
                                       "items": {"enum": list(GRAINS)}}}},
                    "dimensions": {"type": "array", "items": {"oneOf": [name, {"$ref": "#/$defs/dimension"}]},
                                   "description": "Columns to group and filter by. A bare name is a column of that "
                                                  "name."},
                    "measures": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/measure"}},
                },
            },
            "dimension": {
                "type": "object", "required": ["name"], "additionalProperties": False,
                "properties": {"name": name, "column": {**text, "description": "The column, when it is not called "
                                                                               "like the dimension."},
                               "description": text},
            },
            "measure": {
                "type": "object", "required": ["name", "agg"], "additionalProperties": False,
                "properties": {
                    "name": {**name, "description": "Unique in the file, because metrics refer to it."},
                    "agg": {"enum": list(AGGREGATIONS)},
                    "expr": {"type": "string", "minLength": 1, "maxLength": _MAX_EXPR,
                             "examples": ["net_revenue", "price * quantity"],
                             "description": "A DuckDB expression of the columns of the table. A count of rows has "
                                            "none."},
                    "description": text,
                },
                "if": {"properties": {"agg": {"not": {"const": "count"}}}, "required": ["agg"]},
                "then": {"required": ["expr"]},
            },
            "metric": {
                "type": "object", "required": ["name", "type", "description"], "additionalProperties": False,
                "properties": {
                    "name": {**name, "description": "What a person asks for, in ldp metrics query."},
                    "type": {"enum": list(METRIC_TYPES)},
                    "description": {**text, "description": "What the metric means."},
                    "owner": {**text, "description": "Who answers for it. The who of the file when it is left out."},
                    "format": {"enum": list(FORMATS), "default": DEFAULT_FORMAT,
                               "description": "How a person reads the value. A percent is a fraction that shows as "
                                              "a percentage."},
                    "measure": {**name, "description": "The measure of a simple metric."},
                    "numerator": {**name, "description": "The measure on top of a ratio."},
                    "denominator": {**name, "description": "The measure underneath it."},
                },
                "oneOf": [
                    {"properties": {"type": {"const": "simple"}}, "required": ["measure"],
                     "not": {"anyOf": [{"required": ["numerator"]}, {"required": ["denominator"]}]}},
                    {"properties": {"type": {"const": "ratio"}}, "required": ["numerator", "denominator"],
                     "not": {"required": ["measure"]}},
                ],
            },
        },
    }


__all__ = [
    "AGGREGATIONS",
    "DEFAULT_FORMAT",
    "Dimension",
    "FORMATS",
    "GRAINS",
    "KIND",
    "METRICS_KEYS",
    "METRIC_TYPES",
    "Measure",
    "Metric",
    "NAME_PATTERN",
    "SemanticModel",
    "TimeDimension",
    "build",
    "describe_metrics",
    "metrics_schema",
    "validate_metrics",
]
