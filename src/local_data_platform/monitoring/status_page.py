"""``ldp status --html``: the rows of the status as one page that needs no server.

The page is built on :mod:`local_data_platform.html`, the shell that the gold catalog uses, so it keeps the same
promises. It is one file with its CSS inline, it has no script, it carries a policy that stops the browser from asking
for anything, and every value is escaped. Its links are anchors inside the page.

It shows the summary table, which is the table of the command line, and for each pipeline the monitors behind its row
and its last 20 runs. The oldest event of the history is in the footer, so that a history that was trimmed shows.

This module needs only the standard library when it is imported.
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from local_data_platform import html
from local_data_platform.monitoring.results import format_moment
from local_data_platform.monitoring.status import COLUMNS, HEALTH, NO_SLA, RECENT_RUNS, Run, StatusRow, age_text

_KINDS = {"ok": "ok", "published": "ok", "skipped_duplicate": "ok", "succeeded": "ok", "warn": "warn",
          "blocked_quality": "warn", "error": "bad", "failed": "bad", "upstream_failed": "bad"}
"""The colour of a chip for each status of a monitor and of a run. The others are neutral."""

_NUMBERS = ("rows", "quality_7d", "open_alerts")


def _chip(status: str) -> Any:
    return html.chip(status, _KINDS.get(status))


def _slugs(names: Iterable[str]) -> list[str]:
    """One anchor for each name, made of letters, digits and ``-``, and different from every other."""
    seen: dict[str, int] = {}
    slugs = []
    for name in names:
        base = "p-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "pipeline")
        seen[base] = seen.get(base, 0) + 1
        slugs.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return slugs


def _cell(column: str, row: StatusRow, slug: str) -> Any:
    value = row.cells()[column]
    if column == "pipeline":
        return html.h("a", value, href=f"#{slug}")
    if column == "last_run" and row.last_run is not None:
        return html.join(_chip(row.last_run.status), age_text(row.last_run.age(row.as_of)), separator=" ")
    if column in ("freshness", "volume", "schema", "metrics") and value not in ("-", NO_SLA):
        return _chip(value)
    return value


def _summary(rows: Sequence[StatusRow], slugs: Sequence[str]) -> Any:
    table = html.table(list(COLUMNS), [[_cell(column, row, slug) for column in COLUMNS]
                                       for row, slug in zip(rows, slugs)],
                       right=[COLUMNS.index(column) for column in _NUMBERS])
    counts = [html.chip(f"{sum(1 for row in rows if row.health == health)} {health}",
                        {"error": "bad", "warn": "warn", "ok": "ok"}.get(health))
              for health in HEALTH if any(row.health == health for row in rows)]
    return html.h("div", html.h("div", counts, class_="chips"), table, class_="card")


def _monitors(row: StatusRow) -> Any:
    if row.sla is None:
        return html.h("p", "This config has no metadata.sla, so no monitor looks at it.", class_="muted")
    return html.table(["monitor", "status", "observed", "expected", "details"],
                      [[result.monitor, _chip(result.status), result.observed or "-", result.expected or "-",
                        result.details or "-"] for result in row.results])


def _runs(row: StatusRow) -> Any:
    if not row.recent_runs:
        return html.h("p", "No run is in the history of this pipeline.", class_="muted")

    def line(run: Run) -> list[Any]:
        return [format_moment(run.started_at, seconds=True), _chip(run.status), run.attempt, run.rows_read,
                run.rows_written, run.checks or "-", run.duration_s, run.snapshot_id, run.run_id]

    return html.table(["started (UTC)", "status", "attempt", "rows read", "rows written", "checks", "seconds",
                       "snapshot", "run"], [line(run) for run in row.recent_runs], right=[2, 3, 4, 6])


def _section(row: StatusRow, slug: str) -> Any:
    since = format_moment(row.history_since, seconds=True) if row.history_since else None
    config = Path(row.file).name if row.file else None
    facts = html.facts([("Table", row.table), ("Owner", row.owner), ("Config", config), ("SLA", row.sla),
                        ("History since", since), ("Problem", row.problem)])
    heading = html.h("h2", row.pipeline, " ", html.chip(NO_SLA) if row.health == NO_SLA else _chip(row.health), id=slug)
    if row.problem is not None:  # a config that could not be read has no monitors and no runs to show
        return html.h("section", heading, facts, class_="card")
    return html.h("section", heading, facts, html.h("h3", "Monitors"), _monitors(row),
                  html.h("h3", f"Last {RECENT_RUNS} runs"), _runs(row), class_="card")


def render(rows: Sequence[StatusRow]) -> str:
    """The page for the rows of :func:`~local_data_platform.monitoring.status.rows`, as text from the doctype on.

    Args:
        rows: The rows, in the order that they are to be shown.
    """
    slugs = _slugs(row.pipeline for row in rows)
    moments = [row.history_since for row in rows if row.history_since is not None]
    since = format_moment(min(moments), seconds=True) if moments else None
    footer = f"History goes back to {since}." if since else "No run or monitor history was found."
    as_of = format_moment(rows[0].as_of, seconds=True) if rows else None
    body = [_summary(rows, slugs), *[_section(row, slug) for row, slug in zip(rows, slugs)]] if rows else []
    count = f"{len(rows)} pipeline{'' if len(rows) == 1 else 's'}"
    return html.page("Pipeline status", *body, subtitle=f"{count} at {as_of}" if rows else "No pipeline",
                     description="The health of every pipeline in one folder, from the monitors of its SLA",
                     footer=footer)


__all__ = [
    "render",
]
