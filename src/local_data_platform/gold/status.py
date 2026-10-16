"""What ``_ldp`` says about a table: its latest run, its recent runs and its latest quality verdict.

Every run of a pipeline records its events in the ``_ldp`` namespace of the catalog of its table, through the
``iceberg`` sink. ``_ldp.runs`` holds the events and ``_ldp.quality_results`` one row for each check. ``ldp mcp`` shows
them in ``describe_table``, and ``ldp gold list``, ``show`` and ``docs`` show them in the catalog. Both ask
:func:`ldp_status`, so the command line and the MCP server report the same run and the same quality verdict for a table.

A release is cut after a build, and it records a ``release.cut`` event for each table as a run of its own. That event
says that a table is in a release, and it is not a run of the table's pipeline, so it is left out of the runs here.
Without that, the last event of every released table would be its release, and its latest run would read ``running``.

The values are what the rows hold, such as timestamps that are ``datetime`` objects. A caller that needs JSON converts
them, as ``describe_table`` does for the whole of its answer.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from local_data_platform.events import QUALITY_TABLE, RUNS_TABLE, SYSTEM_NAMESPACE
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

TERMINAL_RUN_EVENTS = ("run.published", "run.skipped_duplicate", "run.blocked_quality", "run.failed")
"""The events that end a run of a pipeline."""

NOT_A_RUN = ("release.cut",)
"""Events in ``_ldp.runs`` that are about a table without being a run of its pipeline."""

_TS_COLUMNS = ("ts", "event_ts", "timestamp", "emitted_at", "evaluated_at", "created_at")
_TABLE_KEY_COLUMNS = ("table_uuid", "table_identifier")


# ---------------------------------------------------------------------- small readers


def int_or_none(value: Any) -> int | None:
    """``value`` as a whole number, or ``None`` when it is not one."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def snapshot_summary(snapshot: Any) -> dict[str, str]:
    """The summary properties of an Iceberg snapshot, such as ``total-records``, as a plain dict."""
    summary = getattr(snapshot, "summary", None)
    return dict(summary.additional_properties) if summary is not None else {}


def snapshot_operation(snapshot: Any) -> str | None:
    """What a snapshot did, ``append``, ``overwrite``, ``replace`` or ``delete``."""
    summary = getattr(snapshot, "summary", None)
    operation = getattr(summary, "operation", None)
    return getattr(operation, "value", operation)


# ---------------------------------------------------------------------- the rows


def _load_ldp_table(catalog: Any, name: str) -> Any:
    from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

    try:
        return catalog.load_table((SYSTEM_NAMESPACE, name))
    except (NoSuchTableError, NoSuchNamespaceError):
        return None


def _parse_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return row
    if isinstance(payload, Mapping):
        row = dict(row)
        row["payload"] = dict(payload)
    return row


def _field(row: Mapping[str, Any] | None, key: str) -> Any:
    """``row[key]``, or the same key inside the row's ``payload`` object."""
    if row is None:
        return None
    if row.get(key) is not None:
        return row[key]
    payload = row.get("payload")
    return payload.get(key) if isinstance(payload, Mapping) else None


def ldp_rows(catalog: Any, name: str, table_uuid: str, identifier: str) -> list[dict[str, Any]] | None:
    """The ``_ldp.<name>`` rows about one table, oldest first; ``None`` if the table is absent.

    A row is about the table when its ``table_uuid`` matches, or its ``table_identifier``
    does (the first run of a new table emits ``run.started`` before the table has a UUID).
    """
    ldp_table = _load_ldp_table(catalog, name)
    if ldp_table is None:
        return None
    columns = [item.name for item in ldp_table.schema().fields]
    keys = {column: (table_uuid if column == "table_uuid" else identifier)
            for column in _TABLE_KEY_COLUMNS if column in columns}
    if not keys:
        logger.debug("_ldp.%s has none of the columns %s; cannot attribute rows", name, _TABLE_KEY_COLUMNS)
        return []
    try:
        from pyiceberg.expressions import EqualTo, Or

        expressions = [EqualTo(column, value) for column, value in keys.items()]
        row_filter = expressions[0] if len(expressions) == 1 else Or(*expressions)
        rows = ldp_table.scan(row_filter=row_filter).to_arrow().to_pylist()
    except Exception:  # noqa: BLE001 - e.g. a non-string column type; filter in Python instead
        rows = [row for row in ldp_table.scan().to_arrow().to_pylist()
                if any(str(row.get(column)) == value for column, value in keys.items())]
    ts_column = next((column for column in _TS_COLUMNS if column in columns), None)
    for row in rows:
        row["_ts"] = row.get(ts_column) if ts_column else None
    rows.sort(key=lambda row: (str(row["_ts"] or ""), str(row.get("run_id") or ""),
                               int_or_none(row.get("attempt")) or 0, int_or_none(row.get("seq")) or 0,
                               int_or_none(row.get("check_index")) or 0))
    return [_parse_payload(row) for row in rows]


# ---------------------------------------------------------------------- runs


def _runs(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The rows of each run of the table's pipeline, the run with the newest event first.

    A run is one attempt of one ``run_id``. The events of a release are not a run of the pipeline and are left out.
    """
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    newest: dict[tuple[Any, Any], int] = {}
    for position, row in enumerate(rows):
        if row.get("type") in NOT_A_RUN:
            continue
        key = (row.get("run_id"), row.get("attempt"))
        groups.setdefault(key, []).append(row)
        newest[key] = position
    return [groups[key] for key in sorted(groups, key=newest.__getitem__, reverse=True)]


def _summarize(run: list[dict[str, Any]]) -> dict[str, Any]:
    """The status, times and event types of one run, from its rows."""
    last = run[-1]
    types = [row.get("type") for row in run]
    finished = next((row for row in reversed(run) if row.get("type") == "run.finished"), None)
    terminal = next((kind for kind in reversed(types) if kind in TERMINAL_RUN_EVENTS), None)
    status = _field(finished, "status") if finished is not None else None
    if status is None:
        status = terminal[len("run."):] if terminal else ("finished" if finished is not None else "running")
    return {
        "run_id": last.get("run_id"),
        "attempt": last.get("attempt"),
        "pipeline": _field(last, "pipeline"),
        "status": status,
        "started_at": run[0].get("_ts"),
        "finished_at": finished.get("_ts") if finished is not None else None,
        "snapshot_id": next((_field(row, "snapshot_id") for row in reversed(run)
                             if _field(row, "snapshot_id") is not None), None),
        "idempotency_key": next((_field(row, "idempotency_key") for row in reversed(run)
                                 if _field(row, "idempotency_key") is not None), None),
        "events": types,
    }


def latest_run(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Summarize the most recent run in ``_ldp.runs`` rows: status, times and event types."""
    runs = _runs(rows)
    return _summarize(runs[0]) if runs else None


def recent_runs(rows: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """The summaries of the last ``limit`` runs, the newest first. A retried task is one run for each attempt."""
    return [_summarize(run) for run in _runs(rows)[:limit]]


# ---------------------------------------------------------------------- quality


def latest_quality(quality_rows: list[dict[str, Any]] | None, run_rows: list[dict[str, Any]] | None,
                   iceberg_table: Any) -> dict[str, Any] | None:
    """The latest quality verdict for a table.

    The newest ``quality.evaluated`` event in ``_ldp.runs`` decides which evaluation is
    latest; its per-check rows come from ``_ldp.quality_results`` when they exist. Without
    ``_ldp`` rows, the current snapshot's ``ldp.quality`` property is used.
    """
    evaluated = [row for row in (run_rows or []) if row.get("type") == "quality.evaluated"]
    quality_rows = quality_rows or []
    if evaluated:
        event = evaluated[-1]
        checks = [row for row in quality_rows if row.get("event_id") == event.get("event_id")]
    elif quality_rows:
        event = quality_rows[-1]
        checks = [row for row in quality_rows if row.get("event_id") == event.get("event_id")]
    else:
        snapshot = iceberg_table.current_snapshot()
        if snapshot is not None and "ldp.quality" in snapshot_summary(snapshot):
            return {"source": "snapshot", "snapshot_id": snapshot.snapshot_id,
                    "status": snapshot_summary(snapshot)["ldp.quality"]}
        return None
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    details = [{"check": _field(row, "check_name"), "column": _field(row, "column_name"),
                "passed": _field(row, "passed"), "failing_rows": _field(row, "failing_rows"),
                "details": _field(row, "details")} for row in checks]
    verdicts = [item["passed"] for item in details if item["passed"] is not None]
    passed = payload.get("passed")
    if passed is None and verdicts:
        passed = all(bool(verdict) for verdict in verdicts)
    return {
        "source": f"{SYSTEM_NAMESPACE}.{QUALITY_TABLE}" if checks else f"{SYSTEM_NAMESPACE}.{RUNS_TABLE}",
        "run_id": event.get("run_id"),
        "evaluated_at": event.get("_ts"),
        "passed": passed,
        "on_failure": payload.get("on_failure", _field(event, "on_failure")),
        "checks_run": payload.get("checks_run", len(details)),
        "checks_failed": payload.get("checks_failed", sum(1 for verdict in verdicts if not verdict)),
        "checks": details,
    }


# ---------------------------------------------------------------------- one table


def ldp_status(catalog: Any, iceberg_table: Any, identifier: str, *, recent: int = 0) -> dict[str, Any]:
    """The run and quality status of one table, as ``describe_table`` and the gold catalog both report it.

    Args:
        catalog: The pyiceberg catalog that holds the table, and its ``_ldp`` namespace.
        iceberg_table: The pyiceberg table.
        identifier: ``<namespace>.<name>``, which the first events of a table carry before it has a UUID.
        recent: How many of the latest runs to list as ``recent_runs``. None by default.

    Returns:
        ``runs_table`` and ``quality_table`` (whether ``_ldp`` has them), ``latest_run``, ``quality`` and, when asked,
        ``recent_runs``. When ``_ldp`` cannot be read the answer has an ``error`` instead, no run, and the quality
        that the current snapshot carries. It does not raise.
    """
    table_uuid = str(iceberg_table.metadata.table_uuid)
    try:
        runs = ldp_rows(catalog, RUNS_TABLE, table_uuid, identifier)
        quality = ldp_rows(catalog, QUALITY_TABLE, table_uuid, identifier)
        status: dict[str, Any] = {"runs_table": runs is not None, "quality_table": quality is not None,
                                  "latest_run": latest_run(runs or []),
                                  "quality": latest_quality(quality, runs, iceberg_table)}
        if recent:
            status["recent_runs"] = recent_runs(runs or [], recent)
        return status
    except Exception as error:  # noqa: BLE001 - describe the table even if _ldp is unreadable
        logger.warning("Could not read _ldp status for %s: %s", identifier, error)
        status = {"error": str(error), "latest_run": None, "quality": latest_quality(None, None, iceberg_table)}
        if recent:
            status["recent_runs"] = []
        return status


__all__ = [
    "NOT_A_RUN",
    "TERMINAL_RUN_EVENTS",
    "int_or_none",
    "latest_quality",
    "latest_run",
    "ldp_rows",
    "ldp_status",
    "recent_runs",
    "snapshot_operation",
    "snapshot_summary",
]
