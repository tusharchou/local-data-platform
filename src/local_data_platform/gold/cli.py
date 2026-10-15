"""The ``ldp gold`` commands.

``ldp gold sync CONFIG [--dbt-manifest FILE] [--dry-run] [--json]`` writes the contract of a config into its existing
table. ``cli.py`` wires this in with :func:`add_cli`. Heavy imports happen inside the command.
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
