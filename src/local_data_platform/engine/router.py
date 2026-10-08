"""Pick an engine for a scan from the bytes it will read.

:func:`estimate_scan_bytes` sums the on-disk sizes of the files a scan would read, straight from
the Iceberg manifests (no data is read), and :func:`choose_engine` maps that size and the kind of
work to an engine:

============  =======================================  =============================================
``how``        Small enough                             Too big for the small engine
============  =======================================  =============================================
``"sql"``      DuckDB, up to ``duckdb_max_bytes``       PySpark; DuckDB if Spark is not installed
``"arrow"``    PyArrow (a pyiceberg scan into memory),  DuckDB up to ``duckdb_max_bytes``, then
               up to ``pyarrow_max_bytes``              PySpark
============  =======================================  =============================================

When the preferred engine isn't available the next one in that order is used, with a warning if
it is over its threshold. The thresholds are compressed bytes on disk; Parquet usually expands
several times in memory, which is why ``pyarrow_max_bytes`` is much smaller than DuckDB's limit
(DuckDB streams and spills to disk).

Example:
    >>> size = estimate_scan_bytes(rides, row_filter="city = 'NYC'")      # doctest: +SKIP
    >>> choose_engine(size)
    <SupportedEngine.DUCKDB: 'DUCKDB'>
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from typing import Any

from local_data_platform import SupportedEngine
from local_data_platform.engine import to_pyiceberg_table
from local_data_platform.exceptions import EngineNotFound
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

#: Default for ``duckdb_max_bytes``: 50 GiB of data files.
DUCKDB_MAX_BYTES = 50 * 2**30
#: Default for ``pyarrow_max_bytes``: 1 GiB of data files, read fully into memory.
PYARROW_MAX_BYTES = 2**30
#: The kinds of work :func:`choose_engine` routes.
HOW = ("sql", "arrow")

_MODULES = {SupportedEngine.DUCKDB: "duckdb", SupportedEngine.PYSPARK: "pyspark"}
_INSTALL = {SupportedEngine.DUCKDB: 'pip install "local-data-platform[duckdb]"',
            SupportedEngine.PYSPARK: 'pip install "local-data-platform[spark]"'}


def estimate_scan_bytes(table: Any, row_filter: Any = None, snapshot_id: int | None = None) -> int:
    """Bytes of the data and delete files a scan would read, from the manifests.

    The scan is planned with pyiceberg, which prunes manifests and files by partition and then by
    column statistics, so the estimate covers exactly the files the scan would open. Each delete
    file is counted once. Planning reads manifests only, never data files.

    Args:
        table: An :class:`~local_data_platform.format.iceberg.Iceberg` format object or a pyiceberg ``Table``.
        row_filter: A pyiceberg row filter (string or ``BooleanExpression``).
        snapshot_id: Snapshot to plan. Defaults to the current snapshot.

    Returns:
        The total ``file_size_in_bytes``; ``0`` for a table without snapshots.

    Raises:
        TypeError: If ``table`` is not an Iceberg table.
        ValueError: If ``snapshot_id`` is not a snapshot of the table.
    """
    iceberg_table = to_pyiceberg_table(table, "estimate_scan_bytes()")
    if snapshot_id is not None and iceberg_table.snapshot_by_id(snapshot_id) is None:
        raise ValueError(f"Snapshot {snapshot_id} not found in Iceberg table {'.'.join(iceberg_table.name())}")
    if snapshot_id is None and iceberg_table.current_snapshot() is None:
        return 0
    scan_kwargs: dict[str, Any] = {}
    if snapshot_id is not None:
        scan_kwargs["snapshot_id"] = snapshot_id
    if row_filter is not None:
        scan_kwargs["row_filter"] = row_filter
    total = 0
    delete_files: dict[str, int] = {}
    for task in iceberg_table.scan(**scan_kwargs).plan_files():
        total += int(task.file.file_size_in_bytes)
        for delete in getattr(task, "delete_files", ()) or ():
            delete_files[delete.file_path] = int(delete.file_size_in_bytes)
    total += sum(delete_files.values())
    logger.debug("Scan of %s (snapshot=%s, row_filter=%s) reads %d bytes", ".".join(iceberg_table.name()),
                 snapshot_id if snapshot_id is not None else "current", row_filter, total)
    return total


def available_engines() -> frozenset[SupportedEngine]:
    """Engines usable in this environment: PyArrow always, DuckDB and PySpark when installed.

    Detection uses ``importlib.util.find_spec``, so nothing is imported.
    """
    found = {SupportedEngine.PYARROW}
    for engine, module in _MODULES.items():
        if importlib.util.find_spec(module) is not None:
            found.add(engine)
    return frozenset(found)


def _as_engines(available: Iterable[SupportedEngine | str] | None) -> frozenset[SupportedEngine]:
    if available is None:
        return available_engines()
    if isinstance(available, (str, SupportedEngine)):
        available = [available]
    engines = set()
    for item in available:
        if isinstance(item, SupportedEngine):
            engines.add(item)
            continue
        try:
            engines.add(SupportedEngine[str(item).strip().upper()])
        except KeyError:
            raise ValueError(f"unknown engine {item!r}; expected one of {[e.name for e in SupportedEngine]}") from None
    return frozenset(engines)


def choose_engine(scan_bytes: int, how: str = "sql", available: Iterable[SupportedEngine | str] | None = None, *,
                  duckdb_max_bytes: int = DUCKDB_MAX_BYTES,
                  pyarrow_max_bytes: int = PYARROW_MAX_BYTES) -> SupportedEngine:
    """Pick the engine for a scan of ``scan_bytes`` bytes.

    Args:
        scan_bytes: Bytes the scan reads, usually from :func:`estimate_scan_bytes`.
        how: ``"sql"`` to run SQL over the table, or ``"arrow"`` to read rows into Python.
        available: Engines to choose from (``SupportedEngine`` members or their names). Defaults
            to :func:`available_engines`. ``BIGQUERY`` is never chosen.
        duckdb_max_bytes: The most DuckDB is given before Spark is preferred.
        pyarrow_max_bytes: The most read straight into memory for ``how="arrow"``.

    Returns:
        The chosen engine.

    Raises:
        ValueError: If ``scan_bytes`` is negative, ``how`` is unknown or ``available`` names an unknown engine.
        EngineNotFound: If no available engine can do ``how`` (SQL needs DuckDB or PySpark).
    """
    if isinstance(scan_bytes, bool) or not isinstance(scan_bytes, int) or scan_bytes < 0:
        raise ValueError(f"scan_bytes must be a non-negative integer, got {scan_bytes!r}")
    how = str(how).strip().lower()
    if how not in HOW:
        raise ValueError(f"how must be one of {HOW}, got {how!r}")
    engines = _as_engines(available)

    # (engine, limit) in order of preference; None means no limit.
    if how == "sql":
        ladder = [(SupportedEngine.DUCKDB, duckdb_max_bytes), (SupportedEngine.PYSPARK, None)]
    else:
        ladder = [(SupportedEngine.PYARROW, pyarrow_max_bytes), (SupportedEngine.DUCKDB, duckdb_max_bytes),
                  (SupportedEngine.PYSPARK, None)]
    candidates = [(engine, limit) for engine, limit in ladder if engine in engines]
    if not candidates:
        hints = "; ".join(f"{engine.name}: {_INSTALL[engine]}" for engine, _ in ladder if engine in _INSTALL)
        raise EngineNotFound(f"no engine for how={how!r} is available (have {sorted(e.name for e in engines)}). "
                             f"Install one of them: {hints}")
    for engine, limit in candidates:
        if limit is None or scan_bytes <= limit:
            logger.debug("Routing a %d-byte %s scan to %s", scan_bytes, how, engine.name)
            return engine
    engine, limit = candidates[-1]
    logger.warning("A %d-byte %s scan is over the %s limit of %d bytes, and no bigger engine is available; "
                   "using %s anyway", scan_bytes, how, engine.name, limit, engine.name)
    return engine


__all__ = ["DUCKDB_MAX_BYTES", "HOW", "PYARROW_MAX_BYTES", "available_engines", "choose_engine",
           "estimate_scan_bytes"]
