"""DuckDB SQL engine over Iceberg tables and Arrow data.

``duckdb`` is an optional dependency. It is imported when a :class:`DuckDBEngine`
is created, never when this module is imported, so the rest of the library works
without it.

Iceberg tables are registered one of two ways:

* **Native** (the default when it works): a temporary view over DuckDB's ``iceberg``
  extension, ``iceberg_scan('<metadata_location>', snapshot_from_id => <id>)``. Nothing is
  read when the table is registered; each query streams the data files, and DuckDB pushes
  filters and projections into the scan. The extension is loaded from DuckDB's extension
  folder (``~/.duckdb``) and is never downloaded unless ``install_extensions=True``.
* **In memory** (the 0.1.1 path): pyiceberg scans the table into a ``pyarrow.Table`` that the
  view reads. Used when the extension can't load, when a row filter can't be rendered as
  DuckDB SQL, or when ``native=False``. The auto path logs a warning when it falls back.

Both paths give the same rows for the same snapshot and row filter.

Example:
    >>> with DuckDBEngine() as engine:                        # doctest: +SKIP
    ...     engine.register_iceberg(rides, "rides", row_filter="fare > 0")
    ...     engine.query("SELECT city, count(*) AS n FROM rides GROUP BY city")
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from local_data_platform.engine import Engine, to_pyiceberg_table
from local_data_platform.engine.duckdb.row_filter import (
    SCAN_RELATION, UntranslatableFilter, evolved_field_ids, quote_identifier, quote_string, row_filter_to_sql)
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

INSTALL_HINT = 'pip install "local-data-platform[duckdb]"'
ICEBERG_EXTENSION_HINT = ("Install DuckDB's iceberg extension once with duckdb.connect().execute('INSTALL iceberg'), "
                          "or pass DuckDBEngine(install_extensions=True)")

_UNCHECKED = object()


def _import_duckdb():
    """Import ``duckdb`` or raise :class:`EngineNotFound` with an install hint."""
    try:
        import duckdb
    except ImportError as error:
        raise EngineNotFound(f"The DuckDB engine needs the duckdb package. Install it with: {INSTALL_HINT}") from error
    return duckdb


def _to_pyiceberg_table(table: Any):
    """Return the pyiceberg ``Table`` behind ``table`` (see :func:`~local_data_platform.engine.to_pyiceberg_table`)."""
    return to_pyiceberg_table(table, "register_iceberg()")


def _check_alias(alias: Any) -> str:
    if not isinstance(alias, str) or not alias.strip():
        raise ValueError(f"alias must be a non-empty string, got {alias!r}")
    return alias


def _option_sql(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return quote_string(str(value))


@dataclass
class _Registration:
    """What the engine registered under an alias."""

    native: bool
    table: Any = None  # the pyiceberg Table, for Iceberg registrations
    metadata_location: str | None = None
    snapshot_id: int | None = None


class DuckDBEngine(Engine):
    """Run SQL with DuckDB over Iceberg tables and Arrow tables.

    Tables are registered as temporary views under an alias and then queried with
    :meth:`query`. See the module docstring for the native (``iceberg_scan``) and
    in-memory paths :meth:`register_iceberg` chooses between.

    Args:
        connection: An open ``duckdb.DuckDBPyConnection`` to use. When omitted the
            engine opens an in-memory database and closes it in :meth:`close`. A
            connection you pass in stays open after :meth:`close`; only the views,
            attached catalogs and secrets this engine created on it are removed.
        install_extensions: Download DuckDB's ``iceberg`` extension if it is not
            installed. Off by default, so the engine never goes online by itself;
            without the extension, Iceberg tables are registered in memory.

    Raises:
        EngineNotFound: If ``duckdb`` is not installed.
    """

    def __init__(self, connection: Any = None, *, install_extensions: bool = False):
        super().__init__("duckdb")
        if connection is None:
            duckdb = _import_duckdb()
            self._connection = duckdb.connect(database=":memory:")
            self._owns_connection = True
        else:
            self._connection = connection
            self._owns_connection = False
        self._install_extensions = bool(install_extensions)
        self._registrations: dict[str, _Registration] = {}
        self._attached: dict[str, str | None] = {}  # catalog name -> secret name
        self._iceberg_problem: Any = _UNCHECKED
        self._warned_fallback = False
        self._closed = False

    @property
    def connection(self) -> Any:
        """The underlying ``duckdb.DuckDBPyConnection``."""
        self._check_open()
        return self._connection

    @property
    def aliases(self) -> list[str]:
        """Aliases registered through this engine, in registration order."""
        return list(self._registrations)

    @property
    def iceberg_extension(self) -> bool:
        """Whether DuckDB's ``iceberg`` extension is loaded. The first access tries to load it."""
        self._check_open()
        return self._iceberg_unavailable() is None

    def is_native(self, alias: str) -> bool:
        """Whether ``alias`` is a native ``iceberg_scan`` view (``False`` for in-memory data).

        Raises:
            KeyError: If this engine registered nothing under ``alias``.
        """
        return self._registration(alias).native

    # ------------------------------------------------------------------ registering

    def register_iceberg(self, table: Any, alias: str, snapshot_id: int | None = None,
                         row_filter: Any = None, native: bool | None = None) -> DuckDBEngine:
        """Register an Iceberg table as a view.

        Args:
            table: An :class:`~local_data_platform.format.iceberg.Iceberg` format
                object (anything whose ``table()`` returns a pyiceberg ``Table``)
                or a pyiceberg ``Table``.
            alias: View name to query the table by. Re-using an alias replaces the
                earlier view.
            snapshot_id: Snapshot to read (time travel). Defaults to the table's
                current snapshot when it is registered; later commits are not seen.
            row_filter: A pyiceberg row filter, as a string such as
                ``"fare > 10 AND city = 'NYC'"`` or a ``BooleanExpression``. On the
                native path it becomes the view's ``WHERE`` clause (see
                :mod:`~local_data_platform.engine.duckdb.row_filter` for what is
                supported); in memory, pyiceberg applies it while scanning.
            native: ``None`` (auto) uses ``iceberg_scan`` when the extension loads and
                falls back to the in-memory path with a warning. ``True`` requires the
                native path; ``False`` forces the in-memory path.

        Returns:
            This engine, so calls can be chained.

        Raises:
            TypeError: If ``table`` is not an Iceberg table, or ``native`` is not a bool or None.
            ValueError: If ``alias`` is empty, ``snapshot_id`` is not a snapshot of the table, or
                ``row_filter`` names an unknown column. With ``native=True``, also
                :class:`~local_data_platform.engine.duckdb.row_filter.UntranslatableFilter`
                when the filter can't be rendered as DuckDB SQL.
            EngineNotFound: With ``native=True``, if the iceberg extension can't be loaded.
        """
        self._check_open()
        alias = _check_alias(alias)
        if native is not None and not isinstance(native, bool):
            raise TypeError(f"native must be True, False or None, got {native!r}")
        iceberg_table = _to_pyiceberg_table(table)
        name = ".".join(iceberg_table.name())
        if snapshot_id is not None and iceberg_table.snapshot_by_id(snapshot_id) is None:
            known = [snapshot.snapshot_id for snapshot in iceberg_table.snapshots()]
            raise ValueError(f"Snapshot {snapshot_id} not found in Iceberg table {name}; known snapshots: {known}")

        if native is not False:
            problem = self._iceberg_unavailable()
            if problem is None:
                if self._register_native(iceberg_table, name, alias, snapshot_id, row_filter, required=bool(native)):
                    return self
            elif native:
                raise EngineNotFound(f"register_iceberg(native=True) needs DuckDB's iceberg extension: {problem}. "
                                     f"{ICEBERG_EXTENSION_HINT}")
            elif not self._warned_fallback:
                self._warned_fallback = True
                logger.warning("Registering Iceberg tables in memory, because %s. %s", problem, ICEBERG_EXTENSION_HINT)
        self._register_in_memory(iceberg_table, name, alias, snapshot_id, row_filter)
        return self

    def register_arrow(self, df: pa.Table | pa.RecordBatch, alias: str) -> DuckDBEngine:
        """Register an Arrow table as a view.

        Args:
            df: The data, as a ``pyarrow.Table`` or ``pyarrow.RecordBatch``.
            alias: View name to query the data by. Re-using an alias replaces the
                earlier view.

        Returns:
            This engine, so calls can be chained.

        Raises:
            TypeError: If ``df`` is not Arrow data.
            ValueError: If ``df`` is ``None`` or ``alias`` is empty.
        """
        self._check_open()
        alias = _check_alias(alias)
        if df is None:
            raise ValueError("register_arrow() got None instead of a pyarrow.Table")
        if isinstance(df, pa.RecordBatch):
            df = pa.Table.from_batches([df])
        if not isinstance(df, pa.Table):
            raise TypeError(f"register_arrow() expects a pyarrow.Table or RecordBatch, got {type(df).__name__}")
        self._register_data(alias, df, _Registration(native=False))
        logger.info("Registered Arrow table as %r: %d rows", alias, df.num_rows)
        return self

    def attach_rest(self, name: str, uri: str, warehouse: str, token_env: str | None = None, *,
                    options: Mapping[str, Any] | None = None) -> DuckDBEngine:
        """Attach an Iceberg REST catalog, so its tables can be queried as ``name.namespace.table``.

        Runs ``ATTACH '<warehouse>' AS <name> (TYPE iceberg, ENDPOINT '<uri>', ...)``. With
        ``token_env``, the bearer token is read from that environment variable into a temporary
        (in-memory) DuckDB secret; it is never logged. Without it the catalog is attached with
        ``AUTHORIZATION_TYPE 'none'``. DuckDB talks to the catalog through its ``httpfs``
        extension, which it loads (and, if its autoinstall setting allows, downloads) on attach.

        Args:
            name: Catalog name to attach as.
            uri: The REST catalog endpoint, for example ``http://localhost:8181``.
            warehouse: The warehouse the catalog serves (a name or location, per the catalog).
            token_env: Name of an environment variable holding a bearer token.
            options: Extra ``ATTACH`` options, for example ``{"ACCESS_DELEGATION_MODE": "none"}``.

        Returns:
            This engine, so calls can be chained.

        Raises:
            ConfigError: If ``token_env`` is set but the variable is empty, or an option name is invalid.
            EngineNotFound: If the iceberg extension can't be loaded.
            duckdb.Error: If DuckDB can't attach the catalog.
        """
        self._check_open()
        name = _check_alias(name)
        problem = self._iceberg_unavailable()
        if problem is not None:
            raise EngineNotFound(f"attach_rest() needs DuckDB's iceberg extension: {problem}. {ICEBERG_EXTENSION_HINT}")
        attach_options = ["TYPE iceberg", f"ENDPOINT {quote_string(uri)}"]
        secret = None
        if token_env:
            token = os.environ.get(token_env)
            if not token:
                raise ConfigError(f"attach_rest(): environment variable {token_env} holds no token")
            secret = f"ldp_rest_{name}"
            self._connection.execute(f"CREATE OR REPLACE TEMPORARY SECRET {quote_identifier(secret)} "
                                     f"(TYPE iceberg, TOKEN {quote_string(token)})")
            attach_options.append(f"SECRET {quote_identifier(secret)}")
        else:
            attach_options.append("AUTHORIZATION_TYPE 'none'")
        for key, value in (options or {}).items():
            if not isinstance(key, str) or not key.replace("_", "").isalnum():
                raise ConfigError(f"attach_rest(): invalid ATTACH option name {key!r}")
            attach_options.append(f"{key.upper()} {_option_sql(value)}")
        try:
            self._connection.execute(f"ATTACH {quote_string(warehouse)} AS {quote_identifier(name)} "
                                     f"({', '.join(attach_options)})")
        except Exception:
            if secret:
                self._drop_secret(secret)
            raise
        self._attached[name] = secret
        logger.info("Attached Iceberg REST catalog %r at %s (warehouse %s, %s)", name, uri, warehouse,
                    f"token from ${token_env}" if token_env else "no auth")
        return self

    # ------------------------------------------------------------------ metadata

    def snapshots(self, alias: str) -> pa.Table:
        """The snapshots of the Iceberg table registered as ``alias``, oldest first.

        Native registrations read DuckDB's ``iceberg_snapshots()``; in-memory ones build the same
        columns from pyiceberg metadata: ``sequence_number``, ``snapshot_id``, ``timestamp_ms``
        (a timestamp), ``manifest_list`` and ``operation``. Snapshots committed after the table was
        registered are not listed.

        Raises:
            KeyError: If ``alias`` is not registered.
            ValueError: If ``alias`` is not an Iceberg table.
        """
        registration = self._iceberg_registration(alias)
        if registration.native:
            return self.query(f"SELECT * FROM iceberg_snapshots({quote_string(registration.metadata_location)}) "
                              "ORDER BY sequence_number, timestamp_ms")
        snapshots = sorted(registration.table.snapshots(), key=lambda s: (s.sequence_number or 0, s.timestamp_ms))
        return pa.table({
            "sequence_number": pa.array([s.sequence_number for s in snapshots], pa.uint64()),
            "snapshot_id": pa.array([s.snapshot_id for s in snapshots], pa.uint64()),
            "timestamp_ms": pa.array([s.timestamp_ms * 1000 for s in snapshots], pa.timestamp("us")),
            "manifest_list": pa.array([s.manifest_list for s in snapshots], pa.string()),
            "operation": pa.array([s.summary.operation.value if s.summary else None for s in snapshots], pa.string()),
        })

    def files(self, alias: str) -> pa.Table:
        """The manifest entries (data and delete files) of the registered snapshot of ``alias``.

        Native registrations read DuckDB's ``iceberg_metadata()``; in-memory ones build the same
        columns from pyiceberg manifests: ``manifest_path``, ``manifest_sequence_number``,
        ``manifest_content``, ``status`` (``ADDED``/``EXISTING``/``DELETED``, including deleted
        entries, as DuckDB lists them), ``content``, ``file_path``, ``file_format`` and
        ``record_count``. DuckDB 1.5's extension reports ``content`` with the entry-status names
        (``EXISTING`` for a data file); the in-memory path reports ``DATA``, ``POSITION_DELETES`` or
        ``EQUALITY_DELETES``.

        Raises:
            KeyError: If ``alias`` is not registered.
            ValueError: If ``alias`` is not an Iceberg table.
        """
        registration = self._iceberg_registration(alias)
        if registration.native:
            return self.query(f"SELECT * FROM iceberg_metadata({self._scan_arguments(registration)})")
        table = registration.table
        snapshot = (table.snapshot_by_id(registration.snapshot_id) if registration.snapshot_id is not None
                    else table.current_snapshot())
        rows: dict[str, list[Any]] = {key: [] for key in (
            "manifest_path", "manifest_sequence_number", "manifest_content", "status", "content", "file_path",
            "file_format", "record_count")}
        for manifest in (snapshot.manifests(table.io) if snapshot is not None else []):
            for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=False):
                data_file = entry.data_file
                for key, value in (("manifest_path", manifest.manifest_path),
                                   ("manifest_sequence_number", manifest.sequence_number),
                                   ("manifest_content", manifest.content.name), ("status", entry.status.name),
                                   ("content", data_file.content.name), ("file_path", data_file.file_path),
                                   ("file_format", str(data_file.file_format.value).upper()),
                                   ("record_count", data_file.record_count)):
                    rows[key].append(value)
        types = {"manifest_sequence_number": pa.int64(), "record_count": pa.int64()}
        return pa.table({key: pa.array(values, types.get(key, pa.string())) for key, values in rows.items()})

    # ------------------------------------------------------------------ SQL

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> pa.Table:
        """Run SQL and return the result.

        Args:
            sql: The SQL statement.
            params: Optional values for ``?`` or ``$name`` placeholders in ``sql``.
                Prefer them to formatting values into the SQL string.

        Returns:
            The result as a ``pyarrow.Table``. Statements without a result set,
            such as DDL, return DuckDB's ``Count`` table.
        """
        self._check_open()
        logger.debug("Running DuckDB query: %s", sql)
        result = self._connection.execute(sql, params)
        # duckdb >= 1.4 names it to_arrow_table() and deprecates fetch_arrow_table().
        fetch = getattr(result, "to_arrow_table", None) or result.fetch_arrow_table
        return fetch()

    def get(self, sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> pa.Table:
        """Alias of :meth:`query`, so the engine fits the ``Base.get`` interface."""
        return self.query(sql, params)

    def put(self, df: pa.Table | pa.RecordBatch, alias: str) -> DuckDBEngine:
        """Alias of :meth:`register_arrow`, so the engine fits the ``Base.put`` interface."""
        return self.register_arrow(df, alias)

    def close(self) -> None:
        """Release the engine. Safe to call more than once.

        An engine-owned connection is closed. A connection passed to the
        constructor stays open, and the views, attached catalogs and secrets
        this engine created on it are removed.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()
        else:
            for alias, registration in self._registrations.items():
                if registration.native:
                    self._drop_view(alias)
                else:
                    self._connection.unregister(alias)
            for name, secret in self._attached.items():
                self._connection.execute(f"DETACH DATABASE IF EXISTS {quote_identifier(name)}")
                if secret:
                    self._drop_secret(secret)
        self._registrations.clear()
        self._attached.clear()

    def __enter__(self) -> DuckDBEngine:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"DuckDBEngine({state}, aliases={self.aliases!r})"

    # ------------------------------------------------------------------ internals

    def _register_native(self, iceberg_table: Any, name: str, alias: str, snapshot_id: int | None,
                         row_filter: Any, required: bool) -> bool:
        """Create the ``iceberg_scan`` view. Returns ``False`` (after a warning) to fall back to memory."""
        pinned = snapshot_id
        if pinned is None and iceberg_table.current_snapshot() is not None:
            pinned = iceberg_table.current_snapshot().snapshot_id
        registration = _Registration(native=True, table=iceberg_table,
                                     metadata_location=iceberg_table.metadata_location, snapshot_id=pinned)
        sql = f"SELECT * FROM iceberg_scan({self._scan_arguments(registration)}) AS {quote_identifier(SCAN_RELATION)}"
        if row_filter is not None:
            scan = iceberg_table.scan(snapshot_id=snapshot_id) if snapshot_id is not None else iceberg_table.scan()
            try:
                # Bound to the current schema, as pyiceberg binds row filters; named as the snapshot names them.
                where = row_filter_to_sql(row_filter, iceberg_table.schema(), scan_schema=scan.projection(),
                                          evolved=evolved_field_ids(iceberg_table.metadata.schemas))
            except UntranslatableFilter as error:
                if required:
                    raise
                logger.warning("Registering Iceberg table %s as %r in memory: its row filter can't be rendered as "
                               "DuckDB SQL (%s)", name, alias, error)
                return False
            if where is not None:
                sql += f" WHERE {where}"
        duckdb = _import_duckdb()
        try:
            # CREATE VIEW binds the scan, so a metadata file DuckDB can't read fails here, not in a later query.
            self._connection.execute(f"CREATE OR REPLACE TEMP VIEW {quote_identifier(alias)} AS {sql}")
        except duckdb.Error as error:
            if required:
                raise
            logger.warning("Registering Iceberg table %s as %r in memory: DuckDB's iceberg_scan can't read it (%s)",
                           name, alias, str(error).splitlines()[0])
            return False
        self._registrations[alias] = registration
        logger.info("Registered Iceberg table %s as %r with iceberg_scan (snapshot=%s, row_filter=%s)", name, alias,
                    pinned if pinned is not None else "none", row_filter if row_filter is not None else "none")
        return True

    def _register_in_memory(self, iceberg_table: Any, name: str, alias: str, snapshot_id: int | None,
                            row_filter: Any) -> None:
        scan_kwargs: dict[str, Any] = {}
        if snapshot_id is not None:
            scan_kwargs["snapshot_id"] = snapshot_id
        if row_filter is not None:
            scan_kwargs["row_filter"] = row_filter
        data = iceberg_table.scan(**scan_kwargs).to_arrow()
        pinned = snapshot_id
        if pinned is None and iceberg_table.current_snapshot() is not None:
            pinned = iceberg_table.current_snapshot().snapshot_id
        self._register_data(alias, data, _Registration(native=False, table=iceberg_table,
                                                       metadata_location=iceberg_table.metadata_location,
                                                       snapshot_id=pinned))
        logger.info("Registered Iceberg table %s as %r in memory: %d rows (snapshot=%s, row_filter=%s)",
                    name, alias, data.num_rows, snapshot_id if snapshot_id is not None else "current",
                    row_filter if row_filter is not None else "none")

    def _register_data(self, alias: str, data: pa.Table, registration: _Registration) -> None:
        previous = self._registrations.get(alias)
        if previous is not None and previous.native:
            # connection.register() refuses to replace a view created with SQL.
            self._drop_view(alias)
        self._connection.register(alias, data)
        self._registrations[alias] = registration

    @staticmethod
    def _scan_arguments(registration: _Registration) -> str:
        arguments = [quote_string(registration.metadata_location)]
        if registration.snapshot_id is not None:
            arguments.append(f"snapshot_from_id => {int(registration.snapshot_id)}")
        return ", ".join(arguments)

    def _iceberg_unavailable(self) -> str | None:
        """``None`` when the iceberg extension is loaded, else why it isn't. Checked once per engine."""
        if self._iceberg_problem is _UNCHECKED:
            self._iceberg_problem = self._load_iceberg_extension()
        return self._iceberg_problem

    def _load_iceberg_extension(self) -> str | None:
        duckdb = _import_duckdb()
        try:
            row = self._connection.execute(
                "SELECT installed, loaded FROM duckdb_extensions() WHERE extension_name = 'iceberg'").fetchone()
            if row is None:
                return "this DuckDB build has no iceberg extension"
            installed, loaded = row
            if loaded:
                return None
            if not installed:
                if not self._install_extensions:
                    return "DuckDB's iceberg extension is not installed and install_extensions is off"
                logger.info("Installing DuckDB's iceberg extension")
                self._connection.execute("INSTALL iceberg")
            self._connection.execute("LOAD iceberg")
        except duckdb.Error as error:
            return f"DuckDB's iceberg extension could not be loaded ({str(error).splitlines()[0]})"
        return None

    def _registration(self, alias: str) -> _Registration:
        registration = self._registrations.get(alias)
        if registration is None:
            raise KeyError(f"no table is registered as {alias!r}; registered: {self.aliases}")
        return registration

    def _iceberg_registration(self, alias: str) -> _Registration:
        self._check_open()
        registration = self._registration(alias)
        if registration.table is None:
            raise ValueError(f"{alias!r} is Arrow data, not an Iceberg table")
        return registration

    def _drop_view(self, alias: str) -> None:
        # Qualified with the temp catalog, so a persistent view of the same name is never dropped.
        self._connection.execute(f"DROP VIEW IF EXISTS temp.main.{quote_identifier(alias)}")

    def _drop_secret(self, secret: str) -> None:
        self._connection.execute(f"DROP TEMPORARY SECRET IF EXISTS {quote_identifier(secret)}")

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("DuckDBEngine is closed")


__all__ = ["DuckDBEngine", "ICEBERG_EXTENSION_HINT", "INSTALL_HINT", "UntranslatableFilter"]
