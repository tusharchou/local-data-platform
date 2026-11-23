"""A folder of reports built into one site: the index, the pages and the links between them.

The names, the badges and the index are tested on entries made by hand, and the index is compared with a golden file in
``tests/golden/report/``. After a change that is meant to alter the index, rewrite it with
``LDP_UPDATE_GOLDEN=1 pytest tests/test_report_site.py`` and read the difference in git before committing. What belongs
to the page shell and not to the index, which is its stylesheet, the hash of it and the version of this package, is
replaced by a word before the comparison.

Then a gold table with a config and a history of runs is built into folders of reports. That shows each badge, the links
between the files, the reports that do not build, the files that a build leaves, and the command and its exit status.
Everything runs offline, and the one test of ``s3://`` uses an in-process moto server.
"""

import datetime as dt
import difflib
import json
import logging
import os
import re
import shutil
import uuid
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pyarrow as pa
import pytest

from local_data_platform import __version__
from local_data_platform.cli import main
from local_data_platform.events import MemorySink, RunEvent
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.report import site as report_site
from local_data_platform.report.build import build_report
from local_data_platform.report.formats import utc_text
from local_data_platform.report.build import render
from local_data_platform.report.health import ReportHeld, TableHealth
from local_data_platform.report.render_html import render_html, window_label, window_text
from local_data_platform.report.site import (
    BADGES,
    BUILT,
    FAILED,
    HELD,
    HOME,
    INDEX,
    LATEST,
    Entry,
    Site,
    build_site,
    folder_problem,
    join_location,
    page_name,
    render_index,
)
from local_data_platform.report.spec import load_report
from tests.report_support import BUILT_AT, UTC, golden_run, hours_before

GOLDEN = Path(__file__).parent / "golden" / "report"
UPDATE_GOLDEN = os.environ.get("LDP_UPDATE_GOLDEN") == "1"
HOSTILE = "<script>alert(1)</script> \"quoted\" & 'single' <img src=http://evil.example/x onerror=alert(1)>"
KOLKATA = "Asia/Kolkata"
WINDOW = "2026-07-31/2026-08-02"


class Cards(HTMLParser):
    """What an index has: its links, the chips outside the cards, and for each card its title, link, chips and facts."""

    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.tags: list[str] = []
        self.summary: list[tuple[str, str]] = []
        self.cards: list[dict] = []
        self.scripts = 0
        self.title = ""
        self._card: dict | None = None
        self._open: list[tuple[str, dict]] = []
        self._term = ""
        self.feed(text)
        self.close()

    def _inside(self, tag: str) -> bool:
        return any(name == tag for name, _ in self._open)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self._open.append((tag, attrs))
        self.tags.append(tag)
        if tag == "a" and "href" in attrs:
            self.hrefs.append(attrs["href"])
        if tag == "script":
            self.scripts += 1
        if tag == "section":
            self._card = {"labelled": attrs.get("aria-labelledby"), "title": "", "link": None, "chips": [], "facts": {},
                          "pre": "", "sub": "", "note": ""}
            self.cards.append(self._card)
        elif tag == "a" and self._card is not None and self._inside("h2"):
            self._card["link"] = attrs.get("href")

    def handle_endtag(self, tag):
        while self._open and self._open[-1][0] != tag:
            self._open.pop()
        if self._open:
            self._open.pop()
        if tag == "section":
            self._card = None

    def handle_data(self, data):
        if not self._open:
            return
        tag, attrs = self._open[-1]
        kind = attrs.get("class") or ""
        if tag == "title":
            self.title += data
        elif self._card is None:
            if tag == "span" and kind.startswith("chip"):
                self.summary.append((kind, data))
        elif tag in ("h2", "a") and self._inside("h2"):
            self._card["title"] += data
        elif tag == "span" and kind.startswith("chip"):
            self._card["chips"].append((kind, data))
        elif tag == "dt":
            self._term = data
        elif tag == "dd":
            self._card["facts"][self._term] = self._card["facts"].get(self._term, "") + data
        elif tag == "pre":
            self._card["pre"] += data
        elif tag == "p" and kind in ("sub", "note"):
            self._card[kind] += data


def settled(text: str) -> str:
    """A page with its stylesheet, the hash of it and the version of the package replaced by a word."""
    text = re.sub(r"<style>.*?</style>", "<style>CSS</style>", text, flags=re.S)
    text = re.sub(r"&#x27;sha256-[A-Za-z0-9+/=]+&#x27;", "&#x27;sha256-CSS&#x27;", text)
    return text.replace(f"local-data-platform {__version__}", "local-data-platform VERSION")


# --------------------------------------------------------------------------- the name of a page

DAY_IN_INDIA = (dt.datetime(2026, 7, 30, 18, 30, tzinfo=UTC), dt.datetime(2026, 7, 31, 18, 30, tzinfo=UTC))
DAY_IN_UTC = (dt.datetime(2026, 7, 31, tzinfo=UTC), dt.datetime(2026, 8, 1, tzinfo=UTC))
NOW = dt.datetime(2026, 8, 1, 22, 0, 30, tzinfo=UTC)


@pytest.mark.parametrize("window, timezone, name", [
    (DAY_IN_INDIA, KOLKATA, "2026-07-31.html"),  # midnight where the report lives, which is the day it is about
    (DAY_IN_UTC, "UTC", "2026-07-31.html"),
    (DAY_IN_UTC, KOLKATA, "2026-07-31T0530.html"),  # explicit dates are UTC, which is 05:30 in India, as the page says
    ((dt.datetime(2026, 7, 31, 9, 0, tzinfo=UTC), dt.datetime(2026, 7, 31, 10, 0, tzinfo=UTC)), "UTC",
     "2026-07-31T0900.html"),
    ((dt.datetime(2026, 7, 31, 0, 0, 5, tzinfo=UTC), dt.datetime(2026, 8, 1, tzinfo=UTC)), "UTC",
     "2026-07-31T000005.html"),
    ((dt.datetime(2026, 7, 31, 0, 0, tzinfo=UTC), dt.datetime(2026, 8, 1, 0, 0, 30, tzinfo=UTC)), "UTC",
     "2026-07-31.html"),  # only the start is named
    ((dt.datetime(2026, 7, 31, 23, 0, tzinfo=UTC), dt.datetime(2026, 8, 1, 1, 0, tzinfo=UTC)), "America/Los_Angeles",
     "2026-07-31T1600.html"),
    (None, "UTC", "2026-08-01.html"),  # no window, so the day of the build
    (None, KOLKATA, "2026-08-02.html"),  # which it is in India, where it is already the next day
])
def test_a_page_is_named_for_the_start_of_its_window_in_the_zone_of_the_report(window, timezone, name):
    assert page_name(window, timezone, NOW) == name


def test_two_windows_that_start_at_different_moments_never_have_the_same_name():
    starts = [dt.datetime(2026, 7, 31, hour, minute, second, tzinfo=UTC) for hour in (0, 1, 9, 23)
              for minute in (0, 30) for second in (0, 5)]

    names = {page_name((start, start + dt.timedelta(hours=1)), "UTC", NOW) for start in starts}

    assert len(names) == len(starts) and all(name.endswith(".html") and "/" not in name for name in names)


@pytest.mark.parametrize("identifier", ["daily", "daily_sales", "Sales-2026.v2", "a", "9lives", "a" * 128, "a.b-c_d"])
def test_an_identifier_that_is_a_safe_name_can_be_the_folder_of_a_report(identifier):
    assert folder_problem(identifier) is None


@pytest.mark.parametrize("identifier", [
    "", "../x", "a/b", "a\\b", "a b", ".hidden", "-x", "_x", "a..b", "..", ".", "index.html", "INDEX.HTML",
    "Index.Html", "a" * 129, "caf\u00e9", "a\nb", "a:b", "a?b", "a#b", "a%2Fb", "~x"])
def test_an_identifier_that_is_not_a_safe_name_cannot_be_the_folder_of_a_report(identifier):
    problem = folder_problem(identifier)

    assert problem is not None and repr(identifier) in problem
    assert "cannot be the name of a folder of a site" in problem and "'..'" in problem and INDEX in problem


@pytest.mark.parametrize("base, parts, joined", [
    ("site", ("daily", "latest.html"), "site/daily/latest.html"),
    ("site/", ("index.html",), "site/index.html"),
    ("site///", ("index.html",), "site/index.html"),
    (Path("a") / "site", ("daily",), "a/site/daily"),
    ("file:///tmp/site/", ("daily", "x.html"), "file:///tmp/site/daily/x.html"),
    ("s3://bucket", ("index.html",), "s3://bucket/index.html"),
    ("s3://bucket/site/", ("daily", "latest.html"), "s3://bucket/site/daily/latest.html"),
    ("gs://bucket/a/b", ("c.html",), "gs://bucket/a/b/c.html"),
])
def test_a_place_in_the_site_is_the_folder_and_the_name_whatever_kind_of_address_the_folder_is(base, parts, joined):
    assert join_location(base, *parts) == joined


# --------------------------------------------------------------------------- an entry and its badge


def entry(status: str = BUILT, *, title: str = "Daily sales", identifier: str | None = "daily_sales",
          file: str = "daily_sales.json", what: str | None = "Orders and revenue of one day.",
          window: str | None = "2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata", gated: bool = True,
          stale: str | None = None, error: str | None = None, moment: dt.datetime = BUILT_AT) -> Entry:
    """An entry made by hand. A built one has the latest page of its identifier."""
    page = f"{identifier}/{LATEST}" if status == BUILT else None
    return Entry(file=Path(file), status=status, title=title, moment=moment, identifier=identifier, what=what,
                 window=window, gated=gated, stale=stale, page=page, error=error)


@pytest.mark.parametrize("item, label", [
    (entry(), "fresh"),
    (entry(stale="1 table is stale, gold.a"), "stale"),
    (entry(gated=False), "not checked"),
    (entry(gated=False, stale="1 table is stale, gold.a"), "not checked"),  # a report with no gate has no verdict
    (entry(HELD, stale="1 table is stale, gold.a", error="held"), "held"),
    (entry(HELD, gated=False, error="held"), "held"),
    (entry(FAILED, error="no"), "failed"),
    (entry(FAILED, gated=False, error="no", identifier=None), "failed"),
])
def test_the_badge_of_a_report_says_what_became_of_it_and_whether_its_data_was_fresh(item, label):
    assert item.label == label and label in BADGES


def test_the_badges_are_in_the_order_that_the_index_counts_them():
    assert BADGES == ("fresh", "stale", "not checked", "held", "failed")


def test_the_status_of_an_entry_is_one_of_three_words_that_the_documentation_names():
    assert (BUILT, HELD, FAILED) == ("built", "held", "failed")


def test_a_site_sorts_its_entries_by_what_became_of_them():
    items = (entry(), entry(HELD, error="h"), entry(FAILED, error="f"), entry(), entry(FAILED, error="g"))
    site = Site(out="site", index="site/index.html", document="", built_at=BUILT_AT, entries=items)

    assert (site.built, site.held, site.failed) == (
        (items[0], items[3]), (items[1],), (items[2], items[4]))
    assert site.of("nothing") == ()


# --------------------------------------------------------------------------- the index

HELD_TEXT = ("the report monthly_revenue was held, because 1 table is stale and metadata.freshness says block\n"
             "  - gold.gold_sales: its last run ended blocked_quality (failing not_null(city))")
BROKEN_TEXT = ("broken.json has 2 problems:\n"
               "  - kind: broken.json is a Pipeline, and a report has \"kind\": \"Report\"\n"
               "  - metadata.blocks[0].value: the query 'totals' has no column 'nope'")


def hand_made() -> list[Entry]:
    """One card of every badge, fresh, stale, not checked, held and failed, the last of a file that cannot be read."""
    return [
        entry(),
        entry(title="Weekly orders", identifier="weekly_orders", file="weekly_orders.json",
              what="Orders of the week, by city.", stale="1 table is stale, gold.gold_sales"),
        entry(title="Menu items", identifier="menu_items", file="menu_items.json", what=None, gated=False,
              window="none", moment=BUILT_AT + dt.timedelta(minutes=1)),
        entry(HELD, title="Monthly revenue", identifier="monthly_revenue", file="monthly_revenue.json",
              what="Revenue of the month.", stale="1 table is stale, gold.gold_sales", error=HELD_TEXT),
        entry(FAILED, title="broken.json", identifier=None, file="broken.json", what=None, window=None, gated=False,
              error=BROKEN_TEXT),
    ]


def test_the_index_is_what_its_golden_file_says():
    text = settled(render_index(hand_made(), BUILT_AT))
    path = GOLDEN / "index.html"
    if UPDATE_GOLDEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    assert path.is_file(), f"{path} is missing. Write it with LDP_UPDATE_GOLDEN=1 pytest tests/test_report_site.py"
    expected = path.read_bytes().decode("utf-8").replace("\r\n", "\n")  # a checkout may have made the line ends CRLF
    if text != expected:
        diff = "\n".join(list(difflib.unified_diff(expected.splitlines(), text.splitlines(), "golden", "built",
                                                   lineterm="", n=1))[:60])
        pytest.fail(f"the index is not what {path.name} says. If the change is meant, rewrite it with "
                    f"LDP_UPDATE_GOLDEN=1.\n{diff}")


def test_the_golden_index_is_plain_ascii_with_no_hidden_character_and_ends_in_one_newline():
    data = (GOLDEN / "index.html").read_bytes().replace(b"\r\n", b"\n")  # a checkout may have made the line ends CRLF

    assert data.isascii() and b"\r" not in data and b"\t" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")


def test_the_index_has_one_card_for_each_report_in_the_order_given_with_its_title_what_window_time_and_badge():
    page = Cards(render_index(hand_made(), BUILT_AT))

    assert [card["title"] for card in page.cards] == ["Daily sales", "Weekly orders", "Menu items", "Monthly revenue",
                                                      "broken.json"]
    first = page.cards[0]
    assert first["link"] == "daily_sales/latest.html" and first["sub"] == "Orders and revenue of one day."
    assert first["facts"] == {"Window": "2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata",
                              "Built": "2026-08-02 06:30:15 UTC"}
    assert first["chips"] == [("chip ok", "fresh")] and first["note"] == "" and first["pre"] == ""
    assert [card["chips"] for card in page.cards] == [
        [("chip ok", "fresh")], [("chip warn", "stale")], [("chip", "not checked")], [("chip bad", "held")],
        [("chip bad", "failed")]]
    assert page.cards[2]["facts"]["Window"] == "none" and page.cards[2]["facts"]["Built"] == "2026-08-02 06:31:15 UTC"
    assert page.cards[2]["sub"] == ""  # a report that does not say what it is for has no line for it


def test_the_index_counts_each_badge_in_a_line_of_chips_and_says_how_many_reports_it_lists():
    page = Cards(render_index(hand_made(), BUILT_AT))

    assert page.summary == [("chip ok", "1 fresh"), ("chip warn", "1 stale"), ("chip", "1 not checked"),
                            ("chip bad", "1 held"), ("chip bad", "1 failed")]
    assert page.title == "Reports" and "<p class=\"sub\">5 reports</p>" in render_index(hand_made(), BUILT_AT)
    two = Cards(render_index([entry(), entry(identifier="b", title="B")], BUILT_AT))
    assert two.summary == [("chip ok", "2 fresh")]
    one = render_index([entry()], BUILT_AT)
    assert "<p class=\"sub\">1 report</p>" in one and "1 reports" not in one


def test_only_a_report_that_was_built_links_to_its_page_and_the_others_say_why_not():
    page = Cards(render_index(hand_made(), BUILT_AT))

    assert [card["link"] for card in page.cards] == ["daily_sales/latest.html", "weekly_orders/latest.html",
                                                     "menu_items/latest.html", None, None]
    assert page.hrefs == ["daily_sales/latest.html", "weekly_orders/latest.html", "menu_items/latest.html"]
    assert page.cards[1]["note"] == "1 table is stale, gold.gold_sales."
    assert page.cards[3]["pre"] == HELD_TEXT and page.cards[3]["note"] == ""  # the reasons are in the text
    assert page.cards[4]["pre"] == BROKEN_TEXT
    assert page.cards[3]["facts"].keys() == {"Window", "Tried"} and page.cards[4]["facts"].keys() == {"Tried"}


def test_a_card_that_has_no_window_yet_says_nothing_about_it_and_a_report_with_none_says_none():
    page = Cards(render_index(hand_made(), BUILT_AT))

    assert "Window" not in page.cards[4]["facts"] and page.cards[2]["facts"]["Window"] == "none"


def test_every_card_is_a_section_labelled_by_its_own_heading():
    text = render_index(hand_made(), BUILT_AT)
    page = Cards(text)

    assert [card["labelled"] for card in page.cards] == [f"report-{number}-h" for number in range(1, 6)]
    assert re.findall(r'<h2 id="(report-\d-h)">', text) == [card["labelled"] for card in page.cards]


def test_the_index_is_a_page_of_the_shell_that_loads_nothing_and_runs_no_script():
    text = render_index(hand_made(), BUILT_AT)

    assert text.startswith("<!doctype html>\n") and text.endswith("</html>\n")
    assert "default-src &#x27;none&#x27;" in text and "script-src" not in text and Cards(text).scripts == 0
    assert "http://" not in text and "https://" not in text and "url(" not in text and " style=" not in text
    assert "<title>Reports</title>" in text and '<meta name="description" content="The latest build' in text
    assert "Built 2026-08-02 06:30:15 UTC by ldp report build." in text and f"local-data-platform {__version__}" in text


def test_every_value_of_a_card_is_escaped_so_a_title_or_an_error_cannot_become_markup():
    items = [entry(title=HOSTILE, what=HOSTILE, window=HOSTILE, stale=HOSTILE),
             entry(FAILED, title=HOSTILE, identifier=None, file="x.json", window=None, error=HOSTILE)]

    text = render_index(items, BUILT_AT)

    page = Cards(text)
    assert "<script" not in text and "<img" not in text and "script" not in page.tags and "img" not in page.tags
    assert page.cards[0]["title"] == HOSTILE and page.cards[0]["sub"] == HOSTILE
    assert page.cards[0]["facts"]["Window"] == HOSTILE and page.cards[0]["note"] == HOSTILE + "."
    assert page.cards[1]["pre"] == HOSTILE and page.hrefs == ["daily_sales/latest.html"]


def test_a_link_to_a_page_is_escaped_too():
    item = Entry(file=Path("x.json"), status=BUILT, title="X", moment=BUILT_AT, page='a"b/latest.html?x=1&y=2')

    assert 'href="a&quot;b/latest.html?x=1&amp;y=2"' in render_index([item], BUILT_AT)


def test_two_indexes_of_the_same_entries_are_the_same_text_except_for_the_time_of_the_build():
    first = render_index(hand_made(), BUILT_AT)
    second = render_index(hand_made(), BUILT_AT + dt.timedelta(hours=1))

    assert first != second
    assert first.replace("Built 2026-08-02 06:30:15 UTC by", "Built T by") == second.replace(
        "Built 2026-08-02 07:30:15 UTC by", "Built T by")


def test_the_index_of_a_card_that_has_no_error_and_no_note_has_no_empty_paragraph():
    text = render_index([entry(what=None)], BUILT_AT)

    assert "<pre>" not in text and 'class="note"' not in text and 'class="sub">' not in text.split("<main")[1]


# --------------------------------------------------------------------------- the page of a report links back


def test_a_page_that_is_given_a_home_starts_with_a_link_back_to_the_index():
    plain = render_html(golden_run(), built_at=BUILT_AT)
    linked = render_html(golden_run(), built_at=BUILT_AT, home=HOME)

    assert HOME == "../index.html" and "All reports" not in plain
    main_of = linked.split("<main", 1)[1]
    assert main_of.split(">", 1)[1].lstrip().startswith('<p><a href="../index.html">All reports</a></p>')
    assert linked.replace('<p><a href="../index.html">All reports</a></p>\n', "") == plain
    assert Cards(linked).hrefs == ["../index.html"]
    assert Cards(render_html(golden_run(), built_at=BUILT_AT, home="reports/start.html")).hrefs == [
        "reports/start.html"]


def test_a_home_is_for_a_page_only_and_markdown_and_json_ignore_it():
    run = golden_run()

    assert render(run, "html", BUILT_AT, HOME) == render_html(run, built_at=BUILT_AT, home=HOME)
    assert render(run, "html", BUILT_AT) == render_html(run, built_at=BUILT_AT)
    for fmt in ("md", "json"):
        assert render(run, fmt, BUILT_AT, HOME) == render(run, fmt, BUILT_AT)


def test_a_window_is_written_as_two_wall_clock_times_in_a_zone_and_a_report_with_none_has_no_text():
    run = golden_run()

    assert window_label(run.window, KOLKATA) == "2026-07-31 00:00 to 2026-08-01 00:00, Asia/Kolkata"
    assert window_label(run.window, "UTC") == "2026-07-30 18:30 to 2026-07-31 18:30, UTC"
    assert window_label(None, KOLKATA) is None
    assert window_text(run) == window_label(run.window, run.report.timezone)


# --------------------------------------------------------------------------- a lake with run history

CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
SCHEMA = pa.schema([("order_date", pa.date32()), ("city", pa.string()), ("orders", pa.int64())])
ROWS = [(dt.date(2026, 7, 31), "Pune", 3), (dt.date(2026, 7, 31), "Delhi", 1), (dt.date(2026, 8, 1), "Pune", 4)]
QUERY = {"sql": "SELECT sum(orders) AS orders FROM gold.gold_sales WHERE order_date >= $window_start "
                "AND order_date < $window_end"}
BLOCKS = [{"type": "kpi", "title": "Orders", "query": "totals", "value": "orders", "format": "integer"}]


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def event(kind: str, number: int, moment: dt.datetime, seq: int = 0, **payload) -> RunEvent:
    return RunEvent(event_id=f"gold_sales-{number}-{kind}-{seq}", type=kind, schema_version=1, ts=moment,
                    run_id=f"gold-sales-run-{number}", attempt=1, seq=seq,
                    payload={"pipeline": "gold_sales", "table": "gold.gold_sales", **payload})


def run_events(number: int, started: dt.datetime) -> list[RunEvent]:
    """The events of one run of ``gold_sales`` that published 100 rows."""
    second = dt.timedelta(seconds=1)
    return [event("run.started", number, started, mode="overwrite", publish=True),
            event("run.extracted", number, started + second, 1, rows_read=100),
            event("run.published", number, started + 3 * second, 3, rows_written=100, snapshot_id=1000 + number,
                  failures=[]),
            event("run.finished", number, started + 4 * second, 9, status="published", rows_written=100,
                  duration_s=4.0)]


def now_after() -> dt.datetime:
    """A moment after the table was written, which is the clock of a build that is not meant to be in the past."""
    return dt.datetime.now(UTC) + dt.timedelta(seconds=2)


@pytest.fixture
def lake(tmp_path):
    """``config/`` with the gold table ``gold_sales`` and its config, which keeps its run events in a jsonl file."""
    table = pa.table({column: [row[place] for row in ROWS] for place, column in enumerate(SCHEMA.names)},
                     schema=SCHEMA)
    Iceberg("gold_sales", {"identifier": "gold", "warehouse_path": str(tmp_path / "config" / "warehouse")}).put(table)
    write(tmp_path / "config" / "gold_sales.json", {"identifier": "gold_sales", "who": "ops", "metadata": {
        "source": {"name": "gold_sales", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "gold_sales", "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": "The table gold_sales.", "grain": "one row per order"},
        "observability": {"sinks": ["jsonl"]}}})
    (tmp_path / "reports").mkdir()
    return tmp_path


def history(lake: Path, now: dt.datetime, hours: float = 40) -> None:
    """One run of ``gold_sales`` that published ``hours`` before ``now``, in the file of the jsonl sink."""
    folder = lake / "config" / ".ldp"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(item.to_json() + "\n" for item in run_events(1, hours_before(now, hours)))


def spec_of(identifier: str, *, title: str | None = None, what: str | None = "Orders of the window.",
            freshness: dict | None = None, **metadata) -> dict:
    tables = {"config": ["../config"], "allow": ["gold.*"]}
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": identifier,
            "metadata": {"title": title or identifier.capitalize(), "tables": tables,
                         "queries": {"totals": QUERY}, "blocks": list(BLOCKS)}}
    if what is not None:
        spec["what"] = what
    if freshness is not None:
        spec["metadata"]["freshness"] = freshness
    spec["metadata"].update(metadata)
    return spec


def put_report(lake: Path, identifier: str, name: str | None = None, **keywords) -> Path:
    return write(lake / "reports" / f"{name or identifier}.json", spec_of(identifier, **keywords))


def broken(lake: Path, identifier: str = "broken") -> Path:
    """A report that cannot be built, because a block names a column that its query lacks."""
    return put_report(lake, identifier, blocks=[{"type": "kpi", "query": "totals", "value": "nope"}])


def three(lake: Path) -> None:
    """A folder of three reports. One is fine, one is fine with a gate that looks at its data, and one is broken."""
    put_report(lake, "daily", title="Daily orders")
    put_report(lake, "weekly", title="Weekly orders", freshness={"max_age_hours": 48, "on_stale": "warn"})
    broken(lake)


def five(lake: Path, now: dt.datetime) -> None:
    """A report for every badge, over a table whose last good run was 40 hours before ``now``."""
    history(lake, now, 40)
    put_report(lake, "adhoc", title="Ad hoc")
    broken(lake)
    put_report(lake, "daily", title="Daily orders", freshness={"max_age_hours": 48, "on_stale": "warn"})
    put_report(lake, "monthly", title="Monthly orders", freshness={"max_age_hours": 26, "on_stale": "block"})
    put_report(lake, "weekly", title="Weekly orders", freshness={"max_age_hours": 26, "on_stale": "warn"})


def site_of(lake: Path, now: dt.datetime | None = None, window: str | None = WINDOW, **keywords) -> Site:
    moment = now or now_after()
    keywords.setdefault("sink", MemorySink())
    return build_site(lake / "reports", lake / "site", window, clock=lambda: moment, **keywords)


def tree(folder: Path) -> list[str]:
    """Every file under a folder, relative to it and in order."""
    return sorted(str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file())


def links(path: Path) -> list[str]:
    return Cards(path.read_text(encoding="utf-8")).hrefs


# --------------------------------------------------------------------------- a folder of three reports, one broken


def test_a_folder_of_three_reports_one_of_them_broken_builds_two_pages_and_an_index_that_lists_all_three(lake):
    three(lake)
    now = now_after()
    history(lake, now, 3)

    site = site_of(lake, now)

    assert tree(lake / "site") == ["daily/2026-07-31.html", "daily/latest.html", "index.html", "weekly/2026-07-31.html",
                                   "weekly/latest.html"]
    assert [(item.identifier, item.status) for item in site.entries] == [
        ("broken", FAILED), ("daily", BUILT), ("weekly", BUILT)]
    page = Cards((lake / "site" / INDEX).read_text(encoding="utf-8"))
    assert [card["title"] for card in page.cards] == ["Broken", "Daily orders", "Weekly orders"]
    assert page.hrefs == ["daily/latest.html", "weekly/latest.html"]
    assert [card["chips"][0][1] for card in page.cards] == ["failed", "not checked", "fresh"]
    error = page.cards[0]["pre"]
    assert error.startswith("the report broken has 1 problem and was not built")
    assert "metadata.blocks[0].value: the query 'totals' has no column 'nope'" in error
    assert page.cards[0]["link"] is None


def test_the_index_links_the_latest_page_of_each_report_that_was_built_and_every_link_reaches_a_file(lake):
    three(lake)
    now = now_after()

    site = site_of(lake, now)

    folder = lake / "site"
    index = folder / INDEX
    for href in links(index):
        assert not urlsplit(href).scheme and not href.startswith(("/", "#", "..")) and "\\" not in href
        assert (folder / href).is_file(), href
    assert links(index) == ["daily/latest.html", "weekly/latest.html"]
    assert site.index == str(index) and index.read_text(encoding="utf-8") == site.document


def test_the_latest_page_is_the_page_of_the_window_byte_for_byte_and_links_back_to_the_index(lake):
    three(lake)

    site_of(lake)

    for name in ("daily", "weekly"):
        dated = (lake / "site" / name / "2026-07-31.html").read_bytes()
        assert (lake / "site" / name / "latest.html").read_bytes() == dated
        page = Cards(dated.decode("utf-8"))
        assert page.hrefs == [HOME]
        assert ((lake / "site" / name) / HOME).resolve() == (lake / "site" / INDEX).resolve()


def test_the_site_opens_from_anywhere_because_a_copy_of_the_folder_has_every_link_in_place(lake, tmp_path_factory):
    three(lake)
    site_of(lake)

    elsewhere = tmp_path_factory.mktemp("a shared drive") / "reports of the team"
    shutil.copytree(lake / "site", elsewhere)

    for page in elsewhere.rglob("*.html"):
        for href in links(page):
            assert (page.parent / href).is_file(), f"{page.relative_to(elsewhere)} links to {href}"
    assert tree(elsewhere) == tree(lake / "site")


def test_a_report_that_fails_does_not_stop_the_others_and_is_listed_with_its_error_and_nothing_is_written_for_it(lake):
    three(lake)

    site = site_of(lake)

    assert not (lake / "site" / "broken").exists()
    assert [item.status for item in site.entries] == [FAILED, BUILT, BUILT]
    failed = site.failed[0]
    assert failed.identifier == "broken" and failed.page is None and failed.build is None
    assert failed.error.startswith("the report broken has 1 problem and was not built\n  - metadata.blocks[0].value")


def test_each_card_has_the_window_the_time_and_what_the_report_is_for_and_the_index_counts_the_badges(lake):
    three(lake)
    now = now_after()

    site_of(lake, now)

    page = Cards((lake / "site" / INDEX).read_text(encoding="utf-8"))
    assert page.summary == [("chip ok", "1 fresh"), ("chip", "1 not checked"), ("chip bad", "1 failed")]
    assert [card["facts"]["Window"] for card in page.cards] == ["2026-07-31 00:00 to 2026-08-02 00:00, UTC"] * 3
    assert [card["facts"].get("Built") for card in page.cards[1:]] == [utc_text(now)] * 2
    assert page.cards[0]["facts"]["Tried"] == utc_text(now)
    assert [card["sub"] for card in page.cards] == ["Orders of the window."] * 3
    assert "Built " + utc_text(now) + " by ldp report build." in (lake / "site" / INDEX).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the badge that a report earns


def test_each_report_gets_the_badge_that_its_data_and_its_gate_earn(lake):
    now = now_after()
    five(lake, now)

    site = site_of(lake, now)

    assert {item.identifier: item.label for item in site.entries} == {
        "adhoc": "not checked", "broken": "failed", "daily": "fresh", "monthly": "held", "weekly": "stale"}
    page = Cards((lake / "site" / INDEX).read_text(encoding="utf-8"))
    assert [(card["title"], card["chips"]) for card in page.cards] == [
        ("Ad hoc", [("chip", "not checked")]), ("Broken", [("chip bad", "failed")]),
        ("Daily orders", [("chip ok", "fresh")]), ("Monthly orders", [("chip bad", "held")]),
        ("Weekly orders", [("chip warn", "stale")])]
    assert page.summary == [("chip ok", "1 fresh"), ("chip warn", "1 stale"), ("chip", "1 not checked"),
                            ("chip bad", "1 held"), ("chip bad", "1 failed")]
    assert page.hrefs == ["adhoc/latest.html", "daily/latest.html", "weekly/latest.html"]


def test_a_stale_report_is_built_with_its_banner_and_its_card_names_the_stale_table(lake):
    now = now_after()
    five(lake, now)

    site_of(lake, now)

    card = Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards[4]
    assert card["note"] == "1 table is stale, gold.gold_sales." and card["facts"]["Built"] == utc_text(now)
    page = (lake / "site" / "weekly" / LATEST).read_text(encoding="utf-8")
    assert '<div class="banner" role="status">' in page and "<strong>Stale data.</strong>" in page
    assert "which is more than the 26 hours that this report allows" in page
    fresh = (lake / "site" / "daily" / LATEST).read_text(encoding="utf-8")
    assert 'class="banner"' not in fresh


def test_a_report_that_the_gate_holds_back_has_no_page_and_its_card_says_what_the_gate_said(lake):
    now = now_after()
    five(lake, now)

    site = site_of(lake, now)

    assert not (lake / "site" / "monthly").exists()
    held = site.held[0]
    assert (held.identifier, held.status, held.page, held.build) == ("monthly", HELD, None, None)
    assert held.stale == "1 table is stale, gold.gold_sales"
    assert held.window == "2026-07-31 00:00 to 2026-08-02 00:00, UTC"
    card = Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards[3]
    assert card["pre"].startswith(
        "the report monthly was held, because 1 table is stale and metadata.freshness says block\n"
        "  - gold.gold_sales: its last successful run was at ")
    assert card["pre"].endswith("which is more than the 26 hours that this report allows")
    assert card["facts"] == {"Window": "2026-07-31 00:00 to 2026-08-02 00:00, UTC", "Tried": utc_text(now)}
    assert card["note"] == "" and card["link"] is None


def test_a_report_that_was_built_before_and_is_held_now_keeps_its_old_pages_and_the_index_no_longer_links_them(lake):
    now = now_after()
    history(lake, now, 3)
    put_report(lake, "monthly", title="Monthly orders", freshness={"max_age_hours": 26, "on_stale": "block"})
    site_of(lake, now)
    old = {name: (lake / "site" / "monthly" / name).read_bytes() for name in ("2026-07-31.html", LATEST)}
    assert links(lake / "site" / INDEX) == ["monthly/latest.html"]

    site = site_of(lake, now + dt.timedelta(hours=40))  # the run that was 3 hours old is 43 hours old now

    assert [item.status for item in site.entries] == [HELD]
    assert {name: (lake / "site" / "monthly" / name).read_bytes() for name in old} == old
    assert tree(lake / "site") == ["index.html", "monthly/2026-07-31.html", "monthly/latest.html"]
    assert links(lake / "site" / INDEX) == []


def test_a_report_with_no_gate_is_not_checked_whatever_state_its_data_is_in(lake):
    now = now_after()
    history(lake, now, 400)
    put_report(lake, "adhoc")

    site = site_of(lake, now)

    assert [item.label for item in site.entries] == ["not checked"] and site.entries[0].stale is None
    assert Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards[0]["note"] == ""


# --------------------------------------------------------------------------- the pages a build writes and keeps


def test_a_build_of_another_window_adds_a_page_and_moves_latest_and_nothing_is_deleted(lake):
    three(lake)
    now = now_after()
    site_of(lake, now, "2026-07-31/2026-08-01")
    first = (lake / "site" / "daily" / LATEST).read_bytes()

    site_of(lake, now, "2026-08-01/2026-08-02")

    folder = lake / "site" / "daily"
    assert sorted(path.name for path in folder.iterdir()) == ["2026-07-31.html", "2026-08-01.html", "latest.html"]
    assert (folder / LATEST).read_bytes() == (folder / "2026-08-01.html").read_bytes() != first
    assert (folder / "2026-07-31.html").read_bytes() == first
    assert links(lake / "site" / INDEX) == ["daily/latest.html", "weekly/latest.html"]  # one link for each report


def test_a_build_of_a_window_that_is_already_there_writes_its_page_again_and_leaves_no_temporary_file(lake):
    three(lake)
    now = now_after()
    site_of(lake, now, "2026-07-31/2026-08-01")
    before = {name: (lake / "site" / name).read_bytes() for name in tree(lake / "site")}

    site_of(lake, now, "2026-07-31/2026-08-01")

    assert {name: (lake / "site" / name).read_bytes() for name in tree(lake / "site")} == before
    assert not [path for path in (lake / "site").rglob("*") if path.name.endswith(".tmp")]


def test_a_report_that_has_no_window_is_named_for_the_day_of_the_build_in_its_own_time_zone(lake):
    put_report(lake, "stock", timezone=KOLKATA, queries={"totals": {"sql": "SELECT sum(orders) AS orders FROM "
                                                                           "gold.gold_sales"}})
    now = dt.datetime(2026, 8, 1, 22, 0, tzinfo=UTC)  # the 2nd of August in India

    site = site_of(lake, now, window=None)

    assert tree(lake / "site") == ["index.html", "stock/2026-08-02.html", "stock/latest.html"]
    assert site.entries[0].window == "none"
    assert Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards[0]["facts"]["Window"] == "none"


def test_a_window_that_starts_at_midnight_in_the_zone_of_the_report_is_named_for_the_day_it_is_about(lake):
    put_report(lake, "india", timezone=KOLKATA)
    put_report(lake, "utc")
    now = dt.datetime(2026, 8, 2, 1, 0, tzinfo=UTC)

    site = site_of(lake, now, window="yesterday")

    assert tree(lake / "site") == ["index.html", "india/2026-08-01.html", "india/latest.html", "utc/2026-08-01.html",
                                   "utc/latest.html"]
    cards = Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards
    assert [card["facts"]["Window"] for card in cards] == ["2026-08-01 00:00 to 2026-08-02 00:00, Asia/Kolkata",
                                                           "2026-08-01 00:00 to 2026-08-02 00:00, UTC"]
    assert [item.window for item in site.entries] == [card["facts"]["Window"] for card in cards]


def test_a_window_of_explicit_dates_is_utc_and_so_its_page_says_the_hour_of_the_zone_of_the_report(lake):
    put_report(lake, "india", timezone=KOLKATA)

    site_of(lake, window="2026-07-31/2026-08-01")

    assert tree(lake / "site") == ["index.html", "india/2026-07-31T0530.html", "india/latest.html"]


def test_the_page_of_a_site_is_the_page_that_a_single_build_writes_with_a_link_back_added(lake):
    path = put_report(lake, "daily", title="Daily orders")
    now = now_after()
    build_report(load_report(path), WINDOW, out=lake / "single.html", clock=lambda: now, sink=MemorySink())

    site_of(lake, now)

    single = (lake / "single.html").read_text(encoding="utf-8")
    paged = (lake / "site" / "daily" / LATEST).read_text(encoding="utf-8")
    assert paged.replace('<p><a href="../index.html">All reports</a></p>\n', "") == single and paged != single


def test_two_builds_at_the_same_moment_are_the_same_files_and_at_other_moments_differ_only_in_their_time(lake):
    three(lake)
    now = now_after()
    site_of(lake, now)
    first = {name: (lake / "site" / name).read_text(encoding="utf-8") for name in tree(lake / "site")}
    shutil.rmtree(lake / "site")

    site_of(lake, now)
    assert {name: (lake / "site" / name).read_text(encoding="utf-8") for name in tree(lake / "site")} == first
    shutil.rmtree(lake / "site")
    later = now + dt.timedelta(minutes=5)

    site_of(lake, later)

    second = {name: (lake / "site" / name).read_text(encoding="utf-8") for name in tree(lake / "site")}
    assert list(second) == list(first) and second != first
    for name, text in first.items():
        assert text.replace(utc_text(now), "T") == second[name].replace(utc_text(later), "T"), name


# --------------------------------------------------------------------------- reports that do not build


def test_a_report_file_that_cannot_be_read_is_a_card_with_the_name_of_its_file_and_its_problems(lake):
    put_report(lake, "daily")
    write(lake / "reports" / "bad.json", {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "bad"})
    write(lake / "reports" / "worse.json", {"kind": "Report"})

    site = site_of(lake)

    bad, worse = site.failed
    assert (bad.title, bad.identifier, bad.window, bad.file.name) == ("bad.json", None, None, "bad.json")
    assert bad.error == "bad.json: metadata: is required and must be an object with tables, queries and blocks"
    assert worse.title == "worse.json" and worse.error.startswith("worse.json has 2 problems:\n  - ")
    cards = Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards
    assert [card["title"] for card in cards] == ["bad.json", "Daily", "worse.json"]
    assert cards[0]["pre"] == bad.error and list(cards[0]["facts"]) == ["Tried"] and cards[2]["pre"] == worse.error
    assert tree(lake / "site") == ["daily/2026-07-31.html", "daily/latest.html", "index.html"]


def test_a_file_of_another_kind_or_one_that_is_not_json_is_not_a_report_and_is_left_alone(lake):
    put_report(lake, "daily")
    write(lake / "reports" / "pipeline.json", {"identifier": "p", "metadata": {}})
    write(lake / "reports" / "notes.json", "this is { not json")
    write(lake / "reports" / "queries.sql", "-- name: x\nSELECT 1")
    write(lake / "reports" / "list.json", "[1, 2]")

    site = site_of(lake)

    assert [item.identifier for item in site.entries] == ["daily"] and site.failed == ()


@pytest.mark.parametrize("identifier", ["../escape", "a/b", "a b", ".hidden", "index.html", "INDEX.HTML", "caf\u00e9"])
def test_a_report_whose_identifier_is_not_a_safe_folder_name_is_a_failed_card_and_writes_nothing_for_it(
        lake, identifier):
    put_report(lake, "daily")
    path = put_report(lake, identifier, name="odd")

    site = site_of(lake)

    odd = site.failed[0]
    assert odd.identifier == identifier and odd.file == path and odd.build is None
    assert odd.error.startswith(f"odd.json: the identifier {identifier!r} cannot be the name of a folder of a site")
    assert tree(lake / "site") == ["daily/2026-07-31.html", "daily/latest.html", "index.html"]
    assert not (lake / "escape").exists() and not (lake.parent / "escape").exists()
    assert Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards[1]["pre"] == odd.error


def test_the_error_of_a_report_is_the_whole_text_with_every_problem_and_not_only_the_first(lake):
    put_report(lake, "daily", blocks=[{"type": "kpi", "query": "totals", "value": "nope"},
                                      {"type": "table", "query": "totals", "columns": ["zzz"]}])

    site = site_of(lake)

    error = site.failed[0].error
    assert error.splitlines()[0] == "the report daily has 2 problems and was not built"
    assert "metadata.blocks[0].value: the query 'totals' has no column 'nope'" in error
    assert "metadata.blocks[1].columns[0]: the query 'totals' has no column 'zzz'" in error


def test_a_parameter_that_a_report_does_not_declare_fails_that_report_alone_as_it_does_for_ldp_report_check(lake):
    put_report(lake, "has_it", parameters={"city": {"type": "string", "default": "Pune"}},
               queries={"totals": {"sql": "SELECT $city AS city, 1 AS orders"}})
    put_report(lake, "lacks_it")

    site = site_of(lake, params={"city": "Delhi"})

    assert [(item.identifier, item.status) for item in site.entries] == [("has_it", BUILT), ("lacks_it", FAILED)]
    assert site.failed[0].error == ("the report lacks_it was not built. the report has no parameter 'city'. "
                                    "It declares none")


def test_a_malformed_value_for_a_parameter_fails_the_report_with_the_reason_and_the_others_still_build(lake):
    put_report(lake, "daily", parameters={"top": {"type": "integer", "default": 5}})
    put_report(lake, "weekly")

    site = site_of(lake, params={"top": "many"})

    assert [item.status for item in site.entries] == [FAILED, FAILED]  # weekly does not declare it either
    assert "many" in site.failed[0].error and site.failed[0].error.startswith("the report daily was not built. ")


def test_a_report_that_raises_something_unexpected_is_a_card_with_the_name_of_the_error_and_the_rest_build(
        lake, monkeypatch):
    three(lake)
    real = report_site.build_report

    def flaky(report, *args, **keywords):
        if report.identifier == "daily":
            raise RuntimeError("boom")
        return real(report, *args, **keywords)

    monkeypatch.setattr(report_site, "build_report", flaky)

    site = site_of(lake)

    assert [(item.identifier, item.status) for item in site.entries] == [
        ("broken", FAILED), ("daily", FAILED), ("weekly", BUILT)]
    assert site.failed[1].error == "the report daily was not built. RuntimeError: boom"
    assert tree(lake / "site") == ["index.html", "weekly/2026-07-31.html", "weekly/latest.html"]


def test_a_secret_in_an_error_is_masked_on_the_page_and_in_what_the_command_prints(lake, monkeypatch, capsys):
    put_report(lake, "daily")
    monkeypatch.setattr(report_site, "build_report", lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("could not connect with password=hunter2 to https://user:s3cret@db.example/x")))

    assert main(["report", "build", str(lake / "reports"), "--window", WINDOW, "--out", str(lake / "site")]) == 1

    captured = capsys.readouterr()
    page = (lake / "site" / INDEX).read_text(encoding="utf-8")
    for text in (page, captured.out, captured.err):
        assert "hunter2" not in text and "s3cret" not in text
    assert "password=***" in page and "password=***" in captured.err


def test_what_looks_like_a_secret_is_masked_in_every_kind_of_error_a_card_can_have(lake, monkeypatch, capsys):
    put_report(lake, "daily", name="held")
    put_report(lake, "api_token=hunter2", name="odd", title="Odd one")
    spec = spec_of("worse")
    spec["metadata"]["password=hunter2"] = 1
    write(lake / "reports" / "worse.json", spec)
    real = report_site.build_report

    def held_for_one(report, *args, **keywords):
        if report.file.name == "held.json":
            raise ReportHeld("daily", (TableHealth(table="gold.x", stale=True, reasons=("failing token=hunter2",)),))
        return real(report, *args, **keywords)

    monkeypatch.setattr(report_site, "build_report", held_for_one)

    assert main(command(lake)) == 1

    captured = capsys.readouterr()
    page = (lake / "site" / INDEX).read_text(encoding="utf-8")
    for text in (page, captured.err):  # what the command prints for a report is its identifier, which is a name
        assert "hunter2" not in text
    cards = Cards(page).cards
    assert "failing token=***" in cards[0]["pre"]  # held
    assert "api_token=***" in cards[1]["pre"]  # an identifier that is not a folder name
    assert "password=***" in cards[2]["pre"]  # a file that cannot be read


def test_a_missing_engine_is_not_a_card_for_every_report_and_writes_nothing(lake, monkeypatch):
    three(lake)

    def missing(*args, **keywords):
        raise EngineNotFound("duckdb is not installed")

    monkeypatch.setattr(report_site, "build_report", missing)

    with pytest.raises(EngineNotFound, match="duckdb is not installed"):
        site_of(lake)

    assert not (lake / "site").exists()


def test_a_report_whose_latest_page_cannot_be_written_is_failed_and_the_index_does_not_link_it(lake, monkeypatch):
    three(lake)
    real = report_site.write_document

    def refuse_latest(out, data):
        if str(out).endswith("/weekly/latest.html"):
            raise OSError(13, "Permission denied")
        real(out, data)

    monkeypatch.setattr(report_site, "write_document", refuse_latest)

    site = site_of(lake)

    weekly = site.entries[2]
    assert (weekly.identifier, weekly.status, weekly.page) == ("weekly", FAILED, None)
    assert weekly.error == "the report weekly was not built. PermissionError: [Errno 13] Permission denied"
    assert (lake / "site" / "weekly" / "2026-07-31.html").is_file() and not (lake / "site" / "weekly" / LATEST).exists()
    assert links(lake / "site" / INDEX) == ["daily/latest.html"]


def test_an_index_that_cannot_be_written_is_an_error_and_the_pages_stay(lake, monkeypatch):
    put_report(lake, "daily")
    real = report_site.write_document

    def refuse_index(out, data):
        if str(out).endswith("/index.html"):
            raise OSError(28, "No space left on device")
        real(out, data)

    monkeypatch.setattr(report_site, "write_document", refuse_index)

    with pytest.raises(OSError, match="No space left"):
        site_of(lake)

    assert tree(lake / "site") == ["daily/2026-07-31.html", "daily/latest.html"]


def test_every_report_that_fails_still_gets_an_index_that_says_so(lake):
    broken(lake, "one")
    broken(lake, "two")

    site = site_of(lake)

    assert [item.status for item in site.entries] == [FAILED, FAILED] and tree(lake / "site") == ["index.html"]
    page = Cards((lake / "site" / INDEX).read_text(encoding="utf-8"))
    assert page.summary == [("chip bad", "2 failed")] and page.hrefs == []


# --------------------------------------------------------------------------- what is wrong with the whole run


def test_a_window_that_is_malformed_is_one_error_and_nothing_is_written(lake):
    three(lake)

    with pytest.raises(ConfigError, match="must be an ISO-8601 interval"):
        site_of(lake, window="not a window")

    assert not (lake / "site").exists()


def test_a_folder_that_has_no_report_is_refused_and_so_is_a_path_that_is_not_a_folder(lake):
    write(lake / "reports" / "pipeline.json", {"identifier": "p", "metadata": {}})

    with pytest.raises(ConfigError, match=r"there is no report in .*reports\. A report file has \"kind\": \"Report\""):
        site_of(lake)
    with pytest.raises(ConfigError, match="is not a folder"):
        build_site(lake / "reports" / "pipeline.json", lake / "site", WINDOW)
    with pytest.raises(ConfigError, match="is not a folder"):
        build_site(lake / "missing", lake / "site", WINDOW)

    assert not (lake / "site").exists()


def test_two_reports_with_the_same_identifier_are_refused_before_anything_is_built_whatever_their_case(lake):
    put_report(lake, "daily", name="a")
    put_report(lake, "Daily", name="b")
    put_report(lake, "weekly", name="c")

    with pytest.raises(ConfigError) as caught:
        site_of(lake)

    assert str(caught.value) == ("reports that have the same identifier would write the same folder of a site, so "
                                 "give each its own:\n  - a.json (daily), b.json (Daily)")
    assert not (lake / "site").exists()


def test_an_out_that_is_a_file_is_refused_and_the_file_is_left_as_it_was(lake):
    put_report(lake, "daily")
    target = write(lake / "site", "a file, not a folder")

    with pytest.raises(ConfigError, match="is a file, and a site is written to a folder"):
        site_of(lake)

    assert target.read_text() == "a file, not a folder"


def test_an_out_that_does_not_exist_yet_is_made_with_every_folder_it_needs(lake):
    put_report(lake, "daily")

    build_site(lake / "reports", lake / "a" / "b" / "site", WINDOW, sink=MemorySink())

    assert (lake / "a" / "b" / "site" / INDEX).is_file()
    assert (lake / "a" / "b" / "site" / "daily" / LATEST).is_file()


def test_a_site_can_be_written_to_a_file_uri_and_its_relative_links_still_hold(lake):
    put_report(lake, "daily")
    folder = lake / "a site"

    site = build_site(lake / "reports", folder.as_uri(), WINDOW, sink=MemorySink())

    assert site.index == f"{folder.as_uri()}/index.html"
    assert (folder / INDEX).is_file() and links(folder / INDEX) == ["daily/latest.html"]
    assert (folder / "daily" / "latest.html").is_file()


def ticking(start: dt.datetime):
    """A clock that is a second later every time that it is read, so that each moment of a build is its own."""
    state = {"reads": 0}

    def clock() -> dt.datetime:
        state["reads"] += 1
        return start + dt.timedelta(seconds=state["reads"])

    return clock


def test_each_card_says_the_moment_that_its_own_page_was_built_and_the_index_is_later_than_all_of_them(lake):
    three(lake)
    start = now_after()

    site = build_site(lake / "reports", lake / "site", WINDOW, clock=ticking(start), sink=MemorySink())

    built = site.built
    moments = [entry.moment for entry in built]
    assert moments == [entry.build.built_at for entry in built] and len(set(moments)) == 2
    assert all(moment > start for moment in moments) and site.built_at > max(moments)
    cards = Cards(site.document).cards
    assert [cards[1]["facts"]["Built"], cards[2]["facts"]["Built"]] == [utc_text(moment) for moment in moments]
    for entry, name in zip(built, ("daily", "weekly")):
        page = (lake / "site" / name / LATEST).read_text(encoding="utf-8")
        assert f"<dd>{utc_text(entry.moment)}</dd>" in page  # the card and the footer of the page agree
    assert f"Built {utc_text(site.built_at)} by ldp report build." in site.document
    assert site.entries[0].moment != site.built_at  # a report that failed says when it was tried


def test_the_moment_for_a_window_by_name_is_the_one_that_is_given_and_not_the_clock_of_the_build(lake):
    put_report(lake, "daily")
    counting = dt.datetime(2026, 8, 2, 1, 0, tzinfo=UTC)
    later = dt.datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

    site = build_site(lake / "reports", lake / "site", "yesterday", now=counting, clock=lambda: later,
                      sink=MemorySink())

    assert tree(lake / "site") == ["daily/2026-08-01.html", "daily/latest.html", "index.html"]
    card = Cards(site.document).cards[0]
    assert card["facts"] == {"Window": "2026-08-01 00:00 to 2026-08-02 00:00, UTC", "Built": utc_text(later)}


def test_the_site_says_where_it_was_written_as_it_was_given(lake):
    put_report(lake, "daily")

    site = build_site(str(lake / "reports"), str(lake / "site") + "/", WINDOW, sink=MemorySink())

    assert site.out == str(lake / "site") + "/" and site.index == str(lake / "site") + "/index.html"
    assert (lake / "site" / INDEX).read_text(encoding="utf-8") == site.document


def test_a_title_and_a_description_with_letters_beyond_ascii_are_written_as_utf8_in_both_pages(lake):
    put_report(lake, "cafe", title="Caf\u00e9 \u2013 orders", what="Cr\u00e8me br\u00fbl\u00e9e, by \u4e2d\u6587")

    site = site_of(lake)

    dated = (lake / "site" / "cafe" / "2026-07-31.html").read_bytes()
    assert (lake / "site" / "cafe" / LATEST).read_bytes() == dated
    assert "Caf\u00e9 \u2013 orders" in dated.decode("utf-8")
    index = (lake / "site" / INDEX).read_bytes().decode("utf-8")
    card = Cards(index).cards[0]
    assert card["title"] == "Caf\u00e9 \u2013 orders" and card["sub"] == "Cr\u00e8me br\u00fbl\u00e9e, by \u4e2d\u6587"
    assert site.document == index


def test_an_error_with_many_problems_is_shown_whole_on_the_page_and_in_what_the_command_prints(lake, capsys):
    blocks = [{"type": "kpi", "query": "totals", "value": f"missing_{number}"} for number in range(60)]
    put_report(lake, "daily", blocks=blocks)

    assert main(command(lake)) == 1

    page = Cards((lake / "site" / INDEX).read_text(encoding="utf-8")).cards[0]["pre"]
    assert len(page) > 4000 and "missing_59" in page and not page.endswith("...")
    assert "missing_59" in capsys.readouterr().err


# --------------------------------------------------------------------------- the events of the builds


def test_each_report_records_its_own_build_and_the_event_says_where_its_dated_page_went(lake):
    now = now_after()
    five(lake, now)
    sink = MemorySink()

    site_of(lake, now, sink=sink)

    assert sink.types == [
        "run.started", "report.rendered", "run.finished",  # adhoc
        "run.started", "run.failed", "run.finished",  # broken
        "run.started", "report.rendered", "run.finished",  # daily
        "run.started", "report.held", "run.finished",  # monthly
        "run.started", "report.rendered", "run.finished"]  # weekly
    rendered = [item.payload for item in sink.events if item.type == "report.rendered"]
    assert [item["output"] for item in rendered] == [str(lake / "site" / name / "2026-07-31.html")
                                                     for name in ("adhoc", "daily", "weekly")]
    assert [item["stale"] for item in rendered] == [[], [], ["gold.gold_sales"]]
    held = next(item for item in sink.events if item.type == "report.held").payload
    assert held["output"] == str(lake / "site" / "monthly" / "2026-07-31.html")


def test_the_build_of_a_report_in_a_site_goes_to_the_sinks_of_that_report_and_ldp_runs_lists_it(lake, capsys):
    put_report(lake, "daily", observability={"sinks": [{"type": "jsonl", "path": "events.jsonl"}]})

    assert main(["report", "build", str(lake / "reports"), "--window", WINDOW, "--out", str(lake / "site")]) == 0
    capsys.readouterr()

    assert main(["runs", str(lake / "reports" / "daily.json")]) == 0
    out = capsys.readouterr().out
    assert "succeeded" in out and "daily/2026-07-31.html" in out


# --------------------------------------------------------------------------- the command


def command(lake: Path, *extra: str) -> list[str]:
    return ["report", "build", str(lake / "reports"), "--window", WINDOW, "--out", str(lake / "site"), *extra]


def test_the_command_builds_a_folder_prints_a_line_for_each_report_and_exits_with_one_when_one_failed(lake, capsys):
    three(lake)

    assert main(command(lake)) == 1

    captured = capsys.readouterr()
    site = lake / "site"
    lines = captured.out.splitlines()
    assert lines[0] == "broken (broken.json): not built"
    for line, name in zip(lines[1:3], ("daily", "weekly")):
        assert re.fullmatch(rf"{name} \({name}\.json\): built {re.escape(str(site))}/{name}/2026-07-31\.html and "
                            r"latest\.html, [\d,]+ bytes, 1 query and 1 block, in \d+ ms", line)
    assert lines[3] == f"{site}/index.html: 3 reports, 2 built, 1 not built" and len(lines) == 4
    assert ("the report broken has 1 problem and was not built\n"
            "  - metadata.blocks[0].value: the query 'totals' has no column 'nope'") in captured.err


def test_the_line_of_a_report_that_cannot_be_read_is_the_name_of_its_file_and_not_an_identifier(lake, capsys):
    put_report(lake, "daily")
    write(lake / "reports" / "bad.json", {"kind": "Report", "identifier": "bad"})

    assert main(command(lake)) == 1

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "bad.json: not built" and lines[1].startswith("daily (daily.json): built ")


def test_the_line_of_a_report_counts_its_queries_and_its_blocks_and_writes_the_size_with_separators(lake, capsys):
    put_report(lake, "one")
    put_report(lake, "two", queries={"totals": QUERY, "again": QUERY},
               blocks=[*BLOCKS, {"type": "kpi", "query": "again", "value": "orders"},
                       {"type": "table", "query": "totals"}])

    assert main(command(lake)) == 0

    one, two, _ = capsys.readouterr().out.splitlines()
    assert re.search(r", \d{1,3}(,\d{3})+ bytes, 1 query and 1 block, in \d+ ms$", one), one
    assert re.search(r", \d{1,3}(,\d{3})+ bytes, 2 queries and 3 blocks, in \d+ ms$", two), two


@pytest.mark.parametrize("fresh, stale, held, failed, status", [
    (1, 0, 0, 0, 0),
    (1, 1, 0, 0, 0),  # a stale report is built with a banner, and the command succeeds as it does for one report
    (1, 0, 1, 0, 3),  # a report that was held back and nothing that failed is a data gate
    (1, 0, 0, 1, 1),
    (1, 1, 1, 1, 1),  # a failure beats a gate, as it does for a workflow
    (0, 0, 2, 0, 3),
    (0, 0, 0, 2, 1),
])
def test_the_exit_status_is_1_when_any_report_failed_and_else_3_when_any_was_held_back(
        lake, capsys, fresh, stale, held, failed, status):
    history(lake, now_after(), 40)
    for number in range(fresh):
        put_report(lake, f"fresh{number}", freshness={"max_age_hours": 48, "on_stale": "block"})
    for number in range(stale):
        put_report(lake, f"stale{number}", freshness={"max_age_hours": 26, "on_stale": "warn"})
    for number in range(held):
        put_report(lake, f"held{number}", freshness={"max_age_hours": 26, "on_stale": "block"})
    for number in range(failed):
        broken(lake, f"broken{number}")

    assert main(command(lake)) == status

    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1].startswith(str(lake / "site" / "index.html"))
    assert (lake / "site" / INDEX).is_file()  # every outcome writes the index


def test_a_held_report_is_one_line_and_its_reasons_go_to_standard_error_and_the_exit_status_is_3(lake, capsys):
    history(lake, now_after(), 40)
    put_report(lake, "daily", freshness={"max_age_hours": 48, "on_stale": "block"})
    put_report(lake, "monthly", freshness={"max_age_hours": 26, "on_stale": "block"})

    assert main(command(lake)) == 3

    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[1] == "monthly (monthly.json): held back, 1 table is stale, gold.gold_sales"
    assert lines[2] == f"{lake / 'site' / 'index.html'}: 2 reports, 1 built, 1 held back"
    assert ("the report monthly was held, because 1 table is stale and metadata.freshness says block\n"
            "  - gold.gold_sales: its last successful run was at ") in captured.err


def test_a_stale_report_prints_a_warning_on_standard_error_that_names_the_report(lake, capsys):
    history(lake, now_after(), 40)
    put_report(lake, "weekly", freshness={"max_age_hours": 26, "on_stale": "warn"})

    assert main(command(lake)) == 0

    captured = capsys.readouterr()
    assert [line for line in captured.err.splitlines() if line.startswith("warning:")] == [
        "warning: weekly: 1 table is stale, gold.gold_sales"]  # DuckDB may log a warning of its own to this stream
    assert captured.err.splitlines()[-1] == "warning: weekly: 1 table is stale, gold.gold_sales"
    assert captured.out.splitlines()[-1].endswith("index.html: 1 report, 1 built")


def test_the_summary_says_one_report_in_the_singular_and_leaves_out_what_did_not_happen(lake, capsys):
    put_report(lake, "daily")

    assert main(command(lake)) == 0

    assert capsys.readouterr().out.splitlines()[-1] == f"{lake / 'site' / 'index.html'}: 1 report, 1 built"


def test_the_command_hands_each_of_its_options_to_the_build_of_every_report_and_nothing_else(lake, monkeypatch, capsys):
    put_report(lake, "a")
    put_report(lake, "b")
    calls = []

    def record(report, window=None, **keywords):
        calls.append((report.identifier, window, keywords))
        raise RuntimeError("nothing is built")

    monkeypatch.setattr(report_site, "build_report", record)

    assert main(["report", "build", str(lake / "reports"), "--window", "2026-07-31/2026-08-01", "--param",
                 "city=Delhi", "--release", "r1", "--row-cap", "7", "--timeout", "9", "--out", str(lake / "site"),
                 "--format", "html"]) == 1
    assert main(["report", "build", str(lake / "reports"), "--live", "--out", str(lake / "other")]) == 1

    capsys.readouterr()
    first = "2026-07-31/2026-08-01"
    assert [(name, window) for name, window, _ in calls] == [("a", first), ("b", first), ("a", None), ("b", None)]
    for name, _, keywords in calls[:2]:
        assert keywords["out"] == f"{lake / 'site'}/{name}/2026-07-31.html"
        assert {key: keywords[key] for key in ("fmt", "params", "release", "live", "row_cap", "timeout_s", "home")} == {
            "fmt": "html", "params": {"city": "Delhi"}, "release": "r1", "live": False, "row_cap": 7,
            "timeout_s": 9.0, "home": "../index.html"}
    for name, _, keywords in calls[2:]:
        assert keywords["out"] == f"{lake / 'other'}/{name}/{keywords['now']:%Y-%m-%d}.html"  # the day of the build
        assert (keywords["params"], keywords["release"], keywords["live"], keywords["row_cap"],
                keywords["timeout_s"]) == (None, None, True, None, None)
    assert len({keywords["now"] for _, _, keywords in calls[:2]}) == 1  # one moment for every report's "yesterday"
    assert len({keywords["now"] for _, _, keywords in calls[2:]}) == 1


def test_the_command_refuses_a_format_other_than_html_and_a_dash_for_a_folder_and_writes_nothing(lake, capsys):
    put_report(lake, "daily")

    assert main(command(lake, "--format", "md")) == 1
    assert "a folder is built as a site of HTML pages, so --format md is only for a report file" in (
        capsys.readouterr().err)
    assert main(command(lake, "--format", "json")) == 1
    assert main(["report", "build", str(lake / "reports"), "--out", "-"]) == 1
    assert ("a folder is built as a site of files, so --out is the folder of the site, and not - for standard output"
            in capsys.readouterr().err)
    assert not (lake / "site").exists()
    assert main(command(lake, "--format", "html")) == 0


def test_the_command_needs_out_for_a_folder_too_and_says_so_before_it_builds_anything(lake, capsys):
    put_report(lake, "daily")

    assert main(["report", "build", str(lake / "reports"), "--window", WINDOW]) == 1

    assert "ldp report build needs --out, so that no file is written by surprise" in capsys.readouterr().err
    assert not (lake / "site").exists()


def test_the_command_says_when_a_folder_has_no_report_and_when_the_window_is_malformed(lake, capsys):
    assert main(command(lake)) == 1
    assert "there is no report in " in capsys.readouterr().err

    put_report(lake, "daily")
    assert main(["report", "build", str(lake / "reports"), "--window", "soon", "--out", str(lake / "site")]) == 1
    assert "must be an ISO-8601 interval" in capsys.readouterr().err
    assert not (lake / "site").exists()


def test_the_command_with_a_relative_out_writes_relative_names_and_the_links_hold(lake, monkeypatch, capsys):
    three(lake)
    monkeypatch.chdir(lake)

    assert main(["report", "build", "reports", "--window", WINDOW, "--out", "public/site"]) == 1

    out = capsys.readouterr().out
    assert "built public/site/daily/2026-07-31.html and latest.html" in out
    assert out.splitlines()[-1] == "public/site/index.html: 3 reports, 2 built, 1 not built"
    assert tree(lake / "public" / "site") == ["daily/2026-07-31.html", "daily/latest.html", "index.html",
                                              "weekly/2026-07-31.html", "weekly/latest.html"]


def test_a_single_report_file_is_still_built_as_one_page_and_has_no_link_back(lake, capsys):
    path = put_report(lake, "daily")
    out = lake / "one.html"

    assert main(["report", "build", str(path), "--window", WINDOW, "--out", str(out)]) == 0

    assert "All reports" not in out.read_text(encoding="utf-8")
    assert capsys.readouterr().out.startswith(f"daily (daily.json): built {out} as html, ")


# --------------------------------------------------------------------------- a bucket


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


def test_a_site_can_be_written_to_a_bucket_and_its_links_are_the_keys_next_to_it(lake, moto_endpoint, monkeypatch,
                                                                                 tmp_path_factory):
    boto3 = pytest.importorskip("boto3", reason="boto3 is not installed")
    for name in ("AWS_ENDPOINT_URL_S3", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    nowhere = tmp_path_factory.mktemp("no-aws")
    for name, value in (("AWS_ACCESS_KEY_ID", "testing"), ("AWS_SECRET_ACCESS_KEY", "testing"),
                        ("AWS_REGION", "us-east-1"), ("AWS_DEFAULT_REGION", "us-east-1"),
                        ("AWS_ENDPOINT_URL", moto_endpoint), ("AWS_EC2_METADATA_DISABLED", "true"),
                        ("AWS_CONFIG_FILE", str(nowhere / "config")),
                        ("AWS_SHARED_CREDENTIALS_FILE", str(nowhere / "credentials"))):
        monkeypatch.setenv(name, value)
    bucket = f"ldp-site-{uuid.uuid4().hex[:12]}"
    client = boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    three(lake)

    site = build_site(lake / "reports", f"s3://{bucket}/team/site/", WINDOW, sink=MemorySink())

    keys = sorted(item["Key"] for item in client.list_objects_v2(Bucket=bucket)["Contents"])
    assert keys == ["team/site/daily/2026-07-31.html", "team/site/daily/latest.html", "team/site/index.html",
                    "team/site/weekly/2026-07-31.html", "team/site/weekly/latest.html"]
    index = client.get_object(Bucket=bucket, Key="team/site/index.html")["Body"].read().decode("utf-8")
    assert index == site.document and site.index == f"s3://{bucket}/team/site/index.html"
    for href in Cards(index).hrefs:
        assert f"team/site/{href}" in keys
    latest = client.get_object(Bucket=bucket, Key="team/site/daily/latest.html")["Body"].read()
    dated = client.get_object(Bucket=bucket, Key="team/site/daily/2026-07-31.html")["Body"].read()
    assert latest == dated and b'<a href="../index.html">All reports</a>' in latest
    assert not client.list_multipart_uploads(Bucket=bucket).get("Uploads")
