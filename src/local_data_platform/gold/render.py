"""How the gold catalog is shown: as text for ``ldp gold list`` and ``show``, and as a page or Markdown for ``docs``.

The entries come from :mod:`local_data_platform.gold.catalog`. This module only formats them. Text goes to a terminal,
HTML is one self-contained page built on :mod:`local_data_platform.html`, and Markdown is one file for a MkDocs site.
Every value that a person wrote, such as the description of a table, is escaped for the format it goes into, so it can
never become markup.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Sequence
from typing import Any

from local_data_platform import html
from local_data_platform.gold.catalog import Catalog, CatalogEntry

DESCRIPTION_WIDTH = 48
GRAIN_WIDTH = 36
CELL_WIDTH = 40
"""How much of a text is shown in a column of a listing or in a sample row, before ``...``."""

_RAN_WELL = ("published", "succeeded", "finished")
_RAN_BADLY = ("failed", "blocked_quality", "upstream_failed")


# --------------------------------------------------------------------------- small formatters


def clip(value: Any, width: int) -> str:
    """``value`` on one line, cut to ``width`` characters with ``...`` at the end when it is longer."""
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:max(width - 3, 0)] + "..."


def utc(moment: Any) -> str:
    """A moment as ``YYYY-MM-DD HH:MM:SS`` in UTC. Text is returned as it is, and ``None`` is ``-``."""
    if moment is None:
        return "-"
    if isinstance(moment, dt.datetime):
        aware = moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)
        return aware.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return str(moment)


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _value(value: Any) -> str:
    """A cell of a sample row as the terminal and the page show it."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") or "0"
    return clip(value, CELL_WIDTH)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _numeric_columns(rows: Sequence[Sequence[Any]], width: int) -> list[int]:
    """The positions of the columns whose values are all numbers, ignoring the nulls, in rows of ``width`` cells."""
    numeric = []
    for index in range(width):
        values = [row[index] for row in rows if row[index] is not None]
        if values and all(_is_number(value) for value in values):
            numeric.append(index)
    return numeric


def _what(catalog: Catalog) -> str:
    return "table" if catalog.all_layers else "gold table"


def _run_when(entry: CatalogEntry) -> Any:
    return entry.run.get("finished_at") or entry.run.get("started_at") if entry.run else None


def _runs_rows(entry: CatalogEntry) -> list[list[Any]]:
    return [[run["run_id"], run["attempt"], run["status"], utc(run["started_at"]), utc(run["finished_at"]),
             run["snapshot_id"] if run["snapshot_id"] is not None else "-"] for run in entry.runs]


def _release_rows(entry: CatalogEntry) -> list[list[Any]]:
    return [[item.release + (" (current)" if item.current else ""), utc(item.cut_at), item.snapshot_id,
             f"{item.rows:,}", item.status, "yes" if item.at_live else "no"] for item in entry.releases]


RUN_HEADERS = ["Run", "Attempt", "Status", "Started (UTC)", "Finished (UTC)", "Snapshot"]
RELEASE_HEADERS = ["Release", "Cut (UTC)", "Snapshot", "Rows", "Status", "Live is here"]
COLUMN_HEADERS = ["Column", "Type", "Description"]
METRIC_HEADERS = ["Metric", "Description"]


def _facts(entry: CatalogEntry) -> list[tuple[str, str | None]]:
    """The fixed facts of a table: who owns it, what one row is, which key and which config."""
    return [("Owner", entry.owner), ("Grain", entry.grain), ("Primary key", ", ".join(entry.primary_key)),
            ("Refresh", entry.refresh), ("Tags", ", ".join(entry.tags)), ("Config", entry.config)]


# --------------------------------------------------------------------------- text


def _right(values: Sequence[str]) -> list[str]:
    """The values padded on the left to one width, so that a table prints them right-aligned."""
    width = max((len(value) for value in values), default=0)
    return [value.rjust(width) for value in values]


def _list_row(entry: CatalogEntry, rows: str, all_layers: bool) -> dict[str, Any]:
    row: dict[str, Any] = {"table": entry.table}
    if all_layers:
        row["layer"] = entry.layer or "-"
    row["description"] = clip(entry.shown_description, DESCRIPTION_WIDTH)
    row["owner"] = entry.owner or "-"
    row["grain"] = clip(entry.grain, GRAIN_WIDTH) if entry.grain else "-"
    row["rows"] = rows
    row["refreshed_utc"] = utc(entry.refreshed_at) if entry.refreshed_at else "-"
    row["run"] = entry.run_text or ("-" if entry.exists else "not built")
    row["quality"] = entry.quality_text or "-"
    row["release"] = entry.release_text or "-"
    return row


def list_text(catalog: Catalog) -> str:
    """What ``ldp gold list`` prints: one row for each table, and a line for the gold tables that lack a contract."""
    from local_data_platform.cli import format_table

    entries = catalog.entries
    if not entries:
        hint = "" if catalog.all_layers else " Add --all-layers to list the tables of every layer."
        return (f"No {_what(catalog)}s in {catalog.folder}. A gold table has metadata.docs.layer set to gold, or is in "
                f"a namespace called gold.{hint}")
    counts = _right([f"{entry.rows:,}" if entry.rows is not None else "-" for entry in entries])
    rows = [_list_row(entry, count, catalog.all_layers) for entry, count in zip(entries, counts)]
    lines = [f"{_count(len(entries), _what(catalog))} in {catalog.folder}", format_table(rows)]
    if catalog.lacking:
        lines += [f"{catalog.lacking_summary}.", *(f"{entry.lacks_text}." for entry in catalog.lacking)]
    return "\n".join(lines)


def _runs_title(entry: CatalogEntry) -> str:
    return "Last run" if len(entry.runs) == 1 else f"Last {len(entry.runs)} runs"


def _indent(text: str, by: int = 2) -> str:
    return "\n".join(f"{' ' * by}{line}" if line else line for line in text.splitlines())


def _state(entry: CatalogEntry) -> list[tuple[str, str]]:
    """How the table is doing, as ``(label, text)`` for ``show``."""
    if not entry.exists:
        return [("Rows", "the table does not exist yet")]
    last_run = (f"{entry.run_text}, run {entry.run['run_id']}, {utc(_run_when(entry))} UTC"
                if entry.run else "none recorded")
    return [("Rows", f"{entry.rows:,}" if entry.rows is not None else "-"),
            ("Refreshed", f"{utc(entry.refreshed_at)} UTC" if entry.refreshed_at else "-"),
            ("Last run", last_run), ("Quality", entry.quality_text or "none recorded"),
            ("Release", entry.release_text or "in no release")]


def show_text(entry: CatalogEntry) -> str:
    """The description of one table that ``ldp gold show`` prints."""
    from local_data_platform.cli import format_table

    lines = [f"{entry.table}  ({entry.layer or 'no layer'})", f"  {entry.shown_description}"]
    shown = [(label, value) for label, value in _facts(entry) if value] + _state(entry)
    width = max(len(label) for label, _ in shown)
    lines += [f"  {label:<{width}}  {value}" for label, value in shown]
    lines += [f"  {'Missing':<{width}}  {problem}" for problem in entry.problems]
    lines += [f"  {'Note':<{width}}  {note}" for note in entry.notes]
    columns = [{"column": column.name, "type": column.type or "-",
                "description": column.description or "no description"} for column in entry.columns]
    lines += ["", f"Columns ({len(columns)})", _indent(format_table(columns))]
    if entry.metrics:
        metrics = [{"metric": item.get("name"), "description": item.get("description")} for item in entry.metrics]
        lines += ["", "Metrics", _indent(format_table(metrics))]
    if entry.runs:
        runs = [dict(zip(("run_id", "attempt", "status", "started_utc", "finished_utc", "snapshot_id"), row))
                for row in _runs_rows(entry)]
        lines += ["", _runs_title(entry), _indent(format_table(runs))]
    if entry.releases:
        releases = [dict(zip(("release", "cut_utc", "snapshot_id", "rows", "status", "live_is_here"), row))
                    for row in _release_rows(entry)]
        lines += ["", f"Releases that include it ({len(entry.releases)})", _indent(format_table(releases))]
    elif entry.detailed and entry.exists:
        lines += ["", "Releases that include it", "  none"]
    if entry.sample is not None:
        names, rows = entry.sample["columns"], entry.sample["rows"]
        lines += ["", f"Sample ({_count(len(rows), 'row')})"]
        shown = [{name: clip(value, CELL_WIDTH) if isinstance(value, str) else value for name, value in zip(names, row)}
                 for row in rows]
        lines.append(_indent(format_table(shown)) if rows else "  the table has no rows")
    if entry.query:
        lines += ["", "Read it", f"  {entry.query}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTML


def _anchor(identifier: str, taken: set[str]) -> str:
    base = "t-" + (re.sub(r"[^a-z0-9]+", "-", identifier.lower()).strip("-") or "table")
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    taken.add(name)
    return name


def _run_kind(status: str | None) -> str | None:
    if status in _RAN_WELL:
        return "ok"
    if status in _RAN_BADLY:
        return "bad"
    return "warn" if status == "running" else None


def _quality_chip(entry: CatalogEntry) -> html.Raw:
    text = entry.quality_text or ""
    failed = "failed" in text or (entry.quality or {}).get("passed") is False
    kind = "bad" if failed else "ok" if text.endswith("passed") else None
    return html.chip(f"quality {text}", kind)


def _chips(entry: CatalogEntry) -> html.Raw:
    if not entry.exists:
        return html.h("div", html.chip("not built yet", "warn"), class_="chips")
    chips = [html.chip(f"{entry.rows:,} rows" if entry.rows is not None else "rows unknown")]
    if entry.refreshed_at:
        chips.append(html.chip(f"refreshed {utc(entry.refreshed_at)} UTC"))
    chips.append(html.chip(f"last run {entry.run_text}" if entry.run_text else "no run recorded",
                           _run_kind(entry.run_text)))
    if entry.quality_text:
        chips.append(_quality_chip(entry))
    release_kind = "warn" if entry.release_state == "behind" else "ok" if entry.release else None
    chips.append(html.chip(f"release {entry.release_text}" if entry.release_text else "in no release", release_kind))
    return html.h("div", chips, class_="chips")


def _columns_section(entry: CatalogEntry) -> html.Raw:
    rows = [[html.h("code", column.name), column.type or "-", column.description or "no description"]
            for column in entry.columns]
    return html.details(f"Columns ({len(rows)})", html.table(COLUMN_HEADERS, rows), open=True)


def _sample_section(entry: CatalogEntry) -> html.Raw | None:
    sample = entry.sample
    if not sample or not sample["rows"]:
        return None
    rows = sample["rows"]
    table = html.table(sample["columns"], [[_value(value) for value in row] for row in rows],
                       right=_numeric_columns(rows, len(sample["columns"])))
    return html.details(f"Sample ({_count(len(rows), 'row')})", table)


def _extra_sections(entry: CatalogEntry) -> list[Any]:
    sections: list[Any] = []
    if entry.metrics:
        rows = [[item.get("name"), item.get("description")] for item in entry.metrics]
        sections.append(html.details("Metrics", html.table(METRIC_HEADERS, rows)))
    if entry.runs:
        sections.append(html.details(_runs_title(entry),
                                     html.table(RUN_HEADERS, [[html.h("code", row[0]), *row[1:]]
                                                              for row in _runs_rows(entry)], right=[1, 5])))
    if entry.releases:
        sections.append(html.details(f"Releases that include it ({len(entry.releases)})",
                                     html.table(RELEASE_HEADERS, _release_rows(entry), right=[2, 3])))
    sections.append(_sample_section(entry))
    return sections


def _card(entry: CatalogEntry, anchor: str) -> html.Raw:
    config = html.h("code", entry.config) if entry.config else None
    facts = [(label, config if label == "Config" else value) for label, value in _facts(entry)]
    parts: list[Any] = [
        html.h("h2", html.h("code", entry.table), " ", html.chip(entry.layer or "no layer")),
        html.h("p", entry.shown_description, class_=None if entry.description else "muted"),
        html.facts(facts), _chips(entry)]
    if entry.problems:
        parts.append(html.h("div", [html.chip(problem, "warn") for problem in entry.problems], class_="chips"))
    parts += [html.h("p", note, class_="muted") for note in entry.notes]
    parts.append(_columns_section(entry))
    parts += _extra_sections(entry)
    if entry.query:
        parts += [html.h("p", "Read it with", class_="muted"), html.h("pre", html.h("code", entry.query))]
    return html.h("section", *parts, id=anchor, class_="card", data_search=entry.document().lower())


def html_page(catalog: Catalog) -> str:
    """The catalog as one self-contained page, with a search box. It asks for nothing from the network."""
    taken: set[str] = set()
    cards = [_card(entry, _anchor(entry.table, taken)) for entry in catalog.entries]
    what = _what(catalog)
    summary = f"{_count(len(catalog.entries), what)} in {catalog.folder}, made {utc(catalog.generated_at)} UTC"
    body: list[Any] = [html.search_box("Search the tables", "Search tables, owners and columns")]
    lacking = catalog.lacking
    if lacking:
        body.append(html.h("p", html.chip(catalog.lacking_summary, "warn"), " ",
                           ", ".join(entry.table for entry in lacking), class_="muted"))
    body += cards or [html.h("p", f"There is no {what} here yet.", class_="muted")]
    return html.page("Gold catalog" if not catalog.all_layers else "Catalog", *body, subtitle=summary,
                     description="What each table means, who owns it and how it is doing.",
                     script=html.FILTER_SCRIPT, footer="Made by ldp gold docs.")


# --------------------------------------------------------------------------- Markdown


_MARKDOWN_SPECIALS = re.compile(r"([\\`*_\[\]|])")


def md(value: Any) -> str:
    """Text for a Markdown paragraph or a table cell: on one line, with markup and Markdown syntax made harmless.

    ``&``, ``<`` and ``>`` become entities so that no HTML gets through, and the characters that Markdown reads, such as
    ``*``, ``_``, ``[``, ``]``, a backtick and ``|``, are escaped with a backslash.
    """
    text = " ".join(str(value).split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIALS.sub(r"\\\1", text)


def _row(cells: Iterable[Any]) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _grid(headers: Sequence[str], rows: Iterable[Sequence[Any]], right: Iterable[int] = ()) -> list[str]:
    aligned = set(right)
    return [_row(headers), _row("---:" if index in aligned else "---" for index in range(len(headers))),
            *(_row(row) for row in rows)]


def _md_facts(entry: CatalogEntry) -> list[str]:
    facts = [("Layer", entry.layer), *_facts(entry)]
    lines = [f"- **{label}** {f'`{value}`' if label == 'Config' else md(value)}" for label, value in facts if value]
    if not entry.exists:
        return lines + ["- **Not built yet**"]
    lines.append(f"- **Rows** {entry.rows:,}" if entry.rows is not None else "- **Rows** unknown")
    if entry.refreshed_at:
        lines.append(f"- **Refreshed** {utc(entry.refreshed_at)} UTC")
    lines += [f"- **Last run** {md(entry.run_text or 'none recorded')}",
              f"- **Quality** {md(entry.quality_text or 'none recorded')}",
              f"- **Release** {md(entry.release_text or 'in no release')}"]
    return lines


def _md_sections(entry: CatalogEntry) -> list[str]:
    columns = [[f"`{column.name}`", md(column.type or "-"), md(column.description or "no description")]
               for column in entry.columns]
    lines = ["", f"### Columns ({len(columns)})", "", *_grid(COLUMN_HEADERS, columns)]
    if entry.metrics:
        rows = [[md(item.get("name")), md(item.get("description"))] for item in entry.metrics]
        lines += ["", "### Metrics", "", *_grid(METRIC_HEADERS, rows)]
    if entry.runs:
        rows = [[f"`{md(row[0])}`", row[1], md(row[2]), *row[3:]] for row in _runs_rows(entry)]
        lines += ["", f"### {_runs_title(entry)}", "", *_grid(RUN_HEADERS, rows, right=[1, 5])]
    if entry.releases:
        rows = [[md(row[0]), row[1], row[2], row[3], md(row[4]), row[5]] for row in _release_rows(entry)]
        lines += ["", f"### Releases that include it ({len(entry.releases)})", "",
                  *_grid(RELEASE_HEADERS, rows, right=[2, 3])]
    if entry.sample and entry.sample["rows"]:
        rows = entry.sample["rows"]
        lines += ["", f"### Sample ({_count(len(rows), 'row')})", "",
                  *_grid([md(name) for name in entry.sample["columns"]],
                         [[md(_value(value)) for value in row] for row in rows],
                         right=_numeric_columns(rows, len(entry.sample["columns"])))]
    if entry.query:
        lines += ["", "Read it with", "", "```", entry.query.replace("```", "'''"), "```"]
    return lines


def _md_overview(catalog: Catalog, anchors: dict[str, str]) -> list[str]:
    rows = [[f"[`{entry.table}`](#{anchors[entry.table]})", md(entry.shown_description), md(entry.owner or "-"),
             md(entry.grain or "-"), f"{entry.rows:,}" if entry.rows is not None else "-",
             utc(entry.refreshed_at) if entry.refreshed_at else "-", md(entry.run_text or "-"),
             md(entry.quality_text or "-"), md(entry.release_text or "-")] for entry in catalog.entries]
    headers = ["Table", "Description", "Owner", "Grain", "Rows", "Refreshed (UTC)", "Last run", "Quality", "Release"]
    return _grid(headers, rows, right=[4])


def markdown_page(catalog: Catalog) -> str:
    """The catalog as one Markdown file for a MkDocs site, with a table of all the tables and a section for each."""
    taken: set[str] = set()
    anchors = {entry.table: _anchor(entry.table, taken) for entry in catalog.entries}
    folder = catalog.folder.replace("`", "")
    lines = ["# Gold catalog" if not catalog.all_layers else "# Catalog", "",
             f"{_count(len(catalog.entries), _what(catalog))} in `{folder}`, made {utc(catalog.generated_at)} UTC "
             "by `ldp gold docs`.", ""]
    lacking = catalog.lacking
    if lacking:
        names = ", ".join(f"`{entry.table}`" for entry in lacking)
        lines += [f"**{catalog.lacking_summary}**, {names}.", ""]
    if not catalog.entries:
        return "\n".join(lines + [f"There is no {_what(catalog)} here yet.", ""])
    lines += [*_md_overview(catalog, anchors), ""]
    for entry in catalog.entries:
        lines += [f'<a id="{anchors[entry.table]}"></a>', "", f"## `{entry.table}`", "",
                  md(entry.shown_description), "", *_md_facts(entry)]
        lines += [f"- **Missing** {md(problem)}" for problem in entry.problems]
        lines += [f"- **Note** {md(note)}" for note in entry.notes]
        lines += [*_md_sections(entry), ""]
    return "\n".join(lines)


__all__ = ["clip", "html_page", "list_text", "markdown_page", "md", "show_text", "utc"]
