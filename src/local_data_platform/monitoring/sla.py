"""SLA settings: how fresh, how big and how stable a table should be.

A config writes them down in an optional ``metadata.sla`` block, next to the pipeline that loads the table::

    "sla": {
      "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"},
                    "column": "ordered_at"},
      "volume": {"min_rows": 1, "max_rows": 1000000, "max_change_pct": 50, "lookback_runs": 7},
      "schema": {"on_change": "warn", "allow": ["added_column", "widened_type"]},
      "metrics": [{"metric": "revenue", "grain": "day", "min": 0, "max_change_pct": 50}]
    }

A block has at least one of the four sections. ``freshness`` is how old the table may get. ``warn_after`` and
``error_after`` are each a ``count`` of ``minute``, ``hour`` or ``day`` periods, where a day is 24 hours, a section has
one or both, and ``warn_after`` is the shorter one. ``column`` names the date or timestamp column whose newest value
says how fresh the data is, and without it the table is as old as its last commit. ``volume`` is how many rows a run
writes. ``min_rows`` and ``max_rows`` bound the latest run, and ``max_change_pct`` is how far in percent it may be from
the median of the runs before it, among the last ``lookback_runs`` runs, which is 7 by default. A section has at
least one of the three rules. ``schema`` is what a change of the columns of the table is worth. ``on_change`` is
``ignore``, ``warn`` (the default) or ``error``, and ``allow`` lists the changes that stay fine, from ``added_column``
and ``widened_type``. ``metrics`` is a list of business numbers to watch. Each entry names a metric of the metrics file
that sits next to the config, or gives a query that returns one number, and says at what ``grain`` to look at it. The
monitor checks the value of the latest complete period of that grain against ``min`` and ``max`` and against the
median of the periods before it, which may be ``max_change_pct`` percent away, among the last ``lookback`` periods,
which is 7 by default.

The spec hash leaves the block out, because how a table is watched does not change what a run writes. This module
checks and normalises the block. It reads no table and writes nothing, and it imports nothing outside the standard
library when it is imported, so the spec and the config can use it.
"""

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from local_data_platform.exceptions import ConfigError

SLA_KEYS = ("freshness", "volume", "schema", "metrics")
"""The sections of a ``metadata.sla`` block."""

DURATION_KEYS = ("count", "period")
"""The keys of a length of time, such as ``{"count": 26, "period": "hour"}``."""

FRESHNESS_KEYS = ("warn_after", "error_after", "column")
"""The keys of the ``freshness`` section."""

VOLUME_KEYS = ("min_rows", "max_rows", "max_change_pct", "lookback_runs")
"""The keys of the ``volume`` section."""

SCHEMA_KEYS = ("on_change", "allow")
"""The keys of the ``schema`` section."""

METRIC_KEYS = ("name", "metric", "sql", "grain", "min", "max", "max_change_pct", "filter", "lookback")
"""The keys of an entry of the ``metrics`` section."""

PERIODS = ("minute", "hour", "day")
"""The ``period`` of a length of time. A day is 24 hours."""

PERIOD_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}
"""How many seconds each period is."""

ON_CHANGE = ("ignore", "warn", "error")
"""What ``schema.on_change`` can be."""

DEFAULT_ON_CHANGE = "warn"
"""The ``on_change`` of a ``schema`` section that does not set one."""

CHANGES = ("added_column", "widened_type")
"""The changes that ``schema.allow`` can list."""

DEFAULT_LOOKBACK_RUNS = 7
"""The ``lookback_runs`` of a ``volume`` section that does not set one."""

GRAINS = ("day", "week", "month", "quarter", "year")
"""The ``grain`` of an entry of ``metrics``. They are the grains of :mod:`local_data_platform.metrics`, which this
module cannot import, and a test keeps the two lists the same."""

NAME_PATTERN = r"[a-z][a-z0-9_]{0,62}"
"""A ``name`` of an entry of ``metrics``, written as the names in a metrics file are, and kept the same by a test."""

DEFAULT_LOOKBACK = 7
"""The ``lookback`` of an entry of ``metrics`` that does not set one."""

_NAME = re.compile(NAME_PATTERN)


# ---------------------------------------------------------------------- the block


def _name(value: Any) -> str | None:
    """The text of ``value`` without the space around it, or ``None`` when it is not text or has none."""
    return value.strip() or None if isinstance(value, str) else None


def _whole(value: Any, least: int) -> bool:
    """Whether ``value`` is a whole number of at least ``least``. A bool is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def _number(value: Any) -> bool:
    """Whether ``value`` is a number that is not NaN or infinite. A bool is not one."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def figure(value: int | float) -> str:
    """A number as a person reads it. A whole number is shown as it is, and any other has at most 10 digits."""
    return str(value) if isinstance(value, int) else f"{value:.10g}"


@dataclass(frozen=True)
class Duration:
    """A length of time as an SLA writes it, such as ``{"count": 26, "period": "hour"}``.

    Attributes:
        count: How many periods. At least 1.
        period: ``minute``, ``hour`` or ``day``. A day is 24 hours.
    """

    count: int
    period: str

    @property
    def seconds(self) -> int:
        """The length of time in seconds."""
        return self.count * PERIOD_SECONDS[self.period]

    def to_dict(self) -> dict[str, Any]:
        return {"count": self.count, "period": self.period}

    def __str__(self) -> str:
        return f"{self.count} {self.period}"


@dataclass(frozen=True)
class Freshness:
    """How old a table may get.

    Attributes:
        warn_after: The age from which the table is late, or ``None``.
        error_after: The age from which the table is too late, or ``None``. At least one of the two is set.
        column: The date or timestamp column whose newest value is the age of the data, or ``None`` for the age of the
            last commit.
    """

    warn_after: Duration | None = None
    error_after: Duration | None = None
    column: str | None = None

    def to_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {"warn_after": self.warn_after and self.warn_after.to_dict(),
                                  "error_after": self.error_after and self.error_after.to_dict(), "column": self.column}
        return {key: value for key, value in values.items() if value is not None}

    def summary(self) -> str:
        """The section in a few words, such as ``error after 26 hour``."""
        limits = (("warn", self.warn_after), ("error", self.error_after))
        after = [f"{word} after {duration}" for word, duration in limits if duration is not None]
        return " and ".join(after) + (f" on column {self.column}" if self.column else "")


@dataclass(frozen=True)
class Volume:
    """How many rows a run writes.

    Attributes:
        min_rows: The fewest rows the latest run may write, or ``None``.
        max_rows: The most rows the latest run may write, or ``None``.
        max_change_pct: How far in percent the latest run may be from the median of the runs before it, or ``None``.
        lookback_runs: How many runs, the latest one included, the median is taken over.
    """

    min_rows: int | None = None
    max_rows: int | None = None
    max_change_pct: float | None = None
    lookback_runs: int = DEFAULT_LOOKBACK_RUNS

    def to_dict(self) -> dict[str, Any]:
        values = {"min_rows": self.min_rows, "max_rows": self.max_rows, "max_change_pct": self.max_change_pct,
                  "lookback_runs": self.lookback_runs}
        return {key: value for key, value in values.items() if value is not None}

    def rows_summary(self) -> str:
        """The limits on the rows of the latest run in a few words, such as ``min 1 row``, or nothing without any."""
        rules = []
        if self.min_rows is not None:
            rules.append(f"min {_count(self.min_rows, 'row')}")
        if self.max_rows is not None:
            rules.append(f"max {_count(self.max_rows, 'row')}")
        return " and ".join(rules)

    def change_summary(self) -> str:
        """The limit on the change from the earlier runs in a few words, or nothing when there is none."""
        if self.max_change_pct is None:
            return ""
        return f"change over {self.max_change_pct:g} percent of the last {_count(self.lookback_runs, 'run')}"

    def summary(self) -> str:
        """The section in a few words, such as ``min 1 row``."""
        return " and ".join(rule for rule in (self.rows_summary(), self.change_summary()) if rule)


@dataclass(frozen=True)
class SchemaDrift:
    """What a change of the columns of a table is worth.

    Attributes:
        on_change: ``ignore``, ``warn`` or ``error``.
        allow: The changes that stay fine, from :data:`CHANGES`.
    """

    on_change: str = DEFAULT_ON_CHANGE
    allow: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"on_change": self.on_change, "allow": list(self.allow)}

    def summary(self) -> str:
        """The section in a few words, such as ``warn except added_column``."""
        return self.on_change + (f" except {' and '.join(self.allow)}" if self.allow else "")


@dataclass(frozen=True)
class MetricRule:
    """One business number that the SLA watches.

    The number is a metric of the metrics file next to the config, or the one number that a query returns. It is
    measured for the latest complete period of ``grain``, and checked against ``min`` and ``max`` and against the median
    of the periods before it.

    Attributes:
        grain: How long a period is, one of :data:`GRAINS`. A metric is computed for the latest complete period of it.
        metric: The name of a metric, or ``None`` when ``sql`` gives the number.
        sql: One read-only ``SELECT`` or ``WITH`` query that returns one number, or ``None`` when ``metric`` does.
        name: What the monitors of the entry are called, or ``None`` for a metric, which is then called by its name.
            A query has to have one.
        min: The least that the value may be, or ``None``.
        max: The most that the value may be, or ``None``.
        max_change_pct: How far in percent the value may be from the median of the periods before it, or ``None``.
        filter: ``{dimension: value or values}`` that narrows a metric, or ``None``. A query filters in its own text.
        lookback: How many periods, the latest one included, the median is taken over.
    """

    grain: str
    metric: str | None = None
    sql: str | None = None
    name: str | None = None
    min: float | None = None
    max: float | None = None
    max_change_pct: float | None = None
    filter: Mapping[str, Any] | None = field(default=None, hash=False)
    lookback: int = DEFAULT_LOOKBACK

    @property
    def key(self) -> str:
        """What the monitors of the entry are named after, which is its ``name`` and else its metric."""
        return self.name or self.metric or ""

    def definition(self) -> str:
        """A short fingerprint of what the entry measures, so that a history is compared only with its own kind.

        It follows the metric or the query, the grain and the filter. The limits, the lookback and the name are not in
        it, because changing them does not change the numbers that were measured.
        """
        what = {"metric": self.metric, "sql": self.sql, "grain": self.grain, "filter": self.filter}
        return hashlib.sha256(json.dumps(what, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        values = {"name": self.name, "metric": self.metric, "sql": self.sql, "grain": self.grain, "min": self.min,
                  "max": self.max, "max_change_pct": self.max_change_pct,
                  "filter": copy.deepcopy(dict(self.filter)) if self.filter is not None else None,
                  "lookback": self.lookback}
        return {key: value for key, value in values.items() if value is not None}

    def value_summary(self) -> str:
        """The limits on the value in a few words, such as ``min 0 and max 5000``, or nothing without any."""
        rules = []
        if self.min is not None:
            rules.append(f"min {figure(self.min)}")
        if self.max is not None:
            rules.append(f"max {figure(self.max)}")
        return " and ".join(rules)

    def change_summary(self) -> str:
        """The limit on the change from the earlier periods in a few words, or nothing when there is none."""
        if self.max_change_pct is None:
            return ""
        return f"change over {self.max_change_pct:g} percent of the last {_count(self.lookback, self.grain)}"

    def summary(self) -> str:
        """The entry in a few words, such as ``revenue by day``."""
        return f"{self.key} by {self.grain}"


@dataclass(frozen=True)
class Sla:
    """A normalised ``metadata.sla`` block, with the defaults applied.

    Attributes:
        freshness: How old the table may get, or ``None`` when the block has no such section.
        volume: How many rows a run writes, or ``None``.
        schema: What a change of the columns is worth, or ``None``.
        metrics: The business numbers to watch, which is empty when the block has no such section.
    """

    freshness: Freshness | None = None
    volume: Volume | None = None
    schema: SchemaDrift | None = None
    metrics: tuple[MetricRule, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """The SLA as a block again, with the defaults in it and without the keys that it has no value for."""
        sections = {"freshness": self.freshness, "volume": self.volume, "schema": self.schema}
        block: dict[str, Any] = {key: section.to_dict() for key, section in sections.items() if section is not None}
        if self.metrics:
            block["metrics"] = [rule.to_dict() for rule in self.metrics]
        return block

    def summary(self) -> str:
        """The SLA in a line, such as ``freshness error after 26 hour, volume min 1 row, metrics revenue by day``."""
        sections = (("freshness", self.freshness), ("volume", self.volume), ("schema", self.schema))
        parts = [f"{key} {section.summary()}" for key, section in sections if section is not None]
        if self.metrics:
            parts.append("metrics " + " and ".join(rule.summary() for rule in self.metrics))
        return ", ".join(parts)


# ---------------------------------------------------------------------- checking the block


def _unknown(block: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> list[tuple[str, str]]:
    unknown = sorted(str(key) for key in block if key not in allowed)
    return [(where, f"has unknown keys {unknown}; allowed: {list(allowed)}")] if unknown else []


def _duration(value: Any) -> Duration | None:
    """The length of time that ``value`` says, or ``None`` when ``value`` is not a valid one."""
    if (isinstance(value, Mapping) and set(map(str, value)) == set(DURATION_KEYS) and _whole(value["count"], 1)
            and value["period"] in PERIODS):
        return Duration(count=value["count"], period=value["period"])
    return None


def _duration_problems(value: Any, where: str) -> list[tuple[str, str]]:
    if not isinstance(value, Mapping):
        return [(where, f"must be an object such as {{'count': 26, 'period': 'hour'}}, got {value!r}")]
    problems = _unknown(value, DURATION_KEYS, where)
    if "count" not in value:
        problems.append((f"{where}.count", "is required"))
    elif not _whole(value["count"], 1):
        problems.append((f"{where}.count", f"must be a whole number from 1, got {value['count']!r}"))
    if "period" not in value:
        problems.append((f"{where}.period", f"is required, one of {list(PERIODS)}"))
    elif value["period"] not in PERIODS:
        problems.append((f"{where}.period", f"must be one of {list(PERIODS)}, got {value['period']!r}"))
    return problems


def _freshness_problems(block: Any) -> list[tuple[str, str]]:
    where = ".freshness"
    if not isinstance(block, Mapping):
        return [(where, f"must be an object with warn_after, error_after and column, got {block!r}")]
    problems = _unknown(block, FRESHNESS_KEYS, where)
    for key in ("warn_after", "error_after"):
        if key in block:
            problems.extend(_duration_problems(block[key], f"{where}.{key}"))
    if "column" in block and _name(block["column"]) is None:
        problems.append((f"{where}.column", f"must be a column name, got {block['column']!r}"))
    if "warn_after" not in block and "error_after" not in block:
        problems.append((where, "has no threshold, set warn_after, error_after or both"))
    else:
        warn, error = _duration(block.get("warn_after")), _duration(block.get("error_after"))
        if warn is not None and error is not None and warn.seconds >= error.seconds:
            problems.append((f"{where}.warn_after", f"must be shorter than error_after, got {warn} and {error}"))
    return problems


def _volume_problems(block: Any) -> list[tuple[str, str]]:
    where = ".volume"
    if not isinstance(block, Mapping):
        return [(where, f"must be an object with min_rows, max_rows, max_change_pct and lookback_runs, got {block!r}")]
    problems = _unknown(block, VOLUME_KEYS, where)
    for key in ("min_rows", "max_rows", "lookback_runs"):
        if key in block and not _whole(block[key], 1):
            problems.append((f"{where}.{key}", f"must be a whole number from 1, got {block[key]!r}"))
    if "max_change_pct" in block and not (_number(block["max_change_pct"]) and block["max_change_pct"] > 0):
        problems.append((f"{where}.max_change_pct", f"must be a number above 0, got {block['max_change_pct']!r}"))
    if not any(key in block for key in ("min_rows", "max_rows", "max_change_pct")):
        problems.append((where, "has no rule, set min_rows, max_rows or max_change_pct"))
    else:
        least, most = block.get("min_rows"), block.get("max_rows")
        if _whole(least, 1) and _whole(most, 1) and least > most:
            problems.append((f"{where}.min_rows", f"must not be more than max_rows, got {least} and {most}"))
    return problems


def _schema_problems(block: Any) -> list[tuple[str, str]]:
    where = ".schema"
    if not isinstance(block, Mapping):
        return [(where, f"must be an object with on_change and allow, got {block!r}")]
    problems = _unknown(block, SCHEMA_KEYS, where)
    if "on_change" in block and block["on_change"] not in ON_CHANGE:
        problems.append((f"{where}.on_change", f"must be one of {list(ON_CHANGE)}, got {block['on_change']!r}"))
    if "allow" in block:
        allow = block["allow"]
        if not isinstance(allow, (list, tuple)):
            problems.append((f"{where}.allow", f"must be a list of changes from {list(CHANGES)}, got {allow!r}"))
        else:
            for index, change in enumerate(allow):
                if change not in CHANGES:
                    problems.append((f"{where}.allow[{index}]", f"must be one of {list(CHANGES)}, got {change!r}"))
            known = [change for change in allow if change in CHANGES]
            if len(set(known)) != len(known):
                problems.append((f"{where}.allow", "lists a change twice"))
    return problems


def _scalar(value: Any) -> bool:
    """Whether ``value`` can be the value of a filter, which is text, a bool or a number that is not NaN or infinite."""
    return isinstance(value, (str, bool)) or _number(value)


def _filter_problems(value: Any, where: str) -> list[tuple[str, str]]:
    if not isinstance(value, Mapping) or not value:
        return [(where, f"must be an object with a dimension and its value or values, got {value!r}")]
    problems = []
    for dimension, wanted in value.items():
        if not isinstance(dimension, str) or not dimension.strip():
            problems.append((where, f"has a key that is not the name of a dimension, got {dimension!r}"))
        elif isinstance(wanted, (list, tuple)):
            if not wanted or not all(_scalar(item) for item in wanted):
                problems.append((f"{where}.{dimension}",
                                 f"must be a value or a list of values, each text, a number or a bool, got {wanted!r}"))
        elif not _scalar(wanted):
            problems.append((f"{where}.{dimension}",
                             f"must be a value or a list of values, each text, a number or a bool, got {wanted!r}"))
    return problems


def _entry_problems(entry: Any, where: str) -> list[tuple[str, str]]:
    if not isinstance(entry, Mapping):
        return [(where, f"must be an object with metric or sql, grain and a rule, got {entry!r}")]
    problems = _unknown(entry, METRIC_KEYS, where)
    has_metric, has_sql = "metric" in entry, "sql" in entry
    if has_metric and has_sql:
        problems.append((where, "has both metric and sql, set one of them"))
    elif not has_metric and not has_sql:
        problems.append((where, "has neither metric nor sql, set one of them"))
    if has_metric and _name(entry["metric"]) is None:
        problems.append((f"{where}.metric", f"must be the name of a metric, got {entry['metric']!r}"))
    if has_sql:
        if _name(entry["sql"]) is None:
            problems.append((f"{where}.sql", f"must be a query that returns one number, got {entry['sql']!r}"))
        if "name" not in entry:
            problems.append((f"{where}.name", "is required with sql, because a query has no name of its own"))
        if "filter" in entry:
            problems.append((f"{where}.filter", "is for a metric, and a query says in its own text what to keep"))
    if "name" in entry and not (isinstance(entry["name"], str) and _NAME.fullmatch(entry["name"])):
        problems.append((f"{where}.name", "must be lower case letters, digits and underscores that start with a "
                                          f"letter, got {entry['name']!r}"))
    if "filter" in entry and not has_sql:
        problems.extend(_filter_problems(entry["filter"], f"{where}.filter"))
    if "grain" not in entry:
        problems.append((f"{where}.grain", f"is required, one of {list(GRAINS)}"))
    elif entry["grain"] not in GRAINS:
        problems.append((f"{where}.grain", f"must be one of {list(GRAINS)}, got {entry['grain']!r}"))
    for key in ("min", "max"):
        if key in entry and not _number(entry[key]):
            problems.append((f"{where}.{key}", f"must be a number, got {entry[key]!r}"))
    if "max_change_pct" in entry and not (_number(entry["max_change_pct"]) and entry["max_change_pct"] > 0):
        problems.append((f"{where}.max_change_pct", f"must be a number above 0, got {entry['max_change_pct']!r}"))
    if "lookback" in entry and not _whole(entry["lookback"], 1):
        problems.append((f"{where}.lookback", f"must be a whole number from 1, got {entry['lookback']!r}"))
    if not any(key in entry for key in ("min", "max", "max_change_pct")):
        problems.append((where, "has no rule, set min, max or max_change_pct"))
    else:
        least, most = entry.get("min"), entry.get("max")
        if _number(least) and _number(most) and least > most:
            problems.append((f"{where}.min", f"must not be more than max, got {least} and {most}"))
    return problems


def _key_of(entry: Mapping[str, Any]) -> str | None:
    """What the monitors of an entry are named after, from the raw entry. ``None`` when it has no usable name."""
    for key in ("name", "metric"):
        if isinstance(entry.get(key), str) and entry[key].strip():
            return entry[key].strip()
    return None


def _metrics_problems(block: Any) -> list[tuple[str, str]]:
    where = ".metrics"
    if not isinstance(block, (list, tuple)):
        return [(where, f"must be a list of the metrics to watch, got {block!r}")]
    if not block:
        return [(where, "has nothing to watch, list at least one metric")]
    problems: list[tuple[str, str]] = []
    first: dict[str, int] = {}
    for index, entry in enumerate(block):
        here = f"{where}[{index}]"
        problems.extend(_entry_problems(entry, here))
        key = _key_of(entry) if isinstance(entry, Mapping) else None
        if key is None:
            continue
        if key in first:
            path = f"{here}.name" if "name" in entry else f"{here}.metric"
            problems.append((path, f"{key!r} is also used by metrics[{first[key]}], so set a name that tells the two "
                                   "entries apart"))
        else:
            first[key] = index
    return problems


def sla_problems(block: Any) -> list[tuple[str, str]]:
    """Every problem of a ``metadata.sla`` block, as ``(path, message)`` with the path relative to the block.

    Args:
        block: The value of ``metadata.sla``. ``None`` is no block, and has no problem.

    Returns:
        The problems, which are empty when the block is fine. The path is ``""`` for the block, ``".volume"`` for a
        section, ``".freshness.error_after.period"`` for a key and ``".metrics[1].grain"`` for a key of an item of a
        list.
    """
    if block is None:
        return []
    if not isinstance(block, Mapping):
        return [("", f"must be an object with some of {list(SLA_KEYS)}, got {block!r}")]
    problems = _unknown(block, SLA_KEYS, "")
    for key, check in (("freshness", _freshness_problems), ("volume", _volume_problems), ("schema", _schema_problems),
                       ("metrics", _metrics_problems)):
        if key in block:
            problems.extend(check(block[key]))
    if not block:
        problems.append(("", f"has nothing to check, set at least one of {list(SLA_KEYS)}"))
    return problems


def _metric_rule(raw: Mapping[str, Any]) -> MetricRule:
    return MetricRule(grain=raw["grain"], metric=_name(raw.get("metric")), sql=_name(raw.get("sql")),
                      name=raw.get("name"), min=raw.get("min"), max=raw.get("max"),
                      max_change_pct=raw.get("max_change_pct"),
                      filter=copy.deepcopy(dict(raw["filter"])) if "filter" in raw else None,
                      lookback=raw.get("lookback", DEFAULT_LOOKBACK))


def parse_sla(block: Any) -> Sla | None:
    """Normalise a ``metadata.sla`` block and apply its defaults.

    Args:
        block: The value of ``metadata.sla``.

    Returns:
        The :class:`Sla`, or ``None`` when there is no block.

    Raises:
        ConfigError: If the block is not valid. The message names the first problem, as
            ``metadata.sla.freshness.error_after.period: ...``.
    """
    problems = sla_problems(block)
    if problems:
        path, message = problems[0]
        raise ConfigError(f"metadata.sla{path}: {message}")
    if block is None:
        return None
    freshness = volume = schema = None
    if "freshness" in block:
        raw = block["freshness"]
        freshness = Freshness(warn_after=_duration(raw.get("warn_after")),
                              error_after=_duration(raw.get("error_after")), column=_name(raw.get("column")))
    if "volume" in block:
        raw = block["volume"]
        volume = Volume(min_rows=raw.get("min_rows"), max_rows=raw.get("max_rows"),
                        max_change_pct=raw.get("max_change_pct"),
                        lookback_runs=raw.get("lookback_runs", DEFAULT_LOOKBACK_RUNS))
    if "schema" in block:
        raw = block["schema"]
        schema = SchemaDrift(on_change=raw.get("on_change", DEFAULT_ON_CHANGE), allow=tuple(raw.get("allow", ())))
    metrics = tuple(_metric_rule(item) for item in block.get("metrics", ()))
    return Sla(freshness=freshness, volume=volume, schema=schema, metrics=metrics)


def sla_schema() -> dict[str, Any]:
    """The JSON Schema of a ``metadata.sla`` block."""
    duration = {
        "type": "object", "required": list(DURATION_KEYS), "additionalProperties": False,
        "properties": {"count": {"type": "integer", "minimum": 1}, "period": {"enum": list(PERIODS)}},
        "examples": [{"count": 26, "period": "hour"}],
    }
    return {
        "type": "object", "additionalProperties": False, "minProperties": 1,
        "description": "How fresh, how big and how stable the table should be, and which business numbers to "
                       "watch, checked between runs. A block has at least one of the four sections. Editing it does "
                       "not change the spec hash.",
        "properties": {
            "freshness": {
                "type": "object", "additionalProperties": False,
                "anyOf": [{"required": ["warn_after"]}, {"required": ["error_after"]}],
                "description": "How old the table may get. At least one of warn_after and error_after, and warn_after "
                               "is the shorter one. A day is 24 hours.",
                "properties": {
                    "warn_after": {**duration, "description": "The age from which the table is late."},
                    "error_after": {**duration, "description": "The age from which the table is too late."},
                    "column": {"type": "string", "minLength": 1,
                               "description": "The date or timestamp column whose newest value says how fresh the data "
                                              "is. Without it the table is as old as its last commit."},
                },
            },
            "volume": {
                "type": "object", "additionalProperties": False,
                "anyOf": [{"required": ["min_rows"]}, {"required": ["max_rows"]}, {"required": ["max_change_pct"]}],
                "description": "How many rows a run writes. At least one of min_rows, max_rows and max_change_pct.",
                "properties": {
                    "min_rows": {"type": "integer", "minimum": 1, "description": "The fewest rows of the latest run."},
                    "max_rows": {"type": "integer", "minimum": 1, "description": "The most rows of the latest run."},
                    "max_change_pct": {"type": "number", "exclusiveMinimum": 0,
                                       "description": "How far in percent the latest run may be from the median of the "
                                                      "runs before it."},
                    "lookback_runs": {"type": "integer", "minimum": 1, "default": DEFAULT_LOOKBACK_RUNS,
                                      "description": "How many runs, the latest one included, the median is taken "
                                                     "over."},
                },
            },
            "schema": {
                "type": "object", "additionalProperties": False,
                "description": "What a change of the columns of the table is worth.",
                "properties": {
                    "on_change": {"enum": list(ON_CHANGE), "default": DEFAULT_ON_CHANGE,
                                  "description": "The severity of a change that is not allowed."},
                    "allow": {"type": "array", "items": {"enum": list(CHANGES)}, "uniqueItems": True,
                              "description": "The changes that stay fine."},
                },
            },
            "metrics": {
                "type": "array", "minItems": 1,
                "description": "Business numbers to watch. Each entry is a metric of the metrics file next to the "
                               "config, or a query that returns one number, and it is measured for the latest "
                               "complete period of its grain.",
                "items": {
                    "type": "object", "additionalProperties": False, "required": ["grain"],
                    "oneOf": [{"required": ["metric"]}, {"required": ["sql", "name"]}],
                    "anyOf": [{"required": ["min"]}, {"required": ["max"]}, {"required": ["max_change_pct"]}],
                    "properties": {
                        "name": {"type": "string", "pattern": f"^{NAME_PATTERN}$",
                                 "description": "What the monitors of the entry are called. A query needs one, and a "
                                                "metric is called by its name without one."},
                        "metric": {"type": "string", "minLength": 1,
                                   "description": "The name of a metric of the metrics file next to the config."},
                        "sql": {"type": "string", "minLength": 1,
                                "description": "One read-only SELECT or WITH query that returns one number, for a "
                                               "folder that has no metrics file. The table is called by its name."},
                        "grain": {"enum": list(GRAINS),
                                  "description": "How long a period is. The latest complete period is measured."},
                        "min": {"type": "number", "description": "The least that the value may be."},
                        "max": {"type": "number", "description": "The most that the value may be."},
                        "max_change_pct": {"type": "number", "exclusiveMinimum": 0,
                                           "description": "How far in percent the value may be from the median of "
                                                          "the periods before it."},
                        "filter": {
                            "type": "object", "minProperties": 1,
                            "description": "Dimensions of the metric and the value or values to keep, such as "
                                           "{\"city\": \"Pune\"}.",
                            "additionalProperties": {"anyOf": [
                                {"type": ["string", "number", "boolean"]},
                                {"type": "array", "minItems": 1, "items": {"type": ["string", "number", "boolean"]}}]},
                        },
                        "lookback": {"type": "integer", "minimum": 1, "default": DEFAULT_LOOKBACK,
                                     "description": "How many periods, the latest one included, the median is taken "
                                                    "over."},
                    },
                },
            },
        },
    }


__all__ = [
    "CHANGES",
    "DEFAULT_LOOKBACK",
    "DEFAULT_LOOKBACK_RUNS",
    "DEFAULT_ON_CHANGE",
    "Duration",
    "Freshness",
    "GRAINS",
    "METRIC_KEYS",
    "MetricRule",
    "NAME_PATTERN",
    "ON_CHANGE",
    "PERIODS",
    "PERIOD_SECONDS",
    "SLA_KEYS",
    "SchemaDrift",
    "Sla",
    "Volume",
    "figure",
    "parse_sla",
    "sla_problems",
    "sla_schema",
]
