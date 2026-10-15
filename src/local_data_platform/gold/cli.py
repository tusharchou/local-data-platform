"""The ``ldp gold`` commands.

``ldp gold sync CONFIG [--dbt-manifest FILE] [--dry-run] [--json]`` writes the contract of a config into its existing
table, and ``ldp gold build DIR [--json]`` builds the gold tables of a folder in order. ``cli.py`` wires this in with
:func:`add_cli`. Heavy imports happen inside the command.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any


def add_cli(subparsers: Any, parents: Sequence[Any] = ()) -> Any:
    """Register ``ldp gold sync`` on the ``ldp`` sub-command parser.

    Args:
        subparsers: The object ``argparse.ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers for shared options such as ``-v``.

    Returns:
        The ``gold`` parser.
    """
    parents = list(parents)
    gold = subparsers.add_parser(
        "gold", parents=parents, help="work with the gold layer: tables that carry their contract",
        description="A gold table has an owner, a meaning, a grain and a description of every column, written in the "
                    "metadata.docs block of its config and into the Iceberg table when a run publishes.",
        epilog="Exit status is 0 on success, 1 on an error and 3 when the contract of a gold table does not hold.")
    actions = gold.add_subparsers(dest="gold_command", metavar="COMMAND")

    def usage(args: argparse.Namespace) -> int:
        gold.print_help()
        return 1

    gold.set_defaults(handler=usage)

    sync = actions.add_parser(
        "sync", parents=parents, help="write the contract of a config into its existing table, without writing data",
        description="Write the metadata.docs of CONFIG into the Iceberg table that its target names: the table "
                    "properties comment, ldp.owner, ldp.layer, ldp.grain and ldp.primary-key, and the doc of each "
                    "column. It is one metadata-only commit and adds no snapshot, and it commits nothing when the "
                    "table already has the contract. It checks a gold table first, like a run does. Use it for a "
                    "table that exists and has no contract yet, or whose docs changed with no new data.",
        epilog="Exit status is 0 on success, 1 on an error, such as a table that does not exist, and 3 when the "
               "contract of a gold table does not hold for its columns. Example: ldp gold sync "
               "config/gold_sales.json --dbt-manifest target/manifest.json")
    sync.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config with an Iceberg target")
    sync.add_argument("--dbt-manifest", metavar="FILE", dest="dbt_manifest",
                      help="a dbt manifest.json, for a table that dbt built outside LDP. The model that builds the "
                           "table gives the description, the column descriptions, the owner and grain of its meta, "
                           "and the tags, and the config's metadata.docs is written over it. dbt is not imported")
    sync.add_argument("--dry-run", action="store_true", dest="dry_run",
                      help="say what would change, and change nothing")
    sync.add_argument("--json", action="store_true", help="print the result as JSON")
    sync.set_defaults(handler=_cmd_sync)

    build = actions.add_parser(
        "build", parents=parents, help="build the gold tables of a folder in order, and check the ones built elsewhere",
        description="Find every config in DIR whose metadata.docs.layer is gold, order them by their inputs, since a "
                    "gold table may read another, and build them one at a time. A config whose source has inputs is "
                    "built, which runs its pipeline. A gold config with no inputs is built elsewhere, by dbt, by Spark "
                    "or by a script, so it is not built. It is checked, which means that the table has to exist and "
                    "its quality checks run on the current snapshot and write nothing. The build stops at the first "
                    "table that a gate blocks or that fails, and it prints one line for each table. The runs are "
                    "recorded in the _ldp namespace of each catalog, through the iceberg sink, unless a config "
                    "names sinks of its own.",
        epilog="Exit status is 0 when every table was built or checked, 3 when a gate held a table back, and 1 on any "
               "other failure. Example: ldp gold build ldp_demo/robots")
    build.add_argument("folder", metavar="DIR", help="a folder of dataset configs")
    build.add_argument("--json", action="store_true", help="print the result as JSON when the build is over")
    build.set_defaults(handler=_cmd_build)
    return gold


def _plural(number: int, noun: str, plural: str | None = None) -> str:
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def _cmd_sync(args: argparse.Namespace) -> int:
    from local_data_platform.gold.sync import sync_config

    done = sync_config(args.config, dbt_manifest=args.dbt_manifest, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(done.to_dict(), indent=2))
        return 0
    result = done.result
    if result.ignored:
        print(f"ldp: warning: metadata.docs.columns describes {_plural(len(result.ignored), 'column')} that "
              f"{done.table} does not have: {', '.join(result.ignored)}", file=sys.stderr)
    if not result.changed:
        print(f"{done.config}: {done.table} already has its contract, with {_plural(result.documented, 'column')} "
              "described. Nothing was written.")
    else:
        verb = "would write" if done.dry_run else "wrote"
        properties = _plural(len(result.properties), "property", "properties")
        print(f"{done.config}: {verb} the contract of {done.table}, {properties} "
              f"and {_plural(len(result.columns), 'column doc')}. No data was written.")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    from local_data_platform.gold.build import build_gold, find_gold, order_gold

    width = max((len(item.config.identifier) for item in order_gold(find_gold(args.folder))), default=0)

    def show(outcome: Any) -> None:
        rows = "-" if outcome.rows is None else f"{outcome.rows} rows"
        snapshot = "-" if outcome.snapshot_id is None else f"snapshot {outcome.snapshot_id}"
        print(f"{outcome.gold.config.identifier:<{width}}  {outcome.status:<8}  {rows:>12}  {snapshot}  "
              f"{outcome.gate}".rstrip(), flush=True)

    result = build_gold(args.folder, on_table=None if args.json else show)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        counts = result.counts()
        print(f"gold build {result.folder}: {counts['built']} built, {counts['checked']} checked, "
              f"{counts['blocked']} blocked, {counts['failed']} failed, {counts['not run']} not run in "
              f"{result.duration_s:.1f}s")
    stopped = result.stopped_by
    if stopped is not None and not args.json:
        print(f"ldp: {stopped.gold.config.identifier} {stopped.status}", file=sys.stderr)
        for line in (stopped.error or "").splitlines():
            print(f"  {line}", file=sys.stderr)
    return result.exit_code
