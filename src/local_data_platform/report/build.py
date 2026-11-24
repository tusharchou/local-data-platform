"""Building a report: run its queries, write the page, and record the build as run events.

:func:`build_report` is what ``ldp report build`` does. It runs every query and every metric block as
``ldp report check`` does, and when all of them ran and every block can be shown it writes the report in one of three
formats, ``html``, ``md`` or ``json``. A report with a problem is not built. Nothing is written, the file that was
there is left as it was, and the problems are the error.

The file goes through :mod:`local_data_platform.fs`, so ``out`` can be a local path, a ``file://`` URI, ``s3://`` or
``gs://``, and a reader sees the old file or the whole new one and never half of it.

A build is a run of its own for the sinks in the ``metadata.observability`` of the report. It emits ``run.started``,
``report.rendered`` with what was written and which version of each table was read, and ``run.finished``. A build that
fails emits ``run.failed`` and a ``run.finished`` of status ``failed`` instead. No event has a row of data.
``ldp runs REPORT`` lists the builds.

Before it renders, a build asks :func:`local_data_platform.report.health.collect` about the health of the tables that
the queries read, which is the panel that every report shows. When ``metadata.freshness`` says ``block`` and a table is
stale, nothing is written. The build emits ``report.held`` and a ``run.finished`` of status ``blocked_quality``, and
raises :class:`~local_data_platform.report.health.ReportHeld`, which ``ldp report build`` turns into exit status 3.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform import events, fs
from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.paths import resolve_path
from local_data_platform.report.engine import ReportRun, run_queries
from local_data_platform.report.health import Health, ReportHeld, collect
from local_data_platform.report.render_html import render_html
from local_data_platform.report.render_json import render_json
from local_data_platform.report.render_md import render_md
from local_data_platform.report.spec import Report
from local_data_platform.spec import format_instant

FORMATS = ("html", "md", "json")
"""The formats that a report is written in."""

STDOUT = "-"
"""The ``out`` that means standard output, which the command line writes and ``build_report`` only renders for."""


class ReportBuildError(LDPError):
    """A report was not built, because a query did not run or a block cannot be shown.

    Attributes:
        report: The identifier of the report.
        problems: Every problem, each starting with a JSON path.
    """

    def __init__(self, report: str, problems: list[str]):
        self.report = report
        self.problems = list(problems)
        count = len(self.problems)
        plural = "s" if count != 1 else ""
        super().__init__("\n".join([f"the report {report} has {count} problem{plural} and was not built",
                                    *[f"  - {item}" for item in self.problems]]))


@dataclass
class Build:
    """What a build did.

    Attributes:
        report: The report.
        run: The run that the document was made from, with its tables, its reads, its views and its health.
        format: ``html``, ``md`` or ``json``.
        document: The text that was written, or that would be written to standard output.
        output: Where it was written, as ``out`` was given, or ``None`` when ``out`` was not given or was ``-``.
        bytes: How many bytes the document has.
        sha256: The digest of those bytes.
        built_at: The moment of the build.
        run_id: The id of the build among the run events.
        duration_ms: How long it took.
        health: The health of the tables that the report read, which the document shows.
    """

    report: Report
    run: ReportRun
    format: str
    document: str
    output: str | None
    bytes: int
    sha256: str
    built_at: dt.datetime
    run_id: str
    duration_ms: float
    health: Health


def render(run: ReportRun, fmt: str, built_at: dt.datetime, home: str | None = None) -> str:
    """The document of a run in a format.

    ``home`` is the link to the index of a site that an HTML page starts with, see
    :func:`~local_data_platform.report.render_html.render_html`. Markdown and JSON have no page to link from, so they
    ignore it.

    Raises:
        ConfigError: If the format is not one of :data:`FORMATS`.
    """
    if fmt == "html":
        return render_html(run, built_at=built_at, home=home)
    if fmt == "md":
        return render_md(run, built_at=built_at)
    if fmt == "json":
        return render_json(run, built_at=built_at)
    raise ConfigError(f"unknown format {fmt!r}. The formats are {list(FORMATS)}")


def write_document(out: str | Path, data: bytes) -> None:
    """Write bytes to a local path, ``file://``, ``s3://`` or ``gs://`` so that a reader never sees half of them."""
    with fs.open_output_atomic(str(out)) as stream:
        stream.write(data)


def report_sink(report: Report) -> events.EventSink:
    """The sink of a report, from its ``metadata.observability``.

    It is a :class:`~local_data_platform.events.NullSink` when the report has none.
    """
    metadata: Mapping[str, Any] = {"observability": dict(report.observability)} if report.observability else {}
    sinks = [events.build_sink(spec, report.base_dir) for spec in events.sink_specs(metadata)]
    sinks = [sink for sink in sinks if not isinstance(sink, events.NullSink)]
    if not sinks:
        return events.NullSink()
    return sinks[0] if len(sinks) == 1 else events.MultiSink(sinks)


@dataclass(frozen=True)
class _EventsOf:
    """What :func:`local_data_platform.events.run_events` asks of a config, for a report."""

    identifier: str
    base_dir: Path | None
    metadata: Mapping[str, Any]

    def resolve(self, path: str | Path) -> Path:
        return resolve_path(path, self.base_dir)


def report_events(path: str | Path, run_id: str | None = None) -> tuple[list[events.RunEvent], str]:
    """The events of the builds of a report, from where its sinks keep them, as ``ldp runs`` reads them.

    Args:
        path: The report file.
        run_id: Only the events of this build.

    Returns:
        ``(events, where)``, as :func:`local_data_platform.events.run_events` returns.

    Raises:
        ConfigError: If the file is not a valid report.
    """
    from local_data_platform.report.spec import load_report

    return report_events_of(load_report(path), run_id)


def report_events_of(report: Report, run_id: str | None = None) -> tuple[list[events.RunEvent], str]:
    """The events of a report that is already read, from where its sinks keep them.

    ``ldp report send`` reads the ``report.delivered`` events of a report with it, to find the deliveries that were
    made. A report that has no sink has no events.
    """
    metadata: Mapping[str, Any] = {"observability": dict(report.observability)} if report.observability else {}
    return events.run_events(_EventsOf(report.identifier, report.base_dir, metadata), run_id=run_id)


def _window_text(run: ReportRun) -> str | None:
    """The window of a run as the two UTC instants that the events give, or ``None`` when it has none."""
    return f"{format_instant(run.window[0])}/{format_instant(run.window[1])}" if run.window else None


def held_payload(report: Report, held: ReportHeld, *, fmt: str, output: str | None, window: str | None,
                 release: str | None, live: bool) -> dict[str, Any]:
    """The payload of the ``report.held`` event that a build and a send emit when a report is held back.

    It has the rule of the freshness gate and each stale table with its pipeline and its reasons, and for a send that
    waits for the pipelines of ``metadata.after``, each of them with why. It has no row of data.
    """
    return {
        "format": fmt, "output": output, "window": window, "timezone": report.timezone, "release": release,
        "live": live,
        "rule": {"max_age_hours": report.freshness.max_age_hours, "on_stale": report.freshness.on_stale}
        if report.freshness else None,
        "tables": [{"table": item.table, "pipeline": item.pipeline, "reasons": list(item.reasons)}
                   for item in held.tables],
        "waiting": [{"pipeline": item.pipeline, "reason": item.reason} for item in held.waiting]}


def build_report(report: Report, window: Any = None, *, out: str | Path | None = None, fmt: str = "html",
                 params: Mapping[str, Any] | None = None, release: str | None = None, live: bool = False,
                 row_cap: int | None = None, timeout_s: float | None = None, now: dt.datetime | None = None,
                 sink: events.EventSink | None = None, clock: Callable[[], dt.datetime] | None = None,
                 home: str | None = None) -> Build:
    """Run a report and write it, and record the build as run events.

    Args:
        report: The report, from :func:`~local_data_platform.report.spec.read_report`.
        window: The window, as :func:`~local_data_platform.report.engine.run_queries` takes it.
        out: Where to write the document, a local path, ``file://``, ``s3://`` or ``gs://``. With ``None`` or ``-``
            nothing is written, and the document is in the result.
        fmt: ``html``, ``md`` or ``json``.
        params: The values of the declared parameters.
        release: Read this release instead of the current one.
        live: Read the tables as they are now.
        row_cap: How many rows a query may return, instead of the ``row_cap`` of the report.
        timeout_s: How many seconds a query may run, instead of the ``timeout_s`` of the report.
        now: The current time, for the window shortcuts.
        sink: Where the events go, instead of the sinks of the report.
        clock: The clock of the build and of its events. The real one by default.
        home: A link to the index of a site, as the page reaches it, which an HTML page shows as ``All reports``.
            :func:`~local_data_platform.report.site.build_site` gives it. Markdown and JSON ignore it.

    Returns:
        The :class:`Build`.

    Raises:
        ReportBuildError: If a query did not run or a block cannot be shown. Nothing was written.
        ReportHeld: If ``metadata.freshness`` says ``block`` and a table that the report read is stale. Nothing was
            written.
        ConfigError: If the format, the window or a parameter is not valid.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    if fmt not in FORMATS:
        raise ConfigError(f"unknown format {fmt!r}. The formats are {list(FORMATS)}")
    tick = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    started = time.perf_counter()
    emitter = events.RunEmitter(sink if sink is not None else report_sink(report), events.uuid7(),
                                context={"pipeline": report.identifier, "report": report.identifier}, clock=tick)
    emitter.emit("run.started", {"task_kind": "report", "format": fmt, "release": release, "live": live,
                                 "spec": report.file.name if report.file else None})
    target = None if out is None or str(out) == STDOUT else str(out)
    stage = "queries"
    try:
        run = run_queries(report, window, params=params, release=release, live=live, row_cap=row_cap,
                          timeout_s=timeout_s, now=now)
        if not run.ok:
            raise ReportBuildError(report.identifier, run.problems)
        stage = "health"
        built_at = tick()
        run.health = collect(report, run.reads, built_at)
        if run.health.held:
            raise ReportHeld(report.identifier, run.health.stale)
        stage = "render"
        document = render(run, fmt, built_at, home)
        data = document.encode("utf-8")
        stage = "write"
        if target is not None:
            write_document(out, data)
    except ReportHeld as held:
        emitter.emit("report.held", held_payload(report, held, fmt=fmt, output=target, window=_window_text(run),
                                                 release=release, live=live))
        emitter.emit("run.finished", {"status": "blocked_quality",
                                      "duration_s": round(time.perf_counter() - started, 3)})
        emitter.flush()
        raise
    except Exception as error:
        emitter.emit("run.failed", {"stage": stage, "error_type": type(error).__name__,
                                    "message": events.redact_text(str(error).splitlines()[0] if str(error) else ""),
                                    "problems": len(error.problems) if isinstance(error, ReportBuildError) else None})
        emitter.emit("run.finished", {"status": "failed", "duration_s": round(time.perf_counter() - started, 3)})
        emitter.flush()
        raise
    digest = hashlib.sha256(data).hexdigest()
    rows = sum(item.rows for item in run.queries.values())
    emitter.emit("report.rendered", {
        "format": fmt, "output": target, "bytes": len(data), "sha256": digest, "window": _window_text(run),
        "timezone": report.timezone, "release": release, "live": live, "queries": len(run.queries), "rows": rows,
        "blocks": len(report.blocks), "stale": [item.table for item in run.health.stale],
        "tables": [{"table": read.table, "release": read.release, "snapshot_id": read.snapshot_id}
                   for read in run.reads.values()]})
    duration = time.perf_counter() - started
    emitter.emit("run.finished", {"status": "succeeded", "duration_s": round(duration, 3), "rows_read": rows})
    emitter.flush()
    return Build(report=report, run=run, format=fmt, document=document, output=target, bytes=len(data),
                 sha256=digest, built_at=built_at, run_id=emitter.run_id, duration_ms=round(duration * 1000, 3),
                 health=run.health)


__all__ = ["Build", "FORMATS", "ReportBuildError", "ReportHeld", "STDOUT", "build_report", "held_payload", "render",
           "report_events", "report_events_of", "report_sink", "write_document"]
