"""``ldp gold build``: build every gold table of a folder, in the order that their inputs say.

A gold config is a dataset config whose ``metadata.docs.layer`` is ``gold``. A gold table that is defined as one SQL
file over other tables has a ``DUCKDB`` source with ``inputs``, and ``ldp gold build`` runs its config. A gold table may
read another, so the configs are built after the configs that they read, and a cycle is refused before anything runs. A
gold config with no ``inputs`` is built elsewhere, by dbt, by Spark or by a script. It is not built here. It is
checked, which means that the table has to exist, and that its quality checks run on the current snapshot and write
nothing.

A build stops at the first table that fails or that a gate blocks, because the tables after it could read what it did
not write. The runs record their events through the ``iceberg`` sink, in the ``_ldp`` namespace of the catalog of each
table, unless the config names sinks of its own, so the freshness and the gate status of a gold table show in
``describe_table`` of ``ldp mcp``.

When every table was built or checked, the build can cut a release, which is :mod:`local_data_platform.gold.release`.
A release names the snapshot that each gold table ended the build with, so a reader that reads the current release
sees tables that agree. A build that stops cuts none, and the last release stays current.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError, DataQualityError, TableNotFound
from local_data_platform.paths import resolve_path
from local_data_platform.spec import DEFAULT_KIND, kind_of_file

if TYPE_CHECKING:
    from local_data_platform.gold.release import ReleaseContext, ReleaseOutcome

BUILT, CHECKED, BLOCKED, FAILED, NOT_RUN = "built", "checked", "blocked", "failed", "not run"
"""How a table ended: built here, checked because it is built elsewhere, held back by a gate, failed, or not reached."""


@dataclass(frozen=True)
class GoldConfig:
    """A gold config of a folder.

    Attributes:
        path: The config file.
        config: The config.
        inputs: The input configs of its ``DUCKDB`` source, as absolute paths. Empty for a table built elsewhere.
    """

    path: Path
    config: Config
    inputs: tuple[Path, ...] = ()

    @property
    def built_here(self) -> bool:
        """Whether ``ldp gold build`` builds the table, which it does when the source has ``inputs``."""
        return bool(self.inputs)

    @property
    def table(self) -> str:
        """The Iceberg table, ``<namespace>.<name>``."""
        from local_data_platform.catalog.provider import catalog_namespace

        target = self.config.metadata["target"]
        return f"{catalog_namespace(target['catalog'])}.{target['name']}"


@dataclass
class TableOutcome:
    """What a build did with one gold table.

    Attributes:
        gold: The gold config.
        status: ``built``, ``checked``, ``blocked``, ``failed`` or ``not run``.
        rows: Rows written by a build, or rows of the current snapshot for a table that was only checked.
        snapshot_id: The snapshot that holds them.
        gate: How the quality checks ended, such as ``4 of 4 checks passed``.
        error: The message of a table that failed or that a gate blocked.
        result: The :class:`~local_data_platform.pipeline.PipelineResult` of a table that was built.
    """

    gold: GoldConfig
    status: str = NOT_RUN
    rows: int | None = None
    snapshot_id: int | None = None
    gate: str = ""
    error: str | None = None
    result: Any = None

    def to_dict(self) -> dict[str, Any]:
        """The outcome as JSON-serialisable primitives."""
        return {"config": self.gold.config.identifier, "file": self.gold.path.name, "table": self.gold.table,
                "status": self.status, "rows": self.rows, "snapshot_id": self.snapshot_id, "gate": self.gate,
                "error": self.error, "run_status": getattr(self.result, "status", None)}


@dataclass
class BuildResult:
    """What :func:`build_gold` did.

    Attributes:
        folder: The folder.
        tables: One outcome for each gold config, in the order they were to be built.
        duration_s: How long the build took.
        release: What the build did about its release, or ``None`` when it was not asked to cut one.
    """

    folder: Path
    tables: list[TableOutcome] = field(default_factory=list)
    duration_s: float = 0.0
    release: ReleaseOutcome | None = None

    @property
    def stopped_by(self) -> TableOutcome | None:
        """The table that stopped the build, or ``None``."""
        return next((item for item in self.tables if item.status in (BLOCKED, FAILED)), None)

    @property
    def exit_code(self) -> int:
        """0 when every table was built or checked, 3 when a gate held a table back, 1 for any other failure.

        A release that could not be cut is a failure too, though every table was built.
        """
        stopped = self.stopped_by
        if stopped is None:
            return 1 if self.release is not None and self.release.error else 0
        return 3 if stopped.status == BLOCKED else 1

    def counts(self) -> dict[str, int]:
        """How many tables ended each way."""
        return {status: sum(item.status == status for item in self.tables)
                for status in (BUILT, CHECKED, BLOCKED, FAILED, NOT_RUN)}

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"folder": str(self.folder), "counts": self.counts(), "exit_code": self.exit_code,
                "duration_s": round(self.duration_s, 3), "tables": [item.to_dict() for item in self.tables],
                "release": self.release.to_dict() if self.release is not None else None}


# --------------------------------------------------------------------------- finding and ordering


def _looks_like_a_config(file: Path) -> bool:
    """Whether a JSON file is a dataset config, so that a broken one is reported and a file of another sort is not."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, Mapping) and "metadata" in data and "identifier" in data


def find_gold(folder: str | os.PathLike) -> list[GoldConfig]:
    """The gold configs of a folder, in the order of their file names.

    A file of another kind than ``Pipeline``, such as a workflow, and a JSON file that is not a dataset config, are
    skipped. A dataset config that is not valid is an error, so that a typo does not make a table silently vanish from
    the build.

    Raises:
        ConfigError: If ``folder`` is not a folder, a config is not valid, or a gold config does not write an Iceberg
            table.
    """
    root = resolve_path(folder)
    if not root.is_dir():
        raise ConfigError(f"{root} is not a folder of dataset configs")
    found = []
    for file in sorted(root.glob("*.json")):
        if kind_of_file(file) not in (DEFAULT_KIND, None) or not _looks_like_a_config(file):
            continue
        try:
            config = Config.from_json(file)
        except ConfigError as error:
            raise ConfigError(f"{file.name}: {error}") from None
        if config.docs is None or not config.docs.gold:
            continue
        target = config.metadata["target"]
        if str(target.get("format", "")).strip().upper() != "ICEBERG":
            raise ConfigError(f"{file.name} is a gold table, so its target has to be an Iceberg table, and it is a "
                              f"{target.get('format')} file")
        source = config.metadata["source"]
        inputs = tuple(resolve_path(item, config.base_dir) for item in source.get("inputs") or ())
        found.append(GoldConfig(file, config, inputs))
    return found


def order_gold(found: list[GoldConfig]) -> list[GoldConfig]:
    """The gold configs, each after the gold configs that its ``inputs`` name. Where that leaves a choice, by file name.

    Raises:
        ConfigError: If the configs read each other in a cycle. The message names the files.
    """
    for item in found:
        if item.path.resolve() in [path.resolve() for path in item.inputs]:
            raise ConfigError(f"the gold config {item.path.name} reads its own table")
    by_path = {item.path.resolve(): item for item in found}
    reads = {item.path.resolve(): sorted({path.resolve() for path in item.inputs if path.resolve() in by_path})
             for item in found}
    order: list[GoldConfig] = []
    done: set[Path] = set()
    remaining = sorted(by_path, key=lambda path: path.name)
    while remaining:
        ready = [path for path in remaining if all(read in done for read in reads[path])]
        if not ready:
            raise ConfigError("the gold configs read each other in a cycle, " + _describe_cycle(reads, remaining))
        chosen = ready[0]
        order.append(by_path[chosen])
        done.add(chosen)
        remaining.remove(chosen)
    return order


def _describe_cycle(reads: Mapping[Path, list[Path]], remaining: list[Path]) -> str:
    """One cycle among the configs that could not be ordered, as ``a.json reads b.json, which reads a.json``."""
    path = remaining[0]
    seen: list[Path] = []
    while path not in seen:
        seen.append(path)
        path = next(read for read in reads[path] if read in remaining)
    cycle = [*seen[seen.index(path):], path]
    return f"{cycle[0].name} reads {cycle[1].name}" + "".join(f", which reads {item.name}" for item in cycle[2:])


# --------------------------------------------------------------------------- building


def with_events(config: Config) -> Config:
    """The config with the ``iceberg`` sink when it names no sinks, so the run is recorded next to its table."""
    if "observability" in config.metadata:
        return config
    return dataclasses.replace(config, metadata={**config.metadata, "observability": {"sinks": ["iceberg"]}})


def _gate(report: Any) -> str:
    """How a quality report ended, in a few words."""
    if report is None or not len(report):
        return "no checks"
    failed = report.failures
    if not failed:
        return f"{len(report)} of {len(report)} checks passed"
    return f"{len(failed)} of {len(report)} checks failed ({', '.join(item.name for item in failed)})"


def check_built_elsewhere(gold: GoldConfig) -> TableOutcome:
    """Check a gold table that is built elsewhere, and write nothing.

    The table has to exist. Its quality checks run on its current snapshot, and a failing check blocks the table when
    the config says ``on_failure`` ``fail``, which is what it does for a run.

    Returns:
        The outcome, ``checked`` or ``blocked``.

    Raises:
        TableNotFound: If the table does not exist.
    """
    from local_data_platform.pipeline.builders import iceberg_from_config
    from local_data_platform.quality import checks_from_config, run_checks

    config = gold.config
    iceberg = iceberg_from_config(config, "target", must_exist=True)
    if not iceberg.exists():
        raise TableNotFound(f"{gold.table} is built elsewhere, and it does not exist yet")
    table = iceberg.table()
    snapshot = table.current_snapshot()
    data = iceberg.get()
    quality = config.quality
    report = run_checks(data, checks_from_config(quality["checks"]), {"source_rows": data.num_rows})
    outcome = TableOutcome(gold, CHECKED, rows=data.num_rows,
                           snapshot_id=snapshot.snapshot_id if snapshot is not None else None, gate=_gate(report))
    if not report.passed and quality["on_failure"] == "fail":
        outcome.status = BLOCKED
        outcome.error = "\n".join(["blocked by failed quality checks on the current snapshot, and nothing was written:",
                                   *[f"  - {item.name}: {item.details}" for item in report.failures]])
    return outcome


def _finish_release(result: BuildResult, scope: Any, context: ReleaseContext,
                    sink_for: Callable[[GoldConfig], Any] | None) -> ReleaseOutcome:
    """Cut the release of a build that did not stop, and say what readers still see when none could be cut."""
    from local_data_platform.gold import release as releases

    known = releases.releases_in(scope)
    current = known[0] if known else None
    stopped = result.stopped_by
    if stopped is not None:
        return releases.ReleaseOutcome(current, cut=False, note=f"{stopped.gold.config.identifier} "
                                       f"{'was blocked' if stopped.status == BLOCKED else 'failed'}")
    try:
        return releases.cut_release(result.tables, context=context, sink_for=sink_for)
    except releases.ReleaseError as error:
        return releases.ReleaseOutcome(current, cut=False, error=str(error))


def build_gold(folder: str | os.PathLike, *, on_table: Callable[[TableOutcome], None] | None = None,
               run: Callable[[GoldConfig], Any] | None = None, release: bool | ReleaseContext = False,
               sink_for: Callable[[GoldConfig], Any] | None = None) -> BuildResult:
    """Build the gold tables of a folder, in order, and check the ones that are built elsewhere.

    Args:
        folder: A folder of dataset configs.
        on_table: Called with the outcome of each table as soon as it is known, so a long build shows progress. The
            tables that the build never reached are reported too, after the one that stopped it.
        run: Runs one gold config and returns its :class:`~local_data_platform.pipeline.PipelineResult`. By default the
            config runs as ``ldp run`` runs it, with the ``iceberg`` sink added when it names no sinks. A workflow
            passes a function that runs it for the window of the workflow run.
        release: Cut a release when every table was built or checked. ``True`` cuts one, and a
            :class:`~local_data_platform.gold.release.ReleaseContext` also says what run cuts it. The gold tables have
            to be in one namespace of one warehouse, which is checked before anything is built.
        sink_for: Returns the event sink for the ``release.cut`` event of a table, see
            :func:`~local_data_platform.gold.release.cut_release`.

    Returns:
        The :class:`BuildResult`. It does not raise for a table that failed or that a gate blocked.

    Raises:
        ConfigError: If a config is not valid, the configs read each other in a cycle, or there is no gold config.
        ReleaseError: If a release is wanted and the gold tables are not in one namespace of one warehouse.
    """
    from local_data_platform.etl import run_config
    from local_data_platform.gold import release as releases

    started = time.perf_counter()
    ordered = order_gold(find_gold(folder))
    root = resolve_path(folder)
    if not ordered:
        raise ConfigError(f"{root} has no gold config. A gold config has metadata.docs.layer set to gold")
    context = None
    scope = None
    if release:
        context = release if isinstance(release, releases.ReleaseContext) else releases.ReleaseContext()
        scope = releases.release_scope(ordered)
    result = BuildResult(folder=root)

    def done(outcome: TableOutcome) -> None:
        result.tables.append(outcome)
        if on_table is not None:
            on_table(outcome)

    for gold in ordered:
        if result.stopped_by is not None:
            done(TableOutcome(gold))
            continue
        try:
            if gold.built_here:
                ran = run(gold) if run is not None else run_config(with_events(gold.config))
                done(TableOutcome(gold, BUILT, rows=ran.rows_written,
                                  snapshot_id=ran.published_snapshot_id, gate=_gate(ran.quality), result=ran))
            else:
                done(check_built_elsewhere(gold))
        except DataQualityError as error:
            report = getattr(error, "report", None)
            done(TableOutcome(gold, BLOCKED, gate=_gate(report) if report is not None and len(report) else
                              "the contract does not hold", error=str(error)))
        except Exception as error:  # noqa: BLE001 - reported with the table, and the build stops
            done(TableOutcome(gold, FAILED, error=f"{type(error).__name__}: {error}"))
    if scope is not None:
        result.release = _finish_release(result, scope, context, sink_for)
    result.duration_s = time.perf_counter() - started
    return result


__all__ = ["BLOCKED", "BUILT", "BuildResult", "CHECKED", "FAILED", "GoldConfig", "NOT_RUN", "TableOutcome",
           "build_gold", "check_built_elsewhere", "find_gold", "order_gold", "with_events"]
