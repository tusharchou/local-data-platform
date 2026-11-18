"""The history of the monitors: where the results of ``ldp monitor`` go, and how to read them back.

Every invocation of ``ldp monitor`` records the results of every config that it evaluated, so that a later one can tell
a table that is still late from one that has just become late, and a table that has recovered. A config that lists an
``iceberg`` sink in ``metadata.observability`` records into ``_ldp.monitor_results`` of that sink's catalog, in one
commit for each catalog and invocation. Any other config records into ``.ldp/monitor.jsonl`` next to the config, one
JSON object for each result. A config whose catalog does not exist yet records into the file too, because monitoring
never creates a catalog, and the history of the table starts there.

The history never holds rows of a table. A result has the names, counts and times behind a verdict, as the boundary
allowlist of ``docs/observability.md`` says. The history is separate from the run events, so ``ldp runs`` shows the same
runs before and after ``ldp monitor``.

This module needs only the standard library when it is imported.
"""

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from local_data_platform.logger import get_logger
from local_data_platform.monitoring.results import MonitorResult

logger = get_logger(__name__)

JSONL_PATH = ".ldp/monitor.jsonl"
"""Where the results go when a config has no ``iceberg`` sink, relative to the folder of the config."""


def iceberg_destination(config: Any) -> tuple[dict[str, Any], str] | None:
    """The catalog and the namespace of the ``iceberg`` sink of a config, or ``None`` when it has none to write to.

    A local catalog that does not exist yet is not a destination, because writing into it would create it.
    """
    from local_data_platform.catalog.provider import catalog_database_file
    from local_data_platform.events import SYSTEM_NAMESPACE, sink_specs

    specs = [spec for spec in sink_specs(config.metadata) if spec["type"] == "iceberg"]
    if not specs:
        return None
    catalog = dict(specs[0]["catalog"])
    database = catalog_database_file(catalog, config.base_dir)
    if database is not None and not database.is_file():
        return None
    return catalog, specs[0].get("namespace") or SYSTEM_NAMESPACE


def record(entries: Iterable[tuple[Any, Iterable[MonitorResult]]]) -> list[str]:
    """Record the results of the configs of one invocation.

    The results of the configs that share a catalog go into ``_ldp.monitor_results`` in one commit, and the results of
    the configs that share a folder go into the same ``.ldp/monitor.jsonl``.

    Args:
        entries: ``(config, results)`` for each config.

    Returns:
        Where the results went, such as ``_ldp.monitor_results`` or the path of a file. Nothing for configs that have no
        results.
    """
    from local_data_platform.events import IcebergSink, append_line

    groups: dict[str, tuple[dict[str, Any], Any, str, list[MonitorResult]]] = {}
    files: dict[Path, list[MonitorResult]] = {}
    for config, results in entries:
        results = list(results)
        if not results:
            continue
        destination = iceberg_destination(config)
        if destination is None:
            files.setdefault(config.resolve(JSONL_PATH), []).extend(results)
            continue
        catalog, namespace = destination
        key = json.dumps([catalog, str(config.base_dir), namespace], sort_keys=True, default=str)
        groups.setdefault(key, (catalog, config.base_dir, namespace, []))[3].extend(results)
    written = []
    for catalog, base_dir, namespace, results in groups.values():
        sink = IcebergSink(catalog, base_dir, namespace=namespace, batch_size=len(results))
        for result in results:
            sink.emit_monitor(result.to_dict())
        sink.flush()
        written.append(sink.monitor_identifier)
    for path, results in files.items():
        for result in results:
            append_line(path, json.dumps(result.to_dict(), sort_keys=True))
        written.append(str(path))
    return written


def read_jsonl(path: str | Path) -> list[MonitorResult]:
    """Read the results that were recorded in a ``.ldp/monitor.jsonl`` file, in file order.

    Blank lines are skipped. A line that is not a result, such as one that a crash cut short, is skipped with a warning.
    A file that does not exist has no results.
    """
    file = Path(path)
    if not file.is_file():
        return []
    results = []
    with open(file, encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                results.append(MonitorResult.from_dict(json.loads(line)))
            except (KeyError, TypeError, ValueError) as error:
                logger.warning("Skipping line %d of %s: not a monitor result (%s)", number, file, error)
    return results


def read_iceberg_results(catalog: Mapping[str, Any], base_dir: Any, namespace: str,
                         pipelines: Iterable[str]) -> list[MonitorResult]:
    """The results of the monitors of some pipelines in ``<namespace>.monitor_results``, in one read of the table.

    Args:
        catalog: The catalog spec of the ``iceberg`` sink.
        base_dir: The folder that a relative path of the catalog is relative to.
        namespace: The system namespace.
        pipelines: The identifiers of the configs whose results are wanted.

    Returns:
        The results of all of them, in the order of the table. Nothing when the table is not there. Nothing is created.
    """
    names = sorted(set(pipelines))
    if not names:
        return []
    from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError
    from pyiceberg.expressions import In

    from local_data_platform.catalog.provider import create_catalog
    from local_data_platform.events import MONITOR_TABLE

    try:
        table = create_catalog(catalog, base_dir=base_dir).load_table(f"{namespace}.{MONITOR_TABLE}")
    except (NoSuchTableError, NoSuchNamespaceError):
        return []
    rows = table.scan(row_filter=In("pipeline", names)).to_arrow().to_pylist()
    return [MonitorResult.from_dict(row) for row in rows]


def _read_iceberg(config: Any) -> list[MonitorResult]:
    destination = iceberg_destination(config)
    if destination is None:
        return []
    catalog, namespace = destination
    return read_iceberg_results(catalog, config.base_dir, namespace, [config.identifier])


def read_results(config: Any, *, monitor: str | None = None) -> list[MonitorResult]:
    """The recorded results of the monitors of one config, oldest first.

    They are read from ``_ldp.monitor_results`` when the config has an ``iceberg`` sink and that table exists, and from
    ``.ldp/monitor.jsonl``, which holds the results of the invocations that had no catalog to record into. Nothing is
    created while reading.

    Args:
        config: A :class:`~local_data_platform.Config`.
        monitor: Only the results of the monitor with this name, such as ``freshness``.
    """
    results = _read_iceberg(config)
    results += [result for result in read_jsonl(config.resolve(JSONL_PATH)) if result.pipeline == config.identifier]
    if monitor is not None:
        results = [result for result in results if result.monitor == monitor]
    return sorted(results, key=lambda result: (result.evaluated_at, result.monitor))


class Direct:
    """The history of one config at a time, read from where it is kept each time that it is asked for.

    The monitors ask a history object for the run events and the monitor results of a config, and this is the one
    that ``ldp monitor`` gives them. :class:`~local_data_platform.monitoring.ledger.Ledger` answers the same two
    questions for many configs with one read of each table.
    """

    def run_events(self, config: Any) -> list[Any]:
        """The events of the pipeline of the config, oldest first, as ``local_data_platform.events.run_events``."""
        from local_data_platform.events import run_events

        return run_events(config)[0]

    def monitor_results(self, config: Any, monitor: str | None = None) -> list[MonitorResult]:
        """The recorded results of the config, oldest first, as :func:`read_results`."""
        return read_results(config, monitor=monitor)


DIRECT = Direct()
"""What the monitors read the history with unless they are given another."""


__all__ = [
    "DIRECT",
    "Direct",
    "JSONL_PATH",
    "iceberg_destination",
    "read_iceberg_results",
    "read_jsonl",
    "read_results",
    "record",
]
