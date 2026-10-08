"""Reproducible datasets: pin an Iceberg snapshot, a filter and a column list as a named version.

A training split, an evaluation set or the data behind a report must give the same rows every time
it is read, even while the table keeps changing. :func:`pin` records exactly what to read: the
table, its snapshot id, a row filter and the selected columns, plus the row count and a schema
fingerprint to check against. The record is a :class:`DatasetVersion`, written as a small JSON
manifest under ``<warehouse>/.ldp/datasets/<name>/v<N>.json``::

    from local_data_platform.datasets import export, list_versions, load, pin

    version = pin(episodes, "humanoid_train", row_filter="success = true AND robot_id != 'h08'")
    ...                                       # later appends, overwrites, schema changes
    rows = load(version)                      # the same rows as the day it was pinned
    export(version, "exports/train.parquet")
    list_versions("humanoid_train", warehouse="warehouse")

How it stays reproducible:

* **Snapshots are immutable.** Iceberg never changes the files of a snapshot, so reading snapshot
  ``S`` with filter ``F`` and columns ``C`` always gives the same rows. Time travel reads the
  snapshot's own schema, so later schema changes don't show up either.
* **The snapshot is protected.** By default :func:`pin` also tags the snapshot
  (``ldp_ds_<name>_v<N>``). Snapshot expiry, in pyiceberg and in Iceberg Java, never removes a
  tagged snapshot. Pass ``tag=False`` to skip the tag.
* **Loads are checked.** :func:`load` compares the schema fingerprint and the row count with the
  manifest and raises :class:`DatasetError` if either differs, rather than returning other data.

The manifest also stores a secret-free spec of the catalog and the table's metadata file, so
:func:`load` needs no other arguments. Where the warehouse is not given, the ``LDP_WAREHOUSE``
environment variable names it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pyarrow as pa

from local_data_platform.exceptions import ConfigError, LDPError, TableNotFound
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

logger = get_logger(__name__)

DATASETS_DIR = Path(".ldp") / "datasets"
"""Where manifests live, relative to the warehouse folder."""

MANIFEST_FORMAT = 1
"""The version of the manifest file layout, stored as ``ldp_dataset`` in every manifest."""

WAREHOUSE_ENV = "LDP_WAREHOUSE"
"""Environment variable naming the warehouse when a function is not given one."""

EXPORT_FORMATS = ("parquet", "jsonl")
"""The formats :func:`export` writes."""

TAG_PREFIX = "ldp_ds_"
"""Prefix of the Iceberg tags :func:`pin` creates."""

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_TAG_SAFE_RE = re.compile(r"[A-Za-z0-9_]+")
_SECRET_KEY_RE = re.compile(r"token|secret|password|credential", re.IGNORECASE)
_URI_PASSWORD_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://[^:/@]+):[^@/]*@")
_TAG_ATTEMPTS = 3
_JSONL_BATCH_ROWS = 10_000


class DatasetError(LDPError):
    """Raised when a dataset version can't be found or no longer reads the rows it pinned."""


@dataclass(frozen=True)
class DatasetVersion:
    """One pinned version of a named dataset.

    Attributes:
        name: The dataset name, e.g. ``"humanoid_train"``.
        table_identifier: The Iceberg table, ``"<namespace>.<table>"``.
        snapshot_id: The pinned snapshot.
        row_filter: The pyiceberg row filter string, or ``None`` for every row.
        selected_fields: The pinned columns, or ``None`` for every column.
        row_count: Rows the version reads.
        schema_fingerprint: ``sha256:<hex>`` over the field ids, names, types and nullability of
            the pinned columns in the snapshot's schema.
        created_at: When the version was pinned (UTC).
        version: The version number, from 1, per dataset name.
        catalog: A secret-free catalog spec that :func:`load` reopens the table with (for the
            ``local`` type, ``{"type", "identifier", "warehouse_path"}``).
        metadata_location: The table metadata file current when the version was pinned; the
            fallback :func:`load` reads when the catalog can't be opened.
        tag: The Iceberg tag protecting the snapshot, or ``None``.
        properties: Free-form string labels, e.g. ``{"split": "train"}``.
        manifest_path: The manifest file this version was read from or written to.
    """

    name: str
    table_identifier: str
    snapshot_id: int
    row_filter: str | None
    selected_fields: tuple[str, ...] | None
    row_count: int
    schema_fingerprint: str
    created_at: dt.datetime
    version: int = 0
    catalog: Mapping[str, Any] = field(default_factory=dict, hash=False)
    metadata_location: str | None = None
    tag: str | None = None
    properties: Mapping[str, str] = field(default_factory=dict, hash=False)
    manifest_path: str | None = field(default=None, compare=False, hash=False)

    @property
    def ref(self) -> str:
        """``"<name>@v<version>"``, a short label for logs and messages."""
        return f"{self.name}@v{self.version}"

    def to_dict(self) -> dict[str, Any]:
        """Return the manifest content as JSON-serialisable primitives (without ``manifest_path``)."""
        return {
            "ldp_dataset": MANIFEST_FORMAT,
            "name": self.name,
            "version": self.version,
            "table_identifier": self.table_identifier,
            "snapshot_id": self.snapshot_id,
            "row_filter": self.row_filter,
            "selected_fields": list(self.selected_fields) if self.selected_fields is not None else None,
            "row_count": self.row_count,
            "schema_fingerprint": self.schema_fingerprint,
            "created_at": self.created_at.astimezone(dt.timezone.utc).isoformat(),
            "catalog": dict(self.catalog),
            "metadata_location": self.metadata_location,
            "tag": self.tag,
            "properties": dict(self.properties),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], manifest_path: str | os.PathLike | None = None) -> DatasetVersion:
        """Build a version from a manifest dict. Unknown keys are ignored, for forward compatibility.

        Raises:
            DatasetError: If a required key is missing or has the wrong type.
        """
        try:
            fields = data.get("selected_fields")
            created = dt.datetime.fromisoformat(str(data["created_at"]))
            return cls(
                name=str(data["name"]),
                table_identifier=str(data["table_identifier"]),
                snapshot_id=int(data["snapshot_id"]),
                row_filter=data.get("row_filter"),
                selected_fields=tuple(fields) if fields is not None else None,
                row_count=int(data["row_count"]),
                schema_fingerprint=str(data["schema_fingerprint"]),
                created_at=created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc),
                version=int(data.get("version", 0)),
                catalog=dict(data.get("catalog") or {}),
                metadata_location=data.get("metadata_location"),
                tag=data.get("tag"),
                properties={str(k): str(v) for k, v in (data.get("properties") or {}).items()},
                manifest_path=str(manifest_path) if manifest_path is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            where = f" in {manifest_path}" if manifest_path else ""
            raise DatasetError(f"invalid dataset manifest{where}: {exc!r}") from exc


# --------------------------------------------------------------------------- helpers


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name) or ".." in name:
        raise ConfigError(f"dataset name must be 1-128 letters, digits, '_', '-' or '.', starting with a letter "
                          f"or digit, got {name!r}")
    return name


def datasets_dir(warehouse: Any = None) -> Path:
    """The folder holding the dataset manifests of a warehouse: ``<warehouse>/.ldp/datasets``.

    Args:
        warehouse: The warehouse folder, an :class:`~local_data_platform.format.iceberg.Iceberg`
            table (its local warehouse is used), or ``None`` for ``$LDP_WAREHOUSE``.

    Raises:
        ConfigError: If no warehouse is given and ``LDP_WAREHOUSE`` is not set.
    """
    return _resolve_warehouse(warehouse) / DATASETS_DIR


def _resolve_warehouse(warehouse: Any, table_source: Any = None) -> Path:
    if warehouse is not None and not isinstance(warehouse, (str, os.PathLike)):
        table_source, warehouse = warehouse, None
    if warehouse is not None:
        return resolve_path(warehouse)
    if table_source is not None:
        local = getattr(getattr(table_source, "catalog", None), "warehouse_path", None)
        if local:
            return Path(local)
        path = getattr(table_source, "path", None)
        if isinstance(path, Path):
            return path
    env = os.environ.get(WAREHOUSE_ENV)
    if env:
        return resolve_path(env)
    raise ConfigError(f"no warehouse for the dataset manifests: pass warehouse= or set {WAREHOUSE_ENV}")


def _pyiceberg_table(source: Any) -> Any:
    from pyiceberg.table import Table as PyIcebergTable

    if isinstance(source, PyIcebergTable):
        return source
    getter = getattr(source, "table", None)
    if callable(getter):
        table = getter()
        if isinstance(table, PyIcebergTable):
            return table
    raise TypeError(f"expected an Iceberg table (local_data_platform Iceberg or pyiceberg Table), "
                    f"got {type(source).__name__}")


def _type_signature(field_type: Any) -> Any:
    """A version-independent description of an Iceberg type, with nested field ids."""
    from pyiceberg.types import ListType, MapType, StructType

    if isinstance(field_type, StructType):
        return {"struct": [_field_signature(child) for child in field_type.fields]}
    if isinstance(field_type, ListType):
        return {"list": _type_signature(field_type.element_type), "element_id": field_type.element_id,
                "element_required": field_type.element_required}
    if isinstance(field_type, MapType):
        return {"map": [_type_signature(field_type.key_type), _type_signature(field_type.value_type)],
                "key_id": field_type.key_id, "value_id": field_type.value_id,
                "value_required": field_type.value_required}
    return str(field_type)


def _field_signature(nested_field: Any) -> dict[str, Any]:
    return {"id": nested_field.field_id, "name": nested_field.name, "required": nested_field.required,
            "type": _type_signature(nested_field.field_type)}


def schema_fingerprint(schema: Any) -> str:
    """``sha256:<hex>`` of an Iceberg schema's fields: ids, names, types and nullability, in order.

    Args:
        schema: A pyiceberg ``Schema``, e.g. ``table.scan(...).projection()``.
    """
    payload = json.dumps([_field_signature(nested) for nested in schema.fields], sort_keys=True,
                         separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def _redact_uri(uri: str) -> str:
    return _URI_PASSWORD_RE.sub(r"\g<scheme>:***@", uri)


def _catalog_spec(source: Any, table: Any) -> dict[str, Any]:
    """A secret-free spec :func:`load` can reopen the catalog with."""
    from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog

    catalog = getattr(source, "catalog", None)
    if catalog is None:
        catalog = getattr(table, "catalog", None)
    if isinstance(catalog, LocalIcebergCatalog):
        return {"type": "local", "identifier": catalog.name, "warehouse_path": str(catalog.warehouse_path)}
    if catalog is None or not hasattr(catalog, "properties"):
        return {}
    kind = type(catalog).__name__.removesuffix("Catalog").lower() or "unknown"
    spec: dict[str, Any] = {"type": kind, "name": getattr(catalog, "name", None)}
    for key in ("uri", "warehouse"):
        value = catalog.properties.get(key)
        if isinstance(value, str) and value:
            spec[key] = _redact_uri(value)
    return {key: value for key, value in spec.items() if value is not None and not _SECRET_KEY_RE.search(key)}


def _normalise_fields(selected_fields: str | Iterable[str] | None) -> tuple[str, ...] | None:
    if selected_fields is None:
        return None
    if isinstance(selected_fields, str):
        selected_fields = [part.strip() for part in selected_fields.split(",")]
    fields = tuple(dict.fromkeys(selected_fields))
    if not fields or not all(isinstance(name, str) and name for name in fields):
        raise ConfigError(f"selected_fields must be a non-empty list of column names, got {selected_fields!r}")
    return fields


def _check_filter(row_filter: Any) -> str | None:
    if row_filter is None:
        return None
    if not isinstance(row_filter, str):
        raise TypeError("row_filter must be a string such as \"success = true\", so the manifest can store it; "
                        f"got {type(row_filter).__name__}")
    text = row_filter.strip()
    if not text:
        return None
    from pyiceberg.expressions.parser import parse

    try:
        parse(text)
    except Exception as exc:  # noqa: BLE001 - pyparsing raises its own exception types
        raise ConfigError(f"invalid row_filter {row_filter!r}: {exc}") from exc
    return text


def _scan(table: Any, snapshot_id: int, row_filter: str | None, selected_fields: tuple[str, ...] | None) -> Any:
    kwargs: dict[str, Any] = {"snapshot_id": snapshot_id}
    if row_filter is not None:
        kwargs["row_filter"] = row_filter
    if selected_fields is not None:
        kwargs["selected_fields"] = selected_fields
    return table.scan(**kwargs)


def _count(scan: Any) -> int:
    count = getattr(scan, "count", None)  # DataScan.count() reads file metadata where it can
    if callable(count):
        return int(count())
    return scan.to_arrow().num_rows


def _tag_name(name: str, version: int) -> str:
    safe = name if _TAG_SAFE_RE.fullmatch(name) else (
        re.sub(r"[^A-Za-z0-9_]", "_", name) + "_" + hashlib.sha256(name.encode()).hexdigest()[:8])
    return f"{TAG_PREFIX}{safe}_v{version}"


def _create_tag(reload: Callable[[], Any], snapshot_id: int, tag: str) -> Any:
    """Tag ``snapshot_id``; retry when a concurrent commit moved the table. Returns the table after."""
    from pyiceberg.exceptions import CommitFailedException

    table = reload()
    for attempt in range(1, _TAG_ATTEMPTS + 1):
        existing = table.metadata.refs.get(tag)
        if existing is not None:
            if existing.snapshot_id != snapshot_id:
                raise DatasetError(f"Iceberg tag {tag} already points at snapshot {existing.snapshot_id}")
            return table
        try:
            table.manage_snapshots().create_tag(snapshot_id, tag).commit()
            return reload()
        except CommitFailedException:
            if attempt == _TAG_ATTEMPTS:
                raise
            logger.debug("tag %s: commit conflict, retrying (%d/%d)", tag, attempt, _TAG_ATTEMPTS)
            table = reload()
    return table  # pragma: no cover - the loop returns or raises


def _manifest_versions(folder: Path) -> list[int]:
    versions = []
    for path in folder.glob("v*.json"):
        match = re.fullmatch(r"v(\d+)\.json", path.name)
        if match:
            versions.append(int(match.group(1)))
    return sorted(versions)


def _write_new_manifest(folder: Path, build: Callable[[int], DatasetVersion]) -> DatasetVersion:
    """Write the manifest of the next free version number; never replaces an existing one.

    The content goes to a temporary file first and is then hard-linked into place, which fails if
    another process took that number in the meantime; the loop then tries the next number.
    """
    folder.mkdir(parents=True, exist_ok=True)
    number = (_manifest_versions(folder) or [0])[-1] + 1
    while True:
        version = build(number)
        final = folder / f"v{number:06d}.json"
        tmp = folder / f".v{number:06d}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(version.to_dict(), indent=2, sort_keys=True) + "\n")
        try:
            os.link(tmp, final)
        except FileExistsError:
            number += 1
            continue
        except OSError:  # a filesystem without hard links: create-exclusive, then fill in
            try:
                with open(final, "x"):
                    pass
            except FileExistsError:
                number += 1
                continue
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        return replace(version, manifest_path=str(final))


def _rewrite_manifest(version: DatasetVersion) -> DatasetVersion:
    path = Path(version.manifest_path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(version.to_dict(), indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)
    return version


# --------------------------------------------------------------------------- API


def pin(
    iceberg: Any,
    name: str,
    row_filter: str | None = None,
    selected_fields: str | Sequence[str] | None = None,
    *,
    snapshot_id: int | None = None,
    properties: Mapping[str, Any] | None = None,
    tag: bool = True,
    warehouse: str | os.PathLike | None = None,
) -> DatasetVersion:
    """Pin the rows an Iceberg table gives now (or at ``snapshot_id``) as a new dataset version.

    Args:
        iceberg: The table: a :class:`~local_data_platform.format.iceberg.Iceberg` object, or a
            pyiceberg ``Table`` (then pass ``warehouse``).
        name: The dataset name. Each pin adds version ``N + 1`` of it.
        row_filter: A pyiceberg filter string, e.g. ``"success = true AND task = 'pick_place'"``.
        selected_fields: The columns to keep, as a list or a comma-separated string. All by default.
        snapshot_id: Pin this snapshot instead of the current one.
        properties: String labels stored in the manifest, e.g. ``{"split": "train"}``.
        tag: Tag the snapshot (``ldp_ds_<name>_v<N>``) so snapshot expiry keeps it.
        warehouse: Folder for the manifests. Defaults to the table's local warehouse, then
            ``$LDP_WAREHOUSE``.

    Returns:
        The new :class:`DatasetVersion`, with ``manifest_path`` set.

    Raises:
        ConfigError: If the name, filter or columns are invalid.
        TableNotFound: If the table doesn't exist.
        DatasetError: If the table has no snapshot, or ``snapshot_id`` isn't one of its snapshots.
    """
    name = _check_name(name)
    row_filter = _check_filter(row_filter)
    fields = _normalise_fields(selected_fields)
    labels = {str(key): str(value) for key, value in (properties or {}).items()}
    folder = _resolve_warehouse(warehouse, iceberg) / DATASETS_DIR / name
    table = _pyiceberg_table(iceberg)
    identifier = ".".join(table.name())

    snapshot = table.current_snapshot() if snapshot_id is None else table.snapshot_by_id(snapshot_id)
    if snapshot is None:
        what = "has no snapshots yet; write data before pinning" if snapshot_id is None else \
            f"has no snapshot {snapshot_id}"
        raise DatasetError(f"Iceberg table {identifier} {what}")
    scan = _scan(table, snapshot.snapshot_id, row_filter, fields)
    try:
        projection = scan.projection()
        row_count = _count(scan)
    except ValueError as exc:  # an unknown column in selected_fields or in the filter
        raise ConfigError(f"cannot pin {name} from {identifier}: {exc}") from exc

    catalog = _catalog_spec(iceberg, table)
    created = dt.datetime.now(dt.timezone.utc)

    def build(number: int) -> DatasetVersion:
        return DatasetVersion(
            name=name, table_identifier=identifier, snapshot_id=snapshot.snapshot_id, row_filter=row_filter,
            selected_fields=fields, row_count=row_count, schema_fingerprint=schema_fingerprint(projection),
            created_at=created, version=number, catalog=catalog, metadata_location=table.metadata_location,
            tag=_tag_name(name, number) if tag else None, properties=labels,
        )

    version = _write_new_manifest(folder, build)
    if version.tag is not None:
        version = _tag_snapshot(iceberg, table, version)
    logger.info("Pinned dataset %s: %d rows of %s at snapshot %d (%s)", version.ref, version.row_count,
                identifier, version.snapshot_id, version.manifest_path)
    return version


def _tag_snapshot(source: Any, table: Any, version: DatasetVersion) -> DatasetVersion:
    def reload() -> Any:
        if source is table:
            return table.refresh()
        return _pyiceberg_table(source)

    try:
        tagged = _create_tag(reload, version.snapshot_id, version.tag)
    except Exception as exc:  # noqa: BLE001 - commit conflicts and catalog errors vary by catalog type
        logger.warning("Dataset %s: could not tag snapshot %d (%s: %s); snapshot expiry may remove it",
                       version.ref, version.snapshot_id, type(exc).__name__, exc)
        return _rewrite_manifest(replace(version, tag=None))
    return _rewrite_manifest(replace(version, metadata_location=tagged.metadata_location))


def list_datasets(*, warehouse: Any = None) -> list[str]:
    """Names of the datasets with at least one version in a warehouse, sorted.

    Args:
        warehouse: The warehouse folder (or an ``Iceberg`` table); ``None`` for ``$LDP_WAREHOUSE``.
    """
    root = datasets_dir(warehouse)
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir() and _manifest_versions(path))


def list_versions(name: str, *, warehouse: Any = None) -> list[DatasetVersion]:
    """Every version of a dataset, oldest first. Unreadable manifests are skipped with a warning.

    Args:
        name: The dataset name.
        warehouse: The warehouse folder (or an ``Iceberg`` table); ``None`` for ``$LDP_WAREHOUSE``.

    Returns:
        The versions; an empty list if the dataset has none.
    """
    folder = datasets_dir(warehouse) / _check_name(name)
    versions = []
    for number in _manifest_versions(folder) if folder.is_dir() else []:
        path = folder / f"v{number:06d}.json"
        try:
            versions.append(DatasetVersion.from_dict(json.loads(path.read_text()), manifest_path=path))
        except (OSError, ValueError, DatasetError) as exc:
            logger.warning("Skipping unreadable dataset manifest %s: %s", path, exc)
    return versions


def get_version(name: str, version: int | None = None, *, warehouse: Any = None) -> DatasetVersion:
    """One version of a dataset: ``version`` N, or the latest when ``None``.

    Args:
        name: The dataset name, or ``"<name>@v<N>"``.
        version: The version number.
        warehouse: The warehouse folder (or an ``Iceberg`` table); ``None`` for ``$LDP_WAREHOUSE``.

    Raises:
        DatasetError: If the dataset or the version doesn't exist.
    """
    if isinstance(name, str) and "@" in name and version is None:
        name, _, text = name.partition("@")
        try:
            version = int(text.lstrip("vV"))
        except ValueError:
            raise ConfigError(f"expected '<name>@v<N>', got {name}@{text}") from None
    versions = list_versions(name, warehouse=warehouse)
    if not versions:
        raise DatasetError(f"dataset {name!r} has no versions in {datasets_dir(warehouse)}")
    if version is None:
        return versions[-1]
    for item in versions:
        if item.version == version:
            return item
    raise DatasetError(f"dataset {name!r} has no version {version}; versions: {[v.version for v in versions]}")


def _open_table(version: DatasetVersion, catalog: Any) -> Any:
    from pyiceberg.table import StaticTable

    if catalog is not None:
        if isinstance(catalog, Mapping):
            from local_data_platform.catalog.provider import create_catalog

            catalog = create_catalog(catalog)
        return catalog.load_table(version.table_identifier)
    problems = []
    spec = dict(version.catalog)
    if spec:
        try:
            return _load_from_spec(spec, version.table_identifier)
        except Exception as exc:  # noqa: BLE001 - any catalog failure falls back to the metadata file
            problems.append(f"catalog {spec.get('type')}: {exc}")
    if version.metadata_location:
        try:
            return StaticTable.from_metadata(version.metadata_location)
        except Exception as exc:  # noqa: BLE001 - FileNotFoundError, or a store error for remote metadata
            problems.append(f"metadata file {version.metadata_location}: {exc}")
    raise DatasetError(f"cannot open {version.table_identifier} for dataset {version.ref}: "
                       f"{'; '.join(problems) or 'the manifest names no catalog or metadata file'}. "
                       "Pass catalog= to load().")


def _load_from_spec(spec: dict[str, Any], identifier: str) -> Any:
    from local_data_platform.catalog.provider import catalog_database_file, create_catalog

    database = catalog_database_file(spec)
    if database is not None and not database.is_file():  # don't let the catalog create an empty database
        raise TableNotFound(f"no catalog database at {database}")
    catalog = create_catalog(spec)
    try:
        return catalog.load_table(identifier)
    finally:
        close = getattr(catalog, "close", None)
        if callable(close):
            close()


def load(version: DatasetVersion, *, catalog: Any = None, verify: bool = True) -> pa.Table:
    """Read the rows a dataset version pinned.

    The table is opened through the catalog recorded in the manifest, or through ``catalog``
    when given; if that fails, through the table metadata file recorded at pin time.

    Args:
        version: The version, from :func:`pin`, :func:`list_versions` or :func:`get_version`.
        catalog: A pyiceberg catalog, or a catalog spec dict, to open the table with instead.
        verify: Check the schema fingerprint and row count against the manifest.

    Returns:
        The rows, as a ``pyarrow.Table``. They are the same rows, with the same schema, every time.

    Raises:
        DatasetError: If the table can't be opened, the snapshot is gone (expired), or, with
            ``verify``, the schema or the row count differs from the manifest.
    """
    if not isinstance(version, DatasetVersion):
        raise TypeError(f"load() expects a DatasetVersion, got {type(version).__name__}; "
                        "use get_version(name) to look one up")
    table = _open_table(version, catalog)
    if table.snapshot_by_id(version.snapshot_id) is None:
        raise DatasetError(f"dataset {version.ref}: snapshot {version.snapshot_id} of {version.table_identifier} "
                           "no longer exists (was it expired?)")
    scan = _scan(table, version.snapshot_id, version.row_filter, version.selected_fields)
    if verify:
        actual = schema_fingerprint(scan.projection())
        if actual != version.schema_fingerprint:
            raise DatasetError(f"dataset {version.ref}: schema fingerprint {actual} differs from the pinned "
                               f"{version.schema_fingerprint}")
    rows = scan.to_arrow()
    if verify and rows.num_rows != version.row_count:
        raise DatasetError(f"dataset {version.ref}: read {rows.num_rows} rows, the manifest pinned "
                           f"{version.row_count}")
    logger.info("Loaded dataset %s: %d rows of %s at snapshot %d", version.ref, rows.num_rows,
                version.table_identifier, version.snapshot_id)
    return rows


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)  # Decimal, UUID


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_finite(item) for item in value]
    return value


def _jsonl_line(row: dict[str, Any]) -> str:
    try:
        return json.dumps(row, default=_json_default, allow_nan=False, ensure_ascii=False)
    except ValueError:  # NaN or infinity: JSON has no such numbers, so write null
        return json.dumps(_finite(row), default=_json_default, allow_nan=False, ensure_ascii=False)


def _write(rows: pa.Table, sink: Any, fmt: str) -> None:
    if fmt == "parquet":
        import pyarrow.parquet as pq

        pq.write_table(rows, sink)
        return
    for batch in rows.to_batches(max_chunksize=_JSONL_BATCH_ROWS):
        lines = "".join(_jsonl_line(row) + "\n" for row in batch.to_pylist())
        sink.write(lines.encode("utf-8"))


def export(version: DatasetVersion, uri: str | os.PathLike, format: str = "parquet", *,
           catalog: Any = None, base_dir: str | os.PathLike | None = None) -> int:
    """Write a dataset version's rows to a file.

    Parquet files carry the manifest in their schema metadata under ``ldp.dataset``, so an
    exported file says which table, snapshot and filter it came from. JSONL writes one JSON
    object per row; timestamps become ISO-8601 strings and NaN becomes ``null``.

    Args:
        version: The version to export.
        uri: A local path (relative paths resolve against ``base_dir``, else the cwd), or an
            ``s3://`` / ``gs://`` URI, written through :mod:`local_data_platform.fs`.
        format: ``"parquet"`` (the default) or ``"jsonl"``.
        catalog: Passed to :func:`load`.
        base_dir: The folder a relative ``uri`` resolves against.

    Returns:
        The number of rows written. The file is replaced atomically.

    Raises:
        ConfigError: If the format is unknown.
        DatasetError: If the version can't be loaded (see :func:`load`).
    """
    fmt = str(format).strip().lower()
    if fmt not in EXPORT_FORMATS:
        raise ConfigError(f"unknown export format {format!r}; expected one of {list(EXPORT_FORMATS)}")
    rows = load(version, catalog=catalog)
    if fmt == "parquet":
        metadata = dict(rows.schema.metadata or {})
        metadata[b"ldp.dataset"] = json.dumps(version.to_dict(), sort_keys=True).encode()
        rows = rows.replace_schema_metadata(metadata)
    text = os.fspath(uri)
    if "://" in text and not text.startswith("file://"):
        try:
            from local_data_platform.fs import open_output_atomic
        except ImportError as exc:
            raise ConfigError(f"exporting to {text.split('://', 1)[0]}:// needs local_data_platform.fs") from exc
        with open_output_atomic(text, base_dir) as sink:
            _write(rows, sink, fmt)
    else:
        path = resolve_path(text.removeprefix("file://"), base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as sink:
                _write(rows, sink, fmt)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    logger.info("Exported dataset %s (%d rows) to %s as %s", version.ref, rows.num_rows, text, fmt)
    return rows.num_rows


# --------------------------------------------------------------------------- CLI


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> Any:
    """Register ``ldp datasets pin|list|export`` on the ``ldp`` sub-command parser.

    Args:
        subparsers: The object ``argparse.ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options such as ``-v``.

    Returns:
        The ``datasets`` parser.
    """
    parents = list(parents)
    datasets = subparsers.add_parser(
        "datasets", parents=parents, help="pin, list and export reproducible dataset versions",
        description="Pin an Iceberg table's snapshot, filter and columns as a named dataset version, list the "
                    "versions, or export one. Manifests live in <warehouse>/.ldp/datasets/.")
    actions = datasets.add_subparsers(dest="datasets_action", metavar="ACTION")

    def usage(args: Any) -> int:
        datasets.print_help()
        return 1

    datasets.set_defaults(handler=usage)

    pin_parser = actions.add_parser("pin", parents=parents, help="pin the config's Iceberg table as a new version",
                                    description="Pin the current snapshot of CONFIG's Iceberg target (or source).")
    pin_parser.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    pin_parser.add_argument("name", metavar="NAME", help="dataset name")
    pin_parser.add_argument("--filter", dest="row_filter", help="pyiceberg row filter, e.g. \"success = true\"")
    pin_parser.add_argument("--fields", help="comma-separated columns to keep (default: all)")
    pin_parser.add_argument("--snapshot", type=int, help="pin this snapshot id instead of the current one")
    pin_parser.add_argument("--property", action="append", default=[], metavar="KEY=VALUE",
                            help="a label stored in the manifest; repeatable")
    pin_parser.add_argument("--no-tag", action="store_true", help="don't tag the snapshot")
    pin_parser.set_defaults(handler=_cmd_pin)

    list_parser = actions.add_parser("list", parents=parents, help="list datasets, or the versions of one")
    list_parser.add_argument("name", metavar="NAME", nargs="?", help="dataset name; omit to list every dataset")
    _add_warehouse_args(list_parser)
    list_parser.set_defaults(handler=_cmd_list)

    export_parser = actions.add_parser("export", parents=parents, help="write a version's rows to a file")
    export_parser.add_argument("name", metavar="NAME", help="dataset name, or NAME@vN")
    export_parser.add_argument("dest", metavar="DEST", help="output file or s3:// / gs:// URI")
    export_parser.add_argument("--version", type=int, help="version number (default: the latest)")
    export_parser.add_argument("--format", choices=EXPORT_FORMATS, default="parquet", help="default: parquet")
    _add_warehouse_args(export_parser)
    export_parser.set_defaults(handler=_cmd_export)
    return datasets


def _add_warehouse_args(parser: Any) -> None:
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--config", help="a dataset config; its Iceberg catalog's warehouse holds the manifests")
    where.add_argument("--warehouse", help=f"the warehouse folder (default: ${WAREHOUSE_ENV})")


def _config_iceberg_block(config: Any) -> tuple[str, dict[str, Any]]:
    for section in ("target", "source"):
        block = config.metadata[section]
        if str(block.get("format", "")).strip().upper() == "ICEBERG":
            return section, block
    raise ConfigError(f"config {config.identifier} has no Iceberg table: its source is "
                      f"{config.source['format']!r} and its target is {config.target['format']!r}")


def _config_warehouse(config_path: str) -> Path:
    from local_data_platform.etl import load_config

    config = load_config(config_path)
    _, block = _config_iceberg_block(config)
    catalog = block.get("catalog")
    if not isinstance(catalog, Mapping) or not catalog.get("warehouse_path"):
        raise ConfigError(f"config {config.identifier}: the Iceberg block has no catalog.warehouse_path")
    return config.resolve(catalog["warehouse_path"])


def _cli_warehouse(args: Any) -> Path:
    return _config_warehouse(args.config) if args.config else _resolve_warehouse(args.warehouse)


def _cmd_pin(args: Any) -> int:
    from local_data_platform.etl import load_config
    from local_data_platform.pipeline.builders import iceberg_from_config

    config = load_config(args.config)
    section, _ = _config_iceberg_block(config)
    table = iceberg_from_config(config, section, must_exist=True)
    properties = {}
    for item in args.property:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--property expects KEY=VALUE, got {item!r}")
        properties[key.strip()] = value
    version = pin(table, args.name, row_filter=args.row_filter,
                  selected_fields=args.fields, snapshot_id=args.snapshot, properties=properties,
                  tag=not args.no_tag)
    print(f"Pinned {version.ref}: {version.row_count} rows of {version.table_identifier} "
          f"at snapshot {version.snapshot_id}" + (f" (tag {version.tag})" if version.tag else ""))
    print(f"  manifest: {version.manifest_path}")
    return 0


def _version_row(version: DatasetVersion) -> dict[str, Any]:
    return {
        "name": version.name,
        "version": version.version,
        "table": version.table_identifier,
        "snapshot_id": version.snapshot_id,
        "rows": version.row_count,
        "row_filter": version.row_filter or "-",
        "fields": ",".join(version.selected_fields) if version.selected_fields else "*",
        "created_at_utc": version.created_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _cmd_list(args: Any) -> int:
    from local_data_platform.cli import format_table

    warehouse = _cli_warehouse(args)
    if args.name:
        versions = list_versions(args.name, warehouse=warehouse)
        if not versions:
            raise DatasetError(f"dataset {args.name!r} has no versions in {datasets_dir(warehouse)}")
        rows = [_version_row(version) for version in versions]
    else:
        names = list_datasets(warehouse=warehouse)
        if not names:
            print(f"No datasets in {datasets_dir(warehouse)}")
            return 0
        rows = []
        for dataset in names:
            versions = list_versions(dataset, warehouse=warehouse)
            if versions:
                rows.append({**_version_row(versions[-1]), "versions": len(versions)})
    print(format_table(rows))
    return 0


def _cmd_export(args: Any) -> int:
    warehouse = _cli_warehouse(args)
    version = get_version(args.name, args.version, warehouse=warehouse)
    rows = export(version, args.dest, format=args.format)
    print(f"Exported {version.ref} ({rows} rows, snapshot {version.snapshot_id}) to {args.dest} as {args.format}")
    return 0


__all__ = [
    "DATASETS_DIR",
    "EXPORT_FORMATS",
    "MANIFEST_FORMAT",
    "TAG_PREFIX",
    "WAREHOUSE_ENV",
    "DatasetError",
    "DatasetVersion",
    "add_cli",
    "datasets_dir",
    "export",
    "get_version",
    "list_datasets",
    "list_versions",
    "load",
    "pin",
    "schema_fingerprint",
]
