"""The ``ldp maintain`` command: ``ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]``.

Everything is a dry run unless ``--apply`` is given. With neither ``--expire`` nor ``--orphans`` both run,
expiring snapshots older than :data:`DEFAULT_EXPIRE_DAYS` days.

With ``--system`` the command maintains the ``_ldp`` history tables of the config's catalog and not the table of the
config. It deletes the rows older than ``--keep-days`` days, as :mod:`local_data_platform.maintenance.system` says, and
then expires snapshots and finds orphan files in each of those tables in the same way.

``cli.py`` wires this in with :func:`add_cli`. Heavy imports happen inside the command.
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from typing import Any

from local_data_platform.exceptions import ConfigError

DEFAULT_EXPIRE_DAYS = 7.0
"""The ``--expire`` age used when neither ``--expire`` nor ``--orphans`` is given."""


def _out(text: str = "") -> None:
    print(text, file=sys.stdout)


def _non_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if not value >= 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative number, got {text!r}")
    return value


def _count(minimum: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"expected a number of at least {minimum}, got {value}")
        return value

    return parse


def add_cli(subparsers: Any, *, parents: Sequence[argparse.ArgumentParser] = ()) -> argparse.ArgumentParser:
    """Register ``ldp maintain`` on an argparse subparsers object.

    Args:
        subparsers: The object ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (``cli.py``'s shared ``-v``).

    Returns:
        The ``maintain`` parser. Its ``handler`` default takes the parsed args and returns the exit status.
    """
    from local_data_platform.maintenance.orphans import DEFAULT_KEEP_METADATA_VERSIONS, DEFAULT_OLDER_THAN_HOURS
    from local_data_platform.maintenance.snapshots import DEFAULT_RETAIN_LAST
    from local_data_platform.maintenance.system import DEFAULT_KEEP_DAYS

    parser = subparsers.add_parser(
        "maintain", parents=list(parents),
        help="expire old snapshots and find orphan files, or trim the _ldp history (a dry run unless --apply)",
        description="Expire snapshots of the config's Iceberg target (or source) while keeping every idempotency "
                    "key inside the table's horizon, and find files under the table location that no snapshot "
                    "references. Nothing changes unless --apply is given. With neither --expire nor --orphans, "
                    f"both run and --expire defaults to {DEFAULT_EXPIRE_DAYS:g} days. With --system the tables are "
                    "the _ldp history tables of the config's catalog (runs, quality_results, monitor_results and "
                    "audit). Their rows older than --keep-days days are deleted first, whole UTC days at a time, "
                    "and then the snapshots expire and the orphan files are found as above.",
    )
    parser.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    parser.add_argument("--expire", metavar="DAYS", type=_non_negative,
                        help="expire snapshots committed more than DAYS days ago")
    parser.add_argument("--orphans", action="store_true",
                        help="find files under the table location that no metadata references")
    parser.add_argument("--apply", action="store_true", help="make the changes; without it everything is a dry run")
    parser.add_argument("--system", action="store_true",
                        help="maintain the _ldp history tables of the config's catalog instead of its table: delete "
                             "the rows older than --keep-days, then expire snapshots and find orphan files")
    parser.add_argument("--keep-days", metavar="N", type=_count(0), default=None,
                        help="with --system, keep the last N days of rows, counted in whole UTC days, so a row of "
                             f"the day that holds the moment N days back stays (default {DEFAULT_KEEP_DAYS})")
    parser.add_argument("--retain-last", metavar="N", type=_count(1), default=DEFAULT_RETAIN_LAST,
                        help=f"always keep main's last N snapshots (default {DEFAULT_RETAIN_LAST})")
    parser.add_argument("--orphan-age-hours", metavar="HOURS", type=_non_negative, default=DEFAULT_OLDER_THAN_HOURS,
                        help=f"only files older than this are orphans (default {DEFAULT_OLDER_THAN_HOURS:g})")
    parser.add_argument("--keep-metadata-versions", metavar="N", type=_count(0), default=DEFAULT_KEEP_METADATA_VERSIONS,
                        help="files reachable from this many previous metadata versions are kept "
                             f"(default {DEFAULT_KEEP_METADATA_VERSIONS})")
    parser.add_argument("--json", action="store_true", help="print the reports as one JSON object")
    parser.set_defaults(handler=_cmd_maintain)
    return parser


def config_table(path: str) -> Any:
    """The pyiceberg table a config's Iceberg target (else source) names.

    Raises:
        ConfigError: If the config has no Iceberg table.
        TableNotFound: If the table or its SQLite catalog file doesn't exist.
    """
    from local_data_platform.etl import load_config
    from local_data_platform.pipeline.builders import iceberg_from_config

    config = load_config(path)
    for section in ("target", "source"):
        block = config.metadata[section]
        if str(block.get("format", "")).strip().upper() == "ICEBERG":
            return iceberg_from_config(config, section, must_exist=True).table()
    raise ConfigError(f"config {config.identifier} has no Iceberg table: its source is {config.source['format']!r} "
                      f"and its target is {config.target['format']!r}")


def _size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"  # pragma: no cover


def _print_expiry(report: dict[str, Any], days: float) -> None:
    verb = "would expire" if report["dry_run"] else "expired"
    _out(f"Snapshots older than {days:g} days ({report['older_than']}), keeping the last {report['retain_last']} "
         f"and every idempotency key since {report['protect_keys_since']}:")
    ids = report["expired_snapshot_ids"]
    _out(f"  {verb} {len(ids)} of {report['snapshots_before']} snapshots"
         + (": " + ", ".join(str(i) for i in ids) if ids else ""))
    kept = {reason: len(ids) for reason, ids in report["protected_snapshot_ids"].items() if ids}
    if kept:
        _out("  kept by: " + ", ".join(f"{reason} {count}" for reason, count in kept.items()))


def _print_orphans(report: dict[str, Any]) -> None:
    verb = "would delete" if report["dry_run"] else "deleted"
    count = len(report["orphans"]) if report["dry_run"] else len(report["deleted"])
    _out(f"Orphan files older than {report['older_than_hours']:g} hours under {report['location']}:")
    _out(f"  {verb} {count} files ({_size(report['orphan_bytes'])})")
    for path in report["orphans"]:
        _out(f"    {path}")
    for failure in report["failed"]:
        _out(f"  failed: {failure['path']}: {failure['error']}")
    stats = report["stats"]
    _out(f"  listed {stats['listed']}: referenced {stats['referenced']}, too recent {stats['recent']}, "
         f"hidden {stats['hidden']}, other tables {stats['other_tables']}")


def run_maintenance(config: str, *, expire: float | None = None, orphans: bool = False, apply: bool = False,
                    retain_last: int | None = None, orphan_age_hours: float | None = None,
                    keep_metadata_versions: int | None = None) -> dict[str, Any]:
    """Maintain the Iceberg table of a config, as ``ldp maintain`` and the maintain task of a workflow do.

    Args:
        config: Path to a JSON dataset config. Its Iceberg target, else its source, is the table.
        expire: Expire snapshots committed more than this many days ago. With neither ``expire`` nor ``orphans``, both
            run and ``expire`` is :data:`DEFAULT_EXPIRE_DAYS`.
        orphans: Find, and with ``apply`` delete, files that no metadata references.
        apply: Make the changes. Everything is a dry run without it.
        retain_last: Keep ``main``'s last N snapshots. The ``DEFAULT_RETAIN_LAST`` of
            :mod:`~local_data_platform.maintenance.snapshots` when ``None``.
        orphan_age_hours: Only files older than this are orphans. The module's default when ``None``.
        keep_metadata_versions: Files reachable from this many previous metadata versions are kept. The module's default
            when ``None``.

    Returns:
        ``{"table", "dry_run"}`` and, for each part that ran, ``"expire"`` (the report of ``expire_snapshots``) and
        ``"orphans"`` (the report of ``remove_orphans``). :func:`maintenance_failed` says whether a file could not be
        deleted.

    Raises:
        ConfigError: If the config has no Iceberg table.
        TableNotFound: If the table or its SQLite catalog file doesn't exist.
    """
    from local_data_platform.maintenance.orphans import (
        DEFAULT_KEEP_METADATA_VERSIONS,
        DEFAULT_OLDER_THAN_HOURS,
        remove_orphans,
    )
    from local_data_platform.maintenance.snapshots import DEFAULT_RETAIN_LAST, expire_snapshots

    run_expire, run_orphans = expire is not None, bool(orphans)
    if not run_expire and not run_orphans:
        run_expire = run_orphans = True
    days = expire if expire is not None else DEFAULT_EXPIRE_DAYS
    dry_run = not apply

    table = config_table(config)
    result: dict[str, Any] = {"table": ".".join(table.name()), "dry_run": dry_run}
    if run_expire:
        result["expire"] = expire_snapshots(table, older_than=dt.timedelta(days=days),
                                            retain_last=DEFAULT_RETAIN_LAST if retain_last is None else retain_last,
                                            dry_run=dry_run)
    if run_orphans:
        result["orphans"] = remove_orphans(
            table, older_than_hours=DEFAULT_OLDER_THAN_HOURS if orphan_age_hours is None else orphan_age_hours,
            dry_run=dry_run,
            keep_metadata_versions=(DEFAULT_KEEP_METADATA_VERSIONS if keep_metadata_versions is None
                                    else keep_metadata_versions))
    return result


def run_system_maintenance(config: str, *, keep_days: int | None = None, expire: float | None = None,
                           orphans: bool = False, apply: bool = False, retain_last: int | None = None,
                           orphan_age_hours: float | None = None, keep_metadata_versions: int | None = None,
                           now: dt.datetime | None = None) -> dict[str, Any]:
    """Trim the ``_ldp`` history tables of the catalog of a config, as ``ldp maintain --system`` does.

    Args:
        config: Path to a JSON dataset config. The tables are in the catalog of its ``iceberg`` sink, else the catalog
            of its Iceberg target or source, and in the namespace of the sink, ``_ldp`` unless the sink names another.
        keep_days: Keep this many days of rows, counted in whole UTC days. ``DEFAULT_KEEP_DAYS`` of
            :mod:`~local_data_platform.maintenance.system` when ``None``.
        expire: Expire snapshots committed more than this many days ago. With neither ``expire`` nor ``orphans``, both
            run and ``expire`` is :data:`DEFAULT_EXPIRE_DAYS`.
        orphans: Find, and with ``apply`` delete, files that no metadata references.
        apply: Make the changes. Everything is a dry run without it.
        retain_last: Keep ``main``'s last N snapshots of each table. The default of ``ldp maintain`` when ``None``.
        orphan_age_hours: Only files older than this are orphans. The default of ``ldp maintain`` when ``None``.
        keep_metadata_versions: Files reachable from this many previous metadata versions are kept. The default of
            ``ldp maintain`` when ``None``.
        now: The moment that ``keep_days`` counts back from, ``None`` for the clock.

    Returns:
        The report of :func:`~local_data_platform.maintenance.system.maintain_system_tables` with the ``catalog`` (its
        identifier, or ``None``) and the settings that were used, ``keep_days``, ``expire_days``, ``retain_last``,
        ``orphan_age_hours`` and ``keep_metadata_versions``. A part that did not run has ``None`` for its setting.
        :func:`maintenance_failed` says whether a file could not be deleted.

    Raises:
        ConfigError: If the config has no Iceberg catalog.
        TableNotFound: If its catalog is a SQLite file that does not exist. Nothing is created.
    """
    from local_data_platform.catalog.provider import create_catalog, require_catalog_database
    from local_data_platform.etl import load_config
    from local_data_platform.events import run_source
    from local_data_platform.maintenance.orphans import DEFAULT_KEEP_METADATA_VERSIONS, DEFAULT_OLDER_THAN_HOURS
    from local_data_platform.maintenance.snapshots import DEFAULT_RETAIN_LAST
    from local_data_platform.maintenance.system import DEFAULT_KEEP_DAYS, maintain_system_tables, trim_cutoff

    run_expire, run_orphans = expire is not None, bool(orphans)
    if not run_expire and not run_orphans:
        run_expire = run_orphans = True
    settings = {
        "keep_days": DEFAULT_KEEP_DAYS if keep_days is None else keep_days,
        "expire_days": (DEFAULT_EXPIRE_DAYS if expire is None else expire) if run_expire else None,
        "retain_last": DEFAULT_RETAIN_LAST if retain_last is None else retain_last,
        "orphan_age_hours": ((DEFAULT_OLDER_THAN_HOURS if orphan_age_hours is None else orphan_age_hours)
                             if run_orphans else None),
        "keep_metadata_versions": (DEFAULT_KEEP_METADATA_VERSIONS if keep_metadata_versions is None
                                   else keep_metadata_versions),
    }
    loaded = load_config(config)
    source = run_source(loaded)
    if source.catalog is None:
        raise ConfigError(f"config {loaded.identifier} has no Iceberg catalog, so it has no _ldp tables: its source is "
                          f"{loaded.source['format']!r} and its target is {loaded.target['format']!r}")
    require_catalog_database(source.catalog, loaded.base_dir)
    result = maintain_system_tables(
        create_catalog(source.catalog, base_dir=loaded.base_dir), namespace=source.namespace,
        cutoff=trim_cutoff(settings["keep_days"], now), expire_days=settings["expire_days"],
        retain_last=settings["retain_last"], orphan_age_hours=settings["orphan_age_hours"],
        keep_metadata_versions=settings["keep_metadata_versions"], dry_run=not apply)
    return {**result, "catalog": source.catalog.get("identifier"), **settings}


def maintenance_failed(result: dict[str, Any]) -> bool:
    """Whether a result of :func:`run_maintenance` or :func:`run_system_maintenance` has an undeletable file."""
    entries = [result, *result.get("tables", {}).values()]
    return any(entry.get("orphans", {}).get("failed") for entry in entries)


def _print_system(result: dict[str, Any]) -> None:
    dry_run = result["dry_run"]
    tables = result["tables"]
    present = {identifier: entry for identifier, entry in tables.items() if entry["exists"]}
    width = max(len(identifier) for identifier in tables) + 2
    where = f" of catalog {result['catalog']}" if result["catalog"] else ""
    _out(f"Maintenance of the {result['namespace']} tables{where} "
         f"({'dry run; pass --apply to make the changes' if dry_run else 'applied'})")
    _out(f"Rows before {result['cutoff']}, the start of the day {result['keep_days']} days back:")
    for identifier, entry in tables.items():
        label = f"  {identifier:<{width}}"
        if not entry["exists"]:
            _out(label + "not there")
            continue
        delete = entry["delete"]
        if not delete["rows_deleted"]:
            _out(label + f"nothing to delete, {delete['rows_before']} rows")
            continue
        span = f", {delete['oldest'][:10]} to {delete['newest'][:10]}" if delete["oldest"] else ""
        _out(label + f"{'would delete' if dry_run else 'deleted'} {delete['rows_deleted']} of {delete['rows_before']} "
                     f"rows in {delete['files_deleted']} files{span}")
    if result["expire_days"] is not None and present:
        _out(f"Snapshots older than {result['expire_days']:g} days, keeping the last {result['retain_last']}:")
        for identifier, entry in present.items():
            report = entry["expire"]
            _out(f"  {identifier:<{width}}{'would expire' if dry_run else 'expired'} "
                 f"{len(report['expired_snapshot_ids'])} of {report['snapshots_before']} snapshots")
        if dry_run and any(entry["delete"]["rows_deleted"] for entry in present.values()):
            _out("  a delete is a snapshot of its own, so a real run can expire one more snapshot of a table than "
                 "this lists")
    if result["orphan_age_hours"] is not None and present:
        _out(f"Orphan files older than {result['orphan_age_hours']:g} hours:")
        for identifier, entry in present.items():
            report = entry["orphans"]
            count = len(report["orphans"]) if dry_run else len(report["deleted"])
            _out(f"  {identifier:<{width}}{'would delete' if dry_run else 'deleted'} {count} files "
                 f"({_size(report['orphan_bytes'])})")
            for failure in report["failed"]:
                _out(f"    failed: {failure['path']}: {failure['error']}")
        if dry_run and any(entry.get("expire", {}).get("expired_snapshot_ids") for entry in present.values()):
            _out("  files only the expiring snapshots reference show up once the expiry is applied and "
                 f"{result['keep_metadata_versions']} newer metadata versions exist")


def _cmd_maintain_system(args: argparse.Namespace) -> int:
    result = run_system_maintenance(args.config, keep_days=args.keep_days, expire=args.expire, orphans=args.orphans,
                                    apply=args.apply, retain_last=args.retain_last,
                                    orphan_age_hours=args.orphan_age_hours,
                                    keep_metadata_versions=args.keep_metadata_versions)
    if args.json:
        _out(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_system(result)
    return 1 if maintenance_failed(result) else 0


def _cmd_maintain(args: argparse.Namespace) -> int:
    if args.system:
        return _cmd_maintain_system(args)
    if args.keep_days is not None:
        raise ConfigError("--keep-days only applies with --system")
    result = run_maintenance(args.config, expire=args.expire, orphans=args.orphans, apply=args.apply,
                             retain_last=args.retain_last, orphan_age_hours=args.orphan_age_hours,
                             keep_metadata_versions=args.keep_metadata_versions)
    run_expire, run_orphans = "expire" in result, "orphans" in result
    days = args.expire if args.expire is not None else DEFAULT_EXPIRE_DAYS
    dry_run = result["dry_run"]
    failed = maintenance_failed(result)

    if args.json:
        _out(json.dumps(result, indent=2, sort_keys=True))
        return 1 if failed else 0
    mode = "dry run; pass --apply to make the changes" if dry_run else "applied"
    _out(f"Maintenance of Iceberg table {result['table']} ({mode})")
    if run_expire:
        _print_expiry(result["expire"], days)
    if run_orphans:
        _print_orphans(result["orphans"])
        if dry_run and run_expire and result["expire"]["expired_snapshot_ids"]:
            _out("  files only the expiring snapshots reference show up once the expiry is applied and "
                 f"{args.keep_metadata_versions} newer metadata versions exist")
    return 1 if failed else 0


__all__ = ["DEFAULT_EXPIRE_DAYS", "add_cli", "config_table", "maintenance_failed", "run_maintenance",
           "run_system_maintenance"]
