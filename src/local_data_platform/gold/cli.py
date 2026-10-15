"""The ``ldp gold`` commands.

``ldp gold sync CONFIG [--dbt-manifest FILE] [--dry-run] [--json]`` writes the contract of a config into its existing
table, ``ldp gold build DIR [--no-release] [--json]`` builds the gold tables of a folder in order and cuts a release,
and ``ldp gold releases DIR [--show RELEASE] [--json]`` lists the releases. ``cli.py`` wires this in with
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
                    "names sinks of its own. When every table was built or checked it cuts a release, which names "
                    "the snapshot of each gold table, so that a reader sees tables that agree. A build that a gate "
                    "blocks cuts none, and the last release stays current.",
        epilog="Exit status is 0 when every table was built or checked, 3 when a gate held a table back, and 1 on any "
               "other failure, a release that could not be cut included. Example: ldp gold build ldp_demo/robots")
    build.add_argument("folder", metavar="DIR", help="a folder of dataset configs")
    build.add_argument("--no-release", action="store_true", dest="no_release",
                       help="build the tables without cutting a release")
    build.add_argument("--json", action="store_true", help="print the result as JSON when the build is over")
    build.set_defaults(handler=_cmd_build)

    releases = actions.add_parser(
        "releases", parents=parents, help="list the releases of the gold tables of a folder, newest first",
        description="List the releases that ldp gold build cut for the gold tables of DIR, newest first. A release "
                    "names the snapshot of each gold table, so the tables of one release agree. With --show it lists "
                    "the tables of one release, each with its snapshot, the dataset version that pins it, the run "
                    "that built it and its rows.",
        epilog="Example: ldp gold releases ldp_demo/robots --show r3")
    releases.add_argument("folder", metavar="DIR", help="a folder of dataset configs")
    releases.add_argument("--show", metavar="RELEASE", help="list the tables of this release, such as r3")
    releases.add_argument("--json", action="store_true", help="print the result as JSON")
    releases.set_defaults(handler=_cmd_releases)
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


def _describe_release(result: Any) -> str:
    """What the build did about its release, and which release readers see."""
    outcome = result.release
    release = outcome.release
    seen = f"Readers still see {release.name}." if release is not None else "There is no release yet."
    if outcome.error:
        return f"no release cut. {seen}"
    if outcome.cut:
        return (f"release {release.name} cut with {_plural(len(release.tables), 'table')} and "
                f"{_plural(release.rows, 'row')} in {release.namespace}")
    if result.stopped_by is not None:
        return f"no release cut, because {outcome.note}. {seen}"
    return f"no release cut, because the tables are at the snapshots of {release.name}. Readers see {release.name}."


def _cmd_build(args: argparse.Namespace) -> int:
    from local_data_platform.gold.build import build_gold, find_gold, order_gold
    from local_data_platform.gold.release import ReleaseError

    width = max((len(item.config.identifier) for item in order_gold(find_gold(args.folder))), default=0)

    def show(outcome: Any) -> None:
        rows = "-" if outcome.rows is None else f"{outcome.rows} rows"
        snapshot = "-" if outcome.snapshot_id is None else f"snapshot {outcome.snapshot_id}"
        print(f"{outcome.gold.config.identifier:<{width}}  {outcome.status:<8}  {rows:>12}  {snapshot}  "
              f"{outcome.gate}".rstrip(), flush=True)

    try:
        result = build_gold(args.folder, on_table=None if args.json else show, release=not args.no_release)
    except ReleaseError as error:
        raise ReleaseError(f"{error}. Build with --no-release to build without a release") from None
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        counts = result.counts()
        print(f"gold build {result.folder}: {counts['built']} built, {counts['checked']} checked, "
              f"{counts['blocked']} blocked, {counts['failed']} failed, {counts['not run']} not run in "
              f"{result.duration_s:.1f}s")
        if result.release is not None:
            print(_describe_release(result))
    stopped = result.stopped_by
    if stopped is not None and not args.json:
        print(f"ldp: {stopped.gold.config.identifier} {stopped.status}", file=sys.stderr)
        for line in (stopped.error or "").splitlines():
            print(f"  {line}", file=sys.stderr)
    if result.release is not None and result.release.error and not args.json:
        print("ldp: the release was not cut", file=sys.stderr)
        for line in result.release.error.splitlines():
            print(f"  {line}", file=sys.stderr)
    return result.exit_code


def _utc(moment: Any) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _cmd_releases(args: argparse.Namespace) -> int:
    from local_data_platform.cli import format_table
    from local_data_platform.gold import release as releases

    scope = releases.folder_scope(args.folder)
    known = releases.releases_in(scope)
    if args.show is not None:
        release = releases.release_in(scope, args.show)
        if args.json:
            print(json.dumps(release.to_dict(), indent=2))
            return 0
        by = f", cut by workflow {release.workflow['identifier']} run {release.workflow['run_id']}" \
            if release.workflow else ""
        window = f", for the window {release.window[0]}/{release.window[1]}" if release.window else ""
        print(f"Release {release.name} of {release.namespace}, cut {_utc(release.created_at)} UTC{by}{window}, "
              f"with {_plural(len(release.tables), 'table')}:")
        print(format_table([{"table": item.table, "status": item.status, "rows": item.rows,
                             "snapshot_id": item.snapshot_id, "dataset": f"{item.dataset}@v{item.dataset_version}",
                             "run_id": item.run_id or "-"} for item in release.tables]))
        return 0
    if args.json:
        print(json.dumps({"namespace": scope.namespace, "warehouse": str(scope.root),
                          "current": known[0].name if known else None,
                          "releases": [release.to_dict() for release in known]}, indent=2))
        return 0
    if not known:
        print(f"No releases of {scope} yet. ldp gold build {args.folder} cuts the first one.")
        return 0
    print(f"Releases of {scope}, newest first. {known[0].name} is current.")
    rows = []
    for release in known:
        row = {"release": release.name, "cut_utc": _utc(release.created_at), "tables": len(release.tables),
               "rows": release.rows}
        if any(item.workflow for item in known):
            row["workflow"] = release.workflow["identifier"] if release.workflow else "-"
        if any(item.window for item in known):
            row["window"] = "/".join(release.window) if release.window else "-"
        rows.append(row)
    print(format_table(rows))
    return 0
