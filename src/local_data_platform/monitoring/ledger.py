"""What the history says about the pipelines of many configs, read once for each catalog and each file.

``ldp monitor`` looks at one config at a time, so each monitor reads the run events and the earlier results of its
config on its own, which :class:`~local_data_platform.monitoring.history.Direct` does. ``ldp status`` looks at every
config of a folder, and ten pipelines that share a catalog must not read ``_ldp.runs`` ten times. A :class:`Ledger` is
built from all of the configs, reads ``_ldp.runs``, ``_ldp.quality_results`` and ``_ldp.monitor_results`` of a catalog
once for the pipelines that use it, reads each events file and each results file once, and hands each config its own
part. It answers the two questions that the monitors ask, ``run_events`` and ``monitor_results``, so it can be given to
:func:`~local_data_platform.monitoring.evaluation.evaluate` in place of ``Direct``.

It only reads. It never creates a catalog, a namespace or a file, and a catalog that does not exist yet has no history.

This module needs only the standard library when it is imported. pyiceberg is imported when a table is read.
"""

import datetime as dt
import json
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from local_data_platform.monitoring.history import JSONL_PATH, iceberg_destination, read_iceberg_results, read_jsonl
from local_data_platform.monitoring.results import MonitorResult


def _key(catalog: Any, base_dir: Any, namespace: str) -> str:
    """What makes two configs share the tables of a catalog: the same spec, folder and namespace."""
    return json.dumps([catalog, str(base_dir), namespace], sort_keys=True, default=str)


class _Tables:
    """The rows of one kind of ``_ldp`` table for each catalog, grouped by pipeline, read once for each catalog."""

    def __init__(self) -> None:
        self.pipelines: dict[str, set[str]] = {}
        self._read: dict[str, tuple[set[str], Any]] = {}

    def register(self, key: str, pipeline: str) -> None:
        self.pipelines.setdefault(key, set()).add(pipeline)

    def grouped(self, key: str, pipeline: str, read: Callable[[list[str]], Any]) -> Any:
        """What ``read`` returns for the pipelines of the catalog, which it is asked for once.

        A pipeline that was not registered when the catalog was read makes it read again with that pipeline added.
        """
        entry = self._read.get(key)
        if entry is None or pipeline not in entry[0]:
            self.register(key, pipeline)
            names = set(self.pipelines[key])
            entry = self._read[key] = (names, read(sorted(names)))
        return entry[1]


class Ledger:
    """The run events, the monitor results and the failed checks of the pipelines of several configs.

    Args:
        configs: The configs that will be asked about. A table is read for all of their pipelines at once.
        since: The start of the window of :meth:`failed_checks`, so that ``_ldp.quality_results`` is read from there.
            Without it the window has no start and the whole table is read for those pipelines.
    """

    def __init__(self, configs: Iterable[Any], *, since: dt.datetime | None = None):
        self._since = since
        self._runs, self._quality, self._results = _Tables(), _Tables(), _Tables()
        self._event_files: dict[Path, list[Any]] = {}
        self._result_files: dict[Path, list[MonitorResult]] = {}
        from local_data_platform.events import run_source

        for config in configs:
            try:  # a config whose sinks are invalid fails where it is evaluated, and not for all the others
                source = run_source(config)
                if source.has_catalog(config.base_dir):
                    key = _key(source.catalog, config.base_dir, source.namespace)
                    self._runs.register(key, config.identifier)
                    self._quality.register(key, config.identifier)
                destination = iceberg_destination(config)
                if destination is not None:
                    self._results.register(_key(destination[0], config.base_dir, destination[1]), config.identifier)
            except Exception:  # noqa: BLE001 - see above
                continue

    # ------------------------------------------------------------------ run events

    def _catalog_events(self, config: Any, source: Any) -> dict[str, list[Any]]:
        def read(names: list[str]) -> dict[str, list[Any]]:
            from local_data_platform.catalog.provider import create_catalog
            from local_data_platform.events import read_iceberg_events

            catalog = create_catalog(source.catalog, base_dir=config.base_dir)
            grouped: dict[str, list[Any]] = {}
            for event in read_iceberg_events(catalog, namespace=source.namespace, pipelines=names):
                grouped.setdefault(event.payload.get("pipeline"), []).append(event)
            return grouped

        return self._runs.grouped(_key(source.catalog, config.base_dir, source.namespace), config.identifier, read)

    def _file_events(self, file: Path) -> list[Any]:
        if file not in self._event_files:
            from local_data_platform.events import read_jsonl as read_events

            self._event_files[file] = read_events(file) if file.is_file() else []
        return self._event_files[file]

    def run_events(self, config: Any) -> list[Any]:
        """The events of the pipeline of the config, oldest first, from where its sinks and its catalog keep them.

        It is the answer of ``local_data_platform.events.run_events`` for the config, from the one read of the table.
        """
        from local_data_platform.events import run_source

        source = run_source(config)
        if source.has_catalog(config.base_dir):
            events = self._catalog_events(config, source).get(config.identifier, [])
            if events or source.iceberg or not source.jsonl_listed:
                return list(events)
        return [event for event in self._file_events(source.jsonl)
                if event.payload.get("pipeline") in (None, config.identifier)]

    # ------------------------------------------------------------------ monitor results

    def monitor_results(self, config: Any, monitor: str | None = None) -> list[MonitorResult]:
        """The recorded results of the config, oldest first, as ``read_results`` returns them, from one read."""
        results: list[MonitorResult] = []
        destination = iceberg_destination(config)
        if destination is not None:
            catalog, namespace = destination

            def read(names: list[str]) -> dict[str, list[MonitorResult]]:
                grouped: dict[str, list[MonitorResult]] = {}
                for result in read_iceberg_results(catalog, config.base_dir, namespace, names):
                    grouped.setdefault(result.pipeline, []).append(result)
                return grouped

            results += self._results.grouped(_key(catalog, config.base_dir, namespace), config.identifier,
                                             read).get(config.identifier, [])
        file = config.resolve(JSONL_PATH)
        if file not in self._result_files:
            self._result_files[file] = read_jsonl(file)
        results += [result for result in self._result_files[file] if result.pipeline == config.identifier]
        if monitor is not None:
            results = [result for result in results if result.monitor == monitor]
        return sorted(results, key=lambda result: (result.evaluated_at, result.monitor))

    # ------------------------------------------------------------------ quality results

    def _quality_rows(self, config: Any, source: Any) -> dict[str, list[dict[str, Any]]] | None:
        def read(names: list[str]) -> dict[str, list[dict[str, Any]]] | None:
            from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError
            from pyiceberg.expressions import And, GreaterThanOrEqual, In

            from local_data_platform.catalog.provider import create_catalog
            from local_data_platform.events import QUALITY_TABLE

            catalog = create_catalog(source.catalog, base_dir=config.base_dir)
            try:
                table = catalog.load_table(f"{source.namespace}.{QUALITY_TABLE}")
            except (NoSuchTableError, NoSuchNamespaceError):
                return None
            row_filter: Any = In("pipeline", names)
            if self._since is not None:
                row_filter = And(row_filter, GreaterThanOrEqual("ts", self._since.isoformat()))
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in table.scan(row_filter=row_filter).to_arrow().to_pylist():
                grouped.setdefault(row.get("pipeline"), []).append(row)
            return grouped

        return self._quality.grouped(_key(source.catalog, config.base_dir, source.namespace), config.identifier, read)

    def failed_checks(self, config: Any, until: dt.datetime) -> int | None:
        """How many check results of the pipeline failed from the start of the window up to ``until``.

        They are counted in ``_ldp.quality_results`` when the catalog has that table, and in the
        ``quality.evaluated`` events of the pipeline otherwise, so a config that keeps its events in a file has them
        too. What came after ``until`` is left out.

        Returns:
            The number, which is 0 for a pipeline whose checks all passed in the window, and ``None`` when no check
            result of the pipeline exists at ``until`` or before, because no check ever ran.
        """
        from local_data_platform.events import run_source

        source = run_source(config)

        def inside(moment: dt.datetime) -> bool:
            return (self._since is None or moment >= self._since) and moment <= until

        evaluated = [event for event in self.run_events(config)
                     if event.type == "quality.evaluated" and event.ts <= until and event.payload.get("results")]
        rows = self._quality_rows(config, source) if source.has_catalog(config.base_dir) else None
        mine = [row for row in (rows or {}).get(config.identifier, []) if row["ts"] <= until]
        if rows is not None and (mine or evaluated):
            return sum(1 for row in mine if not row.get("passed") and inside(row["ts"]))
        if not evaluated:
            return None
        return sum(1 for event in evaluated if inside(event.ts)
                   for result in event.payload["results"] if not result.get("passed"))


__all__ = [
    "Ledger",
]
