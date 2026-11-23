"""Building a report: the page, the Markdown and the JSON, the file that is written and the events of the build.

The writers are tested on a run made by hand, so every number on a page is one that the test wrote down, and the pages
are compared with golden files in ``tests/golden/report/``. After a change that is meant to alter a page, rewrite the
golden files with ``LDP_UPDATE_GOLDEN=1 pytest tests/test_report_build.py`` and read the difference in git before
committing. What belongs to the page shell and not to the report, which is its stylesheet, the hash of it and the
version of this package, is replaced by a word before the comparison, so a change of the shell does not churn them.

``build_report`` and ``ldp report build`` are tested on a gold table of five orders with a release, the same lake as the
tests of the engine, and everything runs offline. The one test of ``s3://`` uses an in-process moto server.
"""

import datetime as dt
import difflib
import hashlib
import json
import logging
import os
import re
import uuid
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import __version__, events, html
from local_data_platform.cli import main
from local_data_platform.events import MemorySink, load_run_events, summarize_runs
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.gold.build import build_gold
from local_data_platform.report.build import (
    FORMATS,
    STDOUT,
    ReportBuildError,
    build_report,
    render,
    report_events,
    report_sink,
    write_document,
)
from local_data_platform.report import build as report_build
from local_data_platform.report.engine import QueryRun, TableRead
from local_data_platform.report.render_html import render_html, utc_text, window_text
from local_data_platform.report.render_json import DOCUMENT_VERSION, document, render_json
from local_data_platform.report.render_md import code, md, render_md
from local_data_platform.report.spec import build as build_spec
from local_data_platform.report.spec import load_report
from tests.report_support import BUILT_AT, UTC, WINDOW_UTC, decimals, golden_run, make_run

GOLDEN = Path(__file__).parent / "golden" / "report"
UPDATE_GOLDEN = os.environ.get("LDP_UPDATE_GOLDEN") == "1"
HOSTILE = "<script>alert(1)</script> \"quoted\" & 'single' <img src=http://evil.example/x onerror=alert(1)>"


class Parsed(HTMLParser):
    """The start tags of a page with their attributes, the text of its elements, and the text of its style."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.headings: list[str] = []
        self.style = ""
        self._open: list[str] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))
        self._open.append(tag)

    def handle_endtag(self, tag):
        if self._open and self._open[-1] == tag:
            self._open.pop()

    def handle_data(self, data):
        if self._open and self._open[-1] == "style":
            self.style += data
        elif self._open and self._open[-1] == "h2":
            self.headings.append(data)
            self.text.append(data)
        elif not self._open or self._open[-1] != "script":
            self.text.append(data)

    def names(self) -> list[str]:
        return [tag for tag, _ in self.tags]

    def values(self, attribute: str) -> list[str]:
        return [str(attrs[attribute]) for _, attrs in self.tags if attribute in attrs]

    def words(self) -> str:
        return " ".join(" ".join(self.text).split())


def settled(text: str, fmt: str) -> str:
    """A page with its stylesheet, the hash of it and the version of the package replaced by a word."""
    if fmt != "html":
        return text
    text = re.sub(r"<style>.*?</style>", "<style>CSS</style>", text, flags=re.S)
    text = re.sub(r"&#x27;sha256-[A-Za-z0-9+/=]+&#x27;", "&#x27;sha256-CSS&#x27;", text)
    return text.replace(f"local-data-platform {__version__}", "local-data-platform VERSION")


def page_of(run=None, built_at=BUILT_AT) -> str:
    return render_html(run or golden_run(), built_at=built_at)


# --------------------------------------------------------------------------- golden files


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_report_is_written_as_its_golden_file_says(fmt):
    text = settled(render(golden_run(), fmt, BUILT_AT), fmt)
    path = GOLDEN / f"daily.{fmt}"
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    assert path.is_file(), f"{path} is missing. Write it with LDP_UPDATE_GOLDEN=1 pytest tests/test_report_build.py"
    expected = path.read_bytes().decode("utf-8").replace("\r\n", "\n")  # a checkout may have made the line ends CRLF
    if text != expected:
        diff = "\n".join(list(difflib.unified_diff(expected.splitlines(), text.splitlines(), "golden", "built",
                                                   lineterm="", n=1))[:60])
        pytest.fail(f"the {fmt} of the report is not what {path.name} says. If the change is meant, rewrite the golden "
                    f"files with LDP_UPDATE_GOLDEN=1.\n{diff}")


def test_the_golden_files_are_plain_ascii_with_no_hidden_character_and_end_in_one_newline():
    for path in sorted(GOLDEN.iterdir()):
        data = path.read_bytes().replace(b"\r\n", b"\n")  # a checkout may have made the line ends CRLF
        assert data.isascii() and b"\r" not in data and b"\t" not in data, path.name
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), path.name


def test_the_golden_run_has_one_block_of_every_kind_and_nothing_wrong_with_it():
    run = golden_run()

    assert run.ok and [type(view).__name__ for view in run.views] == ["Kpi", "Kpi", "Kpi", "Chart", "Chart", "Grid",
                                                                      "Words"]


# --------------------------------------------------------------------------- the page


def test_the_page_is_one_document_on_the_page_shell_and_says_how_the_report_was_made():
    text = page_of()
    parsed = Parsed(text)

    assert text.startswith("<!doctype html>\n") and text.count("<!doctype") == 1 and text.count("<html") == 1
    assert parsed.names().count("style") == 1 and parsed.names().count("main") == 1 and parsed.names().count("h1") == 1
    assert "<h1>Daily sales</h1>" in text and 'class="sub">Orders and revenue of one day, from the gold tables.' in text
    assert 'name="description" content="Orders and revenue of one day, from the gold tables."' in text
    assert "<title>Daily sales</title>" in text


def test_the_page_has_its_blocks_in_the_order_of_the_file_and_a_row_of_tiles_for_key_figures():
    parsed = Parsed(page_of())

    layout = [(tag, attrs.get("class")) for tag, attrs in parsed.tags if tag in ("div", "section") and
              attrs.get("class") in ("kpis", "card kpi", "card", "card words")]

    assert layout == [("div", "kpis"), ("div", "card kpi"), ("div", "card kpi"), ("div", "card kpi"),
                      ("section", "card"), ("section", "card"), ("section", "card"), ("section", "card words"),
                      ("section", "card")]  # the last is the data health panel
    assert parsed.headings == ["Revenue by city", "Revenue by day", "Top cuisines", "Note", "Data health"]


def test_the_key_figures_that_follow_each_other_are_one_row_and_a_block_between_them_makes_two():
    rows = pa.table({"v": [5], "c": [4]})
    blocks = [{"type": "kpi", "query": "q", "value": "v"}, {"type": "kpi", "query": "q", "value": "c"},
              {"type": "text", "text": "Between."}, {"type": "kpi", "query": "q", "value": "v", "compare": "c"}]

    text = page_of(make_run(blocks, {"q": rows}))

    assert text.count('class="kpis"') == 2 and text.count('class="card kpi"') == 3
    assert text.index("Between.") > text.index('class="kpis"') and text.rindex('class="kpis"') > text.index("Between.")


def test_a_key_figure_shows_its_change_with_a_direction_and_what_it_was():
    text = page_of()

    assert '<div class="delta up">+12.2% from $1,100.00</div>' in text
    assert '<div class="delta down">-3.3% from 90</div>' in text
    assert text.count('class="delta') == 2  # the third figure has nothing to compare with
    zero = make_run([{"type": "kpi", "query": "q", "value": "v", "compare": "c"}],
                    {"q": pa.table({"v": [5], "c": [0]})})
    assert '<div class="delta">was 0</div>' in page_of(zero)


def test_a_chart_has_its_drawing_a_legend_only_for_several_series_and_a_table_of_every_value():
    parsed = Parsed(page_of())

    assert parsed.names().count("svg") == 2 and parsed.names().count("details") == 2
    # the legend of the line chart, the stale tables of the banner, and the tables read in the footer
    assert parsed.names().count("ul") == 3
    legend = [attrs for tag, attrs in parsed.tags if attrs.get("class") == "legend"]
    assert len(legend) == 1
    sections = [attrs["aria-labelledby"] for tag, attrs in parsed.tags if tag == "section"]
    assert sections == ["chart-3-h", "chart-4-h", "table-5-h", "text-6-h", "health-h"]
    text = page_of()
    for value in ("$520.25", "$410.00", "$304.25", "$480.00", "$350.50"):
        assert f'<td class="num">{value}</td>' in text
    assert text.count("<summary>Data table</summary>") == 2 and "<details open" not in text


def test_a_table_shows_its_first_rows_and_says_how_many_it_left_out():
    text = page_of()

    assert '<p class="note">The table shows the first 3 of 5 rows. 2 rows are left out.</p>' in text
    assert "Greek" not in text and "Mexican" not in text and "Indian" in text


def test_a_chart_of_more_than_500_rows_draws_500_and_says_how_many_it_left_out():
    rows = pa.table({"n": list(range(600)), "v": [float(n % 9) for n in range(600)]})

    text = page_of(make_run([{"type": "line", "query": "q", "x": "n", "y": "v"}], {"q": rows}))

    assert "The chart draws the first 500 of 600 rows. 100 rows are left out." in text
    assert text.count("<tr>") == 501  # the heading row and 500 rows of the table of values


def test_the_footer_says_which_version_of_every_table_was_read_and_for_which_window_and_when_it_was_built():
    footer = page_of().split("<footer")[1]

    assert "<dt>Window</dt>\n<dd>2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata</dd>" in footer
    assert "<dt>Time zone</dt>\n<dd>Asia/Kolkata</dd>" in footer
    assert ("<code>gold.gold_sales</code> release r3, cut 2026-08-02 06:00:00 UTC, snapshot 8420615207115790441"
            in footer)
    assert ("<code>gold.gold_customers</code> the tables as they are now, because release r3 does not hold "
            "gold.gold_customers, snapshot 3187645520913349012") in footer
    assert footer.count("<li>") == 2
    assert footer.index("Window") < footer.index("Time zone") < footer.index("Tables read") < footer.index("Built")
    assert "<dt>Built</dt>\n<dd>2026-08-02 06:30:15 UTC</dd>\n</dl>" in footer


def test_a_table_that_has_no_snapshot_yet_says_so_in_the_footer():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})},
                   reads=(TableRead("gold.gold_new", None, None, "the tables as they are now"),))

    assert "<code>gold.gold_new</code> the tables as they are now, no snapshot yet" in page_of(run)


def test_a_snapshot_numbered_0_is_still_a_snapshot_and_a_run_that_read_no_table_says_none():
    block, rows = [{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})}

    zero = make_run(block, rows, reads=(TableRead("gold.gold_zero", None, 0, "the tables as they are now"),))
    nothing = make_run(block, rows, reads=())

    assert "<code>gold.gold_zero</code> the tables as they are now, snapshot 0</li>" in page_of(zero)
    assert "<dt>Tables read</dt>\n<dd>none</dd>" in page_of(nothing) and "<ul>" not in page_of(nothing)


def test_a_report_that_does_not_say_what_it_is_for_is_described_by_its_identifier():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})}, what=None)

    text = page_of(run)

    assert '<meta name="description" content="The report daily_sales.">' in text and 'class="sub"' not in text


def test_a_run_with_no_window_says_none_and_has_no_chip_for_it():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})}, window=None)

    text = page_of(run)

    assert "<dd>none</dd>" in text and 'class="chip"' not in text and window_text(run) is None


def test_the_window_is_written_as_wall_clock_times_of_the_time_zone_of_the_report():
    run = golden_run()

    assert window_text(run) == "2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata"
    utc = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})}, window=WINDOW_UTC)
    assert window_text(utc) == "2026-07-31 00:00 to 2026-08-01 00:00, UTC"
    assert utc_text(dt.datetime(2026, 8, 2, 12, 0, 0, 999999, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
                    ) == "2026-08-02 06:30:00 UTC"


def test_the_page_loads_nothing_and_the_browser_is_told_to_refuse_to():
    text = page_of()
    parsed = Parsed(text)

    assert not set(parsed.names()) & {"link", "script", "img", "iframe", "object", "embed", "form", "a", "base",
                                      "audio", "video", "source", "input"}
    assert not [name for tag, attrs in parsed.tags for name in attrs if name in ("href", "src", "srcset", "action",
                                                                                 "data", "poster", "style")]
    assert not re.search(r"https?:|ftp:|//[a-z]", text) and "url(" not in parsed.style and "@import" not in parsed.style
    policy = next(attrs["content"] for _, attrs in parsed.tags if attrs.get("http-equiv") == "Content-Security-Policy")
    assert "default-src 'none'" in policy and "script-src" not in policy and "connect-src" not in policy
    assert f"style-src {html._hash(parsed.style)}" in policy


def test_a_link_in_a_text_is_text_and_the_page_does_not_load_or_link_it():
    block = {"type": "text", "text": "See https://example.com/docs and http://example.org for the definitions."}

    text = page_of(make_run([block], {"q": pa.table({"v": [1]})}))
    parsed = Parsed(text)

    assert "See https://example.com/docs and http://example.org for the definitions." in parsed.words()
    outside = [value for _, attrs in parsed.tags for value in attrs.values() if value and "http" in value]
    assert outside == [] and "http" not in parsed.style and "<a " not in text


def test_the_page_reads_in_both_themes_and_at_the_width_of_a_phone():
    parsed = Parsed(page_of())

    assert "width=device-width" in " ".join(parsed.values("content"))
    assert "prefers-color-scheme:dark" in parsed.style
    assert re.search(r"svg\.chart\{[^}]*width:100%;[^}]*height:auto", parsed.style)
    assert re.search(r"\.kpis\{[^}]*repeat\(auto-fit,minmax\(12rem,1fr\)\)", parsed.style)
    assert re.search(r"\.scroll\{[^}]*overflow-x:auto", parsed.style)
    assert re.search(r"\.kpis\{[^}]*gap:12px;margin:16px 0", parsed.style)
    assert ".chart text{fill:var(--muted);font:12px var(--font)}" in parsed.style  # a size that a phone can read
    assert ".chart .tick-y{text-anchor:end}" in parsed.style and "text-anchor:middle" in parsed.style
    assert ".chart .bar{fill:var(--c)}" in parsed.style and ".chart .dot{fill:var(--c);" in parsed.style
    assert ".chart .line{fill:none;stroke:var(--c);" in parsed.style


def test_every_value_that_a_report_has_is_escaped_on_the_page():
    rows = pa.table({"city": [HOSTILE], "k": [HOSTILE], "v": [1.0]})
    blocks = [{"type": "kpi", "title": HOSTILE, "query": "q", "value": "v"},
              {"type": "bar", "title": HOSTILE, "query": "q", "x": "city", "y": "v"},
              {"type": "line", "query": "q", "x": "city", "y": "v", "series": "k"},
              {"type": "table", "title": HOSTILE, "query": "q"},
              {"type": "text", "title": HOSTILE, "text": HOSTILE + " {q.city}"}]
    run = make_run(blocks, {"q": rows}, title=HOSTILE, what=HOSTILE)

    text = page_of(run)
    parsed = Parsed(text)

    assert "<script" not in text and "<img" not in text
    assert "script" not in parsed.names() and "img" not in parsed.names()
    assert parsed.words().count(HOSTILE) >= 8
    assert parsed.values("src") == [] and parsed.values("href") == []
    assert not [name for _, attrs in parsed.tags for name in attrs if name.startswith("on")]


def test_the_ids_of_a_page_are_unique_and_every_label_points_at_an_id_that_exists():
    parsed = Parsed(page_of())

    ids = parsed.values("id")
    labels = [part for value in parsed.values("aria-labelledby") for part in value.split()]

    assert len(ids) == len(set(ids)) and ids
    assert all(label in ids for label in labels)


def test_a_table_without_a_title_is_named_for_a_screen_reader_by_the_query_it_shows():
    run = make_run([{"type": "table", "query": "q"}, {"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})})

    parsed = Parsed(page_of(run))

    assert "The rows of q" in parsed.values("aria-label") and parsed.values("aria-labelledby") == []


def test_the_page_of_two_builds_is_the_same_text_except_for_the_line_that_has_the_time_of_the_build():
    first = render_html(golden_run(), built_at=BUILT_AT)
    later = render_html(golden_run(), built_at=BUILT_AT + dt.timedelta(days=3, seconds=7))

    differing = [(a, b) for a, b in zip(first.splitlines(), later.splitlines()) if a != b]

    assert len(first.splitlines()) == len(later.splitlines())
    assert differing == [("<dd>2026-08-02 06:30:15 UTC</dd>", "<dd>2026-08-05 06:30:22 UTC</dd>")]
    dds = re.findall(r"<dd>(.*?)</dd>", first, flags=re.S)
    assert dds[-1] == "2026-08-02 06:30:15 UTC" and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", dds[-1])


def test_two_renderings_of_the_same_run_have_the_same_bytes_in_every_format():
    for fmt in FORMATS:
        assert render(golden_run(), fmt, BUILT_AT) == render(golden_run(), fmt, BUILT_AT)


def test_the_page_of_a_report_that_was_not_given_a_time_says_when_it_was_built_now():
    text = render_html(golden_run())

    built = re.findall(r"<dd>(.*?)</dd>", text, flags=re.S)[-1]
    moment = dt.datetime.strptime(built, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
    assert abs((dt.datetime.now(UTC) - moment).total_seconds()) < 120


# --------------------------------------------------------------------------- Markdown


def test_markdown_has_the_blocks_of_the_page_in_the_same_order():
    text = render_md(golden_run(), built_at=BUILT_AT)

    order = [text.index(item) for item in ("# Daily sales", "- **Revenue**", "## Revenue by city", "## Revenue by day",
                                           "## Top cuisines", "## Note", "## How this report was made")]
    assert order == sorted(order) and text.endswith("\n") and not text.endswith("\n\n")
    assert "- **Revenue** $1,234.50 (+12.2% from $1,100.00)" in text and "- **Share of the week** 18.8%" in text
    assert "| city | revenue |\n| --- | ---: |\n| Pune | $520.25 |" in text
    assert "*The table shows the first 3 of 5 rows. 2 rows are left out.*" in text


def test_the_tables_that_were_read_are_code_in_markdown_and_their_underscores_are_left_alone():
    text = render_md(golden_run(), built_at=BUILT_AT)

    assert "  - `gold.gold_sales` release r3, cut 2026-08-02 06:00:00 UTC, snapshot 8420615207115790441" in text
    assert "`gold.gold_customers`" in text and "`gold.gold\\_" not in text
    assert "- **Built** 2026-08-02 06:30:15 UTC" in text


def test_markup_in_a_value_is_made_harmless_in_markdown():
    rows = pa.table({"k": ["a|b <i>x</i> & *y* _z_ [l](u) `c` \\ line\nbreak"], "v": [1]})
    blocks = [{"type": "table", "query": "q"}, {"type": "text", "text": "{q.k}"},
              {"type": "bar", "query": "q", "x": "k", "y": "v"}]

    text = render_md(make_run(blocks, {"q": rows}), built_at=BUILT_AT)

    cell = "a\\|b &lt;i&gt;x&lt;/i&gt; &amp; \\*y\\* \\_z\\_ \\[l\\](u) \\`c\\` \\\\ line break"
    assert f"| {cell} | 1 |" in text and f"\n{cell}\n" in text
    assert "<i>" not in text and "\nbreak" not in text


def test_markdown_escapes_and_code_spans_are_exact():
    assert md("a_b*c`d[e]f|g\\h") == "a\\_b\\*c\\`d\\[e\\]f\\|g\\\\h"
    assert md("<b> & </b>") == "&lt;b&gt; &amp; &lt;/b&gt;"
    assert md("  two   spaces\n and a line ") == "two spaces and a line"
    assert md(5) == "5" and md(None) == "None"
    assert code("gold.gold_sales") == "`gold.gold_sales`" and code("a`b  c") == "`a'b c`"


def test_markdown_says_a_key_figure_was_when_there_is_no_change_to_state_and_nothing_when_there_is_no_comparison():
    run = make_run([{"type": "kpi", "query": "q", "value": "v", "compare": "c"},
                    {"type": "kpi", "query": "q", "value": "v", "title": "Alone"}],
                   {"q": pa.table({"v": [5], "c": [0]})})

    text = render_md(run, built_at=BUILT_AT)

    assert "- **v** 5 (was 0)\n- **Alone** 5\n" in text


def test_markdown_says_what_a_key_figure_left_out_and_a_table_with_no_title_has_no_heading():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}, {"type": "table", "query": "q"}],
                   {"q": pa.table({"v": [1, 2, 3]})}, truncated=("q",), row_cap=3)

    text = render_md(run, built_at=BUILT_AT)

    assert "- **v** 1\n  - *The query has more rows than the row cap of 3, so this has the first 3.*\n" in text
    assert "None" not in text and text.count("\n## ") == 1  # the only heading is the one of the footer
    assert "| v |\n| ---: |\n| 1 |" in text


def test_markdown_made_without_a_time_says_when_it_was_built_now():
    text = render_md(golden_run())

    built = re.search(r"- \*\*Built\*\* (.*)\n", text).group(1)
    moment = dt.datetime.strptime(built, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
    assert abs((dt.datetime.now(UTC) - moment).total_seconds()) < 120


def test_markdown_of_a_report_with_no_window_and_no_table_says_none():
    run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})}, window=None, reads=())

    text = render_md(run, built_at=BUILT_AT)

    assert "**Window**" not in text.split("## How")[0] and "- **Window** none" in text and "  - none" in text


def test_markdown_puts_a_note_under_a_chart_that_left_rows_out():
    rows = pa.table({"n": list(range(501)), "v": [1] * 501})

    text = render_md(make_run([{"type": "line", "query": "q", "x": "n", "y": "v"}], {"q": rows}), built_at=BUILT_AT)

    assert "*The chart draws the first 500 of 501 rows. 1 row is left out.*" in text and text.count("\n| ") == 502


# --------------------------------------------------------------------------- JSON


def test_the_json_document_has_the_report_the_window_the_tables_the_rows_and_each_block():
    data = json.loads(render_json(golden_run(), built_at=BUILT_AT))

    assert list(data) == ["ldp_report", "report", "window", "timezone", "built_at", "built", "release", "live",
                          "parameters", "tables", "health", "queries", "blocks"]
    assert data["ldp_report"] == DOCUMENT_VERSION == 1
    assert data["report"] == {"identifier": "daily_sales", "title": "Daily sales", "who": "owner@example.com",
                              "what": "Orders and revenue of one day, from the gold tables.", "where": None,
                              "when": None, "how": None}
    assert data["window"] == {"start": "2026-07-30T18:30:00Z", "end": "2026-07-31T18:30:00Z"}
    assert (data["timezone"], data["built_at"], data["built"], data["release"], data["live"]) == (
        "Asia/Kolkata", "2026-08-02T06:30:15Z", "2026-08-02 06:30:15 UTC", None, False)
    assert [item["table"] for item in data["tables"]] == ["gold.gold_sales", "gold.gold_customers"]
    assert data["tables"][0] == {"table": "gold.gold_sales", "release": "r3", "snapshot_id": 8420615207115790441,
                                 "source": "release r3, cut 2026-08-02 06:00:00 UTC", "pipeline": "gold_sales"}
    assert [item["type"] for item in data["blocks"]] == ["kpi", "kpi", "kpi", "bar", "line", "table", "text"]
    assert [item["index"] for item in data["blocks"]] == list(range(7))


def test_the_json_rows_are_what_the_queries_returned_with_numbers_as_numbers_and_dates_as_text():
    data = json.loads(render_json(golden_run(), built_at=BUILT_AT))

    assert data["queries"]["totals"]["data"] == [{"revenue": 1234.5, "previous": 1100.0, "orders": 87,
                                                  "previous_orders": 90, "share": 0.1875}]
    assert data["queries"]["by_day"]["data"][0] == {"day": "2026-07-31", "city": "Pune", "revenue": 520.25}
    assert data["queries"]["by_day"]["columns"][0] == {"name": "day", "type": "date32[day]", "kind": None,
                                                       "format": None, "description": None}
    assert (data["queries"]["top"]["rows"], data["queries"]["top"]["truncated"], data["queries"]["top"]["kind"]) == (
        5, False, "query")


def test_a_block_says_its_values_both_as_computed_and_as_the_page_writes_them():
    blocks = json.loads(render_json(golden_run(), built_at=BUILT_AT))["blocks"]

    tile, chart, grid, words = blocks[0], blocks[4], blocks[5], blocks[6]

    assert (tile["value"], tile["raw"], tile["compare"], tile["raw_compare"], tile["change"], tile["direction"],
            tile["format"]) == ("$1,234.50", 1234.5, "$1,100.00", 1100.0, "+12.2%", "up", "currency:USD")
    assert chart["values"] == [{"name": "Pune", "values": [480.0, 520.25], "texts": ["$480.00", "$520.25"]},
                               {"name": "Delhi", "values": [350.5, 410.0], "texts": ["$350.50", "$410.00"]}]
    assert (chart["categories"], chart["x"], chart["y"], chart["series"], chart["rows"], chart["total_rows"]) == (
        ["2026-07-30", "2026-07-31"], "day", "revenue", "city", 4, 4)
    assert grid["cells"][0] == ["Thai", "31", "35.6%"] and grid["rows"] == 3 and grid["total_rows"] == 5
    assert grid["notes"] == ["The table shows the first 3 of 5 rows. 2 rows are left out."]
    assert words["paragraphs"][0] == "Orders & revenue of <Pune> and Delhi. There were 87 orders in all."


def test_the_json_document_is_a_run_of_the_same_views_that_the_page_is_made_from():
    run = golden_run()
    data = document(run, built_at=BUILT_AT)

    assert data["blocks"][3]["values"][0]["texts"] == list(run.views[3].series[0].texts)
    assert isinstance(render_json(run), str) and json.loads(render_json(run))["built_at"].endswith("Z")


def test_a_value_that_json_cannot_write_is_null_and_the_document_is_still_json():
    rows = pa.table({"x": ["a", "b", "c"], "y": pa.array([1.0, float("nan"), float("inf")], pa.float64())})
    blocks = [{"type": "line", "query": "q", "x": "x", "y": "y"}, {"type": "kpi", "query": "q", "value": "y"}]

    text = render_json(make_run(blocks, {"q": rows}), built_at=BUILT_AT)
    data = json.loads(text, parse_constant=lambda name: pytest.fail(f"{name} is not JSON"))

    assert data["queries"]["q"]["data"] == [{"x": "a", "y": 1.0}, {"x": "b", "y": None}, {"x": "c", "y": None}]
    assert data["blocks"][0]["values"][0] == {"name": "y", "values": [1.0, None, None], "texts": ["1", "nan", "inf"]}
    assert data["blocks"][1]["raw"] == 1.0
    chart = Parsed(render_html(make_run(blocks, {"q": rows}), built_at=BUILT_AT))
    assert chart.names().count("circle") == 1  # a value that is not a number is a gap and not a point


def test_decimals_in_a_block_are_numbers_like_the_rows_and_a_whole_decimal_has_no_point():
    rows = pa.table({"whole": pa.array([5], pa.decimal128(18, 0)), "money": decimals("1.10")})
    blocks = [{"type": "kpi", "query": "q", "value": "whole"}, {"type": "kpi", "query": "q", "value": "money"}]

    data = json.loads(render_json(make_run(blocks, {"q": rows}), built_at=BUILT_AT))

    assert data["queries"]["q"]["data"] == [{"whole": 5, "money": 1.1}]
    assert [item["raw"] for item in data["blocks"]] == [5, 1.1] and isinstance(data["blocks"][0]["raw"], int)


def test_the_time_of_a_build_is_written_in_utc_whatever_the_zone_of_the_moment():
    moment = dt.datetime(2026, 8, 2, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))

    data = json.loads(render_json(golden_run(), built_at=moment))

    assert data["built_at"] == "2026-08-02T06:30:00Z" and data["built"] == "2026-08-02 06:30:00 UTC"


def test_a_query_that_the_row_cap_cut_says_so_and_a_chart_that_cut_rows_says_how_many():
    rows = pa.table({"n": [1, 2, 3, 4], "v": [1, 1, 1, 1]})
    run = make_run([{"type": "line", "query": "q", "x": "n", "y": "v", "max_points": 3}], {"q": rows},
                   truncated=("q",), row_cap=4)

    data = json.loads(render_json(run, built_at=BUILT_AT))

    assert data["queries"]["q"]["truncated"] is True and data["queries"]["q"]["rows"] == 4
    assert (data["blocks"][0]["rows"], data["blocks"][0]["total_rows"]) == (3, 4)
    assert data["blocks"][0]["notes"][0].startswith("The query has more rows than the row cap of 4")


def test_a_query_that_gave_no_rows_because_it_did_not_run_is_not_in_the_json_document():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})})
    run.queries["broken"] = QueryRun(name="broken", kind="query", problem="metadata.queries.broken.sql: it failed")

    data = json.loads(render_json(run, built_at=BUILT_AT))

    assert list(data["queries"]) == ["q"]


def test_a_value_of_a_kind_that_json_has_no_name_for_is_written_as_text_and_a_decimal_that_is_no_number_is_null():
    rows = pa.table({"at": pa.array([dt.time(12, 30)], pa.time64("us")), "v": [1]})
    run = make_run([{"type": "kpi", "query": "q", "value": "at"}], {"q": rows})
    run.parameters = {"nope": Decimal("NaN"), "later": Decimal("Infinity"), "point": Decimal("1.5"),
                      "whole": Decimal("7")}

    data = json.loads(render_json(run, built_at=BUILT_AT))

    assert data["blocks"][0]["raw"] == "12:30:00"
    assert data["parameters"] == {"nope": None, "later": None, "point": 1.5, "whole": 7}


def test_the_json_document_keeps_text_that_is_not_ascii_as_it_is():
    rows = pa.table({"city": ["Z\u00fcrich"], "v": [1]})

    text = render_json(make_run([{"type": "table", "query": "q"}], {"q": rows}), built_at=BUILT_AT)

    assert "Z\u00fcrich" in text and "\\u" not in text


def test_the_parameters_that_a_run_bound_are_in_the_json_document_as_numbers_text_and_dates():
    run = make_run([{"type": "kpi", "query": "q", "value": "v"}], {"q": pa.table({"v": [1]})})
    run.parameters = {"top": 5, "city": "Pune", "day": dt.date(2026, 7, 31), "since": dt.datetime(2026, 7, 31, 1, 2, 3),
                      "limit": Decimal("2.50")}

    data = json.loads(render_json(run, built_at=BUILT_AT))

    assert data["parameters"] == {"top": 5, "city": "Pune", "day": "2026-07-31", "since": "2026-07-31T01:02:03",
                                  "limit": 2.5}


def test_render_refuses_a_format_it_does_not_write_and_names_the_ones_it_does():
    with pytest.raises(ConfigError, match=r"unknown format 'pdf'. The formats are \['html', 'md', 'json'\]"):
        render(golden_run(), "pdf", BUILT_AT)
    assert FORMATS == ("html", "md", "json") and STDOUT == "-"


# --------------------------------------------------------------------------- a lake


CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
SCHEMA = pa.schema([("order_date", pa.date32()), ("city", pa.string()), ("cuisine", pa.string()),
                    ("orders", pa.int64()), ("revenue", pa.decimal128(18, 2))])
ROWS = [
    (dt.date(2026, 7, 30), "Pune", "Thai", 2, "20.00"),
    (dt.date(2026, 7, 31), "Pune", "Thai", 3, "30.00"),
    (dt.date(2026, 7, 31), "Delhi", "Italian", 1, "15.50"),
    (dt.date(2026, 8, 1), "Pune", "Italian", 4, "40.00"),
    (dt.date(2026, 8, 2), "Delhi", "Thai", 5, "50.00"),
]
"""Five orders. The window of the tests, 31 July to 2 August, holds the second, the third and the fourth, so there were
8 orders for 85.50 in all, 45.50 on the 31st and 40.00 on the 1st."""
METRICS = {
    "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "sales_metrics", "who": "analytics@example.com",
    "semantic_models": [{
        "name": "daily_sales", "table": "gold.gold_sales",
        "time_dimension": {"column": "order_date", "grains": ["day", "week", "month"]},
        "dimensions": ["city", "cuisine"],
        "measures": [{"name": "revenue_sum", "agg": "sum", "expr": "revenue"},
                     {"name": "order_sum", "agg": "sum", "expr": "orders"}]}],
    "metrics": [
        {"name": "revenue", "type": "simple", "measure": "revenue_sum", "format": "currency", "description": "Money."},
        {"name": "orders", "type": "simple", "measure": "order_sum", "description": "Orders."},
    ],
}
LAKE_WINDOW = "2026-07-31/2026-08-02"
BY_DATE = "order_date >= $window_start AND order_date < $window_end"
LAKE_QUERIES = {
    "totals": {"sql": f"SELECT sum(orders) AS orders, sum(revenue) AS revenue FROM gold_sales WHERE {BY_DATE}"},
    "by_city": {"sql": f"SELECT city, sum(orders) AS orders FROM gold.gold_sales WHERE {BY_DATE} GROUP BY 1 "
                       "ORDER BY 1"},
}
LAKE_BLOCKS = [
    {"type": "kpi", "title": "Revenue", "query": "totals", "value": "revenue", "format": "currency:USD"},
    {"type": "kpi", "metric": "revenue", "grain": "day"},
    {"type": "bar", "query": "by_city", "x": "city", "y": "orders"},
    {"type": "line", "metric": "orders", "grain": "day", "by": "city"},
    {"type": "table", "title": "Orders by city", "query": "by_city"},
    {"type": "text", "text": "There were {totals.orders} orders."},
]


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def gold_config(name: str) -> dict:
    return {"identifier": name, "who": "ops", "metadata": {
        "source": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": f"The table {name}.", "grain": "one row per order"}}}


@pytest.fixture
def lake(tmp_path):
    """``config/`` with the gold table of ``ROWS`` and a metrics file, and an empty ``reports/``."""
    config = tmp_path / "config"
    table = pa.table({name: [Decimal(row[place]) if name == "revenue" else row[place] for row in ROWS]
                      for place, name in enumerate(SCHEMA.names)}, schema=SCHEMA)
    Iceberg("gold_sales", {"identifier": "gold", "warehouse_path": str(config / "warehouse")}).put(table)
    write(config / "gold_sales.json", gold_config("gold_sales"))
    write(config / "metrics.json", METRICS)
    (tmp_path / "reports").mkdir()
    return tmp_path


def cut(lake: Path) -> str:
    return build_gold(lake / "config", release=True).release.release.name


def spec_of(**metadata) -> dict:
    """A report over the lake. A value of ``None`` removes a key of ``metadata``."""
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily", "what": "Orders of the window.",
            "metadata": {"title": "Daily orders", "tables": {"config": ["../config"], "allow": ["gold.*"]},
                         "queries": dict(LAKE_QUERIES), "blocks": list(LAKE_BLOCKS)}}
    for key, value in metadata.items():
        if value is None:
            spec["metadata"].pop(key, None)
        else:
            spec["metadata"][key] = value
    return spec


def report_of(lake: Path, **metadata):
    return load_report(write(lake / "reports" / "daily.json", spec_of(**metadata)))


def built(lake: Path, fmt: str = "html", out: str | None = "site/daily.html", **keys):
    sink = keys.setdefault("sink", MemorySink())
    where = (lake / out) if out and out != STDOUT else out
    result = build_report(report_of(lake), keys.pop("window", LAKE_WINDOW), out=where, fmt=fmt,
                          clock=lambda: BUILT_AT, **keys)
    return result, sink


# --------------------------------------------------------------------------- build_report


def test_a_build_writes_the_page_and_says_what_it_wrote(lake):
    cut(lake)

    result, _ = built(lake)

    target = lake / "site" / "daily.html"
    data = target.read_bytes()
    assert data.decode("utf-8") == result.document and result.bytes == len(data)
    assert result.sha256 == hashlib.sha256(data).hexdigest() and result.output == str(target)
    assert (result.format, result.built_at, result.report.identifier) == ("html", BUILT_AT, "daily")
    assert uuid.UUID(result.run_id).version == 7 and result.duration_ms >= 0 and result.run.ok
    assert sorted(path.name for path in target.parent.iterdir()) == ["daily.html"]  # no temporary file is left


def test_a_build_runs_the_queries_and_the_metric_blocks_and_draws_what_they_gave(lake):
    cut(lake)

    result, _ = built(lake, fmt="json")
    data = json.loads(result.document)

    revenue, by_city = data["blocks"][0], data["blocks"][2]
    assert (revenue["value"], revenue["raw"]) == ("$85.50", 85.5)
    assert (by_city["categories"], by_city["values"][0]["values"]) == (["Delhi", "Pune"], [1.0, 7.0])
    metric = data["blocks"][1]  # the last day of the window against the day before it
    assert (metric["value"], metric["compare"], metric["change"], metric["direction"]) == (
        "40.00", "45.50", "-12.1%", "down")
    line = {item["name"]: item["values"] for item in data["blocks"][3]["values"]}
    assert data["blocks"][3]["categories"] == ["2026-07-31", "2026-08-01"]
    assert line == {"Pune": [3.0, 4.0], "Delhi": [1.0, None]}
    assert data["blocks"][5]["paragraphs"] == ["There were 8 orders."]
    assert data["window"] == {"start": "2026-07-31T00:00:00Z", "end": "2026-08-02T00:00:00Z"}
    columns = {name: [(item["name"], item["kind"], item["format"], item["description"]) for item in query["columns"]]
               for name, query in data["queries"].items()}
    assert columns["blocks[1]"] == [("day", "time", None, "the start of the day"),
                                    ("revenue", "metric", "currency", "Money.")]
    assert columns["blocks[3]"][2] == ("orders", "metric", "number", "Orders.")
    assert columns["totals"] == [("orders", None, None, None), ("revenue", None, None, None)]


def test_a_build_in_each_format_writes_the_document_that_render_gives(lake):
    cut(lake)

    for fmt in FORMATS:
        result, _ = built(lake, fmt=fmt, out=f"out/daily.{fmt}")

        assert (lake / "out" / f"daily.{fmt}").read_text(encoding="utf-8") == result.document
        assert result.document == render(result.run, fmt, BUILT_AT) and result.format == fmt
    assert (lake / "out" / "daily.md").read_text(encoding="utf-8").startswith("# Daily orders\n")
    assert (lake / "out" / "daily.html").read_text(encoding="utf-8").startswith("<!doctype html>\n")
    assert json.loads((lake / "out" / "daily.json").read_text(encoding="utf-8"))["ldp_report"] == 1


def test_a_build_can_write_to_a_file_uri_and_makes_the_folders_it_needs(lake):
    cut(lake)
    target = lake / "a" / "b" / "daily.html"

    result = build_report(report_of(lake), LAKE_WINDOW, out=target.as_uri(), clock=lambda: BUILT_AT, sink=MemorySink())

    assert target.read_text(encoding="utf-8") == result.document and result.output == target.as_uri()


def test_a_build_with_no_out_or_with_a_dash_writes_nothing_and_gives_the_document(lake, monkeypatch):
    cut(lake)
    monkeypatch.chdir(lake)  # a file called None or - would land here
    before = sorted(path.name for path in lake.iterdir())

    plain, sink = built(lake, out=None)
    dash, dash_sink = built(lake, out=STDOUT)

    assert plain.output is None and dash.output is None and plain.document == dash.document
    assert plain.document.startswith("<!doctype html>") and sorted(path.name for path in lake.iterdir()) == before
    assert [event.payload["output"] for event in sink.events if event.type == "report.rendered"] == [None]
    assert [event.payload["output"] for event in dash_sink.events if event.type == "report.rendered"] == [None]


def test_the_page_names_the_release_and_the_snapshot_that_a_build_read(lake):
    name = cut(lake)

    result, sink = built(lake)

    read = result.run.reads["gold.gold_sales"]
    assert read.release == name and isinstance(read.snapshot_id, int)
    assert f"<code>gold.gold_sales</code> release {name}, cut " in result.document
    assert f"UTC, snapshot {read.snapshot_id}" in result.document
    rendered = next(event for event in sink.events if event.type == "report.rendered")
    assert rendered.payload["tables"] == [
        {"table": "gold.gold_sales", "release": name, "snapshot_id": read.snapshot_id}]


def test_a_live_build_says_it_read_the_tables_as_they_are_now(lake):
    cut(lake)

    result, sink = built(lake, live=True)

    assert "<code>gold.gold_sales</code> the tables as they are now, snapshot " in result.document
    rendered = next(event for event in sink.events if event.type == "report.rendered")
    assert rendered.payload["live"] is True and rendered.payload["tables"][0]["release"] is None
    assert next(event for event in sink.events if event.type == "run.started").payload["live"] is True


def test_two_builds_over_the_same_snapshots_are_the_same_text_except_for_the_time_of_the_build(lake):
    cut(lake)
    first = dt.datetime.now(UTC).replace(microsecond=0) + dt.timedelta(seconds=2)  # after the table was written
    later = first + dt.timedelta(hours=5, minutes=3, seconds=9)
    texts = {}
    for fmt in FORMATS:
        texts[fmt] = [build_report(report_of(lake), LAKE_WINDOW, out=None, fmt=fmt, clock=lambda stamp=stamp: stamp,
                                   sink=MemorySink()).document for stamp in (first, later)]

    changed = {fmt: [(a, b) for a, b in zip(one.splitlines(), two.splitlines()) if a != b]
               for fmt, (one, two) in texts.items()}
    one, two = utc_text(first), utc_text(later)
    assert all(len(one.splitlines()) == len(two.splitlines()) for one, two in texts.values())
    assert changed["html"] == [(f"<dd>{one}</dd>", f"<dd>{two}</dd>")]
    assert changed["md"] == [(f"- **Built** {one}", f"- **Built** {two}")]
    stamp = [(f'  "built_at": "{first:%Y-%m-%dT%H:%M:%S}Z",', f'  "built_at": "{later:%Y-%m-%dT%H:%M:%S}Z",'),
             (f'  "built": "{one}",', f'  "built": "{two}",')]
    assert changed["json"] == stamp


def test_a_build_of_a_report_with_a_problem_writes_nothing_and_leaves_the_file_that_was_there(lake):
    cut(lake)
    target = lake / "site" / "daily.html"
    write(target, "the page of yesterday")
    blocks = [{"type": "kpi", "query": "totals", "value": "nope"}, {"type": "bar", "query": "by_city", "x": "city",
                                                                    "y": "city"}]

    with pytest.raises(ReportBuildError) as caught:
        build_report(report_of(lake, blocks=blocks), LAKE_WINDOW, out=target, sink=MemorySink())

    assert caught.value.report == "daily" and len(caught.value.problems) == 2
    assert caught.value.problems[0] == ("metadata.blocks[0].value: the query 'totals' has no column 'nope'. Its "
                                        "columns are ['orders', 'revenue']")
    assert caught.value.problems[1].startswith("metadata.blocks[1].y: the column 'city' of the query 'by_city' is a ")
    assert str(caught.value).splitlines()[0] == "the report daily has 2 problems and was not built"
    assert str(caught.value).splitlines()[1] == f"  - {caught.value.problems[0]}"
    assert target.read_text() == "the page of yesterday" and sorted(path.name for path in target.parent.iterdir()) == [
        "daily.html"]


def test_one_problem_is_said_in_the_singular_and_a_query_that_failed_is_a_problem_too(lake):
    cut(lake)
    queries = {**LAKE_QUERIES, "broken": {"sql": "SELECT nope FROM gold_sales"}}

    with pytest.raises(ReportBuildError) as caught:
        build_report(report_of(lake, queries=queries), LAKE_WINDOW, out=None, sink=MemorySink())

    assert str(caught.value).splitlines()[0] == "the report daily has 1 problem and was not built"
    assert caught.value.problems[0].startswith("metadata.queries.broken")


def test_a_build_emits_the_start_the_rendering_and_the_finish_with_no_row_of_data(lake):
    cut(lake)

    result, sink = built(lake)

    assert sink.types == ["run.started", "report.rendered", "run.finished"] and sink.flushes == 1
    started, rendered, finished = sink.events
    assert {event.run_id for event in sink.events} == {result.run_id}
    assert [event.seq for event in sink.events] == [0, 1, 2]
    assert {event.payload["pipeline"] for event in sink.events} == {"daily"} == {event.payload["report"] for event in
                                                                                 sink.events}
    assert started.payload["task_kind"] == "report" and started.payload["format"] == "html"
    assert started.payload["spec"] == "daily.json" and started.payload["release"] is None
    assert rendered.payload["format"] == "html" and rendered.payload["output"] == result.output
    assert (rendered.payload["bytes"], rendered.payload["sha256"]) == (result.bytes, result.sha256)
    assert (rendered.payload["window"], rendered.payload["timezone"]) == ("2026-07-31T00:00:00Z/2026-08-02T00:00:00Z",
                                                                          "UTC")
    assert (rendered.payload["queries"], rendered.payload["blocks"], rendered.payload["rows"]) == (4, 6, 8)
    assert (finished.payload["status"], finished.payload["rows_read"]) == ("succeeded", 8)
    assert all(event.ts == BUILT_AT for event in sink.events)
    text = json.dumps([event.to_dict() for event in sink.events])
    assert "Pune" not in text and "Delhi" not in text and "85.5" not in text


def test_a_build_that_fails_emits_run_failed_and_a_failed_finish(lake):
    cut(lake)
    sink = MemorySink()

    with pytest.raises(ReportBuildError):
        build_report(report_of(lake, blocks=[{"type": "kpi", "query": "totals", "value": "nope"}]), LAKE_WINDOW,
                     out=lake / "x.html", sink=sink, clock=lambda: BUILT_AT)

    assert sink.types == ["run.started", "run.failed", "run.finished"] and sink.flushes == 1
    failed, finished = sink.events[1].payload, sink.events[2].payload
    assert (failed["stage"], failed["error_type"], failed["problems"]) == ("queries", "ReportBuildError", 1)
    assert failed["message"] == "the report daily has 1 problem and was not built"
    assert finished["status"] == "failed" and not (lake / "x.html").exists()


def test_a_build_that_cannot_write_says_so_at_the_write_stage_and_leaves_nothing(lake):
    cut(lake)
    blocker = write(lake / "blocker", "a file where a folder should be")
    sink = MemorySink()

    with pytest.raises(OSError):
        build_report(report_of(lake), LAKE_WINDOW, out=blocker / "daily.html", sink=sink, clock=lambda: BUILT_AT)

    assert sink.types == ["run.started", "run.failed", "run.finished"]
    assert (sink.events[1].payload["stage"], sink.events[1].payload["problems"]) == ("write", None)
    assert blocker.read_text() == "a file where a folder should be"


def test_a_format_that_is_not_known_is_refused_before_anything_runs(lake):
    sink = MemorySink()

    with pytest.raises(ConfigError, match="unknown format 'pdf'"):
        build_report(report_of(lake), LAKE_WINDOW, out=None, fmt="pdf", sink=sink)

    assert sink.events == []


def test_a_window_a_parameter_the_release_and_the_limits_are_passed_to_the_queries(lake):
    first = cut(lake)
    params = {"city": {"type": "string", "default": "Pune"}}
    queries = {"q": {"sql": "SELECT count(*) AS n, $city AS city FROM gold_sales WHERE order_date >= "
                            "$window_start AND order_date < $window_end"}}
    report = report_of(lake, queries=queries, parameters=params, blocks=[{"type": "table", "query": "q"}])

    result = build_report(report, "2026-07-31/2026-08-01", out=None, fmt="json", params={"city": "Delhi"},
                          release=first, row_cap=10, timeout_s=30, sink=MemorySink())

    data = json.loads(result.document)
    assert data["queries"]["q"]["data"] == [{"n": 2, "city": "Delhi"}]
    assert data["release"] == first and data["parameters"]["city"] == "Delhi"
    assert data["window"]["start"] == "2026-07-31T00:00:00Z"
    with pytest.raises(ConfigError, match="either a release or live"):
        build_report(report, LAKE_WINDOW, out=None, release=first, live=True, sink=MemorySink())


def test_a_build_that_cannot_render_says_so_at_the_render_stage_and_writes_nothing(lake, monkeypatch):
    cut(lake)
    sink = MemorySink()

    def broken(run, fmt, built_at):
        raise ValueError("the page broke")

    monkeypatch.setattr(report_build, "render", broken)
    with pytest.raises(ValueError, match="the page broke"):
        build_report(report_of(lake), LAKE_WINDOW, out=lake / "x.html", sink=sink, clock=lambda: BUILT_AT)

    assert sink.types == ["run.started", "run.failed", "run.finished"] and not (lake / "x.html").exists()
    failed = sink.events[1].payload
    assert (failed["stage"], failed["error_type"], failed["message"], failed["problems"]) == (
        "render", "ValueError", "the page broke", None)


def test_the_size_and_the_digest_of_a_document_are_those_of_its_bytes_when_it_has_letters_beyond_ascii(lake):
    cut(lake)
    target, sink = lake / "site" / "cafe.html", MemorySink()

    result = build_report(report_of(lake, title="Caf\u00e9 orders"), LAKE_WINDOW, out=target, clock=lambda: BUILT_AT,
                          sink=sink)

    data = target.read_bytes()
    assert "Caf\u00e9 orders" in result.document and len(data) > len(result.document)
    assert result.bytes == len(data) and result.sha256 == hashlib.sha256(data).hexdigest()
    rendered = next(event for event in sink.events if event.type == "report.rendered")
    assert (rendered.payload["bytes"], rendered.payload["sha256"]) == (len(data), result.sha256)


def test_the_event_of_a_build_says_the_time_zone_of_the_report_and_the_window_as_utc_instants(lake):
    cut(lake)
    sink = MemorySink()

    build_report(report_of(lake, timezone="Asia/Kolkata"), LAKE_WINDOW, out=None, clock=lambda: BUILT_AT, sink=sink)

    rendered = next(event for event in sink.events if event.type == "report.rendered")
    assert rendered.payload["timezone"] == "Asia/Kolkata"
    assert rendered.payload["window"] == "2026-07-31T00:00:00Z/2026-08-02T00:00:00Z"


def test_the_time_of_a_build_is_one_moment_in_the_document_the_result_and_between_the_events(lake):
    cut(lake)
    ticks = iter(BUILT_AT + dt.timedelta(seconds=number) for number in range(100))
    sink = MemorySink()

    result = build_report(report_of(lake), LAKE_WINDOW, out=None, clock=lambda: next(ticks), sink=sink)

    assert re.findall(r"<dd>(.*?)</dd>", result.document)[-1] == utc_text(result.built_at) == "2026-08-02 06:30:16 UTC"
    assert [event.ts for event in sink.events] == [BUILT_AT + dt.timedelta(seconds=number) for number in (0, 2, 3)]


def test_a_window_by_name_is_counted_from_the_time_that_the_build_is_given(lake):
    cut(lake)
    now = dt.datetime(2026, 8, 2, 10, 0, tzinfo=UTC)

    result = build_report(report_of(lake), "yesterday", out=None, fmt="json", now=now, sink=MemorySink())

    assert json.loads(result.document)["window"] == {"start": "2026-08-01T00:00:00Z", "end": "2026-08-02T00:00:00Z"}


# --------------------------------------------------------------------------- the sinks of a report


def test_the_sink_of_a_report_comes_from_its_observability_block(lake):
    none = report_of(lake)
    one = report_of(lake, observability={"sinks": [{"type": "jsonl", "path": "events.jsonl"}]})
    two = report_of(lake, observability={"sinks": ["jsonl", {"type": "jsonl", "path": "other.jsonl"}, "null"]})
    only_null = report_of(lake, observability={"sinks": ["null"]})

    assert isinstance(report_sink(none), events.NullSink) and isinstance(report_sink(only_null), events.NullSink)
    assert isinstance(report_sink(one), events.JsonlSink) and report_sink(one).path == lake / "reports" / "events.jsonl"
    assert isinstance(report_sink(two), events.MultiSink)
    mixed = report_of(lake, observability={"sinks": ["null", {"type": "jsonl", "path": "events.jsonl"}]})
    assert isinstance(report_sink(mixed), events.JsonlSink)  # a sink that drops everything is left out


def test_the_builds_of_a_report_are_listed_by_ldp_runs_with_their_window_and_where_they_wrote(lake, capsys):
    cut(lake)
    sinks = {"sinks": [{"type": "jsonl", "path": "events.jsonl"}]}
    path = write(lake / "reports" / "daily.json", spec_of(observability=sinks))
    out = lake / "site" / "daily.html"
    ids = []
    for window in (LAKE_WINDOW, "2026-07-30/2026-07-31"):
        ids.append(build_report(load_report(path), window, out=out).run_id)
    with pytest.raises(ReportBuildError):
        build_report(load_report(write(path, spec_of(observability=sinks, blocks=[
            {"type": "kpi", "query": "totals", "value": "nope"}]))), LAKE_WINDOW, out=out)

    loaded, where = report_events(path)
    runs = summarize_runs(loaded)

    assert where == str(lake / "reports" / "events.jsonl") and len(runs) == 3
    done = [run for run in runs if run["status"] == "succeeded"]
    assert {run["run_id"] for run in done} == set(ids) and all(run["is_report"] for run in done)
    assert {run["pipeline"] for run in runs} == {"daily"} and [run["status"] for run in runs].count("failed") == 1
    assert sorted(run["window"] for run in done) == ["2026-07-30T00:00:00Z/2026-07-31T00:00:00Z",
                                                     "2026-07-31T00:00:00Z/2026-08-02T00:00:00Z"]
    assert {(run["output"], run["format"]) for run in done} == {(str(out), "html")}
    assert load_run_events(path)[0] == loaded and [e.run_id for e in load_run_events(path, run_id=ids[0])[0]
                                                   ] == [ids[0]] * 3

    assert main(["runs", str(path)]) == 0
    listing = capsys.readouterr().out
    assert "Runs recorded in" in listing and "window" in listing and "output" in listing and "succeeded" in listing
    assert "2026-07-30T00:00:00Z/2026-07-31T00:00:00Z" in listing and str(out) in listing and "failed" in listing
    assert main(["runs", str(path), "--run", ids[0]]) == 0
    detail = capsys.readouterr().out
    assert "report.rendered" in detail and "format=html" in detail and f"output={out}" in detail
    assert "queries=4" in detail


def test_the_builds_of_a_report_that_has_no_sink_are_not_recorded_and_nothing_is_created(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of())

    build_report(load_report(path), LAKE_WINDOW, out=lake / "site" / "daily.html")

    assert report_events(path)[0] == [] and main(["runs", str(path)]) == 0
    assert "No runs recorded" in capsys.readouterr().out
    assert sorted(item.name for item in (lake / "reports").iterdir()) == ["daily.json"]


# --------------------------------------------------------------------------- ldp report build


def test_the_command_writes_the_report_and_says_what_it_did(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of())
    out = lake / "site" / "daily.html"

    assert main(["report", "build", str(path), "--window", LAKE_WINDOW, "--out", str(out)]) == 0

    captured = capsys.readouterr()
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>\n")
    assert "problem" not in captured.err and "not built" not in captured.err  # a log line may be there, an error is not
    assert re.fullmatch(rf"daily \(daily\.json\): built {re.escape(str(out))} as html, [\d,]+ bytes, 4 queries and "
                        r"6 blocks, in \d+ ms\n", captured.out)


@pytest.mark.parametrize("fmt", ["md", "json"])
def test_the_command_writes_markdown_and_json_too(lake, capsys, fmt):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of())
    out = lake / f"daily.{fmt}"

    assert main(["report", "build", str(path), "--window", LAKE_WINDOW, "--out", str(out), "--format", fmt]) == 0

    assert f"as {fmt}," in capsys.readouterr().out
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Daily orders\n") if fmt == "md" else json.loads(text)["ldp_report"] == 1


def test_a_dash_for_out_prints_the_document_to_standard_output_and_the_summary_to_standard_error(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of())

    assert main(["report", "build", str(path), "--window", LAKE_WINDOW, "--out", "-", "--format", "json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["report"]["identifier"] == "daily"
    assert "daily (daily.json): built standard output as json, " in captured.err.splitlines()[-1]  # the last line
    assert sorted(item.name for item in lake.iterdir()) == ["config", "reports"]


def test_the_command_needs_out_and_takes_a_report_file_and_not_a_folder(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of())

    assert main(["report", "build", str(path), "--window", LAKE_WINDOW]) == 1
    assert "ldp report build needs --out, so that no file is written by surprise. Use --out - for standard output" in (
        capsys.readouterr().err)
    assert main(["report", "build", str(lake / "reports"), "--out", str(lake / "x.html")]) == 1
    assert f"takes a report file, and {lake / 'reports'} is a folder" in capsys.readouterr().err
    assert main(["report", "build", str(lake / "missing.json"), "--out", str(lake / "x.html")]) == 1
    assert "missing.json" in capsys.readouterr().err and not (lake / "x.html").exists()


def test_the_command_lists_every_problem_and_exits_with_one_and_writes_nothing(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json",
                 spec_of(blocks=[{"type": "kpi", "query": "totals", "value": "nope"},
                                 {"type": "table", "query": "by_city", "columns": ["zzz"]}]))
    out = lake / "site" / "daily.html"

    assert main(["report", "build", str(path), "--window", LAKE_WINDOW, "--out", str(out)]) == 1

    err = capsys.readouterr().err
    assert "the report daily has 2 problems and was not built" in err
    assert "metadata.blocks[0].value: the query 'totals' has no column 'nope'" in err
    assert "metadata.blocks[1].columns[0]: the query 'by_city' has no column 'zzz'" in err
    assert not out.exists() and not out.parent.exists()


def test_the_command_passes_the_options_of_the_run_to_the_queries(lake, capsys):
    first = cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of(
        queries={"q": {"sql": "SELECT count(*) AS n, $city AS city FROM gold_sales WHERE order_date >= "
                              "$window_start AND order_date < $window_end"}},
        parameters={"city": {"type": "string", "default": "Pune"}}, blocks=[{"type": "table", "query": "q"}]))

    assert main(["report", "build", str(path), "--window", "2026-07-31/2026-08-01", "--param", "city=Delhi",
                 "--release", first, "--row-cap", "10", "--timeout", "30", "--out", "-", "--format", "json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["queries"]["q"]["data"] == [{"n": 2, "city": "Delhi"}] and data["release"] == first
    # a release and live are two ways to say which version to read, so the command refuses both
    assert main(["report", "build", str(path), "--release", first, "--live", "--out", "-"]) == 1
    assert "argument --live: not allowed with argument --release" in capsys.readouterr().err


def test_the_command_reads_the_tables_as_they_are_now_with_live(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of())

    assert main(["report", "build", str(path), "--window", LAKE_WINDOW, "--live", "--out", "-"]) == 0

    assert "<code>gold.gold_sales</code> the tables as they are now, snapshot " in capsys.readouterr().out


def test_the_command_hands_each_of_its_options_to_the_build_and_nothing_else(lake, monkeypatch, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json", spec_of(
        queries={"q": {"sql": "SELECT $city AS city"}}, parameters={"city": {"type": "string", "default": "Pune"}},
        blocks=[{"type": "table", "query": "q"}]))
    calls = []
    real = report_build.build_report

    def spy(report, window=None, **keywords):
        calls.append((window, keywords))
        return real(report, window, **keywords)

    monkeypatch.setattr(report_build, "build_report", spy)

    assert main(["report", "build", str(path), "--window", "yesterday", "--param", "city=Delhi", "--release", "r1",
                 "--row-cap", "7", "--timeout", "9", "--out", "-", "--format", "md"]) == 0
    assert main(["report", "build", str(path), "--live", "--out", str(lake / "x.html")]) == 0

    capsys.readouterr()
    assert calls[0] == ("yesterday", {"out": "-", "fmt": "md", "params": {"city": "Delhi"}, "release": "r1",
                                      "live": False, "row_cap": 7, "timeout_s": 9.0})
    assert calls[1] == (None, {"out": str(lake / "x.html"), "fmt": "html", "params": None, "release": None,
                               "live": True, "row_cap": None, "timeout_s": None})


def test_the_summary_says_one_query_and_one_block_in_the_singular_and_the_size_with_separators(lake, capsys):
    cut(lake)
    path = write(lake / "reports" / "daily.json",
                 spec_of(queries={"q": {"sql": "SELECT 1 AS n"}}, blocks=[{"type": "table", "query": "q"}]))
    out = lake / "one.html"

    assert main(["report", "build", str(path), "--out", str(out)]) == 0

    assert f"daily (daily.json): built {out} as html, {out.stat().st_size:,} bytes, 1 query and 1 block, in " in (
        capsys.readouterr().out)
    assert out.stat().st_size > 1000


def test_the_help_of_the_command_says_what_it_writes_and_how_it_is_called(capsys):
    assert main(["report", "build", "--help"]) == 0

    out = " ".join(capsys.readouterr().out.split())
    assert "--out PATH" in out and "--format {html,md,json}" in out and "s3:// or gs://" in out
    assert "md, Markdown with the charts as tables, or json, the rows and what each block shows" in out
    assert ("Example: ldp report build ldp_demo/restaurant/reports/daily_sales.json --window 2026-07-31/2026-08-01 "
            "--out site/daily.html") in out


def test_the_report_command_lists_build_among_its_commands(capsys):
    assert main(["report"]) == 1

    assert "build" in capsys.readouterr().out


# --------------------------------------------------------------------------- the files go through fs


def test_a_document_is_written_whole_and_replaces_the_one_that_was_there(tmp_path):
    target = tmp_path / "out" / "page.html"

    write_document(target, b"first")
    write_document(str(target), "second \u00fc".encode("utf-8"))

    assert target.read_bytes() == "second \u00fc".encode("utf-8")
    assert [item.name for item in target.parent.iterdir()] == ["page.html"]


@pytest.fixture(scope="module")
def moto_endpoint():
    server_module = pytest.importorskip("moto.server", reason="moto[server] is not installed")
    werkzeug = logging.getLogger("werkzeug")
    level = werkzeug.level
    werkzeug.setLevel(logging.ERROR)
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()
        werkzeug.setLevel(level)


def test_a_build_can_write_to_s3(lake, moto_endpoint, monkeypatch, tmp_path):
    boto3 = pytest.importorskip("boto3", reason="boto3 is not installed")
    for name in ("AWS_ENDPOINT_URL_S3", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                        ("AWS_REGION", "us-east-1"), ("AWS_DEFAULT_REGION", "us-east-1"),
                        ("AWS_ENDPOINT_URL", moto_endpoint), ("AWS_EC2_METADATA_DISABLED", "true"),
                        ("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config")),
                        ("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))):
        monkeypatch.setenv(name, value)
    bucket = f"ldp-report-{uuid.uuid4().hex[:12]}"
    client = boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    cut(lake)

    result = build_report(report_of(lake), LAKE_WINDOW, out=f"s3://{bucket}/reports/daily.md", fmt="md",
                          clock=lambda: BUILT_AT, sink=MemorySink())

    body = client.get_object(Bucket=bucket, Key="reports/daily.md")["Body"].read()
    assert body.decode("utf-8") == result.document and result.output == f"s3://{bucket}/reports/daily.md"
    assert not client.list_multipart_uploads(Bucket=bucket).get("Uploads")


# --------------------------------------------------------------------------- the spec of a report that builds


def test_a_spec_keeps_the_max_points_of_a_chart_and_the_report_builds_from_it():
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "r",
            "metadata": {"tables": {"config": ["."]}, "queries": {"q": {"sql": "SELECT 1"}},
                         "blocks": [{"type": "bar", "query": "q", "x": "a", "y": "b", "max_points": 7}]}}

    assert build_spec(spec).blocks[0].max_points == 7
