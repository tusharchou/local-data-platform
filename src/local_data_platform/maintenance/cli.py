"""The ``ldp maintain`` command: ``ldp maintain CONFIG [--expire DAYS] [--orphans] [--apply]``.

Everything is a dry run unless ``--apply`` is given. With neither ``--expire`` nor ``--orphans`` both run,
expiring snapshots older than :data:`DEFAULT_EXPIRE_DAYS` days.

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

    parser = subparsers.add_parser(
        "maintain", parents=list(parents),
        help="expire old snapshots and find orphan files (a dry run unless --apply)",
        description="Expire snapshots of the config's Iceberg target (or source) while keeping every idempotency "
                    "key inside the table's horizon, and find files under the table location that no snapshot "
                    "references. Nothing changes unless --apply is given. With neither --expire nor --orphans, "
                    f"both run and --expire defaults to {DEFAULT_EXPIRE_DAYS:g} days.",
    )
    parser.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    parser.add_argument("--expire", metavar="DAYS", type=_non_negative,
                        help="expire snapshots committed more than DAYS days ago")
    parser.add_argument("--orphans", action="store_true",
                        help="find files under the table location that no metadata references")
    parser.add_argument("--apply", action="store_true", help="make the changes; without it everything is a dry run")
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


def _cmd_maintain(args: argparse.Namespace) -> int:
    from local_data_platform.maintenance.orphans import remove_orphans
    from local_data_platform.maintenance.snapshots import expire_snapshots

    run_expire = args.expire is not None
    run_orphans = bool(args.orphans)
    if not run_expire and not run_orphans:
        run_expire = run_orphans = True
    days = args.expire if args.expire is not None else DEFAULT_EXPIRE_DAYS
    dry_run = not args.apply

    table = config_table(args.config)
    result: dict[str, Any] = {"table": ".".join(table.name()), "dry_run": dry_run}
    if run_expire:
        result["expire"] = expire_snapshots(table, older_than=dt.timedelta(days=days), retain_last=args.retain_last,
                                            dry_run=dry_run)
    if run_orphans:
        result["orphans"] = remove_orphans(table, older_than_hours=args.orphan_age_hours, dry_run=dry_run,
                                           keep_metadata_versions=args.keep_metadata_versions)
    failed = bool(result.get("orphans", {}).get("failed"))

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


__all__ = ["DEFAULT_EXPIRE_DAYS", "add_cli", "config_table"]
