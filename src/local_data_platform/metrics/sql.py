"""The pieces of SQL that a metric query is made of, and the check that an expression is only an expression.

A measure has a SQL expression that the file's author wrote. It goes into the query as text, so it is held to what an
expression is. That is one value computed from the columns of the row, with no subquery, no window function, no
aggregate of its own, no parameter and no table name. DuckDB's own parser says what the text is, and its tree is walked
here, so the check does not depend on how the text looks. Values that a person gives, such as a filter, never go into
the SQL text at all. They are bound as parameters.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.metrics.model import GRAINS

INSTALL_HINT = 'pip install "local-data-platform[duckdb]"'

_AGGREGATES: frozenset[str] | None = None


def import_duckdb() -> Any:
    """Import ``duckdb`` or raise :class:`EngineNotFound` with an install hint."""
    try:
        import duckdb
    except ImportError as error:
        raise EngineNotFound(f"Metrics need the duckdb package. Install it with: {INSTALL_HINT}") from error
    return duckdb


def quote(name: str) -> str:
    """``name`` as a DuckDB identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def aggregate(agg: str, expr: str | None) -> str:
    """The SQL of an aggregate, such as ``sum(net_revenue)`` or ``count(DISTINCT customer_id)``.

    Args:
        agg: ``sum``, ``count``, ``count_distinct``, ``avg``, ``min`` or ``max``.
        expr: The expression, or ``None`` for ``count`` of rows.
    """
    if agg == "count":
        return f"count({expr})" if expr else "count(*)"
    if agg == "count_distinct":
        return f"count(DISTINCT {expr})"
    return f"{agg}({expr})"


def ratio(numerator: str, denominator: str) -> str:
    """The SQL of a ratio of two aggregates, which is no value when the denominator is zero.

    The ratio is the sum of one over the sum of the other, and never the average of the ratios of the rows. DuckDB
    answers a division by zero with infinity, so the zero is turned into no value first.
    """
    return f"{numerator} / NULLIF({denominator}, 0)"


def time_bucket(column: str, grain: str) -> str:
    """The SQL of the start of the ``grain`` that ``column`` falls in, as a date.

    Raises:
        ValueError: If ``grain`` is not a grain. It goes into the SQL as text, so only the five names get through.
    """
    if grain not in GRAINS:
        raise ValueError(f"{grain!r} is not a grain; the grains are {list(GRAINS)}")
    return f"CAST(date_trunc('{grain}', {quote(column)}) AS DATE)"


def _aggregate_names(connection: Any) -> frozenset[str]:
    """The names of the functions that only aggregate, which DuckDB lists for itself."""
    global _AGGREGATES
    if _AGGREGATES is None:
        rows = connection.execute("SELECT DISTINCT lower(function_name), function_type FROM duckdb_functions() "
                                  "WHERE function_type IN ('aggregate', 'scalar')").fetchall()
        scalar = {name for name, kind in rows if kind == "scalar"}
        _AGGREGATES = frozenset(name for name, kind in rows if kind == "aggregate" and name not in scalar)
    return _AGGREGATES


_NOT_ALLOWED = {
    "SUBQUERY": "a subquery",
    "WINDOW": "a window function",
    "STAR": "a star",
    "PARAMETER": "a parameter",
    "POSITIONAL_REFERENCE": "a positional reference",
}


@dataclass(frozen=True)
class Inspection:
    """What DuckDB's parser found in an expression.

    Attributes:
        columns: The columns the expression reads, in the order it names them, once each.
        problems: What is wrong with it as an expression. Empty when it is one.
    """

    columns: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


def _walk(node: Any, columns: list[str], problems: list[str], aggregates: frozenset[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, columns, problems, aggregates)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("class")
    if kind in _NOT_ALLOWED:
        problem = f"has {_NOT_ALLOWED[kind]}, and a measure is an expression of the columns of one row"
        if problem not in problems:
            problems.append(problem)
        return
    if kind == "COLUMN_REF":
        parts = node.get("column_names") or []
        if len(parts) != 1:
            problem = (f"names the column {'.'.join(parts)!r} with a table in front. Write the column alone, "
                       "because the query chooses the table")
            if problem not in problems:
                problems.append(problem)
        elif parts[0] not in columns:
            columns.append(parts[0])
    elif kind == "FUNCTION" and str(node.get("function_name", "")).lower() in aggregates:
        problem = (f"calls the aggregate {node['function_name']}(), and the measure already aggregates with its agg. "
                   "An aggregate in an aggregate cannot work")
        if problem not in problems:
            problems.append(problem)
    for child in node.values():
        _walk(child, columns, problems, aggregates)


def _select_item(connection: Any, text: str) -> dict[str, Any] | None:
    """The one select item of ``text`` when it is one ``SELECT`` of one expression with no table, else ``None``."""
    data = json.loads(connection.execute("SELECT json_serialize_sql(?)", [text]).fetchone()[0])
    if data.get("error"):
        return None
    statements = data.get("statements") or []
    node = statements[0].get("node", {}) if len(statements) == 1 else {}
    items = node.get("select_list") or []
    clean = (node.get("type") == "SELECT_NODE" and len(items) == 1 and (node.get("from_table") or {}).get("type")
             == "EMPTY" and not node.get("where_clause") and not node.get("group_expressions")
             and not node.get("having") and not node.get("qualify") and not node.get("modifiers"))
    return items[0] if clean else None


def inspect_expression(connection: Any, expr: str) -> Inspection:
    """Parse ``expr`` with DuckDB and say which columns it reads and whether it is only an expression.

    The text is parsed twice. In parentheses, so that a comment that would swallow the rest of the query, or text that
    closes the parenthesis and adds clauses, is found out. And without them, so that a comma, which parentheses would
    turn into one row of values, is found out. Each has to be one select item with no table. Nothing is run and no table
    is read.

    Args:
        connection: A DuckDB connection. It reads no table.
        expr: The SQL expression.
    """
    wrapped = f"SELECT ({expr}) AS v"
    raw = json.loads(connection.execute("SELECT json_serialize_sql(?)", [wrapped]).fetchone()[0])
    if raw.get("error"):
        message = str(raw.get("error_message") or "is not valid SQL").splitlines()[0]
        return Inspection(problems=(f"is not a SQL expression: {message}",))
    item = _select_item(connection, wrapped)
    if item is None or _select_item(connection, f"SELECT {expr} AS v") is None:
        return Inspection(problems=("is more than one expression, and a measure is one SQL expression",))
    columns: list[str] = []
    problems: list[str] = []
    _walk(item, columns, problems, _aggregate_names(connection))
    return Inspection(columns=tuple(columns), problems=tuple(problems))


def only_expression(expr: str) -> None:
    """Raise if ``expr`` is not one plain SQL expression, on a connection of its own that reads nothing.

    Raises:
        ConfigError: With what is wrong with it.
        EngineNotFound: If ``duckdb`` is not installed.
    """
    duckdb = import_duckdb()
    connection = duckdb.connect(":memory:", config={"enable_external_access": False})
    try:
        found = inspect_expression(connection, expr)
    finally:
        connection.close()
    if found.problems:
        raise ConfigError(f"the expression {expr!r} {found.problems[0]}")


def numeric(type_name: str) -> bool:
    """Whether a DuckDB type is a number: an integer, a decimal or a float."""
    name = str(type_name).upper().split("(")[0].strip()
    return name in _NUMBERS


_NUMBERS = frozenset({"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                      "UBIGINT", "UHUGEINT", "DECIMAL", "FLOAT", "DOUBLE", "REAL"})


def names(items: Sequence[str]) -> str:
    """A list of names for a message, such as ``'a', 'b' and 'c'``."""
    quoted = [repr(item) for item in items]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


__all__ = ["INSTALL_HINT", "Inspection", "aggregate", "import_duckdb", "inspect_expression", "names", "numeric",
           "only_expression", "quote", "ratio", "time_bucket"]
