"""Sending a report to the destinations of ``metadata.delivery``: a Slack channel, a webhook and a file.

:func:`send_report` is what ``ldp report send`` does. It runs the report for a window as ``ldp report build`` does, and
sends the result to each destination, through the one notifier of :mod:`local_data_platform.notify`, so a destination
gets the retries, the https rule and the redaction that an alert gets.

* ``slack`` posts a message that has Block Kit blocks to an incoming webhook. It has the title, the window, each key
  figure with its change, the first table as a block of fixed width text, the data health, and a link to the page when
  a ``file`` destination has a ``public_url``. Each section is under the 3000 characters that Slack allows, and the
  message says when rows or key figures were left out. What the report says is escaped, so a value such as
  ``<!channel>`` is shown and does not ping anybody.
* ``webhook`` posts the JSON of ``ldp report build --format json`` with ``Content-Type: application/json``, and the
  headers that the environment variable of ``headers_env`` holds.
* ``file`` writes the HTML page of the report to ``path``, which may be an ``s3://`` or ``gs://`` address. A file is
  delivered before the others, so that the link in the Slack message is to a page that is there.

The address of a ``slack`` or a ``webhook`` destination is in the environment variable that the file names, and it is
never in an event, a log, an error or the output of ``--dry-run``. A plain ``http://`` address is only taken for
127.0.0.1 and localhost.

A delivery has an idempotency key, the hash of the identifier of the report, the window and the name of the destination.
Before anything runs, :func:`send_report` looks for a ``report.delivered`` event with that key in the sinks of the
report. A destination that has one is skipped, with a ``report.skipped_duplicate`` event, and a send in which every
destination is skipped does not run the report at all. ``force`` sends again and records a new event, and ``dry_run``
sends and records nothing. A report that has no sink in ``metadata.observability`` cannot remember a delivery, so it is
sent again each time, and the log says so.

A send is a run of its own. It emits ``run.started``, a ``report.delivered`` or ``report.skipped_duplicate`` for each
destination, a ``run.failed`` for each one that did not arrive, and ``run.finished``. When the freshness gate holds the
report back, nothing is sent and the send emits ``report.held`` and raises
:class:`~local_data_platform.report.health.ReportHeld`, which ``ldp report send`` turns into exit status 3. So does
a pipeline of ``metadata.after`` that has no published run for the window, see :mod:`local_data_platform.report.after`,
which is checked before any query runs. A destination that was delivered already is never held, so a window that was
sent is not asked about its pipelines again.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from local_data_platform import events, notify
from local_data_platform.exceptions import ConfigError
from local_data_platform.logger import get_logger
from local_data_platform.report.after import waiting_for
from local_data_platform.report.build import (
    ReportBuildError,
    build_report,
    held_payload,
    render,
    report_events_of,
    report_sink,
)
from local_data_platform.report.engine import report_window
from local_data_platform.report.health import ReportHeld
from local_data_platform.report.render_html import window_label
from local_data_platform.report.spec import Destination, Report
from local_data_platform.report.view import Grid, Kpi
from local_data_platform.spec import format_instant

if TYPE_CHECKING:
    from local_data_platform.report.engine import ReportRun
    from local_data_platform.report.health import Health

logger = get_logger(__name__)

DELIVERED = "delivered"
SKIPPED = "skipped"
FAILED = "failed"
WOULD_SEND = "would_send"
"""What became of a destination: it got the report, it had got it already, it did not get it, or, in a dry run, it
would get it."""

MAX_KPIS = 20
"""How many key figures a Slack message shows. The others are in the report, and the message says how many."""

MAX_COLUMNS = 12
MAX_CELL = 24
MAX_TITLE = 100
"""How many columns of the table, how many characters of a cell and how many of a title a Slack message shows."""

NOTE_ROOM = 200
"""The characters that a table section keeps free for the line that says what it left out."""


@dataclass(frozen=True)
class Outcome:
    """What became of one destination.

    Attributes:
        destination: The name of the destination.
        type: ``slack``, ``webhook`` or ``file``.
        status: One of :data:`DELIVERED`, :data:`SKIPPED`, :data:`FAILED` and :data:`WOULD_SEND`.
        key: The idempotency key of the delivery.
        attempts: How many times the post was made, 0 when nothing was sent.
        reason: Why it did not arrive, for a failure. It never holds an address.
        delivered_at: When it was delivered before, for a skipped destination.
        payload: What a dry run would send, as text, for a Slack message and a webhook.
        detail: Where a dry run would send it, with no address, such as ``webhook_env SLACK_URL, which is set``.
    """

    destination: str
    type: str
    status: str
    key: str
    attempts: int = 0
    reason: str = ""
    delivered_at: dt.datetime | None = None
    payload: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class Sent:
    """What a send did.

    Attributes:
        report: The report.
        window: The window as the two UTC instants that the events give, ``START/END``.
        run_id: The id of the send among the run events.
        dry_run: Whether nothing was sent and nothing was recorded.
        outcomes: One for each destination, in the order that they were handled, which is the files first.
    """

    report: Report
    window: str
    run_id: str
    dry_run: bool
    outcomes: tuple[Outcome, ...]

    def of(self, status: str) -> tuple[Outcome, ...]:
        """The outcomes that have this status."""
        return tuple(item for item in self.outcomes if item.status == status)

    @property
    def delivered(self) -> tuple[Outcome, ...]:
        return self.of(DELIVERED)

    @property
    def skipped(self) -> tuple[Outcome, ...]:
        return self.of(SKIPPED)

    @property
    def failed(self) -> tuple[Outcome, ...]:
        return self.of(FAILED)


# --------------------------------------------------------------------------- the destinations and their keys


def delivery_key(identifier: str, window: tuple[dt.datetime, dt.datetime], destination: str) -> str:
    """The idempotency key of a delivery, which is 64 lower case hex characters.

    It is the hash of the canonical JSON of the version of the format, the kind of key, the identifier of the report,
    the start and the end of the window as UTC instants, and the name of the destination, so one report sent to one
    destination for one window has one key, whatever the time zone or the day that it is sent.
    """
    start, end = format_instant(window[0]), format_instant(window[1])
    text = json.dumps(["ldp/v1", "report.delivery", identifier, start, end, destination], separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def channel_of(destination: Destination) -> notify.Channel:
    """The channel of the notifier that a destination is sent through. A ``slack`` or ``webhook`` destination wants
    https, and a ``retries`` of the destination is how often a post is sent again."""
    options = destination.options
    if destination.type == "file":
        return notify.Channel(kind="file", name=destination.name, path=options.get("path"))
    retries = options.get("retries", notify.RETRIES)
    if destination.type == "slack":
        return notify.Channel(kind="slack", name=destination.name, url_env=options.get("webhook_env"),
                              retries=retries, https_only=True)
    return notify.Channel(kind="webhook", name=destination.name, url_env=options.get("url_env"),
                          headers_env=options.get("headers_env"), retries=retries, https_only=True)


def ordered(destinations: Sequence[Destination]) -> list[Destination]:
    """The destinations with the files first and the others in the order of the file."""
    return sorted(destinations, key=lambda item: item.type != "file")


def delivered_keys(report: Report) -> dict[str, events.RunEvent]:
    """The deliveries that were made, by idempotency key, each as the latest ``report.delivered`` event of the key.

    They are read from the sinks of the report, which is the jsonl file or the ``_ldp.runs`` table, and nothing is
    created while looking. A report that has no sink has none.
    """
    found, _ = report_events_of(report)
    latest: dict[str, events.RunEvent] = {}
    for event in found:
        key = event.payload.get("idempotency_key")
        if event.type != "report.delivered" or not isinstance(key, str):
            continue
        if key not in latest or (event.ts, event.seq) >= (latest[key].ts, latest[key].seq):
            latest[key] = event
    return latest


# --------------------------------------------------------------------------- the Slack message


def _kpi_field(view: Kpi) -> str:
    """A key figure as the text of a field, which is its title in bold, its value and how it changed. A value, a change
    and a comparison are numbers that a format wrote, so they are short, and only a title can be long."""
    lines = [f"*{notify.escape_mrkdwn(notify.cut(view.title, MAX_TITLE))}*", notify.escape_mrkdwn(view.value)]
    if view.change is not None:
        lines.append(notify.escape_mrkdwn(f"{view.change} from {view.compare}"))
    elif view.compare is not None:
        lines.append(notify.escape_mrkdwn(f"was {view.compare}"))
    return "\n".join(lines)


def _grid_text(grid: Grid) -> str:
    """The text of the section that has the first table of a report.

    It is the title of the table, its rows as a block of fixed width text that Slack shows as code, and a line that
    says how many rows and columns were left out. A row is left out when it does not fit in the 3000 characters of a
    section, which the headings, the cut cells and the limit of columns make room for, so the section is always
    under the limit.
    """
    columns = min(len(grid.headers), MAX_COLUMNS)
    numeric = set(grid.numeric)

    def clean(value: Any) -> str:
        return notify.cut(" ".join(str(value).replace("`", "'").split()), MAX_CELL)

    head = [clean(item) for item in grid.headers[:columns]]
    body = [[clean(cell) for cell in row[:columns]] for row in grid.rows]
    widths = [max([len(head[place])] + [len(row[place]) for row in body]) for place in range(columns)]

    def line(cells: Sequence[str]) -> str:
        shown = (cell.rjust(widths[place]) if place in numeric else cell.ljust(widths[place])
                 for place, cell in enumerate(cells))
        return notify.escape_mrkdwn("  ".join(shown).rstrip())

    title = f"*{notify.escape_mrkdwn(notify.cut(grid.title.replace('`', chr(39)), MAX_TITLE))}*\n" if grid.title else ""
    lines = [line(head), "  ".join("-" * width for width in widths)]
    budget = notify.SLACK_MAX_SECTION - len(title) - len("```\n\n```") - NOTE_ROOM
    used = sum(len(item) + 1 for item in lines)
    shown_rows = 0
    for row in body:
        text = line(row)
        if used + len(text) + 1 > budget:
            break
        lines.append(text)
        used += len(text) + 1
        shown_rows += 1
    if not body:
        lines.append("(no rows)")
    notes = []
    if shown_rows < grid.total_rows:
        notes.append(f"Showing the first {shown_rows:,} of {grid.total_rows:,} rows.")
    if columns < len(grid.headers):
        left = len(grid.headers) - columns
        notes.append(f"{left:,} more column{'' if left == 1 else 's'} {'is' if left == 1 else 'are'} in the report.")
    return title + "```\n" + "\n".join(lines) + "\n```" + ("\n" + " ".join(notes) if notes else "")


def _health_text(health: Health | None) -> str:
    """The data health of a report in one line, as Slack text."""
    if health is None or not health.tables:
        return "Data health: this report read no table."
    count = len(health.tables)
    noun = "table" if count == 1 else "tables"
    if health.rule is None:
        body = f"{count} {noun} read, and the report has no freshness rule to check them against."
    elif health.stale:
        body = health.summary()
    else:
        body = f"{count} {noun} read, and none is stale."
    return "Data health: " + notify.escape_mrkdwn(notify.cut(body, 500))


def slack_payload(run: ReportRun, *, link: str | None = None) -> dict[str, Any]:
    """The body of the Slack message for a run of a report.

    Args:
        run: A run whose blocks were resolved and whose health was collected.
        link: The address of the page of the report, which is added as a link, or ``None``.

    Returns:
        ``{"text": ..., "blocks": [...]}``, which :func:`local_data_platform.notify.slack_message` has checked against
        the limits of Slack.
    """
    report = run.report
    when = window_label(run.window, report.timezone)
    blocks = [notify.header_block(report.title), notify.context_block(f"Window {when}" if when else "No window")]
    kpis = [view for view in run.views if isinstance(view, Kpi)]
    shown = kpis[:MAX_KPIS]
    for place in range(0, len(shown), notify.SLACK_MAX_FIELDS):
        blocks.append(notify.section_block(fields=[_kpi_field(view) for view in shown[place:place + 10]]))
    if len(kpis) > len(shown):
        left = len(kpis) - len(shown)
        blocks.append(notify.context_block(f"{left:,} more key figure{'' if left == 1 else 's'} "
                                           f"{'is' if left == 1 else 'are'} in the report."))
    grid = next((view for view in run.views if isinstance(view, Grid)), None)
    if grid is not None:
        blocks.append(notify.section_block(_grid_text(grid)))
    blocks.append(notify.context_block(_health_text(run.health)))
    if link:
        blocks.append(notify.section_block(f"<{notify.escape_mrkdwn(link).replace('|', '%7C')}|Open the report>"))
    text = notify.cut(notify.escape_mrkdwn(f"{report.title}, {when}" if when else report.title), 300)
    return notify.slack_message(text, blocks)


# --------------------------------------------------------------------------- sending


def _where(channel: notify.Channel) -> str:
    """Where a dry run would send, as the name of the environment variable and whether it is set, never the value."""
    if channel.kind == "file":
        return f"the file {channel.path}"
    value = os.environ.get(channel.url_env or "")
    if not value:
        return f"{channel.url_env}, which is not set"
    problem = notify.url_problem(value)
    return f"{channel.url_env}, which is set" + (f", but {problem}" if problem else "")


def _link(destinations: Sequence[Destination], outcomes: Sequence[Outcome]) -> str | None:
    """The public address of the page, which is the ``public_url`` of the first file destination that has one and whose
    page is there, because it was delivered now, was delivered before, or would be delivered in a dry run."""
    there = {item.destination for item in outcomes if item.status in (DELIVERED, SKIPPED, WOULD_SEND)}
    return next((item.options["public_url"] for item in destinations
                 if item.type == "file" and item.name in there and item.options.get("public_url")), None)


def _payload(item: Destination, built: Any, page: bytes, link: str | None) -> tuple[bytes, str | None]:
    """The bytes that a destination gets, and the text that a dry run shows of them, which is ``None`` for a page."""
    if item.type == "slack":
        body = slack_payload(built.run, link=link)
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), json.dumps(body, indent=2, ensure_ascii=False)
    if item.type == "webhook":
        return built.document.encode("utf-8"), built.document.rstrip("\n")
    return page, None


def send_report(report: Report, window: Any, *, params: Mapping[str, Any] | None = None, release: str | None = None,
                live: bool = False, row_cap: int | None = None, timeout_s: float | None = None,
                dry_run: bool = False, force: bool = False, now: dt.datetime | None = None,
                sink: events.EventSink | None = None, clock: Callable[[], dt.datetime] | None = None) -> Sent:
    """Send a report to the destinations of its ``metadata.delivery`` for a window.

    Args:
        report: The report, which has ``metadata.delivery``.
        window: The window, in any form :func:`~local_data_platform.report.engine.run_queries` takes. It is needed,
            because a delivery is made once for each window.
        params: The values of the declared parameters.
        release: Read this release instead of the current one.
        live: Read the tables as they are now.
        row_cap: How many rows a query may return, instead of the ``row_cap`` of the report.
        timeout_s: How many seconds a query may run, instead of the ``timeout_s`` of the report.
        dry_run: Work out what would be sent, and send and record nothing.
        force: Send to every destination, whether the report was delivered to it for this window or not.
        now: The current time, for the window shortcuts.
        sink: Where the events go, instead of the sinks of the report. The deliveries that were made are still read from
            the sinks of the report.
        clock: The clock of the send and of its events. The real one by default.

    Returns:
        What became of each destination, in the order that the destinations are handled, which is the files first. A
        destination that did not get the report is ``failed`` and says why, and the others were sent as usual.

    Raises:
        ConfigError: If the report has no ``metadata.delivery``, no window is given, or the window or a parameter is
            not valid.
        ReportBuildError: If a query did not run or a block cannot be shown. Nothing was sent.
        ReportHeld: If a pipeline of ``metadata.after`` has no published run for the window, or ``metadata.freshness``
            says ``block`` and a table is stale. Nothing was sent.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    if not report.delivery:
        raise ConfigError(f"the report {report.identifier} has no metadata.delivery, so there is nowhere to send it")
    tick = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    moment = now if now is not None else tick()
    bounds = report_window(report, window, now=moment)
    if bounds is None:
        raise ConfigError("ldp report send needs a window, because a report is delivered once for each window and "
                          "the window is part of the key of the delivery")
    span = f"{format_instant(bounds[0])}/{format_instant(bounds[1])}"
    destinations = ordered(report.delivery)
    channels = {item.name: channel_of(item) for item in destinations}
    keys = {item.name: delivery_key(report.identifier, bounds, item.name) for item in destinations}
    target = events.NullSink() if dry_run else (sink if sink is not None else report_sink(report))
    if not dry_run and isinstance(target, events.NullSink):
        logger.warning("The report %s has no sink in metadata.observability, so this delivery is not recorded, and "
                       "sending it again sends it again", report.identifier)
    earlier = {} if force else delivered_keys(report)
    emitter = events.RunEmitter(target, events.uuid7(), clock=tick,
                                context={"pipeline": report.identifier, "report": report.identifier})
    names = ", ".join(item.name for item in destinations)
    started = time.perf_counter()
    emitter.emit("run.started", {"task_kind": "report_send", "window": span, "output": names, "force": force,
                                 "destinations": [item.name for item in destinations], "release": release,
                                 "live": live, "spec": report.file.name if report.file else None})
    outcomes: list[Outcome] = []
    pending: list[Destination] = []
    for item in destinations:
        before = earlier.get(keys[item.name])
        if before is None:
            pending.append(item)
            continue
        outcomes.append(Outcome(item.name, item.type, SKIPPED, keys[item.name], delivered_at=before.ts))
        emitter.emit("report.skipped_duplicate", {
            "destination": item.name, "type": item.type, "idempotency_key": keys[item.name], "window": span,
            "delivered_at": format_instant(before.ts), "delivered_run_id": before.run_id})
    stage = "queries"
    try:
        if pending:
            stage = "after"
            waiting = waiting_for(report, bounds)
            if waiting:
                raise ReportHeld(report.identifier, waiting=waiting, window=span)
            stage = "queries"
            built = build_report(report, window, out=None, fmt="json", params=params, release=release, live=live,
                                 row_cap=row_cap, timeout_s=timeout_s, now=moment, sink=events.NullSink(),
                                 clock=tick)
            stage = "payload"
            page = (render(built.run, "html", built.built_at).encode("utf-8")
                    if any(item.type == "file" for item in pending) else b"")
            for item in pending:
                channel, key, link = channels[item.name], keys[item.name], _link(destinations, outcomes)
                try:
                    data, shown = _payload(item, built, page, link)
                except Exception as error:  # noqa: BLE001 - a message that cannot be made is that destination's failure
                    logger.debug("The payload for %s could not be made", item.name, exc_info=True)
                    reason = f"its payload could not be made ({type(error).__name__})"
                    outcomes.append(Outcome(item.name, item.type, FAILED, key, reason=reason))
                    emitter.emit("run.failed", {
                        "stage": "payload", "destination": item.name, "type": item.type, "idempotency_key": key,
                        "error_type": type(error).__name__, "message": events.redact_text(str(error), 300)})
                    continue
                if dry_run:
                    outcomes.append(Outcome(item.name, item.type, WOULD_SEND, key, payload=shown,
                                            detail=f"{len(data):,} bytes to {_where(channel)}"))
                    continue
                delivery = (notify.write_file(channel.label, item.options["path"], data, report.base_dir)
                            if item.type == "file" else notify.post(channel, data))
                if not delivery.ok:
                    reason = events.redact_text(delivery.reason)
                    outcomes.append(Outcome(item.name, item.type, FAILED, key, attempts=delivery.attempts,
                                            reason=reason))
                    emitter.emit("run.failed", {
                        "stage": "deliver", "destination": item.name, "type": item.type, "idempotency_key": key,
                        "error_type": "DeliveryError", "message": reason, "attempts": delivery.attempts})
                    continue
                outcomes.append(Outcome(item.name, item.type, DELIVERED, key, attempts=delivery.attempts))
                emitter.emit("report.delivered", {
                    "destination": item.name, "type": item.type, "idempotency_key": key, "window": span,
                    "timezone": report.timezone, "attempts": delivery.attempts, "bytes": len(data),
                    "release": release, "live": live, "link": link if item.type == "slack" else None,
                    "tables": [{"table": read.table, "release": read.release, "snapshot_id": read.snapshot_id}
                               for read in built.run.reads.values()]})
    except ReportHeld as held:
        emitter.emit("report.held", held_payload(report, held, fmt="send", output=names, window=span,
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
    place = {item.name: number for number, item in enumerate(destinations)}
    outcomes.sort(key=lambda item: place[item.destination])
    sent = Sent(report=report, window=span, run_id=emitter.run_id, dry_run=dry_run, outcomes=tuple(outcomes))
    status = "failed" if sent.failed else "succeeded" if sent.delivered else "skipped_duplicate"
    emitter.emit("run.finished", {"status": status, "duration_s": round(time.perf_counter() - started, 3),
                                  "delivered": len(sent.delivered), "skipped": len(sent.skipped),
                                  "failed": len(sent.failed)})
    emitter.flush()
    return sent


__all__ = ["DELIVERED", "FAILED", "MAX_COLUMNS", "MAX_KPIS", "Outcome", "SKIPPED", "Sent", "WOULD_SEND",
           "channel_of", "delivered_keys", "delivery_key", "ordered", "send_report", "slack_payload"]
