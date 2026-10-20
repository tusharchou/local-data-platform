"""The gold catalog: ``ldp gold list``, ``show`` and ``docs``, and the status that they share with ``ldp mcp``.

``collect`` makes one entry for each table of a folder of configs, and the commands print the entries as text, as a page
and as Markdown. Everything here is offline. The pure parts, the status of a table from its ``_ldp`` rows and the
renderers, are tested on plain values first, and the rest runs on small gold layers that the tests build under
``tmp_path``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform import html as ldp_html
from local_data_platform.cli import main
from local_data_platform.etl import run_config
from local_data_platform.exceptions import ConfigError
from local_data_platform.gold import catalog as catalogs
from local_data_platform.gold import render, status
from local_data_platform.gold import release as releases
from local_data_platform.gold.build import build_gold
from local_data_platform.gold.catalog import Catalog, CatalogEntry, ColumnEntry, collect, query_line
from local_data_platform.gold.status import (
    NOT_A_RUN, int_or_none, latest_quality, latest_run, ldp_status, recent_runs, snapshot_operation, snapshot_summary)
from tests.test_gold_build import GOLD_CATALOG, gold, silver, table, write
from tests.test_gold_release import more_orders
from tests.test_html import Parsed, sha

pytest.importorskip("duckdb")

UTC = dt.timezone.utc
FULL_DOCS = {"description": "Orders and revenue for each restaurant.", "owner": "restaurant operations",
             "tags": ["finance", "daily"], "refresh": "daily", "primary_key": ["restaurant"]}
CHECKS = {"checks": [{"check": "row_count", "min": 1}, {"check": "not_null", "columns": ["restaurant"]}]}


# --------------------------------------------------------------------------- the status of a table, from its rows


def at(second: int) -> dt.datetime:
    return dt.datetime(2026, 10, 1, 12, 0, tzinfo=UTC) + dt.timedelta(seconds=second)


def event(kind: str, run: str, second: int, *, attempt: int = 1, **payload) -> dict:
    """A row of ``_ldp.runs``, as ``ldp_rows`` returns it: with ``_ts`` and a payload that is already a dict.

    Every event of a run carries the pipeline, because the emitter puts its context in each payload.
    """
    return {"type": kind, "run_id": run, "attempt": attempt, "_ts": at(second),
            "payload": {"pipeline": "gold_revenue", **payload}}


def a_run(run: str, start: int, *, status_: str = "published", attempt: int = 1, **payload) -> list[dict]:
    return [event("run.started", run, start, attempt=attempt),
            event("run.published" if status_ == "published" else f"run.{status_}", run, start + 1, attempt=attempt,
                  snapshot_id=7, **payload),
            event("run.finished", run, start + 2, attempt=attempt, status=status_)]


def test_the_latest_run_is_the_run_with_the_newest_event_and_says_how_it_ended():
    rows = a_run("old", 0, status_="failed") + a_run("new", 10, idempotency_key="k1")

    run = latest_run(rows)

    assert (run["run_id"], run["status"], run["attempt"], run["pipeline"]) == ("new", "published", 1, "gold_revenue")
    assert (run["started_at"], run["finished_at"]) == (at(10), at(12))
    assert run["snapshot_id"] == 7 and run["idempotency_key"] == "k1"
    assert run["events"] == ["run.started", "run.published", "run.finished"]


@pytest.mark.parametrize("ending, expected", [
    ("run.published", "published"), ("run.skipped_duplicate", "skipped_duplicate"),
    ("run.blocked_quality", "blocked_quality"), ("run.failed", "failed")])
def test_a_run_with_no_finished_event_has_the_status_of_the_event_that_ended_it(ending, expected):
    rows = [event("run.started", "r", 0), event(ending, "r", 1)]

    assert latest_run(rows)["status"] == expected


def test_a_run_with_no_ending_is_running_and_a_finished_run_with_no_status_is_finished():
    assert latest_run([event("run.started", "r", 0), event("run.extracted", "r", 1)])["status"] == "running"
    assert latest_run([event("run.started", "r", 0), event("run.finished", "r", 1)])["status"] == "finished"


def test_the_status_of_a_finished_event_wins_over_the_event_that_ended_the_run():
    rows = [event("run.started", "r", 0), event("run.published", "r", 1),
            event("run.finished", "r", 2, status="succeeded")]

    assert latest_run(rows)["status"] == "succeeded"


def test_no_rows_is_no_run_and_so_are_rows_of_a_release_only():
    assert latest_run([]) is None
    assert latest_run([event("release.cut", "rel", 5, release="r1")]) is None
    assert recent_runs([event("release.cut", "rel", 5, release="r1")]) == []


def test_a_release_cut_after_a_build_does_not_hide_the_build():
    rows = a_run("build", 0) + [event("release.cut", "rel", 20, release="r1", snapshot_id=7)]

    run = latest_run(rows)

    assert run["run_id"] == "build" and run["status"] == "published"
    assert [item["run_id"] for item in recent_runs(rows)] == ["build"]


def test_a_release_event_is_the_only_event_that_is_not_a_run():
    assert NOT_A_RUN == ("release.cut",)


def test_the_attempts_of_a_run_are_runs_of_their_own_and_the_newest_event_decides():
    rows = a_run("r", 0, status_="failed") + a_run("r", 10, attempt=2)

    runs = recent_runs(rows)

    assert [(item["attempt"], item["status"]) for item in runs] == [(2, "published"), (1, "failed")]
    assert latest_run(rows)["attempt"] == 2


def test_recent_runs_are_the_newest_first_and_end_at_the_limit():
    rows = [row for number in range(7) for row in a_run(f"run{number}", number * 10)]

    assert [item["run_id"] for item in recent_runs(rows)] == ["run6", "run5", "run4", "run3", "run2"]
    assert [item["run_id"] for item in recent_runs(rows, 2)] == ["run6", "run5"]
    assert len(recent_runs(rows, 50)) == 7


def test_runs_that_overlap_are_ordered_by_their_newest_event():
    rows = [event("run.started", "slow", 0), event("run.started", "fast", 1), event("run.published", "fast", 2),
            event("run.finished", "fast", 3, status="published"), event("run.published", "slow", 4),
            event("run.finished", "slow", 5, status="published")]

    assert [item["run_id"] for item in recent_runs(rows)] == ["slow", "fast"]
    assert latest_run(rows)["run_id"] == "slow"


def test_a_snapshot_or_key_is_taken_from_the_row_or_from_its_payload():
    rows = [event("run.started", "r", 0), {**event("run.published", "r", 1), "snapshot_id": 99, "payload": {}}]

    assert latest_run(rows)["snapshot_id"] == 99


def test_the_helpers_that_read_a_snapshot():
    summary = SimpleNamespace(additional_properties={"total-records": "5"}, operation=SimpleNamespace(value="append"))
    snapshot = SimpleNamespace(summary=summary)

    assert snapshot_summary(snapshot) == {"total-records": "5"} and snapshot_operation(snapshot) == "append"
    assert snapshot_summary(SimpleNamespace()) == {} and snapshot_operation(SimpleNamespace()) is None
    assert snapshot_operation(SimpleNamespace(summary=SimpleNamespace(operation="plain"))) == "plain"
    assert [int_or_none(value) for value in ("7", 7, 7.9, None, "x", [])] == [7, 7, 7, None, None, None]


def quality_event(second: int, run: str = "r", **payload) -> dict:
    return {**event("quality.evaluated", run, second, **payload), "event_id": f"e{second}"}


def check(event_id: str, name: str, passed: bool, **extra) -> dict:
    return {"event_id": event_id, "payload": {"check_name": name, "column_name": "x", "passed": passed,
                                              "failing_rows": 0 if passed else 2, "details": "d", **extra}}


NO_SNAPSHOT_PROPERTY = SimpleNamespace(current_snapshot=lambda: None)


def test_the_quality_verdict_is_the_newest_evaluation_with_its_checks():
    runs = [quality_event(1, passed=True, checks_run=1, checks_failed=0, on_failure="fail"),
            quality_event(5, run="newer", passed=False, checks_run=2, checks_failed=1, on_failure="warn")]
    results = [check("e5", "range(fare)", False), check("e5", "not_null(id)", True), check("e1", "old", True)]

    verdict = latest_quality(results, runs, NO_SNAPSHOT_PROPERTY)

    counts = (verdict["passed"], verdict["checks_run"], verdict["checks_failed"], verdict["run_id"])
    assert counts == (False, 2, 1, "newer")
    assert verdict["source"] == "_ldp.quality_results" and verdict["on_failure"] == "warn"
    assert [item["check"] for item in verdict["checks"]] == ["range(fare)", "not_null(id)"]
    assert verdict["evaluated_at"] == at(5)


def test_a_verdict_with_no_check_rows_comes_from_the_run_events_alone():
    verdict = latest_quality(None, [quality_event(1, passed=True, checks_run=0, checks_failed=0)], NO_SNAPSHOT_PROPERTY)

    assert verdict["source"] == "_ldp.runs" and verdict["checks"] == [] and verdict["passed"] is True


def test_a_verdict_that_the_event_does_not_state_is_worked_out_from_the_checks():
    runs = [quality_event(1)]
    results = [check("e1", "a", True), check("e1", "b", False), check("e1", "c", True)]

    verdict = latest_quality(results, runs, NO_SNAPSHOT_PROPERTY)

    assert verdict["passed"] is False and verdict["checks_run"] == 3 and verdict["checks_failed"] == 1
    both_pass = latest_quality([check("e1", "a", True), check("e1", "b", True)], runs, NO_SNAPSHOT_PROPERTY)
    assert both_pass["passed"] is True and both_pass["checks_failed"] == 0


def test_without_events_the_quality_rows_or_else_the_snapshot_property_say_it():
    only_rows = latest_quality([check("q1", "a", True)], None, NO_SNAPSHOT_PROPERTY)
    assert only_rows["source"] == "_ldp.quality_results" and only_rows["checks_run"] == 1

    summary = SimpleNamespace(additional_properties={"ldp.quality": "warn"}, operation=None)
    table = SimpleNamespace(current_snapshot=lambda: SimpleNamespace(snapshot_id=3, summary=summary))
    assert latest_quality(None, None, table) == {"source": "snapshot", "snapshot_id": 3, "status": "warn"}
    assert latest_quality(None, None, NO_SNAPSHOT_PROPERTY) is None
    assert latest_quality([], [], SimpleNamespace(current_snapshot=lambda: SimpleNamespace(
        snapshot_id=1, summary=SimpleNamespace(additional_properties={}, operation=None)))) is None


def test_ldp_status_does_not_raise_when_the_events_cannot_be_read(monkeypatch):
    def broken(*arguments):
        raise OSError("the warehouse went away")

    monkeypatch.setattr(status, "ldp_rows", broken)
    table = SimpleNamespace(metadata=SimpleNamespace(table_uuid="u"), current_snapshot=lambda: None)

    result = ldp_status(object(), table, "gold.x", recent=3)

    assert result == {"error": "the warehouse went away", "latest_run": None, "quality": None, "recent_runs": []}
    assert "recent_runs" not in ldp_status(object(), table, "gold.x")


# --------------------------------------------------------------------------- small formatters


def test_clip_puts_a_text_on_one_line_and_ends_a_long_one_with_dots():
    assert render.clip("a  b\nc", 20) == "a b c"
    assert render.clip("abcdefghij", 10) == "abcdefghij"
    assert render.clip("abcdefghijk", 10) == "abcdefg..."
    assert render.clip("abcdef", 3) == "..." and render.clip("", 5) == ""


def test_utc_formats_a_moment_in_utc_and_leaves_other_values_alone():
    bangkok = dt.timezone(dt.timedelta(hours=7))

    assert render.utc(dt.datetime(2026, 10, 1, 19, 30, 5, tzinfo=bangkok)) == "2026-10-01 12:30:05"
    assert render.utc(dt.datetime(2026, 10, 1, 12, 30, 5)) == "2026-10-01 12:30:05"  # no zone means UTC
    assert render.utc(None) == "-" and render.utc("2026-10-01T12:30:05Z") == "2026-10-01T12:30:05Z"


@pytest.mark.parametrize("text, expected", [
    ("plain", "plain"),
    ("a | b", "a \\| b"),
    ("[link](http://x)", "\\[link\\](http://x)"),
    ("*bold* and _it_ and `code`", "\\*bold\\* and \\_it\\_ and \\`code\\`"),
    ("<b>x</b> & <script>", "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;"),
    ("two\nlines   here", "two lines here"),
    ("back\\slash", "back\\\\slash"),
])
def test_md_makes_markup_and_markdown_syntax_harmless(text, expected):
    assert render.md(text) == expected


def test_query_line_names_the_folder_and_a_table_that_is_ready_to_copy():
    assert query_line("ldp_demo/robots", "gold.daily_sales") == (
        'ldp query ldp_demo/robots "SELECT * FROM gold.daily_sales LIMIT 10"')
    assert query_line("my folder", "gold.x") == "ldp query 'my folder' \"SELECT * FROM gold.x LIMIT 10\""
    assert query_line(".", "a-b.c d") == "ldp query . 'SELECT * FROM \"a-b\".\"c d\" LIMIT 10'"
    assert query_line("x", "a.b", limit=3).endswith('LIMIT 3"')


def test_the_name_of_a_table_is_found_by_its_identifier_or_by_a_bare_name_that_only_one_table_has():
    known = ["gold.sales", "gold.stock", "silver.sales"]

    assert catalogs.resolve_table(known, "gold.sales") == "gold.sales"
    assert catalogs.resolve_table(known, "stock") == "gold.stock"
    ambiguous = r"'sales' is the name of 2 tables in cfg, gold.sales, silver.sales. Give the namespace"
    with pytest.raises(ConfigError, match=ambiguous):
        catalogs.resolve_table(known, "sales", "cfg")
    missing = r"there is no table 'nope' in cfg. It has gold.sales, gold.stock, silver.sales$"
    with pytest.raises(ConfigError, match=missing):
        catalogs.resolve_table(known, "nope", "cfg")
    with pytest.raises(ConfigError, match=r"It has gold.a, gold.b, gold.c, gold.d, gold.e, \.\.\.$"):
        catalogs.resolve_table([f"gold.{name}" for name in "abcdefg"], "nope")
    with pytest.raises(ConfigError, match="It has no table"):
        catalogs.resolve_table([], "nope")


# --------------------------------------------------------------------------- a small gold layer


def make_layer(root: Path) -> Path:
    """A silver table, ``gold.gold_revenue`` with a full contract and two checks, built and released as r1, a table in
    the gold namespace that nobody documented, and a gold table that is not built yet."""
    silver(root)
    gold(root, docs=FULL_DOCS, quality=CHECKS)
    build_gold(root, release=True)
    write(root / "extra.json", {"identifier": "extra", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": "extra", "format": "ICEBERG", "write_mode": "overwrite", "catalog": GOLD_CATALOG}}})
    run_config(root / "extra.json")
    gold(root, name="gold_future", docs={"description": "Planned, not built yet.", "owner": "finance"})
    return root


def make_behind(root: Path) -> Path:
    """The same layer, with ``gold.gold_revenue`` built again after r1, so that r1 is behind it."""
    make_layer(root)
    more_orders(root, 2)
    run_config(root / "gold_revenue.json")
    return root


@pytest.fixture(scope="module")
def layer(tmp_path_factory) -> Path:
    """The small gold layer. Tests that use it only read it."""
    return make_layer(tmp_path_factory.mktemp("layer"))


@pytest.fixture(scope="module")
def behind(tmp_path_factory) -> Path:
    return make_behind(tmp_path_factory.mktemp("behind"))


def entry_of(root: Path, name: str, **keywords) -> CatalogEntry:
    return collect(root, all_layers=True, **keywords).find(name)


# --------------------------------------------------------------------------- finding the configs


def test_find_configs_gives_every_dataset_config_in_file_order_and_skips_what_is_not_one(tmp_path):
    from local_data_platform.gold.build import find_configs

    silver(tmp_path)
    gold(tmp_path)
    write(tmp_path / "flow.json", {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "w",
                                   "tasks": [{"id": "a", "pipeline": "silver_orders.json"}]})
    write(tmp_path / "notes.json", {"hello": "world"})
    write(tmp_path / "readme.txt", "not json")

    assert [path.name for path, _ in find_configs(tmp_path)] == ["gold_revenue.json", "silver_orders.json"]


def test_find_configs_names_the_file_of_a_config_that_is_not_valid_and_refuses_a_file(tmp_path):
    from local_data_platform.gold.build import find_configs

    write(tmp_path / "broken.json", {"identifier": "broken", "metadata": {"source": {}}})

    with pytest.raises(ConfigError, match=r"^broken.json: "):
        find_configs(tmp_path)
    with pytest.raises(ConfigError, match="is not a folder of dataset configs"):
        find_configs(tmp_path / "broken.json")


# --------------------------------------------------------------------------- the entries


def test_the_catalog_has_the_gold_tables_by_default_and_every_table_with_all_layers(layer):
    default = collect(layer)
    everything = collect(layer, all_layers=True)

    assert [entry.table for entry in default.entries] == ["gold.extra", "gold.gold_future", "gold.gold_revenue"]
    assert [entry.table for entry in everything.entries] == [
        "gold.extra", "gold.gold_future", "gold.gold_revenue", "lake.silver_orders"]
    assert (default.all_layers, everything.all_layers) == (False, True) and default.folder == str(layer)


def test_a_gold_table_says_what_it_means_who_owns_it_and_what_one_row_is(layer):
    entry = entry_of(layer, "gold_revenue")

    assert (entry.table, entry.namespace, entry.name, entry.layer) == (
        "gold.gold_revenue", "gold", "gold_revenue", "gold")
    assert entry.description == "Orders and revenue for each restaurant."
    assert (entry.owner, entry.grain, entry.refresh) == ("restaurant operations", "one row per restaurant", "daily")
    assert entry.primary_key == ("restaurant",) and entry.tags == ("finance", "daily")
    assert entry.config == "gold_revenue.json" and entry.exists and entry.gold
    assert entry.problems == () and entry.notes == () and entry.metrics == () and not entry.detailed


def test_the_columns_have_their_types_and_descriptions_in_the_order_of_the_table(layer):
    entry = entry_of(layer, "gold_revenue")

    assert [(c.name, c.type, c.description, c.required) for c in entry.columns] == [
        ("restaurant", "string", "The restaurant.", False), ("orders", "long", "How many orders.", False),
        ("revenue", "double", "What they cost.", False)]


def test_the_entry_has_the_rows_the_snapshot_and_the_time_of_the_data(layer):
    entry = entry_of(layer, "gold_revenue")
    snapshot = table(layer, "gold_revenue").table().current_snapshot()

    assert entry.rows == 2 and entry.snapshot_id == snapshot.snapshot_id
    assert entry.refreshed_at.tzinfo is not None
    assert abs((dt.datetime.now(UTC) - entry.refreshed_at).total_seconds()) < 300
    assert entry.refreshed_at == dt.datetime.fromtimestamp(snapshot.timestamp_ms / 1000, tz=UTC)


def test_the_last_run_and_the_last_quality_verdict_of_a_table_that_ran_through_ldp(layer):
    entry = entry_of(layer, "gold_revenue")

    assert entry.run["status"] == "published" and entry.run["pipeline"] == "gold_revenue"
    assert entry.run_text == "published" and entry.quality_text == "2 of 2 passed"
    assert entry.quality["checks_run"] == 2 and entry.quality["passed"] is True


def test_a_release_cut_after_the_build_does_not_make_the_last_run_look_like_it_is_still_running(layer):
    entry = entry_of(layer, "gold_revenue")

    assert entry.release == "r1" and entry.run_text == "published"
    assert "release.cut" not in entry.run["events"]


def test_the_catalog_and_ldp_mcp_report_the_same_run_and_the_same_quality(layer, tmp_path):
    from local_data_platform.mcp_server.tools import LakeTools

    entry = entry_of(layer, "gold_revenue")
    with LakeTools.from_sources([layer], audit_path=tmp_path / "audit.jsonl", iceberg_audit=False) as tools:
        described = tools.call("describe_table", {"table": "gold.gold_revenue"})
    assert described.ok
    mcp = described.data

    assert mcp["latest_run"]["status"] == entry.run["status"] == "published"
    assert mcp["latest_run"]["run_id"] == entry.run["run_id"]
    assert mcp["freshness"]["last_run_status"] == "published"
    assert mcp["quality"]["checks_run"] == entry.quality["checks_run"] == 2
    assert mcp["quality"]["passed"] is entry.quality["passed"] is True
    assert mcp["contract"]["description"] == entry.description and mcp["contract"]["owner"] == entry.owner


def test_a_table_in_the_gold_namespace_that_nobody_documented_is_an_entry_that_lacks_its_contract(layer):
    entry = entry_of(layer, "extra")

    assert entry.layer == "gold" and entry.description is None and entry.shown_description == "no description"
    assert (entry.owner, entry.grain) == (None, None) and entry.run is None and entry.release is None
    assert entry.problems == ("no description", "no owner", "no grain",
                              "3 columns without a description (order_id, restaurant, amount)")
    assert entry.lacks_text == ("gold.extra has no description, no owner, no grain and 3 columns without a "
                                "description (order_id, restaurant, amount)")
    assert [column.description for column in entry.columns] == [None, None, None]


def test_a_table_of_another_layer_lacks_nothing_however_little_it_says(layer):
    entry = entry_of(layer, "silver_orders")

    assert entry.layer is None and entry.description is None and entry.problems == () and entry.lacks_text == ""


def test_a_gold_table_that_is_not_built_yet_is_an_entry_that_says_so(layer):
    entry = entry_of(layer, "gold_future")

    assert not entry.exists and entry.description == "Planned, not built yet." and entry.owner == "finance"
    assert entry.rows is None and entry.snapshot_id is None and entry.refreshed_at is None
    assert entry.run is None and entry.quality is None and entry.release is None
    assert [(c.name, c.type, c.description) for c in entry.columns] == [
        ("restaurant", None, "The restaurant."), ("orders", None, "How many orders."),
        ("revenue", None, "What they cost.")]
    assert entry.problems == ()  # its config promises everything that a gold table has to


def test_a_table_that_the_current_release_holds_is_in_it_and_at_its_snapshot(layer):
    entry = entry_of(layer, "gold_revenue")

    assert (entry.release, entry.release_state, entry.release_text) == ("r1", "current", "r1")


def test_a_table_that_was_built_again_after_its_release_is_behind_it_and_says_so(behind):
    entry = entry_of(behind, "gold_revenue")

    assert entry.release == "r1" and entry.release_state == "behind" and entry.release_text == "r1 (behind live)"
    assert entry.rows == 4  # what is there now, and r1 holds 2
    assert any("was built after r1" in note and "ldp gold build" in note for note in entry.notes)
    assert entry.run_text == "published"


def test_a_namespace_that_cannot_keep_releases_is_still_listed_and_has_none(layer, monkeypatch):
    def refuse(gold):
        raise releases.ReleaseError("no warehouse folder")

    monkeypatch.setattr(releases, "scope_of", refuse)

    entry = entry_of(layer, "gold_revenue", detail=True)

    assert entry.release is None and entry.release_state is None and entry.releases == ()
    assert entry.exists and entry.rows == 2


def test_the_config_wins_over_the_table_and_the_entry_says_that_the_table_lags(tmp_path):
    make_layer(tmp_path)
    config = json.loads((tmp_path / "gold_revenue.json").read_text())
    docs = config["metadata"]["docs"]
    docs.update({"description": "A newer meaning.", "owner": "someone else", "grain": "one row per day",
                 "primary_key": ["orders"], "tags": ["new"], "refresh": "hourly"})
    docs["columns"]["orders"] = "How many orders were taken."
    (tmp_path / "gold_revenue.json").write_text(json.dumps(config))

    entry = entry_of(tmp_path, "gold_revenue")

    assert (entry.description, entry.owner, entry.grain) == ("A newer meaning.", "someone else", "one row per day")
    assert (entry.primary_key, entry.tags, entry.refresh) == (("orders",), ("new",), "hourly")
    assert [column.description for column in entry.columns] == [
        "The restaurant.", "How many orders were taken.", "What they cost."]
    assert [note for note in entry.notes if "says more than the table carries" in note]
    assert f"ldp gold sync {tmp_path / 'gold_revenue.json'}" in entry.notes[0]


def test_a_config_with_no_contract_shows_what_the_table_carries(tmp_path):
    make_layer(tmp_path)
    config = json.loads((tmp_path / "gold_revenue.json").read_text())
    del config["metadata"]["docs"]
    (tmp_path / "gold_revenue.json").write_text(json.dumps(config))

    entry = entry_of(tmp_path, "gold_revenue")

    assert entry.layer == "gold" and entry.description == "Orders and revenue for each restaurant."
    assert entry.owner == "restaurant operations" and entry.grain == "one row per restaurant"
    assert entry.primary_key == ("restaurant",) and entry.tags == () and entry.refresh is None  # these are config only
    assert [column.description for column in entry.columns] == [
        "The restaurant.", "How many orders.", "What they cost."]
    assert entry.problems == () and entry.notes == ()


def test_when_two_configs_write_one_table_the_one_with_a_contract_describes_it(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    plain = json.loads((tmp_path / "gold_revenue.json").read_text())
    plain["identifier"] = "a_copy"
    del plain["metadata"]["docs"]
    write(tmp_path / "a_copy.json", plain)  # found first, and says nothing

    entry = entry_of(tmp_path, "gold_revenue")

    assert entry.config == "gold_revenue.json" and entry.description == "The gold_revenue table."


def test_a_config_that_writes_a_file_and_a_workflow_are_not_tables_of_the_catalog(tmp_path):
    silver(tmp_path)
    write(tmp_path / "export.json", {"identifier": "export", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": "out", "format": "CSV", "path": "out.csv"}}})
    write(tmp_path / "flow.json", {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "w",
                                   "tasks": [{"id": "a", "pipeline": "silver_orders.json"}]})

    assert [entry.table for entry in collect(tmp_path, all_layers=True).entries] == ["lake.silver_orders"]


def test_the_layers_are_ordered_gold_then_silver_then_bronze_then_the_rest(tmp_path):
    silver(tmp_path)
    for name, layer_name in (("zz_bronze", "bronze"), ("aa_silver", "silver"), ("mm_none", None)):
        config = json.loads((tmp_path / "silver_orders.json").read_text())
        config["identifier"] = name
        config["metadata"]["target"]["name"] = name
        config["metadata"]["target"]["catalog"] = {"identifier": "lake", "warehouse_path": "warehouse"}
        if layer_name:
            config["metadata"]["docs"] = {"layer": layer_name}
        write(tmp_path / f"{name}.json", config)
        run_config(tmp_path / f"{name}.json")
    gold(tmp_path)
    build_gold(tmp_path)

    entries = collect(tmp_path, all_layers=True).entries

    assert [(entry.table, entry.layer) for entry in entries] == [
        ("gold.gold_revenue", "gold"), ("lake.aa_silver", "silver"), ("lake.zz_bronze", "bronze"),
        ("lake.mm_none", None), ("lake.silver_orders", None)]


def test_the_layer_that_the_config_declares_wins_over_what_the_table_says_and_its_namespace(tmp_path):
    silver(tmp_path)
    gold(tmp_path, docs=FULL_DOCS)
    build_gold(tmp_path)  # the table says gold, and so does its namespace
    config = json.loads((tmp_path / "gold_revenue.json").read_text())
    config["metadata"]["docs"]["layer"] = "silver"
    (tmp_path / "gold_revenue.json").write_text(json.dumps(config))

    entry = entry_of(tmp_path, "gold_revenue")

    assert entry.layer == "silver" and entry.problems == () and not entry.gold
    assert [item.table for item in collect(tmp_path).entries] == []  # it is no longer one of the gold tables
    assert [item.table for item in collect(tmp_path, all_layers=True).entries][0] == "gold.gold_revenue"


def test_a_table_that_does_not_exist_yet_has_the_layer_its_config_declares(tmp_path):
    silver(tmp_path, build=False)
    config = json.loads((tmp_path / "silver_orders.json").read_text())
    config["metadata"]["docs"] = {"layer": "bronze"}
    (tmp_path / "silver_orders.json").write_text(json.dumps(config))

    entry = entry_of(tmp_path, "silver_orders")

    assert not entry.exists and entry.layer == "bronze"


def test_a_table_is_found_by_its_identifier_or_by_its_bare_name_and_only_it_is_read(layer, monkeypatch):
    opened = []
    real = catalogs._entry

    def spy(identifier, *arguments, **keywords):
        opened.append(identifier)
        return real(identifier, *arguments, **keywords)

    monkeypatch.setattr(catalogs, "_entry", spy)

    by_name = collect(layer, only="silver_orders")
    by_identifier = collect(layer, only="gold.gold_revenue", detail=True)

    assert [entry.table for entry in by_name.entries] == ["lake.silver_orders"]  # whatever its layer
    assert [entry.table for entry in by_identifier.entries] == ["gold.gold_revenue"]
    assert opened == ["lake.silver_orders", "gold.gold_revenue"]
    assert by_name.entries[0].layer is None and by_identifier.entries[0].detailed


def test_a_bare_name_that_two_tables_have_is_refused_and_so_is_a_table_that_is_not_there(layer):
    other = json.loads((Path(layer) / "silver_orders.json").read_text())
    other["identifier"] = "elsewhere"
    other["metadata"]["target"]["catalog"] = {"identifier": "archive", "warehouse_path": "warehouse"}
    write(Path(layer) / "zz_elsewhere.json", other)
    try:
        with pytest.raises(ConfigError, match=r"'silver_orders' is the name of 2 tables"):
            collect(layer, only="silver_orders")
        with pytest.raises(ConfigError, match="there is no table 'nope'"):
            collect(layer, only="nope")
    finally:
        (Path(layer) / "zz_elsewhere.json").unlink()


def test_collect_refuses_what_it_cannot_describe(tmp_path):
    with pytest.raises(ConfigError, match="is not a folder of dataset configs"):
        collect(tmp_path / "missing")
    with pytest.raises(ConfigError, match="has no dataset config that writes an Iceberg table"):
        collect(tmp_path)
    write(tmp_path / "bad.json", {"identifier": "bad", "metadata": {"source": {}}})
    with pytest.raises(ConfigError, match=r"^bad.json: "):
        collect(tmp_path)


# --------------------------------------------------------------------------- what it reads and what it writes


def files_of(root: Path) -> dict[str, tuple[int, int]]:
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*")) if path.is_file()}


def test_collecting_and_describing_write_nothing(tmp_path):
    make_layer(tmp_path)
    before = files_of(tmp_path)

    collect(tmp_path, all_layers=True, detail=True)
    main(["gold", "list", str(tmp_path), "--all-layers"])
    main(["gold", "show", str(tmp_path), "gold.gold_revenue"])

    assert files_of(tmp_path) == before


def test_a_catalog_that_does_not_exist_yet_is_not_created_and_its_tables_are_not_built_yet(tmp_path):
    silver(tmp_path, build=False)
    gold(tmp_path, docs=FULL_DOCS)

    catalog = collect(tmp_path, all_layers=True)

    assert [(entry.table, entry.exists) for entry in catalog.entries] == [
        ("gold.gold_revenue", False), ("lake.silver_orders", False)]
    assert not (tmp_path / "warehouse").exists()


def test_a_namespace_that_does_not_exist_yet_is_not_created(tmp_path):
    silver(tmp_path)
    gold(tmp_path, docs=FULL_DOCS)
    from local_data_platform.mcp_server.discovery import Catalogs

    def namespaces():
        handles = Catalogs()
        handle = handles.get({"identifier": "lake", "warehouse_path": "warehouse"}, tmp_path)
        try:
            return sorted(".".join(item) for item in handle.catalog.list_namespaces())
        finally:
            handle.catalog.close()

    before = namespaces()

    entry = entry_of(tmp_path, "gold_revenue")

    assert not entry.exists and "gold" not in before and namespaces() == before


# --------------------------------------------------------------------------- the detail of a table


def test_a_detailed_entry_has_the_last_runs_the_releases_a_sample_and_the_query(layer):
    entry = entry_of(layer, "gold_revenue", detail=True)

    assert entry.detailed and len(entry.runs) == 1 and entry.runs[0]["status"] == "published"
    assert [(item.release, item.current, item.at_live, item.rows, item.status) for item in entry.releases] == [
        ("r1", True, True, 2, "built")]
    assert entry.sample == {"columns": ["restaurant", "orders", "revenue"],
                            "rows": [["north", 2, 30.5], ["south", 1, 7.25]]}
    assert entry.query == f'ldp query {layer} "SELECT * FROM gold.gold_revenue LIMIT 10"'


def test_an_entry_that_was_not_asked_for_its_detail_has_none(layer):
    entry = entry_of(layer, "gold_revenue")

    assert entry.runs == () and entry.releases == () and entry.sample is None and entry.query == ""
    assert "runs" not in entry.to_dict() and "sample" not in entry.to_dict()


def test_the_sample_has_at_most_the_rows_asked_for_and_none_when_it_is_zero(layer):
    assert len(entry_of(layer, "extra", detail=True).sample["rows"]) == 3
    assert len(entry_of(layer, "extra", detail=True, sample=3).sample["rows"]) == 3
    assert entry_of(layer, "extra", detail=True, sample=2).sample["rows"] == [[1, "north", 10.5], [2, "north", 20.0]]
    assert entry_of(layer, "extra", detail=True, sample=0).sample is None


def test_the_releases_of_a_table_are_all_the_releases_that_hold_it_newest_first(tmp_path):
    make_behind(tmp_path)
    build_gold(tmp_path, release=True)  # r2 holds the table at its newer snapshot

    entry = entry_of(tmp_path, "gold_revenue", detail=True)

    assert [(item.release, item.current, item.at_live, item.rows) for item in entry.releases] == [
        ("r2", True, True, 4), ("r1", False, False, 2)]
    assert entry.release == "r2" and entry.release_state == "current"
    assert not [note for note in entry.notes if "was built after" in note]


def test_the_last_runs_are_the_newest_first_and_end_at_the_limit(tmp_path):
    make_layer(tmp_path)
    for _ in range(3):
        more_orders(tmp_path, 1)
        build_gold(tmp_path)  # a build records its runs, and a bare run_config of this config does not

    entry = entry_of(tmp_path, "gold_revenue", detail=True, runs=2)

    assert len(entry.runs) == 2
    assert entry.runs[0]["started_at"] > entry.runs[1]["started_at"]
    assert len(entry_of(tmp_path, "gold_revenue", detail=True).runs) == 4


# --------------------------------------------------------------------------- the data of an entry


def test_the_entry_as_data_is_json_and_a_table_with_no_description_has_null(layer):
    data = entry_of(layer, "extra", detail=True).to_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["description"] is None and data["owner"] is None and data["problems"][0] == "no description"
    assert data["refreshed_at"].endswith("+00:00") and data["run"] is None and data["exists"] is True


def test_the_catalog_as_data_counts_the_tables_by_layer_and_those_that_lack_a_contract(layer):
    data = collect(layer, all_layers=True).to_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["counts"] == {"tables": 4, "layers": {"gold": 3, "none": 1}, "lacking": 1}
    assert [entry["table"] for entry in data["tables"]][:2] == ["gold.extra", "gold.gold_future"]
    assert data["all_layers"] is True and data["folder"] == str(layer)


def test_the_gold_tables_that_lack_a_contract_are_the_ones_strict_fails_on(layer):
    catalog = collect(layer)

    assert [entry.table for entry in catalog.lacking] == ["gold.extra"]
    assert catalog.lacking_summary == "1 gold table lacks part of its contract"
    assert collect(layer, only="gold_revenue").lacking_summary == "0 gold tables lack part of their contract"


def test_the_document_of_an_entry_is_its_words_and_not_its_numbers(layer):
    document = entry_of(layer, "gold_revenue").document()

    assert document.splitlines()[:3] == ["gold.gold_revenue", "gold", "Orders and revenue for each restaurant."]
    for words in ("owner restaurant operations", "grain one row per restaurant", "key restaurant",
                  "tags finance, daily", "revenue What they cost."):
        assert words in document.splitlines()
    assert "30.5" not in document and "published" not in document


def test_an_entry_can_be_found_in_its_catalog_by_name(layer):
    catalog = collect(layer, all_layers=True)

    assert catalog.find("gold.extra").table == "gold.extra" and catalog.find("silver_orders").layer is None
    with pytest.raises(ConfigError, match="there is no table 'nope'"):
        catalog.find("nope")


def test_the_text_of_the_status_in_a_few_words():
    def entry(**keywords):
        base = dict(table="gold.x", namespace="gold", name="x", layer="gold", description=None, owner=None, grain=None,
                    primary_key=(), tags=(), refresh=None, config=None, exists=True, columns=())
        return CatalogEntry(**{**base, **keywords})

    assert entry().run_text is None and entry().quality_text is None and entry().release_text is None
    assert entry(run={"status": "failed"}).run_text == "failed"
    assert entry(quality={"checks_run": 4, "checks_failed": 0}).quality_text == "4 of 4 passed"
    assert entry(quality={"checks_run": 4, "checks_failed": 3}).quality_text == "3 of 4 failed"
    assert entry(quality={"checks_run": 0, "checks_failed": 0}).quality_text == "no checks"
    assert entry(quality={"source": "snapshot", "status": "warn"}).quality_text == "warn"
    assert entry(quality={"source": "snapshot"}).quality_text is None
    assert entry(release="r2", release_state="current").release_text == "r2"
    assert entry(release="r2", release_state="behind").release_text == "r2 (behind live)"
    assert entry(problems=("no owner",)).lacks_text == "gold.x has no owner"
    assert entry(problems=("no owner", "no grain")).lacks_text == "gold.x has no owner and no grain"
    assert entry().lacks_text == ""


def snapshot_of(number: int, operation: str, parent: int | None, ms: int) -> SimpleNamespace:
    summary = SimpleNamespace(operation=SimpleNamespace(value=operation), additional_properties={})
    return SimpleNamespace(snapshot_id=number, parent_snapshot_id=parent, timestamp_ms=ms, summary=summary)


def table_of(snapshots: dict[int, SimpleNamespace], current: int | None) -> SimpleNamespace:
    return SimpleNamespace(current_snapshot=lambda: snapshots[current] if current is not None else None,
                           snapshot_by_id=lambda number: snapshots.get(number))


def seconds(count: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(count, tz=UTC)


def test_a_refresh_time_skips_the_snapshots_that_maintenance_made():
    chain = {1: snapshot_of(1, "append", None, 1_000), 2: snapshot_of(2, "replace", 1, 2_000),
             3: snapshot_of(3, "replace", 2, 3_000)}

    assert catalogs._refreshed_at(table_of(chain, 3)) == seconds(1)
    assert catalogs._refreshed_at(table_of(chain, 1)) == seconds(1)
    assert catalogs._refreshed_at(table_of({1: snapshot_of(1, "replace", None, 5_000)}, 1)) == seconds(5)
    assert catalogs._refreshed_at(table_of({}, None)) is None
    looping = {1: snapshot_of(1, "replace", 2, 1_000), 2: snapshot_of(2, "replace", 1, 2_000)}
    assert catalogs._refreshed_at(table_of(looping, 1)) == seconds(1)


def test_a_table_with_no_snapshot_has_no_rows_to_count():
    assert catalogs._rows(SimpleNamespace(current_snapshot=lambda: None)) == 0
    summary = SimpleNamespace(additional_properties={"total-records": "12"}, operation=None)
    assert catalogs._rows(SimpleNamespace(current_snapshot=lambda: SimpleNamespace(summary=summary))) == 12
    assert catalogs._rows(SimpleNamespace(current_snapshot=lambda: SimpleNamespace(
        summary=SimpleNamespace(additional_properties={}, operation=None)))) is None


# --------------------------------------------------------------------------- ldp gold list


def gold_cli(capsys, *arguments) -> tuple[int, str, str]:
    """Run ``ldp gold ...`` and return its exit status, standard output and standard error."""
    code = main(["gold", *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def table_rows(output: str) -> list[list[str]]:
    """The rows of the plain table in some output, split on runs of two or more spaces, header first."""
    lines = output.splitlines()
    start = next(index for index, line in enumerate(lines) if set(line.replace(" ", "")) == {"-"})
    body = []
    for line in lines[start - 1:]:
        if line.startswith("(") or not line.strip():
            break
        body.append(re.split(r"\s{2,}", line.strip()))
    return [body[0], *body[2:]]


def test_list_prints_one_row_for_each_gold_table_with_what_it_means_and_how_it_is_doing(layer, capsys):
    code, out, err = gold_cli(capsys, "list", str(layer))

    rows = table_rows(out)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == f"3 gold tables in {layer}"
    assert rows[0] == ["table", "description", "owner", "grain", "rows", "refreshed_utc", "run", "quality", "release"]
    revenue = next(row for row in rows if row[0] == "gold.gold_revenue")
    assert revenue[1:4] == ["Orders and revenue for each restaurant.", "restaurant operations",
                            "one row per restaurant"]
    assert revenue[4] == "2" and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", revenue[5])
    assert revenue[6:] == ["published", "2 of 2 passed", "r1"]
    assert out.rstrip().endswith("gold.extra has no description, no owner, no grain and 3 columns without a "
                                 "description (order_id, restaurant, amount).")


def test_list_shows_no_description_for_a_table_with_no_contract_and_does_not_fail(layer, capsys):
    code, out, _ = gold_cli(capsys, "list", str(layer))

    extra = next(row for row in table_rows(out) if row[0] == "gold.extra")
    assert code == 0 and extra[1] == "no description" and "1 gold table lacks part of its contract." in out


def test_list_says_not_built_for_a_table_that_does_not_exist_yet(layer, capsys):
    _, out, _ = gold_cli(capsys, "list", str(layer))

    future = next(row for row in table_rows(out) if row[0] == "gold.gold_future")
    assert future[1:3] == ["Planned, not built yet.", "finance"] and "not built" in future


def test_list_with_all_layers_adds_the_layer_column_and_the_other_tables(layer, capsys):
    code, out, _ = gold_cli(capsys, "list", str(layer), "--all-layers")

    rows = table_rows(out)
    assert code == 0 and out.splitlines()[0] == f"4 tables in {layer}"
    assert rows[0][:3] == ["table", "layer", "description"]
    assert next(row for row in rows if row[0] == "lake.silver_orders")[1:3] == ["-", "no description"]
    assert [row[1] for row in rows if row[0].startswith("gold.")] == ["gold", "gold", "gold"]


def test_list_shows_a_table_that_was_built_after_its_release_as_behind_live(behind, capsys):
    _, out, _ = gold_cli(capsys, "list", str(behind))

    assert next(row for row in table_rows(out) if row[0] == "gold.gold_revenue")[-1] == "r1 (behind live)"


def test_list_cuts_a_long_description_and_json_keeps_all_of_it(tmp_path, capsys):
    silver(tmp_path)
    long_text = " ".join(["A very long meaning that goes on and on about the restaurant and what its orders cost"] * 3)
    gold(tmp_path, docs={"description": long_text})
    build_gold(tmp_path)

    _, out, _ = gold_cli(capsys, "list", str(tmp_path))
    _, data, _ = gold_cli(capsys, "list", str(tmp_path), "--json")

    cell = table_rows(out)[1][1]
    assert cell.endswith("...") and len(cell) == render.DESCRIPTION_WIDTH and long_text.startswith(cell[:-3])
    assert json.loads(data)["tables"][0]["description"] == long_text


def test_list_json_is_the_catalog_as_data(layer, capsys):
    code, out, _ = gold_cli(capsys, "list", str(layer), "--json")

    data = json.loads(out)
    assert code == 0 and data["counts"] == {"tables": 3, "layers": {"gold": 3}, "lacking": 1}
    assert [entry["table"] for entry in data["tables"]] == ["gold.extra", "gold.gold_future", "gold.gold_revenue"]
    assert data["tables"][0]["description"] is None and data["tables"][2]["release"] == "r1"
    assert "sample" not in data["tables"][2]  # a listing has no detail


def test_list_strict_fails_with_1_and_names_the_tables_that_lack_a_contract(layer, capsys):
    code, out, err = gold_cli(capsys, "list", str(layer), "--strict")

    assert code == 1 and "gold.gold_revenue" in out  # the listing is printed all the same
    assert err.splitlines() == [
        "ldp: 1 gold table lacks part of its contract",
        "  gold.extra has no description, no owner, no grain and 3 columns without a description "
        "(order_id, restaurant, amount)"]


def test_list_strict_passes_when_every_gold_table_has_its_contract_and_the_others_are_not_looked_at(tmp_path, capsys):
    make_layer(tmp_path)
    (tmp_path / "extra.json").unlink()  # nobody wrote the table down any more, and it is no table of the folder

    code, out, err = gold_cli(capsys, "list", str(tmp_path), "--strict")

    assert (code, err) == (0, "") and "lacks part" not in out


def test_list_strict_does_not_look_at_a_table_that_is_not_gold_however_little_it_says(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path, docs=FULL_DOCS)
    build_gold(tmp_path)

    code, _, err = gold_cli(capsys, "list", str(tmp_path), "--strict", "--all-layers")

    assert (code, err) == (0, "")


def test_list_strict_with_json_still_prints_json_and_fails(layer, capsys):
    code, out, err = gold_cli(capsys, "list", str(layer), "--strict", "--json")

    assert code == 1 and json.loads(out)["counts"]["lacking"] == 1 and "lacks part of its contract" in err


def test_list_of_a_folder_with_no_gold_table_says_so_and_how_to_see_the_others(tmp_path, capsys):
    silver(tmp_path)

    code, out, _ = gold_cli(capsys, "list", str(tmp_path), "--strict")

    assert code == 0
    assert out.strip() == (f"No gold tables in {tmp_path}. A gold table has metadata.docs.layer set to gold, or is in "
                           "a namespace called gold. Add --all-layers to list the tables of every layer.")
    assert gold_cli(capsys, "list", str(tmp_path), "--all-layers")[1].splitlines()[0] == f"1 table in {tmp_path}"


def test_list_errors_are_one_line_with_status_1(tmp_path, capsys):
    code, out, err = gold_cli(capsys, "list", str(tmp_path / "missing"))

    assert (code, out) == (1, "") and err.startswith("ldp: error: ") and "is not a folder of dataset configs" in err
    assert err.count("\n") == 1


def synthetic(table_name: str, **keywords) -> CatalogEntry:
    namespace, _, name = table_name.rpartition(".")
    base = dict(table=table_name, namespace=namespace, name=name, layer="gold", description="d", owner="o", grain="g",
                primary_key=(), tags=(), refresh=None, config=f"{name}.json", exists=True,
                columns=(ColumnEntry("a", "long", "The a."),))
    return CatalogEntry(**{**base, **keywords})


def test_a_value_in_a_sample_is_shown_as_a_person_reads_it():
    values = (None, True, False, 1.0, 0.0, 2.50, 30.5, 0.1 + 0.2, 1e-7, 7, "text")

    assert [render._value(value) for value in values] == [
        "NULL", "true", "false", "1", "0", "2.5", "30.5", "0.3", "0", "7", "text"]
    assert render._value("word " * 30) == "word word word word word word word wo..."
    assert render._value("x" * 100) == "x" * (render.CELL_WIDTH - 3) + "..."


def test_the_columns_of_a_sample_that_are_numbers_are_the_ones_with_only_numbers_in_them():
    rows = [["a", 1, None, 2.5, "7", True], ["b", 2.5, None, 3, 8, False], ["c", None, None, 4, 9, True]]

    # not the text, the column of nulls, the mixed one or the booleans
    assert render._numeric_columns(rows, 6) == [1, 3]
    assert render._numeric_columns([["a", 1], ["b", "x"]], 2) == []
    assert render._numeric_columns([], 2) == []


def test_list_aligns_the_rows_to_the_right_and_separates_thousands():
    entries = (synthetic("gold.small", rows=5), synthetic("gold.big", rows=1234567), synthetic("gold.none", rows=None))
    catalog = Catalog("cfg", entries, dt.datetime(2026, 10, 1, tzinfo=UTC))

    lines = render.list_text(catalog).splitlines()

    ends = [[cell.end() for cell in re.finditer(r"\S+(?: \S+)*", line)][4] for line in lines[3:6]]
    assert len(set(ends)) == 1
    assert render._right(["5", "1,234,567", "-"]) == ["        5", "1,234,567", "        -"]
    assert [line.split()[-9:][4] for line in lines[3:6]] == ["5", "1,234,567", "-"]


def test_list_of_nothing_says_so_for_either_scope():
    empty = Catalog("cfg", (), dt.datetime(2026, 10, 1, tzinfo=UTC))

    assert render.list_text(empty).startswith("No gold tables in cfg. A gold table has")
    assert render.list_text(Catalog("cfg", (), empty.generated_at, True)).endswith("namespace called gold.")


# --------------------------------------------------------------------------- ldp gold show


def show_facts(output: str) -> dict[str, list[str]]:
    """The ``Label  value`` lines under the heading of ``ldp gold show``, by label. A label can come more than once."""
    found: dict[str, list[str]] = {}
    for line in output.splitlines()[2:]:
        if not line.strip():
            break
        label, value = re.split(r"\s{2,}", line.strip(), maxsplit=1)
        found.setdefault(label, []).append(value)
    return found


def test_show_prints_the_contract_the_columns_the_runs_the_releases_a_sample_and_the_query(layer, capsys):
    code, out, err = gold_cli(capsys, "show", str(layer), "gold.gold_revenue")

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert lines[:2] == ["gold.gold_revenue  (gold)", "  Orders and revenue for each restaurant."]
    assert [line for line in lines if line and not line.startswith(" ")] == [
        "gold.gold_revenue  (gold)", "Columns (3)", "Last run", "Releases that include it (1)", "Sample (2 rows)",
        "Read it"]
    facts = show_facts(out)
    assert {label: values[0] for label, values in facts.items() if label != "Last run"} == {
        "Owner": "restaurant operations", "Grain": "one row per restaurant", "Primary key": "restaurant",
        "Refresh": "daily", "Tags": "finance, daily", "Config": "gold_revenue.json", "Rows": "2",
        "Refreshed": facts["Refreshed"][0], "Quality": "2 of 2 passed", "Release": "r1"}
    assert re.fullmatch(r"published, run [0-9a-f-]{36}, \d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", facts["Last run"][0])
    assert [line.split()[:2] for line in lines[lines.index("Columns (3)") + 3:lines.index("Columns (3)") + 6]] == [
        ["restaurant", "string"], ["orders", "long"], ["revenue", "double"]]
    assert f'  ldp query {layer} "SELECT * FROM gold.gold_revenue LIMIT 10"' in lines
    assert "r1 (current)" in out and "yes" in out


def test_show_finds_a_table_by_its_bare_name_and_a_table_of_any_layer(layer, capsys):
    assert gold_cli(capsys, "show", str(layer), "gold_revenue")[1].startswith("gold.gold_revenue  (gold)")
    out = gold_cli(capsys, "show", str(layer), "silver_orders")[1]
    assert out.startswith("lake.silver_orders  (no layer)\n  no description\n")


def test_show_of_a_table_with_no_contract_says_what_it_lacks_and_describes_it_all_the_same(layer, capsys):
    code, out, _ = gold_cli(capsys, "show", str(layer), "gold.extra")

    facts = show_facts(out)
    assert code == 0 and out.splitlines()[1] == "  no description"
    assert facts["Missing"] == ["no description", "no owner", "no grain",
                                "3 columns without a description (order_id, restaurant, amount)"]
    assert "Owner" not in facts and facts["Last run"] == ["none recorded"] and facts["Release"] == ["in no release"]
    assert out.count("no description") >= 4 and "Releases that include it\n  none" in out
    assert "Sample (3 rows)" in out


def test_show_of_a_table_that_is_not_built_yet_has_no_runs_and_no_sample(layer, capsys):
    out = gold_cli(capsys, "show", str(layer), "gold.gold_future")[1]

    assert show_facts(out)["Rows"] == ["the table does not exist yet"]
    assert "Last run" not in out and "Sample" not in out and "Releases that include it" not in out
    assert "restaurant  -     The restaurant." in out and out.rstrip().endswith('LIMIT 10"')


def test_show_leaves_the_sample_out_when_asked(layer, capsys):
    out = gold_cli(capsys, "show", str(layer), "gold.gold_revenue", "--no-samples")[1]

    assert "Sample" not in out and "north" not in out and "Read it" in out


def test_show_lists_the_last_runs_up_to_the_limit(tmp_path, capsys):
    make_layer(tmp_path)
    for _ in range(2):
        more_orders(tmp_path, 1)
        build_gold(tmp_path)

    many = gold_cli(capsys, "show", str(tmp_path), "gold.gold_revenue")[1]
    one = gold_cli(capsys, "show", str(tmp_path), "gold.gold_revenue", "--runs", "1")[1]

    assert "Last 3 runs" in many and "Last run\n" in one and "Last 3 runs" not in one
    code, _, err = gold_cli(capsys, "show", str(tmp_path), "gold.gold_revenue", "--runs", "0")
    assert code == 1 and "expected a number of at least 1, got 0" in err


def test_show_says_when_a_table_was_built_after_its_release(behind, capsys):
    out = gold_cli(capsys, "show", str(behind), "gold.gold_revenue")[1]

    facts = show_facts(out)
    assert facts["Release"] == ["r1 (behind live)"]
    assert facts["Note"][0].startswith("gold.gold_revenue was built after r1") and "no" in out.split("live_is_here")[1]


def test_show_prints_numbers_in_a_sample_to_the_right(layer, capsys):
    out = gold_cli(capsys, "show", str(layer), "gold.gold_revenue")[1]

    sample = out.split("Sample (2 rows)")[1].splitlines()
    assert sample[1].split() == ["restaurant", "orders", "revenue"]
    assert sample[3].split() == ["north", "2", "30.5"] and sample[3].index("2") > sample[1].index("orders")


def test_show_json_is_one_entry_with_its_detail(layer, capsys):
    code, out, _ = gold_cli(capsys, "show", str(layer), "gold.gold_revenue", "--json")

    data = json.loads(out)
    assert code == 0 and data["table"] == "gold.gold_revenue" and data["description"].startswith("Orders and revenue")
    assert data["sample"]["rows"] == [["north", 2, 30.5], ["south", 1, 7.25]]
    assert data["releases"][0]["release"] == "r1" and data["runs"][0]["status"] == "published"
    assert data["query"].startswith("ldp query ") and data["metrics"] == []


def test_show_of_a_table_that_is_not_there_names_the_tables_and_fails(layer, capsys):
    code, out, err = gold_cli(capsys, "show", str(layer), "nope")

    assert (code, out) == (1, "")
    assert err.splitlines() == [f"ldp: error: there is no table 'nope' in {layer}. It has gold.extra, "
                                "gold.gold_future, gold.gold_revenue, lake.silver_orders"]


def test_show_documents_the_commands_in_the_help_of_ldp_gold(capsys):
    code, out, _ = gold_cli(capsys, "--help")

    assert code == 0
    for command in ("sync", "build", "releases", "list", "show", "docs"):
        assert re.search(rf"^    {command}\s", out, re.M), command


# --------------------------------------------------------------------------- ldp gold docs


NOW = dt.datetime(2026, 10, 16, 9, 0, 0, tzinfo=UTC)
HOSTILE = ('Revenue | orders <b>bold</b> & <script>alert(1)</script> [click](http://evil.example/x) "quoted" '
           "*stars* _under_ `code`")


def test_docs_writes_one_page_and_says_so(layer, tmp_path, capsys):
    target = tmp_path / "catalog.html"

    code, out, err = gold_cli(capsys, "docs", str(layer), "--out", str(target))

    assert (code, err) == (0, "")
    assert re.fullmatch(rf"wrote {re.escape(str(target))}, 3 tables, \d+\.\d KB\. Open it as a page\. "
                        r"It needs no network\.\n", out)
    assert target.read_text(encoding="utf-8").startswith("<!doctype html>\n")
    assert [item.name for item in tmp_path.iterdir()] == ["catalog.html"]  # no temporary file is left


def test_docs_makes_the_folder_of_the_file_and_replaces_an_older_file_whole(layer, tmp_path, capsys):
    target = tmp_path / "site" / "reports" / "catalog.html"
    target.parent.mkdir(parents=True)
    target.write_text("old " * 50_000)

    assert gold_cli(capsys, "docs", str(layer), "--out", str(target))[0] == 0

    text = target.read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>") and "old old" not in text
    assert sorted(item.name for item in target.parent.iterdir()) == ["catalog.html"]
    deeper = tmp_path / "a" / "b" / "c.html"
    assert gold_cli(capsys, "docs", str(layer), "--out", str(deeper))[0] == 0 and deeper.is_file()


def test_docs_to_standard_output_writes_the_page_there_and_no_file(layer, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code, out, _ = gold_cli(capsys, "docs", str(layer), "--out", "-")

    assert code == 0 and out.startswith("<!doctype html>\n") and out.endswith("</html>\n")
    assert list(tmp_path.iterdir()) == []
    md_out = gold_cli(capsys, "docs", str(layer), "--out", "-", "--format", "md")[1]
    assert md_out.startswith("# Gold catalog\n") and list(tmp_path.iterdir()) == []


def test_docs_md_writes_markdown_and_says_so(layer, tmp_path, capsys):
    target = tmp_path / "catalog.md"

    code, out, _ = gold_cli(capsys, "docs", str(layer), "--out", str(target), "--format", "md")

    assert code == 0 and out.rstrip().endswith("Open it as a Markdown file.") and "needs no network" not in out
    assert target.read_text(encoding="utf-8").startswith("# Gold catalog\n")


def test_docs_with_all_layers_adds_the_other_tables_and_the_page_is_called_a_catalog(layer, tmp_path, capsys):
    target = tmp_path / "all.html"

    code, out, _ = gold_cli(capsys, "docs", str(layer), "--out", str(target), "--all-layers")

    parsed = Parsed(target.read_text(encoding="utf-8"))
    assert code == 0 and ", 4 tables, " in out
    assert [attrs["id"] for attrs in parsed.named("section")][-1] == "t-lake-silver-orders"
    assert "".join(parsed.text).count("Catalog") >= 2 and "Gold catalog" not in "".join(parsed.text)


def test_docs_no_samples_leaves_the_rows_out_of_both_formats_and_the_default_has_them(layer, tmp_path, capsys):
    outputs = {}
    for name, flags in (("with.html", []), ("without.html", ["--no-samples"]), ("with.md", ["--format", "md"]),
                        ("without.md", ["--format", "md", "--no-samples"])):
        assert gold_cli(capsys, "docs", str(layer), "--out", str(tmp_path / name), *flags)[0] == 0
        outputs[name] = (tmp_path / name).read_text(encoding="utf-8")

    for name in ("with.html", "with.md"):
        assert "30.5" in outputs[name] and "Sample (2 rows)" in outputs[name]
    for name in ("without.html", "without.md"):
        assert "30.5" not in outputs[name] and "north" not in outputs[name] and "Sample" not in outputs[name]


def test_docs_of_a_folder_with_no_gold_table_still_writes_a_page_that_says_so(tmp_path, capsys):
    silver(tmp_path)
    target = tmp_path / "out" / "catalog.html"

    code, _, _ = gold_cli(capsys, "docs", str(tmp_path), "--out", str(target))

    assert code == 0 and "There is no gold table here yet." in target.read_text(encoding="utf-8")
    assert gold_cli(capsys, "docs", str(tmp_path), "--out", "-", "--format", "md")[1].rstrip().endswith(
        "There is no gold table here yet.")


def test_docs_needs_a_place_to_write(layer, capsys):
    code, out, err = gold_cli(capsys, "docs", str(layer))  # --out is required, so that no file is written by surprise

    assert (code, out) == (1, "") and "the following arguments are required: --out" in err


# --------------------------------------------------------------------------- the page


def policy_of(parsed: Parsed) -> str:
    """The Content-Security-Policy that a page carries in its head."""
    return next(attrs["content"] for attrs in parsed.named("meta")
                if attrs.get("http-equiv") == "Content-Security-Policy")


@pytest.fixture(scope="module")
def page(layer) -> str:
    return render.html_page(collect(layer, all_layers=True, detail=True, now=NOW))


def test_the_page_asks_for_nothing_from_the_network(page):
    parsed = Parsed(page)

    assert not parsed.named("link") and not parsed.named("img") and not parsed.named("iframe")
    assert not [attrs for attrs in parsed.named("script") if "src" in attrs]
    for value in parsed.attributes("src") + parsed.attributes("href"):
        assert value is None or value.startswith("#"), value  # no external URL in any src or href
    assert "url(" not in page and "@import" not in page and "http://" not in page and "https://" not in page
    assert "default-src 'none'" in policy_of(parsed) and "connect-src" not in policy_of(parsed)


def test_the_page_allows_exactly_its_own_style_and_script(page):
    parsed = Parsed(page)
    policy = policy_of(parsed)

    assert f"style-src {sha(ldp_html.CSS)}" in policy and f"script-src {sha(ldp_html.FILTER_SCRIPT)}" in policy
    assert len(parsed.named("script")) == 1 and len(parsed.named("style")) == 1
    assert f"<script>{ldp_html.FILTER_SCRIPT}</script>" in page


def test_the_page_has_a_card_for_each_table_with_its_own_anchor_and_its_words_to_search(page):
    cards = [attrs for attrs in Parsed(page).named("section") if "card" in (attrs.get("class") or "")]

    assert [card["id"] for card in cards] == ["t-gold-extra", "t-gold-gold-future", "t-gold-gold-revenue",
                                              "t-lake-silver-orders"]
    searchable = {card["id"]: card["data-search"] for card in cards}
    assert all(words == words.lower() for words in searchable.values())
    assert "restaurant operations" in searchable["t-gold-gold-revenue"] and "how many orders." in searchable[
        "t-gold-gold-revenue"]
    assert searchable["t-gold-gold-future"].startswith("gold.gold_future\ngold\nplanned, not built yet.")


def test_the_page_says_what_each_table_means_and_how_it_is_doing(page):
    parsed = Parsed(page)
    words = " ".join("".join(parsed.text).split())

    for expected in ("Orders and revenue for each restaurant.", "restaurant operations", "one row per restaurant",
                     "2 rows", "last run published", "quality 2 of 2 passed", "release r1", "finance, daily",
                     "not built yet", "no description", "no owner", "no grain",
                     "3 columns without a description (order_id, restaurant, amount)",
                     "1 gold table lacks part of its contract"):
        assert expected in words, expected
    assert "made 2026-10-16 09:00:00 UTC" in words and "4 tables in" in words


def test_a_description_that_is_not_there_is_muted_and_one_that_is_there_is_not(page):
    sections = re.split(r'(?=<section )', page)

    def description_paragraph(table_id: str) -> str:
        section = next(part for part in sections if f'id="{table_id}"' in part)
        return re.search(r"</h2>\s*(<p[^>]*>.*?</p>)", section, re.S).group(1)

    assert description_paragraph("t-gold-extra") == '<p class="muted">no description</p>'
    assert description_paragraph("t-gold-gold-revenue") == "<p>Orders and revenue for each restaurant.</p>"


def test_the_page_has_the_columns_the_runs_the_releases_and_the_sample_of_a_table(page):
    parsed = Parsed(page)
    words = " ".join("".join(parsed.text).split())

    for title in ("Columns (3)", "Last run", "Releases that include it (1)", "Sample (2 rows)"):
        assert title in words, title
    assert "r1 (current)" in words and "north" in words and "30.5" in words
    numeric = [attrs for attrs in parsed.named("td") if attrs.get("class") == "num"]
    assert numeric, "numbers are right-aligned"


def test_the_page_has_a_search_box_that_the_script_can_find(page):
    parsed = Parsed(page)

    box = next(attrs for attrs in parsed.named("input") if attrs.get("id") == "q")
    assert box["type"] == "search" and box["autocomplete"] == "off"
    assert any(attrs.get("id") == "count" for attrs in parsed.named("span"))


def test_the_page_is_the_same_page_every_time_it_is_made_for_the_same_moment(layer):
    first = render.html_page(collect(layer, all_layers=True, detail=True, now=NOW))
    second = render.html_page(collect(layer, all_layers=True, detail=True, now=NOW))

    assert first == second


def test_a_description_with_markup_is_text_on_the_page_and_nothing_else(tmp_path):
    silver(tmp_path)
    gold(tmp_path, docs={"description": HOSTILE, "owner": 'o"wner <i>x</i>', "grain": "g <u>y</u>",
                         "columns": {"restaurant": "<script>boom()</script>", "orders": "o", "revenue": "r"}})
    build_gold(tmp_path)

    text = render.html_page(collect(tmp_path, detail=True, now=NOW))
    parsed = Parsed(text)

    assert [tag for tag, _ in parsed.tags if tag in ("b", "i", "u", "a", "img")] == []
    assert len(parsed.named("script")) == 1  # the filter, and not the one that a description tried to add
    assert HOSTILE in "".join(parsed.text) and "<script>boom()</script>" in "".join(parsed.text)
    assert 'o"wner <i>x</i>' in "".join(parsed.text)
    assert not [name for _, attrs in parsed.tags for name in attrs if name.startswith("on")]
    card = next(attrs for attrs in parsed.named("section") if "card" in (attrs.get("class") or ""))
    assert HOSTILE.lower() in card["data-search"]


def test_the_page_of_a_table_that_is_behind_its_release_warns_about_it(behind):
    parsed = Parsed(render.html_page(collect(behind, detail=True, now=NOW)))

    assert "release r1 (behind live)" in "".join(parsed.text)
    chip = next(attrs for tag, attrs in parsed.tags if tag == "span" and attrs.get("class") == "chip warn")
    assert chip is not None
    assert "was built after r1" in " ".join("".join(parsed.text).split())


class Chips(HTMLParser):
    """The text of each chip of some markup, with its classes."""

    def __init__(self, markup: str):
        super().__init__()
        self.found: dict[str, str] = {}
        self._classes: str | None = None
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class") or ""
        self._classes = classes if tag == "span" and classes.startswith("chip") else None

    def handle_data(self, data):
        if self._classes is not None:
            self.found[data] = self._classes
            self._classes = None


def chips_of(entry: CatalogEntry) -> dict[str, str]:
    return Chips(str(render._chips(entry))).found


def test_a_run_that_failed_is_red_and_one_that_published_is_green_and_one_that_is_running_is_amber():
    def run_class(status):
        return chips_of(synthetic("gold.x", rows=1, run={"status": status}))[f"last run {status}"]

    assert run_class("published") == "chip ok" and run_class("succeeded") == "chip ok"
    assert run_class("failed") == "chip bad" and run_class("blocked_quality") == "chip bad"
    assert run_class("running") == "chip warn" and run_class("skipped_duplicate") == "chip"
    assert chips_of(synthetic("gold.x", rows=1))["no run recorded"] == "chip"


def test_the_quality_chip_is_red_when_checks_failed_and_green_when_they_passed():
    def quality_class(quality):
        found = chips_of(synthetic("gold.x", rows=1, quality=quality, run={"status": "published"}))
        return next(classes for words, classes in found.items() if words.startswith("quality "))

    assert quality_class({"checks_run": 3, "checks_failed": 0, "passed": True}) == "chip ok"
    assert quality_class({"checks_run": 3, "checks_failed": 1, "passed": False}) == "chip bad"
    assert quality_class({"checks_run": 0, "checks_failed": 0, "passed": True}) == "chip"
    assert quality_class({"source": "snapshot", "status": "warn"}) == "chip"


def test_the_chips_of_a_table_that_is_not_built_say_only_that():
    assert chips_of(synthetic("gold.x", exists=False)) == {"not built yet": "chip warn"}


def test_the_release_chip_is_amber_when_the_table_is_behind_green_when_it_is_current_and_plain_without_one():
    def release_class(**keywords):
        found = chips_of(synthetic("gold.x", rows=1, **keywords))
        return next(classes for words, classes in found.items() if "release" in words)

    assert release_class(release="r1", release_state="current") == "chip ok"
    assert release_class(release="r1", release_state="behind") == "chip warn"
    assert release_class() == "chip"


def test_two_tables_whose_names_make_one_anchor_get_two_anchors():
    taken: set[str] = set()

    assert [render._anchor(name, taken) for name in ("a.b_c", "a_b.c", "a.b-c", "!!!")] == [
        "t-a-b-c", "t-a-b-c-2", "t-a-b-c-3", "t-table"]


def test_the_page_of_a_catalog_with_nothing_in_it_says_so():
    empty = Catalog("cfg", (), NOW)

    text = render.html_page(empty)

    assert "There is no gold table here yet." in text and "0 gold tables in cfg" in text
    assert Parsed(text).named("section") == []


# --------------------------------------------------------------------------- the Markdown


@pytest.fixture(scope="module")
def markdown(layer) -> str:
    return render.markdown_page(collect(layer, all_layers=True, detail=True, now=NOW))


def test_the_markdown_has_one_overview_table_and_one_section_for_each_table(markdown):
    lines = markdown.splitlines()

    assert lines[0] == "# Catalog" and "4 tables in `" in lines[2] and lines[2].endswith("by `ldp gold docs`.")
    head = lines.index("| Table | Description | Owner | Grain | Rows | Refreshed (UTC) | Last run | Quality | "
                       "Release |")
    assert lines[head + 1] == "| --- | --- | --- | --- | ---: | --- | --- | --- | --- |"
    assert [line.split(" | ")[0] for line in lines[head + 2:head + 6]] == [
        "| [`gold.extra`](#t-gold-extra)", "| [`gold.gold_future`](#t-gold-gold-future)",
        "| [`gold.gold_revenue`](#t-gold-gold-revenue)", "| [`lake.silver_orders`](#t-lake-silver-orders)"]
    assert [line for line in lines if line.startswith("## ")] == [
        "## `gold.extra`", "## `gold.gold_future`", "## `gold.gold_revenue`", "## `lake.silver_orders`"]


def test_every_link_of_the_overview_has_the_anchor_it_points_to(markdown):
    links = re.findall(r"\]\(#([^)]+)\)", markdown)
    anchors = re.findall(r'<a id="([^"]+)"></a>', markdown)

    assert links == anchors and len(set(anchors)) == 4


def test_the_markdown_of_a_table_has_its_facts_columns_runs_releases_sample_and_query(markdown):
    section = markdown.split("## `gold.gold_revenue`")[1].split("\n## ")[0]

    for expected in ("- **Owner** restaurant operations", "- **Grain** one row per restaurant",
                     "- **Primary key** restaurant", "- **Config** `gold_revenue.json`", "- **Rows** 2",
                     "- **Quality** 2 of 2 passed", "- **Release** r1", "### Columns (3)", "### Last run",
                     "### Releases that include it (1)", "### Sample (2 rows)",
                     "| `restaurant` | string | The restaurant. |", "| north | 2 | 30.5 |"):
        assert expected in section.splitlines() or expected in section, expected
    fence = section.split("Read it with")[1]
    assert re.fullmatch(r'\s*```\nldp query .* "SELECT \* FROM gold.gold_revenue LIMIT 10"\n```\s*'
                        r'(<a id="[^"]+"></a>\s*)?', fence)


def test_the_markdown_of_a_table_with_no_contract_and_one_not_built(markdown):
    extra = markdown.split("## `gold.extra`")[1].split("\n## ")[0]
    future = markdown.split("## `gold.gold_future`")[1].split("\n## ")[0]

    assert "no description" in extra and "- **Missing** no owner" in extra
    assert "- **Missing** 3 columns without a description (order\\_id, restaurant, amount)" in extra.splitlines()
    assert "- **Not built yet**" in future and "### Last run" not in future and "### Sample" not in future


def test_the_markdown_of_a_gold_layer_with_tables_that_lack_a_contract_says_so_at_the_top(layer):
    text = render.markdown_page(collect(layer, now=NOW))

    assert text.splitlines()[0] == "# Gold catalog"
    assert "**1 gold table lacks part of its contract**, `gold.extra`." in text.splitlines()


def test_markdown_made_of_what_a_person_wrote_cannot_break_out_of_its_table_or_make_markup(tmp_path):
    silver(tmp_path)
    gold(tmp_path, docs={"description": HOSTILE, "owner": "o|w", "grain": "g <b>x</b>"})
    build_gold(tmp_path)

    text = render.markdown_page(collect(tmp_path, detail=True, now=NOW))

    overview = [line for line in text.splitlines() if line.startswith("| [`gold.gold_revenue`]")][0]
    header = [line for line in text.splitlines() if line.startswith("| Table |")][0]
    assert len(re.findall(r"(?<!\\)\|", overview)) == len(re.findall(r"(?<!\\)\|", header))
    assert "<b>" not in text and "<script>" not in text and "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "[click](http" not in text and "\\[click\\](http://evil.example/x)" in text
    assert "\\*stars\\*" in text and "\\_under\\_" in text and "\\`code\\`" in text
    assert [line for line in text.splitlines() if "<" in line] == [line for line in text.splitlines()
                                                                   if line.startswith("<a id=")]


def test_markdown_made_with_a_backtick_in_the_folder_or_the_query_stays_in_its_fences():
    entry = synthetic("gold.x", rows=1, detailed=True, query="ldp query a ```b``` c")
    text = render.markdown_page(Catalog("we`ird", (entry,), NOW))

    assert "in `weird`" in text and "```b```" not in text and "'''b'''" in text


def test_the_markdown_of_a_catalog_with_nothing_in_it_says_so():
    assert render.markdown_page(Catalog("cfg", (), NOW)).rstrip().endswith("There is no gold table here yet.")
