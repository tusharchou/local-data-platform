"""Checking metric definitions against the gold tables they are about.

:func:`validate_folder` is the check that ``ldp metrics validate`` and ``ldp gold build`` both run. It reads the metrics
files, and when they are valid it looks at the schema of each gold table, and nothing else of the table. A name has to
be a column, a time dimension has to be a date or a timestamp, and every measure is bound to the table in DuckDB, so an
expression that does not parse, reads a column that is not there, or adds up text, is found with the name of the
measure. Every problem is listed, and none stops the check from finding the next.

The check binds each expression to an empty table that has the schema of the gold table, in memory, so it reads no data
and needs no DuckDB extension.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.metrics.load import Definitions, metrics_files, read_definitions, resolve_table
from local_data_platform.metrics.model import Measure, SemanticModel
from local_data_platform.metrics.sql import aggregate, import_duckdb, inspect_expression, numeric, quote
from local_data_platform.paths import resolve_path


@dataclass(frozen=True)
class FolderCheck:
    """What :func:`validate_folder` found.

    Attributes:
        folder: The folder.
        definitions: The definitions, or ``None`` when a file is not valid or there is none.
        problems: Every problem, each starting with the name of its metrics file.
        files: The names of the metrics files. Empty when the folder has none.
    """

    folder: Path
    definitions: Definitions | None = None
    problems: list[str] = field(default_factory=list)
    files: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """Whether there is at least one metrics file and nothing is wrong with them."""
        return self.definitions is not None and not self.problems

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"folder": str(self.folder), "files": list(self.files), "valid": self.valid,
                "semantic_models": len(self.definitions.models) if self.definitions else 0,
                "metrics": len(self.definitions.metrics) if self.definitions else 0, "problems": list(self.problems)}


def table_schemas(folder: str | os.PathLike) -> dict[str, Any]:
    """The Arrow schema of each gold table of a folder, by table, and ``None`` for a table that does not exist yet.

    Raises:
        ConfigError: If a config of the folder is not valid.
    """
    from local_data_platform.gold.build import find_gold
    from local_data_platform.pipeline.builders import iceberg_from_config

    schemas: dict[str, Any] = {}
    for gold in find_gold(folder):
        try:
            iceberg = iceberg_from_config(gold.config, "target", must_exist=True)
            schemas[gold.table] = iceberg.table().schema().as_arrow() if iceberg.exists() else None
        except TableNotFound:
            schemas[gold.table] = None
    return schemas


def _column_problem(name: str, table: str, columns: Mapping[str, str], *, reads: bool = False) -> str:
    """Say that a column is not in a table, with the closest column and all of them. ``reads`` for an expression."""
    close = difflib.get_close_matches(name.lower(), list(columns), n=1)
    hint = f" Did you mean {columns[close[0]]!r}?" if close else ""
    lead = f"reads the column {name!r}, which is" if reads else f"the column {name!r} is"
    return f"{lead} not in {table}.{hint} The columns are {list(columns.values())}"


def _is_time(type_name: str) -> bool:
    upper = type_name.upper()
    return upper == "DATE" or (upper.startswith("TIMESTAMP") and "TIME ZONE" not in upper)


def _check_model(connection: Any, model: SemanticModel, table: str, schema: Any, position: int,
                 measure_types: dict[str, str]) -> list[str]:
    """The problems of one semantic model against the schema of its table. Records the type of each measure."""
    duckdb = import_duckdb()
    label = f"{model.file}: semantic model {model.name!r}"
    view = f"__ldp_check_{position}"
    try:
        connection.register(view, schema.empty_table())
        described = connection.execute(f"DESCRIBE SELECT * FROM {quote(view)}").fetchall()
    except duckdb.Error as error:
        first = (str(error).strip().splitlines() or [type(error).__name__])[0]
        return [f"{label}, table: DuckDB cannot read the schema of {table}. {first}"]
    columns = {name.lower(): name for name in schema.names}
    types = {row[0].lower(): row[1] for row in described}
    problems: list[str] = []
    time = model.time_dimension
    if time is not None:
        actual = columns.get(time.column.lower())
        if actual is None:
            problems.append(f"{label}, time dimension: {_column_problem(time.column, table, columns)}")
        elif not _is_time(types[actual.lower()]):
            kind = types[actual.lower()]
            fix = ("Cast it to a TIMESTAMP in the gold table, so that the time zone of a day is decided once."
                   if "TIME ZONE" in kind.upper() else "Make it a DATE or a TIMESTAMP in the gold table.")
            problems.append(f"{label}, time dimension: the column {time.column!r} of {table} is a {kind}, and a time "
                            f"dimension has to be a DATE or a TIMESTAMP. {fix}")
    for dimension in model.dimensions:
        if dimension.column.lower() not in columns:
            problems.append(f"{label}, dimension {dimension.name!r}: "
                            f"{_column_problem(dimension.column, table, columns)}")
    for measure in model.measures:
        problems += _check_measure(connection, duckdb, label, table, view, columns, measure, measure_types)
    return problems


def _check_measure(connection: Any, duckdb: Any, label: str, table: str, view: str, columns: Mapping[str, str],
                   measure: Measure, measure_types: dict[str, str]) -> list[str]:
    here = f"{label}, measure {measure.name!r}"
    if measure.expr is None:
        return []
    found = inspect_expression(connection, measure.expr)
    if found.problems:
        return [f"{here}: the expression {measure.expr!r} {found.problems[0]}"]
    missing = [name for name in found.columns if name.lower() not in columns]
    if missing:
        return [f"{here}: the expression {measure.expr!r} {_column_problem(name, table, columns, reads=True)}"
                for name in missing]
    try:
        row = connection.execute(f"DESCRIBE SELECT {aggregate(measure.agg, measure.expr)} AS v FROM {quote(view)}"
                                 ).fetchall()[0]
    except duckdb.Error as error:
        first = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
        return [f"{here}: {measure.agg} of {measure.expr!r} does not work on {table}. {first}"]
    measure_types[measure.name] = str(row[1])
    return []


def check_definitions(definitions: Definitions, schemas: Mapping[str, Any]) -> list[str]:
    """The problems of metric definitions for the gold tables they are about.

    Args:
        definitions: The metrics of a folder.
        schemas: The Arrow schema of each gold table by ``<namespace>.<table>``, or ``None`` for a table that does not
            exist yet. See :func:`table_schemas`.

    Returns:
        One sentence for each problem, each starting with its metrics file and the semantic model. Empty when the
        definitions fit the tables.

    Raises:
        EngineNotFound: If ``duckdb`` is not installed.
    """
    duckdb = import_duckdb()
    connection = duckdb.connect(":memory:", config={"enable_external_access": False})
    problems: list[str] = []
    try:
        measure_types: dict[str, dict[str, str]] = {}
        for position, model in enumerate(definitions.models):
            label = f"{model.file}: semantic model {model.name!r}"
            try:
                table = resolve_table(model.table, list(schemas))
            except ConfigError as error:
                problems.append(f"{label}, table: {error}")
                continue
            schema = schemas[table]
            if schema is None:
                problems.append(f"{label}, table: {table} does not exist yet, so its columns cannot be checked. "
                                "Build it first, for instance with ldp gold build")
                continue
            found: dict[str, str] = {}
            problems += _check_model(connection, model, table, schema, position, found)
            measure_types[model.name] = found
        for metric in definitions.metrics:
            known = measure_types.get(metric.model, {})
            for name in metric.measures:
                if name in known and not numeric(known[name]):
                    problems.append(f"{metric.file}: metric {metric.name!r}: the measure {name!r} is a {known[name]}, "
                                    "and a metric is a number. Aggregate a number, or count rows")
    finally:
        connection.close()
    return problems


def validate_folder(folder: str | os.PathLike) -> FolderCheck:
    """Check the metrics files of a folder, and their names and expressions against the gold tables.

    It is the check of ``ldp metrics validate``, and ``ldp gold build`` runs it as well. When a file is not valid the
    tables are not looked at, because the definitions cannot be trusted to say which tables they mean.

    Args:
        folder: A folder of configs, which holds the metrics files and the gold configs.

    Returns:
        The :class:`FolderCheck`. A folder with no metrics file has one problem, which says so.

    Raises:
        ConfigError: If ``folder`` is not a folder, or a config of it is not valid.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    root = resolve_path(folder)
    found, problems = read_definitions(root)
    files = tuple(item.name for item in metrics_files(root))
    if found is None:
        return FolderCheck(root, None, problems, files)
    return FolderCheck(root, found, check_definitions(found, table_schemas(root)), files)


def has_metrics(folder: str | os.PathLike) -> bool:
    """Whether a folder has a metrics file."""
    return bool(metrics_files(folder))


__all__ = ["FolderCheck", "check_definitions", "has_metrics", "table_schemas", "validate_folder"]
