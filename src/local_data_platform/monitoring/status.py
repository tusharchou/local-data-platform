"""One row of health for each pipeline: what ``ldp status`` prints, and the Python objects behind it.

:func:`rows` takes a config file or a folder of them and returns a :class:`StatusRow` for each config, the worst first.
A row says how the last run went and how long ago, how long ago the last run succeeded, how big the table is, what the
four sections of the SLA say now, how many checks failed in the last 7 days and how many alerts are open::

    from local_data_platform.monitoring.status import rows

    for row in rows("config"):
        print(row.pipeline, row.health, row.freshness, row.open_alerts)

It only reads. It evaluates the monitors in memory with :func:`~local_data_platform.monitoring.evaluation.evaluate`, the
code that ``ldp monitor`` runs, and it records no result and sends no alert. The run events, the earlier results and the
quality results come from a :class:`~local_data_platform.monitoring.ledger.Ledger`, so each ``_ldp`` table is read once
for all of the configs that share a catalog.

A config that cannot be read or evaluated is a row too, with the reason in ``problem``, so a panel that shows the rows
shows it and does not fail.

This module needs only the standard library when it is imported.
"""

import dataclasses
import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import LDPError
from local_data_platform.monitoring.results import SEVERITY, MonitorResult, format_moment, worst

NO_SLA = "no sla"
"""What the columns of the SLA and the health say for a config that has no ``metadata.sla``."""

QUALITY_DAYS = 7
"""How many days back ``quality_7d`` counts failed checks."""

RECENT_RUNS = 20
"""How many runs of each pipeline a row keeps for the page."""

SUCCESS = ("published", "skipped_duplicate", "succeeded")
"""The statuses of a run that went well. A duplicate that was skipped is a table that is up to date."""

FAILING = ("failed", "upstream_failed")
"""The statuses of a run that broke, which make a pipeline an error whatever its monitors say."""

HELD = ("blocked_quality",)
"""The statuses of a run that a data gate held back, which make a pipeline a warning."""

HEALTH = ("error", "warn", "unknown", "ok", NO_SLA)
"""The values of ``health``, the worst first, which is the order of the rows."""

SECTIONS = {"freshness": "freshness", "volume": "volume", "schema": "schema", "metrics": "metric"}
"""The columns of the SLA, and the ``kind`` of the monitors that each one sums up."""

COLUMNS = ("pipeline", "table", "owner", "last_run", "last_success_age", "rows", "freshness", "volume", "schema",
           "metrics", "quality_7d", "open_alerts")
"""The columns of the table that ``ldp status`` prints."""

_UTC = dt.timezone.utc


def age_text(seconds: float | None) -> str:
    """A length of time in the units of the monitors, such as ``12.5 minute``, ``30.1 hour`` or ``3.4 day``.

    Under an hour it is minutes, under 48 hours it is hours, and after that it is days. ``None`` is ``-``.
    """
    if seconds is None:
        return "-"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minute"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} hour"
    return f"{seconds / 86400:.1f} day"


def _age(moment: dt.datetime, since: dt.datetime) -> float:
    return max((moment - since).total_seconds(), 0.0)


@dataclasses.dataclass(frozen=True)
class Run:
    """One run of a pipeline, folded from its events.

    Attributes:
        run_id: The id of the run.
        attempt: The attempt, which is 1 unless a task of a workflow was retried.
        status: ``published``, ``skipped_duplicate``, ``blocked_quality``, ``failed``, ``succeeded``,
            ``upstream_failed``, or ``running`` when no event ended it.
        started_at: The moment of its first event.
        rows_read: The rows that it read, or ``None``.
        rows_written: The rows that it wrote, or ``None``.
        checks: The checks that passed over the checks that ran, such as ``3/4``, or ``None`` when none ran.
        duration_s: How long it took, or ``None``.
        snapshot_id: The snapshot that it wrote, or ``None``.
    """

    run_id: str
    attempt: int
    status: str
    started_at: dt.datetime
    rows_read: int | None = None
    rows_written: int | None = None
    checks: str | None = None
    duration_s: float | None = None
    snapshot_id: int | None = None

    @classmethod
    def of(cls, summary: Mapping[str, Any]) -> "Run":
        """The run that a summary of :func:`~local_data_platform.events.summarize_runs` describes."""
        return cls(run_id=summary["run_id"], attempt=summary["attempt"], status=summary["status"],
                   started_at=summary["started_at"], rows_read=summary["rows_read"],
                   rows_written=summary["rows_written"], checks=summary["checks"], duration_s=summary["duration_s"],
                   snapshot_id=summary["snapshot_id"])

    def age(self, now: dt.datetime) -> float:
        """Seconds from the start of the run to ``now``, and never below 0."""
        return _age(now, self.started_at)

    def to_dict(self, now: dt.datetime) -> dict[str, Any]:
        """The run as JSON-serialisable primitives, with its age at ``now``."""
        return {"run_id": self.run_id, "attempt": self.attempt, "status": self.status,
                "started_at": format_moment(self.started_at), "age_seconds": round(self.age(now), 3),
                "rows_read": self.rows_read, "rows_written": self.rows_written, "checks": self.checks,
                "duration_s": self.duration_s, "snapshot_id": self.snapshot_id}


@dataclasses.dataclass(frozen=True)
class StatusRow:
    """The health of one pipeline at one moment.

    Attributes:
        pipeline: The identifier of the config, or the name of its file when it could not be read.
        table: The target table, such as ``shop.orders``, or the path of the file of a target that is a file.
        owner: The ``who`` of the config.
        file: The config file.
        as_of: The moment that the row was measured at.
        health: The worst of what the row says, one of :data:`HEALTH`. A monitor in ``error``, a run that failed and a
            config that has a problem make it ``error``. A monitor in ``warn`` and a run that a data gate held back make
            it ``warn``. It is ``no sla`` for a config that has no SLA and a run that did not break.
        last_run: The latest run, or ``None`` when the history has none.
        last_success_age_seconds: Seconds since the start of the last run that went well, or ``None``.
        last_commit_age_seconds: Seconds since the last commit of the table, read from its metadata, or ``None``.
        rows: The rows in the table, from the summary of its current snapshot, or ``None``.
        freshness: What the freshness monitor says, ``ok``, ``warn``, ``error`` or ``unknown``. It is :data:`NO_SLA`
            for a config with no SLA, and ``None`` for an SLA that has no such section. ``volume``, ``schema`` and
            ``metrics`` are the same for their sections, and ``metrics`` is the worst of all of its monitors.
        quality_7d: How many checks failed in the last :data:`QUALITY_DAYS` days, or ``None`` when no check ever ran.
        open_alerts: How many monitors have an alert open on a channel, or ``None`` when the config has no channel.
        history_since: The oldest run event or monitor result of the pipeline, so that a history that was trimmed shows.
        sla: The SLA in a line, or ``None``.
        results: The monitors behind the columns of the SLA.
        recent_runs: The last :data:`RECENT_RUNS` runs, the newest first.
        problem: Why the config could not be read or evaluated, or ``None``.
    """

    pipeline: str
    table: str | None
    owner: str | None
    file: str | None
    as_of: dt.datetime
    health: str
    last_run: Run | None = None
    last_success_age_seconds: float | None = None
    last_commit_age_seconds: float | None = None
    rows: int | None = None
    freshness: str | None = None
    volume: str | None = None
    schema: str | None = None
    metrics: str | None = None
    quality_7d: int | None = None
    open_alerts: int | None = None
    history_since: dt.datetime | None = None
    sla: str | None = None
    results: tuple[MonitorResult, ...] = ()
    recent_runs: tuple[Run, ...] = ()
    problem: str | None = None

    def cells(self) -> dict[str, Any]:
        """The row as the text of the table, one cell for each of :data:`COLUMNS`."""
        last = self.last_run
        success = age_text(self.last_success_age_seconds)
        if self.last_success_age_seconds is None and self.last_commit_age_seconds is not None:
            success = f"{age_text(self.last_commit_age_seconds)} (commit)"  # no run history, so the commit is the news
        return {
            "pipeline": self.pipeline, "table": self.table or "-", "owner": self.owner or "-",
            "last_run": "-" if last is None else f"{last.status} {age_text(last.age(self.as_of))}",
            "last_success_age": success, "rows": "-" if self.rows is None else self.rows,
            "freshness": self.freshness or "-", "volume": self.volume or "-", "schema": self.schema or "-",
            "metrics": self.metrics or "-", "quality_7d": "-" if self.quality_7d is None else self.quality_7d,
            "open_alerts": "-" if self.open_alerts is None else self.open_alerts}

    def to_dict(self) -> dict[str, Any]:
        """The row as JSON-serialisable primitives. Times are ISO 8601 in UTC and ages are seconds."""
        def seconds(value: float | None) -> float | None:
            return None if value is None else round(value, 3)

        return {
            "pipeline": self.pipeline, "table": self.table, "owner": self.owner, "file": self.file,
            "as_of": format_moment(self.as_of), "health": self.health,
            "last_run": None if self.last_run is None else self.last_run.to_dict(self.as_of),
            "last_success_age_seconds": seconds(self.last_success_age_seconds),
            "last_commit_age_seconds": seconds(self.last_commit_age_seconds), "rows": self.rows,
            "freshness": self.freshness, "volume": self.volume, "schema": self.schema, "metrics": self.metrics,
            "quality_7d": self.quality_7d, "open_alerts": self.open_alerts,
            "history_since": None if self.history_since is None else format_moment(self.history_since),
            "sla": self.sla, "results": [result.to_dict() for result in self.results],
            "recent_runs": [run.to_dict(self.as_of) for run in self.recent_runs], "problem": self.problem}


def _health(results: Iterable[MonitorResult], last_run: Run | None) -> str:
    statuses = [result.status for result in results]
    if last_run is not None and last_run.status in FAILING:
        statuses.append("error")
    elif last_run is not None and last_run.status in HELD:
        statuses.append("warn")
    return max(statuses, key=SEVERITY.__getitem__) if statuses else NO_SLA


def _sections(config: Any, results: Sequence[MonitorResult]) -> dict[str, str | None]:
    """What each section of the SLA says, ``None`` for one that the SLA does not have.

    A section that the SLA has always gives at least one result, and an SLA has at least one section, so a section with
    no result is one that the SLA does not have.
    """
    if config.sla is None:
        return {name: NO_SLA for name in SECTIONS}
    return {name: worst(result for result in results if result.kind == kind) for name, kind in SECTIONS.items()}


def _table_size(table: Any, now: dt.datetime) -> tuple[float | None, int | None]:
    """How long ago the last commit was, and the rows of the table, from the metadata of the current snapshot."""
    snapshot = table.current_snapshot() if table is not None else None
    if snapshot is None:
        return None, None
    commit = dt.datetime.fromtimestamp(snapshot.timestamp_ms / 1000, tz=_UTC)
    summary = getattr(snapshot, "summary", None)
    total = (summary.additional_properties if summary is not None else {}).get("total-records")
    try:
        rows = None if total is None else int(total)
    except ValueError:
        rows = None
    return _age(now, commit), rows


def _row(file: Path, config: Any, now: dt.datetime, ledger: Any) -> StatusRow:
    from local_data_platform.events import summarize_runs
    from local_data_platform.monitoring.alerts import holds_open
    from local_data_platform.monitoring.evaluation import evaluate
    from local_data_platform.monitoring.target import load_target
    from local_data_platform.notify import notifier_for

    target = load_target(config)
    results = evaluate(config, now, history=ledger, target=target)
    events = [event for event in ledger.run_events(config) if event.ts <= now]
    summaries = [run for run in summarize_runs(events, final_attempts_only=True)
                 if not run["is_workflow"] and run["status"] != "released"]
    runs = [Run.of(summary) for summary in summaries]
    success = next((run for run in runs if run.status in SUCCESS), None)
    commit_age, size = _table_size(target.table, now)
    earlier = [result for result in ledger.monitor_results(config) if result.evaluated_at <= now]
    moments = [event.ts for event in events] + [result.evaluated_at for result in earlier]
    notifier = notifier_for(config)
    return StatusRow(
        pipeline=config.identifier, table=target.identifier, owner=config.who or None, file=str(file), as_of=now,
        health=_health(results, runs[0] if runs else None),
        last_run=runs[0] if runs else None,
        last_success_age_seconds=None if success is None else success.age(now),
        last_commit_age_seconds=commit_age, rows=size, **_sections(config, results),
        quality_7d=ledger.failed_checks(config, now),
        open_alerts=None if notifier is None else holds_open(notifier, results, earlier, now),
        history_since=min(moments) if moments else None,
        sla=config.sla.summary() if config.sla is not None else None,
        results=tuple(results), recent_runs=tuple(runs[:RECENT_RUNS]))


def _problem(file: Path, config: Any, now: dt.datetime, message: str) -> StatusRow:
    return StatusRow(pipeline=config.identifier if config is not None else file.name, table=None,
                     owner=(config.who or None) if config is not None else None, file=str(file), as_of=now,
                     health="error", problem=message)


def rows(path: str | Path, now: dt.datetime | str | None = None) -> list[StatusRow]:
    """The health of every pipeline of a config file or of a folder of configs, the worst first.

    Nothing is recorded and nothing is sent, and nothing is created: no catalog, no namespace and no file.

    Args:
        path: A dataset config, or a folder of them.
        now: The moment to measure at, as a ``datetime`` or as ISO 8601 text, where a time with no offset is UTC. The
            current time by default. The last run, the last success, the failed checks and the oldest event leave out
            what came after it, and the monitors read the history as ``ldp monitor --now`` does.

    Returns:
        A :class:`StatusRow` for each config, and for each file that could not be read as one, ordered by ``health``
        (:data:`HEALTH`) and then by pipeline.

    Raises:
        ConfigError: If the path is not there, a folder has no dataset config, or ``now`` is not a time.
    """
    from local_data_platform.lake import parse_moment
    from local_data_platform.monitoring.configs import load_configs
    from local_data_platform.monitoring.ledger import Ledger

    moment = dt.datetime.now(_UTC) if now is None else parse_moment(now)
    configs, failures = load_configs(path)
    ledger = Ledger([config for _, config in configs], since=moment - dt.timedelta(days=QUALITY_DAYS))
    found = []
    for file, config in configs:
        try:
            found.append(_row(file, config, moment, ledger))
        except Exception as error:  # noqa: BLE001 - one config that cannot be evaluated must not hide the others
            found.append(_problem(file, config, moment,
                                  str(error) if isinstance(error, LDPError) else f"{type(error).__name__}: {error}"))
    found += [_problem(file, None, moment, message) for file, message in failures]
    return sorted(found, key=lambda row: (HEALTH.index(row.health), row.pipeline))


__all__ = [
    "COLUMNS",
    "FAILING",
    "HEALTH",
    "HELD",
    "NO_SLA",
    "QUALITY_DAYS",
    "RECENT_RUNS",
    "Run",
    "SECTIONS",
    "SUCCESS",
    "StatusRow",
    "age_text",
    "rows",
]
