"""A report as one self-contained HTML page, on the page shell of :mod:`local_data_platform.html`.

The page has the title of the report, the window and the time zone, each block in the order of the file, and a footer
that says how the report was made. A run of key figures is a row of tiles. A chart is an inline SVG from
:mod:`local_data_platform.report.svg` with its legend, what it left out, and a table that repeats every value for a
reader who cannot see the picture. A table shows its first rows and says how many it left out, and a text is paragraphs.
The footer lists each table that the report read with the release and the snapshot that it read, the window, the time
zone and the moment the page was built.

It is the one shell, so it keeps the promises of the shell. The page loads nothing, the browser is told to refuse to, it
reads in the light and the dark theme and at the width of a phone, and every value is escaped. Two builds of a report
over the same snapshots are the same text, except for the line that has the moment of the build, which is a ``<dd>``
that holds ``YYYY-MM-DD HH:MM:SS UTC`` and is the last fact of the footer.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from local_data_platform import html
from local_data_platform.orchestration.windows import get_timezone
from local_data_platform.report.formats import MOMENT_FORMAT, utc_text
from local_data_platform.report.health import banner, panel
from local_data_platform.report.svg import chart_svg, legend
from local_data_platform.report.view import Chart, Grid, Kpi, Words

if TYPE_CHECKING:
    from local_data_platform.report.engine import ReportRun, TableRead

BUILT_FORMAT = MOMENT_FORMAT
"""How the moment of the build is written, always in UTC, as the last fact of the footer."""


def window_label(window: tuple[dt.datetime, dt.datetime] | None, timezone: str) -> str | None:
    """A window of two UTC instants as two wall-clock times in a time zone, or ``None`` when there is no window."""
    if window is None:
        return None
    zone = get_timezone(timezone)
    start, end = (moment.astimezone(zone).strftime("%Y-%m-%d %H:%M") for moment in window)
    return f"{start} to {end}, {timezone}"


def window_text(run: ReportRun) -> str | None:
    """The window of a run as two wall-clock times in the time zone of the report, or ``None`` when there is none."""
    return window_label(run.window, run.report.timezone)


def read_text(read: TableRead) -> str:
    """Which version of a table a report read, with its snapshot, as a person reads it."""
    snapshot = f", snapshot {read.snapshot_id}" if read.snapshot_id is not None else ", no snapshot yet"
    return f"{read.source}{snapshot}"


def _notes(notes) -> list[html.Raw]:
    return [html.h("p", note, class_="note") for note in notes]


def _kpi(view: Kpi) -> html.Raw:
    delta = None
    if view.change is not None:
        delta = html.h("div", f"{view.change} from {view.compare}", class_=f"delta {view.direction}")
    elif view.compare is not None:
        delta = html.h("div", f"was {view.compare}", class_="delta")
    return html.h("div", html.join(html.h("div", view.title, class_="label"), html.h("div", view.value, class_="value"),
                                   delta, *_notes(view.notes)), class_="card kpi")


def _chart(view: Chart, number: int) -> html.Raw:
    uid = f"chart-{number}"
    data = html.table(view.headers, view.table, right=range(1, len(view.headers)))
    return html.h("section", html.join(
        html.h("h2", view.title, id=f"{uid}-h"), legend([item.name for item in view.series]) if len(view.series) > 1
        else None, chart_svg(view, uid), *_notes(view.notes), html.details("Data table", data)),
        class_="card", aria_labelledby=f"{uid}-h")


def _grid(view: Grid, number: int) -> html.Raw:
    label = f"table-{number}"
    table = html.table(view.headers, view.rows, right=view.numeric)
    heading = html.h("h2", view.title, id=f"{label}-h") if view.title else None
    return html.h("section", html.join(heading, table, *_notes(view.notes)), class_="card",
                  aria_labelledby=f"{label}-h" if view.title else None,
                  aria_label=None if view.title else f"The rows of {view.result}")


def _words(view: Words, number: int) -> html.Raw:
    heading = f"text-{number}-h"
    return html.h("section", html.join(html.h("h2", view.title, id=heading) if view.title else None,
                                       *[html.h("p", paragraph) for paragraph in view.paragraphs]),
                  class_="card words", aria_labelledby=heading if view.title else None)


def _body(views) -> list[html.Raw]:
    out: list[html.Raw] = []
    tiles: list[html.Raw] = []
    for view in views:
        if isinstance(view, Kpi):
            tiles.append(_kpi(view))
            continue
        if tiles:
            out.append(html.h("div", html.join(*tiles), class_="kpis"))
            tiles = []
        number = view.block.index
        out.append(_chart(view, number) if isinstance(view, Chart) else _grid(view, number) if isinstance(view, Grid)
                   else _words(view, number))
    if tiles:
        out.append(html.h("div", html.join(*tiles), class_="kpis"))
    return out


def _footer(run: ReportRun, built_at: dt.datetime) -> html.Raw:
    reads = [html.h("li", html.h("code", read.table), " ", read_text(read)) for read in run.reads.values()]
    facts = [("Window", window_text(run) or "none"), ("Time zone", run.report.timezone),
             ("Tables read", html.h("ul", html.join(*reads)) if reads else "none"), ("Built", utc_text(built_at))]
    lines = [html.h(tag, value) for name, value in facts for tag, value in (("dt", name), ("dd", value))]
    return html.h("dl", html.Raw("\n"), html.join(*lines), html.Raw("\n"), class_="facts")


def render_html(run: ReportRun, *, built_at: dt.datetime | None = None, home: str | None = None) -> str:
    """The page of a run, as text that starts with the doctype.

    Args:
        run: A run whose blocks were resolved, which is what ``run_queries`` gives when it has no problem.
        built_at: The moment of the build. The clock by default.
        home: A link to the index of the site that the page belongs to, as the page reaches it, such as
            ``../index.html``. The page then starts with a link to it called ``All reports``.
    """
    report = run.report
    moment = built_at if built_at is not None else dt.datetime.now(dt.timezone.utc)
    when = window_text(run)
    back = html.h("p", html.h("a", "All reports", href=home)) if home else None
    chips = html.h("p", html.chip(f"Window {when}"), class_="chips") if when else None
    stale = banner(run.health) if run.health is not None else None
    health = panel(run.health) if run.health is not None else None
    return html.page(report.title, back, chips, stale, *_body(run.views), health, subtitle=report.what or "",
                     description=report.what or f"The report {report.identifier}.", footer=_footer(run, moment))


__all__ = ["BUILT_FORMAT", "read_text", "render_html", "utc_text", "window_label", "window_text"]
