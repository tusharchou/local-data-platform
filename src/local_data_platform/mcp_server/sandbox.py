"""A locked-down DuckDB connection that serves Iceberg tables to untrusted SQL.

The sandbox is set up in this order, and every step was checked against DuckDB 1.5.6:

1. Connect in memory with ``autoinstall_known_extensions``, ``autoload_known_extensions``,
   ``allow_community_extensions`` and ``allow_persistent_secrets`` off, ``temp_directory``
   empty (no spill files, and DuckDB then adds no temp folder to the allowed paths) and
   Python replacement scans off (SQL cannot read the server's Python variables).
2. ``LOAD iceberg`` (``INSTALL`` first only when ``install_extensions=True``), then register
   each table as a view in a schema named after its namespace: natively as
   ``iceberg_scan('<metadata file>')`` when the table's files are all local and under its
   location, otherwise as a materialized Arrow table scanned by pyiceberg.
3. ``SET allowed_directories`` to the local table locations only (not the whole warehouse,
   so the SQLite catalog and non-allowlisted tables stay unreadable), then
   ``SET enable_external_access = false``. File functions (``read_csv``, ``read_parquet``,
   ``glob``, ``iceberg_scan``) now fail outside those folders; ``..`` and sibling-prefix
   paths are refused, and ``INSTALL``, ``LOAD``, ``ATTACH`` and secret creation fail.
4. ``SET lock_configuration = true``, so no ``SET`` or ``RESET`` can undo steps 1-3. The
   settings are read back, and the sandbox refuses to start if they did not take.

``allowed_directories`` grants writes as well as reads (``COPY ... TO`` a table folder
succeeds once external access is off), so the statement guard in
:mod:`~local_data_platform.mcp_server.guard` is what keeps ``COPY`` and ``EXPORT`` out.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

from local_data_platform.logger import get_logger

from .discovery import ExposedTable
from .errors import AccessDenied, QueryFailed, QueryTimeout
from .guard import check_sql, import_duckdb

logger = get_logger(__name__)

_CONNECT_CONFIG = {
    "autoinstall_known_extensions": False,
    "autoload_known_extensions": False,
    "allow_community_extensions": False,
    "allow_persistent_secrets": False,
    "temp_directory": "",
    "python_enable_replacements": False,
}

_BATCH_ROWS = 2048


def quote_identifier(name: str) -> str:
    """Quote ``name`` as a DuckDB identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def quote_literal(text: str) -> str:
    """Quote ``text`` as a DuckDB string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def local_path(uri: str | None) -> str | None:
    """Return the local filesystem path of ``uri``, or ``None`` if it is not a local file.

    ``file:///a/b`` and ``file:/a/b`` give ``/a/b``; a plain absolute path is returned
    normalized; anything with another scheme (``s3://``) gives ``None``.
    """
    if not uri:
        return None
    if uri.startswith("file://"):
        path = uri[len("file://"):]
    elif uri.startswith("file:"):
        path = uri[len("file:"):]
    elif "://" in uri:
        return None
    else:
        path = uri
    return os.path.normpath(path) if os.path.isabs(path) else None


@dataclass
class QueryResult:
    """Rows a sandboxed query returned.

    Attributes:
        table: At most ``max_rows`` rows.
        truncated: Whether the query had more rows than ``max_rows``.
        duration_ms: Wall time for the query and fetch.
    """

    table: pa.Table
    truncated: bool
    duration_ms: float


@dataclass
class _Registration:
    table: ExposedTable
    sql_name: str
    access: str = ""
    metadata_location: str | None = None
    location: str | None = None
    arrow_name: str | None = None
    aliases: list[str] = field(default_factory=list)


class DuckDBSandbox:
    """Serve Iceberg tables as read-only DuckDB views behind DuckDB's own file sandbox.

    Args:
        tables: The allowlisted tables to register.
        native: ``None`` uses ``iceberg_scan`` when the DuckDB ``iceberg`` extension loads and
            falls back to Arrow otherwise; ``True`` requires the extension to load; ``False``
            always uses Arrow. A table whose files are not all local and under its location
            is served as Arrow in every mode.
        install_extensions: Run ``INSTALL iceberg`` before loading it (needs network the
            first time). Off by default, so the server never downloads anything.
        memory_limit: Optional DuckDB ``memory_limit``, such as ``"2GB"``.
        threads: Optional DuckDB thread count.

    Raises:
        EngineNotFound: If ``duckdb`` is not installed.
        RuntimeError: If ``native=True`` and the extension cannot load, or the lockdown
            settings do not read back as set.
    """

    def __init__(self, tables: Sequence[ExposedTable], *, native: bool | None = None, install_extensions: bool = False,
                 memory_limit: str | None = None, threads: int | None = None):
        self._duckdb = import_duckdb()
        config = dict(_CONNECT_CONFIG)
        if memory_limit:
            config["memory_limit"] = str(memory_limit)
        if threads:
            config["threads"] = int(threads)
        self._con = self._duckdb.connect(":memory:", config=config)
        self._lock = threading.RLock()
        self._active = False
        self._timed_out = False
        self._closed = False
        self._arrow_counter = 0
        self._registrations: dict[str, _Registration] = {}
        self.native_available = False if native is False else self._load_iceberg(install_extensions, native)
        allowed: list[str] = []
        short_names: dict[str, int] = {}
        for table in tables:
            short_names[table.name] = short_names.get(table.name, 0) + 1
        for table in tables:
            registration = self._register(table)
            if registration is None:
                continue
            if registration.location and self.native_available:
                allowed.append(registration.location.rstrip("/") + "/")
            if short_names[table.name] == 1 and ".".join(table.namespace) != "main":
                alias = quote_identifier(table.name)
                self._con.execute(f"CREATE OR REPLACE VIEW main.{alias} AS SELECT * FROM {registration.sql_name}")
                registration.aliases.append(table.name)
        self.allowed_directories = sorted(set(allowed))
        self._lockdown()

    # ------------------------------------------------------------------ setup

    def _load_iceberg(self, install: bool, native: bool | None) -> bool:
        try:
            if install:
                self._con.execute("INSTALL iceberg")
            self._con.execute("LOAD iceberg")
        except self._duckdb.Error as error:
            if native:
                raise RuntimeError(f"native=True but the DuckDB iceberg extension did not load: {error}. "
                                   "Install it once with install_extensions=True (needs network).") from error
            logger.warning("DuckDB iceberg extension is not available (%s); serving tables as in-memory Arrow. "
                           "Load it once with --install-extensions for streaming scans.", str(error).splitlines()[0])
            return False
        return True

    def _register(self, table: ExposedTable) -> _Registration | None:
        schema = quote_identifier(".".join(table.namespace) or "main")
        sql_name = f"{schema}.{quote_identifier(table.name)}"
        registration = _Registration(table=table, sql_name=sql_name)
        try:
            self._con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            iceberg_table = table.load()
            registration.location = local_path(iceberg_table.location())
            self._bind(registration, iceberg_table)
        except Exception as error:  # noqa: BLE001 - one bad table must not stop the server
            logger.warning("Could not register table %s: %s", table.identifier, error)
            return None
        self._registrations[table.identifier] = registration
        return registration

    def _bind(self, registration: _Registration, iceberg_table: Any) -> None:
        """Point ``registration``'s view at ``iceberg_table``'s current snapshot."""
        metadata_location = iceberg_table.metadata_location
        if self.native_available and self._can_scan_natively(registration, iceberg_table):
            try:
                self._con.execute(f"CREATE OR REPLACE VIEW {registration.sql_name} AS "
                                  f"SELECT * FROM iceberg_scan({quote_literal(metadata_location)})")
                self._con.execute(f"SELECT * FROM {registration.sql_name} LIMIT 0").fetchall()
            except self._duckdb.Error as error:
                logger.warning("iceberg_scan failed for %s (%s); falling back to Arrow",
                               registration.table.identifier, str(error).splitlines()[0])
            else:
                self._drop_arrow(registration)
                registration.access = "native"
                registration.metadata_location = metadata_location
                return
        data = iceberg_table.scan().to_arrow()
        if registration.arrow_name is None:
            self._arrow_counter += 1
            registration.arrow_name = f"__ldp_arrow_{self._arrow_counter}"
        self._con.register(registration.arrow_name, data)
        self._con.execute(f"CREATE OR REPLACE VIEW {registration.sql_name} AS "
                          f"SELECT * FROM {quote_identifier(registration.arrow_name)}")
        registration.access = "arrow"
        registration.metadata_location = metadata_location

    def _drop_arrow(self, registration: _Registration) -> None:
        if registration.arrow_name is not None:
            self._con.unregister(registration.arrow_name)
            registration.arrow_name = None

    def _can_scan_natively(self, registration: _Registration, iceberg_table: Any) -> bool:
        """Whether every file the current snapshot reads is local and under the table location."""
        root = registration.location
        if root is None or local_path(iceberg_table.metadata_location) is None:
            return False
        prefix = root.rstrip("/") + "/"

        def inside(uri: str | None) -> bool:
            path = local_path(uri)
            return path is not None and path.startswith(prefix)

        if not inside(iceberg_table.metadata_location):
            return False
        snapshot = iceberg_table.current_snapshot()
        if snapshot is None:
            return True
        if not inside(snapshot.manifest_list):
            return False
        for task in iceberg_table.scan().plan_files():
            if not inside(task.file.file_path) or not all(inside(d.file_path) for d in task.delete_files):
                return False
        return True

    def _lockdown(self) -> None:
        directories = ", ".join(quote_literal(path) for path in self.allowed_directories)
        self._con.execute(f"SET allowed_directories = [{directories}]")
        self._con.execute("SET enable_external_access = false")
        self._con.execute("SET lock_configuration = true")
        settings = self.settings()
        expected = {"enable_external_access": "false", "lock_configuration": "true",
                    "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
                    "allow_persistent_secrets": "false", "allowed_configs": "[]", "allowed_paths": "[]"}
        wrong = {name: settings.get(name) for name, value in expected.items() if settings.get(name) != value}
        if wrong:
            self._con.close()
            raise RuntimeError(f"DuckDB sandbox settings did not take effect: {wrong}")
        logger.info("DuckDB sandbox locked: %d views, allowed_directories=%s", len(self._registrations),
                    self.allowed_directories)

    # ------------------------------------------------------------------ info

    def settings(self) -> dict[str, str]:
        """The sandbox-relevant DuckDB settings, read back from ``duckdb_settings()``."""
        names = ("enable_external_access", "lock_configuration", "allowed_directories", "allowed_paths",
                 "allowed_configs", "autoinstall_known_extensions", "autoload_known_extensions",
                 "allow_community_extensions", "allow_persistent_secrets", "temp_directory")
        with self._lock:
            rows = self._con.execute(
                "SELECT name, value FROM duckdb_settings() WHERE list_contains(?, name)", [list(names)]).fetchall()
        return {name: value for name, value in rows}

    def access(self, identifier: str) -> str | None:
        """How a table is served: ``"native"`` (``iceberg_scan``), ``"arrow"``, or ``None``."""
        registration = self._registrations.get(identifier)
        return registration.access if registration else None

    def sql_name(self, identifier: str) -> str | None:
        """The quoted name to use for a table in SQL, such as ``"demo"."rides"``."""
        registration = self._registrations.get(identifier)
        return registration.sql_name if registration else None

    def aliases(self, identifier: str) -> list[str]:
        """Unqualified view names that also point at the table (its bare name, when unique)."""
        registration = self._registrations.get(identifier)
        return list(registration.aliases) if registration else []

    @property
    def identifiers(self) -> list[str]:
        """The identifiers of the registered tables."""
        return list(self._registrations)

    # ------------------------------------------------------------------ run

    def refresh(self) -> None:
        """Re-point every view whose table has a new metadata file (new commits since the last call)."""
        with self._lock:
            self._check_open()
            for registration in self._registrations.values():
                try:
                    iceberg_table = registration.table.load()
                    if iceberg_table.metadata_location != registration.metadata_location:
                        self._bind(registration, iceberg_table)
                        logger.debug("Refreshed %s (%s)", registration.table.identifier, registration.access)
                except Exception as error:  # noqa: BLE001 - keep serving the last good snapshot
                    logger.warning("Could not refresh %s: %s", registration.table.identifier, error)

    def query(self, sql: str, *, max_rows: int, timeout_s: float) -> QueryResult:
        """Guard, run and fetch one query.

        Args:
            sql: The agent's SQL; it must pass :func:`~local_data_platform.mcp_server.guard.check_sql`.
            max_rows: Return at most this many rows.
            timeout_s: Interrupt the query after this many seconds.

        Returns:
            A :class:`QueryResult`.

        Raises:
            QueryRejected: If the SQL fails the statement guard.
            AccessDenied: If DuckDB's sandbox refuses a file or extension access.
            QueryTimeout: If the query is interrupted by the timeout.
            QueryFailed: For any other DuckDB error.
        """
        with self._lock:
            self._check_open()
            statement = check_sql(sql, self._con)
            return self._run(statement, max_rows=max_rows, timeout_s=timeout_s)

    def sample(self, identifier: str, n: int, *, timeout_s: float) -> QueryResult:
        """Return the first ``n`` rows of a registered table in scan order."""
        with self._lock:
            self._check_open()
            registration = self._registrations[identifier]
            return self._run(f"SELECT * FROM {registration.sql_name} LIMIT {int(n)}", max_rows=int(n),
                             timeout_s=timeout_s)

    def _run(self, statement: Any, *, max_rows: int, timeout_s: float) -> QueryResult:
        duckdb = self._duckdb
        timer = threading.Timer(timeout_s, self._interrupt)
        timer.daemon = True
        started = time.perf_counter()
        self._timed_out = False
        self._active = True
        timer.start()
        reader = None
        try:
            self._con.execute(statement)
            batch_rows = max(1, min(max_rows + 1, _BATCH_ROWS))
            to_reader = getattr(self._con, "to_arrow_reader", None) or self._con.fetch_record_batch
            reader = to_reader(batch_rows)
            batches, rows = [], 0
            while rows <= max_rows:
                try:
                    batch = reader.read_next_batch()
                except StopIteration:
                    break
                batches.append(batch)
                rows += batch.num_rows
            table = pa.Table.from_batches(batches, schema=reader.schema).slice(0, max_rows)
        except duckdb.InterruptException as error:
            if self._timed_out:
                raise QueryTimeout(f"the query ran longer than the {timeout_s:g}s timeout and was stopped") from None
            raise QueryFailed(_first_line(error)) from None
        except duckdb.PermissionException as error:
            raise AccessDenied(f"blocked by the sandbox: {_first_line(error)}") from None
        except duckdb.Error as error:
            raise QueryFailed(_first_line(error)) from None
        finally:
            self._active = False
            timer.cancel()
            close = getattr(reader, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - closing a partly read stream is best effort
                    pass
        return QueryResult(table=table, truncated=rows > max_rows,
                           duration_ms=round((time.perf_counter() - started) * 1000, 3))

    def _interrupt(self) -> None:
        if self._active:
            self._timed_out = True
            self._con.interrupt()

    def close(self) -> None:
        """Close the DuckDB connection. Safe to call more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._con.close()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("DuckDBSandbox is closed")

    def __enter__(self) -> DuckDBSandbox:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"DuckDBSandbox(tables={self.identifiers!r}, native={self.native_available})"


def _first_line(error: BaseException) -> str:
    text = str(error).strip()
    return text.splitlines()[0] if text else type(error).__name__


__all__ = ["DuckDBSandbox", "QueryResult", "local_path", "quote_identifier", "quote_literal"]
