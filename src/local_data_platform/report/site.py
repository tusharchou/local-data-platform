"""A folder of reports built into one site: a page for each report, and an index that links the latest of each.

:func:`build_site` is what ``ldp report build FOLDER --out SITE`` does. It reads every report file of the folder, builds
each one as :func:`~local_data_platform.report.build.build_report` builds a page, and writes::

    SITE/index.html                          one card for each report
    SITE/<identifier>/<window start>.html    the page of this build
    SITE/<identifier>/latest.html            the same page, which the card links to

The window start is the date in the time zone of the report when the window starts at midnight there, such as
``2026-07-31``, and the date with the time when it does not, such as ``2026-07-31T0530``. A report that has no window is
named for the date of the build. A build of a window that is already there writes its page again, a build of another
window adds a page, and nothing is ever deleted.

Every link is relative, so the folder opens from ``file://``, from a shared drive, from a bucket and from any static
host. ``out`` can be a path, a ``file://``, ``s3://`` or ``gs://`` address, and each file is written whole or not at
all. Each page has a link back to the index.

A report that cannot be built does not stop the others. It is a card on the index that says why, and the pages of its
earlier builds stay in the folder, but the index does not link to them, because they are not the latest. A report that
the freshness gate holds back is listed the same way. :func:`build_site` raises only for what is wrong with the whole
run, which is a window that is malformed, a folder that has no report, two reports that have the same identifier, or an
``out`` that is a file, and then nothing is written.

The badge of a card is ``fresh`` when the report has ``metadata.freshness`` and none of its tables is stale, ``stale``
when it has one and a table is, ``not checked`` when it has none, ``held`` when the gate held it back, and ``failed``
when it could not be built.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform import events, fs, html
from local_data_platform.exceptions import ConfigError, EngineNotFound, LDPError
from local_data_platform.logger import get_logger
from local_data_platform.orchestration.windows import get_timezone
from local_data_platform.paths import resolve_path
from local_data_platform.report.build import Build, ReportBuildError, build_report, write_document
from local_data_platform.report.engine import report_window
from local_data_platform.report.formats import utc_text
from local_data_platform.report.health import ReportHeld
from local_data_platform.report.render_html import window_label
from local_data_platform.report.spec import Report, load_report, report_files
from local_data_platform.spec import parse_window

logger = get_logger(__name__)

INDEX = "index.html"
"""The page that lists the reports."""

LATEST = "latest.html"
"""The page of the latest build of a report, which is the same page as the one that is named for its window."""

HOME = "../index.html"
"""How a page of a report reaches the index, because the page is one folder below it."""

TITLE = "Reports"

NO_WINDOW = "none"
"""What a card says for a report that has no window."""

BUILT = "built"
HELD = "held"
FAILED = "failed"
"""What became of a report: its page was written, the freshness gate held it back, or it could not be built."""

FRESH = "fresh"
STALE = "stale"
NOT_CHECKED = "not checked"
BADGES = (FRESH, STALE, NOT_CHECKED, HELD, FAILED)
"""The badges of a card, in the order that the index counts them."""

_KINDS = {FRESH: "ok", STALE: "warn", HELD: "bad", FAILED: "bad"}
"""The colour of each badge. A report that is not checked has the neutral one."""

_FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True)
class Entry:
    """What one report of the folder came to.

    Attributes:
        file: The report file.
        status: ``built``, ``held`` or ``failed``.
        title: What the report is called, or the name of its file when the file could not be read.
        moment: When the page was built, or when the build was tried, as a moment with a time zone.
        identifier: The identifier of the report, or ``None`` when the file could not be read.
        what: What the report is for, or ``None``.
        window: The window as the page says it, ``none`` for a report that has no window, and ``None`` when the report
            did not get as far as having one.
        gated: Whether the report has ``metadata.freshness``, so that a verdict about its data means something.
        stale: The stale tables in one line, or ``None`` when there is none.
        page: The link from the index to the latest page, which only a built report has.
        error: Why the report was not built, as the lines that the command prints, or ``None``.
        build: The build, which only a built report has.
    """

    file: Path
    status: str
    title: str
    moment: dt.datetime
    identifier: str | None = None
    what: str | None = None
    window: str | None = None
    gated: bool = False
    stale: str | None = None
    page: str | None = None
    error: str | None = None
    build: Build | None = None

    @property
    def label(self) -> str:
        """The badge of the card, one of :data:`BADGES`."""
        if self.status != BUILT:
            return self.status
        if not self.gated:
            return NOT_CHECKED
        return STALE if self.stale else FRESH


@dataclass(frozen=True)
class Site:
    """A site that was built.

    Attributes:
        out: Where the site was written, as it was given.
        index: Where the index was written.
        document: The text of the index.
        built_at: The moment of the index.
        entries: One for each report file of the folder, in the order of the file names.
    """

    out: str
    index: str
    document: str
    built_at: dt.datetime
    entries: tuple[Entry, ...]

    def of(self, status: str) -> tuple[Entry, ...]:
        """The entries that ended with this status."""
        return tuple(entry for entry in self.entries if entry.status == status)

    @property
    def built(self) -> tuple[Entry, ...]:
        return self.of(BUILT)

    @property
    def held(self) -> tuple[Entry, ...]:
        return self.of(HELD)

    @property
    def failed(self) -> tuple[Entry, ...]:
        return self.of(FAILED)


def folder_problem(identifier: str) -> str | None:
    """Why an identifier cannot be the name of the folder of a report in a site, or ``None`` when it can.

    The folder is the identifier, so it has to be a name that is safe on every file system and in every link: up to 128
    letters, digits, ``.``, ``_`` and ``-``, starting with a letter or a digit, with no ``..``, and not ``index.html``,
    which is the index.
    """
    if _FOLDER.fullmatch(identifier) and ".." not in identifier and identifier.lower() != INDEX:
        return None
    return (f"the identifier {identifier!r} cannot be the name of a folder of a site. Use up to 128 letters, digits, "
            f"'.', '_' and '-', start with a letter or a digit, and do not use '..' or {INDEX}")


def page_name(window: tuple[dt.datetime, dt.datetime] | None, timezone: str, now: dt.datetime) -> str:
    """The name of the page of a build: the start of its window in the time zone of the report, or else the day.

    A window that starts at midnight there is named for the date, such as ``2026-07-31.html``, and one that starts at
    another time has the time too, such as ``2026-07-31T0530.html``, and the seconds when it has some. A report that has
    no window is named for the date of ``now``.
    """
    start = (window[0] if window is not None else now).astimezone(get_timezone(timezone))
    name = start.strftime("%Y-%m-%d")
    if window is not None and start.time() != dt.time():
        name += start.strftime("T%H%M") + (start.strftime("%S") if start.second else "")
    return f"{name}.html"


def join_location(base: str | Path, *parts: str) -> str:
    """A place inside ``base``, which is a folder as a path or as a ``file://``, ``s3://`` or ``gs://`` address."""
    return "/".join([str(base).rstrip("/"), *parts])


def _clean(text: str) -> str:
    """Text for a page and a terminal, with whatever looks like a secret masked, because a page has more readers."""
    return events.redact_text(text, None)


def _describe(report: Report, error: Exception) -> str:
    """Why a report was not built, as lines that start by saying which report."""
    if isinstance(error, ReportBuildError):
        return str(error)
    reason = str(error) if isinstance(error, LDPError) else f"{type(error).__name__}: {error}"
    return f"the report {report.identifier} was not built. {reason}"


def _check_out(out: str | Path) -> None:
    location = fs.resolve_location(out)
    if isinstance(location, Path) and location.is_file():
        raise ConfigError(f"{out} is a file, and a site is written to a folder")


def _refuse_duplicates(reports: Sequence[tuple[Path, Report]]) -> None:
    """Refuse two reports that would write the same folder of the site, whatever the case of their identifiers."""
    owners: dict[str, list[tuple[Path, Report]]] = {}
    for file, report in reports:
        owners.setdefault(report.identifier.lower(), []).append((file, report))
    clashes = [group for group in owners.values() if len(group) > 1]
    if clashes:
        lines = [f"  - {', '.join(f'{file.name} ({report.identifier})' for file, report in group)}"
                 for group in clashes]
        raise ConfigError("\n".join(["reports that have the same identifier would write the same folder of a site, "
                                     "so give each its own:", *lines]))


def _build_one(report: Report, file: Path, out: str | Path, window: Any, now: dt.datetime,
               clock: Callable[[], dt.datetime], sink: events.EventSink | None, options: Mapping[str, Any]) -> Entry:
    """Build one report into the site, and say what came of it. Nothing but a missing engine is raised."""
    base = {"file": file, "title": report.title, "identifier": report.identifier, "what": report.what,
            "gated": report.freshness is not None}
    problem = folder_problem(report.identifier)
    if problem is not None:
        return Entry(status=FAILED, moment=now, error=_clean(f"{file.name}: {problem}"), **base)
    text = None
    try:
        bounds = report_window(report, window, now=now)
        text = window_label(bounds, report.timezone) or NO_WINDOW
        built = build_report(report, window, out=join_location(out, report.identifier,
                                                               page_name(bounds, report.timezone, now)),
                             fmt="html", now=now, sink=sink, clock=clock, home=HOME, **options)
        write_document(join_location(out, report.identifier, LATEST), built.document.encode("utf-8"))
    except ReportHeld as held:
        return Entry(status=HELD, moment=now, window=text, stale=held.summary(), error=_clean(str(held)), **base)
    except EngineNotFound:
        raise  # nothing can be built without it, and a card for every report would say so again and again
    except Exception as error:  # noqa: BLE001 - a report that fails is a card, and the others still build
        logger.debug("The report %s was not built", report.identifier, exc_info=True)
        return Entry(status=FAILED, moment=now, window=text, error=_clean(_describe(report, error)), **base)
    return Entry(status=BUILT, moment=built.built_at, window=text, stale=built.health.summary() or None,
                 page=f"{report.identifier}/{LATEST}", build=built, **base)


def _card(entry: Entry, number: int) -> html.Raw:
    heading = f"report-{number}-h"
    title = html.h("a", entry.title, href=entry.page) if entry.page else entry.title
    facts = html.facts([("Window", entry.window),
                        ("Built" if entry.status == BUILT else "Tried", utc_text(entry.moment))])
    badge = html.chip(entry.label, _KINDS.get(entry.label))
    return html.h("section", html.join(
        html.h("h2", title, id=heading), html.h("p", badge, class_="chips"),
        html.h("p", entry.what, class_="sub") if entry.what else None, facts,
        html.h("p", f"{entry.stale}.", class_="note") if entry.status == BUILT and entry.stale else None,
        html.h("pre", entry.error) if entry.error else None), class_="card", aria_labelledby=heading)


def render_index(entries: Sequence[Entry], built_at: dt.datetime) -> str:
    """The index page: one card for each report, in the order given, under a count of each badge.

    A card has the title, which links to the latest page of a report that was built, what the report is for, the window,
    when it was built or when the build was tried, and the badge. A stale report says which tables are stale, and a
    report that was held or failed says why. Like every page of the shell it loads nothing and escapes every value.
    """
    counts = Counter(entry.label for entry in entries)
    chips = [html.chip(f"{counts[badge]} {badge}", _KINDS.get(badge)) for badge in BADGES if counts[badge]]
    cards = [_card(entry, number) for number, entry in enumerate(entries, 1)]
    return html.page(TITLE, html.h("p", chips, class_="chips"), html.h("div", html.join(*cards), class_="reports"),
                     subtitle=f"{len(entries)} report{'' if len(entries) == 1 else 's'}",
                     description="The latest build of each report of a folder, with the health of its data.",
                     footer=f"Built {utc_text(built_at)} by ldp report build.")


def build_site(folder: str | Path, out: str | Path, window: Any = None, *, params: Mapping[str, Any] | None = None,
               release: str | None = None, live: bool = False, row_cap: int | None = None,
               timeout_s: float | None = None, now: dt.datetime | None = None,
               sink: events.EventSink | None = None, clock: Callable[[], dt.datetime] | None = None) -> Site:
    """Build every report of a folder into a site.

    Each report is built as :func:`~local_data_platform.report.build.build_report` builds one, with the same window,
    parameters, release and limits, so the options of ``ldp report check`` mean the same here. A parameter that a report
    does not declare is a problem of that report, as it is for ``ldp report check``. The index is written last.

    Args:
        folder: The folder that has the report files. A file whose kind is not ``Report`` is left alone.
        out: The folder of the site, a path, a ``file://``, ``s3://`` or ``gs://`` address. It is made when it is not
            there.
        window: The window of every report, as :func:`~local_data_platform.report.engine.run_queries` takes it. A
            shortcut such as ``yesterday`` is counted in the time zone of each report.
        params: The values of the declared parameters.
        release: Read this release instead of the current one.
        live: Read the tables as they are now.
        row_cap: How many rows a query may return, instead of the ``row_cap`` of the report.
        timeout_s: How many seconds a query may run, instead of the ``timeout_s`` of the report.
        now: The current time, for the window shortcuts and for the name of a report that has no window. The clock by
            default.
        sink: Where the events of the builds go, instead of the sinks of each report.
        clock: The clock of the builds and of the index. The real one by default.

    Returns:
        The :class:`Site`, which says what became of each report.

    Raises:
        ConfigError: If ``folder`` is not a folder or has no report, the window is malformed, two reports have the same
            identifier, or ``out`` is a file. Nothing was written.
        EngineNotFound: If ``duckdb`` is not installed.
        OSError: If the index cannot be written.
    """
    tick = clock or (lambda: dt.datetime.now(dt.timezone.utc))
    moment = now if now is not None else tick()
    parse_window(window, now=moment)
    if not resolve_path(folder).is_dir():
        raise ConfigError(f"{folder} is not a folder")
    files = report_files(folder)
    if not files:
        raise ConfigError(f"there is no report in {folder}. A report file has \"kind\": \"Report\"")
    _check_out(out)
    loaded: list[tuple[Path, Report | None, str | None]] = []
    for file in files:
        try:
            loaded.append((file, load_report(file), None))
        except ConfigError as error:
            loaded.append((file, None, _clean(str(error))))
    _refuse_duplicates([(file, report) for file, report, _ in loaded if report is not None])
    options = {"params": params, "release": release, "live": live, "row_cap": row_cap, "timeout_s": timeout_s}
    entries = [
        Entry(file=file, status=FAILED, title=file.name, moment=moment, error=problem) if report is None
        else _build_one(report, file, out, window, moment, tick, sink, options)
        for file, report, problem in loaded]
    built_at = tick()
    document = render_index(entries, built_at)
    index = join_location(out, INDEX)
    write_document(index, document.encode("utf-8"))
    return Site(out=str(out), index=index, document=document, built_at=built_at, entries=tuple(entries))


__all__ = ["BADGES", "BUILT", "Entry", "FAILED", "HELD", "HOME", "INDEX", "LATEST", "Site", "build_site",
           "folder_problem", "join_location", "page_name", "render_index"]
