"""``ldp monitor`` and ``ldp status``: check the tables of a config, or of a folder of configs, against their SLA.

::

    ldp monitor CONFIG_OR_FOLDER [--json] [--now ISO-8601]
    ldp status CONFIG_OR_FOLDER [--json] [--html FILE] [--now ISO-8601]

Each monitor prints one line. The results are recorded in the history of the monitors (see
:mod:`local_data_platform.monitoring.history`). The command reads the metadata of tables and the history of the runs
and of the monitors, and a metric monitor runs one aggregate query on a table. It writes to no table and creates no
catalog. A config that lists channels in ``metadata.observability.alerts`` is told when a monitor changes state and
when it recovers (see :mod:`local_data_platform.monitoring.alerts`), after the results are recorded, and a channel that
is down is a warning and no failure. Its exit status is 0 when every monitor is ``ok`` or ``warn``, 2 when at least one
is ``error``, and 1 when ``ldp`` itself fails, such as a config that cannot be read.

``ldp status`` prints one row for each config, the worst first, with the last run, the SLA, the failed checks of the
last 7 days and the open alerts (see :mod:`local_data_platform.monitoring.status`). It evaluates the monitors in memory
and records and sends nothing, so its exit status is 0 unless ``ldp`` itself fails.
"""

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.monitoring.configs import load_configs
from local_data_platform.monitoring.results import STATUSES, MonitorResult, format_moment


def _moment(text: str) -> Any:
    from local_data_platform.lake import parse_moment

    try:
        return parse_moment(text)
    except ConfigError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _counts(results: Sequence[MonitorResult]) -> str:
    counts = [f"{sum(1 for result in results if result.status == status)} {status}" for status in STATUSES]
    return ", ".join(text for text in counts if not text.startswith("0 "))


def _cmd_monitor(args: argparse.Namespace) -> int:
    from local_data_platform.cli import EXIT_ERROR, EXIT_OK, EXIT_SLA, format_table
    from local_data_platform.monitoring.alerts import plan_alerts, send_alerts
    from local_data_platform.monitoring.evaluation import evaluate
    from local_data_platform.monitoring.history import read_results, record
    from local_data_platform.notify import notifier_for

    configs, failures = load_configs(args.config)
    now = args.now if args.now is not None else dt.datetime.now(dt.timezone.utc)
    evaluated: list[tuple[Any, list[MonitorResult]]] = []
    queued: list[tuple[Any, Any, list[MonitorResult]]] = []
    without_sla: list[Path] = []
    for file, config in configs:
        if config.sla is None:
            without_sla.append(file)
            continue
        try:
            notifier = notifier_for(config)
            found = evaluate(config, now)
            if notifier is not None:  # what the history says is read now, before this look is recorded
                queued.append((notifier, plan_alerts(notifier, found, read_results(config), now), found))
            evaluated.append((config, found))
        except Exception as error:  # noqa: BLE001 - one config that cannot be read must not hide the others
            failures.append((file, str(error) if isinstance(error, LDPError) else f"{type(error).__name__}: {error}"))
    results = [result for _, found in evaluated for result in found]

    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    elif results:
        print(f"Monitors at {format_moment(results[0].evaluated_at, seconds=True)}")
        print(format_table([{"pipeline": result.pipeline, "table": result.table_identifier, "monitor": result.monitor,
                             "status": result.status, "observed": result.observed or "-",
                             "expected": result.expected or "-", "details": result.details or "-"}
                            for result in results]))
        print(_counts(results))
    if not args.json:
        if len(without_sla) == 1:
            print(f"{without_sla[0].name} has no metadata.sla, so there is nothing to check")
        elif without_sla:
            names = ", ".join(file.name for file in without_sla)
            print(f"{len(without_sla)} configs have no metadata.sla, so there is nothing to check in {names}")
    written = record(evaluated)
    if written and not args.json:
        print(f"Results recorded in {', '.join(dict.fromkeys(written))}")
    told: dict[str, int] = {}
    for notifier, planned, found in queued:
        for delivery in send_alerts(notifier, planned, found, now).alerts:
            if delivery.ok:
                told[delivery.channel] = told.get(delivery.channel, 0) + 1
    if told and not args.json:
        print("Alerts sent to " + ", ".join(f"{channel} ({number})" for channel, number in told.items()))
    for file, message in failures:
        print(f"ldp: {file.name}: {message}", file=sys.stderr)
    if failures:
        return EXIT_ERROR
    return EXIT_SLA if any(result.status == "error" for result in results) else EXIT_OK


def _health_counts(rows: Sequence[Any]) -> str:
    from local_data_platform.monitoring.status import HEALTH

    counts = [f"{sum(1 for row in rows if row.health == health)} {health}" for health in HEALTH]
    return ", ".join(text for text in counts if not text.startswith("0 "))


def _history_line(rows: Sequence[Any]) -> str:
    """How far back the history goes, so that a history that was trimmed shows."""
    oldest = min((row.history_since for row in rows if row.history_since is not None), default=None)
    if oldest is None:
        return "No run or monitor history was found."
    without = sum(1 for row in rows if row.history_since is None and row.problem is None)
    line = f"History goes back to {format_moment(oldest, seconds=True)}, the oldest event of any pipeline here."
    return line + (f" {without} pipeline{'' if without == 1 else 's'} {'has' if without == 1 else 'have'} none."
                   if without else "")


def _write_page(out: str, text: str) -> Path:
    """Write the page whole or not at all, so that a reader never opens half of it."""
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    return target


def _cmd_status(args: argparse.Namespace) -> int:
    from local_data_platform.cli import EXIT_ERROR, EXIT_OK, format_table
    from local_data_platform.monitoring.status import rows
    from local_data_platform.monitoring.status_page import render

    now = args.now if args.now is not None else dt.datetime.now(dt.timezone.utc)
    found = rows(args.config, now)
    if args.json:
        print(json.dumps([row.to_dict() for row in found], indent=2))
    else:
        print(f"Status at {format_moment(now, seconds=True)}")
        print(format_table([row.cells() for row in found], right=("rows", "quality_7d", "open_alerts")))
        print(_health_counts(found))
        print(_history_line(found))
    if args.html is not None:
        text = render(found)
        target = _write_page(args.html, text)
        print(f"wrote {target}, {len(found)} pipeline{'' if len(found) == 1 else 's'}, "
              f"{len(text.encode('utf-8')) / 1024:.1f} KB. Open it as a page. It needs no network.",
              file=sys.stderr if args.json else sys.stdout)
    problems = [row for row in found if row.problem is not None]
    for row in problems:
        print(f"ldp: {Path(row.file).name}: {row.problem}", file=sys.stderr)
    return EXIT_ERROR if problems else EXIT_OK


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> None:
    """Register ``ldp monitor`` and ``ldp status`` on an ``argparse`` subparsers object.

    Args:
        subparsers: What ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (e.g. the CLI's ``-v``).
    """
    monitor = subparsers.add_parser(
        "monitor", parents=list(parents), help="check tables against their SLA",
        description="Check the SLA in the metadata.sla block of CONFIG, or of every dataset config of a folder, and "
                    "print one line for each monitor. It reads the metadata of the tables and the history of the "
                    "runs, and it runs one aggregate query for each metric of a metrics section. It writes to no "
                    "table and creates no catalog. The results are recorded in _ldp.monitor_results when a config has "
                    "an iceberg sink and its catalog exists, and in .ldp/monitor.jsonl next to the config otherwise. A "
                    "config with alert channels is told when a monitor changes state, and when it recovers.",
        epilog="Exit status is 0 when every monitor is ok or warn, 2 when at least one is error, and 1 when ldp itself "
               "fails, such as a config that cannot be read.")
    monitor.add_argument("config", metavar="CONFIG", help="a JSON dataset config, or a folder of them")
    monitor.add_argument("--json", action="store_true", help="print the results as JSON")
    monitor.add_argument("--now", type=_moment, metavar="TIME",
                         help="measure at this moment, as ISO 8601 such as 2026-07-31T23:59:59Z, and not at the "
                              "current time. A time with no offset is UTC")
    monitor.set_defaults(handler=_cmd_monitor)
    status = subparsers.add_parser(
        "status", parents=list(parents), help="one screen of health for every pipeline",
        description="Print one row for each dataset config of CONFIG or of a folder, the worst first. A row has the "
                    "last run, how long ago a run succeeded, the rows of the table, what the freshness, volume, "
                    "schema and metrics monitors say now, the checks that failed in the last 7 days, and the open "
                    "alerts. It evaluates the monitors in memory with the code of ldp monitor, and it records no "
                    "result and sends no alert. Each _ldp table is read once for all the configs that share a catalog.",
        epilog="Exit status is 0 unless ldp itself fails, such as a config that cannot be read. A monitor in error is "
               "not a failure here. ldp monitor reports it with 2.")
    status.add_argument("config", metavar="CONFIG", help="a JSON dataset config, or a folder of them")
    status.add_argument("--json", action="store_true", help="print the rows as JSON")
    status.add_argument("--html", metavar="FILE",
                        help="also write one self-contained page with the table, the monitors and the last 20 runs of "
                             "each pipeline. It needs no network and no script")
    status.add_argument("--now", type=_moment, metavar="TIME",
                        help="measure at this moment, as ISO 8601 such as 2026-07-31T23:59:59Z, and not at the "
                             "current time. A time with no offset is UTC")
    status.set_defaults(handler=_cmd_status)
