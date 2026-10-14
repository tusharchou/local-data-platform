"""Running a workflow: the tasks one at a time, in dependency order, for one window.

::

    from local_data_platform.orchestration import run_workflow

    result = run_workflow("workflows/daily_orders.json", window="2026-08-01/2026-08-02")
    for task in result.tasks.values():
        print(task.id, task.status)
    raise SystemExit(result.exit_code)

A run checks the whole workflow first, with :func:`~local_data_platform.orchestration.workflow.plan_workflow`, and
refuses to start when anything in it is invalid. Then it runs the tasks with ``graphlib.TopologicalSorter``, one at a
time, which also respects DuckDB's single writer. Every pipeline task runs with the window of the workflow run, so its
idempotency key is the one ``ldp plan CONFIG --window`` prints. Running the same window again after fixing a cause
skips every pipeline task that already published, by its key, without reading its source, and a dbt task that already
built the window, and runs the tasks that failed or did not run.

A task ends with one of these statuses:

* ``published`` or ``skipped_duplicate``: a pipeline task wrote its rows, or found them already written. A ``dbt`` task
  is ``skipped_duplicate`` when it already built the window.
* ``succeeded``: a task of another kind did its work.
* ``blocked_quality``: a data gate held a task back, such as a quality check of a pipeline or the data tests of a dbt
  task. The data has to change, so a scheduler must not retry.
* ``failed``: the task raised any other error, or an error that another attempt may fix and every attempt it has raised
  one. See below.
* ``upstream_failed``: a task it depends on did not succeed, so it did not run. Tasks that do not depend on a task that
  did not succeed still run.

Retries. A workflow and each of its tasks may set ``retries``, see :mod:`local_data_platform.orchestration.retries`. A
task that fails with an error that another attempt may fix, such as a dropped connection, runs again after a wait, until
it succeeds or has used its ``max_attempts``. A retry is the next attempt of the same run: the task keeps its run id and
the attempt counts up, so the events of each attempt carry the attempt number, and a pipeline task keeps its idempotency
key, so the new attempt fences the old one and a load that already landed is not written twice. A quality check that
blocked a write, a config that is wrong and a fenced attempt are never retried. By default a task runs once.

The workflow is ``failed`` when a task failed, ``blocked_quality`` when the only problem is a data gate, and
``succeeded`` otherwise, and :attr:`WorkflowResult.exit_code` is 1, 3 or 0 to match the exit-code table of ``ldp``.

Events. A run writes ``workflow.started``, the events of every task, and ``workflow.finished`` to
``.ldp/events.jsonl`` next to the workflow file, and to the sinks of the workflow's ``metadata.observability``. A
pipeline task's events also go to the sinks of its own config. Every event of a task carries ``workflow``,
``workflow_run_id`` and ``task_id`` in its payload, and ``ldp runs .ldp/events.jsonl`` lists them.
"""

import datetime as dt
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from types import MappingProxyType
from typing import Any

from local_data_platform.exceptions import ConfigError, DataQualityError
from local_data_platform.logger import get_logger
from local_data_platform.orchestration import tasks as task_registry
from local_data_platform.orchestration.retries import is_retriable
from local_data_platform.orchestration.tasks import TASK_OK, RunContext, Task, TaskOutcome
from local_data_platform.orchestration.workflow import Workflow, load_workflow, plan_workflow
from local_data_platform.spec import format_instant, window_bounds

logger = get_logger(__name__)

# Re-exported: the registry of task kinds is part of running a workflow.
register_task_kind = task_registry.register_task_kind
task_kinds = task_registry.task_kinds

NOT_OK = ("failed", "blocked_quality", "upstream_failed")
"""The statuses of a task that stop the tasks that depend on it."""


@dataclass(frozen=True)
class TaskResult:
    """How one task of a workflow run ended.

    Attributes:
        id: The task's id.
        kind: The task's kind.
        status: ``published``, ``skipped_duplicate``, ``succeeded``, ``blocked_quality``, ``failed`` or
            ``upstream_failed``.
        detail: Small facts about the run, such as the row counts, the snapshot id and the idempotency key of a
            pipeline task, or the tasks that blocked an ``upstream_failed`` one. Never rows.
        error: What went wrong, for ``failed`` and ``blocked_quality``. For a task that was retried, the error of the
            last attempt.
        duration_s: How long the task took, the waits between its attempts included.
        attempts: How many times the task ran: 1 for a task that was not retried, and 0 for one that did not run.
    """

    id: str
    kind: str
    status: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_s: float = 0.0
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"id": self.id, "kind": self.kind, "status": self.status, "attempts": self.attempts,
                "detail": dict(self.detail), "error": self.error, "duration_s": round(self.duration_s, 3)}


@dataclass
class WorkflowResult:
    """How a workflow run ended.

    Attributes:
        workflow: The workflow's identifier.
        run_id: The id of the workflow run.
        window: The window of the run, as UTC ``(start, end)``.
        started_at: When the run started, in UTC.
        duration_s: How long the run took.
        tasks: The result of every task, in the order the tasks ran.
        ledger: The file that every event of the run was written to.
    """

    workflow: str
    run_id: str
    window: tuple[dt.datetime, dt.datetime]
    started_at: dt.datetime
    duration_s: float = 0.0
    tasks: dict[str, TaskResult] = field(default_factory=dict)
    ledger: Path | None = None

    @property
    def status(self) -> str:
        """``failed`` if any task failed, else ``blocked_quality`` if a data gate held one back, else ``succeeded``."""
        statuses = {task.status for task in self.tasks.values()}
        if "failed" in statuses:
            return "failed"
        return "blocked_quality" if "blocked_quality" in statuses else "succeeded"

    @property
    def exit_code(self) -> int:
        """The exit status of ``ldp workflow run``: 0 if it succeeded, 3 if a data gate is the only problem, else 1."""
        return {"succeeded": 0, "blocked_quality": 3}.get(self.status, 1)

    def counts(self) -> dict[str, int]:
        """How many tasks ended with each status."""
        counted: dict[str, int] = {}
        for task in self.tasks.values():
            counted[task.status] = counted.get(task.status, 0) + 1
        return counted

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"workflow": self.workflow, "run_id": self.run_id,
                "window": f"{format_instant(self.window[0])}/{format_instant(self.window[1])}",
                "status": self.status, "exit_code": self.exit_code, "duration_s": round(self.duration_s, 3),
                "ledger": str(self.ledger) if self.ledger else None, "counts": self.counts(),
                "tasks": [task.to_dict() for task in self.tasks.values()]}


class _Counting:
    """A sink wrapper that counts the events that pass, so the runner can tell whether a task emitted its own."""

    def __init__(self, sink: Any, counts: dict[str, int], key: str):
        self.sink, self.counts, self.key = sink, counts, key

    def emit(self, event: Any) -> None:
        self.counts[self.key] = self.counts.get(self.key, 0) + 1
        self.sink.emit(event)

    def flush(self) -> None:
        self.sink.flush()


def _message(error: BaseException) -> str:
    from local_data_platform.events import redact_text

    return redact_text(f"{type(error).__name__}: {error}")


def _detail_of(error: BaseException) -> dict[str, Any]:
    """The facts a task kind attached to the error it raised, which go into the events and the result of the task."""
    detail = getattr(error, "detail", None)
    return dict(detail) if isinstance(detail, Mapping) else {}


def _task_emitter(ctx: RunContext, task: Task, run_id: str | None = None, attempt: int = 1) -> Any:
    """An emitter for the run events of an attempt of a task whose kind leaves them to the runner.

    The attempts of a task share its ``run_id``, so a retry shows up in the events as the next attempt of the same run.
    """
    from local_data_platform.events import MultiSink, RunEmitter, TaggedSink, uuid7

    emitter = RunEmitter(TaggedSink(MultiSink([ctx.sink]), ctx.tags(task)), run_id or uuid7(), attempt=attempt,
                         context={"pipeline": task.id, "task_kind": task.kind})
    emitter.emit("run.started", {})
    return emitter


def _finish_task_run(emitter: Any, status: str, *, error: BaseException | None = None, duration_s: float = 0.0,
                     extra: Mapping[str, Any] | None = None) -> None:
    """Emit ``run.failed`` for an error and ``run.finished``, and flush."""
    from local_data_platform.events import redact_text

    if error is not None:
        emitter.emit("run.failed", {"stage": emitter.context.get("task_kind"), "error_type": type(error).__name__,
                                    "message": redact_text(str(error))})
    emitter.emit("run.finished", {"status": status, "duration_s": round(duration_s, 3), **dict(extra or {})})
    emitter.flush()


@dataclass(frozen=True)
class _Pause:
    """What a run waits with between the attempts of a retried task: the function that waits, and the jitter."""

    sleep: Callable[[float], None]
    rng: Any


def _run_task(task: Task, ctx: RunContext, results: Mapping[str, TaskResult], counts: dict[str, int],
              pause: _Pause) -> TaskResult:
    """Run one task, and again after a failure that another attempt may fix, or mark it ``upstream_failed``."""
    blockers = [name for name in task.depends_on if results[name].status not in TASK_OK]
    if blockers:
        logger.info("Workflow %s: task %s did not run, because %s did not succeed", ctx.workflow.identifier, task.id,
                    ", ".join(blockers))
        _finish_task_run(_task_emitter(ctx, task), "upstream_failed", extra={"blocked_by": blockers})
        return TaskResult(task.id, task.kind, "upstream_failed", {"blocked_by": blockers}, attempts=0)
    from local_data_platform.events import uuid7

    kind = task_registry.get_task_kind(task.kind)
    policy = ctx.workflow.retries_of(task)
    run_id = uuid7()  # one run id for every attempt of the task, so a retry fences the attempt before it
    seen = _Counting(ctx.sink, counts, task.id)
    started = time.perf_counter()
    attempt = 0
    while True:
        attempt += 1
        counts[task.id] = 0
        scoped = RunContext(workflow=ctx.workflow, bounds=ctx.bounds, run_id=ctx.run_id, sink=seen, policy=ctx.policy,
                            search_since_ms=ctx.search_since_ms, finished=MappingProxyType(results), attempt=attempt,
                            task_run_id=run_id)
        emitter = None if kind.emits_events else _task_emitter(ctx, task, run_id, attempt)
        began = time.perf_counter()
        error: BaseException | None = None
        try:
            outcome = kind.run(task, scoped)
            if not isinstance(outcome, TaskOutcome):
                raise TypeError(f"the {task.kind} task kind returned {type(outcome).__name__}, not a TaskOutcome")
            status, detail = outcome.status, dict(outcome.detail)
        except DataQualityError as caught:
            error, status, detail = caught, "blocked_quality", _detail_of(caught)
        except Exception as caught:  # noqa: BLE001 - any error of a task fails the task, and the workflow goes on
            error, status, detail = caught, "failed", _detail_of(caught)
        took = time.perf_counter() - began
        if emitter is None and error is not None and counts.get(task.id, 0) == 0:
            emitter = _task_emitter(ctx, task, run_id, attempt)  # a pipeline that failed before it left any event
        if emitter is not None:
            _finish_task_run(emitter, status, error=error if status == "failed" else None, duration_s=took,
                             extra=detail)
        logger.info("Workflow %s: task %s %s in %.2fs", ctx.workflow.identifier, task.id, status, took)
        if not (status == "failed" and attempt < policy.max_attempts and is_retriable(error)):
            break
        wait = policy.delay(attempt, pause.rng)
        logger.warning("Workflow %s: task %s failed on attempt %d of %d (%s), and runs again in %.1f s",
                       ctx.workflow.identifier, task.id, attempt, policy.max_attempts, _message(error), wait)
        pause.sleep(wait)
    return TaskResult(task.id, task.kind, status, detail, _message(error) if error is not None else None,
                      time.perf_counter() - started, attempt)


def run_workflow(source: "str | Path | Workflow", *, window: Any, policy: Any = None,
                 search_since_ms: int | None = None, sleep: Callable[[float], None] | None = None,
                 rng: Any = None) -> WorkflowResult:
    """Run the tasks of a workflow, one at a time, in dependency order, for one window.

    The workflow and every config it names are checked first, and a workflow that is not valid does not start. A
    task that fails, or whose data a quality check blocks, makes the tasks that depend on it ``upstream_failed``. The
    tasks that do not depend on it still run. A task that fails with an error that another attempt may fix runs again as
    the ``retries`` of the workflow and of the task say, with the same run id and the next attempt number. Running the
    same window again skips the pipeline tasks that already published.

    Args:
        source: A workflow file, a dataset config with a schedule, or a :class:`Workflow`.
        window: The window to run: ``START/END``, a ``(start, end)`` pair, or ``yesterday``, ``last-week`` or
            ``last-month``, counted in the time zone of the workflow.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` of the writes. By default the
            one :func:`~local_data_platform.etl.batch_search` gives a batch of as many writes as the workflow has
            pipeline tasks, and ``search_since_ms`` is the start of the run less the clock skew, as it is for a batch.
        search_since_ms: The start of the key search of the publish loop, in epoch milliseconds. A caller that runs
            many windows, such as a backfill, passes the start of its batch.
        sleep: What waits between the attempts of a task that is retried, called with the seconds to wait. It is
            ``time.sleep`` by default, and a test passes a function that only records the wait.
        rng: What draws the jitter of those waits, anything with ``uniform(a, b)``. A new :class:`random.Random` by
            default, and a seeded one makes the waits repeatable.

    Returns:
        The :class:`WorkflowResult`. It does not raise for a failed task: read ``result.status`` or
        ``result.exit_code``.

    Raises:
        ConfigError: If the workflow or the window is not valid, or a config it names is not.
    """
    from local_data_platform.etl import batch_search
    from local_data_platform.events import JsonlSink, MultiSink, NullSink, RunEmitter, build_sink, sink_specs, uuid7

    workflow = source if isinstance(source, Workflow) else load_workflow(source)
    if window is None:
        raise ConfigError(f"workflow {workflow.identifier} runs for a window: pass one, such as START/END or "
                          "yesterday")
    bounds = window_bounds(workflow.spec, window)
    plan = plan_workflow(workflow, window=bounds)
    if not plan["valid"]:
        listed = "\n".join(f"  - {message}" for message in plan["errors"])
        raise ConfigError(f"workflow {workflow.identifier} is not valid, so no task ran:\n{listed}")
    pause = _Pause(sleep if sleep is not None else time.sleep, rng if rng is not None else random.Random())

    started_at = dt.datetime.now(dt.timezone.utc)
    pipelines = sum(1 for task in workflow.tasks if task.kind == "pipeline")
    batch_since, batch_policy = batch_search(pipelines, started=started_at)
    run_id = uuid7()
    sinks = [JsonlSink(workflow.ledger_path)]
    sinks += [sink for sink in (build_sink(spec, workflow.base_dir) for spec in sink_specs(workflow.metadata))
              if not isinstance(sink, NullSink)]
    sink = MultiSink(sinks)
    context = RunContext(workflow=workflow, bounds=bounds, run_id=run_id, sink=sink,
                         policy=policy if policy is not None else batch_policy,
                         search_since_ms=search_since_ms if search_since_ms is not None else batch_since)
    emitter = RunEmitter(sink, run_id, context={"pipeline": workflow.identifier, "workflow": workflow.identifier,
                                                "workflow_run_id": run_id})
    result = WorkflowResult(workflow=workflow.identifier, run_id=run_id, window=bounds, started_at=started_at,
                            ledger=workflow.ledger_path)
    clock = time.perf_counter()
    emitter.emit("workflow.started", lambda: {
        "window": [format_instant(bounds[0]), format_instant(bounds[1])],
        "schedule": workflow.schedule.expression if workflow.schedule else None,
        "tasks": [_started_task(workflow, task) for task in workflow.order()],
        "ldp_version": _version()})
    counts: dict[str, int] = {}
    try:
        by_id = {task.id: task for task in workflow.tasks}
        sorter = TopologicalSorter(workflow.graph())
        sorter.prepare()
        while sorter.is_active():
            ready = sorted(sorter.get_ready(), key=lambda task_id: by_id[task_id].index)
            for task_id in ready:
                result.tasks[task_id] = _run_task(by_id[task_id], context, result.tasks, counts, pause)
            sorter.done(*ready)
    finally:
        result.duration_s = time.perf_counter() - clock
        emitter.emit("workflow.finished", lambda: {
            "status": result.status, "duration_s": round(result.duration_s, 3), "counts": result.counts(),
            "tasks": {task_id: item.status for task_id, item in result.tasks.items()}})
        emitter.flush()
    return result


def _started_task(workflow: Workflow, task: Task) -> dict[str, Any]:
    """What ``workflow.started`` says about a task: its id, kind and dependencies, and its attempts if it is retried."""
    listed: dict[str, Any] = {"id": task.id, "kind": task.kind, "depends_on": list(task.depends_on)}
    if (retry := workflow.retries_of(task)).enabled:
        listed["max_attempts"] = retry.max_attempts
    return listed


def _version() -> str:
    from local_data_platform import __version__

    return __version__


__all__ = ["NOT_OK", "TaskResult", "WorkflowResult", "register_task_kind", "run_workflow", "task_kinds"]
