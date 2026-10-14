"""Backfill: run a workflow, or a dataset config, for every window of a range, and run it again to resume.

::

    from local_data_platform.orchestration import backfill

    result = backfill("workflows/daily_orders.json", "2026-06-01", "2026-09-01")
    print(result.counts())   # {'published': 90, 'skipped': 2, 'blocked': 0, 'failed': 0}
    raise SystemExit(result.exit_code)

The windows are the windows of one size, laid end to end from ``start`` to ``end``: the ``metadata.window.size`` of the
workflow or config, or the size its schedule means (``@hourly`` is an hour, ``@daily`` a day). ``start`` and ``end``
are dates or datetimes in the time zone of the workflow, each of them has to start a window, and ``end`` is not part of
the range, so ``2026-06-01`` to ``2026-06-04`` holds the days of June 1st, 2nd and 3rd. Each window is run with
:func:`~local_data_platform.orchestration.runner.run_workflow`, so a pipeline task publishes under the key of its
window, the key that ``ldp plan CONFIG --window W`` prints, and a task with ``retries`` is retried.

**Resume.** Running the same range again does the work that is left. Before it runs anything, a backfill finds out, for
every window, whether the keys of the window are on ``main`` already, with one walk of the history of each table, and
what the last run of the window left in the ledger, ``.ldp/events.jsonl`` next to the workflow file. A window that is
done is skipped without being read, planned or written to the ledger, so running a finished backfill again does
nothing, and a backfill that stopped at window 40 starts again at window 40. A window is done when every pipeline task
that writes to Iceberg has published its key, and when the workflow has any other task that a key cannot vouch for,
such as a dbt task or a pipeline that writes a file, the last run of the window in the ledger succeeded for every task.
A maintain task does not count. A window that failed, was blocked by a data gate, or is only partly done runs again,
and the tasks of it that finished are skipped by their keys.

**Replace.** ``replace=True`` writes each window again, under the replace key of its key and the spec hash of its
config, with a window overwrite, so the rows that the window has are replaced by the rows the source has now, and a
window that has none is loaded. A window that was replaced under the same spec is skipped, and one that was replaced
under an older spec is replaced again. It is the way to redo a window whose rows changed at the source, or whose key
expired. Every pipeline that writes to Iceberg needs a ``metadata.window.column``, or the backfill does not start.

A window that fails does not stop the others, unless ``stop_on_failure`` says so. The result counts the windows that
published, were skipped, were blocked by a data gate and failed.
"""

import datetime as dt
import itertools
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.logger import get_logger
from local_data_platform.orchestration import windows
from local_data_platform.orchestration.ledger import Continuation, read_windows, run_status, trace_runs
from local_data_platform.orchestration.runner import WorkflowResult, run_workflow
from local_data_platform.orchestration.tasks import get_task_kind
from local_data_platform.orchestration.workflow import Workflow, load_workflow, plan_workflow
from local_data_platform.spec import format_instant, idempotency_key, replace_key, spec_hash

logger = get_logger(__name__)

DEFAULT_MAX_WINDOWS = 1000
"""How many windows a backfill may have, unless it is told more. A range that holds more is most likely a typo."""

STATES = ("published", "partial", "missing", "failed", "blocked")
"""What a window is before the backfill runs it. Only ``published`` is skipped."""

OUTCOMES = ("published", "skipped", "blocked", "failed")
"""How a window ended. ``not_run`` also counts when ``stop_on_failure`` left windows undone."""

_WHERE_WINDOWS_START = {"day": "midnight", "hour": "the start of an hour", "15m": "the start of a quarter of an hour"}


def _message_lines(message: str) -> list[str]:
    """The first line of an error message and, when it ends in a colon, the indented lines that follow it."""
    lines = message.splitlines() or [""]
    kept = lines[:1]
    if kept[0].rstrip().endswith(":"):
        kept.extend(itertools.takewhile(lambda line: line.startswith(" "), lines[1:]))
    return kept


@dataclass
class BackfillWindow:
    """One window of a backfill.

    Attributes:
        start: Where the window starts, an aware UTC datetime.
        end: Where it ends.
        state: What the window was before the run: ``published`` (done, so it is skipped), ``partial`` (some of its
            keys are on ``main``), ``missing``, ``failed`` or ``blocked`` (the last run of it in the ledger failed, or
            a data gate held it back).
        keys: The idempotency key of each pipeline task that writes to Iceberg, by task id. The replace keys when the
            backfill replaces.
        outcome: How the window ended: ``published`` (something was written or built), ``skipped`` (it was done, or
            every task of it found its work done), ``blocked``, ``failed`` or ``not_run``. ``None`` in a dry run.
        result: The :class:`~local_data_platform.orchestration.runner.WorkflowResult` of the run of the window, or
            ``None`` when the window was not run.
        error: Why the window failed when its workflow could not start, such as a source file that cannot be planned.
        attempt: The attempt of the workflow run that this run is. It is 1 for a run of its own, and more for a run that
            carried on the last run of the window, as ``ldp schedule tick`` does. ``None`` when the window was not run.
    """

    start: dt.datetime
    end: dt.datetime
    state: str
    keys: dict[str, str] = field(default_factory=dict)
    outcome: str | None = None
    result: WorkflowResult | None = None
    error: str | None = None
    attempt: int | None = None

    @property
    def window(self) -> str:
        """The window as ``START/END``, in UTC."""
        return f"{format_instant(self.start)}/{format_instant(self.end)}"

    def problems(self) -> list[str]:
        """The lines that say what went wrong in the window, such as ``orders: FileNotFoundError: CSV not found``.

        An error that is a headline ending in a colon and a list under it, as a failed quality check is, gives the list
        too, so that the line names the check and not only how many failed.
        """
        if self.error is not None:
            return _message_lines(self.error)
        if self.result is None:
            return []
        lines: list[str] = []
        for task in self.result.tasks.values():
            if task.error and task.status in ("failed", "blocked_quality"):
                headline, *items = _message_lines(task.error)
                lines += [f"{task.id}: {headline}", *items]
        return lines

    def to_dict(self) -> dict[str, Any]:
        """The window as JSON-serialisable primitives."""
        data: dict[str, Any] = {"window": self.window, "state": self.state, "keys": dict(self.keys),
                                "outcome": self.outcome, "error": self.error, "attempt": self.attempt}
        if self.result is not None:
            data["workflow_run_id"] = self.result.run_id
            data["tasks"] = [{"id": task.id, "kind": task.kind, "status": task.status, "attempts": task.attempts,
                              "error": task.error} for task in self.result.tasks.values()]
        return data


@dataclass
class BackfillResult:
    """How a backfill went.

    Attributes:
        workflow: The identifier of the workflow.
        ledger: The file that the events of the runs go to, next to the workflow file.
        window_size: ``day``, ``hour`` or ``15m``, or ``None`` for the windows of a schedule that has no size.
        timezone: The time zone the days are counted in.
        start: Where the range starts, an aware UTC datetime.
        end: Where it ends.
        order: ``oldest first`` or ``newest first``.
        replace: Whether the backfill replaced windows.
        dry_run: Whether it only looked, and ran nothing.
        windows: Every window of the range, in the order they are run.
        duration_s: How long the backfill took.
    """

    workflow: str
    ledger: Path
    window_size: str | None
    timezone: str
    start: dt.datetime
    end: dt.datetime
    order: str
    replace: bool
    dry_run: bool
    windows: list[BackfillWindow] = field(default_factory=list)
    duration_s: float = 0.0

    def counts(self) -> dict[str, int]:
        """How many windows ended with each outcome: ``published``, ``skipped``, ``blocked`` and ``failed``.

        ``not_run`` is there too, when ``stop_on_failure`` left windows undone. A dry run has no outcomes.
        """
        counted = {name: 0 for name in OUTCOMES}
        undone = 0
        for entry in self.windows:
            if entry.outcome in counted:
                counted[entry.outcome] += 1
            elif entry.outcome == "not_run":
                undone += 1
        if undone:
            counted["not_run"] = undone
        return counted

    def states(self) -> dict[str, int]:
        """How many windows were in each state before the run: ``published``, ``partial``, ``missing``, ..."""
        counted = {name: 0 for name in STATES}
        for entry in self.windows:
            counted[entry.state] += 1
        return counted

    @property
    def status(self) -> str:
        """``failed`` if a window failed, else ``blocked_quality`` if a data gate held one back, else ``succeeded``.

        A dry run ran nothing, so its status is ``dry_run``.
        """
        if self.dry_run:
            return "dry_run"
        counted = self.counts()
        if counted["failed"]:
            return "failed"
        return "blocked_quality" if counted["blocked"] else "succeeded"

    @property
    def exit_code(self) -> int:
        """The exit status of ``ldp backfill``: 0 if no window failed or was blocked, 3 if a data gate is the only
        problem, else 1. A dry run exits with 0."""
        return {"failed": 1, "blocked_quality": 3}.get(self.status, 0)

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"workflow": self.workflow, "ledger": str(self.ledger), "window_size": self.window_size,
                "timezone": self.timezone, "from": format_instant(self.start), "to": format_instant(self.end),
                "order": self.order, "replace": self.replace, "dry_run": self.dry_run, "status": self.status,
                "exit_code": self.exit_code, "duration_s": round(self.duration_s, 3), "counts": self.counts(),
                "states": self.states(), "windows": [entry.to_dict() for entry in self.windows]}


# ---------------------------------------------------------------------- what is done already


@dataclass(frozen=True)
class _Keyed:
    """A pipeline task that writes to an Iceberg table, with the keys that table already holds."""

    task_id: str
    config: Any
    spec_hash: str
    published: frozenset[str]


def _writes_iceberg(config: Any) -> bool:
    return str(config.target.get("format", "")).strip().upper() == "ICEBERG"


def _published_keys(config: Any) -> frozenset[str]:
    """The idempotency keys on ``main`` of the table that a config writes, without creating anything.

    Building an ``Iceberg`` table would create the namespace, and a backfill that only looks must not write, so the
    catalog is opened as it is. A table that does not exist yet holds no key.
    """
    from local_data_platform.catalog import provider
    from local_data_platform.format.iceberg.commit import published_keys

    spec, name = config.target["catalog"], config.target["name"]
    try:
        provider.require_catalog_database(spec, config.base_dir, table=name)
    except TableNotFound:
        return frozenset()
    catalog = provider.create_catalog(spec, base_dir=config.base_dir)
    identifier = f"{provider.catalog_namespace(spec)}.{name}"
    if not catalog.table_exists(identifier):
        return frozenset()
    return frozenset(published_keys(catalog.load_table(identifier)))


def _read_ledger(workflow: Workflow) -> dict[tuple[str, str], Mapping[str, str]]:
    """What the last run of each window left in the ledger: ``{(start, end): {task id: status}}``.

    The window is as ``workflow.started`` wrote it, and the statuses are the ones ``workflow.finished`` wrote for the
    tasks that ran. A line that is not an event, such as one cut short by a crash, and the runs of other workflows that
    share the ledger, are ignored, and so is a ledger that does not exist.
    """
    records = read_windows(workflow.ledger_path, workflow.identifier)
    return {window: record.tasks for window, record in records.items() if record.tasks is not None}


class _Inspector:
    """Finds out what is done for each window of a backfill, from the tables and the ledger, before anything runs."""

    def __init__(self, workflow: Workflow, replace: bool):
        from local_data_platform.etl import load_config

        self.workflow, self.replace = workflow, replace
        self.keyed: list[_Keyed] = []
        self.needs_ledger = False  # a task that a key cannot vouch for: a file pipeline, a dbt task, a kind of its own
        for task in workflow.tasks:
            if task.kind == "pipeline":
                config = load_config(workflow.resolve(task.value))
                if _writes_iceberg(config):
                    self.keyed.append(_Keyed(task.id, config, spec_hash(config), _published_keys(config)))
                else:
                    self.needs_ledger = True
            elif task.kind != "maintain":
                self.needs_ledger = True
        self.records = read_windows(workflow.ledger_path, workflow.identifier)
        self.ledger = {window: record.tasks for window, record in self.records.items() if record.tasks is not None}
        self.task_ids = [task.id for task in workflow.tasks]

    def keys(self, bounds: tuple[dt.datetime, dt.datetime]) -> dict[str, str]:
        """The key of each keyed task for a window: its idempotency key, or its replace key when replacing."""
        found = {}
        for keyed in self.keyed:
            key = idempotency_key(keyed.config, bounds)
            found[keyed.task_id] = replace_key(key, keyed.spec_hash) if self.replace else key
        return found

    def state(self, bounds: tuple[dt.datetime, dt.datetime], keys: Mapping[str, str]) -> str:
        """The state of a window, see :data:`STATES`."""
        on_main = [keys[keyed.task_id] in keyed.published for keyed in self.keyed]
        ledger = run_status(self.ledger.get((format_instant(bounds[0]), format_instant(bounds[1]))), self.task_ids)
        by_keys = all(on_main)  # true when nothing is keyed, which leaves the ledger to say
        by_ledger = ledger == "succeeded" if (self.needs_ledger or not self.keyed) else True
        # A replace cannot be told from the ledger alone: what it did is on main, under the keys it wrote.
        if by_keys and by_ledger and not (self.replace and not self.keyed):
            return "published"
        if ledger == "failed":
            return "failed"
        if ledger == "blocked":
            return "blocked"
        return "partial" if any(on_main) else "missing"

    def resumable(self, bounds: tuple[dt.datetime, dt.datetime]) -> str | None:
        """The run id of the last run of a window, when that run did not succeed, so that the next run can carry it on.

        A run that failed, was blocked by a data gate or never finished is carried on. A window whose last run succeeded
        is not, and nor is one that no run has started.
        """
        record = self.records.get((format_instant(bounds[0]), format_instant(bounds[1])))
        if record is None or run_status(record.tasks, self.task_ids) == "succeeded":
            return None
        return record.run_id


def _outcome_of(result: WorkflowResult) -> str:
    """How a window ended, from the tasks of its run."""
    statuses = {task.status for task in result.tasks.values()}
    if "failed" in statuses:
        return "failed"
    if "blocked_quality" in statuses:
        return "blocked"
    for task in result.tasks.values():
        if task.status == "published" or (task.status == "succeeded" and get_task_kind(task.kind).changes_data):
            return "published"
    return "skipped"


# ---------------------------------------------------------------------- the range


def _range(workflow: Workflow, start: Any, end: Any,
           now: dt.datetime | None) -> tuple[str, str, dt.datetime, dt.datetime]:
    """The size, the time zone and the two ends of the range, as UTC datetimes, checked."""
    tz = workflow.timezone
    zone = getattr(tz, "key", "UTC")
    size = windows.window_size_of(workflow.metadata, workflow.spec.get("when"))
    if size is None:
        raise ConfigError(f"{workflow.identifier} does not say how long a window is. Set metadata.window.size to "
                          f"{', '.join(windows.WINDOW_SIZES)}. A schedule of @daily or @hourly means a day or an hour")
    first = windows.parse_boundary(start, tz, "the start of the range")
    last = (windows.floor_boundary(now if now is not None else windows.now_utc(), size, tz) if end is None
            else windows.parse_boundary(end, tz, "the end of the range"))
    for what, moment in (("start", first), ("end", last)):
        if not windows.is_aligned(moment, size, tz):
            raise ConfigError(f"the {what} of the range, {format_instant(moment)}, does not start a window of size "
                              f"{size} in {zone}. Windows of that size start at {_WHERE_WINDOWS_START[size]}")
    if first >= last:
        by_default = " (the end is the last complete window, as no end was given)" if end is None else ""
        raise ConfigError(f"the range is empty: its start {format_instant(first)} is not before its end "
                          f"{format_instant(last)}{by_default}")
    return size, zone, first, last


def backfill(source: "str | Path | Workflow", start: Any, end: Any = None, *, replace: bool = False,
             dry_run: bool = False, run_backwards: bool = False, max_windows: int = DEFAULT_MAX_WINDOWS,
             stop_on_failure: bool = False, policy: Any = None, sleep: Callable[[float], None] | None = None,
             rng: Any = None, on_window: Callable[[BackfillWindow, int, int], None] | None = None,
             now: dt.datetime | None = None) -> BackfillResult:
    """Run a workflow, or a dataset config, for every window from ``start`` to ``end``, skipping what is done.

    Args:
        source: A workflow file, a dataset config with a ``metadata.window`` or a schedule, or a
            :class:`~local_data_platform.orchestration.workflow.Workflow`.
        start: Where the range starts: a date or datetime, or ISO-8601 text. A value with no offset is read in the time
            zone of the workflow. It has to start a window.
        end: Where the range ends, read as ``start`` is. The window that starts at ``end`` is not part of the range. The
            end of the last complete window by default, which is the start of the window that ``now`` is in.
        replace: Replace the windows instead of loading them, see the module.
        dry_run: Work out the windows, their keys and their states, and run nothing.
        run_backwards: Run the newest window first.
        max_windows: Refuse a range that holds more windows than this, which is most likely a typo.
        stop_on_failure: Stop at the first window that failed. A window that a data gate blocked does not stop it,
            because the next window may be fine. The windows that were not run have the outcome ``not_run``.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` of the writes. The default policy
            with the depth that the whole backfill needs, see :func:`~local_data_platform.etl.batch_search`, by default.
        sleep: What waits between the attempts of a task that is retried, see
            :func:`~local_data_platform.orchestration.runner.run_workflow`.
        rng: What draws the jitter of those waits.
        on_window: Called as ``on_window(window, number, total)`` when a window is done, or was skipped. A caller that
            shows progress uses it. A window that was not run because of ``stop_on_failure`` is not announced.
        now: The current time, for the default ``end`` and for tests.

    Returns:
        The :class:`BackfillResult`. It does not raise for a window that failed: read ``result.counts()`` or
        ``result.exit_code``.

    Raises:
        ConfigError: If the workflow or a config it names is not valid, the range is empty, does not start a window or
            holds more than ``max_windows`` windows, the workflow does not say how long a window is, or ``replace`` is
            set and a pipeline that writes to Iceberg has no ``metadata.window.column``. Nothing has run then.
    """
    began = time.perf_counter()
    if not isinstance(max_windows, int) or isinstance(max_windows, bool) or max_windows < 1:
        raise ConfigError(f"max_windows must be a whole number from 1, got {max_windows!r}")
    workflow = source if isinstance(source, Workflow) else load_workflow(source, require_schedule=False)
    size, zone, first, last = _range(workflow, start, end, now)
    check_workflow(workflow, replace)
    grid = list(itertools.islice(windows.iter_windows(first, last, size, workflow.timezone), max_windows + 1))
    if len(grid) > max_windows:
        raise ConfigError(f"the range from {format_instant(first)} to {format_instant(last)} holds more than "
                          f"{max_windows} windows of size {size}. Check the start and the end, or pass a larger "
                          "max_windows (--max-windows)")
    if run_backwards:
        grid.reverse()
    return run_windows(workflow, grid, window_size=size, timezone=zone, start=first, end=last,
                       order="newest first" if run_backwards else "oldest first", replace=replace, dry_run=dry_run,
                       stop_on_failure=stop_on_failure, policy=policy, sleep=sleep, rng=rng, on_window=on_window,
                       began=began)


def check_workflow(workflow: Workflow, replace: bool = False) -> None:
    """Check the workflow and every config it names, which no window can change, and a replace's window columns.

    Raises:
        ConfigError: If anything is not valid, with every problem in the message. Nothing has run then.
    """
    checked = plan_workflow(workflow, replace=replace)
    if not checked["valid"]:
        listed = "\n".join(f"  - {message}" for message in checked["errors"])
        raise ConfigError(f"workflow {workflow.identifier} is not valid, so no window ran:\n{listed}")


def run_windows(workflow: Workflow, grid: Sequence[tuple[dt.datetime, dt.datetime]], *, window_size: str | None,
                timezone: str, start: dt.datetime, end: dt.datetime, order: str = "oldest first",
                replace: bool = False, dry_run: bool = False, stop_on_failure: bool = False, policy: Any = None,
                sleep: Callable[[float], None] | None = None, rng: Any = None,
                on_window: Callable[[BackfillWindow, int, int], None] | None = None, continue_runs: bool = False,
                max_pending: int | None = None, began: float | None = None) -> BackfillResult:
    """Run a workflow for each window of a list, in that order, skipping the windows that are done.

    It is what :func:`backfill` does once it has the windows of its range, and what ``ldp schedule tick`` does with the
    windows that are due. The windows need not lie on a grid. A window is done by the rules in the module, and a window
    that is done is skipped without being read, planned or written to the ledger.

    Args:
        workflow: The workflow, which the caller has checked with :func:`check_workflow`.
        grid: The windows, ``(start, end)`` pairs in UTC, in the order they run.
        window_size: The size of the windows for the result, or ``None`` when they have none.
        timezone: The time zone name for the result.
        start: Where the range of the result starts.
        end: Where it ends.
        order: What the result says about the order of the windows.
        replace: Replace the windows instead of loading them.
        dry_run: Work out the states and the keys, and run nothing.
        stop_on_failure: Stop at the first window that failed. The windows that were not run have the outcome
            ``not_run``.
        policy: The commit policy of the writes, see :func:`backfill`.
        sleep: What waits between the attempts of a task that is retried.
        rng: What draws the jitter of those waits.
        on_window: Called as ``on_window(window, number, total)`` when a window is done, or was skipped.
        continue_runs: Carry on the last run of a window that did not succeed, as the next attempt of the same run:
            the run id of the workflow and of each task is the one from the ledger, and the attempt is one more. That
            is how a scheduler retries a failed window, and a backfill starts a run of its own instead.
        max_pending: The most windows that are not done to run. The ones after them are left for a later call, with the
            outcome ``not_run``. A scheduler that catches up uses it to take a few windows at a time.
        began: When the caller started, from ``time.perf_counter()``, for the duration of the result.

    Returns:
        The :class:`BackfillResult`. It does not raise for a window that failed.
    """
    from local_data_platform.etl import batch_search

    began = began if began is not None else time.perf_counter()
    inspector = _Inspector(workflow, replace)
    entries = []
    for bounds in grid:
        keys = inspector.keys(bounds)
        entries.append(BackfillWindow(bounds[0], bounds[1], inspector.state(bounds, keys), keys))
    result = BackfillResult(workflow=workflow.identifier, ledger=workflow.ledger_path, window_size=window_size,
                            timezone=timezone, start=start, end=end, order=order, replace=replace, dry_run=dry_run,
                            windows=entries)
    if dry_run:
        result.duration_s = time.perf_counter() - began
        return result
    pending = [entry for entry in entries if entry.state != "published"]
    if max_pending is not None:
        pending = pending[:max_pending]
    to_run = {(entry.start, entry.end) for entry in pending}
    since_ms, batch_policy = batch_search(len(pending) * max(1, len(inspector.keyed)), policy=policy)
    carried: dict[tuple[dt.datetime, dt.datetime], str] = {}
    if continue_runs:
        carried = {(entry.start, entry.end): run_id for entry in pending
                   if (run_id := inspector.resumable((entry.start, entry.end))) is not None}
    continuations = trace_runs(workflow.ledger_path, set(carried.values()))
    stopped = False
    try:
        for number, entry in enumerate(entries, start=1):
            if entry.state == "published":
                entry.outcome = "skipped"
            elif stopped or (entry.start, entry.end) not in to_run:
                entry.outcome = "not_run"
            else:
                continues = continuations.get(carried.get((entry.start, entry.end), ""))
                _run_window(workflow, entry, batch_policy, since_ms, sleep, rng, replace, continues)
                stopped = stop_on_failure and entry.outcome == "failed"
            if on_window is not None and entry.outcome != "not_run":
                on_window(entry, number, len(entries))
    finally:
        result.duration_s = time.perf_counter() - began
    return result


def _run_window(workflow: Workflow, entry: BackfillWindow, policy: Any, since_ms: int,
                sleep: Callable[[float], None] | None, rng: Any, replace: bool,
                continues: Continuation | None = None) -> None:
    """Run the workflow for one window and record how it ended. An interrupt goes on."""
    try:
        outcome = run_workflow(workflow, window=(entry.start, entry.end), policy=policy, search_since_ms=since_ms,
                               sleep=sleep, rng=rng, replace=replace, continues=continues)
    except ConfigError as error:  # a problem of this window alone, such as a source that cannot be planned
        entry.outcome, entry.error = "failed", str(error)
        logger.warning("Backfill of %s: the window %s could not start: %s", workflow.identifier, entry.window,
                       str(error).splitlines()[0])
        return
    entry.result, entry.outcome = outcome, _outcome_of(outcome)
    entry.attempt = continues.attempt + 1 if continues is not None else 1


__all__ = [
    "DEFAULT_MAX_WINDOWS",
    "OUTCOMES",
    "STATES",
    "BackfillResult",
    "BackfillWindow",
    "backfill",
    "check_workflow",
    "run_windows",
]
