"""The ledger of a workflow: what its events say about each window, and how to carry on a run that did not succeed.

Every run of a workflow writes ``workflow.started``, the events of its tasks and ``workflow.finished`` to
``.ldp/events.jsonl`` next to the workflow file. :func:`read_windows` reads that file for the last run of each window,
which ``ldp backfill`` and ``ldp schedule`` use to tell a window that is done from one that is not. A window whose last
run failed, was blocked by a data gate or never finished is carried on by the next run as the next attempt of the same
run. :func:`trace_runs` reads the ids that takes: the run id of the workflow and of each task, and the attempt each one
reached, as a :class:`Continuation`, which :func:`~local_data_platform.orchestration.runner.run_workflow` accepts.

The scans read the file line by line and parse only the lines they need, so a ledger of many runs stays cheap to read.
A line that is not an event, such as one cut short by a crash, is ignored, and so is a ledger that does not exist.
"""

import json
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.orchestration.tasks import TASK_OK

Window = tuple[str, str]
"""A window as ``workflow.started`` wrote it: its start and its end, in UTC, such as ``2026-08-01T00:00:00Z``."""


@dataclass(frozen=True)
class WindowRecord:
    """What the ledger holds about one window.

    Attributes:
        run_id: The run id of the workflow run that started last for the window.
        attempt: The highest attempt of that run. A run that is carried on by a later tick has an attempt for each tick.
        tasks: ``{task id: status}`` from the last run of the window that finished, or ``None`` when no run of the
            window ever finished.
    """

    run_id: str
    attempt: int
    tasks: Mapping[str, str] | None


@dataclass(frozen=True)
class Continuation:
    """The ids that carry on a run that did not succeed, so that the next attempt fences the ones before it.

    Attributes:
        run_id: The run id of the workflow run.
        attempt: The highest attempt of the workflow run so far.
        tasks: ``{task id: (task run id, highest attempt)}`` for the tasks that ran, or that were held back, in it.
    """

    run_id: str
    attempt: int
    tasks: Mapping[str, tuple[str, int]] = field(default_factory=dict)

    def task(self, task_id: str) -> tuple[str | None, int]:
        """The run id and the highest attempt of a task, or ``(None, 0)`` for a task that did not take part."""
        return self.tasks.get(task_id, (None, 0))


def _events(path: Path, needles: tuple[str, ...]) -> Iterator[tuple[str, str, int, Mapping[str, Any]]]:
    """The parsed events of the lines that hold one of the ``needles``, as ``(run_id, type, attempt, payload)``."""
    try:
        handle = open(path, encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            if not any(needle in line for needle in needles):
                continue  # a cheap test that spares most lines the JSON parser
            try:
                event = json.loads(line)
                payload, run_id, kind = event["payload"], event["run_id"], event["type"]
            except (ValueError, KeyError, TypeError):
                continue
            if not isinstance(payload, Mapping) or not isinstance(run_id, str) or not isinstance(kind, str):
                continue
            attempt = event.get("attempt")
            yield run_id, kind, attempt if isinstance(attempt, int) and attempt >= 1 else 1, payload


def read_windows(path: str | Path, workflow: str) -> dict[Window, WindowRecord]:
    """What the ledger says about each window of a workflow: its last run, and how the last finished run ended.

    The window is as ``workflow.started`` wrote it, and the statuses are the ones ``workflow.finished`` wrote for the
    tasks that ran. A line that is not an event, such as one cut short by a crash, and the runs of other workflows
    that share the ledger, are ignored, and so is a ledger that does not exist.

    Args:
        path: The ledger, ``.ldp/events.jsonl`` next to the workflow file.
        workflow: The identifier of the workflow.

    Returns:
        ``{(start, end): WindowRecord}`` for every window that a run of the workflow started.
    """
    last_run: dict[Window, str] = {}
    attempts: dict[str, int] = {}
    finished: dict[Window, Mapping[str, str]] = {}
    opened: dict[str, Window] = {}
    for run_id, kind, attempt, payload in _events(Path(path), ('"workflow.',)):
        if payload.get("workflow") != workflow:
            continue
        span = payload.get("window")
        if kind == "workflow.started" and isinstance(span, list) and len(span) == 2:
            window = (str(span[0]), str(span[1]))
            opened[run_id], last_run[window] = window, run_id
            attempts[run_id] = max(attempts.get(run_id, 0), attempt)
        elif kind == "workflow.finished" and run_id in opened and isinstance(payload.get("tasks"), Mapping):
            finished[opened[run_id]] = payload["tasks"]
    return {window: WindowRecord(run_id, attempts[run_id], finished.get(window)) for window, run_id in last_run.items()}


def run_status(tasks: Mapping[str, str] | None, task_ids: Sequence[str]) -> str | None:
    """What the last run of a window says: ``failed``, ``blocked``, ``succeeded``, ``incomplete``, or ``None``.

    ``succeeded`` means that every task of the workflow ended well. ``incomplete`` is a run that stopped before every
    task ran, such as an interrupted one, or one from before a task was added to the workflow. ``None`` is a window
    that no run finished.

    Args:
        tasks: ``{task id: status}`` of the last run that finished, or ``None``.
        task_ids: The ids of the tasks of the workflow now.
    """
    if tasks is None:
        return None
    statuses = list(tasks.values())
    if "failed" in statuses:
        return "failed"
    if "blocked_quality" in statuses:
        return "blocked"
    return "succeeded" if all(tasks.get(task_id) in TASK_OK for task_id in task_ids) else "incomplete"


def trace_runs(path: str | Path, run_ids: Collection[str]) -> dict[str, Continuation]:
    """The ids and attempts of workflow runs, to carry them on: one scan of the ledger for all of them.

    A task keeps one run id for all its attempts, and a task that a run held back has the run id and the attempt 1 of
    the event that said so. A run that the ledger does not hold is not in the result.

    Args:
        path: The ledger.
        run_ids: The run ids of the workflow runs.

    Returns:
        ``{workflow run id: Continuation}``.
    """
    wanted = set(run_ids)
    if not wanted:
        return {}
    attempts: dict[str, int] = {}
    tasks: dict[str, dict[str, tuple[str, int]]] = {}
    for run_id, kind, attempt, payload in _events(Path(path), tuple(wanted)):
        if run_id in wanted and kind.startswith("workflow."):
            attempts[run_id] = max(attempts.get(run_id, 0), attempt)
        owner, task_id = payload.get("workflow_run_id"), payload.get("task_id")
        if owner in wanted and isinstance(task_id, str):
            known = tasks.setdefault(owner, {})
            if attempt >= known.get(task_id, (run_id, 0))[1]:
                known[task_id] = (run_id, attempt)
    return {run_id: Continuation(run_id, attempts[run_id], dict(tasks.get(run_id, {})))
            for run_id in wanted if run_id in attempts}


__all__ = ["Continuation", "Window", "WindowRecord", "read_windows", "run_status", "trace_runs"]
