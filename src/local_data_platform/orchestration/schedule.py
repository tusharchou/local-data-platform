"""A scheduler with no services: ``ldp schedule tick`` runs what is due, and ``ldp schedule list`` shows what is coming.

::

    from local_data_platform.orchestration.schedule import tick

    result = tick("ldp_demo/restaurant", now="2026-08-05T02:05:00Z")
    print(result.counts())   # {'published': 1, 'skipped': 0, 'blocked': 0, 'failed': 0}
    raise SystemExit(result.exit_code)

A tick is meant to be called every 5 minutes by cron or launchd, see
:mod:`~local_data_platform.orchestration.recipes`. It finds every workflow file and every dataset config under a
folder that has a schedule, works out which windows are due at the time of the tick, runs the ones that are not done,
and exits. Nothing stays running between ticks, so a laptop that was asleep catches up at the first tick after it
wakes.

**Fires.** A schedule is a cron expression, in the time zone of the workflow (``metadata.window.timezone``, UTC by
default). It fires at the moments the expression names. A fixed time that a clock change skips fires right after the
gap, and one that happens twice fires once, and an interval such as every 30 minutes fires in both passes of the
repeated hour, as cron does.

**The window of a fire.** The window a fire owns is the interval between the previous fire and this fire. When
``metadata.window.size`` is set, or the schedule is ``@daily`` or ``@hourly``, it is instead the last complete window
of that size that ends at or before the fire, in the time zone of the workflow, so the fire of ``5 2 * * *`` on 5
August owns 4 August. Several fires can own the same window, and the window is run once.

**Due windows.** Without ``catchup``, which is the default, a tick runs the window of the latest fire, if it is not
done. The windows before it that were missed are not run, and ``ldp schedule list`` prints the ``ldp backfill``
command that fills them. With ``metadata.catchup`` true a tick runs the window of every fire since ``metadata.start``
that is not done, oldest first. A tick runs at most ``max_windows`` windows that are not done, and the next tick goes
on. A window before
``metadata.start`` is never run. A window is done by the rules of :mod:`~local_data_platform.orchestration.backfills`:
the keys of its pipelines are on the table, and when the workflow has a task that a key cannot vouch for, the last run
in the ledger succeeded.

**Retries.** A window that failed, was blocked by a data gate, or never finished on one tick is not done, so the next
tick runs it again, and it carries on the run the tick before started: the run id of the workflow and of each task is
the one in the ledger, and the attempt is one more. The first commit of the new attempt fences every older attempt,
also one that a crashed process left behind, so a window is loaded once however many ticks it takes. The ``retries``
of a workflow are the retries within one tick.

**One tick at a time.** A tick takes a lock on ``DIR/.ldp/locks/schedule.lock`` that it does not wait for. A tick that
starts while another runs does nothing and exits with 0, and the lock goes with its process, so a crash leaves none.

Each workflow writes its events to ``.ldp/events.jsonl`` next to its own file, as ``ldp workflow run`` does. A tick
reads the file with :func:`~local_data_platform.orchestration.ledger.read_windows`. This module imports pyiceberg only
when it runs a window.
"""

import datetime as dt
import itertools
import json
import os
import shlex
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.filelock import file_lock
from local_data_platform.logger import get_logger
from local_data_platform.orchestration import windows
from local_data_platform.orchestration.backfills import (
    DEFAULT_MAX_WINDOWS,
    OUTCOMES,
    BackfillResult,
    BackfillWindow,
    check_workflow,
    run_windows,
)
from local_data_platform.orchestration.ledger import read_windows, run_status
from local_data_platform.orchestration.workflow import Workflow, read_workflow
from local_data_platform.spec import format_instant, kind_of

logger = get_logger(__name__)

LOCK_PATH = Path(".ldp") / "locks" / "schedule.lock"
"""The lock of a tick, relative to the folder it ticks."""

SKIPPED_FOLDERS = frozenset({"node_modules", "__pycache__", "site-packages", "venv", "env"})
"""Folders that discovery does not enter, besides those whose name starts with a dot."""

MAX_SPEC_BYTES = 1_000_000
"""A JSON file larger than this is not read when discovery looks for schedules."""

_UTC = dt.timezone.utc
_MINUTE = dt.timedelta(minutes=1)
_SEARCH = 1000  # the fires that a search for the next fire looks through, when a start rules some of them out

MAX_ENUMERATED = 100_000
"""The most windows that a catch-up or a listing goes through, which a ``metadata.start`` long ago could exceed."""


# ---------------------------------------------------------------------- fires and their windows


def parse_now(value: "dt.datetime | str | None") -> dt.datetime:
    """The time of a tick as an aware UTC datetime: the current time, or ISO-8601 text or a datetime, UTC when naive.

    Raises:
        ConfigError: If the text is not an ISO-8601 date or datetime.
    """
    if value is None:
        return windows.now_utc()
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ConfigError(f"now {value!r} is not an ISO-8601 date or datetime, "
                              "such as 2026-08-05T02:05:00Z") from None
    if not isinstance(value, dt.datetime):
        raise ConfigError(f"now must be a datetime or ISO-8601 text, got {value!r}")
    return value.astimezone(_UTC) if value.tzinfo is not None else value.replace(tzinfo=_UTC)


def _minute(moment: dt.datetime) -> dt.datetime:
    return moment.astimezone(_UTC).replace(second=0, microsecond=0)


def fire_at_or_before(schedule: windows.Schedule, tz: dt.tzinfo, moment: dt.datetime) -> dt.datetime | None:
    """The latest fire of a schedule at or before ``moment``, in UTC, or ``None`` when it has none.

    A fire is on a whole minute, so the search starts from the minute after ``moment``, which every version of the cron
    library reads the same way.
    """
    from cronsim import CronSim, CronSimError

    try:
        return next(CronSim(schedule.cron, (_minute(moment) + _MINUTE).astimezone(tz), reverse=True)).astimezone(_UTC)
    except (StopIteration, CronSimError):
        return None


def fires_after(schedule: windows.Schedule, tz: dt.tzinfo, moment: dt.datetime) -> Iterator[dt.datetime]:
    """The fires of a schedule after ``moment``, oldest first, in UTC."""
    from cronsim import CronSim, CronSimError

    try:
        for fire in CronSim(schedule.cron, _minute(moment).astimezone(tz)):
            yield fire.astimezone(_UTC)
    except CronSimError:
        return


def window_of_fire(fire: dt.datetime, schedule: windows.Schedule, size: str | None,
                   tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime] | None:
    """The window that a fire owns, as ``(start, end)`` in UTC, or ``None`` when there is no fire before it.

    With a ``size`` it is the last complete window of that size that ends at or before the fire. Without one it is the
    interval between the previous fire and this fire.
    """
    if size is not None:
        return windows.window_ending_at(windows.floor_boundary(fire, size, tz), size, tz)
    previous = fire_at_or_before(schedule, tz, fire - _MINUTE)
    return None if previous is None else (previous, fire)


def _scheduled(workflow: Workflow) -> tuple[windows.Schedule, dt.tzinfo, str | None, dt.datetime | None]:
    """What a workflow's schedule needs: the schedule, its time zone, the window size and the start of the history."""
    schedule = workflow.schedule
    if schedule is None:
        raise ConfigError(f"workflow {workflow.identifier} has no schedule")
    value = workflow.metadata.get("start")
    start = None if value is None else windows.parse_boundary(value, workflow.timezone, "metadata.start")
    return schedule, workflow.timezone, windows.window_size_of(workflow.metadata, workflow.spec.get("when")), start


def latest_window(workflow: Workflow, now: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """The window of the latest fire at or before ``now``, or ``None`` when it has none or starts before ``start``."""
    schedule, tz, size, start = _scheduled(workflow)
    fire = fire_at_or_before(schedule, tz, now)
    span = None if fire is None else window_of_fire(fire, schedule, size, tz)
    return None if span is None or (start is not None and span[0] < start) else span


def windows_since(workflow: Workflow, since: dt.datetime | None, now: dt.datetime,
                  limit: int) -> list[tuple[dt.datetime, dt.datetime]]:
    """The windows of the fires after ``since`` up to ``now``, oldest first, each once, at most ``limit`` of them.

    A window that starts before ``since`` is left out. Without ``since`` there is nothing before ``now`` to look at, so
    the list is empty.
    """
    if since is None:
        return []
    schedule, tz, size, _ = _scheduled(workflow)
    found: list[tuple[dt.datetime, dt.datetime]] = []
    previous = None  # the first window looks up the fire before it, and the others know it
    for fire in fires_after(schedule, tz, since):
        if fire > now or len(found) >= limit:
            break
        span = (previous, fire) if size is None and previous is not None else window_of_fire(fire, schedule, size, tz)
        previous = fire
        if span is None or span[0] < since or (found and found[-1] == span):
            continue
        found.append(span)
    return found


def _enumerate(workflow: Workflow, since: dt.datetime | None,
               now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    found = windows_since(workflow, since, now, MAX_ENUMERATED + 1)
    if len(found) > MAX_ENUMERATED:
        raise ConfigError(f"workflow {workflow.identifier} has more than {MAX_ENUMERATED} windows since "
                          f"{format_instant(since)}. Set metadata.start to a later time, and fill the history before "
                          "it with ldp backfill")
    return found


def due_windows(workflow: Workflow, now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """The windows a tick at ``now`` looks at, oldest first, whether they are done or not.

    That is the window of the latest fire, or, when ``metadata.catchup`` is true, the windows of every fire since
    ``metadata.start``.

    Raises:
        ConfigError: If a catch-up has more than :data:`MAX_ENUMERATED` windows.
    """
    if workflow.metadata.get("catchup") is True:
        return _enumerate(workflow, _scheduled(workflow)[3], now)
    latest = latest_window(workflow, now)
    return [] if latest is None else [latest]


def next_fire(workflow: Workflow, now: dt.datetime) -> dt.datetime | None:
    """The next fire after ``now`` whose window is not before ``metadata.start``, in UTC, or ``None``."""
    schedule, tz, size, start = _scheduled(workflow)
    for fire in itertools.islice(fires_after(schedule, tz, now), _SEARCH):
        if start is None:
            return fire
        span = window_of_fire(fire, schedule, size, tz)
        if span is not None and span[0] >= start:
            return fire
    return None


# ---------------------------------------------------------------------- finding what is scheduled


@dataclass(frozen=True)
class Scheduled:
    """A workflow file, or a dataset config, that has a schedule.

    Attributes:
        path: The file, spelled as the folder that was searched spells it.
        workflow: The workflow, or the one-task workflow of the config.
        kind: ``Workflow`` for a workflow file and ``Pipeline`` for a dataset config.
    """

    path: Path
    workflow: Workflow
    kind: str


@dataclass(frozen=True)
class Discovery:
    """What a folder holds: the scheduled workflows and configs, and the files that have a schedule and cannot be used.

    Attributes:
        scheduled: Every scheduled workflow and config, sorted by path.
        problems: ``(path, lines)`` for each file that has a schedule and is not valid, with what is wrong.
    """

    scheduled: list[Scheduled] = field(default_factory=list)
    problems: list[tuple[Path, list[str]]] = field(default_factory=list)


def _json_files(folder: Path) -> Iterator[Path]:
    for root, names, files in os.walk(folder):
        names[:] = [name for name in names if not name.startswith(".") and name not in SKIPPED_FOLDERS]
        for name in files:
            if name.endswith(".json") and not name.endswith(".metadata.json"):
                yield Path(root) / name


def discover(directory: "str | Path") -> Discovery:
    """Find every workflow file and dataset config with a schedule under a folder, told apart by their ``kind``.

    It reads each ``.json`` file under the folder, not those in a folder whose name starts with a dot or in
    ``node_modules``, ``venv`` or ``site-packages``, and not one larger than 1 MB. A file that is not a workflow or a
    dataset config, or has no schedule, is not an error and is not listed. A workflow or a config that has a schedule
    and is not valid is listed in :attr:`Discovery.problems`.

    Raises:
        ConfigError: If ``directory`` is not a folder.
    """
    folder = Path(directory)
    if not folder.is_dir():
        raise ConfigError(f"{folder} is not a folder")
    found = Discovery()
    for path in sorted(_json_files(folder)):
        try:
            if path.stat().st_size > MAX_SPEC_BYTES:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # not a JSON file that can be read, so not a spec
        if not isinstance(data, Mapping):
            continue
        try:
            kind = kind_of(data)
        except ConfigError:
            continue
        metadata = data.get("metadata") if isinstance(data.get("metadata"), Mapping) else {}
        if kind not in ("Workflow", "Pipeline") or (
                kind == "Pipeline" and "kind" not in data and not {"source", "target"} <= set(metadata)):
            continue
        try:
            if windows.effective_schedule(metadata, data.get("when")) is None:
                continue
        except ConfigError as error:
            found.problems.append((path, [f"metadata.schedule: {error}"]))
            continue
        workflow, problems = read_workflow(path)
        if workflow is None:
            found.problems.append((path, problems))
        else:
            found.scheduled.append(Scheduled(path, workflow, kind))
    return found


# ---------------------------------------------------------------------- the tick


@dataclass
class WorkflowTick:
    """What a tick did for one workflow.

    Attributes:
        path: The workflow file or config.
        workflow: The identifier of the workflow.
        kind: ``Workflow`` or ``Pipeline``.
        due: How many of the windows it looked at were not done, so that this tick ran them or left them for the next.
        result: The windows it looked at, with how each ended, or ``None`` when there was none or the workflow could
            not be ticked.
        error: Why the workflow could not be ticked, such as a config it names that is not valid.
    """

    path: Path
    workflow: str
    kind: str
    due: int = 0
    result: BackfillResult | None = None
    error: str | None = None

    @property
    def exit_code(self) -> int:
        """1 when the workflow could not be ticked or a window failed, 3 when a data gate blocked one, else 0."""
        if self.error is not None:
            return 1
        return self.result.exit_code if self.result is not None else 0

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"file": str(self.path), "workflow": self.workflow, "kind": self.kind, "due": self.due,
                "exit_code": self.exit_code, "error": self.error,
                "windows": [entry.to_dict() for entry in self.result.windows] if self.result else []}


@dataclass
class TickResult:
    """What a tick did.

    Attributes:
        directory: The folder that was ticked.
        now: The time of the tick.
        overlapped: True when another tick held the lock, so this one did nothing.
        workflows: What the tick did for each scheduled workflow, in the order of their paths.
        problems: ``(path, lines)`` for each file with a schedule that is not valid, from :func:`discover`.
        duration_s: How long the tick took.
    """

    directory: Path
    now: dt.datetime
    overlapped: bool = False
    workflows: list[WorkflowTick] = field(default_factory=list)
    problems: list[tuple[Path, list[str]]] = field(default_factory=list)
    duration_s: float = 0.0

    def counts(self) -> dict[str, int]:
        """How many windows ended with each outcome, over every workflow, see :meth:`BackfillResult.counts`.

        ``not_run`` is there too when a catch-up left windows for the next tick.
        """
        total = dict.fromkeys(OUTCOMES, 0)
        for item in self.workflows:
            for name, count in (item.result.counts() if item.result else {}).items():
                total[name] = total.get(name, 0) + count
        return total

    @property
    def exit_code(self) -> int:
        """0 when nothing failed or was blocked, or another tick was running, 3 when only a data gate held a window
        back, and 1 when a window failed, a workflow could not be ticked, or a file with a schedule is not valid."""
        codes = [item.exit_code for item in self.workflows]
        if self.problems or 1 in codes:
            return 1
        return 3 if 3 in codes else 0

    @property
    def status(self) -> str:
        """``overlapped``, ``failed``, ``blocked_quality`` or ``succeeded``."""
        if self.overlapped:
            return "overlapped"
        return {0: "succeeded", 1: "failed", 3: "blocked_quality"}[self.exit_code]

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"directory": str(self.directory), "now": format_instant(self.now), "status": self.status,
                "exit_code": self.exit_code, "overlapped": self.overlapped, "duration_s": round(self.duration_s, 3),
                "counts": self.counts(), "workflows": [item.to_dict() for item in self.workflows],
                "problems": [{"file": str(path), "lines": lines} for path, lines in self.problems]}


def tick(directory: "str | Path", *, now: "dt.datetime | str | None" = None, max_windows: int = DEFAULT_MAX_WINDOWS,
         policy: Any = None, sleep: Callable[[float], None] | None = None, rng: Any = None,
         on_window: Callable[[Scheduled, BackfillWindow, int, int], None] | None = None) -> TickResult:
    """Run every window that is due at ``now`` and not done, for every scheduled workflow under a folder, then return.

    A window that failed on an earlier tick is carried on, see the module. A workflow that cannot be ticked, such as one
    whose config is not valid, is recorded in its :class:`WorkflowTick` and does not stop the others. A tick that starts
    while another runs on the same folder does nothing.

    Args:
        directory: The folder to tick. Its ``.ldp/locks/schedule.lock`` is the lock.
        now: The time of the tick: ISO-8601 text or a datetime, UTC when naive. The current time by default.
        max_windows: The most windows that are not done that one workflow runs in one tick, which only a catch-up can
            exceed. The windows after them are left for the next tick, with the outcome ``not_run``.
        policy: The commit policy of the writes, see :func:`~local_data_platform.orchestration.backfills.backfill`.
        sleep: What waits between the attempts of a task that is retried.
        rng: What draws the jitter of those waits.
        on_window: Called as ``on_window(scheduled, window, number, total)`` when a window of a workflow is done, or
            was skipped, so a caller can show progress.

    Returns:
        The :class:`TickResult`. It does not raise for a window that failed: read ``result.exit_code``.

    Raises:
        ConfigError: If ``directory`` is not a folder, ``now`` is not a time, or ``max_windows`` is not a whole number
            from 1.
    """
    began = time.perf_counter()
    if not isinstance(max_windows, int) or isinstance(max_windows, bool) or max_windows < 1:
        raise ConfigError(f"max_windows must be a whole number from 1, got {max_windows!r}")
    folder = Path(directory)
    if not folder.is_dir():
        raise ConfigError(f"{folder} is not a folder")
    result = TickResult(folder, parse_now(now))
    with file_lock(folder / LOCK_PATH, blocking=False) as held:
        if not held:
            result.overlapped = True
        else:
            found = discover(folder)
            result.problems = found.problems
            for item in found.scheduled:
                result.workflows.append(_tick_workflow(item, result.now, max_windows, policy, sleep, rng, on_window))
    result.duration_s = time.perf_counter() - began
    return result


def _tick_workflow(item: Scheduled, now: dt.datetime, max_windows: int, policy: Any,
                   sleep: Callable[[float], None] | None, rng: Any,
                   on_window: Callable[[Scheduled, BackfillWindow, int, int], None] | None) -> WorkflowTick:
    done = WorkflowTick(item.path, item.workflow.identifier, item.kind)
    try:
        due = due_windows(item.workflow, now)
        if not due:
            return done
        check_workflow(item.workflow)
        zone = getattr(item.workflow.timezone, "key", "UTC")
        size = windows.window_size_of(item.workflow.metadata, item.workflow.spec.get("when"))
        announce = None if on_window is None else (lambda entry, number, total: on_window(item, entry, number, total))
        done.result = run_windows(item.workflow, due, window_size=size, timezone=zone, start=due[0][0],
                                  end=due[-1][1], policy=policy, sleep=sleep, rng=rng, continue_runs=True,
                                  max_pending=max_windows, on_window=announce)
        done.due = sum(1 for entry in done.result.windows if entry.state != "published")
    except ConfigError as error:
        done.error = str(error)
    except Exception as error:  # noqa: BLE001 - one workflow must not stop the others on an unattended tick
        logger.debug("Tick of %s failed", item.path, exc_info=True)
        done.error = f"{type(error).__name__}: {error}"
    return done


# ---------------------------------------------------------------------- listing


@dataclass
class ScheduleStatus:
    """What ``ldp schedule list`` says about one scheduled workflow.

    Attributes:
        path: The file.
        workflow: The identifier of the workflow.
        kind: ``Workflow`` or ``Pipeline``.
        cron: The schedule as written, such as ``@daily`` or ``5 2 * * *``.
        timezone: The time zone the schedule and its days are counted in.
        window_size: ``day``, ``hour`` or ``15m``, or ``None`` when a window is the interval between two fires.
        catchup: Whether a tick runs every window it missed since ``start``.
        start: Where the history starts, or ``None``.
        next_fire: The next fire, in UTC.
        last_window: The latest window that a run started, as ``START/END``, or ``None`` when none did.
        last_status: How the last run of that window ended: ``succeeded``, ``failed``, ``blocked``, ``incomplete``, or
            ``unfinished`` for a run that never finished.
        due: The windows that the next tick runs, because they are not done.
        missed: The windows before the latest fire that are not done and that a tick does not run. With ``catchup`` it
            is empty, because every window that is not done is due.
        more_missed: True when there are more missed windows than the list holds.
        commands: The commands that fill the missed windows: one ``ldp backfill`` for all of them, or an
            ``ldp workflow run`` for each when the windows have no size.
        error: Why the schedule could not be read, such as a config that is not valid.
    """

    path: Path
    workflow: str
    kind: str
    cron: str
    timezone: str
    window_size: str | None
    catchup: bool
    start: dt.datetime | None
    next_fire: dt.datetime | None = None
    last_window: str | None = None
    last_status: str | None = None
    due: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)
    missed: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)
    more_missed: bool = False
    commands: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self, local_tz: dt.tzinfo | None = None) -> dict[str, Any]:
        """The status as JSON-serialisable primitives, with the next fire in UTC and in local time."""
        def span(pair: tuple[dt.datetime, dt.datetime]) -> str:
            return f"{format_instant(pair[0])}/{format_instant(pair[1])}"

        local = self.next_fire.astimezone(local_tz).isoformat(timespec="seconds") if self.next_fire else None
        return {"file": str(self.path), "workflow": self.workflow, "kind": self.kind, "schedule": self.cron,
                "timezone": self.timezone, "window_size": self.window_size, "catchup": self.catchup,
                "start": format_instant(self.start) if self.start else None,
                "next_fire": format_instant(self.next_fire) if self.next_fire else None, "next_fire_local": local,
                "last_window": self.last_window, "last_status": self.last_status,
                "due": [span(pair) for pair in self.due], "missed": [span(pair) for pair in self.missed],
                "more_missed": self.more_missed, "commands": list(self.commands), "error": self.error}


@dataclass
class Listing:
    """What ``ldp schedule list`` found under a folder.

    Attributes:
        directory: The folder.
        now: The time the listing is for.
        items: One :class:`ScheduleStatus` for each scheduled workflow, in the order of their paths.
        problems: ``(path, lines)`` for each file with a schedule that is not valid.
    """

    directory: Path
    now: dt.datetime
    items: list[ScheduleStatus] = field(default_factory=list)
    problems: list[tuple[Path, list[str]]] = field(default_factory=list)

    def to_dict(self, local_tz: dt.tzinfo | None = None) -> dict[str, Any]:
        """The listing as JSON-serialisable primitives."""
        return {"directory": str(self.directory), "now": format_instant(self.now),
                "scheduled": [item.to_dict(local_tz) for item in self.items],
                "problems": [{"file": str(path), "lines": lines} for path, lines in self.problems]}


def _boundary_text(moment: dt.datetime, tz: dt.tzinfo) -> str:
    """A bound for ``ldp backfill``: the date when it is a local midnight, else the instant in UTC."""
    local = moment.astimezone(tz)
    if (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0):
        return local.date().isoformat()
    return format_instant(moment)


def fill_commands(path: Path, workflow: Workflow, missed: list[tuple[dt.datetime, dt.datetime]]) -> list[str]:
    """The commands that fill the missed windows of a workflow.

    Windows of a size are filled by one ``ldp backfill`` from the first window to the last, which skips the windows in
    between that are done. Windows that have no size, because they are the intervals between two fires, are not a range,
    so each gets an ``ldp workflow run``.
    """
    if not missed:
        return []
    file = shlex.quote(str(path))
    if windows.window_size_of(workflow.metadata, workflow.spec.get("when")) is None:
        return [f"ldp workflow run {file} --window {format_instant(first)}/{format_instant(last)}"
                for first, last in missed]
    tz = workflow.timezone
    return [f"ldp backfill {file} --from {_boundary_text(missed[0][0], tz)} --to {_boundary_text(missed[-1][1], tz)}"]


def _status_of(item: Scheduled, now: dt.datetime, limit: int) -> ScheduleStatus:
    workflow = item.workflow
    schedule, tz, size, start = _scheduled(workflow)
    status = ScheduleStatus(item.path, workflow.identifier, item.kind, schedule.expression,
                            getattr(tz, "key", "UTC"), size, workflow.metadata.get("catchup") is True, start)
    status.next_fire = next_fire(workflow, now)
    records = read_windows(workflow.ledger_path, workflow.identifier)
    if records:
        last = max(records, key=lambda pair: pair[1])
        status.last_window = f"{last[0]}/{last[1]}"
        record = records[last]
        status.last_status = ("unfinished" if record.tasks is None
                              else run_status(record.tasks, [task.id for task in workflow.tasks]))
    due = due_windows(workflow, now)
    lower = start
    if lower is None and records:
        lower = windows.parse_boundary(min(records, key=lambda pair: pair[0])[0], None, "the first window")
    considered = _enumerate(workflow, lower, now) if lower is not None else []
    candidates = list(dict.fromkeys([*considered, *due]))
    if not candidates:
        return status
    check = run_windows(workflow, candidates, window_size=size, timezone=status.timezone, start=candidates[0][0],
                        end=candidates[-1][1], dry_run=True)
    pending = {(entry.start, entry.end) for entry in check.windows if entry.state != "published"}
    status.due = [pair for pair in due if pair in pending]
    due_set = set(due)
    missed = [pair for pair in considered if pair in pending and pair not in due_set]
    status.more_missed = len(missed) > limit
    status.missed = missed[:limit]
    status.commands = fill_commands(item.path, workflow, status.missed)
    return status


def list_scheduled(directory: "str | Path", *, now: "dt.datetime | str | None" = None,
                   max_windows: int = DEFAULT_MAX_WINDOWS) -> Listing:
    """List every scheduled workflow and config under a folder, with its next fire, its last window and what it missed.

    It reads the schedules, the ledgers and the keys on the tables, and writes nothing, so it can run while a tick runs.

    Args:
        directory: The folder.
        now: The time to list for: ISO-8601 text or a datetime, UTC when naive. The current time by default.
        max_windows: The most missed windows that the listing names for each workflow, the oldest first.

    Raises:
        ConfigError: If ``directory`` is not a folder, ``now`` is not a time, or ``max_windows`` is not a whole number
            from 1.
    """
    if not isinstance(max_windows, int) or isinstance(max_windows, bool) or max_windows < 1:
        raise ConfigError(f"max_windows must be a whole number from 1, got {max_windows!r}")
    moment = parse_now(now)
    found = discover(directory)
    listing = Listing(Path(directory), moment, problems=found.problems)
    for item in found.scheduled:
        try:
            listing.items.append(_status_of(item, moment, max_windows))
        except Exception as error:  # noqa: BLE001 - one workflow that cannot be read does not hide the others
            logger.debug("Listing of %s failed", item.path, exc_info=True)
            schedule = item.workflow.schedule
            listing.items.append(ScheduleStatus(
                item.path, item.workflow.identifier, item.kind, schedule.expression if schedule else "",
                getattr(item.workflow.timezone, "key", "UTC"), None, False, None,
                error=f"{type(error).__name__}: {error}"))
    return listing


__all__ = [
    "LOCK_PATH",
    "Discovery",
    "Listing",
    "ScheduleStatus",
    "Scheduled",
    "TickResult",
    "WorkflowTick",
    "discover",
    "due_windows",
    "fill_commands",
    "fire_at_or_before",
    "fires_after",
    "latest_window",
    "list_scheduled",
    "next_fire",
    "parse_now",
    "tick",
    "window_of_fire",
    "windows_since",
]
