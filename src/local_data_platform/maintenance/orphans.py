"""Find, and on request delete, files under a table's location that no metadata references.

A file is an orphan when it sits under the table location, nothing reachable from the table's metadata
names it, and it is older than ``older_than_hours``. "Reachable" is deliberately generous:

* the current metadata file and every metadata file in its ``metadata-log``;
* every manifest list, manifest, data file and delete file of every snapshot (all branches and tags) in the
  current metadata *and* in the last ``keep_metadata_versions`` previous metadata versions, counting
  manifest entries marked deleted (an older snapshot may still need them);
* statistics and partition-statistics files.

Files are also skipped, never reported, when:

* any path component is hidden (starts with ``.``, or with ``_`` and has no ``=``), as Iceberg Java's
  orphan-file action does;
* they belong to another table nested under this one (a folder holding ``metadata/*.metadata.json``);
* they are younger than the age cut, or their age is unknown. That protects files a writer is producing
  right now.

It fails closed. If the current metadata's manifest lists or manifests can't be read, it raises
:class:`MaintenanceError` rather than treating everything as unreferenced. If the table commits while the
location is being listed, the references of the new metadata are added before anything is reported.
"""

import datetime as dt
import os
import posixpath
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from local_data_platform.logger import get_logger
from local_data_platform.maintenance._common import (
    MaintenanceError,
    as_table,
    check_count,
    check_non_negative,
    table_name,
    utc_now,
)

logger = get_logger(__name__)

DEFAULT_OLDER_THAN_HOURS = 72.0
"""Files younger than this are never orphan candidates by default."""

DEFAULT_KEEP_METADATA_VERSIONS = 50
"""Previous metadata versions whose snapshots also count as references (SaaS design §9.2)."""

MIN_APPLY_AGE_HOURS = 24.0
""":func:`remove_orphans` refuses to delete files younger than this unless ``allow_recent=True``."""

MAX_RECHECKS = 3
"""How many times the scan re-reads a table that committed during the listing before giving up."""

_LOCAL_SCHEMES = ("", "file")
_SCHEME_ALIASES = {"s3a": "s3", "s3n": "s3", "gcs": "gs"}


@dataclass(frozen=True)
class _Listed:
    uri: str          # the location to report and delete, in the table location's scheme
    key: str          # normalised, for comparing against references
    rel: str          # "/"-separated path relative to the table location
    size: int
    mtime: float | None  # epoch seconds, None when unknown


@dataclass
class _Scan:
    orphans: list[_Listed]
    stats: dict[str, int]
    location: str


# ---------------------------------------------------------------------- paths


def _is_local(parsed: Any, location: str) -> bool:
    if parsed.scheme in _LOCAL_SCHEMES:
        return True
    return len(parsed.scheme) == 1 and os.name == "nt"  # a Windows drive letter such as C:\


def _local_path(location: str) -> str:
    parsed = urlparse(location)
    if parsed.scheme == "file":
        path = parsed.path
        if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return location


def normalize_location(location: str) -> str:
    """A comparison key for a file location, so ``file:/x``, ``file:///x`` and ``/x`` compare equal.

    Local paths are resolved with ``os.path.realpath`` (on macOS ``/tmp`` is ``/private/tmp``), and ``s3a``
    and ``s3n`` compare equal to ``s3``.
    """
    parsed = urlparse(location)
    if _is_local(parsed, location):
        return "file:" + os.path.normcase(os.path.realpath(_local_path(location)))
    scheme = _SCHEME_ALIASES.get(parsed.scheme.lower(), parsed.scheme.lower())
    return f"{scheme}://{parsed.netloc}{parsed.path}"


def _hidden(component: str) -> bool:
    return component.startswith(".") or (component.startswith("_") and "=" not in component)


# ---------------------------------------------------------------------- references


def _read_version(io: Any, location: str) -> Any | None:
    from pyiceberg.serializers import FromInputFile

    try:
        return FromInputFile.table_metadata(io.new_input(location))
    except FileNotFoundError:
        logger.debug("Previous metadata file %s is gone; skipping it", location)
        return None


def _referenced_keys(table: Any, keep_metadata_versions: int) -> tuple[set[str], int]:
    """Every file the table's metadata can reach, as normalised keys, and how many metadata versions were read.

    Raises:
        MaintenanceError: If a manifest list or manifest of the current metadata can't be read.
    """
    io = table.io
    metadata = table.metadata
    keys = {normalize_location(table.metadata_location)}
    keys.update(normalize_location(entry.metadata_file) for entry in metadata.metadata_log)

    versions: list[tuple[str, Any, bool]] = [(table.metadata_location, metadata, True)]
    previous = list(metadata.metadata_log)[-keep_metadata_versions:] if keep_metadata_versions else []
    for entry in reversed(previous):
        older = _read_version(io, entry.metadata_file)
        if older is not None:
            versions.append((entry.metadata_file, older, False))

    seen_lists: set[str] = set()
    seen_manifests: set[str] = set()
    for location, version, current in versions:
        for stats in list(getattr(version, "statistics", []) or []) + list(
                getattr(version, "partition_statistics", []) or []):
            keys.add(normalize_location(stats.statistics_path))
        for snapshot in version.snapshots:
            manifest_list = getattr(snapshot, "manifest_list", None)
            if not manifest_list:
                if current:
                    raise MaintenanceError(f"snapshot {snapshot.snapshot_id} of {table_name(table)} has no manifest "
                                           "list, so its files can't be enumerated; refusing to look for orphans")
                continue
            if manifest_list in seen_lists:
                continue
            try:
                manifests = snapshot.manifests(io)
            except FileNotFoundError as exc:
                if current:
                    raise MaintenanceError(f"manifest list {manifest_list} of snapshot {snapshot.snapshot_id} is "
                                           f"missing; refusing to look for orphans in {table_name(table)}") from exc
                logger.warning("Manifest list %s of an older metadata version %s is missing; skipping it",
                               manifest_list, location)
                continue
            seen_lists.add(manifest_list)
            keys.add(normalize_location(manifest_list))
            for manifest in manifests:
                if manifest.manifest_path in seen_manifests:
                    continue
                try:
                    entries = manifest.fetch_manifest_entry(io, discard_deleted=False)
                except FileNotFoundError as exc:
                    if current:
                        raise MaintenanceError(f"manifest {manifest.manifest_path} is missing; refusing to look for "
                                               f"orphans in {table_name(table)}") from exc
                    logger.warning("Manifest %s of an older metadata version %s is missing; skipping it",
                                   manifest.manifest_path, location)
                    continue
                seen_manifests.add(manifest.manifest_path)
                keys.add(normalize_location(manifest.manifest_path))
                keys.update(normalize_location(entry.data_file.file_path) for entry in entries)
    return keys, len(versions)


# ---------------------------------------------------------------------- listing


def _list_local(location: str) -> Iterator[_Listed]:
    root = _local_path(location)
    if not os.path.isdir(root):
        return
    with_scheme = urlparse(location).scheme == "file"
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full, follow_symlinks=False)
            except FileNotFoundError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if with_scheme:
                uri = "file://" + (full if full.startswith("/") else "/" + full.replace("\\", "/"))
            else:
                uri = full
            yield _Listed(uri=uri, key=normalize_location(full), rel=rel, size=stat.st_size, mtime=stat.st_mtime)


def _remote_filesystem(table: Any, location: str) -> tuple[Any, str, str]:
    """``(pyarrow filesystem, base path, uri prefix)`` for an object-store location.

    The table's own ``PyArrowFileIO`` builds the filesystem when it can, so listing uses the same endpoint
    and credentials as the table's writes.
    """
    import pyarrow.fs as pafs

    parsed = urlparse(location)
    scheme, netloc = parsed.scheme, parsed.netloc
    if scheme in ("hdfs", "viewfs"):
        base, prefix = parsed.path, f"{scheme}://{netloc}"
    else:
        base, prefix = f"{netloc}{parsed.path}", f"{scheme}://"
    fs_by_scheme = getattr(table.io, "fs_by_scheme", None)
    if callable(fs_by_scheme):
        return fs_by_scheme(scheme, netloc), base.rstrip("/"), prefix
    filesystem, path = pafs.FileSystem.from_uri(location)
    return filesystem, path.rstrip("/"), prefix


def _list_remote(table: Any, location: str) -> Iterator[_Listed]:
    import pyarrow.fs as pafs

    filesystem, base, prefix = _remote_filesystem(table, location)
    selector = pafs.FileSelector(base, recursive=True, allow_not_found=True)
    for info in sorted(filesystem.get_file_info(selector), key=lambda item: item.path):
        if info.type != pafs.FileType.File:
            continue
        rel = info.path[len(base):].lstrip("/") if info.path.startswith(base) else posixpath.basename(info.path)
        mtime = None
        if info.mtime is not None:
            moment = info.mtime if info.mtime.tzinfo else info.mtime.replace(tzinfo=dt.timezone.utc)
            mtime = moment.timestamp()
        uri = prefix + info.path
        yield _Listed(uri=uri, key=normalize_location(uri), rel=rel, size=info.size or 0, mtime=mtime)


def _list_files(table: Any, location: str) -> list[_Listed]:
    parsed = urlparse(location)
    if _is_local(parsed, location):
        return list(_list_local(location))
    return list(_list_remote(table, location))


def _guard_location(table: Any, location: str) -> None:
    """Refuse to scan a location that holds more than this table (the filesystem root, or a local warehouse)."""
    parsed = urlparse(location)
    if not _is_local(parsed, location):
        if not parsed.path.strip("/"):
            raise MaintenanceError(f"table location {location} is a bucket root; refusing to look for orphans")
        return
    path = os.path.realpath(_local_path(location))
    if path == os.path.dirname(path):
        raise MaintenanceError(f"table location {location} is a filesystem root; refusing to look for orphans")
    warehouse = getattr(table.catalog, "warehouse_path", None)
    if warehouse is not None:
        warehouse_real = os.path.realpath(str(warehouse))
        if warehouse_real == path or warehouse_real.startswith(path.rstrip(os.sep) + os.sep):
            raise MaintenanceError(f"table location {location} contains the catalog warehouse {warehouse}; "
                                   "refusing to look for orphans")


# ---------------------------------------------------------------------- scan


def _reload(table: Any) -> Any | None:
    try:
        return table.catalog.load_table(table.name())
    except (NotImplementedError, AttributeError):  # a StaticTable has no catalog to reload from
        return None


def _scan(table: Any, older_than_hours: float, keep_metadata_versions: int) -> _Scan:
    location = table.location()
    _guard_location(table, location)
    cutoff = utc_now().timestamp() - older_than_hours * 3600
    referenced, versions = _referenced_keys(table, keep_metadata_versions)
    listed = _list_files(table, location)

    nested_roots = set()
    for item in listed:
        parts = item.rel.split("/")
        if len(parts) >= 3 and parts[-2] == "metadata" and parts[-1].endswith(".metadata.json"):
            nested_roots.add("/".join(parts[:-2]) + "/")

    stats = {"listed": len(listed), "referenced": 0, "recent": 0, "hidden": 0, "other_tables": 0,
             "metadata_versions_read": versions, "rechecks": 0}
    candidates = []
    for item in listed:
        if any(_hidden(part) for part in item.rel.split("/")):
            stats["hidden"] += 1
        elif any(item.rel.startswith(root) for root in nested_roots):
            stats["other_tables"] += 1
        elif item.key in referenced:
            stats["referenced"] += 1
        elif item.mtime is None or item.mtime >= cutoff:
            stats["recent"] += 1
        else:
            candidates.append(item)

    # The table may have committed while we listed; a commit only adds references, so add them.
    for _ in range(MAX_RECHECKS + 1):
        fresh = _reload(table)
        if fresh is None or fresh.metadata_location == table.metadata_location:
            break
        stats["rechecks"] += 1
        if stats["rechecks"] > MAX_RECHECKS:
            raise MaintenanceError(f"{table_name(table)} kept committing while it was scanned; no orphans reported")
        table = fresh
        newer, _ = _referenced_keys(table, keep_metadata_versions)
        now_referenced = [item for item in candidates if item.key in newer]
        stats["referenced"] += len(now_referenced)
        candidates = [item for item in candidates if item.key not in newer]
    return _Scan(orphans=candidates, stats=stats, location=location)


def _check_args(older_than_hours: Any, keep_metadata_versions: Any) -> tuple[float, int]:
    return (check_non_negative(older_than_hours, "older_than_hours"),
            check_count(keep_metadata_versions, "keep_metadata_versions", 0))


def find_orphans(
    table: Any,
    *,
    older_than_hours: float = DEFAULT_OLDER_THAN_HOURS,
    keep_metadata_versions: int = DEFAULT_KEEP_METADATA_VERSIONS,
) -> list[str]:
    """List the orphan files under a table's location. A dry run: nothing is deleted.

    Args:
        table: A pyiceberg ``Table`` or an :class:`~local_data_platform.format.iceberg.Iceberg` format.
        older_than_hours: Only files last modified more than this many hours ago are reported.
        keep_metadata_versions: Also count as referenced every file reachable from this many previous metadata
            versions (0 means only the current metadata's snapshots).

    Returns:
        The orphan files' locations, sorted, in the scheme of the table location (for example
        ``file:///...`` or ``s3://bucket/...``).

    Raises:
        TypeError, ValueError: If an argument is invalid.
        MaintenanceError: If the table's files can't be enumerated safely, or its location holds more than
            the table.
    """
    older_than_hours, keep_metadata_versions = _check_args(older_than_hours, keep_metadata_versions)
    scan = _scan(as_table(table), older_than_hours, keep_metadata_versions)
    return sorted(item.uri for item in scan.orphans)


def remove_orphans(
    table: Any,
    *,
    older_than_hours: float = DEFAULT_OLDER_THAN_HOURS,
    dry_run: bool = True,
    keep_metadata_versions: int = DEFAULT_KEEP_METADATA_VERSIONS,
    allow_recent: bool = False,
) -> dict[str, Any]:
    """Find orphan files and, only when ``dry_run=False``, delete them.

    Args:
        table: A pyiceberg ``Table`` or an :class:`~local_data_platform.format.iceberg.Iceberg` format.
        older_than_hours: Only files last modified more than this many hours ago are orphan candidates.
        dry_run: Report only. This is the default; pass ``False`` to delete.
        keep_metadata_versions: See :func:`find_orphans`.
        allow_recent: Allow deleting with ``older_than_hours`` below :data:`MIN_APPLY_AGE_HOURS` (24). A file
            that young may belong to a write that hasn't committed yet.

    Returns:
        A JSON-ready report: ``table``, ``location``, ``dry_run``, ``older_than_hours``,
        ``keep_metadata_versions``, ``orphans`` (sorted locations), ``orphan_bytes``, ``deleted``, ``missing``
        (orphans already gone when deleted), ``failed`` (``[{"path", "error"}]``) and ``stats`` (counts of
        files listed, referenced, too recent, hidden, in nested tables, metadata versions read and rechecks).

    Raises:
        TypeError, ValueError: If an argument is invalid, or deleting with a short age without ``allow_recent``.
        MaintenanceError: As for :func:`find_orphans`.
    """
    older_than_hours, keep_metadata_versions = _check_args(older_than_hours, keep_metadata_versions)
    if not dry_run and older_than_hours < MIN_APPLY_AGE_HOURS and not allow_recent:
        raise ValueError(
            f"refusing to delete files younger than {MIN_APPLY_AGE_HOURS:g} hours (older_than_hours="
            f"{older_than_hours:g}): they may belong to a write in progress. Pass allow_recent=True to override."
        )
    tbl = as_table(table)
    started = time.monotonic()
    scan = _scan(tbl, older_than_hours, keep_metadata_versions)
    orphans = sorted(scan.orphans, key=lambda item: item.uri)
    report: dict[str, Any] = {
        "table": table_name(tbl),
        "location": scan.location,
        "dry_run": bool(dry_run),
        "older_than_hours": older_than_hours,
        "keep_metadata_versions": keep_metadata_versions,
        "orphans": [item.uri for item in orphans],
        "orphan_bytes": sum(item.size for item in orphans),
        "deleted": [],
        "missing": [],
        "failed": [],
        "stats": scan.stats,
    }
    if not dry_run:
        for item in orphans:
            try:
                tbl.io.delete(item.uri)
            except FileNotFoundError:
                report["missing"].append(item.uri)
            except OSError as exc:
                report["failed"].append({"path": item.uri, "error": f"{type(exc).__name__}: {exc}"})
            else:
                report["deleted"].append(item.uri)
    logger.info("%s %d orphan files (%d bytes) of %s in %.2fs", "Found" if dry_run else "Deleted",
                len(report["orphans"]) if dry_run else len(report["deleted"]), report["orphan_bytes"],
                report["table"], time.monotonic() - started)
    if report["failed"]:
        logger.warning("Could not delete %d orphan files of %s", len(report["failed"]), report["table"])
    return report


__all__ = ["DEFAULT_KEEP_METADATA_VERSIONS", "DEFAULT_OLDER_THAN_HOURS", "MIN_APPLY_AGE_HOURS", "find_orphans",
           "normalize_location", "remove_orphans"]
