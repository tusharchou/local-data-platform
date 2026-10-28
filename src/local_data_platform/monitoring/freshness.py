"""The freshness monitor: how old a table is, from the metadata of Iceberg and without reading a row.

The age of a table is the time since its last commit, which is the current snapshot on ``main``. When the SLA names a
``column``, it is the time since the newest value of that column, which the data files of the current snapshot carry as
upper bounds, so the manifests are read and no data file is. Only a timestamp or a date column can say how fresh a
table is. A date counts as the end of its day in UTC, so the newest date of a table that was loaded this morning for
yesterday is hours old and not a day and more. A timestamp without a time zone is in UTC.

The table is late when its age is more than ``warn_after``, and too late when it is more than ``error_after``. A table
that was never written has no age, which is an error, because nothing has arrived.

This module needs only the standard library when it is imported.
"""

import datetime as dt
from typing import Any

from local_data_platform.monitoring.results import MonitorResult, format_moment
from local_data_platform.monitoring.sla import PERIOD_SECONDS, Freshness
from local_data_platform.monitoring.target import Target

MONITOR = "freshness"
"""The name and the kind of the freshness monitor."""

TIME_TYPES = ("TimestampType", "TimestamptzType", "TimestampNanoType", "TimestamptzNanoType", "DateType")
"""The classes of the Iceberg types of the columns that can say how fresh a table is."""

_UTC = dt.timezone.utc
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=_UTC)


def _instant(value: Any) -> dt.datetime | None:
    """The moment that a bound stands for in UTC, or ``None`` when it is not a time. A date is the end of that day."""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=_UTC) if value.tzinfo is None else value.astimezone(_UTC)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value + dt.timedelta(days=1), dt.time(), tzinfo=_UTC)
    return None


def _column_problem(table: Any, column: str) -> str | None:
    """Why ``column`` cannot say how fresh the table is, or ``None`` when it can."""
    try:
        field = table.schema().find_field(column)
    except ValueError:
        return f"the table has no column {column}"
    if type(field.field_type).__name__ not in TIME_TYPES:
        return f"the column {column} is {field.field_type}, and only timestamp and date columns are accepted"
    return None


def _newest(table: Any, column: str) -> tuple[dt.datetime | None, Any, int]:
    """The newest value of ``column`` in the data files of the current snapshot, with no scan of the data.

    Returns:
        ``(moment, bound, files)``, the newest upper bound as a moment, as the file holds it, and how many data files
        there are. The moment is ``None`` when no file has an upper bound for the column that is a time.
    """
    if not table.current_snapshot().manifests(table.io):  # no manifest, no data files, and inspect fails on that
        return None, None, 0
    files = table.inspect.data_files()
    best: tuple[dt.datetime, Any] | None = None
    for entry in files.column("readable_metrics").to_pylist():
        bound = (entry.get(column) or {}).get("upper_bound")
        moment = _instant(bound)
        if moment is not None and (best is None or moment > best[0]):
            best = (moment, bound)
    return (best[0], best[1], files.num_rows) if best is not None else (None, None, files.num_rows)


def evaluate_freshness(rule: Freshness, target: Target, now: dt.datetime) -> MonitorResult:
    """Check the age of the target table against the ``freshness`` section of an SLA.

    Args:
        rule: The ``freshness`` section.
        target: The table, from :func:`~local_data_platform.monitoring.target.load_target`.
        now: The moment to measure the age at, with a time zone.

    Returns:
        One result. ``unknown`` when the target is a file, or when the data files carry no bound for the column.
        ``error`` when the table was never written, has no such column, has a column that is not a time, has no data
        files, or is older than ``error_after``. ``warn`` when it is older than ``warn_after``, and ``ok`` otherwise.
    """
    def result(status: str, observed: str | None = None, details: str = "", **metrics: Any) -> MonitorResult:
        return MonitorResult(monitor=MONITOR, kind=MONITOR, status=status, evaluated_at=now, pipeline=target.pipeline,
                             table_identifier=target.identifier, table_uuid=target.table_uuid, observed=observed,
                             expected=rule.summary(), details=details,
                             metrics={key: value for key, value in metrics.items() if value is not None})

    if not target.iceberg:
        return result("unknown", details=target.why)
    snapshot = target.table.current_snapshot() if target.table is not None else None
    if snapshot is None:
        return result("error", observed="never written", details="table has never been written",
                      reason=target.why or "the table has no snapshot")
    table, column = target.table, rule.column
    if column is None:
        reference, bound = _EPOCH + dt.timedelta(milliseconds=snapshot.timestamp_ms), None
    else:
        problem = _column_problem(table, column)
        if problem is not None:
            return result("error", details=problem, column=column)
        reference, bound, files = _newest(table, column)
        if reference is None and files == 0:
            return result("error", details=f"the table has no data files, so there is no newest {column}",
                          column=column)
        if reference is None:
            return result("unknown", details=f"the data files carry no upper bound for {column}, so its newest value "
                                             "cannot be read without scanning the table", column=column)
    age = max((now - reference).total_seconds(), 0.0)
    thresholds = {"warn": rule.warn_after, "error": rule.error_after}
    unit = (rule.error_after or rule.warn_after).period
    observed = f"{age / PERIOD_SECONDS[unit]:.1f} {unit}"
    status = "ok"
    for name in ("warn", "error"):  # the later one wins
        if thresholds[name] is not None and age > thresholds[name].seconds:
            status = name
    if column is None:
        details = f"the last commit was {observed} ago, at {format_moment(reference, seconds=True)}"
    elif isinstance(bound, dt.datetime):
        details = f"the newest {column} is {format_moment(reference, seconds=True)}, {observed} ago"
    else:
        details = (f"the newest {column} is {bound.isoformat()}, which ends at "
                   f"{format_moment(reference, seconds=True)}, {observed} ago")
    return result(status, observed=observed, details=details, age_seconds=round(age, 3),
                  reference_time=format_moment(reference), source="snapshot" if column is None else "column",
                  column=column, snapshot_id=snapshot.snapshot_id,
                  warn_after_seconds=rule.warn_after.seconds if rule.warn_after else None,
                  error_after_seconds=rule.error_after.seconds if rule.error_after else None)


__all__ = [
    "MONITOR",
    "TIME_TYPES",
    "evaluate_freshness",
]
