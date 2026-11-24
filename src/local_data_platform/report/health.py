"""The data health panel of a report, and the gate that holds a report back when its data is stale.

Every report shows one row for each table that its queries read. The row is the row of ``ldp status`` for the pipeline
that writes the table, from :func:`local_data_platform.monitoring.status.rows`, so the panel says what the platform says
about that pipeline. It has the last run, its status, the rows that it wrote, when it finished, the checks that passed
out of the checks that ran with the names of the ones that failed, and the verdict of the freshness monitor. Freshness
is worked out by the monitors of 0.1.5 and not here. A table that has no run data says so, and a table that no config of
the report writes says that too.

The panel names moments and verdicts and no ages, so two builds over the same snapshots and the same health are the same
text except for the time of the build, which the page already has.

``metadata.freshness`` of the report is the gate. A table is stale for the gate when

* its last run ended ``failed``, ``upstream_failed`` or ``blocked_quality``,
* its news is older than ``max_age_hours``, where its news is the start of its last successful run, and the time of its
  last commit, which 0.1.5 reads from the metadata of Iceberg, when no run succeeded and no run data says more, or
* ``max_age_hours`` is not set and the freshness monitor of the SLA of its pipeline says ``warn`` or ``error``.

A table whose health cannot be read is stale too, because a report that cannot say that its data is fresh must not say
that it is. ``on_stale`` ``warn`` puts a banner above the report that names the stale tables. ``on_stale`` ``block``
makes :func:`local_data_platform.report.build.build_report` raise :class:`ReportHeld`, which is a
:class:`~local_data_platform.exceptions.DataQualityError`, so ``ldp report build`` exits with 3, the status of a data
gate in every ``ldp`` command, and writes nothing. A report with no ``metadata.freshness`` has the panel and no gate.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_data_platform import html
from local_data_platform.events import redact_text
from local_data_platform.exceptions import DataQualityError
from local_data_platform.monitoring.status import FAILING, HELD, NO_SLA, Run, StatusRow
from local_data_platform.report.formats import format_value, utc_text

if TYPE_CHECKING:
    from local_data_platform.report.engine import TableRead
    from local_data_platform.report.spec import Freshness, Report

STALE_RUNS = FAILING + HELD
"""The statuses of a last run that make a table stale for the gate, which are the runs that broke and the runs that a
data gate held back."""

COLUMNS = ("Table", "Last run", "Status", "Rows written", "Finished", "Checks", "Freshness")
"""The columns of the panel. A report that has a gate also has ``Stale``."""

BANNER_WORDS = ("Some of the tables behind this report are out of date or held back, so check the numbers that come "
                "from them.")
"""What the banner says before it names the stale tables."""

STALE = "Stale"
NO_RUN_DATA = "no run data"
NOT_MONITORED = "no pipeline"
UNREADABLE = "health could not be read"

_KINDS = {"ok": "ok", "published": "ok", "skipped_duplicate": "ok", "succeeded": "ok", "warn": "warn",
          "blocked_quality": "warn", "error": "bad", "failed": "bad", "upstream_failed": "bad"}
"""The colour of a chip for each verdict of a monitor and each status of a run. The others are neutral."""


# --------------------------------------------------------------------------- what the panel holds


@dataclass(frozen=True)
class TableHealth:
    """The health of one table that a report read.

    Attributes:
        table: The table, such as ``gold.gold_sales``.
        pipeline: The identifier of the config that writes it, or ``None`` when no config of the report does.
        row: The row of :func:`~local_data_platform.monitoring.status.rows` for that pipeline, or ``None`` when there
            is no pipeline or its row could not be read.
        problem: Why the health could not be read, or ``None``.
        news_at: The moment of the news about the table, which the gate compares with ``max_age_hours``, or ``None``.
        news_from: ``run`` when the news is the start of the last successful run, ``commit`` when it is the last commit
            of the table, which Iceberg says, and ``None`` when there is none.
        stale: Whether the table is stale for the gate. It is never so in a report that has no gate.
        reasons: Why it is stale, each a plain sentence part.
    """

    table: str
    pipeline: str | None = None
    row: StatusRow | None = None
    problem: str | None = None
    news_at: dt.datetime | None = None
    news_from: str | None = None
    stale: bool = False
    reasons: tuple[str, ...] = ()

    @property
    def last_run(self) -> Run | None:
        """The last run of the pipeline, or ``None`` when its history has none."""
        return self.row.last_run if self.row is not None else None

    @property
    def freshness(self) -> str | None:
        """The verdict of the freshness monitor of the SLA, or ``None`` when there is no row."""
        return self.row.freshness if self.row is not None else None

    @property
    def has_run_data(self) -> bool:
        """Whether the history of the pipeline has a run."""
        return self.last_run is not None

    def to_dict(self) -> dict[str, Any]:
        """The table as JSON-serialisable primitives. Moments are ISO 8601 in UTC, and there are no ages."""
        from local_data_platform.monitoring.results import format_moment

        run = self.last_run
        return {
            "table": self.table, "pipeline": self.pipeline, "health": self.row.health if self.row else None,
            "last_run": None if run is None else {
                "run_id": run.run_id, "status": run.status, "started_at": format_moment(run.started_at),
                "finished_at": format_moment(run.finished_at) if run.finished_at else None,
                "rows_written": run.rows_written, "checks": run.checks, "failing_checks": list(run.failing_checks)},
            "freshness": self.freshness,
            "news_at": format_moment(self.news_at) if self.news_at else None, "news_from": self.news_from,
            "no_run_data": not self.has_run_data, "stale": self.stale, "reasons": list(self.reasons),
            "problem": self.problem}


def stale_text(tables: Sequence[TableHealth]) -> str:
    """Tables in one line, such as ``1 table is stale, gold.a``, or an empty text when there is none."""
    names = [item.table for item in tables]
    if not names:
        return ""
    many = len(names) != 1
    return f"{len(names)} table{'s' if many else ''} {'are' if many else 'is'} stale, {', '.join(names)}"


@dataclass(frozen=True)
class Health:
    """The health of every table that a report read, at the moment that it was built.

    Attributes:
        as_of: The moment that the rows were measured at, which is the moment of the build.
        rule: The ``metadata.freshness`` of the report, or ``None`` when it has none, and then there is no gate.
        tables: One entry for each table, by name.
    """

    as_of: dt.datetime
    rule: Freshness | None
    tables: tuple[TableHealth, ...]

    @property
    def stale(self) -> tuple[TableHealth, ...]:
        """The tables that are stale for the gate."""
        return tuple(item for item in self.tables if item.stale)

    @property
    def held(self) -> bool:
        """Whether the report is held back, which is when a table is stale and the gate says ``block``."""
        return bool(self.stale) and self.rule is not None and self.rule.on_stale == "block"

    @property
    def banner(self) -> bool:
        """Whether the report shows a banner, which is when a table is stale and the gate says ``warn``."""
        return bool(self.stale) and self.rule is not None and self.rule.on_stale == "warn"

    def to_dict(self) -> dict[str, Any]:
        """The health as JSON-serialisable primitives, measured at the moment of the build, which the document has."""
        rule = None if self.rule is None else {"max_age_hours": self.rule.max_age_hours, "on_stale": self.rule.on_stale}
        return {"rule": rule, "stale": [item.table for item in self.stale],
                "tables": [item.to_dict() for item in self.tables]}

    def summary(self) -> str:
        """The stale tables in one line, such as ``2 tables are stale, gold.a, gold.b``, or an empty text."""
        return stale_text(self.stale)


@dataclass(frozen=True)
class Waiting:
    """A pipeline of ``metadata.after`` that has no published run for the window, so the report does not go yet.

    Attributes:
        pipeline: The identifier of the pipeline.
        reason: Why, as the end of a sentence, such as ``it has not run for the window``.
    """

    pipeline: str
    reason: str


class ReportHeld(DataQualityError):
    """A report was held back, because the data it needs is not ready.

    That is a table that it read and that is stale while ``metadata.freshness`` says ``block``, which stops a build and
    a send, or a pipeline of ``metadata.after`` that has no published run for the window, which stops a send. It is a
    data gate, so ``ldp report build`` and ``ldp report send`` exit with 3 and a scheduler does not retry it within the
    tick, because the data has to change first. A window that was held has no delivery, so the next tick runs it again.

    Attributes:
        identifier: The identifier of the report.
        tables: The stale tables, each with its reasons.
        waiting: The pipelines of ``metadata.after`` that have no published run for the window, each with its reason.
        window: The window that the pipelines are waited for, as ``START/END`` in UTC, or ``None``.
    """

    def __init__(self, identifier: str, tables: Sequence[TableHealth] = (), *, waiting: Sequence[Waiting] = (),
                 window: str | None = None):
        self.identifier = identifier
        self.tables = tuple(tables)
        self.waiting = tuple(waiting)
        self.window = window
        lines: list[str] = []
        if self.waiting:
            count = len(self.waiting)
            when = f" {window}" if window else ""
            lines.append(f"the report {identifier} was held, because {count} pipeline{'s' if count != 1 else ''} of "
                         f"metadata.after {'have' if count != 1 else 'has'} no published run for the window{when}:")
            lines += [f"  - {item.pipeline}: {item.reason}" for item in self.waiting]
        if self.tables:
            count = len(self.tables)
            lines.append(f"the report {identifier} was held, because {count} table{'s' if count != 1 else ''} "
                         f"{'are' if count != 1 else 'is'} stale and metadata.freshness says block")
            lines += [f"  - {item.table}: {' and '.join(item.reasons)}" for item in self.tables]
        super().__init__("\n".join(lines))

    def summary(self) -> str:
        """What holds the report in one line, such as ``2 tables are stale, gold.a, gold.b`` or ``waiting for a``.

        A report that is held for both says both, with ``and`` between them.
        """
        parts = []
        if self.waiting:
            parts.append(f"waiting for {', '.join(item.pipeline for item in self.waiting)}")
        if self.tables:
            parts.append(stale_text(self.tables))
        return " and ".join(parts)


# --------------------------------------------------------------------------- judging a table


def _hours(hours: float) -> str:
    return f"{hours:g} hour{'' if hours == 1 else 's'}"


def news_of(row: StatusRow) -> tuple[dt.datetime | None, str | None]:
    """The news about the table of a row, and where it comes from.

    It is the start of the last successful run, and when no run succeeded it is the last commit of the table, which
    ``ldp status`` reads from the metadata of Iceberg. It is the order in which ``ldp status`` writes its own column of
    the last success.

    Returns:
        ``(moment, source)`` where source is ``run`` or ``commit``, or ``(None, None)`` when there is neither.
    """
    if row.last_success_age_seconds is not None:
        return row.as_of - dt.timedelta(seconds=row.last_success_age_seconds), "run"
    if row.last_commit_age_seconds is not None:
        return row.as_of - dt.timedelta(seconds=row.last_commit_age_seconds), "commit"
    return None, None


def _old(moment: dt.datetime, source: str | None, limit: float) -> str:
    what = "successful run" if source == "run" else "commit"
    return f"its last {what} was at {utc_text(moment)}, which is more than the {_hours(limit)} that this report allows"


def judge(table: str, pipeline: str | None, row: StatusRow | None, rule: Freshness | None,
          problem: str | None = None) -> TableHealth:
    """The health of a table from its row, and whether the gate calls it stale.

    Args:
        table: The table.
        pipeline: The pipeline that writes it, or ``None``.
        row: The row of ``ldp status`` for the pipeline, or ``None`` when there is none.
        rule: The ``metadata.freshness`` of the report, or ``None`` for a report with no gate.
        problem: Why the row could not be read, or ``None``. A row that has a problem of its own counts too. The text
            goes through :func:`~local_data_platform.events.redact_text`, because a page and an event carry it.
    """
    problem = problem or (row.problem if row is not None else None)
    problem = redact_text(problem) if problem else None  # a page is read by more people than a terminal is
    if row is None:
        reasons = (f"its health could not be read ({problem})",) if problem and rule else ()
        return TableHealth(table=table, pipeline=pipeline, problem=problem, stale=bool(reasons), reasons=reasons)
    news, source = news_of(row) if problem is None else (None, None)
    reasons: list[str] = []
    if rule is not None:
        run = row.last_run
        if problem is not None:
            reasons.append(f"its health could not be read ({problem})")
        else:
            if run is not None and run.status in STALE_RUNS:
                names = f" (failing {', '.join(run.failing_checks)})" if run.failing_checks else ""
                reasons.append(f"its last run ended {run.status}{names}")
            if rule.max_age_hours is not None:
                if news is None:
                    reasons.append("it has never been written")
                elif (row.as_of - news).total_seconds() > rule.max_age_hours * 3600:
                    reasons.append(_old(news, source, rule.max_age_hours))
            elif row.freshness in ("warn", "error"):
                reasons.append(f"the freshness monitor of its SLA says {row.freshness}")
    return TableHealth(table=table, pipeline=pipeline, row=row, problem=problem, news_at=news, news_from=source,
                       stale=bool(reasons), reasons=tuple(reasons))


# --------------------------------------------------------------------------- collecting the rows


def config_index(report: Report) -> dict[str, tuple[Path, Any]]:
    """The config file and the config of each pipeline in the config folders of the report, by identifier."""
    from local_data_platform.exceptions import ConfigError
    from local_data_platform.monitoring.configs import load_configs
    from local_data_platform.paths import resolve_path

    found: dict[str, tuple[Path, Any]] = {}
    for item in report.tables.config:
        try:
            configs, _failures = load_configs(resolve_path(item, report.base_dir))
        except ConfigError:
            continue  # a folder that holds no dataset config, such as one of metrics
        for file, config in configs:
            found.setdefault(config.identifier, (file, config))
    return found


def _config_files(report: Report) -> dict[str, Path]:
    """The config file of each pipeline in the config folders of the report, by identifier."""
    return {identifier: file for identifier, (file, _config) in config_index(report).items()}


def _status_row(file: Path, as_of: dt.datetime) -> tuple[StatusRow | None, str | None]:
    """The row of ``ldp status`` for one config file, or the reason that there is none."""
    from local_data_platform.monitoring.status import rows

    try:
        found = rows(file, as_of)
    except Exception as error:  # noqa: BLE001 - a table whose health cannot be read is a row, and not a failed build
        return None, f"{type(error).__name__}: {error}"
    return (found[0], None) if found else (None, f"{file.name} has no pipeline")


def _commit_news(report: Report, tables: Sequence[str], rule: Freshness | None,
                 as_of: dt.datetime) -> dict[str, TableHealth]:
    """The health of tables that no config of the report writes, from the metadata of Iceberg.

    It asks the freshness monitor of 0.1.5 for the age of each table with the gate of the report as its rule, so the age
    of a table is worked out by the monitor and not here. A report whose gate has no ``max_age_hours`` has no rule to
    ask with, and then these tables have no verdict.
    """
    if rule is None or rule.max_age_hours is None or not tables:
        return {}
    from local_data_platform.mcp_server.discovery import discover_tables
    from local_data_platform.monitoring.freshness import evaluate_freshness
    from local_data_platform.monitoring.sla import Duration
    from local_data_platform.monitoring.sla import Freshness as SlaFreshness
    from local_data_platform.monitoring.target import Target
    from local_data_platform.paths import resolve_path

    limit = SlaFreshness(error_after=Duration(max(1, round(rule.max_age_hours * 60)), "minute"))
    found: dict[str, TableHealth] = {}
    discovery = discover_tables([resolve_path(item, report.base_dir) for item in report.tables.config],
                                [resolve_path(item, report.base_dir) for item in report.tables.catalog],
                                allow=list(tables))
    try:
        for exposed in discovery.tables:
            try:
                target = Target(pipeline=exposed.identifier, identifier=exposed.identifier, iceberg=True,
                                table=exposed.load())
                result = evaluate_freshness(limit, target, as_of)
            except Exception as error:  # noqa: BLE001 - see _status_row
                found[exposed.identifier] = judge(exposed.identifier, None, None, rule,
                                                  f"{type(error).__name__}: {error}")
                continue
            reference = result.metrics.get("reference_time")
            news = dt.datetime.fromisoformat(reference) if reference else None
            reasons = ()
            if result.observed == "never written":
                reasons = ("it has never been written",)
            elif result.status == "error" and news is not None:
                reasons = (_old(news, "commit", rule.max_age_hours),)
            elif result.status == "unknown":
                reasons = (f"its age could not be read ({result.details})",)
            found[exposed.identifier] = TableHealth(table=exposed.identifier, news_at=news,
                                                    news_from="commit" if news else None, stale=bool(reasons),
                                                    reasons=reasons)
    finally:
        discovery.close()
    return found


def collect(report: Report, reads: Mapping[str, TableRead], as_of: dt.datetime) -> Health:
    """The health of every table that a report read.

    It reads nothing that it does not need and writes nothing. For each table it asks ``ldp status`` about the pipeline
    that writes it, as :func:`local_data_platform.monitoring.status.rows` does, with the moment of the build as the
    moment to measure at. It does not raise for a table that it cannot read, which is a row with its reason.

    Args:
        report: The report, which names the config folders that the pipelines are in and the gate.
        reads: The tables that its queries read, from :attr:`~local_data_platform.report.engine.ReportRun.reads`.
        as_of: The moment to measure at, with a time zone.
    """
    rule = report.freshness
    files = _config_files(report)
    found: dict[str, TableHealth] = {}
    unmonitored: list[str] = []
    for name in reads:
        pipeline = reads[name].pipeline
        file = files.get(pipeline) if pipeline else None
        if file is None:
            unmonitored.append(name)
            continue
        row, problem = _status_row(file, as_of)
        found[name] = judge(name, pipeline, row, rule, problem)
    measured = _commit_news(report, unmonitored, rule, as_of)
    for name in unmonitored:
        found[name] = measured.get(name) or TableHealth(table=name)
    return Health(as_of=as_of, rule=rule, tables=tuple(found[name] for name in sorted(found)))


# --------------------------------------------------------------------------- writing the panel


def columns(health: Health) -> list[str]:
    """The headings of the panel, with ``Stale`` when the report has a gate."""
    return [*COLUMNS, *([STALE] if health.rule is not None else [])]


def _moment(moment: dt.datetime | None) -> str:
    return "-" if moment is None else utc_text(moment)


def news_text(item: TableHealth) -> str:
    """Where the news about a table comes from and when, such as ``last success 2026-08-02 06:00:03 UTC``, or ``-``."""
    if item.news_at is None:
        return "-"
    return f"{'last success' if item.news_from == 'run' else 'last commit'} {utc_text(item.news_at)}"


def cells(item: TableHealth, health: Health) -> list[str]:
    """The text of each cell of a row of the panel, in the order of :func:`columns`."""
    run = item.last_run
    if run is None:
        reason = f"{UNREADABLE} ({item.problem})" if item.problem else (
            NO_RUN_DATA if item.pipeline is not None else NOT_MONITORED)
        last = [reason, "-", "-", "-", "-"]
    else:
        checks = run.checks or "-"
        if run.failing_checks:
            checks += f", failing {', '.join(run.failing_checks)}"
        finished = "running" if run.status == "running" else _moment(run.finished_at)
        rows = format_value(run.rows_written, "integer")
        last = [_moment(run.started_at), run.status, rows, finished, checks]
    verdict = item.freshness or "-"
    if item.news_at is not None:
        verdict += f", {news_text(item)}"
    stale = [("yes" if item.stale else "no")] if health.rule is not None else []
    return [item.table, *last, verdict, *stale]


_MOMENT = re.compile(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d UTC)")


def _chip(text: str) -> Any:
    return html.chip(text, _KINDS.get(text))


def _moments(text: str) -> Any:
    """The text with each moment in it as a date and a time that do not break inside, so a cell wraps between them."""
    parts: list[Any] = []
    last = 0
    for found in _MOMENT.finditer(text):
        parts += [text[last:found.start()], html.h("span", found.group(1), class_="nowrap"), " ",
                  html.h("span", found.group(2), class_="nowrap")]
        last = found.end()
    parts.append(text[last:])
    return html.join(*parts, separator="")


def _html_row(item: TableHealth, health: Health) -> list[Any]:
    texts = cells(item, health)
    status = _chip(texts[2]) if item.last_run is not None else texts[2]
    verdict = item.freshness
    freshness: Any = _moments(texts[6])
    if verdict is not None and verdict != NO_SLA:
        news = _moments(news_text(item)) if item.news_at is not None else None
        freshness = html.join(_chip(verdict), news, separator=" ")
    row: list[Any] = [html.h("code", item.table), _moments(texts[1]), status, texts[3], _moments(texts[4]), texts[5],
                      freshness]
    if health.rule is not None:
        row.append(html.chip("yes", "bad") if item.stale else "no")
    return row


def banner(health: Health) -> Any:
    """The banner of a report that shows stale data with a warning, or ``None`` when it has none."""
    if not health.banner:
        return None
    items = [html.h("li", html.h("code", item.table), " ", " and ".join(item.reasons)) for item in health.stale]
    return html.h("div", html.join(html.h("p", html.h("strong", "Stale data."), " ", BANNER_WORDS),
                                   html.h("ul", html.join(*items))), class_="banner", role="status")


def panel(health: Health) -> Any:
    """The data health panel, a card with one row for each table that the report read."""
    note = html.h("p", "One row for each table that this report read, measured when it was built.", class_="note")
    if health.tables:
        body = html.table(columns(health), [_html_row(item, health) for item in health.tables], right=[3])
    else:
        body = html.h("p", "This report read no table, so there is no data to check.")
    return html.h("section", html.join(html.h("h2", "Data health", id="health-h"), note, body), class_="card",
                  aria_labelledby="health-h")


__all__ = [
    "BANNER_WORDS",
    "COLUMNS",
    "Health",
    "ReportHeld",
    "STALE_RUNS",
    "TableHealth",
    "Waiting",
    "banner",
    "cells",
    "collect",
    "columns",
    "config_index",
    "judge",
    "news_of",
    "news_text",
    "panel",
    "stale_text",
]
