"""Metrics: numbers that a business reads, defined once and computed the same way by every reader.

A metrics file, with ``"kind": "Metrics"``, defines semantic models over gold tables and the metrics made of their
measures, with the terms of MetricFlow. This package reads and checks those files, compiles a query into DuckDB SQL with
bound parameters, and runs it on the gold tables as the current release holds them::

    from local_data_platform import metrics

    table = metrics.query("config", "revenue,avg_ticket", by="cuisine", grain="month",
                          filters=["city=Pune"], start="2026-01-01", end="2026-03-31")

The modules are ``model`` (the definitions, their checks without a table, and the JSON Schema), ``load`` (the files of
a folder), ``check`` (the definitions against the gold tables), ``sql`` (the pieces of SQL), ``compiler`` (a query as
SQL), ``runner`` (running it on a release), ``render`` (showing and writing the result) and ``cli`` (``ldp metrics``).

The names below are loaded when they are first used, so that ``local_data_platform.spec``, which hands the ``Metrics``
kind to :mod:`~local_data_platform.metrics.model`, does not import pyarrow or DuckDB.
"""

import importlib
from typing import Any

_LAZY = {
    "AGGREGATIONS": "model",
    "Definitions": "load",
    "Dimension": "model",
    "FORMATS": "model",
    "FolderCheck": "check",
    "GRAINS": "model",
    "CompiledQuery": "compiler",
    "Measure": "model",
    "Metric": "model",
    "Result": "runner",
    "SemanticModel": "model",
    "TimeDimension": "model",
    "check_definitions": "check",
    "compile_for": "runner",
    "compile_query": "compiler",
    "load_metrics": "load",
    "metrics_files": "load",
    "metrics_schema": "model",
    "query": "runner",
    "read_definitions": "load",
    "run": "runner",
    "validate_folder": "check",
    "validate_metrics": "model",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


__all__ = sorted(_LAZY)
