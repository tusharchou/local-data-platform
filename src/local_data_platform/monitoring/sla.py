"""SLA settings: how fresh, how big and how stable a table should be.

A config writes them down in an optional ``metadata.sla`` block, next to the pipeline that loads the table::

    "sla": {
      "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"},
                    "column": "ordered_at"},
      "volume": {"min_rows": 1, "max_rows": 1000000, "max_change_pct": 50, "lookback_runs": 7},
      "schema": {"on_change": "warn", "allow": ["added_column", "widened_type"]}
    }

A block has at least one of the three sections. ``freshness`` is how old the table may get. ``warn_after`` and
``error_after`` are each a ``count`` of ``minute``, ``hour`` or ``day`` periods, where a day is 24 hours, a section has
one or both, and ``warn_after`` is the shorter one. ``column`` names the date or timestamp column whose newest value
says how fresh the data is, and without it the table is as old as its last commit. ``volume`` is how many rows a run
writes. ``min_rows`` and ``max_rows`` bound the latest run, and ``max_change_pct`` is how far in percent it may be from
the median of the runs before it, among the last ``lookback_runs`` runs, which is 7 by default. A section has at
least one of the three rules. ``schema`` is what a change of the columns of the table is worth. ``on_change`` is
``ignore``, ``warn`` (the default) or ``error``, and ``allow`` lists the changes that stay fine, from ``added_column``
and ``widened_type``.

The spec hash leaves the block out, because how a table is watched does not change what a run writes. This module
checks and normalises the block. It reads no table and writes nothing, and it imports nothing outside the standard
library when it is imported, so the spec and the config can use it.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from local_data_platform.exceptions import ConfigError

SLA_KEYS = ("freshness", "volume", "schema")
"""The sections of a ``metadata.sla`` block."""

DURATION_KEYS = ("count", "period")
"""The keys of a length of time, such as ``{"count": 26, "period": "hour"}``."""

FRESHNESS_KEYS = ("warn_after", "error_after", "column")
"""The keys of the ``freshness`` section."""

VOLUME_KEYS = ("min_rows", "max_rows", "max_change_pct", "lookback_runs")
"""The keys of the ``volume`` section."""

SCHEMA_KEYS = ("on_change", "allow")
"""The keys of the ``schema`` section."""

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

    def summary(self) -> str:
        """The section in a few words, such as ``min 1 row``."""
        rules = []
        if self.min_rows is not None:
            rules.append(f"min {_count(self.min_rows, 'row')}")
        if self.max_rows is not None:
            rules.append(f"max {_count(self.max_rows, 'row')}")
        if self.max_change_pct is not None:
            rules.append(f"change over {self.max_change_pct:g} percent of the last {_count(self.lookback_runs, 'run')}")
        return " and ".join(rules)


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
class Sla:
    """A normalised ``metadata.sla`` block, with the defaults applied.

    Attributes:
        freshness: How old the table may get, or ``None`` when the block has no such section.
        volume: How many rows a run writes, or ``None``.
        schema: What a change of the columns is worth, or ``None``.
    """

    freshness: Freshness | None = None
    volume: Volume | None = None
    schema: SchemaDrift | None = None

    def to_dict(self) -> dict[str, Any]:
        """The SLA as a block again, with the defaults in it and without the keys that it has no value for."""
        sections = {"freshness": self.freshness, "volume": self.volume, "schema": self.schema}
        return {key: section.to_dict() for key, section in sections.items() if section is not None}

    def summary(self) -> str:
        """The SLA in a line, such as ``freshness error after 26 hour, volume min 1 row, schema warn``."""
        sections = (("freshness", self.freshness), ("volume", self.volume), ("schema", self.schema))
        return ", ".join(f"{key} {section.summary()}" for key, section in sections if section is not None)


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


def sla_problems(block: Any) -> list[tuple[str, str]]:
    """Every problem of a ``metadata.sla`` block, as ``(path, message)`` with the path relative to the block.

    Args:
        block: The value of ``metadata.sla``. ``None`` is no block, and has no problem.

    Returns:
        The problems, which are empty when the block is fine. The path is ``""`` for the block, ``".volume"`` for a
        section, ``".freshness.error_after.period"`` for a key and ``".schema.allow[1]"`` for an item of a list.
    """
    if block is None:
        return []
    if not isinstance(block, Mapping):
        return [("", f"must be an object with some of {list(SLA_KEYS)}, got {block!r}")]
    problems = _unknown(block, SLA_KEYS, "")
    for key, check in (("freshness", _freshness_problems), ("volume", _volume_problems), ("schema", _schema_problems)):
        if key in block:
            problems.extend(check(block[key]))
    if not block:
        problems.append(("", f"has nothing to check, set at least one of {list(SLA_KEYS)}"))
    return problems


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
    return Sla(freshness=freshness, volume=volume, schema=schema)


def sla_schema() -> dict[str, Any]:
    """The JSON Schema of a ``metadata.sla`` block."""
    duration = {
        "type": "object", "required": list(DURATION_KEYS), "additionalProperties": False,
        "properties": {"count": {"type": "integer", "minimum": 1}, "period": {"enum": list(PERIODS)}},
        "examples": [{"count": 26, "period": "hour"}],
    }
    return {
        "type": "object", "additionalProperties": False, "minProperties": 1,
        "description": "How fresh, how big and how stable the table should be, checked between runs. A block has at "
                       "least one of the three sections. Editing it does not change the spec hash.",
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
        },
    }


__all__ = [
    "CHANGES",
    "DEFAULT_LOOKBACK_RUNS",
    "DEFAULT_ON_CHANGE",
    "Duration",
    "Freshness",
    "ON_CHANGE",
    "PERIODS",
    "PERIOD_SECONDS",
    "SLA_KEYS",
    "SchemaDrift",
    "Sla",
    "Volume",
    "parse_sla",
    "sla_problems",
    "sla_schema",
]
