"""Apache Iceberg tables in any pyiceberg catalog.

Writes support three modes:

* ``append`` adds the rows.
* ``overwrite`` replaces every current row (or the rows an ``overwrite_filter`` matches), so
  re-running a load is idempotent.
* ``upsert`` updates rows whose ``join_cols`` match and inserts the rest.

There are two write paths:

* **Direct mode**, ``put(df, mode)``: one transaction, so one catalog commit, holds the schema
  union and the data. ``rows_before`` and ``rows_after`` come from snapshot summaries. On a
  ``local`` catalog, ``overwrite`` and ``upsert`` hold an exclusive file lock
  (``<warehouse>/.ldp/locks/<identifier>.lock``), which closes the read-modify-write race between
  processes on one machine. On a shared remote catalog direct mode is a single-writer mode.
* **Staged mode**, ``put(df, mode, commit=CommitContext(...))``: the exactly-once publish protocol
  in :mod:`local_data_platform.format.iceberg.commit`. Re-running a key is a no-op.

The catalog is built by :func:`local_data_platform.catalog.provider.create_catalog` from the
``target.catalog`` block, or passed in ready-made as ``catalog_obj``. Only public pyiceberg APIs
are used; direct mode works across the supported pyiceberg range (``>=0.10,<0.13``) and staged
mode needs pyiceberg 0.10 or newer.
"""

import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pyarrow as pa
from pyiceberg.exceptions import NoSuchTableError, TableAlreadyExistsError
from pyiceberg.expressions import AlwaysTrue
from pyiceberg.table import Table as PyIcebergTable
from pyiceberg.table import Transaction
from pyiceberg.transforms import (
    BucketTransform,
    DayTransform,
    HourTransform,
    IdentityTransform,
    MonthTransform,
    Transform,
    TruncateTransform,
    YearTransform,
)

from local_data_platform.catalog import provider as catalog_provider
from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.format import Format
from local_data_platform.format.iceberg.commit import (
    CommitConflict,
    CommitContext,
    CommitPolicy,
    CommitSearchExhausted,
    StagedWrite,
    WriteResult,
    check_upsert_columns,
    is_sequence_race,
    snapshot_records,
    write_once,
)
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

logger = get_logger(__name__)

WRITE_MODES = ("append", "overwrite", "upsert")
"""The write modes :meth:`Iceberg.put` accepts."""

_SIMPLE_TRANSFORMS = {
    "identity": IdentityTransform,
    "year": YearTransform,
    "month": MonthTransform,
    "day": DayTransform,
    "hour": HourTransform,
}
_PARAMETRISED_TRANSFORM = re.compile(r"(bucket|truncate)\[(\d+)\]")
_PARTITION_KEYS = {"column", "transform", "name"}
_LOCAL_TYPES = {"local", "localiceberg"}
_LOCKED_MODES = ("overwrite", "upsert")
_SEQUENCE_RACE_TRIES = 5


class PartitionItem(NamedTuple):
    """One parsed ``partition_by`` entry."""

    column: str
    transform: Transform
    name: str | None = None


def parse_transform(value: str) -> Transform:
    """Parse a partition transform name into a pyiceberg transform.

    Args:
        value: One of ``identity``, ``year``, ``month``, ``day``, ``hour``,
            ``bucket[N]`` or ``truncate[W]`` (case-insensitive), with N and W positive.

    Returns:
        The pyiceberg transform.

    Raises:
        ConfigError: If the transform is unknown or its parameter is not a positive integer.
    """
    if not isinstance(value, str):
        raise ConfigError(f"partition transform must be a string, got {type(value).__name__}")
    text = value.strip().lower().replace(" ", "")
    if text in _SIMPLE_TRANSFORMS:
        return _SIMPLE_TRANSFORMS[text]()
    match = _PARAMETRISED_TRANSFORM.fullmatch(text)
    if match:
        kind, number = match.group(1), int(match.group(2))
        if number <= 0:
            raise ConfigError(f"partition transform {value!r} needs a positive parameter")
        return BucketTransform(num_buckets=number) if kind == "bucket" else TruncateTransform(width=number)
    raise ConfigError(
        f"unknown partition transform {value!r}; expected identity, year, month, day, hour, bucket[N] or truncate[W]"
    )


def parse_partition_by(items: list[dict[str, Any]] | None) -> list[PartitionItem]:
    """Parse and validate a ``partition_by`` config list.

    Column names are checked against the data when the table is created.

    Args:
        items: A list of ``{"column", "transform", "name"?}`` objects, or ``None``.

    Returns:
        The parsed entries, in order.

    Raises:
        ConfigError: If the list or an entry is malformed or a transform is unknown.
    """
    if items is None:
        return []
    if not isinstance(items, (list, tuple)):
        raise ConfigError(f"partition_by must be a list of objects, got {type(items).__name__}")
    parsed = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"partition_by[{position}] must be an object, got {type(item).__name__}")
        unknown = set(item) - _PARTITION_KEYS
        if unknown:
            raise ConfigError(f"partition_by[{position}] has unknown keys {sorted(unknown)}")
        column, transform, name = item.get("column"), item.get("transform"), item.get("name")
        if not column or not isinstance(column, str):
            raise ConfigError(f"partition_by[{position}] needs a 'column' name")
        if transform is None:
            raise ConfigError(f"partition_by[{position}] needs a 'transform'")
        if name is not None and (not isinstance(name, str) or not name):
            raise ConfigError(f"partition_by[{position}] 'name' must be a non-empty string")
        parsed.append(PartitionItem(column, parse_transform(transform), name))
    return parsed


def _microsecond_type(data_type: pa.DataType) -> pa.DataType:
    """Return ``data_type`` with every timestamp in microseconds (tz-aware ones in UTC)."""
    if pa.types.is_timestamp(data_type):
        return pa.timestamp("us", tz="UTC" if data_type.tz is not None else None)
    if pa.types.is_time64(data_type) and data_type.unit == "ns":
        return pa.time64("us")
    if pa.types.is_struct(data_type):
        return pa.struct([
            data_type.field(i).with_type(_microsecond_type(data_type.field(i).type))
            for i in range(data_type.num_fields)
        ])
    if pa.types.is_map(data_type):
        return pa.map_(
            data_type.key_field.with_type(_microsecond_type(data_type.key_type)),
            data_type.item_field.with_type(_microsecond_type(data_type.item_type)),
            keys_sorted=data_type.keys_sorted,
        )
    if pa.types.is_large_list(data_type):
        return pa.large_list(data_type.value_field.with_type(_microsecond_type(data_type.value_type)))
    if pa.types.is_fixed_size_list(data_type):
        return pa.list_(data_type.value_field.with_type(_microsecond_type(data_type.value_type)), data_type.list_size)
    if pa.types.is_list(data_type):
        return pa.list_(data_type.value_field.with_type(_microsecond_type(data_type.value_type)))
    return data_type


def cast_timestamps_to_us(df: pa.Table) -> pa.Table:
    """Cast every timestamp column to microseconds so Iceberg (format v2) can store it.

    Iceberg v2 stores timestamps in microseconds, and ``timestamptz`` values in
    UTC. This casts nanosecond (and second or millisecond) timestamps to
    microseconds, converts tz-aware timestamps to UTC (the instant is unchanged)
    and casts ``time64[ns]`` to ``time64[us]``, including inside structs, lists
    and maps. Sub-microsecond precision is truncated. Other columns are untouched.

    Args:
        df: The table to cast.

    Returns:
        ``df`` itself when nothing needs casting, otherwise a cast copy.
    """
    schema = pa.schema(
        [field.with_type(_microsecond_type(field.type)) for field in df.schema],
        metadata=df.schema.metadata,
    )
    if schema.equals(df.schema):
        return df
    changed = [f.name for f, new in zip(df.schema, schema) if not f.type.equals(new.type)]
    logger.debug("Casting timestamp columns %s to microseconds", changed)
    return df.cast(schema, safe=False)


def _row_count(table: PyIcebergTable) -> int:
    """Rows in the current snapshot, from its ``total-records`` summary when available, else by scanning."""
    return snapshot_records(table, table.current_snapshot())


def _is_local_spec(spec: dict[str, Any]) -> bool:
    """Whether a catalog spec means the ``local`` catalog (0.1.1's ``{identifier, warehouse_path}``)."""
    kind = str(spec.get("type") or "local").strip().lower().replace("_", "").replace("-", "")
    if kind in _LOCAL_TYPES:
        return True
    # 0.1.1 also accepted "sql" / "sqlite" on a local block; the provider keeps that alias.
    return kind in ("sql", "sqlite") and not spec.get("uri") and bool(spec.get("warehouse_path"))


def _lock_file(handle) -> None:
    """Block until this process holds an exclusive lock on the open file ``handle``."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:  # LK_LOCK gives up after about 10 seconds; keep waiting
                continue
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Iceberg(Format):
    """An Iceberg table in any pyiceberg catalog (a local SQLite-backed one by default).

    Args:
        name: Table name. The table identifier is ``"<namespace>.<name>"``.
        config: Catalog spec, the config's ``target.catalog`` block. The 0.1.1 form
            ``{"identifier": <namespace>, "warehouse_path": <folder>}`` is the ``local`` type;
            ``warehouse_path`` resolves against ``base_dir`` when relative. Other types
            (``sql``, ``rest``, ``glue`` or a registered plugin) are built by
            :func:`local_data_platform.catalog.provider.create_catalog`, with the namespace
            from ``namespace`` (falling back to ``identifier``).
        catalog: Legacy alias for ``config``.
        catalog_obj: A ready pyiceberg catalog to use instead of building one. ``config`` is
            then optional and only supplies the namespace; without it the namespace is the
            catalog's name.
        partition_by: Partition spec applied when the table is created, as a list of
            ``{"column", "transform", "name"?}`` objects. See :func:`parse_transform`.
        write_mode: Default mode for :meth:`put`: ``append``, ``overwrite`` or ``upsert``.
            ``None`` means ``append``.
        join_cols: Key columns for ``upsert``. A single string is accepted.
        base_dir: Folder that relative paths in the catalog spec resolve against.
        schema_evolution: When true, :meth:`put` adds columns that are new in the
            data to an existing table (union by name), in the same commit as the write.
        **options: Kept on ``self.options``. The legacy ``path`` and ``format`` keys
            are accepted and ignored.

    Raises:
        ConfigError: If the catalog spec, write mode, join columns or partition
            spec is invalid.

    Attributes:
        catalog: The pyiceberg catalog (not the spec dict).
        catalog_spec: The catalog spec dict, or ``None`` when only ``catalog_obj`` was given.
        namespace: The catalog namespace.
        identifier: ``"<namespace>.<name>"``.
        path: The warehouse folder for a ``local`` catalog, else ``None``.
        warehouse: The warehouse location as a URI (``file://``, ``s3://``), when known.
    """

    def __init__(
        self,
        name: str,
        config: dict[str, Any] | None = None,
        *,
        catalog: dict[str, Any] | None = None,
        catalog_obj: Any = None,
        partition_by: list[dict[str, Any]] | None = None,
        write_mode: str | None = "append",
        join_cols: str | list[str] | None = None,
        base_dir: str | os.PathLike | None = None,
        schema_evolution: bool = True,
        **options,
    ):
        spec = self._catalog_config(config, catalog, required=catalog_obj is None)
        if not name or not isinstance(name, str):
            raise ConfigError("Iceberg table needs a non-empty 'name'")
        if "." in name:
            raise ConfigError(f"Iceberg table name {name!r} must not contain '.'; set the namespace in the catalog")
        for legacy in ("path", "format"):
            if options.pop(legacy, None) is not None:
                logger.debug("Ignoring legacy %r option for Iceberg table %s", legacy, name)
        super().__init__(name, path=None, format="ICEBERG", base_dir=base_dir, **options)

        self.write_mode = self._check_mode(write_mode or "append")
        self.join_cols = self._check_join_cols(join_cols)
        if self.write_mode == "upsert" and not self.join_cols:
            raise ConfigError(f"write_mode 'upsert' for Iceberg table {name!r} needs join_cols")
        self.partition_by = parse_partition_by(partition_by)
        self.schema_evolution = bool(schema_evolution)
        self.catalog_spec = dict(spec) if spec is not None else None

        local = spec is not None and _is_local_spec(spec)
        if spec is None:
            self.namespace = str(getattr(catalog_obj, "name", "") or "")
            if not self.namespace:
                raise ConfigError("pass a catalog spec with a 'namespace' along with a catalog_obj that has no name")
        elif local:
            self.namespace = str(spec["identifier"])
        else:
            self.namespace = catalog_provider.catalog_namespace(spec)
        self.catalog_identifier = self.namespace
        self.identifier = f"{self.namespace}.{name}"

        if catalog_obj is not None:
            self.catalog = catalog_obj
        elif local:
            # Resolve once here (a legacy leading-slash path warns once), then hand the provider
            # an absolute path.
            warehouse_path = resolve_path(spec["warehouse_path"], base_dir)
            self.catalog = catalog_provider.create_catalog({**spec, "warehouse_path": str(warehouse_path)},
                                                           base_dir=base_dir)
        else:
            self.catalog = catalog_provider.create_catalog(spec, base_dir=base_dir)

        if isinstance(self.catalog, LocalIcebergCatalog):
            self.path = warehouse_path if catalog_obj is None and local else Path(self.catalog.warehouse_path)
        else:
            self.path = None
        properties = getattr(self.catalog, "properties", None) or {}
        self.warehouse = properties.get("warehouse") if isinstance(properties, dict) else None
        self._ensure_namespace()
        logger.debug("Iceberg table %s in warehouse %s", self.identifier, self.path or self.warehouse)

    def __repr__(self) -> str:
        location = str(self.path) if self.path is not None else self.warehouse
        return f"Iceberg(identifier={self.identifier!r}, warehouse={location!r})"

    def _ensure_namespace(self) -> None:
        if isinstance(self.catalog, LocalIcebergCatalog):
            self.catalog.create_namespace_if_not_exists(self.namespace)
            return
        try:
            self.catalog.create_namespace_if_not_exists(self.namespace)
        except Exception as exc:  # noqa: BLE001 - a read-only principal may still load and write tables
            logger.warning("Could not create namespace %s in catalog %s (%s: %s); continuing",
                           self.namespace, getattr(self.catalog, "name", "?"), type(exc).__name__, exc)

    # ------------------------------------------------------------------ config

    @staticmethod
    def _catalog_config(config: dict[str, Any] | None, catalog: dict[str, Any] | None,
                        required: bool = True) -> dict[str, Any] | None:
        if config is not None and catalog is not None and config != catalog:
            raise ConfigError("pass the catalog config as 'config' or the legacy 'catalog', not both")
        catalog_config = config if config is not None else catalog
        if catalog_config is None:
            if not required:
                return None
            raise ConfigError("Iceberg table needs a catalog config {'identifier', 'warehouse_path'}")
        if not isinstance(catalog_config, dict):
            raise ConfigError(f"Iceberg catalog config must be an object, got {type(catalog_config).__name__}")
        if _is_local_spec(catalog_config):
            missing = [key for key in ("identifier", "warehouse_path") if not catalog_config.get(key)]
            if missing:
                raise ConfigError(f"Iceberg catalog config is missing {missing}")
        return catalog_config

    @staticmethod
    def _check_mode(mode: str) -> str:
        normalised = mode.strip().lower() if isinstance(mode, str) else mode
        if normalised not in WRITE_MODES:
            raise ConfigError(f"unknown write mode {mode!r}; expected one of {list(WRITE_MODES)}")
        return normalised

    @staticmethod
    def _check_join_cols(join_cols: str | list[str] | None) -> list[str]:
        if join_cols is None:
            return []
        if isinstance(join_cols, str):
            join_cols = [join_cols]
        if not isinstance(join_cols, (list, tuple)) or not all(isinstance(c, str) and c for c in join_cols):
            raise ConfigError(f"join_cols must be a column name or a list of column names, got {join_cols!r}")
        return list(join_cols)

    # ------------------------------------------------------------------ table

    def exists(self) -> bool:
        """Return whether the table exists in the catalog."""
        return self.catalog.table_exists(self.identifier)

    def table(self) -> PyIcebergTable:
        """Load the pyiceberg table.

        Raises:
            TableNotFound: If the table doesn't exist yet.
        """
        try:
            return self.catalog.load_table(self.identifier)
        except NoSuchTableError as exc:
            location = self.path if self.path is not None else (self.warehouse or getattr(self.catalog, "name", "?"))
            raise TableNotFound(f"Iceberg table {self.identifier} does not exist in {location}") from exc

    def row_count(self) -> int:
        """Rows in the current snapshot, read from snapshot metadata where possible.

        Raises:
            TableNotFound: If the table doesn't exist yet.
        """
        return _row_count(self.table())

    def snapshots(self) -> list[dict[str, Any]]:
        """List the table's snapshots, oldest first.

        Returns:
            One dict per snapshot with ``snapshot_id``, ``parent_id``,
            ``timestamp_ms``, ``operation`` and ``summary`` (a dict of strings).

        Raises:
            TableNotFound: If the table doesn't exist yet.
        """
        result = []
        for snapshot in self.table().snapshots():
            summary = snapshot.summary
            result.append({
                "snapshot_id": snapshot.snapshot_id,
                "parent_id": snapshot.parent_snapshot_id,
                "timestamp_ms": snapshot.timestamp_ms,
                "operation": summary.operation.value if summary is not None else None,
                "summary": dict(summary.additional_properties) if summary is not None else {},
            })
        return result

    # ------------------------------------------------------------------ read

    def get(
        self,
        snapshot_id: int | None = None,
        row_filter: Any = None,
        selected_fields: str | list[str] | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> pa.Table:
        """Read the table, optionally as of an older snapshot.

        Args:
            snapshot_id: Read this snapshot instead of the current one (time travel).
            row_filter: A pyiceberg filter expression or string such as ``"fare > 10"``.
            selected_fields: Columns to return; all columns by default.
            limit: Return at most this many rows.

        Returns:
            The rows as a ``pyarrow.Table``.

        Raises:
            TableNotFound: If the table doesn't exist yet.
            ValueError: If ``snapshot_id`` is not a snapshot of this table.
        """
        table = self.table()
        scan_args: dict[str, Any] = {}
        if snapshot_id is not None:
            if table.snapshot_by_id(snapshot_id) is None:
                raise ValueError(f"snapshot {snapshot_id} not found in Iceberg table {self.identifier}")
            scan_args["snapshot_id"] = snapshot_id
        if row_filter is not None:
            scan_args["row_filter"] = row_filter
        if selected_fields is not None:
            scan_args["selected_fields"] = (
                (selected_fields,) if isinstance(selected_fields, str) else tuple(selected_fields)
            )
        if limit is not None:
            scan_args["limit"] = limit
        df = table.scan(**scan_args).to_arrow()
        logger.info("Read %d rows from Iceberg table %s", df.num_rows, self.identifier)
        return df

    # ------------------------------------------------------------------ write

    def put(
        self,
        df: pa.Table,
        mode: str | None = None,
        *,
        commit: CommitContext | None = None,
        overwrite_filter: Any = None,
        policy: CommitPolicy | None = None,
    ) -> WriteResult:
        """Write ``df`` to the table, creating it on first write.

        Without ``commit`` this is a direct write: one transaction (one catalog commit) holds
        the schema union and the data, and on a ``local`` catalog ``overwrite`` and ``upsert``
        hold the table's exclusive file lock (see :meth:`exclusive_lock`). With ``commit`` the
        write goes through the staged, exactly-once protocol
        (:func:`~local_data_platform.format.iceberg.commit.write_once`): re-running the same
        idempotency key is a no-op that returns ``skipped_duplicate=True``.

        Args:
            df: The rows to write. Timestamps are cast to microseconds first
                (see :func:`cast_timestamps_to_us`).
            mode: ``append``, ``overwrite`` or ``upsert``. Defaults to the
                ``write_mode`` given at construction. ``overwrite`` and ``upsert``
                behave as ``append`` when the table is new or empty.
            commit: Run the staged protocol under this
                :class:`~local_data_platform.format.iceberg.commit.CommitContext`.
            overwrite_filter: For ``overwrite`` only: a pyiceberg expression or string such as
                ``"day = '2024-01-01'"``. Only matching rows are replaced (a window overwrite);
                by default every row is.
            policy: Retry limits for the staged protocol; the
                :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` defaults otherwise.

        Returns:
            A :class:`WriteResult`. ``rows_before`` and ``rows_after`` come from the
            ``total-records`` snapshot summaries.

        Raises:
            ValueError: If ``df`` is ``None`` or empty, or, for ``upsert``, has
                duplicate or null keys.
            ConfigError: If the mode is unknown, ``upsert`` has no ``join_cols``, a
                join column is missing from ``df``, an ``upsert`` into a table with rows lacks
                one of the table's columns, ``overwrite_filter`` is given for another mode, or
                the partition spec doesn't fit the data.
            CommitConflict: Staged mode only: the publish could not win within the policy
                (``retriable``), or this attempt was fenced by a newer one.
        """
        mode = self.write_mode if mode is None else self._check_mode(mode)
        if mode == "upsert" and not self.join_cols:
            raise ConfigError(f"write mode 'upsert' for Iceberg table {self.identifier} needs join_cols")
        if overwrite_filter is not None and mode != "overwrite":
            raise ConfigError(f"overwrite_filter only applies to mode 'overwrite', not {mode!r}")
        df = cast_timestamps_to_us(self._check_input(df))
        if mode == "upsert":
            self._check_upsert_keys(df)

        if commit is not None:
            result = self._put_staged(df, mode, commit, overwrite_filter, policy)
        else:
            lock = self.exclusive_lock() if mode in _LOCKED_MODES else contextlib.nullcontext()
            with lock:
                result = self._put_direct(df, mode, overwrite_filter)
        logger.info(
            "%s %d rows to Iceberg table %s (%d -> %d rows, snapshot %s%s)",
            mode, result.rows_written, self.identifier, result.rows_before, result.rows_after, result.snapshot_id,
            ", duplicate key: nothing written" if result.skipped_duplicate else "",
        )
        return result

    @contextlib.contextmanager
    def exclusive_lock(self) -> Iterator[Path | None]:
        """Hold the table's exclusive lock, ``<warehouse>/.ldp/locks/<identifier>.lock``.

        Only ``local`` catalogs have a lock file (``fcntl`` on POSIX, ``msvcrt`` on Windows); on
        other catalogs this does nothing and yields ``None``. Direct ``overwrite`` and ``upsert``
        take it, so two processes on one machine never read-modify-write the same table at
        once. The lock is released when the block exits or the process dies.

        Yields:
            The lock file path, or ``None`` when the catalog is not local.
        """
        path = self.lock_path()
        if path is None:
            yield None
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+b") as handle:
            _lock_file(handle)
            try:
                yield path
            finally:
                _unlock_file(handle)

    def lock_path(self) -> Path | None:
        """The lock file of a ``local`` catalog's table, or ``None`` for other catalogs."""
        if not isinstance(self.catalog, LocalIcebergCatalog):
            return None
        return Path(self.catalog.warehouse_path) / ".ldp" / "locks" / f"{self.identifier}.lock"

    def _put_staged(self, df: pa.Table, mode: str, commit: CommitContext, overwrite_filter: Any,
                    policy: CommitPolicy | None) -> WriteResult:
        result = write_once(
            lambda: self._load_or_create(df.schema)[0],
            self.catalog,
            df,
            mode,
            commit,
            policy=policy or CommitPolicy(),
            join_cols=list(self.join_cols) or None,
            overwrite_filter=overwrite_filter,
            schema_evolution=self.schema_evolution,
        )
        return _replace(result, table_identifier=self.identifier)

    def _put_direct(self, df: pa.Table, mode: str, overwrite_filter: Any) -> WriteResult:
        for attempt in range(1, _SEQUENCE_RACE_TRIES + 1):
            try:
                return self._put_direct_once(df, mode, overwrite_filter)
            except ValueError as exc:
                # A staged writer committed to its branch meanwhile; nothing was written, so redo.
                if not is_sequence_race(exc) or attempt == _SEQUENCE_RACE_TRIES:
                    raise
                logger.debug("Direct %s to %s raced a concurrent commit (%s); retrying", mode, self.identifier, exc)
        raise AssertionError("unreachable")  # pragma: no cover

    def _put_direct_once(self, df: pa.Table, mode: str, overwrite_filter: Any) -> WriteResult:
        table, created = self._load_or_create(df.schema)
        base = table.current_snapshot()
        rows_before = snapshot_records(table, base)
        evolve = self.schema_evolution and not created
        before = table.schema()
        # On a table with no rows, overwrite and upsert are both a plain append.
        effective = mode if rows_before > 0 else "append"
        if effective == "upsert":
            check_upsert_columns(table, df)
        if effective == "upsert" and not hasattr(Transaction, "upsert"):
            # pyiceberg 0.9: Table.upsert opens its own transaction, so the union commits first.
            if evolve:
                with table.update_schema() as update:
                    update.union_by_name(df.schema)
            upserted = table.upsert(df, join_cols=list(self.join_cols))
            rows_written = upserted.rows_updated + upserted.rows_inserted
        else:
            with table.transaction() as tx:
                if evolve:
                    with tx.update_schema() as update:
                        update.union_by_name(df.schema)
                if effective == "overwrite":
                    tx.overwrite(df, overwrite_filter=AlwaysTrue() if overwrite_filter is None else overwrite_filter)
                    rows_written = df.num_rows
                elif effective == "upsert":
                    upserted = tx.upsert(df, join_cols=list(self.join_cols))
                    rows_written = upserted.rows_updated + upserted.rows_inserted
                else:
                    tx.append(df)
                    rows_written = df.num_rows
        after = table.schema()
        if after.schema_id != before.schema_id:
            added = sorted(set(after.column_names) - set(before.column_names))
            logger.info("Evolved schema of Iceberg table %s; new columns %s", self.identifier, added)

        snapshot = table.current_snapshot()
        if effective == "append" and snapshot is not None and snapshot.parent_snapshot_id is not None:
            # An append adds one snapshot; its parent is the true base even if pyiceberg's retry
            # rebased it onto a concurrent append.
            rows_before = snapshot_records(table, table.metadata.snapshot_by_id(snapshot.parent_snapshot_id))
        return WriteResult(
            table_identifier=self.identifier,
            mode=mode,
            rows_written=rows_written,
            rows_before=rows_before,
            rows_after=snapshot_records(table, snapshot),
            snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
        )

    def _check_upsert_keys(self, df: pa.Table) -> None:
        missing = [column for column in self.join_cols if column not in df.column_names]
        if missing:
            raise ConfigError(f"join_cols {missing} are not columns of the data written to {self.identifier}")
        for column in self.join_cols:
            if df.column(column).null_count:
                raise ValueError(f"join column {column!r} has null values; upsert keys must be non-null")
        distinct_keys = df.select(self.join_cols).group_by(self.join_cols).aggregate([]).num_rows
        if distinct_keys != df.num_rows:
            raise ValueError(
                f"upsert data for {self.identifier} has {df.num_rows - distinct_keys} duplicate rows on "
                f"join_cols {self.join_cols}; keys must be unique"
            )

    def _load_or_create(self, schema: pa.Schema) -> tuple[PyIcebergTable, bool]:
        """Return ``(table, created)``, creating the table with the partition spec if needed."""
        try:
            table = self.catalog.load_table(self.identifier)
        except NoSuchTableError:
            pass
        else:
            self._warn_if_spec_differs(table)
            return table, False
        try:
            table = self._create(schema)
        except TableAlreadyExistsError:
            return self.catalog.load_table(self.identifier), False
        logger.info("Created Iceberg table %s", self.identifier)
        return table, True

    def _create(self, schema: pa.Schema) -> PyIcebergTable:
        if not self.partition_by:
            return self.catalog.create_table(self.identifier, schema=schema)
        transaction = self.catalog.create_table_transaction(self.identifier, schema=schema)
        iceberg_schema = transaction.table_metadata.schema()
        for item in self.partition_by:
            try:
                field = iceberg_schema.find_field(item.column)
            except ValueError as exc:
                raise ConfigError(
                    f"partition column {item.column!r} is not in the data for {self.identifier}; "
                    f"columns are {schema.names}"
                ) from exc
            if not item.transform.can_transform(field.field_type):
                raise ConfigError(f"partition transform {item.transform} cannot apply to column "
                                  f"{item.column!r} of type {field.field_type}")
        with transaction:
            with transaction.update_spec() as spec:
                for item in self.partition_by:
                    try:
                        spec.add_field(item.column, item.transform, item.name)
                    except ValueError as exc:
                        raise ConfigError(f"invalid partition_by for {self.identifier}: {exc}") from exc
        return self.catalog.load_table(self.identifier)

    def _warn_if_spec_differs(self, table: PyIcebergTable) -> None:
        if not self.partition_by:
            return
        schema = table.schema()
        actual = [(schema.find_column_name(f.source_id), str(f.transform)) for f in table.spec().fields]
        wanted = [(item.column, str(item.transform)) for item in self.partition_by]
        if actual != wanted:
            logger.warning(
                "Iceberg table %s already exists with partition spec %s; partition_by %s only applies at creation",
                self.identifier, actual, wanted,
            )


def _replace(result: WriteResult, **changes: Any) -> WriteResult:
    import dataclasses

    return dataclasses.replace(result, **changes)


__all__ = [
    "CommitConflict",
    "CommitContext",
    "CommitPolicy",
    "CommitSearchExhausted",
    "Iceberg",
    "PartitionItem",
    "StagedWrite",
    "WRITE_MODES",
    "WriteResult",
    "cast_timestamps_to_us",
    "parse_partition_by",
    "parse_transform",
]
