"""Open the Iceberg tables of dataset configs as one DuckDB database, and read them as of a snapshot or a time.

``open_lake`` is what ``ldp query`` uses for a folder of configs, or for more than one config. It finds every
Iceberg table the configs name, the way ``ldp mcp`` and ``ldp dbt`` do, and registers each one as a view in a
:class:`~local_data_platform.engine.duckdb.DuckDBEngine`, so SQL can join them::

    with open_lake("config") as lake:
        lake.query("SELECT o.id, c.label FROM raw.orders o JOIN raw.customers c ON c.id = o.id")

A table is a view named ``namespace.table``, in the schema ``namespace``, and also a view with the bare table name
when no other table has it. A table that a config names but that does not exist yet is skipped with a warning.

Nothing is written and nothing is downloaded. DuckDB's iceberg extension is loaded when it is installed and
installed only when ``install_extensions`` says so. Without it the tables are read into memory, with a warning, as
:class:`~local_data_platform.engine.duckdb.DuckDBEngine` does. The MCP SDK is not needed, and importing this module
loads neither DuckDB nor pyiceberg.
"""

from __future__ import annotations

import datetime as dt
import os
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.logger import get_logger

if TYPE_CHECKING:
    from local_data_platform.engine.duckdb import DuckDBEngine
    from local_data_platform.mcp_server.discovery import ExposedTable

logger = get_logger(__name__)

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_MOMENT_EXAMPLE = "'2026-07-31 23:59:59' or '2026-07-31T23:59:59+07:00'"


def open_lake(path: str | os.PathLike | Iterable[str | os.PathLike], *, install_extensions: bool = False,
              snapshot_id: int | None = None, as_of: str | dt.datetime | None = None,
              release: str | None = None) -> DuckDBEngine:
    """Register every Iceberg table that some dataset configs name, in one DuckDB engine.

    Args:
        path: A config file, a folder of ``*.json`` configs (not recursive), or several of them.
        install_extensions: Download DuckDB's iceberg extension if it is not installed. Off by default, so
            this never goes online by itself.
        snapshot_id: Read this snapshot instead of the current one. Only for a lake of one table.
        as_of: Read the newest snapshot committed at or before this time instead of the current one: a
            ``datetime``, or ISO 8601 text such as ``"2026-07-31 23:59:59"``. A time with no offset is UTC.
            Only for a lake of one table.
        release: Read the gold tables as a release names them, such as ``"r2"``, see
            :mod:`local_data_platform.gold.release`. A table that the release holds is read at its snapshot there. A
            table that it does not hold, or that is in a namespace with no releases, is read at its current snapshot.

    Returns:
        The engine, open. Close it, or use it in a ``with`` block. Every table is registered as
        ``namespace.table`` and, when its name is unique among the tables, as the bare name too. A namespace of
        several parts, ``a.b``, is one schema named ``a.b``, so SQL quotes it: ``"a.b".table``.

    Raises:
        ConfigError: If a path does not exist or an explicitly named config is invalid, if both
            ``snapshot_id`` and ``as_of`` are given, if either is given and there is more than one table, if
            ``snapshot_id`` is not a snapshot of the table (the message lists the ones that are), or if
            ``as_of`` is not a time or is before the table's first snapshot, or if ``release`` is given with either of
            them.
        ReleaseError: If ``release`` names a release that a namespace of the tables does not have, or none of the
            tables is in a namespace that has releases.
        TableNotFound: If the configs name no table that exists.
        EngineNotFound: If DuckDB is not installed.
    """
    from local_data_platform.engine.duckdb import DuckDBEngine
    from local_data_platform.mcp_server.discovery import discover_tables

    paths = [path] if isinstance(path, (str, os.PathLike)) else list(path)
    if not paths:
        raise ConfigError("open_lake needs a config file or a folder of configs")
    engine = DuckDBEngine(install_extensions=install_extensions)
    try:
        discovery = discover_tables(paths)
        try:
            for item in discovery.unavailable:
                logger.warning("Skipping %s, named in %s: %s", item["table"], item["source"], item["reason"])
            tables = discovery.tables
            if not tables:
                raise _no_tables(paths, discovery.unavailable)
            if release is not None and (snapshot_id is not None or as_of is not None):
                raise ConfigError("pass one of a release, a snapshot or a time, not more than one")
            if (snapshot_id is not None or as_of is not None) and len(tables) > 1:
                names = ", ".join(table.identifier for table in tables[:5]) + (", ..." if len(tables) > 5 else "")
                raise ConfigError(f"a snapshot or a time applies to one table, but {len(tables)} were found "
                                  f"({names}); pass a single config that names one table")
            names = Counter(table.name for table in tables)
            released = _release_snapshots(tables, release) if release is not None else {}
            for table in tables:
                _register(engine, table, names[table.name] == 1, released.get(table.identifier, snapshot_id), as_of)
        finally:
            discovery.close()
    except BaseException:
        engine.close()
        raise
    logger.info("Opened a lake of %d tables from %d paths", len(tables), len(paths))
    return engine


def _release_snapshots(tables: list[ExposedTable], name: str) -> dict[str, int]:
    """The snapshot of each table in release ``name``, for the tables that the release holds.

    A namespace is looked at where its catalog keeps its warehouse. One that has no releases at all, such as a silver
    namespace, has nothing to say, and its tables are read as they are. One that has releases has to have ``name``.
    """
    from local_data_platform.gold.release import ReleaseError, ReleaseScope, release_in, releases_in

    scopes: dict[ReleaseScope, None] = {}
    for table in tables:
        if table.catalog.warehouse is not None:
            scopes[ReleaseScope(table.catalog.warehouse, ".".join(table.namespace))] = None
    snapshots: dict[str, int] = {}
    found = False
    for scope in scopes:
        if releases_in(scope):
            found = True
            snapshots.update(release_in(scope, name).snapshots())
    if not found:
        raise ReleaseError("none of the tables is in a release, because no namespace of them has one. "
                           "ldp gold build cuts a release of the gold tables")
    return snapshots


def _no_tables(paths: list, unavailable: list[dict[str, str]]) -> TableNotFound:
    where = ", ".join(str(path) for path in paths)
    if not unavailable:
        return TableNotFound(f"no Iceberg table found in {where}; a config needs a source or a target with "
                             f"format ICEBERG")
    reasons = "; ".join(f"{item['table']}: {item['reason']}" for item in unavailable[:3])
    more = f" (and {len(unavailable) - 3} more)" if len(unavailable) > 3 else ""
    return TableNotFound(f"no Iceberg table to read in {where}. {reasons}{more}")


def _register(engine: DuckDBEngine, exposed: ExposedTable, unique: bool, snapshot_id: int | None,
              as_of: str | dt.datetime | None) -> None:
    """Register one table as ``namespace.table`` (and as its bare name when ``unique``) in ``engine``."""
    table = exposed.load()
    snapshot = pick_snapshot([(item.snapshot_id, item.timestamp_ms) for item in table.snapshots()],
                             snapshot_id=snapshot_id, as_of=as_of, table=exposed.identifier)
    try:
        engine.register_iceberg(table, exposed.identifier, snapshot_id=snapshot)
    except ValueError as error:  # an unknown snapshot id: the message lists the ones that exist
        raise ConfigError(str(error)) from error
    name_views(engine, exposed.identifier, ".".join(exposed.namespace) or "main", exposed.name, unique)


def name_views(engine: DuckDBEngine, identifier: str, namespace: str, name: str, unique: bool = True) -> None:
    """Make a table that is registered as ``identifier`` work as ``namespace.table`` in SQL, and by its bare name.

    The engine names the view ``namespace.table``, one identifier. Views in a schema make ``namespace.table`` work
    unquoted in SQL, and a view in ``main`` makes the bare name work, which is left out when ``unique`` is false.
    """
    from local_data_platform.engine.duckdb.row_filter import quote_identifier

    schema, source = quote_identifier(namespace), quote_identifier(identifier)
    connection = engine.connection
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    connection.execute(f"CREATE OR REPLACE VIEW {schema}.{quote_identifier(name)} AS SELECT * FROM {source}")
    if unique and namespace != "main":
        connection.execute(f"CREATE OR REPLACE VIEW main.{quote_identifier(name)} AS SELECT * FROM {source}")


def parse_moment(value: str | dt.datetime) -> dt.datetime:
    """A point in time as a timezone-aware UTC ``datetime``.

    Text is ISO 8601: ``"2026-07-31 23:59:59"``, ``"2026-07-31T23:59:59.250"``, ``"2026-07-31T23:59:59+07:00"``
    or ``"2026-07-31"``. A time with no offset is UTC, and a date alone is midnight UTC.

    Raises:
        ConfigError: If the text is not a time.
    """
    try:
        moment = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).strip())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        return moment.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):  # not a time, or one that UTC cannot represent
        raise ConfigError(f"cannot read {str(value).strip()!r} as a time; use ISO 8601, such as "
                          f"{_MOMENT_EXAMPLE}") from None


def snapshot_as_of(snapshots: Iterable[tuple[int, int]], moment: str | dt.datetime, *, table: str) -> int:
    """The id of the newest snapshot committed at or before ``moment``.

    Args:
        snapshots: ``(snapshot_id, timestamp_ms)`` for each snapshot of the table, oldest first. When two were
            committed in the same millisecond the later one in the list wins.
        moment: The time, as :func:`parse_moment` reads it.
        table: The table's name, for the error message.

    Raises:
        ConfigError: If ``moment`` is not a time, the table has no snapshots, or all of them are newer.
    """
    when = parse_moment(moment)
    limit_ms = (when - _EPOCH) // dt.timedelta(milliseconds=1)
    known = list(snapshots)
    if not known:
        raise ConfigError(f"Iceberg table {table} has no snapshots, so there is nothing to read as of "
                          f"{_show(limit_ms)} UTC")
    chosen: tuple[int, int] | None = None
    for snapshot_id, timestamp_ms in known:
        if timestamp_ms <= limit_ms and (chosen is None or timestamp_ms >= chosen[1]):
            chosen = (snapshot_id, timestamp_ms)
    if chosen is None:
        oldest = min(timestamp_ms for _, timestamp_ms in known)
        raise ConfigError(f"Iceberg table {table} has no snapshot committed at or before {_show(limit_ms)} UTC; "
                          f"its oldest snapshot was committed {_show(oldest)} UTC")
    logger.info("Reading %s as of %s UTC: snapshot %d, committed %s UTC", table, _show(limit_ms), chosen[0],
                _show(chosen[1]))
    return chosen[0]


def pick_snapshot(snapshots: Iterable[tuple[int, int]], *, snapshot_id: int | None = None,
                  as_of: str | dt.datetime | None = None, table: str) -> int | None:
    """The snapshot id to read, or ``None`` for the current snapshot.

    That is ``snapshot_id`` as given, or what :func:`snapshot_as_of` finds for ``as_of``.

    Raises:
        ConfigError: If both ``snapshot_id`` and ``as_of`` are given, or :func:`snapshot_as_of` fails.
    """
    if snapshot_id is not None and as_of is not None:
        raise ConfigError("pass either a snapshot id or a time, not both")
    if as_of is not None:
        return snapshot_as_of(snapshots, as_of, table=table)
    return snapshot_id


def _show(timestamp_ms: int) -> str:
    """``timestamp_ms`` as ``YYYY-MM-DD HH:MM:SS.mmm`` in UTC, computed without floating point."""
    moment = _EPOCH + dt.timedelta(milliseconds=timestamp_ms)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


__all__ = ["open_lake", "parse_moment", "pick_snapshot", "snapshot_as_of"]
