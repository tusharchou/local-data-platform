"""How the answer to a metric query, and the list of metrics, are shown and written.

A metric has a ``format``. It says how a person reads the value, and it changes only how the value is shown on a
terminal. A percent is a fraction, so 0.5 shows as ``50.0%``. The values that are returned, written to a file or sent
to an agent are never rounded or scaled.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.metrics.compiler import Column
from local_data_platform.metrics.load import Definitions

FILE_FORMATS = {".csv": "CSV", ".parquet": "Parquet", ".jsonl": "JSONL", ".ndjson": "JSONL"}
"""The files that ``--out`` writes, by the ending of the name."""

DESCRIPTION_WIDTH = 48
LIST_WIDTH = 36


def _clip(value: Any, width: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= width else text[:max(width - 3, 0)] + "..."


def _number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _whole(value: Any) -> bool:
    return _number(value) and not (isinstance(value, float) and not math.isfinite(value)) and value == int(value)


def display(value: Any, fmt: str | None, *, whole: bool | None = None) -> Any:
    """A value of a metric as a person reads it, by the format of the metric.

    A ``percent`` is a fraction shown as a percentage with one decimal. A ``currency`` has two decimals, and a
    ``number`` has none when it is whole and two otherwise, and all of them group the thousands. No value stays no
    value, and what is not a number stays as it is.

    Args:
        value: The value.
        fmt: ``number``, ``percent`` or ``currency``.
        whole: For a ``number``, whether to leave out the decimals. By default that is whether the value is whole, and
            a column passes whether all of its values are, so that they line up.
    """
    if value is None or not _number(value):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if fmt == "percent":
        return f"{value * 100:,.1f}%"
    if fmt == "currency":
        return f"{value:,.2f}"
    if _whole(value) if whole is None else whole:
        return f"{int(round(value)):,}"
    return f"{value:,.2f}"


def table_text(table: Any, columns: Sequence[Column], *, max_rows: int | None = None, raw: bool = False) -> str:
    """The rows of a result as a table for a terminal, with each metric in its format.

    Args:
        table: The result, a ``pyarrow.Table``.
        columns: The columns that the query says it returns, which give the format of each metric.
        max_rows: Show at most this many rows.
        raw: Show the values as they were computed, with no format.
    """
    from local_data_platform.cli import format_table

    formats = {item.name: item.format for item in columns if item.kind == "metric"}
    rows = table.to_pylist()
    if not raw:
        whole = {name: all(_whole(row[name]) for row in rows if row[name] is not None) for name in formats}
        rows = [{name: display(value, formats[name], whole=whole[name]) if name in formats else value
                 for name, value in row.items()} for row in rows]
    return format_table(rows if rows else table, max_rows=max_rows, right=[] if raw else list(formats))


def plain_numbers(table: Any) -> Any:
    """The table with each decimal column as numbers, whole numbers where the decimal has no digits after the point.

    DuckDB adds up integers into a decimal wide enough for any sum. An agent and a JSON file read a number better than
    the text of a decimal, so the columns that carry the values leave the engine as numbers.
    """
    import pyarrow as pa

    columns = []
    for field, column in zip(table.schema, table.columns):
        if pa.types.is_decimal(field.type):
            try:
                column = column.cast(pa.int64() if field.type.scale == 0 else pa.float64())
            except pa.ArrowInvalid:
                column = column.cast(pa.float64())
        columns.append(column)
    return pa.Table.from_arrays(columns, names=table.column_names)


def _json_value(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


def json_rows(table: Any) -> list[dict[str, Any]]:
    """The rows of a result as dicts that ``json.dumps`` writes, with numbers as numbers and dates as text."""
    return json.loads(json.dumps(plain_numbers(table).to_pylist(), default=_json_value))


def file_format(path: str | os.PathLike) -> str:
    """The format that ``--out`` writes at ``path``, by its ending.

    Raises:
        ConfigError: If the ending is not one of ``.csv``, ``.parquet``, ``.jsonl`` and ``.ndjson``.
    """
    suffix = Path(path).suffix.lower()
    if suffix not in FILE_FORMATS:
        raise ConfigError(f"cannot write {Path(path).name!r}. The file has to end in one of {sorted(FILE_FORMATS)}")
    return FILE_FORMATS[suffix]


def write_table(table: Any, path: str | os.PathLike) -> str:
    """Write a result to a CSV, Parquet or JSONL file by the ending of the name, with the values as computed.

    The file is written under a temporary name and renamed, so a reader sees all of it or none of it.

    Returns:
        The name of the format.

    Raises:
        ConfigError: If the ending of the name is not one of ``.csv``, ``.parquet``, ``.jsonl`` and ``.ndjson``.
    """
    kind = file_format(path)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        if kind == "CSV":
            import pyarrow.csv as csv

            csv.write_csv(table, str(temporary))
        elif kind == "Parquet":
            import pyarrow.parquet as parquet

            parquet.write_table(table, str(temporary))
        else:
            lines = [json.dumps(row, ensure_ascii=False) for row in json_rows(table)]
            temporary.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return kind


def list_text(definitions: Definitions) -> str:
    """Every metric with its description, owner, format, table, dimensions and grains, as a table for a terminal."""
    from local_data_platform.cli import format_table

    rows = []
    for metric in definitions.metrics:
        model = definitions.model_of(metric)
        rows.append({
            "metric": metric.name, "description": _clip(metric.description, DESCRIPTION_WIDTH),
            "owner": _clip(metric.owner, LIST_WIDTH), "format": metric.format, "table": model.table,
            "dimensions": _clip(", ".join(item.name for item in model.dimensions) or "-", LIST_WIDTH),
            "grains": ", ".join(model.grains) or "-"})
    return format_table(rows)


__all__ = ["DESCRIPTION_WIDTH", "FILE_FORMATS", "display", "file_format", "json_rows", "list_text", "plain_numbers",
           "table_text", "write_table"]
