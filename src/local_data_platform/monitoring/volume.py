"""The volume monitor: how many rows the last run wrote, and how that compares with the runs before it.

The count of a run is the ``rows_written`` of its ``run.published`` event, which the history of the pipeline keeps in
``_ldp.runs`` or in the file of its ``jsonl`` sink. The runs that count are the ``published`` ones, the attempt that
published, so a run that was blocked, that failed, or that found its idempotency key already published has no count, and
a run that published on its first attempt and found its key on the second one counts once. What
the count means follows the write mode. An append counts the rows that it added, an overwrite counts the rows of the
new batch, and an upsert counts the rows that it inserted or changed, which is the batch and not the table, so a batch
that is the same as the stored rows counts 0.

A config with no history has the snapshots of its table to read from. The monitor counts the ``added-records`` of the
commits that append rows. An overwrite is a delete and then an append, so the append holds the new batch. A commit that
rewrites files, such as a delete of some rows, adds no new rows, and is left out. An upsert is several commits, an
overwrite that rewrites the files of the rows that it changes and an append for the changed rows and another for the
new ones, so the snapshots are no count of a run for an upsert. Expiring snapshots removes the commits too, so the
snapshots are a fallback and not a record.

The monitor makes two results, so that each rule has its own history and its own status. ``volume.rows`` holds the
latest run against ``min_rows`` and ``max_rows``. ``volume.change`` holds the latest run against the median of the runs
before it, among the last ``lookback_runs`` runs, and it needs 3 earlier runs, so that one odd run is not the baseline.
A breach of either is an ``error``. With too little to compare, the result is ``unknown``, which never alerts.

This module needs only the standard library when it is imported.
"""

import datetime as dt
import statistics
from dataclasses import dataclass
from typing import Any

from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.monitoring.sla import Volume
from local_data_platform.monitoring.target import Target

KIND = "volume"
"""The kind of the volume monitors."""

ROWS = "volume.rows"
"""The name of the monitor of ``min_rows`` and ``max_rows``."""

CHANGE = "volume.change"
"""The name of the monitor of ``max_change_pct``."""

MIN_EARLIER_RUNS = 3
"""How many runs before the latest one the change rule needs."""

COUNTED_OPERATIONS = ("append",)
"""The operations of a snapshot that add the rows of a batch. A delete, a compaction and the rewrite of the files of an
upsert have an ``added-records`` too, and the rows are not new."""


@dataclass(frozen=True)
class Counts:
    """The row counts of the latest runs of a table, newest first.

    Attributes:
        values: The counts. The first one is the latest run.
        source: ``events`` when they are the ``rows_written`` of runs, ``snapshots`` when they are the ``added-records``
            of commits, and ``none`` when there is neither.
        run_id: The id of the latest run, when the counts are from events.
        snapshot_id: The snapshot of the latest run or commit.
    """

    values: tuple[int, ...] = ()
    source: str = "none"
    run_id: str | None = None
    snapshot_id: int | None = None


def _from_events(config: Any, limit: int) -> Counts | None:
    """The counts of the last published runs in the history of the pipeline, or ``None`` when it has none."""
    from local_data_platform.events import run_events, summarize_runs

    events, _ = run_events(config)
    runs = [run for run in summarize_runs(events)
            if run["status"] == "published" and isinstance(run["rows_written"], int)]
    if not runs:
        return None
    return Counts(tuple(run["rows_written"] for run in runs[:limit]), "events", runs[0]["run_id"],
                  runs[0]["snapshot_id"])


def _from_snapshots(table: Any, limit: int) -> Counts | None:
    """The ``added-records`` of the last commits that added rows to ``main``, or ``None`` when there are none."""
    values: list[int] = []
    latest = None
    snapshot = table.current_snapshot()
    while snapshot is not None and len(values) < limit:
        summary = snapshot.summary
        operation = getattr(getattr(summary, "operation", None), "value", None)
        added = (summary.additional_properties if summary is not None else {}).get("added-records")
        if operation in COUNTED_OPERATIONS and added is not None:
            values.append(int(added))
            latest = snapshot.snapshot_id if latest is None else latest
        parent = snapshot.parent_snapshot_id
        snapshot = table.metadata.snapshot_by_id(parent) if parent is not None else None
    return Counts(tuple(values), "snapshots", None, latest) if values else None


def counts_of(config: Any, target: Target, limit: int) -> Counts:
    """The row counts of the last ``limit`` runs, from the history of the pipeline, else from the snapshots."""
    counts = _from_events(config, limit)
    if counts is None and target.table is not None:
        counts = _from_snapshots(target.table, limit)
    return counts or Counts()


def _rows(number: int | float) -> str:
    return f"{number:g} row" if number == 1 else f"{number:g} rows"


def _what(counts: Counts) -> str:
    """What the latest count is of, in the words of its source."""
    return "the latest run wrote" if counts.source == "events" else "the latest commit added"


def evaluate_volume(rule: Volume, config: Any, target: Target, now: dt.datetime) -> list[MonitorResult]:
    """Check the rows of the latest run of a pipeline against the ``volume`` section of an SLA.

    Args:
        rule: The ``volume`` section.
        config: The :class:`~local_data_platform.Config`, whose history holds the runs.
        target: The table, from :func:`~local_data_platform.monitoring.target.load_target`.
        now: The moment of the evaluation, with a time zone.

    Returns:
        ``volume.rows`` when the section has ``min_rows`` or ``max_rows``, and ``volume.change`` when it has
        ``max_change_pct``. ``unknown`` when there is no run to measure, or for the change when fewer than 3 earlier
        runs are there. ``error`` when a limit is broken, and ``ok`` otherwise.
    """
    counts = counts_of(config, target, rule.lookback_runs)
    common = {"kind": KIND, "evaluated_at": now, "pipeline": target.pipeline, "table_identifier": target.identifier,
              "table_uuid": target.table_uuid}
    where = {"source": counts.source, "run_id": counts.run_id, "snapshot_id": counts.snapshot_id}
    latest = counts.values[0] if counts.values else None
    results = []
    if rule.min_rows is not None or rule.max_rows is not None:
        results.append(_rows_result(rule, counts, latest, where, common))
    if rule.max_change_pct is not None:
        results.append(_change_result(rule, counts, latest, where, common))
    return results


def _rows_result(rule: Volume, counts: Counts, latest: int | None, where: dict[str, Any],
                 common: dict[str, Any]) -> MonitorResult:
    limits = {"min_rows": rule.min_rows, "max_rows": rule.max_rows}

    def result(status: str, shown: str | None, details: str, **metrics: Any) -> MonitorResult:
        found = {**limits, **where, **metrics}
        return MonitorResult(monitor=ROWS, status=status, observed=shown, expected=rule.rows_summary(),
                             details=details, metrics={key: value for key, value in found.items() if value is not None},
                             **common)

    if latest is None:
        return result("unknown", None, "no published run yet, so there is no row count to check")
    breaks = []
    if rule.min_rows is not None and latest < rule.min_rows:
        breaks.append(f"fewer than the {_rows(rule.min_rows)} that min_rows asks for")
    if rule.max_rows is not None and latest > rule.max_rows:
        breaks.append(f"more than the {_rows(rule.max_rows)} that max_rows allows")
    details = f"{_what(counts)} {_rows(latest)}" + (f", {breaks[0]}" if breaks else ", inside the limits")
    return result("error" if breaks else "ok", _rows(latest), details, observed=latest)


def _change_result(rule: Volume, counts: Counts, latest: int | None, where: dict[str, Any],
                   common: dict[str, Any]) -> MonitorResult:
    earlier = counts.values[1:]

    def result(status: str, shown: str | None, details: str, **metrics: Any) -> MonitorResult:
        found = {"observed": latest, "max_change_pct": rule.max_change_pct, "earlier_runs": len(earlier),
                 "lookback_runs": rule.lookback_runs, **where, **metrics}
        return MonitorResult(monitor=CHANGE, status=status, observed=shown, expected=rule.change_summary(),
                             details=details, metrics={key: value for key, value in found.items() if value is not None},
                             **common)

    if latest is None or len(earlier) < MIN_EARLIER_RUNS:
        return result("unknown", None, "not enough history", needed_runs=MIN_EARLIER_RUNS)
    median = statistics.median(earlier)
    if median == 0 and latest != 0:
        return result("unknown", None, "the earlier runs wrote no rows, so a change in percent has no meaning",
                      baseline_median=median)
    change = 0.0 if median == 0 else (latest - median) / median * 100
    breaks = abs(change) > rule.max_change_pct
    direction = "more" if change > 0 else "fewer"
    compared = f"the median of {len(earlier)} earlier runs, {_rows(median)}"
    details = (f"{_what(counts)} {_rows(latest)}, the same as {compared}" if change == 0 else
               f"{_what(counts)} {_rows(latest)}, {abs(change):.1f} percent {direction} than {compared}")
    return result("error" if breaks else "ok", f"{change:+.1f} percent", details, baseline_median=median,
                  change_pct=round(change, 3))


__all__ = [
    "CHANGE",
    "COUNTED_OPERATIONS",
    "Counts",
    "KIND",
    "MIN_EARLIER_RUNS",
    "ROWS",
    "counts_of",
    "evaluate_volume",
]
