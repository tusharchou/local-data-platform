"""Trimming the ``_ldp`` history tables: drop the days of rows that are too old, then expire snapshots and find orphans.

``ldp monitor`` commits to ``_ldp.monitor_results`` every time that it runs, a run commits to ``_ldp.runs`` and
``_ldp.quality_results`` when it ends, and the MCP server commits its audit records to ``_ldp.audit``. A commit is one
small data file, one manifest, one manifest list and one snapshot, so a laptop that runs ``ldp monitor`` every 15
minutes adds thousands of small files a month to the catalog, and nothing in the history ever leaves it.

:func:`delete_old_rows` drops the rows of one table that came before a cutoff. Every ``_ldp`` table is partitioned by
``day(ts)`` and the cutoff is the start of a UTC day, so each data file lies on one side of it and a delete drops whole
files from the table without rewriting one. :func:`maintain_system_tables` does that for the four tables of a catalog,
and then expires their snapshots and finds the orphan files with the functions that ``ldp maintain`` uses for any
table. A delete changes the table and nothing else, so expiring snapshots and finding orphans are what bring the files
themselves off the disk. A file that a delete dropped is named by the snapshots from before the delete and by the
snapshot of the delete, and after they expire by the last ``keep_metadata_versions`` metadata versions that still list
them. So it leaves the disk at a later run, once those snapshots have expired and that many more commits have been made.

The CLI is ``ldp maintain CONFIG --system [--keep-days N] [--apply]``, registered by
:func:`local_data_platform.maintenance.cli.add_cli`.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any

from pyiceberg.exceptions import CommitFailedException, NoSuchNamespaceError, NoSuchTableError

from local_data_platform.events import AUDIT_TABLE, MONITOR_TABLE, QUALITY_TABLE, RUNS_TABLE, SYSTEM_NAMESPACE
from local_data_platform.logger import get_logger
from local_data_platform.maintenance._common import (
    MaintenanceError,
    as_table,
    check_count,
    table_name,
    to_utc,
    utc_now,
)
from local_data_platform.maintenance.orphans import remove_orphans
from local_data_platform.maintenance.snapshots import expire_snapshots

logger = get_logger(__name__)

DEFAULT_KEEP_DAYS = 90
"""How many days of rows ``ldp maintain --system`` keeps in each table unless ``--keep-days`` says otherwise."""

SYSTEM_TABLES = (RUNS_TABLE, QUALITY_TABLE, MONITOR_TABLE, AUDIT_TABLE)
"""The tables of the system namespace that hold history, in the order that they are trimmed."""

TIME_COLUMN = "ts"
"""The column that every one of those tables is partitioned by day on."""

MAX_COMMIT_ATTEMPTS = 3
"""How many times :func:`delete_old_rows` plans again after a concurrent commit before it gives up."""


def trim_cutoff(keep_days: int, now: dt.datetime | None = None) -> dt.datetime:
    """The moment before which rows go, which is the start of a UTC day.

    It is the start of the day that holds the moment ``keep_days`` days before ``now``. A cutoff at the start of a day
    leaves each partition of ``day(ts)`` on one side of it, so a delete drops files and rewrites none. The rows of the
    day that holds that moment stay, so a table keeps at least ``keep_days`` days and at most a day more.

    Args:
        keep_days: How many days to keep. At least 0, and 0 keeps today only.
        now: The current time, ``None`` for the clock. A naive value is taken to be UTC.

    Returns:
        A datetime in UTC.
    """
    keep_days = check_count(keep_days, "keep_days", 0)
    moment = to_utc(utc_now() if now is None else now) - dt.timedelta(days=keep_days)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _iso(moment: dt.datetime) -> str:
    """A moment as ISO 8601 in UTC to the second, with a ``Z``."""
    return to_utc(moment).isoformat(timespec="seconds").replace("+00:00", "Z")


def _bound(table: Any, tasks: list[Any], attribute: str, pick: Callable[[list[int]], int]) -> str | None:
    """The lowest or highest ``ts`` that the data files of some tasks record, or ``None`` when they record none."""
    from pyiceberg.conversions import from_bytes

    field = table.schema().find_field(TIME_COLUMN)
    values = []
    for task in tasks:
        raw = (getattr(task.file, attribute) or {}).get(field.field_id)
        if raw is not None:
            values.append(from_bytes(field.field_type, raw))
    if not values:
        return None
    return _iso(dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=pick(values)))


def delete_old_rows(table: Any, cutoff: dt.datetime, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete the rows of a system table whose ``ts`` is before ``cutoff``.

    The files that hold such rows are found from the metadata of the table, so a dry run of a table that is partitioned
    by ``day(ts)`` reads no data file. With a cutoff that :func:`trim_cutoff` gives, every one of those files lies
    before the cutoff entirely, and the delete drops it from the table. A table that is not partitioned that way still
    loses the right rows, and a file that holds rows on both sides of the cutoff is rewritten without them. A delete
    that matches no row commits nothing.

    Only the table changes. The files that it dropped stay on disk until the snapshots that name them are expired and
    :func:`~local_data_platform.maintenance.remove_orphans` reclaims them.

    Args:
        table: A pyiceberg ``Table`` or an :class:`~local_data_platform.format.iceberg.Iceberg` format. A pyiceberg
            table is refreshed in place after the commit.
        cutoff: Rows before this go. A naive value is taken to be UTC.
        dry_run: Plan only, and change nothing.

    Returns:
        A JSON-ready report with ``table``, ``dry_run``, ``committed``, ``cutoff`` (ISO UTC), ``rows_before`` (the rows
        of the table now), ``rows_deleted`` and ``files_deleted`` (what was deleted, or what would be on a dry run, with
        a file counted when it may hold a row that goes, which for a table that keeps no statistics is every file), and
        ``oldest`` and ``newest``, the lowest and the highest ``ts`` among the rows to go, from the statistics of their
        files (``None`` when there are no statistics or no such row).

    Raises:
        MaintenanceError: If concurrent commits kept invalidating the plan ``MAX_COMMIT_ATTEMPTS`` times.
    """
    from pyiceberg.expressions import LessThan

    tbl = as_table(table)
    cutoff = to_utc(cutoff)
    row_filter = LessThan(TIME_COLUMN, cutoff.isoformat())
    current = tbl
    for attempt in range(1, MAX_COMMIT_ATTEMPTS + 1):
        scan = current.scan(row_filter=row_filter)
        tasks = list(scan.plan_files())
        report = {
            "table": table_name(current),
            "dry_run": dry_run,
            "committed": False,
            "cutoff": _iso(cutoff),
            "rows_before": current.scan().count(),
            "rows_deleted": scan.count(),
            "files_deleted": len(tasks),
            "oldest": _bound(current, tasks, "lower_bounds", min),
            "newest": _bound(current, tasks, "upper_bounds", max),
        }
        if dry_run or not report["rows_deleted"]:
            logger.info("%s %d rows in %d files of %s before %s", "Would delete" if dry_run else "Deleted",
                        report["rows_deleted"], report["files_deleted"], report["table"], report["cutoff"])
            return report
        try:
            current.delete(row_filter)
        except CommitFailedException as exc:
            logger.info("The delete from %s conflicted with a concurrent commit (attempt %d/%d): %s",
                        report["table"], attempt, MAX_COMMIT_ATTEMPTS, exc)
            if attempt == MAX_COMMIT_ATTEMPTS:
                raise MaintenanceError(f"deleting old rows of {report['table']} conflicted with concurrent commits "
                                       f"{MAX_COMMIT_ATTEMPTS} times, so nothing was deleted") from exc
            current = tbl.catalog.load_table(tbl.name())
            continue
        tbl.refresh()
        report["committed"] = True
        logger.info("Deleted %d rows in %d files of %s before %s", report["rows_deleted"], report["files_deleted"],
                    report["table"], report["cutoff"])
        return report
    raise AssertionError("unreachable")  # pragma: no cover


def maintain_system_tables(catalog: Any, *, namespace: str = SYSTEM_NAMESPACE, cutoff: dt.datetime,
                           expire_days: float | None, retain_last: int, orphan_age_hours: float | None,
                           keep_metadata_versions: int, dry_run: bool) -> dict[str, Any]:
    """Trim the history tables of a catalog, one table after the other.

    For each table of :data:`SYSTEM_TABLES` that exists it deletes the rows before ``cutoff``, then expires the
    snapshots older than ``expire_days`` when that is given, then finds the orphan files when ``orphan_age_hours`` is
    given. A table that does not exist is reported and left alone, and no table is created. The first error stops the
    run, and a later run finds the rest of the work, because every step is safe to repeat.

    Args:
        catalog: A pyiceberg catalog.
        namespace: The system namespace, ``_ldp`` unless a sink names another.
        cutoff: Rows before this go, as :func:`delete_old_rows` takes it.
        expire_days: Expire snapshots committed more than this many days ago, or ``None`` to leave the snapshots alone.
        retain_last: Keep ``main``'s last N snapshots of each table.
        orphan_age_hours: Only files older than this are orphans, or ``None`` to leave the files alone.
        keep_metadata_versions: Files reachable from this many previous metadata versions are kept.
        dry_run: Change nothing.

    Returns:
        A JSON-ready report with ``namespace``, ``dry_run``, ``cutoff`` and ``tables``, which maps each table such as
        ``_ldp.runs`` to ``{"exists": False}`` or to ``{"exists": True, "delete": ...}`` and, for each part that ran,
        ``"expire"`` and ``"orphans"``, the reports of :func:`delete_old_rows`,
        :func:`~local_data_platform.maintenance.expire_snapshots` and
        :func:`~local_data_platform.maintenance.remove_orphans`.

    Raises:
        MaintenanceError: If a delete or an expiry kept conflicting with concurrent commits.
    """
    tables: dict[str, dict[str, Any]] = {}
    for name in SYSTEM_TABLES:
        identifier = f"{namespace}.{name}"
        try:
            table = catalog.load_table(identifier)
        except (NoSuchTableError, NoSuchNamespaceError):
            tables[identifier] = {"exists": False}
            continue
        entry: dict[str, Any] = {"exists": True, "delete": delete_old_rows(table, cutoff, dry_run=dry_run)}
        if expire_days is not None:
            entry["expire"] = expire_snapshots(table, older_than=dt.timedelta(days=expire_days),
                                               retain_last=retain_last, dry_run=dry_run)
        if orphan_age_hours is not None:
            entry["orphans"] = remove_orphans(table, older_than_hours=orphan_age_hours, dry_run=dry_run,
                                              keep_metadata_versions=keep_metadata_versions)
        tables[identifier] = entry
    return {"namespace": namespace, "dry_run": dry_run, "cutoff": _iso(cutoff), "tables": tables}


__all__ = ["DEFAULT_KEEP_DAYS", "SYSTEM_TABLES", "delete_old_rows", "maintain_system_tables", "trim_cutoff"]
