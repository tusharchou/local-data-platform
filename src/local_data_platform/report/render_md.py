"""A report as Markdown, for a wiki, a pull request or a chat that shows Markdown and no pictures.

The Markdown has the same blocks in the same order as the page. A key figure is a bullet, a chart is a table of its
values under its title, a table is a table, and a text is paragraphs. Markup in a value is made harmless as in the page
of ``ldp gold docs``, with ``&``, ``<`` and ``>`` as entities and a backslash before the characters that Markdown reads.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from local_data_platform.report.formats import utc_text
from local_data_platform.report.health import BANNER_WORDS, Health, cells, columns
from local_data_platform.report.render_html import read_text, window_text
from local_data_platform.report.view import Chart, Grid, Kpi, Words

if TYPE_CHECKING:
    from local_data_platform.report.engine import ReportRun

_SPECIALS = re.compile(r"([\\`*_\[\]|])")


def md(value: Any) -> str:
    """Text for a paragraph or a cell: on one line, with markup and Markdown syntax made harmless."""
    text = " ".join(str(value).split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _SPECIALS.sub(r"\\\1", text)


def code(text: Any) -> str:
    """Text as a code span, in which the characters of Markdown are not read, so nothing is escaped."""
    return "`" + " ".join(str(text).split()).replace("`", "'") + "`"


def _row(cells: Iterable[Any]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _grid(headers: Sequence[str], rows: Iterable[Sequence[str]], right: Iterable[int] = ()) -> list[str]:
    aligned = set(right)
    lines = [_row(md(header) for header in headers),
             _row("---:" if place in aligned else "---" for place in range(len(headers)))]
    lines += [_row(md(cell) for cell in row) for row in rows]
    return lines


def _notes(notes: Iterable[str]) -> list[str]:
    return [f"*{md(note)}*" for note in notes]


def _kpis(tiles: list[Kpi]) -> list[str]:
    lines = []
    for tile in tiles:
        text = f"- **{md(tile.title)}** {md(tile.value)}"
        if tile.change is not None:
            text += f" ({md(tile.change)} from {md(tile.compare)})"
        elif tile.compare is not None:
            text += f" (was {md(tile.compare)})"
        lines.append(text)
        lines += [f"  - *{md(note)}*" for note in tile.notes]
    return lines


def _stale(health: Health) -> list[str]:
    """The lines of the banner of a report that shows stale data with a warning."""
    return [f"> **Stale data.** {BANNER_WORDS}", ">",
            *[f"> - {code(item.table)} {md(' and '.join(item.reasons))}" for item in health.stale]]


def _health(health: Health) -> list[list[str]]:
    """The blocks of the data health panel. A table is named as code and every other value is made harmless."""
    blocks = [["## Data health"], ["*One row for each table that this report read, measured when it was built.*"]]
    if not health.tables:
        return blocks + [[md("This report read no table, so there is no data to check.")]]
    headers = columns(health)
    lines = [_row(md(header) for header in headers),
             _row("---:" if place == 3 else "---" for place in range(len(headers)))]
    for item in health.tables:
        texts = cells(item, health)
        lines.append(_row([code(item.table), *[md(text) for text in texts[1:]]]))
    return blocks + [lines]


def render_md(run: ReportRun, *, built_at: dt.datetime | None = None) -> str:
    """The Markdown of a run.

    Args:
        run: A run whose blocks were resolved.
        built_at: The moment of the build. The clock by default.
    """
    report = run.report
    moment = built_at if built_at is not None else dt.datetime.now(dt.timezone.utc)
    blocks: list[list[str]] = [[f"# {md(report.title)}"]]
    if report.what:
        blocks.append([md(report.what)])
    when = window_text(run)
    if when:
        blocks.append([f"**Window** {md(when)}"])
    if run.health is not None and run.health.banner:
        blocks.append(_stale(run.health))
    tiles: list[Kpi] = []

    def flush() -> None:
        if tiles:
            blocks.append(_kpis(tiles))
            tiles.clear()

    for view in run.views:
        if isinstance(view, Kpi):
            tiles.append(view)
            continue
        flush()
        if isinstance(view, Chart):
            blocks.append([f"## {md(view.title)}"])
            blocks.append(_grid(view.headers, view.table, range(1, len(view.headers))))
            blocks += [[note] for note in _notes(view.notes)]
        elif isinstance(view, Grid):
            if view.title:
                blocks.append([f"## {md(view.title)}"])
            blocks.append(_grid(view.headers, view.rows, view.numeric))
            blocks += [[note] for note in _notes(view.notes)]
        elif isinstance(view, Words):
            if view.title:
                blocks.append([f"## {md(view.title)}"])
            blocks += [[md(paragraph)] for paragraph in view.paragraphs]
    flush()
    if run.health is not None:
        blocks += _health(run.health)
    reads = [f"  - {code(read.table)} {md(read_text(read))}" for read in run.reads.values()] or ["  - none"]
    blocks.append(["## How this report was made", "", f"- **Window** {md(when or 'none')}",
                   f"- **Time zone** {md(report.timezone)}", "- **Tables read**", *reads,
                   f"- **Built** {md(utc_text(moment))}"])
    return "\n\n".join("\n".join(block) for block in blocks) + "\n"


__all__ = ["code", "md", "render_md"]
