"""Run ``dbt build`` or ``dbt test`` on a project, and publish what a build made.

``ldp dbt build``, ``ldp dbt test`` and the ``dbt`` task of a workflow run the same steps, and these functions are
those steps. Nothing here prints: a caller reports what it learns from the result, or from the error that says why
dbt did not succeed, and the contract is ``docs/design/warehousing.md``.

* :func:`prepare` loads the project, finds dbt and checks the profile, which writes nothing, so a missing dbt changes
  nothing. :func:`register` then registers the Iceberg tables as sources.
* :func:`run_build` runs ``dbt build`` and, when it succeeded, writes the models marked ``ldp_publish`` to Iceberg.
* :func:`run_test` runs ``dbt test`` and publishes nothing.
* :func:`project_fingerprint` hashes the files that decide what a build makes, so a caller can tell that a build
  would be the same as an earlier one.

Nothing here imports dbt, and DuckDB is imported only when sources are registered.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.dbt_layer.project import (
    DbtProject,
    check_profile,
    find_dbt,
    find_target_path,
    run_dbt,
)
from local_data_platform.dbt_layer.publish import (
    Published,
    PublishItem,
    PublishOutcome,
    failed_nodes,
    plan_publish,
    publish,
    read_artifacts,
)
from local_data_platform.dbt_layer.sources import MODES, SourceRegistration, register_sources, source_tables
from local_data_platform.exceptions import ConfigError, DataQualityError, LDPError

CLOCK_SLACK_S = 2.0
"""How much older than the start of a dbt run an artifact may be and still count as written by it."""

MAX_FAILED_LINES = 10
"""How many failed nodes an error message lists."""

SKIPPED_FOLDERS = frozenset({"target", "logs", "dbt_packages", "dbt_modules", "__pycache__", "node_modules", "venv"})
"""Folders that hold what dbt or a tool made, not what the project says, so a fingerprint leaves them out."""


@dataclass(frozen=True)
class Prepared:
    """A project that dbt can run on.

    Attributes:
        project: The project, as ``ldp_dbt.json`` describes it.
        binary: The dbt to run.
    """

    project: DbtProject
    binary: str


@dataclass(frozen=True)
class BuildResult:
    """What a successful ``dbt build`` or ``dbt test`` did.

    Attributes:
        command: ``"build"`` or ``"test"``.
        nodes: How many dbt nodes ended with each status, such as ``{"success": 3, "pass": 5}``. Empty when dbt wrote
            no ``run_results.json`` for this run.
        elapsed_s: The time dbt says it took, or ``None``.
        planned: The models a build planned to publish, in the order it wrote them. Empty for a test, and when no
            model marked ``ldp_publish`` ran.
        published: The models a build wrote to Iceberg, in order.
    """

    command: str
    nodes: Mapping[str, int] = field(default_factory=dict)
    elapsed_s: float | None = None
    planned: tuple[PublishItem, ...] = ()
    published: tuple[Published, ...] = ()


class DbtFailed(LDPError):
    """dbt exited with a status other than 0, so nothing was published.

    Attributes:
        command: ``"build"`` or ``"test"``.
        status: dbt's exit status.
        failed: Every node of the run that failed, each ``{"node", "status", "message"}``. Empty when dbt wrote none.
        nodes: How many nodes ended with each status.
    """

    def __init__(self, message: str, *, command: str, status: int, failed: Sequence[Mapping[str, str]] = (),
                 nodes: Mapping[str, int] | None = None):
        super().__init__(message)
        self.command = command
        self.status = status
        self.failed = [dict(node) for node in failed]
        self.nodes = dict(nodes or {})

    @property
    def data_gate(self) -> bool:
        """True when the only thing wrong is that data tests failed, which retrying cannot fix.

        A data test whose status is ``fail`` found data that breaks its rule. Any other failed node means something is
        broken, so it is not a data gate: a model that raised an error, a test whose own SQL did, and a unit test, which
        fails when the logic of a model is wrong and not when the data is.
        """
        return bool(self.failed) and all(node["status"] == "fail" and node["node"].startswith("test.")
                                         for node in self.failed)


class PublishFailed(LDPError):
    """Publishing a model failed after dbt had succeeded.

    Attributes:
        outcome: What was and was not published. ``outcome.failed`` is ``(model, error)``.
    """

    def __init__(self, message: str, outcome: PublishOutcome):
        super().__init__(message)
        self.outcome = outcome

    @property
    def data_gate(self) -> bool:
        """True when the quality check of a model blocked its write, which is a data gate."""
        return self.outcome.failed is not None and isinstance(self.outcome.failed[1], DataQualityError)


# --------------------------------------------------------------------------- preparing


def prepare(directory: str | os.PathLike, *, dbt_bin: str | os.PathLike | None = None) -> Prepared:
    """Load the project, find dbt and check the profile. It writes nothing.

    Args:
        directory: The dbt project folder, which holds ``ldp_dbt.json``.
        dbt_bin: The dbt to run. By default the one next to this Python, else the one on ``PATH``.

    Raises:
        ConfigError: If the project is missing or invalid, or its profile points at another build file.
        EngineNotFound: If dbt is not installed.
    """
    project = DbtProject.load(directory)
    binary = find_dbt(dbt_bin)
    check_profile(project)
    return Prepared(project, binary)


def register(project: DbtProject, *, mode: str = "auto") -> SourceRegistration:
    """Make every Iceberg table that the project's source configs name a relation in its build file.

    Args:
        project: The project.
        mode: How to register the tables, one of :data:`~local_data_platform.dbt_layer.sources.MODES`.

    Raises:
        ConfigError: If ``mode`` is not one of them, or a source cannot be registered.
        EngineNotFound: If DuckDB is not installed.
    """
    if mode not in MODES:
        raise ConfigError(f"the sources mode must be one of {list(MODES)}, got {mode!r}")
    return register_sources(project.database, project.source_configs, project.source_catalogs, mode=mode,
                            sources_file=project.sources_file)


def registration_summary(registration: SourceRegistration) -> str:
    """One line that says what registering the sources did, such as ``registered 3 sources as views over ...``."""
    def plural(count: int, one: str, many: str) -> str:
        return f"{count} {one if count == 1 else many}"

    views = sum(source.kind == "view" for source in registration.sources)
    copies = len(registration.sources) - views
    total = plural(len(registration.sources), "source", "sources")
    if not registration.sources:
        summary = "no sources (no Iceberg tables were found)"
    elif not copies:
        summary = f"{total} as views over Iceberg snapshots"
    elif not views:
        summary = f"{total} as copies of Iceberg snapshots"
    else:
        summary = f"{total} ({plural(views, 'view', 'views')}, {plural(copies, 'copy', 'copies')})"
    dropped = f", removed {len(registration.dropped)} that no longer exist" if registration.dropped else ""
    return f"registered {summary}{dropped}"


# --------------------------------------------------------------------------- running dbt


def run_build(project: DbtProject, binary: str, extra: Sequence[str] = (), *,
              report: Callable[[Published], None] | None = None, wrap_sink: Callable[[Any], Any] | None = None,
              stdout: int | None = None) -> BuildResult:
    """Run ``dbt build`` and, when it succeeded, write each model marked ``ldp_publish`` to Iceberg.

    Args:
        project: The prepared project.
        binary: The dbt to run.
        extra: What follows ``build`` on the dbt command line, such as ``["--select", "gold"]``.
        report: Called with each model right after it was written.
        wrap_sink: Passed to :func:`~local_data_platform.dbt_layer.publish.publish`.
        stdout: Where dbt's standard output goes. See :func:`~local_data_platform.dbt_layer.project.run_dbt`.

    Returns:
        The :class:`BuildResult`.

    Raises:
        DbtFailed: If dbt exited with a status other than 0. Nothing is published.
        PublishFailed: If writing a model failed. The models before it stay published.
        ConfigError: If dbt left no usable artifacts, or the meta of a model is wrong. Nothing is published.
    """
    started = time.time() - CLOCK_SLACK_S
    status = run_dbt(project, binary, ["build", *extra], stdout=stdout)
    target = find_target_path(project, extra)
    if status != 0:
        raise _dbt_failed("build", project, target, started, status)
    manifest, run_results = read_artifacts(project, target, since=started)
    plan = plan_publish(project, manifest, run_results, source_tables=source_tables(project.source_configs))
    nodes, elapsed = _summary(run_results)
    if not plan:
        return BuildResult("build", nodes, elapsed)
    outcome = publish(plan, report=report, wrap_sink=wrap_sink)
    if outcome.failed:
        raise _publish_failed(outcome)
    return BuildResult("build", nodes, elapsed, tuple(plan), tuple(outcome.published))


def run_test(project: DbtProject, binary: str, extra: Sequence[str] = (), *, stdout: int | None = None) -> BuildResult:
    """Run ``dbt test``. It publishes nothing.

    Raises:
        DbtFailed: If dbt exited with a status other than 0.
    """
    started = time.time() - CLOCK_SLACK_S
    status = run_dbt(project, binary, ["test", *extra], stdout=stdout)
    target = find_target_path(project, extra)
    if status != 0:
        raise _dbt_failed("test", project, target, started, status)
    results = _read_results(project, target, started)
    nodes, elapsed = _summary(results) if results is not None else ({}, None)
    return BuildResult("test", nodes, elapsed)


def _read_results(project: DbtProject, target: str, started: float) -> Mapping[str, Any] | None:
    """The ``run_results.json`` this run wrote, or ``None`` when there is none to read."""
    folder = Path(target)
    file = (folder if folder.is_absolute() else project.directory / folder) / "run_results.json"
    try:
        if file.stat().st_mtime < started:
            return None  # an earlier run's file
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _summary(run_results: Mapping[str, Any]) -> tuple[dict[str, int], float | None]:
    counts = Counter(str(result.get("status")) for result in run_results.get("results", []))
    elapsed = run_results.get("elapsed_time")
    return dict(counts), round(float(elapsed), 3) if isinstance(elapsed, (int, float)) else None


def _dbt_failed(command: str, project: DbtProject, target: str, started: float, status: int) -> DbtFailed:
    """The error for a dbt command that exited with ``status``, naming the nodes that failed when dbt wrote them."""
    results = _read_results(project, target, started)
    failed = failed_nodes(results) if results is not None else []
    nodes = _summary(results)[0] if results is not None else {}
    if command != "build":
        return DbtFailed(f"dbt {command} exited with status {status}", command=command, status=status, failed=failed,
                         nodes=nodes)
    lines = [f"dbt build failed with exit status {status}, so nothing was published"]
    for node in failed[:MAX_FAILED_LINES]:
        lines.append(f"  - {node['node']}: {node['status']}" + (f", {node['message']}" if node["message"] else ""))
    if len(failed) > MAX_FAILED_LINES:
        lines.append(f"  ... and {len(failed) - MAX_FAILED_LINES} more")
    return DbtFailed("\n".join(lines), command=command, status=status, failed=failed, nodes=nodes)


def _publish_failed(outcome: PublishOutcome) -> PublishFailed:
    model, error = outcome.failed
    detail = str(error) if isinstance(error, LDPError) else f"{type(error).__name__}: {error}"
    lines = [f"publishing {model} failed, so the models after it were not tried"]
    lines += [f"  {line}" for line in detail.splitlines()]
    if outcome.published:
        lines.append("  published: " + ", ".join(item.table for item in outcome.published))
    lines.append("  not published: " + ", ".join([model, *(outcome.pending or [])]))
    lines.append(f"  to retry {model} by hand: ldp run {outcome.failed_config}")
    return PublishFailed("\n".join(lines), outcome)


# --------------------------------------------------------------------------- fingerprint


def project_fingerprint(project: DbtProject, *, target_path: str | os.PathLike = "target",
                        extra: Mapping[str, Any] | None = None) -> str:
    """A hash of everything that decides what a build of the project makes, as 16 hex characters.

    It covers the content of every file of the project folder (models, macros, seeds, tests, ``dbt_project.yml``,
    ``profiles.yml`` and ``ldp_dbt.json``) and ``extra``, such as the command, the selection and the variables of a
    run. It leaves out what dbt and LDP write: dbt's target and log folders, packages, the build file, the generated
    sources file, the generated configs, and hidden files, which tools such as Finder and editors create. So the same
    project and the same ``extra`` give the same fingerprint on every run, and changing a model, a test, a profile or
    a variable changes it.

    What a model reads from outside the project folder, such as a file it names by an absolute path, is not covered.
    """
    folder = project.directory.resolve()
    skipped_dirs = {project.config_dir.resolve(), (folder / target_path).resolve()}
    skipped_files = {project.database.resolve(), project.database.with_name(project.database.name + ".wal").resolve(),
                     project.sources_file.resolve()}
    digest = hashlib.sha256()
    for current, names, files in os.walk(folder):
        here = Path(current)
        names[:] = sorted(name for name in names
                          if name not in SKIPPED_FOLDERS and not name.startswith(".")
                          and (here / name).resolve() not in skipped_dirs)
        for name in sorted(files):
            path = here / name
            if name.startswith(".") or path.resolve() in skipped_files:
                continue
            digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
            digest.update(_file_digest(path) + b"\0")
    digest.update(json.dumps(dict(extra or {}), sort_keys=True, default=str, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()[:16]


def _file_digest(path: Path) -> bytes:
    content = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                content.update(block)
    except OSError:  # a broken link or a file that went away: say so, so that the fingerprint differs from a read one
        content.update(b"unreadable")
    return content.digest()


__all__ = [
    "BuildResult", "DbtFailed", "Prepared", "PublishFailed", "SKIPPED_FOLDERS", "prepare", "project_fingerprint",
    "register", "registration_summary", "run_build", "run_test",
]
