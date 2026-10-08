"""Render a pyiceberg row filter as a DuckDB SQL predicate.

:meth:`~local_data_platform.engine.duckdb.DuckDBEngine.register_iceberg` uses this on its native path,
where an Iceberg table is a view over DuckDB's ``iceberg_scan``: the filter becomes the view's
``WHERE`` clause, which DuckDB pushes into the scan to skip data files and row groups. The aim is
the rows a pyiceberg ``Table.scan(row_filter=...)`` returns, exactly.

What is supported:

* A ``row_filter`` is a pyiceberg ``BooleanExpression`` or a string in pyiceberg's row-filter grammar,
  the one ``Table.scan(row_filter=...)`` accepts: ``=``, ``==``, ``!=``, ``<>``, ``<``, ``<=``,
  ``>``, ``>=``, ``BETWEEN``, ``IN``, ``NOT IN``, ``IS [NOT] NULL``, ``IS [NOT] NAN``,
  ``[NOT] LIKE 'prefix%'``, ``AND``, ``OR``, ``NOT``, ``true`` and ``false``. Strings are parsed
  by pyiceberg and the SQL is rendered from the parsed expression, so raw DuckDB SQL is never
  spliced into the view; for arbitrary SQL, filter in the query instead.
* The expression is bound to the table's current schema, as pyiceberg binds it (also when time
  travelling), so an unknown column or a literal of the wrong type raises the error the pyiceberg
  scan raises. Columns are then referenced by their name in the scanned snapshot's schema, so a
  renamed column works in both directions; a column the snapshot doesn't have is null for every
  row and its predicate becomes a constant, as in pyiceberg.
* Columns can be top-level or nested inside structs (``location.lat``); literals of every
  primitive type up to Iceberg v2 (boolean, int, long, float, double, decimal, date, time,
  timestamp, timestamptz, string, uuid, fixed, binary).
* Null and NaN semantics follow pyiceberg's Arrow evaluation, not plain SQL: ``IN`` and
  ``NOT IN`` treat a null value as "not in the set" (so ``NOT IN`` keeps nulls), and ``>`` /
  ``>=`` never match NaN (DuckDB orders NaN above every number).

What raises :class:`UntranslatableFilter` (the auto path then falls back to the in-memory scan):

* nanosecond timestamps (Iceberg v3) and columns with an ``initial-default``;
* fields inside lists or maps, and terms other than a plain column reference;
* on a column added by schema evolution, a predicate whose answer for a data file written before
  the column existed could differ from pyiceberg's. pyiceberg doesn't read such a file's column;
  it turns the predicate into a constant, and which constant depends on its version (0.9 makes
  everything but ``IS NULL`` false; 0.12 evaluates the predicate on a missing value in Python,
  where it is ``!=`` everything), while DuckDB sees ``NULL``. So ``!=``, ``NOT IN``, ``NOT LIKE``
  and ``IS NOT NAN`` on such a column fall back, as do ``=``, ``<``, ``<=``, ``>``, ``>=``,
  ``LIKE`` and ``IS NAN`` under ``NOT``. ``IS [NOT] NULL`` and ``IN`` always stay native.

Known difference: pyiceberg's file pruning compares a ``float`` (32-bit) column's statistics with the
64-bit literal, so ``small = 0.1`` on a float column can skip files that hold ``0.1f``; the native
path compares in 32 bits and returns those rows.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

#: Relation name the native view gives ``iceberg_scan``, so column references are unambiguous.
SCAN_RELATION = "_ldp_scan"


class UntranslatableFilter(ValueError):
    """Raised when a row filter can't be expressed exactly in DuckDB SQL."""


def quote_identifier(name: str) -> str:
    """Quote a DuckDB identifier (``"name"``, with embedded quotes doubled)."""
    return '"' + str(name).replace('"', '""') + '"'


def quote_string(value: str) -> str:
    """Quote a DuckDB string literal (``'value'``, with embedded quotes doubled)."""
    return "'" + str(value).replace("'", "''") + "'"


def parse_row_filter(row_filter: Any):
    """Return ``row_filter`` as a pyiceberg ``BooleanExpression``.

    Args:
        row_filter: A ``BooleanExpression``, or a string in pyiceberg's row-filter grammar.

    Raises:
        TypeError: If ``row_filter`` is neither.
        pyparsing.ParseException: If the string is not valid row-filter syntax, as for a pyiceberg scan.
    """
    from pyiceberg.expressions import BooleanExpression
    from pyiceberg.expressions.parser import parse

    if isinstance(row_filter, str):
        return parse(row_filter)
    if isinstance(row_filter, BooleanExpression):
        return row_filter
    raise TypeError(f"row_filter must be a string or a pyiceberg BooleanExpression, got {type(row_filter).__name__}")


def evolved_field_ids(schemas: Iterable[Any]) -> frozenset[int]:
    """Field ids of the last schema that some earlier schema lacks: columns added by schema evolution.

    Data files written before such a column existed don't contain it.

    Args:
        schemas: The table's schemas, oldest first (``table.metadata.schemas``).
    """
    ids = [_field_ids(schema) for schema in schemas]
    if not ids:
        return frozenset()
    return frozenset(field_id for field_id in ids[-1] if any(field_id not in earlier for earlier in ids[:-1]))


def _field_ids(schema: Any) -> set[int]:
    from pyiceberg.schema import index_by_id

    return set(index_by_id(schema))


def row_filter_to_sql(row_filter: Any, schema: Any, *, scan_schema: Any = None,
                      evolved: Iterable[int] = (), relation: str | None = SCAN_RELATION,
                      case_sensitive: bool = True) -> str | None:
    """Render a row filter as a DuckDB predicate.

    Args:
        row_filter: A pyiceberg ``BooleanExpression`` or a row-filter string.
        schema: The table's current pyiceberg ``Schema``, which the filter is bound to.
        scan_schema: The schema of the snapshot being scanned, whose column names the SQL uses.
            Defaults to ``schema``.
        evolved: Field ids added by schema evolution (see :func:`evolved_field_ids`).
        relation: Relation to qualify column references with, or ``None`` for bare column names.
        case_sensitive: Match column names case-sensitively, as pyiceberg scans do by default.

    Returns:
        The predicate, or ``None`` when the filter is always true (no ``WHERE`` needed).

    Raises:
        UntranslatableFilter: If the filter can't be rendered exactly (see the module docstring).
        ValueError: If the filter names an unknown column or has a literal of the wrong type.
    """
    from pyiceberg.expressions import AlwaysTrue
    from pyiceberg.expressions.visitors import bind

    bound = bind(schema, parse_row_filter(row_filter), case_sensitive=case_sensitive)
    if isinstance(bound, AlwaysTrue):
        return None
    renderer = _Renderer(scan_schema if scan_schema is not None else schema, frozenset(evolved), relation)
    return renderer.render(bound, negated=False)


class _Renderer:
    """Turn a bound pyiceberg expression into DuckDB SQL."""

    def __init__(self, scan_schema: Any, evolved: frozenset[int], relation: str | None):
        self._schema = scan_schema
        self._evolved = evolved
        self._relation = relation

    def render(self, expr: Any, negated: bool) -> str:
        from pyiceberg import expressions as ex

        if isinstance(expr, ex.AlwaysTrue):
            return "TRUE"
        if isinstance(expr, ex.AlwaysFalse):
            return "FALSE"
        if isinstance(expr, ex.And):
            return f"({self.render(expr.left, negated)} AND {self.render(expr.right, negated)})"
        if isinstance(expr, ex.Or):
            return f"({self.render(expr.left, negated)} OR {self.render(expr.right, negated)})"
        if isinstance(expr, ex.Not):
            return f"(NOT {self.render(expr.child, not negated)})"
        if isinstance(expr, ex.BoundPredicate):
            return self._predicate(expr, negated)
        raise UntranslatableFilter(f"cannot render {type(expr).__name__} as DuckDB SQL")

    def _predicate(self, pred: Any, negated: bool) -> str:
        from pyiceberg import expressions as ex
        from pyiceberg.types import DoubleType, FloatType

        field = self._field(pred.term)
        if getattr(field, "initial_default", None) is not None:
            raise UntranslatableFilter(f"column {field.name!r} has an initial-default, which DuckDB may not apply")
        path = _field_path(self._schema.as_struct(), field.field_id)
        operation = type(pred).__name__.removeprefix("Bound")
        if path is None:
            if _field_path(self._schema.as_struct(), field.field_id, through_collections=True) is not None:
                raise UntranslatableFilter(f"column {field.name!r} is inside a list or map, which DuckDB filters "
                                           "can't reference")
            # The scanned snapshot has no such column, so no data file has it: pyiceberg makes the
            # predicate a constant.
            constant = _missing_value_matches(pred)
            if constant is None:
                raise UntranslatableFilter(f"column {field.name!r} doesn't exist in the scanned snapshot, and "
                                           f"pyiceberg versions disagree on {operation} for a missing column")
            return "TRUE" if constant else "FALSE"
        if field.field_id in self._evolved and _diverges_on_missing(pred, negated):
            raise UntranslatableFilter(f"column {field.name!r} was added by schema evolution, and pyiceberg may "
                                       f"answer {operation} differently from SQL for data files written before it "
                                       "existed")
        column = self._column(path)
        if isinstance(pred, ex.BoundIsNull):
            return f"({column} IS NULL)"
        if isinstance(pred, ex.BoundNotNull):
            return f"({column} IS NOT NULL)"
        if isinstance(pred, ex.BoundIsNaN):
            return f"isnan({column})"
        if isinstance(pred, ex.BoundNotNaN):
            return f"(NOT isnan({column}))"
        if isinstance(pred, (ex.BoundIn, ex.BoundNotIn)):
            values = ", ".join(sorted(literal_sql(lit.value, field.field_type) for lit in pred.literals))
            # pyarrow's is_in() answers false (not null) for a null value, so a null is never "in" the
            # set and always "not in" it.
            if isinstance(pred, ex.BoundIn):
                return f"({column} IS NOT NULL AND {column} IN ({values}))"
            return f"({column} IS NULL OR {column} NOT IN ({values}))"
        if isinstance(pred, (ex.BoundStartsWith, ex.BoundNotStartsWith)):
            test = f"starts_with({column}, {literal_sql(pred.literal.value, field.field_type)})"
            return test if isinstance(pred, ex.BoundStartsWith) else f"(NOT {test})"
        for cls, operator in _comparisons():
            if isinstance(pred, cls):
                comparison = f"({column} {operator} {literal_sql(pred.literal.value, field.field_type)})"
                if operator in (">", ">=") and isinstance(field.field_type, (FloatType, DoubleType)):
                    # DuckDB sorts NaN above every number; IEEE (and pyarrow) comparisons with NaN are false.
                    return f"({comparison} AND NOT isnan({column}))"
                return comparison
        raise UntranslatableFilter(f"cannot render {type(pred).__name__} as DuckDB SQL")

    @staticmethod
    def _field(term: Any) -> Any:
        from pyiceberg.expressions import BoundReference

        if not isinstance(term, BoundReference):
            raise UntranslatableFilter(f"only column references can be rendered, got {type(term).__name__}")
        return term.field

    def _column(self, path: list[str]) -> str:
        parts = [quote_identifier(name) for name in path]
        if self._relation:
            parts.insert(0, quote_identifier(self._relation))
        return ".".join(parts)


def _comparisons() -> list[tuple[type, str]]:
    from pyiceberg import expressions as ex

    return [(ex.BoundEqualTo, "="), (ex.BoundNotEqualTo, "<>"), (ex.BoundLessThan, "<"),
            (ex.BoundLessThanOrEqual, "<="), (ex.BoundGreaterThan, ">"), (ex.BoundGreaterThanOrEqual, ">=")]


def _missing_value_matches(pred: Any) -> bool | None:
    """The constant pyiceberg makes of ``pred`` for a data file without its column.

    Returns ``None`` where pyiceberg versions disagree: 0.9 answers true only for ``IS NULL``, while
    0.12 evaluates the predicate on ``None`` in Python, so ``!=``, ``NOT IN``, ``NOT LIKE`` and
    ``IS NOT NAN`` are true there too.
    """
    from pyiceberg import expressions as ex

    if isinstance(pred, ex.BoundIsNull):
        return True
    if isinstance(pred, (ex.BoundNotIn, ex.BoundNotNaN, ex.BoundNotEqualTo, ex.BoundNotStartsWith)):
        return None
    return False


def _diverges_on_missing(pred: Any, negated: bool) -> bool:
    """Whether SQL on ``NULL`` could filter a row differently from pyiceberg's constant for a missing column.

    ``IS NULL``, ``IS NOT NULL`` and ``IN`` (rendered two-valued) give SQL the same definite answer.
    The rest give ``NULL``, which drops the row like pyiceberg's ``False`` does, until a ``NOT``
    flips the answer: ``NOT NULL`` is still ``NULL``, while ``not False`` keeps the row.
    """
    from pyiceberg import expressions as ex

    constant = _missing_value_matches(pred)
    if constant is None:
        return True
    if isinstance(pred, (ex.BoundIsNull, ex.BoundNotNull, ex.BoundIn)):
        return False
    return negated


def _field_path(struct: Any, field_id: int, through_collections: bool = False) -> list[str] | None:
    """Names from the top-level column down to ``field_id``, through structs (and lists and maps if asked)."""
    from pyiceberg.types import ListType, MapType, StructType

    for field in struct.fields:
        if field.field_id == field_id:
            return [field.name]
        children: list[Any] = []
        if isinstance(field.field_type, StructType):
            children = [field.field_type]
        elif through_collections and isinstance(field.field_type, ListType):
            children = [_single_field_struct(field.field_type.element_field)]
        elif through_collections and isinstance(field.field_type, MapType):
            children = [_single_field_struct(field.field_type.key_field),
                        _single_field_struct(field.field_type.value_field)]
        for child in children:
            below = _field_path(child, field_id, through_collections)
            if below is not None:
                return [field.name, *below]
    return None


def _single_field_struct(field: Any) -> Any:
    from pyiceberg.types import StructType

    return StructType(field)


def literal_sql(value: Any, field_type: Any) -> str:
    """Render a bound pyiceberg literal value of ``field_type`` as a DuckDB SQL literal.

    Raises:
        UntranslatableFilter: For types or values DuckDB SQL can't express exactly.
    """
    from pyiceberg import types as t
    from pyiceberg.utils.datetime import days_to_date, micros_to_time, micros_to_timestamp, micros_to_timestamptz

    if isinstance(field_type, t.BooleanType):
        return "TRUE" if value else "FALSE"
    if isinstance(field_type, (t.IntegerType, t.LongType)):
        return str(int(value))
    if isinstance(field_type, (t.FloatType, t.DoubleType)):
        number = float(value)
        if math.isnan(number):
            raise UntranslatableFilter("NaN literals compare differently in DuckDB; use 'IS NAN' / 'IS NOT NAN'")
        sql_type = "FLOAT" if isinstance(field_type, t.FloatType) else "DOUBLE"
        return f"CAST({quote_string(repr(number))} AS {sql_type})"
    if isinstance(field_type, t.DecimalType):
        text = quote_string(format(Decimal(value), "f"))
        return f"CAST({text} AS DECIMAL({field_type.precision}, {field_type.scale}))"
    if isinstance(field_type, t.StringType):
        return quote_string(value)
    try:
        if isinstance(field_type, t.DateType):
            return f"CAST({quote_string(days_to_date(value).isoformat())} AS DATE)"
        if isinstance(field_type, t.TimeType):
            return f"CAST({quote_string(micros_to_time(value).isoformat())} AS TIME)"
        if isinstance(field_type, t.TimestamptzType):
            return f"CAST({quote_string(micros_to_timestamptz(value).isoformat(sep=' '))} AS TIMESTAMPTZ)"
        if isinstance(field_type, t.TimestampType):
            return f"CAST({quote_string(micros_to_timestamp(value).isoformat(sep=' '))} AS TIMESTAMP)"
    except (OverflowError, ValueError) as error:
        raise UntranslatableFilter(f"{field_type} literal {value!r} is outside Python's date range") from error
    if isinstance(field_type, t.UUIDType):
        text = value if isinstance(value, uuid.UUID) else uuid.UUID(bytes=bytes(value))
        return f"CAST({quote_string(str(text))} AS UUID)"
    if isinstance(field_type, (t.BinaryType, t.FixedType)):
        return f"from_hex({quote_string(bytes(value).hex())})"
    raise UntranslatableFilter(f"literals of type {field_type} are not supported in DuckDB row filters")


__all__ = [
    "SCAN_RELATION",
    "UntranslatableFilter",
    "evolved_field_ids",
    "literal_sql",
    "parse_row_filter",
    "quote_identifier",
    "quote_string",
    "row_filter_to_sql",
]
