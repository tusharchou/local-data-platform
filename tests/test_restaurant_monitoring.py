"""The monitoring layer of the restaurant example, ``run.py --layer monitoring``, and the days its generator writes.

The layer watches the orders through five nights, a late table, a short batch, a new column, a revenue drop and a fresh
night, with ``ldp monitor --now``, an alert file and ``ldp status``. Most tests read one run of it without dbt, which
also checks that the layer needs neither the ``dbt`` nor the ``excel`` extra. The test with the revenue step runs the
real dbt, so it carries the ``dbt`` marker and the ``dbt`` CI job runs it. The numbers are the ones that the example
prints for the fixed seed.
"""

import contextlib
import csv
import datetime as dt
import io
import json
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform import Config
from local_data_platform.gold import release as releases
from local_data_platform.monitoring.history import read_results
from tests.support import load_example_module

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "restaurant"
run = load_example_module("restaurant_run_monitoring", EXAMPLE / "run.py")
generate = run.generate
UTC = dt.timezone.utc

NOW = ["2026-08-05T21:34:26Z", "2026-08-07T06:00:00Z", "2026-08-08T06:00:00Z", "2026-08-09T06:00:00Z",
       "2026-08-10T06:00:00Z", "2026-08-10T06:05:00Z"]
AFTER = ["a normal day", "a short day", "a day with a new column", "a day with a revenue drop", "a fresh day",
         "a second look"]
EXIT_WITHOUT_DBT = [2, 2, 0, 0, 0, 0]
STATUSES_WITHOUT_DBT = [
    {"freshness": "error", "volume.rows": "ok", "schema": "ok"},
    {"freshness": "ok", "volume.rows": "error", "schema": "ok"},
    {"freshness": "ok", "volume.rows": "ok", "schema": "warn"},
    {"freshness": "ok", "volume.rows": "ok", "schema": "ok"},
    {"freshness": "ok", "volume.rows": "ok", "schema": "ok"},
    {"freshness": "ok", "volume.rows": "ok", "schema": "ok"},
]
ALERTS_WITHOUT_DBT = [
    [("monitor.error", "freshness")],
    [("monitor.error", "volume.rows"), ("monitor.resolved", "freshness")],
    [("monitor.resolved", "volume.rows"), ("monitor.warn", "schema")],
    [("monitor.resolved", "schema")],
    [],
    [],
]


def narrated(workdir: Path, **options) -> SimpleNamespace:
    """Run the layer, and return its summary, its work folder and everything it printed."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        whole = run.run_restaurant(workdir, layer="monitoring", **options)
    return SimpleNamespace(summary=whole["monitoring"], whole=whole, workdir=Path(whole["workdir"]),
                           text=printed.getvalue())


def sent(look: dict) -> list[tuple[str, str]]:
    return sorted((alert["event"], alert["monitor"]) for alert in look["alerts"])


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """One run of the layer with dbt hidden and no Slack webhook, which is how a plain install runs it."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(run, "dbt_installed", lambda: False)
        patch.delenv(run.SLACK_ENV, raising=False)
        return narrated(tmp_path_factory.mktemp("restaurant") / "work")


@pytest.fixture(scope="module")
def dataset():
    return generate.generate()


# ====================================================================== the days that the generator writes


def test_a_normal_day_repeats_the_first_8_orders_of_the_first_week_of_august_on_its_own_date(dataset):
    template = dataset.week_orders()[:8]
    rows, lines = dataset.monitoring_orders(dt.date(2026, 8, 3))

    assert [row["order_id"] for row in rows] == [f"M0803{number:02d}" for number in range(1, 9)]
    assert all(row["order_ts"].date() == dt.date(2026, 8, 3) for row in rows)
    for row, order in zip(rows, template):
        assert (row["restaurant_id"], row["customer_id"], row["channel"], row["total_amount"]) == (
            order["restaurant_id"], order["customer_id"], order["channel"], order["total_amount"])
        assert row["order_ts"].time() == order["order_ts"].time()
        assert row["updated_at"] - row["order_ts"] == order["updated_at"] - order["order_ts"]
    assert round(sum(row["total_amount"] for row in rows), 2) == 6330
    assert len(lines) == 19 and {line["order_id"] for line in lines} == {row["order_id"] for row in rows}


def test_a_day_has_the_same_orders_on_every_date_and_only_the_ids_and_the_date_change(dataset):
    first, first_lines = dataset.monitoring_orders(dt.date(2026, 8, 3))
    later, later_lines = dataset.monitoring_orders(dt.date(2026, 9, 14))

    assert [row["order_id"] for row in later] == [f"M0914{number:02d}" for number in range(1, 9)]
    for row, other in zip(first, later):
        assert {key: value for key, value in row.items() if key not in ("order_id", "order_ts", "updated_at")} == {
            key: value for key, value in other.items() if key not in ("order_id", "order_ts", "updated_at")}
    assert [(line["line_no"], line["menu_item_id"], line["quantity"]) for line in first_lines] == [
        (line["line_no"], line["menu_item_id"], line["quantity"]) for line in later_lines]


def test_a_short_day_keeps_the_first_orders_of_a_normal_day(dataset):
    normal, _ = dataset.monitoring_orders(dt.date(2026, 8, 6))
    short, lines = dataset.monitoring_orders(dt.date(2026, 8, 6), orders=3)

    assert short == normal[:3]
    assert round(sum(row["total_amount"] for row in short), 2) == 1950
    assert {line["order_id"] for line in lines} == {"M080601", "M080602", "M080603"} and len(lines) == 8


def test_the_amounts_of_a_day_and_the_unit_prices_of_its_lines_are_scaled(dataset):
    normal, normal_lines = dataset.monitoring_orders(dt.date(2026, 8, 8))
    small, small_lines = dataset.monitoring_orders(dt.date(2026, 8, 8), amount_scale=0.1)

    assert round(sum(row["total_amount"] for row in small), 2) == 633
    assert [row["total_amount"] for row in small] == [round(row["total_amount"] * 0.1, 2) for row in normal]
    assert [line["unit_price"] for line in small_lines] == [round(line["unit_price"] * 0.1, 2) for line in normal_lines]
    assert [line["quantity"] for line in small_lines] == [line["quantity"] for line in normal_lines]
    assert len(small) == len(normal) == 8


def test_a_day_may_keep_one_order_or_all_eight(dataset):
    one, one_lines = dataset.monitoring_orders(dt.date(2026, 8, 3), orders=1)
    eight, eight_lines = dataset.monitoring_orders(dt.date(2026, 8, 3), orders=8)

    assert [row["order_id"] for row in one] == ["M080301"]
    assert {line["order_id"] for line in one_lines} == {"M080301"}
    assert len(eight) == 8 and {line["order_id"] for line in eight_lines} == {f"M0803{n:02d}" for n in range(1, 9)}


@pytest.mark.parametrize("options, message", [
    ({"orders": 0}, "from 1 to 8 orders, got 0"), ({"orders": 9}, "from 1 to 8 orders, got 9"),
    ({"amount_scale": 0}, "amount_scale must be above 0, got 0"),
    ({"amount_scale": -1.5}, "amount_scale must be above 0, got -1.5")])
def test_a_day_that_cannot_be_made_is_refused_and_says_why(dataset, options, message):
    with pytest.raises(ValueError, match=message):
        dataset.monitoring_orders(dt.date(2026, 8, 3), **options)


def test_the_days_of_the_story_are_five_and_arrive_in_this_order_with_two_nights_missing():
    days = generate.MONITORING_DAYS

    assert [(day.day.isoformat(), day.story, day.orders, day.amount_scale, day.discount) for day in days] == [
        ("2026-08-03", "a normal day", 8, 1.0, False), ("2026-08-06", "a short day", 3, 1.0, False),
        ("2026-08-07", "a day with a new column", 8, 1.0, True),
        ("2026-08-08", "a day with a revenue drop", 8, 0.1, True), ("2026-08-09", "a fresh day", 8, 1.0, True)]
    assert dt.date(2026, 8, 4) not in {day.day for day in days} and dt.date(2026, 8, 5) not in {day.day for day in days}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def test_a_day_is_written_as_the_export_of_the_restaurant_system_and_discount_is_the_last_column(tmp_path, dataset):
    plain_orders, plain_lines = generate.write_monitoring_day(tmp_path, dataset, dt.date(2026, 8, 3))
    column_orders, _ = generate.write_monitoring_day(tmp_path, dataset, generate.MONITORING_DAYS[2],
                                                     "column_orders.csv", "column_lines.csv")

    assert plain_orders.name == "orders.csv" and plain_lines.name == "order_lines.csv"
    assert plain_orders.read_text().splitlines()[0] == ",".join(generate.ORDER_COLUMNS)
    assert plain_lines.read_text().splitlines()[0] == ",".join(generate.LINE_COLUMNS)
    rows = read_csv(column_orders)
    assert list(rows[0]) == [*generate.ORDER_COLUMNS, "discount"]
    assert all(row["discount"] == ("0.10" if row["channel"] == "delivery" else "0.00") for row in rows)
    assert {"delivery", "dine_in"} <= {row["channel"] for row in rows}
    assert rows[0]["order_ts"] == "2026-08-07 10:19:25" and rows[0]["total_amount"] == "230.00"
    assert len(read_csv(plain_orders)) == 8
    assert all(re.fullmatch(r"\d+\.\d\d", line["unit_price"]) for line in read_csv(plain_lines))  # in money


def test_a_date_is_a_normal_day_and_a_story_brings_its_own_options(tmp_path, dataset):
    by_date, _ = generate.write_monitoring_day(tmp_path, dataset, dt.date(2026, 8, 6))
    short_day, drop_day = generate.MONITORING_DAYS[1], generate.MONITORING_DAYS[3]
    by_story, _ = generate.write_monitoring_day(tmp_path, dataset, short_day, "short.csv", "lines.csv")

    assert len(read_csv(by_date)) == 8 and len(read_csv(by_story)) == 3
    drop, _ = generate.write_monitoring_day(tmp_path, dataset, drop_day, "drop.csv", "drop_lines.csv")
    assert round(sum(float(row["total_amount"]) for row in read_csv(drop)), 2) == 633


def test_the_days_are_named_after_their_date_and_do_not_clash_with_the_drops(tmp_path, dataset):
    written = generate.write_monitoring_days(tmp_path, dataset)

    assert sorted(path.name for path in written) == sorted(
        [f"monitor_orders_{day}.csv" for day in ("2026-08-03", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09")]
        + [f"monitor_order_lines_{day}.csv" for day in
           ("2026-08-03", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09")])
    assert len(read_csv(tmp_path / "monitor_orders_2026-08-06.csv")) == 3
    assert "discount" in (tmp_path / "monitor_orders_2026-08-09.csv").read_text().splitlines()[0]
    assert "discount" not in (tmp_path / "monitor_orders_2026-08-03.csv").read_text().splitlines()[0]
    generate.write_drop(tmp_path, dataset, "2026-08-06")
    assert len(read_csv(tmp_path / "orders_2026-08-06.csv")) == 3
    assert read_csv(tmp_path / "orders_2026-08-06.csv")[0]["order_id"] != "M080601", "a drop is not a monitoring day"


def test_the_command_line_writes_the_small_tables_and_the_days_with_a_flag_and_the_usual_files_without(
        tmp_path, capsys):
    assert generate.main(["--out", str(tmp_path / "story"), "--monitoring"]) == 0
    out = capsys.readouterr().out
    assert out.strip() == (f"Wrote 14 files to {tmp_path / 'story'}: the four small tables and 5 days of orders for "
                           "the monitoring story, 8 orders on a normal day")
    assert sorted(path.name for path in (tmp_path / "story").iterdir()) == sorted(
        ["restaurants.xlsx", "customers.csv", "menu_items.csv", "reviews.csv",
         *(f"monitor_{kind}_{day}.csv" for kind in ("orders", "order_lines")
           for day in ("2026-08-03", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09"))])

    assert generate.main(["--out", str(tmp_path / "usual")]) == 0
    assert capsys.readouterr().out.startswith("Wrote 22 files to ")
    assert not list((tmp_path / "usual").glob("monitor_*"))


# ====================================================================== the helpers of the layer


def test_a_moment_is_given_to_now_in_utc_to_the_second():
    assert run.stamp(dt.datetime(2026, 8, 5, 21, 34, 26, 999999, tzinfo=UTC)) == "2026-08-05T21:34:26Z"
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert run.stamp(dt.datetime(2026, 8, 6, 3, 4, 26, tzinfo=zone)) == "2026-08-05T21:34:26Z"


def test_the_freshness_of_a_day_is_measured_from_its_newest_order(dataset):
    normal, short = generate.MONITORING_DAYS[0], generate.MONITORING_DAYS[1]

    assert run.newest_order(dataset, normal) == dt.datetime(2026, 8, 3, 19, 34, 26, tzinfo=UTC)
    assert run.newest_order(dataset, short) == dt.datetime(2026, 8, 6, 16, 50, 42, tzinfo=UTC)


def test_an_alert_file_that_is_not_there_has_no_alerts_and_one_that_is_has_one_alert_for_each_line(tmp_path):
    assert run.alert_lines(tmp_path / "alerts.jsonl") == []
    (tmp_path / "alerts.jsonl").write_text('{"event": "a"}\n\n{"event": "b"}\n')
    assert run.alert_lines(tmp_path / "alerts.jsonl") == [{"event": "a"}, {"event": "b"}]


@pytest.mark.parametrize("revenue", [True, False])
def test_the_block_that_the_config_gains_is_printed_as_json_that_reads_back_as_the_block(tmp_path, revenue):
    workdir = run.prepare_workdir(tmp_path / "work")
    added = run.monitoring_config(workdir, revenue=revenue)

    lines = run.block_lines(added)
    assert json.loads("{" + "\n".join(lines) + "}") == added
    assert lines[0] == '"sla": {' and any(line == '"observability": {' for line in lines)
    assert ('"metrics"' in "\n".join(lines)) is revenue
    assert max(len(line) for line in lines) < 140, "a section is one line"


def test_the_work_folder_gets_the_sla_the_sinks_and_the_channel_and_the_committed_config_does_not(
        tmp_path, monkeypatch):
    monkeypatch.delenv(run.SLACK_ENV, raising=False)
    committed = (EXAMPLE / "config" / "orders.json").read_text()
    workdir = run.prepare_workdir(tmp_path / "work")

    added = run.monitoring_config(workdir, revenue=True)

    spec = json.loads((workdir / "config" / "orders.json").read_text())
    before = json.loads(committed)
    assert spec["metadata"]["sla"] == added["sla"] == {
        "freshness": {"warn_after": {"count": 26, "period": "hour"}, "error_after": {"count": 48, "period": "hour"},
                      "column": "order_ts"},
        "volume": {"min_rows": 5}, "schema": {"on_change": "warn"},
        "metrics": [{"metric": "revenue", "grain": "day", "min": 1000}]}
    assert spec["metadata"]["observability"] == {
        "sinks": ["iceberg", {"type": "jsonl", "path": "../.ldp/events.jsonl"}],
        "alerts": [{"type": "file", "path": "../.ldp/alerts.jsonl",
                    "on": ["monitor.error", "monitor.warn", "monitor.resolved"]}]}
    assert {key: value for key, value in spec["metadata"].items() if key not in ("sla", "observability")} == {
        key: value for key, value in before["metadata"].items() if key != "observability"}
    assert (EXAMPLE / "config" / "orders.json").read_text() == committed and "sla" not in before["metadata"]
    assert Config.from_json(workdir / "config" / "orders.json").sla is not None


def test_the_sla_watches_the_revenue_only_when_the_gold_table_can_be_built(tmp_path, monkeypatch):
    monkeypatch.delenv(run.SLACK_ENV, raising=False)
    without = run.monitoring_config(run.prepare_workdir(tmp_path / "plain"), revenue=False)
    with_revenue = run.monitoring_config(run.prepare_workdir(tmp_path / "gold"), revenue=True)

    assert "metrics" not in without["sla"] and set(without["sla"]) == {"freshness", "volume", "schema"}
    assert set(with_revenue["sla"]) == {"freshness", "volume", "schema", "metrics"}


def test_a_slack_channel_is_added_when_the_webhook_is_set_and_holds_the_name_of_the_variable_and_not_the_url(
        tmp_path, monkeypatch):
    monkeypatch.setenv(run.SLACK_ENV, "https://hooks.slack.com/services/T000/B000/secret")
    workdir = run.prepare_workdir(tmp_path / "work")

    added = run.monitoring_config(workdir, revenue=False)

    channels = added["observability"]["alerts"]
    assert [channel["type"] for channel in channels] == ["file", "slack"]
    assert channels[1] == {"type": "slack", "url_env": "LDP_SLACK_WEBHOOK",
                           "on": ["monitor.error", "monitor.warn", "monitor.resolved"]}
    assert "secret" not in (workdir / "config" / "orders.json").read_text()


def test_the_metrics_file_is_kept_to_what_the_one_gold_table_answers(tmp_path):
    workdir = run.prepare_workdir(tmp_path / "work")
    committed = json.loads((EXAMPLE / "config" / "metrics.json").read_text())

    run.trim_metrics(workdir / "config")

    trimmed = json.loads((workdir / "config" / "metrics.json").read_text())
    assert [model["name"] for model in trimmed["semantic_models"]] == ["daily_sales"]
    assert [metric["name"] for metric in trimmed["metrics"]] == ["revenue", "orders", "avg_ticket"]
    assert trimmed["identifier"] == committed["identifier"] and trimmed["semantic_models"][0] == (
        committed["semantic_models"][0])
    assert len(committed["semantic_models"]) == 3 and len(committed["metrics"]) == 6, "the committed file is whole"


def test_dbt_builds_the_whole_project_unless_a_selection_is_given_and_a_failure_is_the_message_of_ldp(
        tmp_path, monkeypatch):
    calls = []

    def fake(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="ldp: error: it broke")

    monkeypatch.setattr(run.subprocess, "run", fake)
    for select in ((), ("+gold_daily_sales", "stg_customers")):
        with pytest.raises(run.BuildFailed, match="it broke"):
            run.dbt_build(tmp_path, select)

    project = str(tmp_path / "dbt")
    assert calls[0][-3:] == ["dbt", "build", project] and "--select" not in calls[0]
    assert calls[1][-5:] == ["build", project, "--select", "+gold_daily_sales", "stg_customers"]


# ====================================================================== the story, without dbt


def test_the_layer_is_a_choice_of_the_command_line_and_needs_no_other_layer_first():
    assert run.LAYERS[-1] == "monitoring" and run.LAYER_NEEDS["monitoring"] == ()
    with pytest.raises(ValueError, match="unknown layer 'alerts'"):
        run.run_restaurant("unused", layer="alerts")


def test_the_layer_needs_no_extra_and_says_that_the_sla_has_no_revenue_and_that_the_revenue_step_is_skipped(demo):
    assert demo.summary["revenue"] is False and demo.summary["slack"] is False
    assert "The SLA has no entry for revenue, because the revenue is read from a gold table" in demo.text
    assert 'Skipping the revenue step: dbt is not installed (pip install "local-data-platform[dbt]").' in demo.text
    assert not (demo.workdir / "dbt" / "target").exists(), "dbt did not run"
    assert not (demo.workdir / "config" / "gold_daily_sales.json").exists(), "no gold table was published"
    assert "dimensions" not in demo.summary, "the four small tables were not loaded"


def test_the_commands_are_run_and_printed_in_the_order_of_the_story(demo):
    commands = [line.split()[2] for line in demo.text.splitlines() if line.startswith("   $ ldp ")]

    assert commands == ["run", "monitor", "run", "monitor", "run", "monitor", "run", "monitor", "run", "monitor",
                        "monitor", "status", "status"]
    assert [line for line in demo.text.splitlines() if "--now" in line and line.startswith("   $ ldp monitor")] == [
        f"   $ ldp monitor {run.shown(demo.workdir / 'config')} --now {now}" for now in NOW]
    assert f"--now 2026-08-10T06:10:00Z --html {run.shown(demo.workdir / 'status.html')}" in demo.text
    assert f"   $ ldp status {run.shown(demo.workdir / 'config')} --now 2026-08-10T06:10:00Z" in demo.text.splitlines()


def test_each_look_is_at_the_time_it_says_and_exits_with_the_status_of_its_findings(demo):
    looks = demo.summary["looks"]

    assert [look["after"] for look in looks] == AFTER
    assert [look["now"] for look in looks] == NOW
    assert [look["exit"] for look in looks] == EXIT_WITHOUT_DBT


def test_each_monitor_says_what_the_story_says_at_each_look(demo):
    assert [look["statuses"] for look in demo.summary["looks"]] == STATUSES_WITHOUT_DBT


def test_the_first_look_is_50_hours_after_the_newest_order_and_raises_the_one_alert(demo):
    first = demo.summary["looks"][0]

    assert first["now"] == "2026-08-05T21:34:26Z" and sent(first) == [("monitor.error", "freshness")]
    assert "the newest order_ts is 2026-08-03T19:34:26Z, 50.0 hour ago" in demo.text
    assert "Exit status 2 says that a monitor is in error. It is a finding and not a failure of ldp" in demo.text


def test_each_look_sends_the_alerts_of_its_changes_of_state_and_the_second_look_sends_nothing(demo):
    assert [sent(look) for look in demo.summary["looks"]] == ALERTS_WITHOUT_DBT
    assert [alert["event"] for alert in demo.summary["alerts"]] == [
        "monitor.error", "monitor.resolved", "monitor.error", "monitor.resolved", "monitor.warn", "monitor.resolved"]
    assert demo.text.count("Alerts sent to file ../.ldp/alerts.jsonl") == 4, "the last two looks have none to say"


def test_the_alerts_are_in_the_file_of_the_work_folder_and_each_says_how_to_look_closer(demo):
    path = demo.workdir / ".ldp" / "alerts.jsonl"

    assert demo.summary["files"]["alerts"] == str(path) and path.is_file()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == demo.summary["alerts"] and len(lines) == 6
    assert {line["pipeline"] for line in lines} == {"orders"} and {line["table"] for line in lines} == {"raw.orders"}
    assert {line["owner"] for line in lines} == {"restaurant owner"}
    assert {line["command"] for line in lines} == {f"ldp status {demo.workdir / 'config' / 'orders.json'}"}
    assert [(line["monitor"], line["status"], line.get("previous_status")) for line in lines] == [
        ("freshness", "error", None), ("freshness", "ok", "error"), ("volume.rows", "error", "ok"),
        ("volume.rows", "ok", "error"), ("schema", "warn", "ok"), ("schema", "ok", "warn")]
    assert [line["at"] for line in lines] == [NOW[0], NOW[1], NOW[1], NOW[2], NOW[2], NOW[3]]


def test_the_history_holds_a_result_for_each_monitor_at_each_look(demo):
    results = read_results(Config.from_json(demo.workdir / "config" / "orders.json"))

    assert len(results) == 18
    assert sorted({result.evaluated_at.strftime("%Y-%m-%dT%H:%M:%SZ") for result in results}) == sorted(NOW)
    assert {result.monitor for result in results} == {"freshness", "volume.rows", "schema"}
    assert [result.metrics["fingerprint"] for result in results if result.monitor == "schema"][0]


def test_the_schema_warns_once_for_the_new_column_and_then_agrees_with_it(demo):
    schema = [look["statuses"]["schema"] for look in demo.summary["looks"]]

    assert schema == ["ok", "ok", "warn", "ok", "ok", "ok"]
    assert "the table gained the column discount (double), the source gained the column discount (double)" in demo.text


def test_the_orders_table_has_the_five_nights_and_the_new_column(demo):
    table = run.iceberg_from_config(Config.from_json(demo.workdir / "config" / "orders.json"), "target")

    assert demo.summary["tables"] == {"raw.orders": {"rows": 35, "snapshots": 5}}
    assert table.row_count() == 8 + 3 + 8 + 8 + 8 and len(table.snapshots()) == 5
    assert "discount" in table.table().schema().column_names


def test_the_status_screen_and_its_page_are_written_and_the_page_asks_for_nothing(demo):
    page = Path(demo.summary["files"]["status"])

    assert page == demo.workdir / "status.html" and page.is_file()
    text = page.read_text()
    assert "orders" in text and "https://" not in text.replace("http://www.w3.org", "")
    assert "last_run is empty because the runs of this walkthrough were written when you ran it" in demo.text
    assert "1 ok, 6 no sla" in demo.text and "History goes back to 2026-08-05T21:34:26Z" in demo.text


def test_the_summary_table_has_a_row_for_each_look_with_what_each_monitor_said(demo):
    table = demo.text.split("== 8. Summary")[1].split("Try these next:")[0]
    rows = [line for line in table.splitlines() if line.strip()[:1].isdigit()]

    assert len(rows) == 6
    assert [row.split()[-6:] for row in rows] == [
        ["2", "error", "ok", "ok", "-", "1"], ["2", "ok", "error", "ok", "-", "2"],
        ["0", "ok", "ok", "warn", "-", "2"], ["0", "ok", "ok", "ok", "-", "1"],
        ["0", "ok", "ok", "ok", "-", "0"], ["0", "ok", "ok", "ok", "-", "0"]]
    assert "2026-08-05T21:34:26Z  a normal day" in rows[0]
    tries = demo.text.split("Try these next:")[1]
    assert "ldp status" in tries and "ldp alert test" in tries and "ldp maintain" in tries and "--system" in tries


def test_the_layer_runs_again_in_its_own_folder_and_says_the_same_thing(demo, monkeypatch):
    monkeypatch.setattr(run, "dbt_installed", lambda: False)
    monkeypatch.delenv(run.SLACK_ENV, raising=False)

    again = narrated(demo.workdir)

    assert [look["statuses"] for look in again.summary["looks"]] == STATUSES_WITHOUT_DBT
    assert [sent(look) for look in again.summary["looks"]] == ALERTS_WITHOUT_DBT


def test_quiet_prints_only_the_last_line_and_the_exit_status_is_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "dbt_installed", lambda: False)
    monkeypatch.delenv(run.SLACK_ENV, raising=False)

    status = run.main(["--workdir", str(tmp_path / "work"), "--layer", "monitoring", "--quiet"])

    out = capsys.readouterr().out.strip().splitlines()
    assert status == 0 and len(out) == 1
    assert out[0].startswith("restaurant: {'raw.orders': {'rows': 35, 'snapshots': 5}} in ")


def test_a_folder_that_is_not_the_examples_is_refused_and_the_message_says_so(tmp_path, capsys):
    (tmp_path / "keep.txt").write_text("mine")

    status = run.main(["--workdir", str(tmp_path), "--layer", "monitoring"])

    assert status == 1 and "was not created by this example" in capsys.readouterr().err
    assert (tmp_path / "keep.txt").read_text() == "mine"


# ====================================================================== Slack, as a local server


class Hook(BaseHTTPRequestHandler):
    posts: list[bytes] = []

    def do_POST(self):  # noqa: N802 - the name http.server asks for
        length = int(self.headers.get("Content-Length", 0))
        type(self).posts.append(self.rfile.read(length))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


@pytest.fixture
def slack():
    """A Slack webhook on localhost. Yields its URL and the bodies that were posted to it."""
    handler = type("Handler", (Hook,), {"posts": []})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/services/T000/B000/hook", handler.posts
    finally:
        server.shutdown()
        server.server_close()


def test_with_a_slack_webhook_the_same_alerts_are_posted_to_it_as_well_as_written_to_the_file(
        tmp_path, monkeypatch, slack):
    url, posts = slack
    monkeypatch.setattr(run, "dbt_installed", lambda: False)
    monkeypatch.setenv(run.SLACK_ENV, url)

    result = narrated(tmp_path / "work")

    assert result.summary["slack"] is True
    assert [sent(look) for look in result.summary["looks"]] == ALERTS_WITHOUT_DBT
    texts = [json.loads(body)["text"] for body in posts]
    assert len(texts) == len(result.summary["alerts"]) == 6
    assert texts[0].startswith("ldp monitor error on orders (table raw.orders, owner restaurant owner)")
    assert "freshness: observed 50.0 hour" in texts[0]
    assert [text.splitlines()[0].split(" on ")[0] for text in texts] == [
        "ldp monitor error", "ldp monitor resolved", "ldp monitor error", "ldp monitor resolved",
        "ldp monitor warning", "ldp monitor resolved"]
    assert "LDP_SLACK_WEBHOOK is set, so the same alerts were posted to that Slack webhook." in result.text
    assert url not in (result.workdir / "config" / "orders.json").read_text()
    assert url not in result.text


def test_without_the_webhook_nothing_is_posted_and_the_layer_makes_no_connection(tmp_path, monkeypatch):
    import socket

    monkeypatch.setattr(run, "dbt_installed", lambda: False)
    monkeypatch.delenv(run.SLACK_ENV, raising=False)

    def refuse(self, address):
        raise AssertionError(f"the layer opened a connection to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    result = narrated(tmp_path / "work")

    assert result.summary["slack"] is False and "Slack" not in result.text


# ====================================================================== the revenue step, with dbt


@pytest.mark.dbt
def test_with_dbt_the_revenue_is_read_from_the_gold_table_and_its_drop_raises_an_error_that_the_fresh_day_resolves(
        tmp_path, network_trap, monkeypatch):
    environment, connections = network_trap
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, environment[name])
    monkeypatch.delenv(run.SLACK_ENV, raising=False)

    result = narrated(tmp_path / "work")

    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert result.summary["revenue"] is True
    looks = result.summary["looks"]
    assert [look["exit"] for look in looks] == [2, 2, 0, 2, 0, 0]
    assert [look["statuses"]["metric.revenue.value"] for look in looks] == ["unknown", "ok", "ok", "error", "ok", "ok"]
    assert [look["statuses"]["freshness"] for look in looks] == ["error", "ok", "ok", "ok", "ok", "ok"]
    assert [sent(look) for look in looks] == [
        [("monitor.error", "freshness")],
        [("monitor.error", "volume.rows"), ("monitor.resolved", "freshness")],
        [("monitor.resolved", "volume.rows"), ("monitor.warn", "schema")],
        [("monitor.error", "metric.revenue.value"), ("monitor.resolved", "schema")],
        [("monitor.resolved", "metric.revenue.value")],
        []]
    assert len(result.summary["alerts"]) == 8
    values = {look["now"]: result_of(result.workdir, look["now"]) for look in looks}
    assert values["2026-08-07T06:00:00Z"] == (1950, "2026-08-06")
    assert values["2026-08-08T06:00:00Z"] == (6330, "2026-08-07")
    assert values["2026-08-09T06:00:00Z"] == (633, "2026-08-08")
    assert values["2026-08-10T06:00:00Z"] == (6330, "2026-08-09")
    assert [release.name for release in releases.list_releases(result.workdir / "config")] == [
        "r5", "r4", "r3", "r2", "r1"]
    assert (json.loads((result.workdir / "config" / "metrics.json").read_text())["metrics"][0]["name"]) == "revenue"
    assert set(result.summary["dimensions"]) == {"raw.restaurants", "raw.customers", "raw.menu_items", "raw.reviews"}
    assert "release r5 cut with 1 table and 23 rows in gold" in result.text
    assert "revenue for day 2026-08-08 was 633, below the 1000 that min asks for" in result.text


def result_of(workdir: Path, now: str):
    """The value and the day of the revenue result of the look at ``now``."""
    config = Config.from_json(workdir / "config" / "orders.json")
    found = [result for result in read_results(config) if result.monitor == "metric.revenue.value"
             and result.evaluated_at.strftime("%Y-%m-%dT%H:%M:%SZ") == now]
    if not found or found[0].metrics.get("value") is None:
        return None
    return found[0].metrics["value"], found[0].metrics["period"][:10] if "period" in found[0].metrics else None
