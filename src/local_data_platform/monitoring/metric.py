"""The metric monitors: a business number, such as the revenue of yesterday, and how it compares with the days before.

An entry of the ``metrics`` section of an SLA is either the name of a metric that the metrics file next to the config
defines, or one query that returns one number. The monitor measures it for the latest complete period of the entry's
``grain`` in UTC, which at 10:00 on a Wednesday is Tuesday for the grain ``day`` and the week that ended on Sunday for
the grain ``week``. A metric is computed by :mod:`local_data_platform.metrics` from the current release of the gold
tables, so it is the number that the reports and the agents show. A query runs on the table of the config, through the
same statement guard as the MCP server and a DuckDB engine that has only that table, which it names by its name.

An entry makes up to two results, so that each rule has its own history and its own status, as the volume monitor does.
``metric.<key>.value`` holds the value against ``min`` and ``max``. ``metric.<key>.change`` holds it against the median
of the periods before it, among the last ``lookback`` periods, the latest one included, and it needs 3 earlier values.
The key is the ``name`` of the entry, or its metric. A breach is an ``error``. A period that has no rows, a value
that is NULL and too little history are ``unknown``, which never alerts. A definition that cannot run, such as a metric
that does not exist or a query that the guard rejects, is an ``error``, because a monitor that silently never alerts is
worse than one that says it is broken. Without the duckdb package the result is ``unknown`` and says how to install it.

The earlier values come from the history of the monitors, so there is no query for the days before. The history holds
one look for each time that ``ldp monitor`` ran, and it may run many times in a period, so the baseline takes one value
for each earlier period, the last that was measured, and only from results of the same definition, which is the metric
or the query, the grain and the filter. A history is never mixed with the history of another definition. The history
holds the value and its period and never a row. An error that DuckDB raised about a value of a row is said by its kind
only, because its text may quote the row.

This module needs only the standard library when it is imported. The metrics and DuckDB are imported when a number is
read.
"""

import datetime as dt
import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, EngineNotFound, LDPError, TableNotFound
from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.monitoring.sla import MetricRule, figure
from local_data_platform.monitoring.target import Target

KIND = "metric"
"""The kind of the metric monitors."""

VALUE = "value"
"""The last part of the name of the monitor of ``min`` and ``max``, as in ``metric.revenue.value``."""

CHANGE = "change"
"""The last part of the name of the monitor of ``max_change_pct``."""

MIN_EARLIER_VALUES = 3
"""How many values of earlier periods the change rule needs."""

QUERY_NEEDS_NO_METRICS_FILE = ("this folder has a metrics file, so define the number there and name it with metric. "
                               "A query is for a folder that has no metrics file")
"""Why an entry with ``sql`` is refused in a folder that has a metrics file."""

_UTC = dt.timezone.utc
_NAMES_ONLY = ("BinderException", "CatalogException", "ParserException")
"""The kinds of DuckDB error that speak about the text of a query, and not about a value of a row."""


def monitor_name(rule: MetricRule, part: str) -> str:
    """The name of a monitor of an entry, such as ``metric.revenue.change``."""
    return f"{KIND}.{rule.key}.{part}"


# ---------------------------------------------------------------------- the period


def latest_period(now: dt.datetime, grain: str) -> tuple[dt.date, dt.date]:
    """The first and the last day of the latest period of ``grain`` that is over at ``now``, counted in UTC.

    A week starts on Monday, as ``date_trunc`` of DuckDB has it, and a quarter starts in January, April, July or
    October.
    """
    today = now.astimezone(_UTC).date()
    if grain == "day":
        last = today - dt.timedelta(days=1)
        return last, last
    if grain == "week":
        last = today - dt.timedelta(days=today.weekday() + 1)
        return last - dt.timedelta(days=6), last
    if grain == "month":
        last = today.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=1), last
    if grain == "quarter":
        last = today.replace(month=3 * ((today.month - 1) // 3) + 1, day=1) - dt.timedelta(days=1)
        return last.replace(month=3 * ((last.month - 1) // 3) + 1, day=1), last
    last = dt.date(today.year - 1, 12, 31)
    return dt.date(last.year, 1, 1), last


def _label(grain: str, first: dt.date) -> str:
    """A period as a person reads it, such as ``2026-10-28`` for a day and ``2026-Q4`` for a quarter."""
    if grain == "month":
        return first.strftime("%Y-%m")
    if grain == "quarter":
        return f"{first.year}-Q{(first.month - 1) // 3 + 1}"
    if grain == "year":
        return str(first.year)
    return first.isoformat()


def _subject(rule: MetricRule, first: dt.date) -> str:
    return f"{rule.key} for {rule.grain} {_label(rule.grain, first)}"


# ---------------------------------------------------------------------- reading the number


@dataclass(frozen=True)
class Reading:
    """What reading the number of one entry gave: the value, or the reason that there is none.

    Attributes:
        value: The number, or ``None`` when there is none.
        status: ``ok`` with a value, and without one ``unknown`` when the monitor cannot tell, or ``error`` when the
            entry cannot be measured.
        details: Why there is no value, in one sentence.
        where: Where the number came from, for the metrics of the result.
    """

    value: int | float | None = None
    status: str = "ok"
    details: str = ""
    where: Mapping[str, Any] = field(default_factory=dict)


def _sentence(error: BaseException) -> str:
    return " ".join(str(error).split())


def _is_duckdb_error(error: BaseException | None) -> bool:
    return error is not None and type(error).__module__.split(".")[0].lstrip("_") == "duckdb"


def _why(error: BaseException) -> str:
    """Why something failed, in one line, without a value that DuckDB copied out of a row.

    The error of DuckDB about the text of a query, such as a table or a column that is not there or a slip of syntax, is
    said as it is. Any other error of DuckDB, such as a value that cannot be cast, quotes the row that it came from, so
    only its kind is said.
    """
    cause = error if _is_duckdb_error(error) else error.__cause__
    if _is_duckdb_error(cause):
        kind = type(cause).__name__
        if kind in _NAMES_ONLY:
            return _sentence((str(cause).strip().splitlines() or [kind])[0])
        return f"DuckDB said {kind}, and the query has to be run by hand to see why"
    return _sentence(error)


def _reading_of(cell: Any, where: Mapping[str, Any]) -> Reading:
    """The reading of one value of a result, which has to be a number."""
    if cell is None:
        return Reading(status="unknown", details="the value is NULL, so there is no number to check", where=where)
    if isinstance(cell, Decimal):
        cell = float(cell)
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        return Reading(status="error", details=f"the value is of the type {type(cell).__name__}, and it has to be a "
                                               "number", where=where)
    if not math.isfinite(cell):
        return Reading(status="unknown", details="the value is not a finite number, so there is nothing to check",
                       where=where)
    return Reading(value=cell, where=where)


def _read_metric(rule: MetricRule, folder: Path, period: tuple[dt.date, dt.date], definitions: Any,
                 problems: Sequence[str]) -> Reading:
    if definitions is None:
        return Reading(status="error", details=f"the metrics of {folder} cannot be used, {_sentence(problems[0])}")
    from local_data_platform.metrics import run

    first, last = period
    result = run(folder, rule.metric, grain=rule.grain, filters=rule.filter, start=first, end=last,
                 definitions=definitions)
    where = {"table": result.query.table, "read": result.source}
    rows = result.table.to_pylist()
    if not rows:
        return Reading(status="unknown", where=where,
                       details=f"{result.query.table} has no rows in that {rule.grain}, so there is no value")
    return _reading_of(rows[0][rule.metric], where)


def _read_query(rule: MetricRule, config: Any, target: Target, folder: Path) -> Reading:
    from local_data_platform.engine.duckdb import DuckDBEngine
    from local_data_platform.mcp_server.errors import QueryRejected
    from local_data_platform.mcp_server.guard import check_sql
    from local_data_platform.metrics.load import metrics_files
    from local_data_platform.metrics.sql import import_duckdb

    if metrics_files(folder):
        return Reading(status="error", details=QUERY_NEEDS_NO_METRICS_FILE)
    try:
        check_sql(rule.sql)
    except QueryRejected as error:
        return Reading(status="error", details=f"the query was rejected, {_sentence(error)}")
    if target.table is None:
        return Reading(status="unknown", details=f"there is no table to run the query on, {target.why}")
    duckdb = import_duckdb()
    where = {"table": target.identifier}
    with DuckDBEngine() as engine:
        engine.register_iceberg(target.table, config.metadata["target"]["name"])
        try:
            data = engine.query(rule.sql)
        except duckdb.Error as error:
            return Reading(status="error", details=f"the query could not run, {_why(error)}", where=where)
    if data.num_rows != 1 or data.num_columns != 1:
        return Reading(status="error", details=f"the query has to return one number, and this one returned "
                                               f"{data.num_rows} row{'' if data.num_rows == 1 else 's'} and "
                                               f"{data.num_columns} column{'' if data.num_columns == 1 else 's'}",
                       where=where)
    return _reading_of(data.column(0)[0].as_py(), where)


def _read(rule: MetricRule, config: Any, target: Target, folder: Path, period: tuple[dt.date, dt.date],
          definitions: Any, problems: Sequence[str]) -> Reading:
    try:
        if rule.sql is not None:
            return _read_query(rule, config, target, folder)
        return _read_metric(rule, folder, period, definitions, problems)
    except EngineNotFound:
        from local_data_platform.metrics.sql import INSTALL_HINT

        return Reading(status="unknown", details=f"a metric monitor needs the duckdb package, install it with "
                                                 f"{INSTALL_HINT}")
    except TableNotFound as error:
        return Reading(status="unknown", details=_sentence(error))
    except LDPError as error:
        return Reading(status="error", details=_why(error))


# ---------------------------------------------------------------------- the earlier values


def _earlier(history: Sequence[MonitorResult], rule: MetricRule, first: dt.date, now: dt.datetime) -> list[float]:
    """The values of the periods before ``first`` in the history of an entry, oldest first.

    There is one for each period, the last one that was measured. Only a look that was made by ``now`` and that has the
    definition of this entry counts, whatever the entry was called, so a history of another metric, grain or filter is
    not the baseline of this one. The latest period is among the ``lookback`` periods, so ``lookback - 1`` earlier ones
    are left.
    """
    definition = rule.definition()
    by_period: dict[str, float] = {}
    for result in history:
        found = result.metrics
        value, period = found.get("value"), found.get("period")
        if (result.evaluated_at <= now and found.get("definition") == definition
                and isinstance(value, (int, float)) and not isinstance(value, bool) and isinstance(period, str)
                and period < first.isoformat()):
            by_period[period] = value
    wanted = rule.lookback - 1
    return [by_period[period] for period in sorted(by_period)[-wanted:]] if wanted else []


# ---------------------------------------------------------------------- the results


def _result(monitor: str, status: str, observed: str | None, expected: str, details: str, metrics: Mapping[str, Any],
            common: Mapping[str, Any]) -> MonitorResult:
    return MonitorResult(monitor=monitor, status=status, observed=observed, expected=expected, details=details,
                         metrics={key: value for key, value in metrics.items() if value is not None}, **common)


def _value_result(rule: MetricRule, reading: Reading, first: dt.date, found: Mapping[str, Any],
                  common: Mapping[str, Any]) -> MonitorResult:
    name, expected = monitor_name(rule, VALUE), rule.value_summary()
    metrics = {**found, "min": rule.min, "max": rule.max}
    if reading.value is None:
        return _result(name, reading.status, None, expected, reading.details, metrics, common)
    broken = None
    if rule.min is not None and reading.value < rule.min:
        broken = f"below the {figure(rule.min)} that min asks for"
    elif rule.max is not None and reading.value > rule.max:
        broken = f"above the {figure(rule.max)} that max allows"
    shown = figure(reading.value)
    details = f"{_subject(rule, first)} was {shown}, {broken or 'inside the limits'}"
    return _result(name, "error" if broken else "ok", shown, expected, details, metrics, common)


def _change_result(rule: MetricRule, reading: Reading, first: dt.date, found: Mapping[str, Any],
                   history: Sequence[MonitorResult], now: dt.datetime, common: Mapping[str, Any]) -> MonitorResult:
    name, expected = monitor_name(rule, CHANGE), rule.change_summary()
    metrics = {**found, "max_change_pct": rule.max_change_pct, "lookback": rule.lookback}
    if reading.value is None:
        return _result(name, reading.status, None, expected, reading.details, metrics, common)
    earlier = _earlier(history, rule, first, now)
    metrics["earlier_values"] = len(earlier)
    if len(earlier) < MIN_EARLIER_VALUES:
        return _result(name, "unknown", None, expected, "not enough history",
                       {**metrics, "needed_values": MIN_EARLIER_VALUES}, common)
    median = statistics.median(earlier)
    value = reading.value
    if median == 0 and value != 0:
        return _result(name, "unknown", None, expected,
                       "the median of the earlier values is 0, so a change in percent has no meaning",
                       {**metrics, "baseline_median": median}, common)
    change = 0.0 if median == 0 else (value - median) / abs(median) * 100
    was = f"{_subject(rule, first)} was {figure(value)}"
    compared = f"the median of the {len(earlier)} earlier {rule.grain}s, {figure(median)}"
    if change == 0:
        details = f"{was}, the same as {compared}"
    else:
        details = f"{was}, {abs(change):.1f} percent {'higher' if change > 0 else 'lower'} than {compared}"
    status = "error" if abs(change) > rule.max_change_pct else "ok"
    return _result(name, status, f"{change:+.1f} percent", expected, details,
                   {**metrics, "baseline_median": median, "change_pct": round(change, 3)}, common)


def _definitions(folder: Path, rules: Sequence[MetricRule]) -> tuple[Any, list[str]]:
    """The definitions of the metrics file of the folder when some entry names a metric, else nothing."""
    if all(rule.metric is None for rule in rules):
        return None, []
    from local_data_platform.metrics.load import read_definitions

    try:
        return read_definitions(folder)
    except ConfigError as error:
        return None, [str(error)]


def evaluate_metrics(rules: Sequence[MetricRule], config: Any, target: Target, now: dt.datetime) -> list[MonitorResult]:
    """Check the business numbers of the ``metrics`` section of an SLA.

    Args:
        rules: The entries of the section.
        config: The :class:`~local_data_platform.Config`, whose folder holds the metrics file.
        target: The table of the config, from :func:`~local_data_platform.monitoring.target.load_target`, which a query
            runs on.
        now: The moment of the evaluation, with a time zone.

    Returns:
        ``metric.<key>.value`` for an entry that has ``min`` or ``max``, and ``metric.<key>.change`` for one that has
        ``max_change_pct``, in the order of the entries. ``error`` when a limit is broken or an entry cannot be
        measured, and ``unknown`` when there is no value or too little history, or the duckdb package is not installed.
    """
    from local_data_platform.monitoring.history import read_results

    folder = Path(config.base_dir) if config.base_dir is not None else Path.cwd()
    history = read_results(config) if any(rule.max_change_pct is not None for rule in rules) else []
    definitions, problems = _definitions(folder, rules)
    common = {"kind": KIND, "evaluated_at": now, "pipeline": target.pipeline, "table_identifier": target.identifier,
              "table_uuid": target.table_uuid}
    results: list[MonitorResult] = []
    for rule in rules:
        period = latest_period(now, rule.grain)
        reading = _read(rule, config, target, folder, period, definitions, problems)
        found: dict[str, Any] = {"grain": rule.grain, "period": period[0].isoformat(), "definition": rule.definition(),
                                 **reading.where}
        if reading.value is not None:
            found["value"] = reading.value
        if rule.min is not None or rule.max is not None:
            results.append(_value_result(rule, reading, period[0], found, common))
        if rule.max_change_pct is not None:
            results.append(_change_result(rule, reading, period[0], found, history, now, common))
    return results


# ---------------------------------------------------------------------- the folder


def folder_problems(rules: Sequence[MetricRule], folder: Path) -> list[tuple[str, str]]:
    """What is wrong with the entries of ``metrics`` given the folder of their config, found without reading a table.

    A metric has to be one that the metrics file of the folder defines, at a grain and with filters that its semantic
    model allows, and a query is for a folder that has no metrics file.

    Args:
        rules: The entries of the section.
        folder: The folder of the config.

    Returns:
        ``(path, message)`` for each problem, with the path relative to the SLA block as in
        :func:`~local_data_platform.monitoring.sla.sla_problems`. The message of an unknown metric lists the valid
        names.
    """
    from local_data_platform.metrics.load import metrics_files, read_definitions

    if not Path(folder).is_dir():
        return [(".metrics", f"cannot be checked, {folder} is not a folder of configs")]
    problems: list[tuple[str, str]] = []
    queries = [index for index, rule in enumerate(rules) if rule.sql is not None]
    if queries and metrics_files(folder):
        problems += [(f".metrics[{index}].sql", QUERY_NEEDS_NO_METRICS_FILE) for index in queries]
    named = [(index, rule) for index, rule in enumerate(rules) if rule.metric is not None]
    if not named:
        return problems
    definitions, found = read_definitions(folder)
    if definitions is None:
        return problems + [(f".metrics[{index}].metric", f"cannot be checked, {_sentence(found[0])}")
                           for index, _ in named]
    for index, rule in named:
        where = f".metrics[{index}]"
        try:
            metric = definitions.metric(rule.metric)
        except ConfigError as error:
            problems.append((f"{where}.metric", str(error)))
            continue
        model = definitions.model_of(metric)
        if model.time_dimension is None:
            problems.append((f"{where}.grain", f"{metric.name} has no period to measure, because {model.name} has no "
                                               "time dimension"))
        elif rule.grain not in model.grains:
            problems.append((f"{where}.grain", f"{metric.name} cannot be reported by {rule.grain}, the time dimension "
                                               f"of {model.name} allows {list(model.grains)}"))
        known = [item.name for item in model.dimensions]
        for dimension in rule.filter or {}:
            if dimension not in known:
                problems.append((f"{where}.filter.{dimension}",
                                 f"is not a dimension of {model.name}, the dimensions are {known}"))
    return problems


__all__ = [
    "CHANGE",
    "KIND",
    "MIN_EARLIER_VALUES",
    "QUERY_NEEDS_NO_METRICS_FILE",
    "Reading",
    "VALUE",
    "evaluate_metrics",
    "folder_problems",
    "latest_period",
    "monitor_name",
]
