"""Reports: the queries and the blocks of a report defined in a file, and run against the gold tables.

A report file, with ``"kind": "Report"``, names the tables it reads, the queries it runs and the blocks it shows. This
package reads and checks those files, and runs their queries on the gold tables as the current release holds them, so a
report agrees with ``ldp metrics query`` and with the exports of the gold layer::

    from local_data_platform import report

    spec = report.load_report("reports/daily_sales.json")
    run = report.run_queries(spec, window="2026-07-31/2026-08-01")
    run.tables["by_restaurant"]          # a pyarrow.Table

The modules are ``spec`` (the definitions, their checks without a table, the SQL files and the JSON Schema), ``engine``
(running the queries), ``build`` (writing a report as a page, Markdown or JSON), ``health`` (the data health panel and
the freshness gate), ``site`` (a folder of reports built into one site) and ``cli`` (``ldp report``).

The names below are loaded when they are first used, so that ``local_data_platform.spec``, which hands the ``Report``
kind to :mod:`~local_data_platform.report.spec`, does not import pyarrow or DuckDB.
"""

import importlib
from typing import Any

_LAZY = {
    "BLOCK_TYPES": "spec",
    "BUILTIN_PARAMETERS": "spec",
    "Block": "spec",
    "Check": "engine",
    "KIND": "spec",
    "Parameter": "spec",
    "Query": "spec",
    "QueryRun": "engine",
    "Report": "spec",
    "ReportRun": "engine",
    "TableRead": "engine",
    "check_report": "engine",
    "describe_report": "spec",
    "load_report": "spec",
    "named_queries": "spec",
    "read_report": "spec",
    "report_files": "spec",
    "report_schema": "spec",
    "run_queries": "engine",
    "validate_report": "spec",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


__all__ = sorted(_LAZY)
