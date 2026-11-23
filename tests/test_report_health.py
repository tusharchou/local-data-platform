"""The data health panel of a report, and the freshness gate that holds a report back when its data is stale.

The rules are tested on rows of ``ldp status`` made by hand, so every age in a test is one that the test wrote down. The
panel is tested as markup, as Markdown and as JSON. Then a gold table with a config and a history of runs is built into
reports, which shows a fresh table, a stale table with ``warn`` and with ``block``, a table with no run data, a run that
a quality check blocked, and a table that no config of the report writes. Everything is offline.
"""

import datetime as dt
import json
import re
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform.cli import main
from local_data_platform.events import MemorySink, RunEvent
from local_data_platform.exceptions import DataQualityError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.gold.build import build_gold
from local_data_platform.report import health as health_module
from local_data_platform.report.build import ReportHeld, build_report
from local_data_platform.report.formats import utc_text
from local_data_platform.report.health import (
    COLUMNS,
    STALE_RUNS,
    Health,
    TableHealth,
    banner,
    cells,
    collect,
    columns,
    judge,
    news_of,
    news_text,
    panel,
    stale_text,
)
from local_data_platform.report.render_json import render_json
from local_data_platform.report.render_md import render_md
from local_data_platform.report.spec import Freshness, load_report
from tests.report_support import (
    BUILT_AT,
    UTC,
    golden_health,
    golden_run,
    hours_before,
    make_row,
    make_run,
    make_run_of,
)

GATE = Freshness(max_age_hours=26.0, on_stale="warn")
HOSTILE = "<script>alert(1)</script> \"quoted\" & 'single'"


def stale_for(row, rule=GATE, **keywords):
    return judge("gold.gold_sales", "gold_sales", row, rule, **keywords)


# --------------------------------------------------------------------------- the news of a table


def test_the_news_of_a_table_is_the_start_of_its_last_success_and_then_its_last_commit():
    run, commit = make_row(success_hours=3, commit_hours=1), make_row(commit_hours=5)

    assert news_of(run) == (hours_before(BUILT_AT, 3), "run")
    assert news_of(commit) == (hours_before(BUILT_AT, 5), "commit")
    assert news_of(make_row()) == (None, None)


def test_the_moment_of_the_news_is_exact_to_the_microsecond_whatever_the_age():
    row = make_row(as_of=dt.datetime(2026, 8, 2, 6, 30, 15, 987654, tzinfo=UTC), success_hours=3.123456)

    moment, source = news_of(row)

    assert source == "run" and moment == row.as_of - dt.timedelta(hours=3.123456)


# --------------------------------------------------------------------------- the gate


def test_a_table_that_is_fresh_and_whose_last_run_went_well_is_not_stale():
    item = stale_for(make_row(run=make_run_of("published"), success_hours=3, commit_hours=3))

    assert (item.stale, item.reasons, item.news_from, item.news_at) == (False, (), "run", hours_before(BUILT_AT, 3))
    assert (item.table, item.pipeline, item.problem) == ("gold.gold_sales", "gold_sales", None)


@pytest.mark.parametrize("status", ["failed", "upstream_failed", "blocked_quality"])
def test_a_table_whose_last_run_failed_or_was_held_back_is_stale_however_fresh_it_is(status):
    item = stale_for(make_row(run=make_run_of(status), success_hours=1))

    assert item.stale and item.reasons == (f"its last run ended {status}",)
    assert status in STALE_RUNS


@pytest.mark.parametrize("status", ["published", "skipped_duplicate", "succeeded", "running"])
def test_a_table_whose_last_run_went_well_or_is_running_is_not_stale_for_the_run(status):
    assert not stale_for(make_row(run=make_run_of(status), success_hours=1)).stale


def test_a_run_that_a_check_blocked_names_the_checks_that_failed():
    run = make_run_of("blocked_quality", checks="3/5", failing=("not_null(city)", "unique(id)"))

    item = stale_for(make_row(run=run, success_hours=1))

    assert item.reasons == ("its last run ended blocked_quality (failing not_null(city), unique(id))",)


def test_a_table_is_stale_when_its_news_is_older_than_max_age_hours_and_not_at_the_limit():
    exactly = stale_for(make_row(run=make_run_of(), success_hours=26))
    over = stale_for(make_row(run=make_run_of(), success_hours=26.01))

    assert not exactly.stale and over.stale
    assert over.reasons == (f"its last successful run was at {utc_text(hours_before(BUILT_AT, 26.01))}, which is more "
                            "than the 26 hours that this report allows",)


@pytest.mark.parametrize(("hours", "words"), [
    (1, "1 hour"), (0.5, "0.5 hours"), (1.5, "1.5 hours"), (24.0, "24 hours")])
def test_the_hours_that_a_report_allows_are_written_in_the_singular_and_without_a_trailing_zero(hours, words):
    item = stale_for(make_row(run=make_run_of(), success_hours=100), Freshness(max_age_hours=hours))

    assert item.reasons[0].endswith(f"which is more than the {words} that this report allows")


def test_a_table_that_has_no_run_data_is_judged_by_the_last_commit_that_iceberg_says():
    old = stale_for(make_row(commit_hours=30))
    new = stale_for(make_row(commit_hours=2))

    assert old.stale and old.news_from == "commit" and not old.has_run_data
    assert old.reasons == (f"its last commit was at {utc_text(hours_before(BUILT_AT, 30))}, which is more than the "
                           "26 hours that this report allows",)
    assert not new.stale and new.news_at == hours_before(BUILT_AT, 2)


def test_a_table_that_was_never_written_is_stale_for_a_gate_that_has_an_age():
    item = stale_for(make_row())

    assert item.stale and item.reasons == ("it has never been written",) and item.news_at is None


@pytest.mark.parametrize(("verdict", "stale"), [("ok", False), ("warn", True), ("error", True), ("unknown", False),
                                                ("no sla", False), (None, False)])
def test_without_max_age_hours_the_freshness_monitor_of_the_sla_decides(verdict, stale):
    item = stale_for(make_row(run=make_run_of(), success_hours=100, freshness=verdict), Freshness(on_stale="block"))

    assert item.stale is stale
    if stale:
        assert item.reasons == (f"the freshness monitor of its SLA says {verdict}",)


def test_max_age_hours_replaces_what_the_sla_says():
    sla_late = stale_for(make_row(run=make_run_of(), success_hours=2, freshness="error"))
    sla_ok = stale_for(make_row(run=make_run_of(), success_hours=30, freshness="ok"))

    assert not sla_late.stale and sla_ok.stale


def test_every_reason_that_a_table_has_is_given():
    run = make_run_of("failed")

    item = stale_for(make_row(run=run, success_hours=40))

    assert len(item.reasons) == 2 and item.reasons[0] == "its last run ended failed"
    assert item.reasons[1].startswith("its last successful run was at ")


def test_a_report_that_has_no_gate_has_no_stale_table_whatever_the_rows_say():
    item = stale_for(make_row(run=make_run_of("failed"), success_hours=500, freshness="error"), None)

    assert (item.stale, item.reasons) == (False, ()) and item.freshness == "error"


def test_a_table_whose_health_cannot_be_read_is_stale_for_a_gate_and_only_for_a_gate():
    nothing = stale_for(None, problem="OSError: the catalog is gone")
    flagged = stale_for(make_row(problem="the config could not be evaluated"))
    no_gate = stale_for(None, None, problem="OSError: the catalog is gone")

    assert nothing.stale and nothing.reasons == ("its health could not be read (OSError: the catalog is gone)",)
    assert flagged.stale and flagged.reasons == ("its health could not be read (the config could not be evaluated)",)
    assert flagged.problem == "the config could not be evaluated" and flagged.news_at is None
    assert not no_gate.stale and no_gate.problem == "OSError: the catalog is gone" and no_gate.reasons == ()


def test_a_problem_that_carries_a_secret_is_masked_in_the_reasons_the_panel_and_the_json():
    text = "could not log in with password=hunter2 to s3://user:pw@host/bucket"

    item = stale_for(None, problem=text)
    health = Health(as_of=BUILT_AT, rule=GATE, tables=(item,))

    masked = "could not log in with password=*** to s3://user:***@host/bucket"
    assert item.problem == masked and item.reasons == (f"its health could not be read ({masked})",)
    assert cells(item, health)[1] == f"health could not be read ({masked})"
    assert "hunter2" not in json.dumps(health.to_dict()) and "hunter2" not in str(panel(health))
    assert "hunter2" not in str(ReportHeld("daily", (item,))) and "pw@" not in str(banner(health))


def test_a_table_is_judged_by_its_news_and_its_runs_and_never_by_the_age_of_its_last_failed_run():
    item = stale_for(make_row(run=make_run_of("failed", started=hours_before(BUILT_AT, 1)), success_hours=2))

    assert item.reasons == ("its last run ended failed",) and item.news_at == hours_before(BUILT_AT, 2)


# --------------------------------------------------------------------------- what a health says


def tables(*items):
    return Health(as_of=BUILT_AT, rule=GATE, tables=tuple(items))


def test_a_health_lists_the_stale_tables_and_says_whether_it_holds_the_report_or_warns():
    fresh = TableHealth(table="gold.a")
    old = TableHealth(table="gold.b", stale=True, reasons=("because",))

    warn = Health(as_of=BUILT_AT, rule=Freshness(on_stale="warn"), tables=(fresh, old))
    block = Health(as_of=BUILT_AT, rule=Freshness(on_stale="block"), tables=(fresh, old))
    quiet = Health(as_of=BUILT_AT, rule=None, tables=(fresh, old))
    clean = Health(as_of=BUILT_AT, rule=Freshness(on_stale="block"), tables=(fresh,))

    assert warn.stale == (old,) and (warn.banner, warn.held) == (True, False)
    assert (block.banner, block.held) == (False, True)
    assert (quiet.banner, quiet.held) == (False, False)
    assert (clean.stale, clean.banner, clean.held) == ((), False, False)


def test_a_health_says_in_one_line_which_tables_are_stale():
    one = TableHealth(table="gold.a", stale=True)
    two = TableHealth(table="gold.b", stale=True)

    assert Health(BUILT_AT, GATE, (one,)).summary() == "1 table is stale, gold.a"
    assert Health(BUILT_AT, GATE, (one, TableHealth(table="gold.c"), two)).summary() == (
        "2 tables are stale, gold.a, gold.b")
    assert Health(BUILT_AT, GATE, (TableHealth(table="gold.c"),)).summary() == ""


def test_the_tables_of_a_report_that_was_held_are_said_in_the_same_line_as_those_of_a_health():
    first = TableHealth(table="gold.a", stale=True, reasons=("old",))
    second = TableHealth(table="gold.b", stale=True, reasons=("older",))

    assert ReportHeld("daily", (first,)).summary() == "1 table is stale, gold.a"
    assert ReportHeld("daily", (first, second)).summary() == "2 tables are stale, gold.a, gold.b"
    assert ReportHeld("daily", (first, second)).summary() == stale_text((first, second))
    assert stale_text(()) == "" and stale_text([first]) == Health(BUILT_AT, GATE, (first,)).summary()


def test_a_report_that_is_held_is_a_data_gate_that_names_every_stale_table_with_its_reasons():
    first = TableHealth(table="gold.a", stale=True, reasons=("its last run ended failed", "it is old"))
    second = TableHealth(table="gold.b", stale=True, reasons=("it has never been written",))

    held = ReportHeld("daily", (first, second))

    assert isinstance(held, DataQualityError) and held.identifier == "daily" and held.tables == (first, second)
    assert str(held).splitlines() == [
        "the report daily was held, because 2 tables are stale and metadata.freshness says block",
        "  - gold.a: its last run ended failed and it is old", "  - gold.b: it has never been written"]
    assert str(ReportHeld("daily", (first,))).splitlines()[0] == (
        "the report daily was held, because 1 table is stale and metadata.freshness says block")


def test_the_health_is_json_with_moments_and_verdicts_and_no_ages():
    data = golden_health().to_dict()

    assert data["rule"] == {"max_age_hours": 26.0, "on_stale": "warn"} and data["stale"] == ["gold.gold_sales"]
    sales, customers = data["tables"]
    assert sales["last_run"] == {
        "run_id": "gold-sales-run-7", "status": "blocked_quality", "started_at": "2026-08-02T05:30:15Z",
        "finished_at": "2026-08-02T05:30:17.500000Z", "rows_written": 0, "checks": "4/5",
        "failing_checks": ["not_null(city)"]}
    assert (sales["pipeline"], sales["freshness"], sales["news_from"], sales["news_at"], sales["stale"]) == (
        "gold_sales", "ok", "run", "2026-08-02T03:30:15Z", True)
    assert sales["reasons"] == ["its last run ended blocked_quality (failing not_null(city))"]
    assert customers["table"] == "gold.gold_customers" and customers["last_run"] is None
    assert customers["no_run_data"] is True and customers["stale"] is False and customers["health"] is None
    assert "age_seconds" not in json.dumps(data) and "_age" not in json.dumps(sales)
    assert json.loads(json.dumps(data)) == data


# --------------------------------------------------------------------------- the cells


def test_a_row_of_the_panel_has_the_last_run_its_status_its_rows_when_it_finished_and_its_checks():
    health = golden_health()

    assert columns(health) == [*COLUMNS, "Stale"] and COLUMNS[0] == "Table"
    assert cells(health.tables[0], health) == [
        "gold.gold_sales", "2026-08-02 05:30:15 UTC", "blocked_quality", "0", "2026-08-02 05:30:17 UTC",
        "4/5, failing not_null(city)", "ok, last success 2026-08-02 03:30:15 UTC", "yes"]


def test_a_table_with_no_run_data_says_so_and_one_that_no_config_writes_says_that_instead():
    health = Health(as_of=BUILT_AT, rule=GATE, tables=(
        stale_for(make_row(commit_hours=2, freshness="no sla")), TableHealth(table="gold.other")))

    assert cells(health.tables[0], health) == ["gold.gold_sales", "no run data", "-", "-", "-", "-",
                                               "no sla, last commit 2026-08-02 04:30:15 UTC", "no"]
    assert cells(health.tables[1], health) == ["gold.other", "no pipeline", "-", "-", "-", "-", "-", "no"]


def test_a_table_whose_health_could_not_be_read_says_so_in_the_cell_of_its_last_run():
    health = Health(as_of=BUILT_AT, rule=None, tables=(stale_for(None, None, problem="boom"),))

    assert cells(health.tables[0], health)[1] == "health could not be read (boom)"
    assert columns(health) == list(COLUMNS)


def test_a_run_is_written_with_its_rows_in_thousands_its_checks_and_when_it_finished_or_that_it_is_running():
    done = make_run_of("published", rows=1234567, checks="5/5")
    running = make_run_of("running", rows=None, checks=None, duration=None)
    unfinished = make_run_of("published", duration=None)
    health = Health(as_of=BUILT_AT, rule=None, tables=tuple(
        stale_for(make_row(run=item, success_hours=3), None) for item in (done, running, unfinished)))

    one, two, three = (cells(item, health) for item in health.tables)

    assert one[3:6] == ["1,234,567", "2026-08-02 03:30:17 UTC", "5/5"]
    assert two[2:6] == ["running", "-", "running", "-"]
    assert three[4] == "-"


def test_the_news_of_a_table_is_said_with_where_it_comes_from_and_when():
    run = TableHealth(table="a", news_at=BUILT_AT, news_from="run")
    commit = TableHealth(table="a", news_at=BUILT_AT, news_from="commit")

    assert news_text(run) == "last success 2026-08-02 06:30:15 UTC"
    assert news_text(commit) == "last commit 2026-08-02 06:30:15 UTC" and news_text(TableHealth(table="a")) == "-"


# --------------------------------------------------------------------------- the page


def test_the_panel_has_one_row_for_each_table_with_chips_for_the_status_and_the_verdicts():
    text = str(panel(golden_health()))

    assert text.startswith('<section class="card" aria-labelledby="health-h">')
    assert '<h2 id="health-h">Data health' in text
    assert "One row for each table that this report read, measured when it was built." in text
    assert text.count("<tr>") == 3 and text.count("<th ") == 8
    assert '<span class="chip warn">blocked_quality</span>' in text and '<span class="chip ok">ok</span>' in text
    assert '<span class="chip bad">yes</span>' in text and "<td>no pipeline</td>" in text
    assert '<td class="num">0</td>' in text and "<code>gold.gold_customers</code>" in text


def test_the_panel_of_a_report_with_no_gate_has_no_stale_column_and_one_with_no_table_says_so():
    quiet = Health(as_of=BUILT_AT, rule=None, tables=(TableHealth(table="gold.a"),))
    empty = Health(as_of=BUILT_AT, rule=GATE, tables=())

    assert "Stale" not in str(panel(quiet)) and str(panel(quiet)).count("<th ") == 7
    assert "This report read no table, so there is no data to check." in str(panel(empty))
    assert "<table>" not in str(panel(empty))


def test_a_banner_names_the_stale_tables_with_their_reasons_and_only_for_a_gate_that_warns():
    warn = golden_health()

    text = str(banner(warn))

    assert text.startswith('<div class="banner" role="status">') and "<strong>Stale data.</strong>" in text
    assert "<li><code>gold.gold_sales</code> its last run ended blocked_quality (failing not_null(city))</li>" in text
    assert "gold.gold_customers" not in text
    assert banner(Health(as_of=BUILT_AT, rule=Freshness(on_stale="block"), tables=warn.tables)) is None
    assert banner(Health(as_of=BUILT_AT, rule=None, tables=warn.tables)) is None
    assert banner(Health(as_of=BUILT_AT, rule=GATE, tables=(TableHealth(table="gold.a"),))) is None


def test_every_value_of_a_health_is_escaped_on_the_page_in_the_markdown_and_in_the_json():
    item = TableHealth(table=HOSTILE, stale=True, reasons=(HOSTILE,))
    health = Health(as_of=BUILT_AT, rule=GATE, tables=(item,))
    run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})}, health=health)

    from local_data_platform.report.render_html import render_html

    text = render_html(run, built_at=BUILT_AT)
    markdown = render_md(run, built_at=BUILT_AT)

    assert "<script" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert f"`{HOSTILE}` &lt;script&gt;alert(1)&lt;/script&gt;" in markdown  # the name is code, the reason is escaped
    assert "<script" not in markdown.replace(f"`{HOSTILE}`", "")
    assert json.loads(render_json(run, built_at=BUILT_AT))["health"]["tables"][0]["table"] == HOSTILE


def test_the_banner_comes_before_the_blocks_and_the_panel_after_them_on_the_page_and_in_the_markdown():
    from local_data_platform.report.render_html import render_html

    text = render_html(golden_run(), built_at=BUILT_AT)
    markdown = render_md(golden_run(), built_at=BUILT_AT)

    places = [text.index(part) for part in ('class="banner"', 'class="kpis"', 'id="health-h"', "<footer")]
    assert places == sorted(places)
    assert markdown.index("> **Stale data.**") < markdown.index("- **Revenue**") < markdown.index("## Data health")
    assert markdown.index("## Data health") < markdown.index("## How this report was made")


def test_the_markdown_panel_names_a_table_as_code_and_aligns_the_rows_to_the_right():
    markdown = render_md(golden_run(), built_at=BUILT_AT)

    assert ("| Table | Last run | Status | Rows written | Finished | Checks | Freshness | Stale |\n"
            "| --- | --- | --- | ---: | --- | --- | --- | --- |") in markdown
    assert ("| `gold.gold_sales` | 2026-08-02 05:30:15 UTC | blocked\\_quality | 0 | 2026-08-02 05:30:17 UTC |"
            in markdown)
    assert "| `gold.gold_customers` | no pipeline | - | - | - | - | - | no |" in markdown
    assert "*One row for each table that this report read, measured when it was built.*" in markdown


def test_the_json_document_has_the_health_after_the_tables_and_a_run_with_none_has_null():
    data = json.loads(render_json(golden_run(), built_at=BUILT_AT))
    plain = json.loads(render_json(make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})}),
                                   built_at=BUILT_AT))

    keys = list(data)
    assert keys.index("health") == keys.index("tables") + 1 and data["health"]["stale"] == ["gold.gold_sales"]
    assert plain["health"] is None


def test_a_run_that_has_no_health_has_no_banner_and_no_panel():
    from local_data_platform.report.render_html import render_html

    run = make_run([{"type": "text", "text": "Words."}], {"q": pa.table({"v": [1]})})

    assert 'class="banner"' not in render_html(run, built_at=BUILT_AT) and "Data health" not in render_md(run)


# --------------------------------------------------------------------------- a lake with run history

CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
SCHEMA = pa.schema([("order_date", pa.date32()), ("city", pa.string()), ("orders", pa.int64())])
ROWS = [(dt.date(2026, 7, 31), "Pune", 3), (dt.date(2026, 7, 31), "Delhi", 1), (dt.date(2026, 8, 1), "Pune", 4)]
QUERY = {"sql": "SELECT sum(orders) AS orders FROM gold.gold_sales"}


def write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def gold_config(name: str, *, observe: bool = True, sla: dict | None = None) -> dict:
    metadata = {
        "source": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": name, "format": "ICEBERG", "catalog": CATALOG},
        "docs": {"layer": "gold", "description": f"The table {name}.", "grain": "one row per order"}}
    if observe:
        metadata["observability"] = {"sinks": ["jsonl"]}
    if sla:
        metadata["sla"] = sla
    return {"identifier": name, "who": "ops", "metadata": metadata}


def put(lake: Path, name: str) -> None:
    table = pa.table({column: [row[place] for row in ROWS] for place, column in enumerate(SCHEMA.names)},
                     schema=SCHEMA)
    Iceberg(name, {"identifier": "gold", "warehouse_path": str(lake / "config" / "warehouse")}).put(table)


@pytest.fixture
def lake(tmp_path):
    """``config/`` with the gold table ``gold_sales`` and its config, which keeps its run events in a jsonl file."""
    put(tmp_path, "gold_sales")
    write(tmp_path / "config" / "gold_sales.json", gold_config("gold_sales"))
    (tmp_path / "reports").mkdir()
    return tmp_path


def event(kind: str, number: int, moment: dt.datetime, seq: int = 0, **payload) -> RunEvent:
    return RunEvent(event_id=f"gold_sales-{number}-{kind}-{seq}", type=kind, schema_version=1, ts=moment,
                    run_id=f"gold-sales-run-{number}", attempt=1, seq=seq,
                    payload={"pipeline": "gold_sales", "table": "gold.gold_sales", **payload})


def run_events(number: int, started: dt.datetime, status: str = "published", rows: int = 100,
               failing: tuple[str, ...] = (), checks: int = 3) -> list[RunEvent]:
    """The events of one run of ``gold_sales`` that ended with ``status``, which ran ``checks`` checks."""
    results = [{"name": name, "column": "orders", "passed": False, "failing_rows": 1, "details": "d", "metrics": {}}
               for name in failing]
    results += [{"name": f"check_{place}(orders)", "column": "orders", "passed": True, "failing_rows": 0,
                 "details": "d", "metrics": {}} for place in range(checks - len(failing))]
    second = dt.timedelta(seconds=1)
    events = [event("run.started", number, started, mode="overwrite", publish=True),
              event("run.extracted", number, started + second, 1, rows_read=rows),
              event("quality.evaluated", number, started + 2 * second, 2, checks_run=len(results),
                    checks_failed=len(failing), passed=not failing, results=results)]
    if status == "failed":
        events.append(event("run.failed", number, started + 3 * second, 3, stage="extract", error_type="OSError",
                            message="boom"))
    else:
        events.append(event(f"run.{status}", number, started + 3 * second, 3, rows_written=rows,
                            snapshot_id=1000 + number, failures=list(failing)))
    events.append(event("run.finished", number, started + 4 * second, 9, status=status, rows_written=rows,
                        duration_s=4.0))
    return events


def history(lake: Path, *runs: list[RunEvent]) -> None:
    """Append the events of the runs to the file of the jsonl sink next to the configs."""
    folder = lake / "config" / ".ldp"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "events.jsonl", "a", encoding="utf-8") as handle:
        handle.writelines(item.to_json() + "\n" for run in runs for item in run)


def report_of(lake: Path, *, freshness=None, **metadata):
    spec = {"apiVersion": "ldp/v1", "kind": "Report", "identifier": "daily", "what": "Orders.",
            "metadata": {"title": "Daily orders", "tables": {"config": ["../config"], "allow": ["gold.*"]},
                         "queries": {"totals": QUERY},
                         "blocks": [{"type": "kpi", "query": "totals", "value": "orders"}], **metadata}}
    if freshness is not None:
        spec["metadata"]["freshness"] = freshness
    return load_report(write(lake / "reports" / "daily.json", spec))


def build(lake: Path, now: dt.datetime, *, out="site/daily.html", fmt="html", sink=None, **spec):
    where = lake / out if out and out != "-" else out
    return build_report(report_of(lake, **spec), None, out=where, fmt=fmt, clock=lambda: now,
                        sink=sink if sink is not None else MemorySink())


def now_after(lake: Path) -> dt.datetime:
    """A moment after the table was written, which is the clock of a build that is not meant to be in the past."""
    return dt.datetime.now(UTC) + dt.timedelta(seconds=2)


def only(health: Health) -> TableHealth:
    assert [item.table for item in health.tables] == ["gold.gold_sales"]
    return health.tables[0]


def test_a_fresh_table_with_a_good_last_run_shows_its_run_and_is_not_stale(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 30)), run_events(2, hours_before(now, 3), rows=1234))

    result = build(lake, now, freshness={"max_age_hours": 26, "on_stale": "warn"})

    item = only(result.health)
    assert (item.stale, item.reasons, item.pipeline, item.news_from) == (False, (), "gold_sales", "run")
    assert item.news_at == hours_before(now, 3)
    run = item.last_run
    assert (run.status, run.rows_written, run.checks, run.failing_checks) == ("published", 1234, "3/3", ())
    assert utc_text(run.finished_at) == utc_text(hours_before(now, 3) + dt.timedelta(seconds=4))
    page = (lake / "site" / "daily.html").read_text(encoding="utf-8")
    assert 'class="banner"' not in page and "<code>gold.gold_sales</code>" in page
    assert '<span class="chip ok">published</span>' in page and '<td class="num">1,234</td>' in page
    assert "<td>3/3</td>" in page and '<td><span class="chip bad">yes</span></td>' not in page


def test_a_table_that_is_too_old_is_stale_and_a_gate_that_warns_puts_a_banner_on_the_page_and_builds_it(lake, capsys):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 40)))
    path = report_of(lake, freshness={"max_age_hours": 26, "on_stale": "warn"}).file
    out = lake / "site" / "daily.html"

    assert main(["report", "build", str(path), "--out", str(out)]) == 0

    captured = capsys.readouterr()
    assert "warning: 1 table is stale, gold.gold_sales" in captured.err
    assert captured.out.startswith("daily (daily.json): built ")
    page = out.read_text(encoding="utf-8")
    assert '<div class="banner" role="status">' in page and "<strong>Stale data.</strong>" in page
    assert "its last successful run was at " in page
    assert "which is more than the 26 hours that this report allows" in page
    assert '<span class="chip bad">yes</span>' in page


def test_the_warning_goes_to_standard_error_even_when_the_document_goes_to_standard_output(lake, capsys):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 40)))
    path = report_of(lake, freshness={"max_age_hours": 26, "on_stale": "warn"}).file

    assert main(["report", "build", str(path), "--out", "-", "--format", "json"]) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)["health"]["stale"] == ["gold.gold_sales"]
    assert captured.err.splitlines()[-1] == "warning: 1 table is stale, gold.gold_sales"


def test_a_gate_that_blocks_writes_nothing_leaves_the_file_that_was_there_and_emits_report_held(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 40)))
    target = write(lake / "site" / "daily.html", "the page of yesterday")
    sink = MemorySink()

    with pytest.raises(ReportHeld) as caught:
        build(lake, now, out="site/daily.html", sink=sink, freshness={"max_age_hours": 26, "on_stale": "block"})

    assert target.read_text() == "the page of yesterday" and sorted(path.name for path in target.parent.iterdir()) == [
        "daily.html"]
    assert [item.table for item in caught.value.tables] == ["gold.gold_sales"]
    assert sink.types == ["run.started", "report.held", "run.finished"] and sink.flushes == 1
    held, finished = sink.events[1].payload, sink.events[2].payload
    assert held["output"] == str(target) and held["format"] == "html" and held["timezone"] == "UTC"
    assert held["rule"] == {"max_age_hours": 26.0, "on_stale": "block"} and held["window"] is None
    assert held["tables"] == [{"table": "gold.gold_sales", "pipeline": "gold_sales",
                               "reasons": [caught.value.tables[0].reasons[0]]}]
    assert finished["status"] == "blocked_quality" and "rows_read" not in finished
    assert "Pune" not in json.dumps([event.to_dict() for event in sink.events])


def test_a_held_report_exits_with_3_says_why_and_prints_nothing_to_standard_output(lake, capsys):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 3), status="blocked_quality", failing=("unique(order_date)",),
                             checks=4))
    path = report_of(lake, freshness={"max_age_hours": 26, "on_stale": "block"}).file
    out = lake / "site" / "daily.html"

    assert main(["report", "build", str(path), "--out", str(out)]) == 3
    assert main(["report", "build", str(path), "--out", "-"]) == 3

    captured = capsys.readouterr()
    assert captured.out == "" and not out.exists() and not out.parent.exists()
    assert ("ldp: error: the report daily was held, because 1 table is stale and metadata.freshness says block"
            in captured.err)
    assert "  - gold.gold_sales: its last run ended blocked_quality (failing unique(order_date))" in captured.err


def test_a_gate_that_blocks_lets_a_fresh_report_through_and_records_that_it_was_not_held(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 3)))
    sink = MemorySink()

    result = build(lake, now, sink=sink, freshness={"max_age_hours": 26, "on_stale": "block"})

    assert sink.types == ["run.started", "report.rendered", "run.finished"]
    assert result.health.stale == () and sink.events[1].payload["stale"] == []


def test_a_report_that_warns_records_the_stale_tables_in_the_event_that_says_it_was_written(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 40)))
    sink = MemorySink()

    build(lake, now, sink=sink, freshness={"max_age_hours": 26, "on_stale": "warn"})

    assert sink.events[1].payload["stale"] == ["gold.gold_sales"]


def test_a_run_that_a_quality_check_blocked_makes_the_table_stale_and_names_the_check(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 10)),
            run_events(2, hours_before(now, 2), status="blocked_quality", rows=0, failing=("unique(order_date)",),
                       checks=4))

    result = build(lake, now, freshness={"max_age_hours": 26, "on_stale": "warn"})

    item = only(result.health)
    run = item.last_run
    assert (run.status, run.checks, run.failing_checks) == ("blocked_quality", "3/4", ("unique(order_date)",))
    assert item.stale and item.reasons == ("its last run ended blocked_quality (failing unique(order_date))",)
    page = result.document
    assert '<span class="chip warn">blocked_quality</span>' in page
    assert "<td>3/4, failing unique(order_date)</td>" in page


def test_a_failed_run_makes_the_table_stale(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 2), status="failed"))

    item = only(build(lake, now, freshness={"on_stale": "warn"}).health)

    assert item.stale and item.reasons == ("its last run ended failed",)


def test_a_table_with_no_run_data_says_so_and_the_gate_uses_the_last_commit_that_iceberg_says(lake):
    now = now_after(lake)

    fresh = build(lake, now, freshness={"max_age_hours": 26, "on_stale": "warn"})
    old = build(lake, now + dt.timedelta(hours=30), freshness={"max_age_hours": 26, "on_stale": "warn"})

    item, late = only(fresh.health), only(old.health)
    assert not item.has_run_data and item.news_from == "commit" and not item.stale
    assert item.news_at is not None and now - item.news_at < dt.timedelta(minutes=5)
    assert late.stale and late.reasons[0].startswith("its last commit was at ")
    assert "<td>no run data</td>" in fresh.document and "no sla, last commit " in fresh.document


def test_the_news_of_a_table_that_has_run_data_is_its_last_success_and_not_its_last_commit(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 20)))

    item = only(build(lake, now, freshness={"max_age_hours": 26, "on_stale": "warn"}).health)

    assert item.news_from == "run" and item.news_at == hours_before(now, 20) and not item.stale


def test_without_max_age_hours_the_sla_of_the_pipeline_decides(lake):
    now = now_after(lake)
    write(lake / "config" / "gold_sales.json",
          gold_config("gold_sales", sla={"freshness": {"warn_after": {"count": 6, "period": "hour"},
                                                       "error_after": {"count": 26, "period": "hour"}}}))
    history(lake, run_events(1, hours_before(now, 2)))

    fresh = build(lake, now, freshness={"on_stale": "warn"})
    late = build(lake, now + dt.timedelta(hours=30), freshness={"on_stale": "warn"})

    assert only(fresh.health).freshness == "ok" and not only(fresh.health).stale
    assert only(late.health).freshness == "error"
    assert only(late.health).reasons == ("the freshness monitor of its SLA says error",)


def test_a_report_with_no_freshness_has_the_panel_and_no_gate_however_stale_its_data_is(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 500), status="failed"))
    sink = MemorySink()

    result = build(lake, now, sink=sink)

    item = only(result.health)
    assert item.last_run.status == "failed" and not item.stale and item.reasons == ()
    assert 'class="banner"' not in result.document and "Data health" in result.document
    assert "<th scope=\"col\">Stale</th>" not in result.document and sink.types[1] == "report.rendered"


def test_the_markdown_and_the_json_of_a_build_have_the_banner_and_the_health(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 40)))

    markdown = build(lake, now, out=None, fmt="md", freshness={"max_age_hours": 26, "on_stale": "warn"}).document
    data = json.loads(build(lake, now, out=None, fmt="json",
                            freshness={"max_age_hours": 26, "on_stale": "warn"}).document)

    assert "> **Stale data.**" in markdown and "## Data health" in markdown and "| `gold.gold_sales` |" in markdown
    table = data["health"]["tables"][0]
    assert table["table"] == "gold.gold_sales" and table["stale"] is True and table["pipeline"] == "gold_sales"
    assert table["last_run"]["status"] == "published" and data["health"]["rule"]["on_stale"] == "warn"
    assert data["tables"][0]["pipeline"] == "gold_sales"


def test_a_table_that_no_config_of_the_report_writes_is_a_row_that_says_so_and_is_judged_by_its_commit(lake):
    now = now_after(lake)
    put(lake, "gold_extra")
    write(lake / "catalog.json", {"catalog": CATALOG | {"warehouse_path": str(lake / "config" / "warehouse")}})
    queries = {"totals": QUERY, "extra": {"sql": "SELECT count(*) AS n FROM gold.gold_extra"}}
    blocks = [{"type": "kpi", "query": "totals", "value": "orders"}, {"type": "kpi", "query": "extra", "value": "n"}]
    kept = {"queries": queries, "blocks": blocks, "tables": {"config": ["../config"], "catalog": ["../catalog.json"],
                                                             "allow": ["gold.*"]}}
    history(lake, run_events(1, hours_before(now, 3)))

    fresh = build(lake, now, freshness={"max_age_hours": 26, "on_stale": "warn"}, **kept)
    old = build(lake, now + dt.timedelta(hours=30), freshness={"max_age_hours": 26, "on_stale": "warn"}, **kept)
    quiet = build(lake, now + dt.timedelta(hours=30), **kept)

    extra = fresh.health.tables[0]
    assert [item.table for item in fresh.health.tables] == ["gold.gold_extra", "gold.gold_sales"]
    assert (extra.pipeline, extra.row, extra.has_run_data, extra.stale, extra.news_from) == (
        None, None, False, False, "commit")
    assert "<td>no pipeline</td>" in fresh.document
    stale = old.health.tables[0]
    assert stale.stale and stale.reasons[0].startswith("its last commit was at ")
    assert [item.table for item in old.health.stale] == ["gold.gold_extra", "gold.gold_sales"]
    assert quiet.health.tables[0].news_at is None and not quiet.health.stale  # no rule to ask the monitor with


def test_a_table_whose_health_cannot_be_read_is_a_row_with_the_reason_and_stale_for_a_gate(lake, monkeypatch):
    now = now_after(lake)

    def broken(file, moment):
        raise OSError("the catalog is gone")

    monkeypatch.setattr("local_data_platform.monitoring.status.rows", broken)

    result = build(lake, now, freshness={"on_stale": "warn"})

    item = only(result.health)
    assert item.row is None and item.stale and item.problem == "OSError: the catalog is gone"
    assert item.reasons == ("its health could not be read (OSError: the catalog is gone)",)
    assert "<td>health could not be read (OSError: the catalog is gone)</td>" in result.document


def test_a_report_that_reads_no_table_has_a_panel_that_says_so(lake):
    now = now_after(lake)

    blocks = [{"type": "kpi", "query": "one", "value": "n"}]

    result = build(lake, now, queries={"one": {"sql": "SELECT 1 AS n"}}, blocks=blocks,
                   freshness={"max_age_hours": 26, "on_stale": "block"})

    assert result.health.tables == () and "This report read no table, so there is no data to check." in result.document


def test_the_health_of_a_build_is_measured_at_the_time_of_the_build_and_not_at_the_time_of_the_window(lake):
    now = now_after(lake)
    history(lake, run_events(1, hours_before(now, 3)))

    result = build_report(report_of(lake, freshness={"max_age_hours": 26, "on_stale": "warn"}), "2026-07-31/2026-08-02",
                          out=None, clock=lambda: now, sink=MemorySink())

    assert result.health.as_of == now and only(result.health).news_at == hours_before(now, 3)


def test_the_page_of_a_build_with_a_panel_is_still_the_same_text_whatever_time_it_is_built(lake):
    first = now_after(lake)
    history(lake, run_events(1, hours_before(first, 3)))
    later = first + dt.timedelta(hours=2, minutes=7)

    one = build(lake, first, out=None, freshness={"max_age_hours": 26, "on_stale": "warn"}).document
    two = build(lake, later, out=None, freshness={"max_age_hours": 26, "on_stale": "warn"}).document

    changed = [(a, b) for a, b in zip(one.splitlines(), two.splitlines()) if a != b]
    assert len(one.splitlines()) == len(two.splitlines())
    assert changed == [(f"<dd>{utc_text(first)}</dd>", f"<dd>{utc_text(later)}</dd>")]


def test_the_tables_of_a_health_are_in_the_order_of_their_names(lake):
    now = now_after(lake)
    put(lake, "gold_alpha")
    write(lake / "config" / "gold_alpha.json", gold_config("gold_alpha", observe=False))
    queries = {"totals": QUERY, "alpha": {"sql": "SELECT count(*) AS n FROM gold.gold_alpha"}}
    blocks = [{"type": "kpi", "query": "totals", "value": "orders"}, {"type": "kpi", "query": "alpha", "value": "n"}]

    result = build(lake, now, queries=queries, blocks=blocks)

    assert [item.table for item in result.health.tables] == ["gold.gold_alpha", "gold.gold_sales"]
    assert [item.pipeline for item in result.health.tables] == ["gold_alpha", "gold_sales"]


def test_collect_reads_the_pipelines_of_the_report_and_only_those_that_wrote_a_table_it_read(lake):
    now = now_after(lake)
    put(lake, "gold_other")
    write(lake / "config" / "gold_other.json", gold_config("gold_other"))
    write(lake / "config" / "notes.json", {"kind": "Metrics", "identifier": "m"})  # a file of another kind
    report = report_of(lake)
    from local_data_platform.report.engine import run_queries

    run = run_queries(report)
    health = collect(report, run.reads, now)

    assert [item.table for item in health.tables] == ["gold.gold_sales"]
    assert run.reads["gold.gold_sales"].pipeline == "gold_sales" and health.as_of == now and health.rule is None


def test_the_release_that_a_report_read_is_not_what_the_panel_describes(lake):
    now = now_after(lake)
    build_gold(lake / "config", release=True)
    history(lake, run_events(1, hours_before(now, 3)))

    result = build(lake, now, freshness={"max_age_hours": 26, "on_stale": "warn"})

    assert result.run.reads["gold.gold_sales"].release == "r1"
    assert only(result.health).last_run.run_id == "gold-sales-run-1"
    assert health_module.STALE_RUNS == ("failed", "upstream_failed", "blocked_quality")


def test_the_golden_health_has_one_stale_table_and_one_that_no_pipeline_writes():
    health = golden_health()

    assert [item.table for item in health.tables] == ["gold.gold_sales", "gold.gold_customers"]
    assert [item.stale for item in health.tables] == [True, False] and re.search(r"26", str(health.rule.max_age_hours))


# --------------------------------------------------------------------------- what the mutations asked for


@pytest.mark.parametrize(("status", "kind"), [
    ("published", "ok"), ("skipped_duplicate", "ok"), ("succeeded", "ok"), ("blocked_quality", "warn"),
    ("failed", "bad"), ("upstream_failed", "bad"), ("running", None)])
def test_a_status_of_a_run_has_the_colour_of_a_chip_that_says_how_bad_it_is(status, kind):
    item = stale_for(make_row(run=make_run_of(status), success_hours=1), None)

    text = str(panel(Health(as_of=BUILT_AT, rule=None, tables=(item,))))

    assert f'<span class="chip{" " + kind if kind else ""}">{status}</span>' in text


@pytest.mark.parametrize(("verdict", "kind"), [("ok", "ok"), ("warn", "warn"), ("error", "bad"), ("unknown", None)])
def test_a_verdict_of_the_freshness_monitor_has_the_colour_of_a_chip_that_says_how_bad_it_is(verdict, kind):
    item = stale_for(make_row(run=make_run_of(), success_hours=1, freshness=verdict), None)

    text = str(panel(Health(as_of=BUILT_AT, rule=None, tables=(item,))))

    assert f'<span class="chip{" " + kind if kind else ""}">{verdict}</span>' in text


def test_an_age_of_zero_is_an_age_and_not_a_missing_one():
    assert news_of(make_row(success_hours=0)) == (BUILT_AT, "run")
    assert news_of(make_row(commit_hours=0)) == (BUILT_AT, "commit")


def test_the_reason_that_a_caller_gives_comes_before_the_one_of_the_row_and_a_row_with_a_problem_has_no_news():
    row = make_row(problem="the row says so", success_hours=3, commit_hours=3)

    given = stale_for(row, problem="the caller says so")
    own = stale_for(row)

    assert given.reasons == ("its health could not be read (the caller says so)",)
    assert own.reasons == ("its health could not be read (the row says so)",)
    assert own.news_at is None and own.news_from is None and given.problem == "the caller says so"


def test_the_cells_of_a_row_are_as_many_as_the_headings_with_a_gate_and_without_one():
    item = stale_for(make_row(run=make_run_of(), success_hours=3), None)
    gate = Health(as_of=BUILT_AT, rule=GATE, tables=(item,))
    quiet = Health(as_of=BUILT_AT, rule=None, tables=(item,))

    assert len(cells(item, gate)) == len(columns(gate)) == 8
    assert len(cells(item, quiet)) == len(columns(quiet)) == 7


def test_the_markdown_of_a_report_with_no_gate_has_the_panel_and_no_banner_and_one_with_no_table_says_so():
    quiet = Health(as_of=BUILT_AT, rule=None, tables=golden_health().tables)
    empty = Health(as_of=BUILT_AT, rule=GATE, tables=())
    blocks = [{"type": "text", "text": "Words."}]
    rows = {"q": pa.table({"v": [1]})}

    plain = render_md(make_run(blocks, rows, health=quiet), built_at=BUILT_AT)
    nothing = render_md(make_run(blocks, rows, health=empty), built_at=BUILT_AT)

    assert "## Data health" in plain and "Stale data" not in plain and "| Stale |" not in plain
    assert ("| Table | Last run | Status | Rows written | Finished | Checks | Freshness |\n"
            "| --- | --- | --- | ---: | --- | --- | --- |") in plain
    assert "| `gold.gold_sales` |" in plain
    assert "This report read no table, so there is no data to check." in nothing and "| Table |" not in nothing


def test_a_stale_table_is_named_in_the_banner_of_the_markdown_only_for_a_gate_that_warns():
    block = Health(as_of=BUILT_AT, rule=Freshness(on_stale="block"), tables=golden_health().tables)
    blocks = [{"type": "text", "text": "Words."}]

    markdown = render_md(make_run(blocks, {"q": pa.table({"v": [1]})}, health=block), built_at=BUILT_AT)

    assert "> **Stale data.**" not in markdown and "## Data health" in markdown


def test_the_stylesheet_keeps_a_date_and_a_time_together_with_the_rule_that_the_panel_uses():
    from local_data_platform import html as shell

    assert ".nowrap{white-space:nowrap}" in shell.CSS
    assert str(panel(golden_health())).count('<span class="nowrap">') >= 6


def test_a_file_that_says_nothing_of_a_pipeline_is_a_row_with_the_reason_and_the_row_of_a_file_is_its_first(
        lake, monkeypatch):
    now = now_after(lake)
    report = report_of(lake, freshness={"on_stale": "warn"})
    from local_data_platform.report.engine import run_queries

    reads = run_queries(report).reads
    worse, better = make_row(problem="first"), make_row(pipeline="gold_sales")

    monkeypatch.setattr("local_data_platform.monitoring.status.rows", lambda file, moment: [])
    empty = collect(report, reads, now).tables[0]
    monkeypatch.setattr("local_data_platform.monitoring.status.rows", lambda file, moment: [worse, better])
    two = collect(report, reads, now).tables[0]
    monkeypatch.setattr("local_data_platform.monitoring.status.rows", lambda file, moment: (_ for _ in ()).throw(
        ValueError("boom")))
    broken = collect(report, reads, now).tables[0]

    assert empty.problem == "gold_sales.json has no pipeline" and empty.stale
    assert two.row is worse and two.problem == "first"
    assert broken.problem == "ValueError: boom" and broken.stale


def test_a_folder_of_the_report_that_has_no_dataset_config_is_left_alone_and_the_first_folder_names_a_pipeline(lake):
    now = now_after(lake)
    write(lake / "metrics_only" / "metrics.json", {"kind": "Metrics", "identifier": "m"})
    write(lake / "second" / "gold_sales.json", gold_config("gold_sales"))
    tables = {"config": ["../config", "../metrics_only", "../second"], "allow": ["gold.*"]}
    report = report_of(lake, tables=tables)

    files = health_module._config_files(report)
    from local_data_platform.report.engine import run_queries

    health = collect(report, run_queries(report).reads, now)

    assert files == {"gold_sales": lake / "config" / "gold_sales.json"}
    assert [item.table for item in health.tables] == ["gold.gold_sales"]


def catalog_lake(lake: Path) -> dict:
    """The keys of a report that reads ``gold_extra``, which only a catalog spec of the lake names."""
    put(lake, "gold_extra")
    write(lake / "catalog.json", {"catalog": CATALOG | {"warehouse_path": str(lake / "config" / "warehouse")}})
    queries = {"totals": QUERY, "extra": {"sql": "SELECT count(*) AS n FROM gold.gold_extra"}}
    blocks = [{"type": "kpi", "query": "totals", "value": "orders"}, {"type": "kpi", "query": "extra", "value": "n"}]
    return {"queries": queries, "blocks": blocks, "tables": {"config": ["../config"], "catalog": ["../catalog.json"],
                                                             "allow": ["gold.*"]}}


def test_a_table_that_no_config_writes_is_judged_by_the_minutes_of_a_fraction_of_an_hour(lake):
    now = now_after(lake)
    kept = catalog_lake(lake)
    rule = {"max_age_hours": 0.5, "on_stale": "warn"}

    early = build(lake, now + dt.timedelta(minutes=20), freshness=rule, **kept)
    late = build(lake, now + dt.timedelta(minutes=40), freshness=rule, **kept)

    assert not early.health.tables[0].stale and late.health.tables[0].stale
    assert late.health.tables[0].reasons[0].endswith("which is more than the 0.5 hours that this report allows")


def test_a_gate_with_no_max_age_hours_has_no_verdict_for_a_table_that_no_config_writes_and_does_not_fail(lake):
    now = now_after(lake)
    kept = catalog_lake(lake)

    result = build(lake, now + dt.timedelta(hours=500), freshness={"on_stale": "warn"}, **kept)

    extra = result.health.tables[0]
    assert (extra.table, extra.stale, extra.news_at, extra.reasons) == ("gold.gold_extra", False, None, ())


def test_a_table_that_no_config_writes_and_that_cannot_be_loaded_is_a_row_with_the_reason_and_stale(lake, monkeypatch):
    now = now_after(lake)
    kept = catalog_lake(lake)
    from local_data_platform.mcp_server.discovery import ExposedTable

    real = ExposedTable.load

    def load(self):
        if self.identifier == "gold.gold_extra" and getattr(load, "armed", False):
            raise OSError("the catalog is gone")
        return real(self)

    monkeypatch.setattr(ExposedTable, "load", load)
    report = report_of(lake, freshness={"max_age_hours": 26, "on_stale": "warn"}, **kept)
    from local_data_platform.report.engine import run_queries

    reads = run_queries(report).reads
    load.armed = True
    health = collect(report, reads, now)

    extra, sales = health.tables
    assert extra.stale and extra.problem == "OSError: the catalog is gone"
    assert extra.reasons == ("its health could not be read (OSError: the catalog is gone)",)
    assert sales.table == "gold.gold_sales" and not sales.stale


def test_the_catalogs_that_the_health_opened_are_closed(lake, monkeypatch):
    now = now_after(lake)
    kept = catalog_lake(lake)
    import local_data_platform.mcp_server.discovery as discovery_module

    closed = []
    real = discovery_module.discover_tables

    def spy(*arguments, **keywords):
        found = real(*arguments, **keywords)
        found.close = lambda found=found, original=found.close: (closed.append(1), original())[1]
        return found

    monkeypatch.setattr(discovery_module, "discover_tables", spy)
    report = report_of(lake, freshness={"max_age_hours": 26, "on_stale": "warn"}, **kept)
    from local_data_platform.report.engine import run_queries

    reads = run_queries(report).reads
    closed.clear()
    collect(report, reads, now)

    assert closed == [1]


def test_a_gate_that_blocks_holds_the_stale_tables_and_not_the_fresh_ones_and_the_event_says_the_window_and_the_zone(
        lake):
    now = now_after(lake)
    put(lake, "gold_alpha")
    write(lake / "config" / "gold_alpha.json", gold_config("gold_alpha", observe=False))
    history(lake, run_events(1, hours_before(now, 40)))
    queries = {"totals": QUERY, "alpha": {"sql": "SELECT count(*) AS n FROM gold.gold_alpha"}}
    blocks = [{"type": "kpi", "query": "totals", "value": "orders"}, {"type": "kpi", "query": "alpha", "value": "n"}]
    sink = MemorySink()

    with pytest.raises(ReportHeld) as caught:
        build_report(report_of(lake, queries=queries, blocks=blocks, timezone="Asia/Kolkata",
                               freshness={"max_age_hours": 26, "on_stale": "block"}),
                     "2026-07-31/2026-08-02", out=lake / "x.html", clock=lambda: now, sink=sink)

    assert [item.table for item in caught.value.tables] == ["gold.gold_sales"]
    held = sink.events[1].payload
    assert [item["table"] for item in held["tables"]] == ["gold.gold_sales"]
    assert held["window"] == "2026-07-31T00:00:00Z/2026-08-02T00:00:00Z" and held["timezone"] == "Asia/Kolkata"


def test_a_build_that_cannot_work_out_the_health_fails_at_the_health_stage(lake, monkeypatch):
    now = now_after(lake)
    sink = MemorySink()

    def broken(report, reads, moment):
        raise RuntimeError("the health broke")

    monkeypatch.setattr("local_data_platform.report.build.collect", broken)
    with pytest.raises(RuntimeError, match="the health broke"):
        build_report(report_of(lake), None, out=lake / "x.html", clock=lambda: now, sink=sink)

    assert sink.types == ["run.started", "run.failed", "run.finished"] and not (lake / "x.html").exists()
    assert sink.events[1].payload["stage"] == "health"
