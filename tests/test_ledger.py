"""The ledger: what the events say about each window, and the ids that carry on a run that did not succeed.

The scans are tested on ledgers written by hand, so every shape of line is one the test chose, and the carrying on is
tested on real runs of a workflow whose source file arrives late.
"""

import json
import types
from pathlib import Path

import pytest

from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import commit_log
from local_data_platform.orchestration import ledger as ledger_module
from local_data_platform.orchestration import run_workflow
from local_data_platform.orchestration.ledger import Continuation, read_windows, run_status, trace_runs
from local_data_platform.orchestration.workflow import load_workflow

CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}
WINDOW = ("2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z")
OTHER = ("2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z")


def _started(run_id, window=WINDOW, workflow="w", attempt=1):
    return {"type": "workflow.started", "run_id": run_id, "attempt": attempt,
            "payload": {"workflow": workflow, "window": list(window)}}


def _finished(run_id, tasks, workflow="w", attempt=1):
    return {"type": "workflow.finished", "run_id": run_id, "attempt": attempt,
            "payload": {"workflow": workflow, "tasks": tasks}}


def _task_event(kind, task_run, run_id, task_id, attempt=1):
    return {"type": kind, "run_id": task_run, "attempt": attempt,
            "payload": {"workflow": "w", "workflow_run_id": run_id, "task_id": task_id}}


def _ledger(tmp_path: Path, *lines) -> Path:
    path = tmp_path / ".ldp" / "events.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("".join((line if isinstance(line, str) else json.dumps(line)) + "\n" for line in lines))
    return path


# ====================================================================== what the ledger says about each window


def test_the_last_run_of_each_window_is_read_with_its_attempt_and_how_the_last_finished_run_ended(tmp_path):
    path = _ledger(
        tmp_path,
        _started("a"), _finished("a", {"orders": "failed"}),
        _started("b"), _finished("b", {"orders": "published"}),
        _started("c", OTHER), _started("c", OTHER, attempt=2), _finished("c", {"orders": "blocked_quality"}, attempt=2))

    records = read_windows(path, "w")

    assert set(records) == {WINDOW, OTHER}
    assert (records[WINDOW].run_id, records[WINDOW].attempt, dict(records[WINDOW].tasks)) == (
        "b", 1, {"orders": "published"}), "the last run of the window"
    assert (records[OTHER].run_id, records[OTHER].attempt, dict(records[OTHER].tasks)) == (
        "c", 2, {"orders": "blocked_quality"}), "one run, carried on, with the attempts of its starts"


def test_a_window_whose_last_run_never_finished_keeps_the_end_of_the_last_run_that_did(tmp_path):
    path = _ledger(tmp_path, _started("a"), _finished("a", {"orders": "failed"}), _started("b"), _started("c", OTHER))

    records = read_windows(path, "w")

    assert (records[WINDOW].run_id, dict(records[WINDOW].tasks)) == ("b", {"orders": "failed"})
    assert (records[OTHER].run_id, records[OTHER].tasks) == ("c", None), "no run of this window finished"


def test_the_runs_of_another_workflow_that_shares_the_ledger_are_not_read(tmp_path):
    path = _ledger(tmp_path, _started("a"), _finished("a", {"orders": "published"}),
                   _started("x", OTHER, workflow="other"), _finished("x", {"orders": "failed"}, workflow="other"))

    assert set(read_windows(path, "w")) == {WINDOW}
    assert set(read_windows(path, "other")) == {OTHER}
    assert read_windows(path, "nobody") == {}


def test_a_ledger_with_junk_in_it_is_read_as_far_as_it_goes(tmp_path):
    path = _ledger(
        tmp_path, "", "not json at all but it says workflow.started", '{"payload": 5} "workflow.finished"',
        '["workflow.started"]', '"workflow.finished"', '{"type": "workflow.started", "run_id": "x"}',
        '{"type": "workflow.finished", "payload": {"workflow": "w"}}',
        '{"type": "workflow.started", "run_id": "y", "payload": 5}',
        '{"type": 7, "run_id": "z", "payload": {"workflow": "w", "window": ["a", "b"]}, "x": "workflow."}',
        '{"type": "workflow.started", "run_id": 5, "payload": {"workflow": "w", "window": ["a", "b"]}}',
        _started("short", ("only one end",)), _finished("short", {"orders": "failed"}),
        _started("g"), _finished("g", "not a mapping"),
        {"type": "workflow.started", "run_id": "odd", "attempt": "two", "payload": {
            "workflow": "w", "window": list(OTHER)}},
        {"type": "workflow.started", "run_id": "zero", "attempt": 0, "payload": {
            "workflow": "w", "window": ["2026-06-09T00:00:00Z", "2026-06-10T00:00:00Z"]}},
        _started("ok", ("2026-06-03T00:00:00Z", "2026-06-04T00:00:00Z")),
        _finished("ok", {"orders": "published"}))

    records = read_windows(path, "w")

    assert (records[OTHER].run_id, records[OTHER].attempt) == ("odd", 1), "a bad attempt reads as the first"
    assert records[("2026-06-09T00:00:00Z", "2026-06-10T00:00:00Z")].attempt == 1, "and so does an attempt of 0"
    assert records[WINDOW].run_id == "g" and records[WINDOW].tasks is None
    assert dict(records[("2026-06-03T00:00:00Z", "2026-06-04T00:00:00Z")].tasks) == {"orders": "published"}


def _counting_parser(monkeypatch):
    """Count what the ledger module parses with ``json.loads``, without touching anyone else's ``json``."""
    parsed = []
    real = json.loads

    def loads(text, *args, **kwargs):
        parsed.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(ledger_module, "json", types.SimpleNamespace(loads=loads))
    return parsed


def test_reading_the_windows_parses_the_workflow_events_and_not_the_events_of_the_tasks(tmp_path, monkeypatch):
    path = _ledger(tmp_path, _started("a"),
                   *[_task_event("run.started", f"t{n}", "a", "orders") for n in range(200)],
                   _finished("a", {"orders": "published"}))
    parsed = _counting_parser(monkeypatch)

    records = read_windows(path, "w")

    assert len(parsed) == 2 and dict(records[WINDOW].tasks) == {"orders": "published"}


def test_tracing_a_run_parses_only_the_lines_that_name_it(tmp_path, monkeypatch):
    path = _ledger(tmp_path, _started("run-0001"), _started("run-0002", OTHER),
                   *[_task_event("run.started", f"t{n}", "run-0002", "orders") for n in range(200)],
                   _task_event("run.started", "task-run", "run-0001", "orders", attempt=2))
    parsed = _counting_parser(monkeypatch)

    found = trace_runs(path, {"run-0001"})

    assert len(parsed) == 2 and found["run-0001"].tasks == {"orders": ("task-run", 2)}


def test_a_ledger_that_does_not_exist_has_no_windows(tmp_path):
    assert read_windows(tmp_path / ".ldp" / "events.jsonl", "w") == {}
    assert trace_runs(tmp_path / ".ldp" / "events.jsonl", {"a"}) == {}


@pytest.mark.parametrize("tasks, expected", [
    (None, None),
    ({"a": "published", "b": "succeeded"}, "succeeded"),
    ({"a": "skipped_duplicate", "b": "succeeded"}, "succeeded"),
    ({"a": "published", "b": "failed"}, "failed"),
    ({"a": "failed", "b": "blocked_quality"}, "failed"),
    ({"a": "published", "b": "blocked_quality"}, "blocked"),
    ({"a": "published", "b": "upstream_failed"}, "incomplete"),
    ({"a": "published"}, "incomplete"),
    ({}, "incomplete"),
])
def test_what_the_last_run_of_a_window_says(tasks, expected):
    assert run_status(tasks, ["a", "b"]) == expected


# ====================================================================== the ids that carry on a run


def test_the_ids_of_a_run_are_its_own_and_the_run_id_and_the_highest_attempt_of_each_task(tmp_path):
    path = _ledger(
        tmp_path,
        _started("a"),
        _task_event("run.started", "t-orders", "a", "orders"), _task_event("run.finished", "t-orders", "a", "orders"),
        _task_event("run.started", "t-orders", "a", "orders", attempt=2),
        _task_event("run.started", "t-export", "a", "export"),
        _finished("a", {"orders": "failed", "export": "upstream_failed"}),
        _started("a", attempt=2),
        _task_event("run.started", "t-orders", "a", "orders", attempt=3),
        _task_event("run.started", "t-other", "b", "orders", attempt=9),
        _started("b", OTHER))

    found = trace_runs(path, {"a", "missing"})

    assert set(found) == {"a"}
    assert found["a"] == Continuation("a", 2, {"orders": ("t-orders", 3), "export": ("t-export", 1)})
    assert found["a"].task("orders") == ("t-orders", 3) and found["a"].task("new") == (None, 0)


def test_several_runs_are_traced_in_one_scan_and_no_run_is_the_empty_result(tmp_path):
    path = _ledger(tmp_path, _started("a"), _task_event("run.started", "t1", "a", "orders"),
                   _started("b", OTHER), _task_event("run.started", "t2", "b", "orders", attempt=4))

    found = trace_runs(path, ["a", "b"])

    assert found["a"].tasks == {"orders": ("t1", 1)} and found["b"].tasks == {"orders": ("t2", 4)}
    assert trace_runs(path, []) == {}


def test_a_task_that_ran_under_two_run_ids_is_traced_to_the_one_of_its_latest_attempt(tmp_path):
    path = _ledger(tmp_path, _started("a"), _task_event("run.started", "first", "a", "orders"),
                   _task_event("run.started", "second", "a", "orders", attempt=2))

    assert trace_runs(path, {"a"})["a"].tasks == {"orders": ("second", 2)}


def test_a_trace_skips_what_is_not_an_event_and_events_without_a_task(tmp_path):
    path = _ledger(tmp_path, "not json run a", '["a"]', '{"run_id": "a"}', _started("a"),
                   {"type": "run.started", "run_id": "t", "attempt": 1, "payload": {"workflow_run_id": "a"}},
                   {"type": "run.started", "run_id": "t", "attempt": 1,
                    "payload": {"workflow_run_id": "a", "task_id": 7}})

    assert trace_runs(path, {"a"}) == {"a": Continuation("a", 1, {})}


# ====================================================================== carrying on a run


def _project(root: Path, *, extra_tasks=None, retries=None) -> Path:
    """A workflow of one pipeline that loads ``landing/orders.csv``, a file that is not there until a test writes it."""
    (root / "config").mkdir(parents=True)
    (root / "config" / "orders.json").write_text(json.dumps({"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG},
        "window": {"size": "day", "column": "at"}}}))
    tasks = [{"id": "orders", "pipeline": "config/orders.json"}] + (extra_tasks or [])
    metadata = {"schedule": "@daily", "window": {"size": "day"}, **({"retries": retries} if retries else {})}
    path = root / "daily.json"
    path.write_text(json.dumps({"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily", "metadata": metadata,
                                "tasks": tasks}))
    return path


def _land(root: Path) -> None:
    (root / "landing").mkdir(exist_ok=True)
    (root / "landing" / "orders.csv").write_text("id,at\n1,2026-06-01T10:00:00\n2,2026-06-01T11:00:00\n")


def _events(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / ".ldp" / "events.jsonl").read_text().splitlines()]


def test_a_run_carried_on_keeps_the_run_ids_and_goes_on_with_the_next_attempts(tmp_path):
    workflow = load_workflow(_project(tmp_path))
    first = run_workflow(workflow, window="2026-06-01/2026-06-02")
    assert first.status == "failed", "the file has not landed"
    _land(tmp_path)
    carried = trace_runs(workflow.ledger_path, {first.run_id})[first.run_id]

    second = run_workflow(workflow, window="2026-06-01/2026-06-02", continues=carried)

    assert second.status == "succeeded" and second.run_id == first.run_id
    assert second.tasks["orders"].attempts == 1, "the attempts of this run, and not of the ones before"
    task_runs = [e for e in _events(tmp_path) if e["type"] == "run.started"]
    assert [e["attempt"] for e in task_runs] == [1, 2] and len({e["run_id"] for e in task_runs}) == 1
    starts = [e for e in _events(tmp_path) if e["type"] == "workflow.started"]
    assert [(e["run_id"], e["attempt"]) for e in starts] == [(first.run_id, 1), (first.run_id, 2)]
    table = Iceberg("orders", {"identifier": "shop", "warehouse_path": str(tmp_path / "warehouse")}).table()
    [published] = commit_log(table)
    assert published["attempt"] == "2"
    assert published["run_id"].replace("-", "") == task_runs[0]["run_id"].replace("-", "")


def test_the_retries_of_a_run_that_is_carried_on_count_from_the_attempt_it_starts_with(tmp_path):
    workflow = load_workflow(_project(tmp_path, retries={"max_attempts": 3, "delay_s": 5}))
    waits = []
    first = run_workflow(workflow, window="2026-06-01/2026-06-02", sleep=waits.append)
    assert first.tasks["orders"].attempts == 3 and len(waits) == 2, "a missing file is an OSError, which is retried"
    carried = trace_runs(workflow.ledger_path, {first.run_id})[first.run_id]
    assert carried.tasks["orders"][1] == 3 and carried.attempt == 1
    waits.clear()

    second = run_workflow(workflow, window="2026-06-01/2026-06-02", sleep=waits.append, continues=carried)

    assert second.tasks["orders"].attempts == 3, "this run gets its 3 attempts, which are the attempts 4, 5 and 6"
    assert [e["attempt"] for e in _events(tmp_path) if e["type"] == "run.started"] == [1, 2, 3, 4, 5, 6]
    assert len(waits) == 2 and all(0 <= wait <= 10 for wait in waits), "the waits count from the first retry again"


def test_a_task_that_was_held_back_by_the_failure_before_it_goes_on_under_its_run_id_with_the_next_attempt(tmp_path):
    workflow = load_workflow(_project(tmp_path, extra_tasks=[
        {"id": "tidy", "maintain": {"config": "config/orders.json"}, "depends_on": ["orders"]}]))
    failed = run_workflow(workflow, window="2026-06-01/2026-06-02")
    assert failed.tasks["orders"].status == "failed" and failed.tasks["tidy"].status == "upstream_failed"
    carried = trace_runs(workflow.ledger_path, {failed.run_id})[failed.run_id]
    tidy_run, tidy_attempt = carried.tasks["tidy"]
    assert tidy_attempt == 1, "the event that said it was held back is its first attempt"
    _land(tmp_path)

    second = run_workflow(workflow, window="2026-06-01/2026-06-02", continues=carried)

    assert second.status == "succeeded" and second.tasks["tidy"].status == "succeeded"
    tidy = [e for e in _events(tmp_path) if e["type"] == "run.started" and e["payload"]["task_id"] == "tidy"]
    assert [(e["run_id"], e["attempt"]) for e in tidy] == [(tidy_run, 1), (tidy_run, 2)]


def test_a_task_that_the_earlier_run_did_not_have_starts_a_run_of_its_own(tmp_path):
    _land(tmp_path)
    workflow = load_workflow(_project(tmp_path, extra_tasks=[
        {"id": "tidy", "maintain": {"config": "config/orders.json"}, "depends_on": ["orders"]}]))
    carried = Continuation("the-run", 1, {"orders": ("the-orders-run", 2)})

    result = run_workflow(workflow, window="2026-06-01/2026-06-02", continues=carried)

    assert result.status == "succeeded" and result.run_id == "the-run"
    events = _events(tmp_path)
    assert [(e["run_id"], e["attempt"]) for e in events if e["type"] == "workflow.started"] == [("the-run", 2)]
    started = {e["payload"]["task_id"]: (e["run_id"], e["attempt"]) for e in events if e["type"] == "run.started"}
    assert started["orders"] == ("the-orders-run", 3)
    assert started["tidy"][0] != "the-orders-run" and started["tidy"][1] == 1


def test_a_task_that_is_held_back_again_by_a_failure_that_has_not_passed_goes_on_under_its_run_id(tmp_path):
    workflow = load_workflow(_project(tmp_path, extra_tasks=[
        {"id": "tidy", "maintain": {"config": "config/orders.json"}, "depends_on": ["orders"]}]))
    failed = run_workflow(workflow, window="2026-06-01/2026-06-02")
    carried = trace_runs(workflow.ledger_path, {failed.run_id})[failed.run_id]
    tidy_run, _ = carried.tasks["tidy"]

    again = run_workflow(workflow, window="2026-06-01/2026-06-02", continues=carried)

    assert again.tasks["orders"].status == "failed" and again.tasks["tidy"].status == "upstream_failed"
    assert again.tasks["tidy"].attempts == 0
    tidy = [e for e in _events(tmp_path) if e["type"] == "run.started" and e["payload"]["task_id"] == "tidy"]
    assert [(e["run_id"], e["attempt"]) for e in tidy] == [(tidy_run, 1), (tidy_run, 2)]


def test_a_run_that_is_not_carried_on_starts_new_ids_for_the_same_window(tmp_path):
    workflow = load_workflow(_project(tmp_path))
    first = run_workflow(workflow, window="2026-06-01/2026-06-02")
    _land(tmp_path)

    second = run_workflow(workflow, window="2026-06-01/2026-06-02")

    assert second.run_id != first.run_id
    assert [e["attempt"] for e in _events(tmp_path) if e["type"] == "workflow.started"] == [1, 1]
    assert len({e["run_id"] for e in _events(tmp_path) if e["type"] == "run.started"}) == 2


def test_the_events_of_a_carried_on_run_survive_the_scan_that_reads_them_back(tmp_path):
    workflow = load_workflow(_project(tmp_path))
    first = run_workflow(workflow, window="2026-06-01/2026-06-02")
    _land(tmp_path)
    run_workflow(workflow, window="2026-06-01/2026-06-02",
                 continues=trace_runs(workflow.ledger_path, {first.run_id})[first.run_id])

    records = read_windows(workflow.ledger_path, "daily")

    [record] = records.values()
    assert record.run_id == first.run_id and record.attempt == 2
    assert dict(record.tasks) == {"orders": "published"} and run_status(record.tasks, ["orders"]) == "succeeded"
