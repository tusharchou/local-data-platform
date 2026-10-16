"""The ``ldp gold`` commands.

``ldp gold sync CONFIG [--dbt-manifest FILE] [--dry-run] [--json]`` writes the contract of a config into its existing
table, ``ldp gold build DIR [--no-release] [--json]`` builds the gold tables of a folder in order and cuts a release,
``ldp gold releases DIR [--show RELEASE] [--json]`` lists the releases, ``ldp gold list DIR [--all-layers] [--json]
[--strict]`` lists the tables with what they mean and how they are doing, ``ldp gold show DIR TABLE [--json]
[--no-samples]`` describes one, and ``ldp gold docs DIR --out FILE [--format html|md]`` writes the catalog as a page.
``cli.py`` wires this in with :func:`add_cli`. Heavy imports happen inside the command.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
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

    listing = actions.add_parser(
        "list", parents=parents, help="list the gold tables of a folder with what they mean and how they are doing",
        description="One row for each gold table of DIR: the table, its description, owner and grain, its rows, "
                    "when its data last changed, the status of its last run, its last quality result and the release "
                    "it is in. "
                    "A table with no contract shows no description. The tables are the Iceberg targets of the dataset "
                    "configs of DIR, and a gold table is one whose metadata.docs.layer is gold, or whose table says "
                    "so, or that is in a namespace called gold. The run and quality status come from the _ldp "
                    "namespace, the same as ldp mcp reports. Nothing is written.",
        epilog="Exit status is 0 on success and 1 on an error, and with --strict also when a gold table lacks part of "
               "its contract. Example: ldp gold list ldp_demo/robots --strict")
    listing.add_argument("folder", metavar="DIR", help="a folder of dataset configs")
    listing.add_argument("--all-layers", action="store_true", dest="all_layers",
                         help="also list the bronze and silver tables, and the tables that have no layer")
    listing.add_argument("--json", action="store_true", help="print the result as JSON")
    listing.add_argument("--strict", action="store_true",
                         help="exit with 1 when a gold table has no description, no owner or no grain, or has a "
                              "column without a description. For CI")
    listing.set_defaults(handler=_cmd_list)

    show = actions.add_parser(
        "show", parents=parents, help="describe one table: its columns, runs, releases and a few rows",
        description="Everything the catalog knows about one table of DIR: its description, owner, grain and key, every "
                    "column with its type and description, the metrics defined on it, its last runs, the releases that "
                    "include it, a few sample rows and an ldp query line to read it. Nothing is written.",
        epilog="Example: ldp gold show ldp_demo/robots gold.gold_robot_daily")
    show.add_argument("folder", metavar="DIR", help="a folder of dataset configs")
    show.add_argument("table", metavar="TABLE",
                      help="a table such as gold.daily_sales, or its bare name when only one table has it")
    show.add_argument("--runs", type=_positive, default=5, metavar="N", help="how many of the last runs to list")
    show.add_argument("--no-samples", action="store_true", dest="no_samples",
                      help="leave the sample rows out, for a table whose rows must not be shown")
    show.add_argument("--json", action="store_true", help="print the result as JSON")
    show.set_defaults(handler=_cmd_show)

    docs = actions.add_parser(
        "docs", parents=parents, help="write the catalog as one page, or as Markdown for a MkDocs site",
        description="Write what ldp gold list and show print as one file. The HTML is a single self-contained "
                    "page with a search box. It has its CSS and its script inline, asks for nothing from the network "
                    "and works from a file or an e-mail. With --format md it is one Markdown file with a table of the "
                    "tables and a section for each. The sample rows of a table are in it, so leave them out with "
                    "--no-samples when the file will be shared and the rows must not be.",
        epilog="Example: ldp gold docs ldp_demo/robots --out catalog.html")
    docs.add_argument("folder", metavar="DIR", help="a folder of dataset configs")
    docs.add_argument("--out", required=True, metavar="FILE", help="where to write it, or - for standard output")
    docs.add_argument("--format", choices=("html", "md"), default="html", help="html (the default) or md")
    docs.add_argument("--all-layers", action="store_true", dest="all_layers",
                      help="also include the bronze and silver tables, and the tables that have no layer")
    docs.add_argument("--no-samples", action="store_true", dest="no_samples", help="leave the sample rows out")
    docs.set_defaults(handler=_cmd_docs)

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


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a number of at least 1, got {value}")
    return value


def _cmd_list(args: argparse.Namespace) -> int:
    from local_data_platform.gold import catalog as catalogs
    from local_data_platform.gold import render

    catalog = catalogs.collect(args.folder, all_layers=args.all_layers)
    print(json.dumps(catalog.to_dict(), indent=2) if args.json else render.list_text(catalog))
    lacking = catalog.lacking
    if args.strict and lacking:
        print(f"ldp: {catalog.lacking_summary}", file=sys.stderr)
        for entry in lacking:
            print(f"  {entry.lacks_text}", file=sys.stderr)
        return 1
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    from local_data_platform.gold import catalog as catalogs
    from local_data_platform.gold import render

    catalog = catalogs.collect(args.folder, only=args.table, detail=True, runs=args.runs,
                               sample=0 if args.no_samples else catalogs.DEFAULT_SAMPLE)
    entry = catalog.entries[0]
    print(json.dumps(entry.to_dict(), indent=2) if args.json else render.show_text(entry))
    return 0


def _cmd_docs(args: argparse.Namespace) -> int:
    from local_data_platform.gold import catalog as catalogs
    from local_data_platform.gold import render

    catalog = catalogs.collect(args.folder, all_layers=args.all_layers, detail=True,
                               sample=0 if args.no_samples else catalogs.DEFAULT_SAMPLE)
    text = render.html_page(catalog) if args.format == "html" else render.markdown_page(catalog)
    if args.out == "-":
        sys.stdout.write(text)
        return 0
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    kind = "page" if args.format == "html" else "Markdown file"
    note = " It needs no network." if args.format == "html" else ""
    print(f"wrote {target}, {_plural(len(catalog.entries), 'table')}, {len(text.encode('utf-8')) / 1024:.1f} KB. "
          f"Open it as a {kind}.{note}")
    return 0


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
