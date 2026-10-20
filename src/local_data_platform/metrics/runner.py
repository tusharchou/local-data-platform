"""Running a metric query on the gold tables of a folder.

The tables are read the way every reader of the gold layer reads them. By default that is the current release, so the
numbers of two metrics come from tables that agree, ``release`` names an older one, and ``live`` reads the tables as
they are now. The query runs on a :class:`~local_data_platform.engine.duckdb.DuckDBEngine` that has only the one table
registered, at the snapshot that the release names for it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.metrics.compiler import CompiledQuery, compile_query, select_metrics
from local_data_platform.metrics.load import Definitions, load_metrics, resolve_table
from local_data_platform.metrics.sql import import_duckdb, names, quote

if TYPE_CHECKING:
    import datetime as dt

    import pyarrow as pa

    from local_data_platform.gold.release import Release


@dataclass
class Result:
    """What a metric query returned.

    Attributes:
        query: The compiled query.
        table: The rows, or ``None`` when the query was only compiled.
        release: The release that the table was read as, or ``None`` when it was read as it is now.
        source: Which version of the table was read, in words, such as ``release r3, cut 2026-10-20 08:07:55 UTC``.
    """

    query: CompiledQuery
    table: pa.Table | None
    release: Release | None = None
    source: str = ""


def describe_source(current: Release | None, held: bool, table: str, live: bool = False) -> str:
    """Which version of a table a read used, in words.

    Args:
        current: The release that the read would use, or ``None`` when there is none.
        held: Whether that release holds the table.
        table: The table, ``<namespace>.<table>``.
        live: Whether the reader asked for the tables as they are now.
    """
    if live:
        return "the tables as they are now"
    if current is None:
        return "the tables as they are now, because no release has been cut yet"
    if not held:
        return f"the tables as they are now, because release {current.name} does not hold {table}"
    return f"release {current.name}, cut {current.created_at.strftime('%Y-%m-%d %H:%M:%S')} UTC"


def column_types(engine: Any, table_sql: str) -> dict[str, str]:
    """The DuckDB type of each column of a registered table, by lower case name."""
    described = engine.query(f"DESCRIBE SELECT * FROM {table_sql}").to_pylist()
    return {str(row["column_name"]).lower(): str(row["column_type"]) for row in described}


def run(folder: str | os.PathLike, metrics: str | Sequence[str], *, by: str | Sequence[str] | None = None,
        grain: str | None = None, filters: Mapping[str, Any] | Sequence[str] | None = None,
        start: str | dt.date | None = None, end: str | dt.date | None = None, release: str | None = None,
        live: bool = False, execute: bool = True, definitions: Definitions | None = None) -> Result:
    """Compile a metric query, and run it unless ``execute`` is false.

    Args:
        folder: A folder of configs, with its metrics files and the configs of its gold tables.
        metrics: The metrics, as ``revenue,avg_ticket`` or a list. They are of one semantic model.
        by: The dimensions to group by, as ``cuisine,city`` or a list.
        grain: The grain of time to group by.
        filters: ``dimension=value`` items, or ``{dimension: value or values}``. Never SQL.
        start: The first day to include, ``YYYY-MM-DD``.
        end: The last day to include, ``YYYY-MM-DD``.
        release: Read this release, such as ``r1``, and not the current one.
        live: Read the tables as they are now, and no release.
        execute: Run the query. With false, only compile it, and no data is read.
        definitions: The definitions, when they are already read.

    Returns:
        The :class:`Result`.

    Raises:
        ConfigError: If a name, a grain or a value is not allowed, the metrics are of two tables, or the query cannot
            run on the release that was asked for.
        ReleaseError: If ``release`` is not a release of the folder.
        TableNotFound: If the gold table does not exist yet.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    from local_data_platform.engine.duckdb import DuckDBEngine
    from local_data_platform.gold import release as releases
    from local_data_platform.gold.build import find_gold

    duckdb = import_duckdb()
    found = definitions if definitions is not None else load_metrics(folder)
    model, selected = select_metrics(found, metrics)
    table = resolve_table(model.table, [gold.table for gold in find_gold(folder)])
    chosen = releases.choose_release(folder, release=release, live=live)
    held = chosen is not None and releases.snapshot_for(table, chosen) is not None
    if release is not None and not held:
        raise ConfigError(f"release {chosen.name} does not hold {table}, so it cannot be read as of that release. "
                          f"The release holds {sorted(chosen.snapshots())}")
    namespace, _, name = table.rpartition(".")
    table_sql = f"{quote(namespace)}.{quote(name)}" if namespace else quote(name)
    with DuckDBEngine() as engine:
        releases.register_release(engine, folder, release=chosen.name if chosen is not None else None,
                                  live=chosen is None, tables=[table])
        compiled = compile_query(found, metrics, table=table, types=column_types(engine, table_sql), by=by,
                                 grain=grain, filters=filters, start=start, end=end, table_sql=table_sql)
        data = None
        if execute:
            try:
                data = engine.query(compiled.sql, list(compiled.parameters))
            except duckdb.Error as error:
                first = (str(error).strip().splitlines() or [type(error).__name__])[0].rstrip(".!")
                hint = (f" The definitions describe {table} as it is now, and {chosen.name} may have the columns of an "
                        "earlier schema. Try a newer release, or --live") if chosen is not None else ""
                raise ConfigError(f"the query for {names([item.name for item in selected])} could not run on "
                                  f"{table}. {first}.{hint}") from error
    return Result(query=compiled, table=data, release=chosen if held else None,
                  source=describe_source(chosen, held, table, live))


def query(folder: str | os.PathLike, metrics: str | Sequence[str], *, by: str | Sequence[str] | None = None,
          grain: str | None = None, filters: Mapping[str, Any] | Sequence[str] | None = None,
          start: str | dt.date | None = None, end: str | dt.date | None = None, release: str | None = None,
          live: bool = False) -> pa.Table:
    """Compute metrics from the gold tables of a folder, and return them as a ``pyarrow.Table``.

    The columns are the grain when there is one, then the dimensions, then the metrics, in the order they were asked
    for. The values are what DuckDB computed, so a sum of decimals is a decimal. By default the tables are read as the
    current release holds them.

    Args:
        folder: A folder of configs, with its metrics files and the configs of its gold tables.
        metrics: The metrics, as ``revenue,avg_ticket`` or a list. They are of one semantic model.
        by: The dimensions to group by, as ``cuisine,city`` or a list.
        grain: The grain of time to group by, one that the time dimension allows. Without one the period is added up
            into one row, if the time dimension allows ``year``.
        filters: ``dimension=value`` items such as ``["city=Pune"]``, or ``{"city": "Pune"}``. A dimension with two
            values means either. The values are bound as parameters and are never SQL.
        start: The first day to include, ``YYYY-MM-DD``.
        end: The last day to include, ``YYYY-MM-DD``.
        release: Read this release, such as ``r1``, and not the current one.
        live: Read the tables as they are now, and no release.

    Returns:
        The result.

    Raises:
        ConfigError: If a name, a grain or a value is not allowed, or the metrics are of two tables. The message lists
            the names that are valid.
        ReleaseError: If ``release`` is not a release of the folder.
        TableNotFound: If the gold table does not exist yet.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    result = run(folder, metrics, by=by, grain=grain, filters=filters, start=start, end=end, release=release,
                 live=live)
    assert result.table is not None
    return result.table


def compile_for(folder: str | os.PathLike, metrics: str | Sequence[str], **options: Any) -> CompiledQuery:
    """The SQL of a metric query, without running it. It takes what :func:`query` takes."""
    return run(folder, metrics, execute=False, **options).query


__all__ = ["Result", "column_types", "compile_for", "describe_source", "query", "run"]
