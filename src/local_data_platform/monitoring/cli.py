"""``ldp monitor``: check the tables of a config, or of a folder of configs, against their SLA.

::

    ldp monitor CONFIG_OR_FOLDER [--json] [--now ISO-8601]

Each monitor prints one line. The results are recorded in the history of the monitors (see
:mod:`local_data_platform.monitoring.history`). The command reads the metadata of tables and the history of the runs and
of the monitors, and a metric monitor runs one aggregate query on a table. It writes to no table and creates no catalog.
Its exit status is 0 when every monitor is ``ok`` or ``warn``, 2 when at least one is ``error``, and 1 when ``ldp``
itself fails, such as a config that cannot be read.
"""

import argparse
import datetime as dt
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.monitoring.results import STATUSES, MonitorResult, format_moment


def _moment(text: str) -> Any:
    from local_data_platform.lake import parse_moment

    try:
        return parse_moment(text)
    except ConfigError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _is_a_config(file: Path) -> bool:
    """Whether a JSON file of a folder is a dataset config, so that a file of another kind or sort is left alone.

    A file that cannot be read as JSON counts as a config, so that it is reported. A table must not leave the monitors
    without a word because of a typo.
    """
    from local_data_platform.spec import DEFAULT_KIND, kind_of_file

    if kind_of_file(file) not in (DEFAULT_KIND, None):
        return False
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return isinstance(data, dict) and "identifier" in data and "metadata" in data


def _load(target: str) -> tuple[list[tuple[Path, Any]], list[tuple[Path, str]]]:
    """The dataset configs of a file or a folder, and the files that could not be read as one."""
    from local_data_platform.config import Config
    from local_data_platform.paths import resolve_path

    path = resolve_path(target)
    if path.is_dir():
        files = [file for file in sorted(path.glob("*.json")) if file.is_file() and _is_a_config(file)]
        if not files:
            raise ConfigError(f"no dataset configs in {path}")
    elif path.is_file():
        files = [path]
    else:
        raise ConfigError(f"config path not found: {path}")
    configs, failures = [], []
    for file in files:
        try:
            configs.append((file, Config.from_json(file)))
        except ConfigError as error:
            failures.append((file, str(error)))
    return configs, failures


def _counts(results: Sequence[MonitorResult]) -> str:
    counts = [f"{sum(1 for result in results if result.status == status)} {status}" for status in STATUSES]
    return ", ".join(text for text in counts if not text.startswith("0 "))


def _cmd_monitor(args: argparse.Namespace) -> int:
    from local_data_platform.cli import EXIT_ERROR, EXIT_OK, EXIT_SLA, format_table
    from local_data_platform.monitoring.evaluation import evaluate
    from local_data_platform.monitoring.history import record

    configs, failures = _load(args.config)
    now = args.now if args.now is not None else dt.datetime.now(dt.timezone.utc)
    evaluated: list[tuple[Any, list[MonitorResult]]] = []
    without_sla: list[Path] = []
    for file, config in configs:
        if config.sla is None:
            without_sla.append(file)
            continue
        try:
            evaluated.append((config, evaluate(config, now)))
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
    for file, message in failures:
        print(f"ldp: {file.name}: {message}", file=sys.stderr)
    if failures:
        return EXIT_ERROR
    return EXIT_SLA if any(result.status == "error" for result in results) else EXIT_OK


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> None:
    """Register ``ldp monitor`` on an ``argparse`` subparsers object.

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
                    "an iceberg sink and its catalog exists, and in .ldp/monitor.jsonl next to the config otherwise.",
        epilog="Exit status is 0 when every monitor is ok or warn, 2 when at least one is error, and 1 when ldp itself "
               "fails, such as a config that cannot be read.")
    monitor.add_argument("config", metavar="CONFIG", help="a JSON dataset config, or a folder of them")
    monitor.add_argument("--json", action="store_true", help="print the results as JSON")
    monitor.add_argument("--now", type=_moment, metavar="TIME",
                         help="measure at this moment, as ISO 8601 such as 2026-07-31T23:59:59Z, and not at the "
                              "current time. A time with no offset is UTC")
    monitor.set_defaults(handler=_cmd_monitor)
