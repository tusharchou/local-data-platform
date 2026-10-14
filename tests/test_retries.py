"""Task retries: a failed task of a workflow runs again, as the same run, and never loads its window twice.

A workflow, and each of its tasks, may set ``retries``. A task that fails with an error that another attempt may fix,
such as a dropped connection or a file that has not landed yet, runs again after a wait, with the run id it had and the
next attempt number. A pipeline task keeps its idempotency key, so the new attempt fences the old one, and a load that
already reached the table is found and not written again.

Every test is offline and never really waits: ``run_workflow`` takes the function that sleeps, and the tests pass one
that only records the wait, or one that makes the missing file land, as the wait over a real drop would.
"""

import json
import random
import threading
from pathlib import Path

import pytest
from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException

from local_data_platform.cli import main
from local_data_platform.events import MemorySink, read_jsonl, summarize_runs, uuid7
from local_data_platform.exceptions import ConfigError, DataQualityError, LDPError, PipelineNotFound, TableNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg import commit as commit_module
from local_data_platform.format.iceberg.commit import CommitConflict, commit_log, fence_marker, staged_branches
from local_data_platform.orchestration import (
    RetryPolicy,
    TaskBlocked,
    TaskFailed,
    TaskOutcome,
    TaskPlan,
    is_retriable,
    load_workflow,
    register_task_kind,
    run_workflow,
)
from local_data_platform.orchestration.retries import DEFAULT_BACKOFF, DEFAULT_DELAY_S, MAX_DELAY_S, retries_problems
from local_data_platform.orchestration.tasks import unregister_task_kind
from local_data_platform.pipeline import Pipeline
from local_data_platform.spec import idempotency_key, json_schema, kind_schema, spec_hash, validate_spec

DAY1 = "2026-08-01/2026-08-02"
ORDERS = "id,at\n1,2026-08-01T10:00:00\n2,2026-08-01T11:00:00\n3,2026-08-02T09:00:00\n"  # two rows on day 1
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}
THREE = {"max_attempts": 3, "delay_s": 30, "backoff": 2.0}
NO_WAIT = {"max_attempts": 3, "delay_s": 0}


class Longest:
    """A jitter that always draws the longest wait, so that a test can say exactly what the waits are."""

    @staticmethod
    def uniform(low: float, high: float) -> float:
        return high


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _orders_config(root: Path, **metadata) -> Path:
    return _write(root / "config" / "orders.json", {"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG},
        "window": {"size": "day", "column": "at"}, "quality": [{"check": "row_count", "min": 2}], **metadata}})


def _project(root: Path, *, retries=None, task_retries=None, landed: bool = True, tasks=None) -> Path:
    """Write the landing file, the configs and the workflow ``daily.json``, which has an ``orders`` task by default."""
    if landed:
        _write(root / "landing" / "orders.csv", ORDERS)
    _orders_config(root)
    _write(root / "config" / "export.json", {"identifier": "export_orders", "metadata": {
        "source": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "orders_export", "format": "CSV", "path": "../out/orders.csv"}}})
    orders: dict = {"id": "orders", "pipeline": "config/orders.json"}
    if task_retries is not None:
        orders["retries"] = task_retries
    metadata: dict = {"schedule": "@daily", "window": {"size": "day"}}
    if retries is not None:
        metadata["retries"] = retries
    return _write(root / "daily.json", {
        "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders", "metadata": metadata,
        "tasks": tasks if tasks is not None else [orders]})


def _orders(root: Path) -> Iceberg:
    return Iceberg("orders", {"identifier": "shop", "warehouse_path": str(root / "warehouse")})


def _events(root: Path) -> list:
    return read_jsonl(root / ".ldp" / "events.jsonl")


def _of_task(root: Path, task_id: str = "orders") -> list:
    return [event for event in _events(root) if event.payload.get("task_id") == task_id]


def _by_attempt(events: list) -> dict[int, list[str]]:
    """The event types of each attempt, in order."""
    grouped: dict[int, list[str]] = {}
    for event in sorted(events, key=lambda item: (item.attempt, item.seq)):
        grouped.setdefault(event.attempt, []).append(event.type)
    return grouped


def _the_run_id(events: list) -> str:
    run_ids = {event.run_id for event in events}
    assert len(run_ids) == 1, f"the attempts of a task share one run id, got {run_ids}"
    return run_ids.pop()


def _lands_after(root: Path, waits: list, count: int):
    """A ``sleep`` that records the wait and, at wait number ``count``, makes the missing landing file arrive."""
    def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == count:
            _write(root / "landing" / "orders.csv", ORDERS)
    return sleep


def _kind(name: str, run) -> None:
    """Register a task kind called ``name`` that does what ``run`` does. The test unregisters it."""
    register_task_kind(name, validate=lambda value: [], json_schema=dict, plan=lambda task, ctx: TaskPlan(), run=run)


class Flaky:
    """Makes ``Pipeline.extract`` raise ``error`` for its first ``times`` calls, and read the source after that."""

    def __init__(self, monkeypatch, error, times: int = 1):
        self.calls, self.error, self.times = 0, error, times
        real = Pipeline.extract

        def extract(pipeline):
            self.calls += 1
            if self.calls <= self.times:
                raise self.error
            return real(pipeline)

        monkeypatch.setattr(Pipeline, "extract", extract)


# ====================================================================== the policy and its settings


def test_a_task_is_tried_once_unless_it_says_otherwise(tmp_path):
    workflow = load_workflow(_project(tmp_path))

    assert workflow.retries_of(workflow.tasks[0]) == RetryPolicy() and RetryPolicy().max_attempts == 1
    assert RetryPolicy().enabled is False and RetryPolicy(max_attempts=2).enabled is True


def test_a_task_overrides_the_workflow_one_key_at_a_time(tmp_path):
    both = {"max_attempts": 4, "delay_s": 10, "backoff": 3}
    workflow = load_workflow(_project(tmp_path, retries=both, task_retries={"max_attempts": 2}))

    assert workflow.retries_of(workflow.tasks[0]) == RetryPolicy(max_attempts=2, delay_s=10.0, backoff=3.0)
    one = load_workflow(_project(tmp_path / "other", retries=THREE, task_retries={"max_attempts": 1}))
    assert one.retries_of(one.tasks[0]).enabled is False, "a task can turn the retries of the workflow off"


def test_a_setting_that_gives_only_the_attempts_gets_the_default_delay_and_backoff():
    policy = RetryPolicy.from_settings({"max_attempts": 3})

    assert (policy.delay_s, policy.backoff) == (DEFAULT_DELAY_S, DEFAULT_BACKOFF) == (30.0, 2.0)
    assert RetryPolicy.from_settings(None, None) == RetryPolicy()
    assert RetryPolicy.from_settings({"max_attempts": 2, "unknown": 1}, "not an object").max_attempts == 2


def test_the_longest_wait_grows_by_the_backoff_and_stops_at_a_day():
    policy = RetryPolicy(max_attempts=10, delay_s=30, backoff=2.0)

    assert [policy.delay_cap(n) for n in (1, 2, 3, 4)] == [30.0, 60.0, 120.0, 240.0]
    assert RetryPolicy(delay_s=1000, backoff=10).delay_cap(5) == MAX_DELAY_S == 86400.0
    assert RetryPolicy(delay_s=30, backoff=1e6).delay_cap(500) == MAX_DELAY_S, "an overflow is a day, not an error"
    assert RetryPolicy(delay_s=0, backoff=2).delay_cap(3) == 0.0


def test_the_wait_is_a_full_jitter_from_zero_to_the_longest_wait():
    policy = RetryPolicy(max_attempts=5, delay_s=30, backoff=2.0)
    rng = random.Random(7)

    draws = [policy.delay(2, rng) for _ in range(500)]

    assert all(0.0 <= draw <= 60.0 for draw in draws)
    assert min(draws) < 6.0 and max(draws) > 54.0, "the whole range is used, not only the top"
    assert policy.delay(1, random.Random(3)) == policy.delay(1, random.Random(3)), "a seeded generator repeats"
    assert 0.0 <= policy.delay(1) <= 30.0, "a new generator by default"
    assert policy.delay(2, Longest()) == 60.0 and policy.to_dict() == {"max_attempts": 5, "delay_s": 30, "backoff": 2.0}


@pytest.mark.parametrize("value, expected", [
    ({"max_attempts": 3, "delay_s": 30, "backoff": 2.0}, []),
    ({"max_attempts": 1}, []),
    ({}, []),
    (3, [("", "must be an object such as")]),
    ({"tries": 3}, [("", "has unknown keys ['tries']; allowed: ['max_attempts', 'delay_s', 'backoff']")]),
    ({"max_attempts": 0}, [(".max_attempts", "must be a whole number from 1, got 0")]),
    ({"max_attempts": True}, [(".max_attempts", "must be a whole number from 1, got True")]),
    ({"max_attempts": 2.0}, [(".max_attempts", "must be a whole number from 1, got 2.0")]),
    ({"delay_s": -1}, [(".delay_s", "must be a number from 0, got -1")]),
    ({"delay_s": "30"}, [(".delay_s", "must be a number from 0, got '30'")]),
    ({"backoff": 0.5}, [(".backoff", "must be a number from 1, got 0.5")]),
    ({"max_attempts": 0, "backoff": 0}, [(".max_attempts", "must be a whole number from 1, got 0"),
                                         (".backoff", "must be a number from 1, got 0")]),
])
def test_a_retries_object_is_checked(value, expected):
    problems = retries_problems(value)

    assert len(problems) == len(expected)
    for (path, message), (expected_path, expected_message) in zip(problems, expected):
        assert path == expected_path and message.startswith(expected_message), problems


# ====================================================================== which errors are retried


@pytest.mark.parametrize("error", [
    OSError("the network went away"), ConnectionError("reset"), TimeoutError("slow"), FileNotFoundError("not here yet"),
    BrokenPipeError(), CommitConflict("the publish attempts ran out", retriable=True),
    CommitFailedException("lost the race"), CommitStateUnknownException("the gateway timed out"),
], ids=lambda error: type(error).__name__)
def test_these_errors_are_retried(error):
    assert is_retriable(error) is True


@pytest.mark.parametrize("error", [
    ConfigError("the config is wrong"), PipelineNotFound("no route"), ValueError("a bad value"), KeyError("a"),
    TypeError("a"), RuntimeError("a"), LDPError("something of ldp"), TableNotFound("no table"),
    TaskFailed("a task failed"), TaskBlocked("a data gate"),
    CommitConflict("fenced by a newer attempt", retriable=False),
], ids=lambda error: type(error).__name__)
def test_these_errors_are_never_retried(error):
    assert is_retriable(error) is False


@pytest.mark.parametrize("never", [DataQualityError, ConfigError, PipelineNotFound],
                         ids=lambda never: never.__name__)
def test_an_error_on_the_never_list_is_not_retried_even_when_it_is_also_an_os_error(never):
    class Odd(never, OSError):
        pass

    assert is_retriable(Odd("looks like an I/O error, and is not")) is False, "the never-list wins"
    assert is_retriable(type("Fine", (OSError,), {})("an I/O error")) is True


def test_the_connection_errors_of_requests_are_retried():
    requests = pytest.importorskip("requests")

    assert is_retriable(requests.exceptions.ConnectionError("no route to host")) is True
    assert is_retriable(requests.exceptions.ReadTimeout("slow")) is True


def test_an_error_that_says_whether_it_is_retriable_is_believed():
    class Fenced(OSError):
        retriable = False

    assert is_retriable(Fenced("stop")) is False
    assert is_retriable(type("Maybe", (ValueError,), {"retriable": True})("try again")) is True
    assert is_retriable(type("Vague", (OSError,), {"retriable": "yes"})("not a bool")) is True, "only a bool counts"


# ====================================================================== a retried pipeline task


def test_two_transient_source_failures_then_a_success_load_the_window_exactly_once(tmp_path):
    path = _project(tmp_path, retries=THREE, landed=False)  # the file has not landed: attempts 1 and 2 fail
    waits: list[float] = []

    result = run_workflow(path, window=DAY1, sleep=_lands_after(tmp_path, waits, 2), rng=Longest())

    task = result.tasks["orders"]
    assert task.status == "published" and task.attempts == 3 and result.status == "succeeded" and result.exit_code == 0
    assert waits == [30.0, 60.0], "up to 30 s after the first attempt, up to 60 s after the second"
    assert _orders(tmp_path).row_count() == 2, "the two rows of the window, once"
    summary = _orders(tmp_path).table().current_snapshot().summary
    assert summary["ldp.attempt"] == "3" and summary["ldp.run-id"] == _the_run_id(_of_task(tmp_path))
    assert task.to_dict()["attempts"] == 3 and task.error is None, "a task that got through has no error"


def test_each_attempt_emits_its_own_events_with_its_attempt_number_under_one_run_id(tmp_path):
    path = _project(tmp_path, retries=THREE, landed=False)

    run_workflow(path, window=DAY1, sleep=_lands_after(tmp_path, [], 2), rng=Longest())

    events = _of_task(tmp_path)
    assert _by_attempt(events) == {
        1: ["run.started", "run.failed", "run.finished"], 2: ["run.started", "run.failed", "run.finished"],
        3: ["run.started", "run.extracted", "quality.evaluated", "run.published", "run.finished"]}
    failed = [event for event in events if event.type == "run.failed"]
    assert [(e.attempt, e.payload["stage"], e.payload["error_type"]) for e in failed] == [
        (1, "extract", "FileNotFoundError"), (2, "extract", "FileNotFoundError")]
    finished = {event.attempt: event.payload["status"] for event in events if event.type == "run.finished"}
    assert finished == {1: "failed", 2: "failed", 3: "published"}
    assert all(event.payload["workflow"] == "daily_orders" and event.payload["task_id"] == "orders" for event in events)
    assert len({event.payload["workflow_run_id"] for event in events}) == 1, "one run of the workflow"
    _the_run_id(events)


def test_every_attempt_runs_with_the_key_that_ldp_plan_prints(tmp_path):
    from local_data_platform import Config

    path = _project(tmp_path, retries=THREE, landed=False)

    run_workflow(path, window=DAY1, sleep=_lands_after(tmp_path, [], 2), rng=Longest())

    keys = {event.attempt: event.payload["idempotency_key"]
            for event in _of_task(tmp_path) if event.type == "run.started"}
    expected = idempotency_key(Config.from_json(tmp_path / "config" / "orders.json"), DAY1)
    assert keys == {1: expected, 2: expected, 3: expected}, "a retry is the same key, so it can be found and fenced"


def test_ldp_runs_shows_the_last_attempt_of_each_run_and_all_of_them_on_request(tmp_path, capsys):
    path = _project(tmp_path, retries=THREE, landed=False)
    run_workflow(path, window=DAY1, sleep=_lands_after(tmp_path, [], 2), rng=Longest())
    ledger = str(tmp_path / ".ldp" / "events.jsonl")
    run_id = _the_run_id(_of_task(tmp_path))

    assert main(["runs", ledger]) == 0
    last = [line for line in capsys.readouterr().out.splitlines() if run_id in line]
    assert len(last) == 1 and last[0].split()[1] == "3" and "published" in last[0]

    assert main(["runs", ledger, "--attempts"]) == 0
    every = [line for line in capsys.readouterr().out.splitlines() if run_id in line]
    assert sorted(line.split()[1] for line in every) == ["1", "2", "3"]
    statuses = [word for line in every for word in line.split() if word in ("failed", "published")]
    assert sorted(statuses) == ["failed", "failed", "published"]


def test_summarize_runs_can_keep_only_the_last_attempt_of_each_run():
    sink = MemorySink()
    from local_data_platform.events import RunEmitter

    for run, attempts in (("a", 3), ("b", 1)):
        for attempt in range(1, attempts + 1):
            emitter = RunEmitter(sink, run, attempt=attempt, context={"pipeline": "p"})
            emitter.emit("run.started", {})
            emitter.emit("run.finished", {"status": "failed" if attempt < attempts else "published"})

    everything = summarize_runs(sink.events)
    last = summarize_runs(sink.events, final_attempts_only=True)

    assert sorted((run["run_id"], run["attempt"]) for run in everything) == [("a", 1), ("a", 2), ("a", 3), ("b", 1)]
    assert sorted((run["run_id"], run["attempt"], run["status"]) for run in last) == [
        ("a", 3, "published"), ("b", 1, "published")]


@pytest.mark.parametrize("error", [
    OSError("a blip"), ConnectionError("reset"), TimeoutError("slow"), FileNotFoundError("not here yet"),
    CommitConflict("the publish attempts ran out", retriable=True), CommitFailedException("lost the race"),
    CommitStateUnknownException("the gateway timed out"),
], ids=lambda error: type(error).__name__)
def test_a_task_that_fails_once_with_a_retried_error_succeeds_on_its_second_attempt(tmp_path, monkeypatch, error):
    path = _project(tmp_path, retries=THREE)
    flaky = Flaky(monkeypatch, error)
    waits: list[float] = []

    result = run_workflow(path, window=DAY1, sleep=waits.append, rng=Longest())

    assert result.tasks["orders"].status == "published" and result.tasks["orders"].attempts == 2
    assert flaky.calls == 2 and waits == [30.0] and _orders(tmp_path).row_count() == 2


def test_the_connection_errors_of_requests_make_a_task_retry(tmp_path, monkeypatch):
    requests = pytest.importorskip("requests")
    path = _project(tmp_path, retries=THREE)
    Flaky(monkeypatch, requests.exceptions.ConnectionError("no route to host"))

    result = run_workflow(path, window=DAY1, sleep=lambda seconds: None, rng=Longest())

    assert result.tasks["orders"].status == "published" and result.tasks["orders"].attempts == 2


@pytest.mark.parametrize("error", [
    ConfigError("the config is wrong"), PipelineNotFound("no route"), ValueError("a bad value"), KeyError("a"),
    LDPError("something of ldp"), CommitConflict("fenced by a newer attempt", retriable=False),
], ids=lambda error: type(error).__name__ + (f"-{error.retriable}" if hasattr(error, "retriable") else ""))
def test_a_task_that_fails_with_an_error_that_cannot_be_fixed_is_not_retried(tmp_path, monkeypatch, error):
    path = _project(tmp_path, retries=THREE)
    flaky = Flaky(monkeypatch, error, times=99)
    waits: list[float] = []

    result = run_workflow(path, window=DAY1, sleep=waits.append, rng=Longest())

    task = result.tasks["orders"]
    assert task.status == "failed" and task.attempts == 1 and flaky.calls == 1 and waits == []
    assert result.exit_code == 1 and type(error).__name__ in task.error
    assert len([e for e in _of_task(tmp_path) if e.type == "run.failed"]) == 1


def test_a_quality_gate_is_a_data_gate_and_is_never_retried(tmp_path):
    path = _project(tmp_path, retries=THREE)
    _write(tmp_path / "landing" / "orders.csv", "id,at\n1,2026-08-01T10:00:00\n")  # one row, and the check wants two
    waits: list[float] = []

    result = run_workflow(path, window=DAY1, sleep=waits.append)

    task = result.tasks["orders"]
    assert task.status == "blocked_quality" and task.attempts == 1 and waits == []
    assert result.status == "blocked_quality" and result.exit_code == 3
    assert [e.type for e in _of_task(tmp_path) if e.type == "run.blocked_quality"] == ["run.blocked_quality"]


def test_a_task_that_keeps_failing_is_tried_as_often_as_it_may_and_then_fails(tmp_path):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]}]
    path = _project(tmp_path, retries=THREE, landed=False, tasks=tasks)
    waits: list[float] = []

    result = run_workflow(path, window=DAY1, sleep=waits.append, rng=Longest())

    orders, export = result.tasks["orders"], result.tasks["export"]
    assert (orders.status, orders.attempts) == ("failed", 3) and waits == [30.0, 60.0], "no wait after the last attempt"
    assert "FileNotFoundError" in orders.error and result.exit_code == 1
    assert (export.status, export.attempts) == ("upstream_failed", 0), "a task that did not run was not tried"
    assert [(e.attempt, e.type) for e in _of_task(tmp_path) if e.type == "run.failed"] == [
        (1, "run.failed"), (2, "run.failed"), (3, "run.failed")]
    assert not _orders(tmp_path).exists(), "nothing was loaded"


def test_a_task_that_got_through_on_a_retry_lets_the_tasks_after_it_run(tmp_path):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]}]
    path = _project(tmp_path, retries=THREE, landed=False, tasks=tasks)

    result = run_workflow(path, window=DAY1, sleep=_lands_after(tmp_path, [], 1), rng=Longest())

    assert {task_id: (task.status, task.attempts) for task_id, task in result.tasks.items()} == {
        "orders": ("published", 2), "export": ("published", 1)}
    assert (tmp_path / "out" / "orders.csv").is_file()


def test_a_task_overrides_the_retries_of_the_workflow_in_a_run(tmp_path):
    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "once", "pipeline": "config/missing.json", "retries": {"max_attempts": 1}},
             {"id": "twice", "pipeline": "config/missing.json", "retries": {"max_attempts": 2}}]
    path = _project(tmp_path, retries={"max_attempts": 4, "delay_s": 10}, landed=False, tasks=tasks)
    _write(tmp_path / "config" / "missing.json", {"identifier": "missing", "metadata": {
        "source": {"name": "missing", "format": "CSV", "path": "../landing/missing.csv"},
        "target": {"name": "missing", "format": "ICEBERG", "write_mode": "append", "catalog": CATALOG}}})
    waits: list[float] = []

    result = run_workflow(path, window=DAY1, sleep=waits.append, rng=Longest())

    assert {task_id: task.attempts for task_id, task in result.tasks.items()} == {"orders": 4, "once": 1, "twice": 2}
    assert waits == [10.0, 20.0, 40.0, 10.0], "the delay of every task is the workflow's, and a task sets the attempts"


def test_a_load_that_landed_before_the_error_is_found_by_its_key_and_not_written_again(tmp_path, monkeypatch):
    path = _project(tmp_path, retries=THREE)
    real_write, calls = Pipeline.write, []

    def write_then_lose_the_answer(pipeline, df, mode=None, **kwargs):
        result = real_write(pipeline, df, mode, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            raise CommitStateUnknownException("the gateway timed out after the commit")
        return result

    monkeypatch.setattr(Pipeline, "write", write_then_lose_the_answer)

    result = run_workflow(path, window=DAY1, sleep=lambda seconds: None, rng=Longest())

    task = result.tasks["orders"]
    assert (task.status, task.attempts) == ("skipped_duplicate", 2), "attempt 2 found the key attempt 1 published"
    assert len(calls) == 1 and _orders(tmp_path).row_count() == 2, "the window was written once"
    assert _orders(tmp_path).table().current_snapshot().summary["ldp.attempt"] == "1"
    assert len(commit_log(_orders(tmp_path).table())) == 1


def test_an_attempt_that_failed_after_it_staged_is_fenced_by_the_retry(tmp_path, monkeypatch):
    path = _project(tmp_path, retries=THREE)
    real_publish, calls = commit_module.publish, []

    def publish_once_the_disk_is_back(catalog, table, staged):
        calls.append(staged.branch)
        if len(calls) == 1:
            raise OSError("the disk went away before the publish")  # the rows are staged, and main has not moved
        return real_publish(catalog, table, staged)

    monkeypatch.setattr(commit_module, "publish", publish_once_the_disk_is_back)

    result = run_workflow(path, window=DAY1, sleep=lambda seconds: None, rng=Longest())

    assert (result.tasks["orders"].status, result.tasks["orders"].attempts) == ("published", 2)
    table = _orders(tmp_path).table()
    run_id = _the_run_id(_of_task(tmp_path))
    assert _orders(tmp_path).row_count() == 2 and table.current_snapshot().summary["ldp.attempt"] == "2"
    assert fence_marker(run_id, 1) in table.metadata.refs, "attempt 1 can never publish now"
    assert staged_branches(table) == {}, "the branch attempt 1 left behind is gone"
    assert "_a1_" in calls[0] and "_a2_" in calls[1]


def test_a_zombie_attempt_that_wakes_up_after_its_successor_published_cannot_write_again(tmp_path, monkeypatch):
    from local_data_platform.etl import run_config

    _project(tmp_path)
    config = str(tmp_path / "config" / "orders.json")
    run = uuid7()
    staged, resume, real_publish, outcome = threading.Event(), threading.Event(), commit_module.publish, {}

    def publish(catalog, table, staged_write):
        if "_a1_" not in staged_write.branch:
            return real_publish(catalog, table, staged_write)
        staged.set()  # attempt 1 has staged its rows, and the machine it runs on stalls
        assert resume.wait(60)
        try:
            return real_publish(catalog, table, staged_write)
        except CommitFailedException as error:
            outcome["publish"] = error
            raise

    monkeypatch.setattr(commit_module, "publish", publish)

    def zombie():
        try:
            outcome["result"] = run_config(config, window=DAY1, run_id=run, attempt=1)
        except BaseException as error:  # noqa: BLE001 - the thread reports what happened
            outcome["error"] = error

    thread = threading.Thread(target=zombie)
    thread.start()
    assert staged.wait(60)
    retry = run_config(config, window=DAY1, run_id=run, attempt=2)  # the retry: its first commit fences attempt 1
    assert retry.status == "published" and _orders(tmp_path).row_count() == 2
    resume.set()
    thread.join(60)

    assert not thread.is_alive()
    assert isinstance(outcome["publish"], CommitFailedException), "the zombie could not publish"
    assert "error" not in outcome and outcome["result"].status == "skipped_duplicate"
    assert outcome["result"].published_snapshot_id == retry.published_snapshot_id, "it found what its successor wrote"
    table = _orders(tmp_path).table()
    assert _orders(tmp_path).row_count() == 2, "the rows of the window, once"
    [published] = commit_log(table)
    assert published["attempt"] == "2" and published["run_id"] == run
    assert fence_marker(run, 1) in table.metadata.refs and staged_branches(table) == {}


def test_a_pipeline_that_writes_a_file_is_retried_as_the_same_run(tmp_path, monkeypatch):
    from local_data_platform.format.csv import CSV

    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]}]
    path = _project(tmp_path, retries=THREE, tasks=tasks)
    real_put, calls = CSV.put, []

    def put_after_the_disk_is_back(self, df, allow_empty=False):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("no space left on device")
        return real_put(self, df, allow_empty)

    monkeypatch.setattr(CSV, "put", put_after_the_disk_is_back)

    result = run_workflow(path, window=DAY1, sleep=lambda seconds: None, rng=Longest())

    assert (result.tasks["export"].status, result.tasks["export"].attempts) == ("published", 2)
    events = _of_task(tmp_path, "export")
    assert sorted({event.attempt for event in events}) == [1, 2]
    _the_run_id(events)
    assert (tmp_path / "out" / "orders.csv").is_file()


def test_the_run_id_and_attempt_of_a_task_reach_the_context_of_its_kind(tmp_path):
    seen = []

    def run(task, ctx):
        seen.append((ctx.attempt, ctx.task_run_id))
        if ctx.attempt < 3:
            raise OSError("a blip")
        return TaskOutcome("succeeded", {"attempt": ctx.attempt})

    _kind("blippy", run)
    try:
        path = _project(tmp_path, retries=THREE, tasks=[{"id": "b", "blippy": 1}])
        waits: list[float] = []
        result = run_workflow(path, window=DAY1, sleep=waits.append, rng=Longest())
    finally:
        unregister_task_kind("blippy")

    task = result.tasks["b"]
    assert (task.status, task.attempts, dict(task.detail)) == ("succeeded", 3, {"attempt": 3}) and waits == [30.0, 60.0]
    assert [attempt for attempt, _ in seen] == [1, 2, 3] and len({run_id for _, run_id in seen}) == 1
    events = _of_task(tmp_path, "b")
    assert _by_attempt(events) == {1: ["run.started", "run.failed", "run.finished"],
                                   2: ["run.started", "run.failed", "run.finished"],
                                   3: ["run.started", "run.finished"]}
    assert _the_run_id(events) == seen[0][1], "the events of every attempt are of the run the task was given"


def test_an_attempt_that_fails_before_its_kind_emitted_anything_still_gets_its_events(tmp_path):
    from local_data_platform.events import RunEmitter

    def run(task, ctx):
        if ctx.attempt == 1:  # a kind that emits its own events, as a pipeline does
            emitter = RunEmitter(ctx.task_sink(task), ctx.task_run_id, attempt=1, context={"pipeline": task.id})
            emitter.emit("run.started", {})
            emitter.emit("run.failed", {"stage": "extract", "error_type": "OSError", "message": "first"})
            emitter.emit("run.finished", {"status": "failed"})
        raise OSError(f"attempt {ctx.attempt}")

    register_task_kind("emitter", validate=lambda value: [], json_schema=dict, plan=lambda task, ctx: TaskPlan(),
                       run=run, emits_events=True)
    try:
        path = _project(tmp_path, retries={"max_attempts": 2, "delay_s": 0}, tasks=[{"id": "e", "emitter": 1}])
        result = run_workflow(path, window=DAY1, sleep=lambda seconds: None)
    finally:
        unregister_task_kind("emitter")

    assert (result.tasks["e"].status, result.tasks["e"].attempts) == ("failed", 2)
    events = _of_task(tmp_path, "e")
    assert _by_attempt(events) == {1: ["run.started", "run.failed", "run.finished"],
                                   2: ["run.started", "run.failed", "run.finished"]}, "the runner covers attempt 2"
    _the_run_id(events)


@pytest.mark.parametrize("error", [
    TaskBlocked("a data gate"), TaskFailed("a failure nobody can fix"), ValueError("no"),
], ids=lambda error: type(error).__name__)
def test_a_kind_that_raises_an_error_that_cannot_be_fixed_is_run_once(tmp_path, error):
    calls = []

    def run(task, ctx):
        calls.append(ctx.attempt)
        raise error

    _kind("stuck", run)
    try:
        path = _project(tmp_path, retries=THREE, tasks=[{"id": "s", "stuck": 1}])
        result = run_workflow(path, window=DAY1, sleep=lambda seconds: None)
    finally:
        unregister_task_kind("stuck")

    assert calls == [1] and result.tasks["s"].attempts == 1


def test_a_maintain_task_is_retried_too(tmp_path, monkeypatch):
    from local_data_platform.maintenance import cli as maintenance

    tasks = [{"id": "orders", "pipeline": "config/orders.json"},
             {"id": "tidy", "depends_on": ["orders"], "maintain": {"config": "config/orders.json"}}]
    path = _project(tmp_path, retries=THREE, tasks=tasks)
    real, calls = maintenance.run_maintenance, []

    def run_maintenance(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("the catalog could not be reached")
        return real(*args, **kwargs)

    monkeypatch.setattr(maintenance, "run_maintenance", run_maintenance)

    result = run_workflow(path, window=DAY1, sleep=lambda seconds: None, rng=Longest())

    assert (result.tasks["tidy"].status, result.tasks["tidy"].attempts) == ("succeeded", 2)
    assert result.tasks["orders"].attempts == 1


def test_a_wait_that_is_interrupted_leaves_the_events_of_the_run_behind(tmp_path):
    path = _project(tmp_path, retries=THREE, landed=False)

    def interrupt(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_workflow(path, window=DAY1, sleep=interrupt)

    assert [e.type for e in _of_task(tmp_path) if e.type == "run.failed"] == ["run.failed"]
    assert [e.type for e in _events(tmp_path) if e.type.startswith("workflow.")] == [
        "workflow.started", "workflow.finished"]


def test_by_default_a_run_really_waits_with_a_random_wait(tmp_path, monkeypatch):
    import time

    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    path = _project(tmp_path, retries={"max_attempts": 3, "delay_s": 30, "backoff": 2.0}, landed=False)

    run_workflow(path, window=DAY1)

    assert len(waits) == 2 and 0.0 <= waits[0] <= 30.0 and 0.0 <= waits[1] <= 60.0


def test_the_workflow_started_event_names_the_attempts_of_the_tasks_that_are_retried(tmp_path):
    tasks = [{"id": "orders", "pipeline": "config/orders.json", "retries": {"max_attempts": 2}},
             {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]}]
    path = _project(tmp_path, tasks=tasks)

    run_workflow(path, window=DAY1)

    started = next(event for event in _events(tmp_path) if event.type == "workflow.started")
    assert started.payload["tasks"] == [
        {"id": "orders", "kind": "pipeline", "depends_on": [], "max_attempts": 2},
        {"id": "export", "kind": "pipeline", "depends_on": ["orders"]}]


# ====================================================================== the command line


def test_ldp_workflow_run_shows_how_often_a_task_ran_only_when_one_ran_again(tmp_path, monkeypatch, capsys):
    path = _project(tmp_path, retries=NO_WAIT)
    Flaky(monkeypatch, OSError("a blip"))

    assert main(["workflow", "run", str(path), "--window", DAY1]) == 0

    table = [line.split() for line in capsys.readouterr().out.splitlines()]
    header = next(line for line in table if line[:2] == ["id", "kind"])
    assert header[:4] == ["id", "kind", "status", "attempts"]
    assert next(line for line in table if line[:1] == ["orders"])[:4] == ["orders", "pipeline", "published", "2"]
    plain = _project(tmp_path / "plain")
    assert main(["workflow", "run", str(plain), "--window", DAY1]) == 0
    assert "attempts" not in capsys.readouterr().out


def test_ldp_workflow_run_json_has_the_attempts_of_each_task_and_exit_code_one_when_they_ran_out(tmp_path, capsys):
    path = _project(tmp_path, retries={"max_attempts": 2, "delay_s": 0}, landed=False)

    assert main(["workflow", "run", str(path), "--window", DAY1, "--json"]) == 1

    printed = json.loads(capsys.readouterr().out)
    assert [(task["id"], task["status"], task["attempts"]) for task in printed["tasks"]] == [("orders", "failed", 2)]


def test_a_plan_does_not_run_anything_and_shows_the_retries(tmp_path, capsys):
    path = _project(tmp_path, retries=THREE)

    assert main(["workflow", "plan", str(path), "--window", DAY1, "--json"]) == 0

    plan = json.loads(capsys.readouterr().out)
    assert plan["tasks"][0]["retries"] == {"max_attempts": 3, "delay_s": 30.0, "backoff": 2.0}
    assert plan["warnings"] == [] and not (tmp_path / ".ldp").exists()


def test_the_plan_table_has_a_column_for_the_attempts_only_when_a_task_is_retried(tmp_path, capsys):
    tasks = [{"id": "orders", "pipeline": "config/orders.json", "retries": {"max_attempts": 4}},
             {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]}]

    assert main(["workflow", "plan", str(_project(tmp_path, tasks=tasks)), "--window", DAY1]) == 0

    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    header = next(row for row in rows if row[:2] == ["order", "id"])
    assert header[-1] == "attempts"
    assert [row[-1] for row in rows if row[:1] in (["1"], ["2"]) and len(row) > 3] == ["4", "1"]
    assert main(["workflow", "plan", str(_project(tmp_path / "plain")), "--window", DAY1]) == 0
    assert "attempts" not in capsys.readouterr().out


# ====================================================================== a config that runs as a workflow


def test_a_dataset_config_with_a_schedule_carries_its_retries_into_the_workflow_it_is(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "landing" / "orders.csv", ORDERS)
    config = _orders_config(tmp_path, schedule="@daily", retries={"max_attempts": 2, "delay_s": 0})
    Flaky(monkeypatch, OSError("a blip"))

    assert main(["workflow", "run", str(config), "--window", DAY1, "--json"]) == 0

    [task] = json.loads(capsys.readouterr().out)["tasks"]
    assert (task["status"], task["attempts"]) == ("published", 2)
    assert load_workflow(config).retries_of(load_workflow(config).tasks[0]).max_attempts == 2


def test_retries_do_not_change_the_spec_hash_of_a_config(tmp_path):
    plain = json.loads(_orders_config(tmp_path).read_text())
    with_retries = json.loads(_orders_config(tmp_path / "other", retries=THREE).read_text())

    assert "retries" in with_retries["metadata"] and "retries" not in plain["metadata"]
    assert spec_hash(plain) == spec_hash(with_retries), "how often a run is tried again does not change what it does"


def test_the_json_schema_of_a_config_has_the_retries_and_a_bad_value_is_reported_with_its_path(tmp_path):
    properties = json_schema()["$defs"]["metadata"]["properties"]
    assert properties["retries"] == {"$ref": "#/$defs/retries"}
    shape = json_schema()["$defs"]["retries"]["properties"]
    assert shape["max_attempts"]["default"] == 1 and shape["delay_s"]["default"] == 30.0
    assert kind_schema("Workflow")["$defs"]["retries"]["properties"] == shape, "one definition for both kinds"

    bad = json.loads(_orders_config(tmp_path).read_text())
    bad["metadata"]["retries"] = {"max_attempts": 0, "tries": 3}
    messages = [str(problem) for problem in validate_spec(bad)]
    assert any(m.startswith("metadata.retries: has unknown keys ['tries']") for m in messages), messages
    assert any(m.startswith("metadata.retries.max_attempts: must be a whole number from 1, got 0") for m in messages)
    from local_data_platform import Config

    with pytest.raises(ConfigError, match=r"metadata\.retries\.max_attempts: must be a whole number from 1"):
        Config.from_dict({**bad, "metadata": {**bad["metadata"], "retries": {"max_attempts": 0}}})


# ====================================================================== the run id and attempt of a direct run


def _export_pipeline(root: Path):
    """The pipeline of ``config/export.json``, which reads the orders table and writes a CSV file, after a load."""
    from local_data_platform import Config
    from local_data_platform.pipeline.registry import create_pipeline

    run_workflow(_project(root), window=DAY1)
    return create_pipeline(Config.from_json(root / "config" / "export.json"))


def test_a_direct_run_takes_the_run_id_and_the_attempt_it_is_given(tmp_path):
    pipeline, sink, other = _export_pipeline(tmp_path), MemorySink(), MemorySink()

    result = pipeline.run(sink=sink, run_id="the-run", attempt=4)

    assert result.run_id == "the-run" and {(e.run_id, e.attempt) for e in sink.events} == {("the-run", 4)}
    pipeline.run(sink=other)
    assert {event.attempt for event in other.events} == {1} and {event.run_id for event in other.events} != {"the-run"}


@pytest.mark.parametrize("arguments", [{"attempt": 0}, {"attempt": -1}, {"attempt": True}, {"attempt": "2"}])
def test_a_direct_run_refuses_an_attempt_that_is_not_a_whole_number_from_one(tmp_path, arguments):
    pipeline = _export_pipeline(tmp_path)

    with pytest.raises(ConfigError, match="attempt must be a whole number from 1"):
        pipeline.run(run_id="r", **arguments)
    assert not (tmp_path / "out").exists(), "nothing ran"


def test_a_run_with_a_commit_context_brings_its_own_run_id_and_does_not_take_another(tmp_path):
    from local_data_platform import Config
    from local_data_platform.format.iceberg.commit import CommitContext
    from local_data_platform.pipeline.registry import create_pipeline

    _write(tmp_path / "landing" / "orders.csv", ORDERS)
    pipeline = create_pipeline(Config.from_json(_orders_config(tmp_path)))
    context = CommitContext.create("a-key", run_id="r1", attempt=2)

    with pytest.raises(ConfigError, match="pass either commit= or run_id=/attempt="):
        pipeline.run(commit=context, run_id="r2")
    with pytest.raises(ConfigError, match="pass either commit= or run_id=/attempt="):
        pipeline.run(commit=context, attempt=3)
