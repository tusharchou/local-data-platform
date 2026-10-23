"""The orchestration layer of the restaurant example, ``run.py --layer orchestration``, run offline.

The layer drops three nights of orders into a folder with a bad row in the third, backfills them with the daily
workflow, fixes the row and runs the same command again, runs it a third time, and lets a scheduler tick do the fourth
night. Most tests read one run of it without dbt, which also checks that the layer needs neither the ``dbt`` nor the
``excel`` extra. The test with the models task runs the real dbt, so it carries the ``dbt`` marker and the ``dbt`` CI
job runs it. The numbers are the ones the example prints for the fixed seed.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_data_platform import Config
from local_data_platform.events import read_jsonl, summarize_runs
from local_data_platform.exceptions import EngineNotFound
from local_data_platform.format.iceberg.commit import commit_log
from local_data_platform.gold import release as releases
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.spec import plan
from tests.support import load_example_module

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "restaurant"
run = load_example_module("restaurant_run_orchestration", EXAMPLE / "run.py")
generate = run.generate

DAYS = ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05")
ORDERS_PER_DAY = [3, 4, 7, 7]
LINES_PER_DAY = [8, 10, 20, 16]
GOLD_PER_DAY = [3, 3, 4, 4]


def narrated(workdir: Path, **options) -> SimpleNamespace:
    """Run the layer, and return its summary, its work folder and everything it printed."""
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        whole = run.run_restaurant(workdir, layer="orchestration", **options)
    return SimpleNamespace(summary=whole["orchestration"], whole=whole, workdir=Path(whole["workdir"]),
                           text=printed.getvalue())


def story(ledger: Path) -> list[dict]:
    """The runs of the workflow in the ledger, oldest first, with the window and the status of each task."""
    events = list(read_jsonl(ledger))
    windows = {event.run_id: event.payload["window"][0][:10] for event in events if event.type == "workflow.started"}
    runs = summarize_runs(events)
    flows = sorted((item for item in runs if item["is_workflow"]), key=lambda item: item["started_at"])
    return [{"window": windows[flow["run_id"]], "status": flow["status"],
             # a published model is a run of its own that carries the task id of its task, so the task is the one
             # whose pipeline is its id
             "tasks": {item["task_id"]: item["status"] for item in runs
                       if item["workflow_run_id"] == flow["run_id"] and item["pipeline"] == item["task_id"]}}
            for flow in flows]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """One run of the layer with dbt hidden and the Excel reader blocked, which is how a plain install runs it."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(run, "dbt_installed", lambda: False)
        patch.setitem(sys.modules, "openpyxl", None)  # an import of it fails, as if the excel extra were missing
        return narrated(tmp_path_factory.mktemp("restaurant") / "work")


# ====================================================================== what the example ships


def test_the_daily_workflow_and_its_two_configs_are_valid_and_every_pipeline_can_replace_a_window():
    for name in ("orders", "order_lines"):
        result = plan(EXAMPLE / "workflows" / f"{name}.json", window="2026-08-01/2026-08-02")
        assert result["valid"], result["errors"]
        assert (result["window_size"], result["window_column"]) == ("day", "order_ts"), "a replace needs a column"
        assert result["source_path"] == f"../drops/{name}_2026-08-01.csv"
    workflow = json.loads((EXAMPLE / "workflows" / "daily_orders.json").read_text())
    assert (workflow["kind"], workflow["identifier"], workflow["metadata"]["schedule"]) == (
        "Workflow", "daily_orders", "5 2 * * *")
    assert [(task["id"], task.get("depends_on", [])) for task in workflow["tasks"]] == [
        ("orders", []), ("order_lines", []), ("models", ["orders", "order_lines"]), ("gold", ["models"]),
        ("tidy", ["gold"])]
    models = next(task for task in workflow["tasks"] if task["id"] == "models")["dbt"]
    assert models == {"project": "../dbt", "select": ["+gold_daily_sales", "stg_customers"]}
    assert next(task for task in workflow["tasks"] if task["id"] == "gold")["gold"] == {"dir": "../config"}


def test_the_windowed_orders_config_is_the_orders_config_of_the_warehouse_layer_with_a_window():
    daily = json.loads((EXAMPLE / "workflows" / "orders.json").read_text())["metadata"]
    batch = json.loads((EXAMPLE / "config" / "orders.json").read_text())["metadata"]

    assert daily["target"] == batch["target"], "the same table, with the same partitions"
    assert daily["quality"] == batch["quality"], "the same checks"
    assert daily["window"] == {"size": "day", "column": "order_ts"}


def test_the_work_folder_gets_the_workflow_files_next_to_the_drops_they_read(tmp_path):
    work = run.prepare_workdir(tmp_path / "work")

    assert sorted(path.name for path in (work / "workflows").iterdir()) == [
        "daily_orders.json", "order_lines.json", "orders.json"]
    assert (work / "workflows" / "daily_orders.json").read_text() == (
        EXAMPLE / "workflows" / "daily_orders.json").read_text()


def test_a_ledger_in_the_example_folder_is_not_copied_into_a_work_folder(tmp_path):
    ledger = EXAMPLE / "workflows" / ".ldp"
    assert not ledger.exists(), "a run in the example folder left its ledger behind"
    ledger.mkdir()
    try:
        (ledger / "events.jsonl").write_text("{}\n")
        work = run.prepare_workdir(tmp_path / "work")
    finally:
        (ledger / "events.jsonl").unlink()
        ledger.rmdir()

    assert not (work / "workflows" / ".ldp").exists()


# ====================================================================== the story, without dbt


def test_the_layer_needs_no_extra_and_skips_the_models_task_with_a_line_that_says_how_to_install_dbt(demo):
    assert demo.summary["models"] is False
    assert ('Skipping the models and gold tasks: dbt is not installed (pip install "local-data-platform[dbt]")'
            in demo.text)
    shipped = json.loads((EXAMPLE / "workflows" / "daily_orders.json").read_text())
    used = json.loads((demo.workdir / "workflows" / "daily_orders.json").read_text())
    assert [task["id"] for task in used["tasks"]] == ["orders", "order_lines", "tidy"]
    assert next(task for task in used["tasks"] if task["id"] == "tidy")["depends_on"] == ["orders", "order_lines"]
    assert [task["id"] for task in shipped["tasks"]] == ["orders", "order_lines", "models", "gold", "tidy"], \
        "the file in the example is not changed"


def test_the_workflow_without_models_makes_what_waited_for_the_models_wait_for_what_they_waited_for():
    spec = {"tasks": [{"id": "a", "pipeline": "a.json"}, {"id": "b", "pipeline": "b.json"},
                      {"id": "m", "depends_on": ["a", "b"], "dbt": {"project": "p"}},
                      {"id": "n", "depends_on": ["m", "a"], "maintain": {"config": "a.json"}},
                      {"id": "z", "pipeline": "z.json"}]}

    assert run.without_models(spec)["tasks"] == [
        {"id": "a", "pipeline": "a.json"}, {"id": "b", "pipeline": "b.json"},
        {"id": "n", "depends_on": ["a", "b"], "maintain": {"config": "a.json"}}, {"id": "z", "pipeline": "z.json"}]
    assert spec["tasks"][2]["id"] == "m", "the spec it was given is not changed"


def test_the_workflow_without_models_also_leaves_out_the_gold_task_that_builds_what_the_models_publish():
    spec = json.loads((EXAMPLE / "workflows" / "daily_orders.json").read_text())

    tasks = run.without_models(spec)["tasks"]

    assert [(task["id"], task.get("depends_on")) for task in tasks] == [
        ("orders", None), ("order_lines", None), ("tidy", ["orders", "order_lines"])]
    assert all("gold" not in task and "dbt" not in task for task in tasks)


def test_the_first_backfill_publishes_two_days_and_a_data_gate_holds_back_the_third(demo):
    assert demo.summary["first_backfill"] == {"exit": 3, "published": 2, "skipped": 0, "blocked": 1, "failed": 0}
    assert "[3/3] 2026-08-03T00:00:00Z/2026-08-04T00:00:00Z  blocked" in demo.text
    assert "accepted_values(channel)" in demo.text and "drive_thru" in demo.text
    assert demo.summary["bad_order"] == "O000861"


def test_after_the_fix_the_same_command_runs_only_the_third_day_and_a_third_run_skips_every_day(demo):
    assert demo.summary["second_backfill"] == {"exit": 0, "published": 1, "skipped": 2, "blocked": 0, "failed": 0}
    assert demo.summary["third_backfill"] == {"exit": 0, "published": 0, "skipped": 3, "blocked": 0, "failed": 0}


def test_the_tick_of_the_next_night_loads_the_fourth_day_and_nothing_is_missed_or_due_after_it(demo):
    assert demo.summary["tick"] == {"exit": 0, "published": 1, "skipped": 0, "blocked": 0, "failed": 0}
    status = json.loads(subprocess.run(
        [sys.executable, "-m", "local_data_platform.cli", "schedule", "list", str(demo.workdir), "--now",
         run.LIST_AT, "--json"], capture_output=True, text=True, check=True).stdout)
    (workflow,) = status["scheduled"]
    assert workflow["last_window"] == "2026-08-04T00:00:00Z/2026-08-05T00:00:00Z"
    assert workflow["last_status"] == "succeeded" and workflow["due"] == [] and workflow["missed"] == []
    assert workflow["next_fire"] == "2026-08-06T02:05:00Z"


def test_the_ledger_tells_the_story_of_each_run_of_the_workflow(demo):
    flows = story(demo.workdir / "workflows" / ".ldp" / "events.jsonl")

    ok = {"orders": "published", "order_lines": "published", "tidy": "succeeded"}
    assert [(flow["window"], flow["status"]) for flow in flows] == [
        ("2026-08-01", "succeeded"), ("2026-08-02", "succeeded"), ("2026-08-03", "blocked_quality"),
        ("2026-08-03", "succeeded"), ("2026-08-04", "succeeded")]
    assert [flow["tasks"] for flow in flows] == [
        ok, ok,
        {"orders": "blocked_quality", "order_lines": "published", "tidy": "upstream_failed"},
        {"orders": "published", "order_lines": "skipped_duplicate", "tidy": "succeeded"},
        ok]


def test_there_is_one_publish_for_each_day_under_the_key_that_ldp_plan_prints_for_it(demo):
    config = demo.workdir / "workflows" / "orders.json"
    table = iceberg_from_config(Config.from_json(config), "target")

    published = commit_log(table.table())

    keys = [plan(config, window=f"{day}/{after}")["idempotency_key"] for day, after in zip(DAYS, DAYS[1:])]
    assert [row["idempotency_key"] for row in reversed(published)] == keys
    assert [row["total_records"] for row in reversed(published)] == ["3", "7", "14", "21"]
    assert demo.summary["publishes"] == 4


def test_each_day_has_its_rows_once_in_the_raw_tables(demo):
    days = demo.summary["days"]

    assert [row["day"] for row in days] == list(DAYS[:4])
    assert [row["raw.orders"] for row in days] == ORDERS_PER_DAY
    assert [row["raw.order_lines"] for row in days] == LINES_PER_DAY
    assert all("gold.gold_daily_sales" not in row for row in days), "no models, no gold table"
    assert {name: info["rows"] for name, info in demo.summary["tables"].items()} == {
        "raw.orders": 21, "raw.order_lines": 54}


def test_the_demo_ends_with_a_table_of_the_days_and_three_commands_to_try(demo):
    summary = demo.text.split("== 8. Summary", 1)[1]

    assert "day         raw.orders  raw.order_lines" in summary
    assert "2026-08-03           7               20" in summary
    assert "finished in" in summary and "Try these next:" in summary
    assert [line.split()[1:3] for line in summary.splitlines() if line.startswith("     ldp ")] == [
        ["schedule", "list"], ["backfill", str(demo.workdir / "workflows" / "daily_orders.json")],
        ["schedule", "install"]]
    assert "--dry-run" in summary and "launchd" in summary


def test_the_demo_prints_every_command_it_runs_in_the_order_of_the_story(demo):
    typed = [line.split("$ ldp ", 1)[1].split() for line in demo.text.splitlines() if "$ ldp " in line]

    assert [" ".join(words[:2]) if words[0] in ("workflow", "schedule") else words[0] for words in typed] == [
        "workflow plan", "backfill", "backfill", "backfill", "schedule tick", "schedule list", "runs", "commits",
        "schedule install"]
    assert "Exit status 3: a data gate held 3 August back" in demo.text
    assert "== 7. What happened, from the ledger and from the table" in demo.text


# ====================================================================== running it again, and the options


def test_running_it_again_in_the_same_folder_starts_from_nothing_and_gives_the_same_story(tmp_path):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(run, "dbt_installed", lambda: False)
        first = narrated(tmp_path / "work", quiet=True)
        (first.workdir / "notes.txt").write_text("keep me")
        again = narrated(tmp_path / "work", quiet=True)

    assert again.summary["first_backfill"] == first.summary["first_backfill"]
    assert again.summary["tables"] == first.summary["tables"], "not twice the rows"
    assert (again.workdir / "notes.txt").read_text() == "keep me"
    assert len(story(again.workdir / "workflows" / ".ldp" / "events.jsonl")) == 5, "the old ledger is gone"
    assert (again.workdir / ".ldp" / "locks").is_dir()


def test_the_work_folder_of_the_warehouse_layer_is_cleared_for_this_layer(tmp_path):
    work = run.prepare_workdir(tmp_path / "work")
    for name in ("landing", "warehouse", "drops"):
        (work / name).mkdir(exist_ok=True)
        (work / name / "old.csv").write_text("old")
    (work / ".ldp").mkdir()
    (work / ".ldp" / "old.lock").write_text("old")

    run.prepare_workdir(work)

    assert not any((work / name / "old.csv").exists() for name in ("landing", "warehouse", "drops"))
    assert not (work / ".ldp").exists()


def test_the_layer_runs_from_the_command_line_and_prints_one_line_when_quiet(tmp_path, capsys):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(run, "dbt_installed", lambda: False)

        assert run.main(["--workdir", str(tmp_path / "work"), "--layer", "orchestration", "--quiet"]) == 0

    printed = capsys.readouterr().out
    assert printed.count("\n") == 1 and printed.startswith("restaurant: {'raw.orders': {'rows': 21")


def test_a_step_that_fails_for_another_reason_than_a_data_gate_ends_the_layer_with_a_message(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "dbt_installed", lambda: False)
    real = run.ldp_main

    def failing_backfill(argv):
        if argv[0] == "backfill":
            print("ldp: error: the ledger is locked", file=sys.stderr)
            return 1
        return real(argv)

    monkeypatch.setattr(run, "ldp_main", failing_backfill)

    assert run.main(["--workdir", str(tmp_path / "work"), "--layer", "orchestration", "--quiet"]) == 1

    error = capsys.readouterr().err
    assert error.startswith("error: ldp backfill ")
    assert error.endswith("daily_orders.json failed: ldp: error: the ledger is locked\n")


def test_what_a_child_process_writes_to_standard_error_is_kept_and_shown_only_when_the_step_fails(
        monkeypatch, capfd):
    def noisy(argv):
        subprocess.run([sys.executable, "-c", "import os; os.write(2, b'dbt log line\\n')"], check=True)
        print("the result")
        return 0 if argv[0] == "ok" else 2

    monkeypatch.setattr(run, "ldp_main", noisy)
    out = run.Narrator()

    status, stdout = run.ldp(out, "ok")
    with pytest.raises(run.StepFailed, match="ldp bad failed: dbt log line"):
        run.ldp(out, "bad")

    shown = capfd.readouterr()
    assert (status, stdout) == (0, "the result\n") and "dbt log line" not in shown.err + shown.out
    os.write(2, b"after\n")  # the descriptor is given back, so the next write is seen as before
    assert capfd.readouterr().err == "after\n"


def test_a_status_the_layer_expects_is_not_a_failure_and_any_other_is(monkeypatch):
    monkeypatch.setattr(run, "ldp_main", lambda argv: int(argv[0]))
    out = run.Narrator(quiet=True)

    assert run.ldp(out, "3", ok=(0, 3))[0] == 3
    with pytest.raises(run.StepFailed):
        run.ldp(out, "3")
    with pytest.raises(run.StepFailed):
        run.ldp(out, "1", ok=(0, 3))


def test_dbt_is_installed_when_it_can_be_found_and_not_when_it_cannot(monkeypatch):
    from local_data_platform.dbt_layer import project

    monkeypatch.setattr(project, "find_dbt", lambda *args, **kwargs: "/usr/bin/dbt")
    assert run.dbt_installed() is True

    def missing(*args, **kwargs):
        raise EngineNotFound("dbt was not found")

    monkeypatch.setattr(project, "find_dbt", missing)
    assert run.dbt_installed() is False


def test_the_helpers_read_a_count_line_and_change_a_channel_in_a_drop(tmp_path):
    assert run.counts_of("backfill x: 2 published, 0 skipped, 1 blocked, 0 failed in 1.1s.") == {
        "published": 2, "skipped": 0, "blocked": 1, "failed": 0}
    with pytest.raises(run.StepFailed):
        run.counts_of("nothing to see")
    drop = tmp_path / "orders.csv"
    drop.write_text("order_id,channel\nO1,dine_in\nO2,takeaway\n")

    assert run.change_channel(drop, "drive_thru") == ("O1", "dine_in")
    assert drop.read_text() == "order_id,channel\nO1,drive_thru\nO2,takeaway\n"
    assert run.change_channel(drop, "dine_in") == ("O1", "drive_thru")


def test_an_unknown_layer_names_the_three_layers(tmp_path):
    with pytest.raises(ValueError, match=r"the layers are \['warehouse', 'orchestration', 'gold'\]"):
        run.run_restaurant(tmp_path / "work", layer="bronze", quiet=True)


# ====================================================================== with dbt


@pytest.mark.dbt
def test_with_dbt_the_models_task_builds_the_gold_table_for_each_day_and_waits_for_the_gate(
        tmp_path, network_trap, monkeypatch):
    environment, connections = network_trap
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, environment[name])

    demo = narrated(tmp_path / "work")

    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert demo.summary["models"] is True and "Skipping the models" not in demo.text
    assert demo.summary["dimensions"] == {"raw.restaurants": 6, "raw.customers": 150, "raw.menu_items": 36,
                                          "raw.reviews": 94}
    assert not (demo.workdir / "config" / "metrics.json").exists(), "one gold table here, so no metrics to check"
    assert [demo.summary[name]["exit"] for name in ("first_backfill", "second_backfill", "third_backfill", "tick")] == [
        3, 0, 0, 0]
    assert [row["gold.gold_daily_sales"] for row in demo.summary["days"]] == GOLD_PER_DAY
    assert demo.summary["tables"]["silver.fct_orders"]["rows"] == 21
    assert demo.summary["tables"]["gold.gold_daily_sales"]["rows"] == sum(GOLD_PER_DAY)
    assert "gold.gold_daily_sales so far: 2026-08-01 3 rows, 2026-08-02 3 rows\n" in demo.text
    assert "gold.gold_daily_sales now: 2026-08-01 3 rows, 2026-08-02 3 rows, 2026-08-03 4 rows, " \
           "2026-08-04 4 rows" in demo.text
    assert "Running with dbt" not in demo.text and "Completed successfully" not in demo.text, "dbt's log stays out"
    flows = story(demo.workdir / "workflows" / ".ldp" / "events.jsonl")
    ok = {"orders": "published", "order_lines": "published", "models": "succeeded", "gold": "succeeded",
          "tidy": "succeeded"}
    assert [flow["tasks"] for flow in flows] == [
        ok, ok,
        {"orders": "blocked_quality", "order_lines": "published", "models": "upstream_failed",
         "gold": "upstream_failed", "tidy": "upstream_failed"},
        {"orders": "published", "order_lines": "skipped_duplicate", "models": "succeeded", "gold": "succeeded",
         "tidy": "succeeded"},
        ok]
    assert os.path.isfile(demo.workdir / "dbt" / "target" / "run_results.json")


@pytest.mark.dbt
def test_with_dbt_each_day_that_passes_the_gate_cuts_a_release_of_the_gold_table_and_the_blocked_day_cuts_none(
        tmp_path):
    demo = narrated(tmp_path / "work", quiet=True)

    known = releases.list_releases(demo.workdir / "config")

    assert [release.name for release in reversed(known)] == ["r1", "r2", "r3", "r4"], "one for each day that passed"
    assert [[item.table for item in release.tables] for release in known] == [["gold.gold_daily_sales"]] * 4
    assert [release.workflow["task"] for release in known] == ["gold"] * 4
    assert [release.window[0][:10] for release in reversed(known)] == [
        "2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04"]
    assert [item.rows for item in reversed([release.tables[0] for release in known])] == [3, 6, 10, 14]
