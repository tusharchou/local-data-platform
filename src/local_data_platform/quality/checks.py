"""Data quality checks over pyarrow tables.

Every check has ``run(df, context=None) -> CheckResult`` and never raises because of the data:
a missing column, a column of the wrong type or a comparison pyarrow can't do gives a failed
:class:`~local_data_platform.quality.report.CheckResult`. Bad *parameters* are caught when the
check is built and raise ``ValueError`` or ``TypeError``
(:func:`~local_data_platform.quality.runner.checks_from_config` turns those into ``ConfigError``).

Semantics:

* **Nulls.** ``NotNull`` counts Arrow nulls; a floating-point NaN is a value, not a null.
  ``AcceptedValues`` and ``Range`` skip nulls, so pair them with ``NotNull`` to forbid nulls.
  ``Range`` counts NaN as out of range, since NaN lies inside no interval.
* **Unique and nulls.** ``Unique`` follows SQL ``UNIQUE`` and dbt's ``unique`` test: a row with a
  null in *any* key column takes no part in the uniqueness test, so two rows keyed ``(1, null)``
  are not duplicates. Those rows are counted in ``metrics["null_key_rows"]``. Add ``NotNull`` on
  the key columns to forbid them.
* **failing_rows.** Row-level checks report the rows that violate them. ``Unique`` counts every
  copy of a duplicated key, so two rows sharing a key give ``failing_rows == 2``. Table-level
  checks (``RowCount``, ``Freshness``, ``SchemaHas``), and checks that could not run because a
  column is missing, report 0 and put the numbers in ``metrics``.
* **Time.** ``Freshness`` and timestamp bounds in ``Range`` compare instants. A timestamp column
  without a time zone, and a naive ``datetime``, are read as UTC. Any timestamp unit works.
"""

import datetime as dt
import math
import numbers
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import reduce
from typing import Any, ClassVar

import pyarrow as pa
import pyarrow.compute as pc

from local_data_platform.logger import get_logger
from local_data_platform.quality.report import CheckResult, to_jsonable

logger = get_logger(__name__)

SAMPLE_SIZE = 5
"""How many example keys or values a failed check keeps in its metrics."""

_UTC = dt.timezone.utc
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=_UTC)
_MICROSECOND = dt.timedelta(microseconds=1)
_US_PER_HOUR = 3_600_000_000
_TIMESTAMP_RE = re.compile(r"^timestamp\[\s*(s|ms|us|ns)\s*(?:,\s*tz\s*=\s*([^\]]+?)\s*)?\]$", re.IGNORECASE)
_DECIMAL_RE = re.compile(r"^decimal(128|256)?\(\s*(\d+)\s*,\s*(-?\d+)\s*\)$", re.IGNORECASE)


# --------------------------------------------------------------------------- helpers


def as_table(df: Any) -> pa.Table:
    """Return ``df`` as a ``pyarrow.Table``.

    Args:
        df: A ``pyarrow.Table``, a ``pyarrow.RecordBatch`` or anything ``pyarrow.table()``
            accepts (a dict of columns, a pandas DataFrame, an Arrow C stream).

    Returns:
        The table. A ``pyarrow.Table`` is returned unchanged, without copying.

    Raises:
        TypeError: If ``df`` is ``None`` or can't be converted.
    """
    if isinstance(df, pa.Table):
        return df
    if isinstance(df, pa.RecordBatch):
        return pa.Table.from_batches([df])
    if df is None:
        raise TypeError("data quality checks need a pyarrow.Table, got None")
    try:
        return pa.table(df)
    except (TypeError, ValueError, pa.ArrowException) as exc:
        raise TypeError(f"data quality checks need a pyarrow.Table, got {type(df).__name__}: {exc}") from exc


def parse_type(spec: str | pa.DataType) -> pa.DataType:
    """Parse a pyarrow type string such as ``"int64"`` or ``"timestamp[us, tz=UTC]"``.

    Accepts every alias ``pyarrow.type_for_alias`` knows (``int64``, ``double``, ``string``,
    ``bool``, ``date32``, ``timestamp[ms]``, ...), timestamps with a time zone written the way
    pyarrow prints them, and ``decimal128(p, s)`` / ``decimal256(p, s)`` / ``decimal(p, s)``.

    Args:
        spec: The type string, or a ``pyarrow.DataType`` (returned unchanged).

    Returns:
        The pyarrow type.

    Raises:
        TypeError: If ``spec`` is not a string or a ``pyarrow.DataType``.
        ValueError: If the string names no pyarrow type.
    """
    if isinstance(spec, pa.DataType):
        return spec
    if not isinstance(spec, str) or not spec.strip():
        raise TypeError(f"a column type must be a pyarrow type string such as 'int64', got {spec!r}")
    text = spec.strip()
    match = _TIMESTAMP_RE.match(text)
    if match:
        return pa.timestamp(match.group(1).lower(), tz=match.group(2))
    match = _DECIMAL_RE.match(text)
    if match:
        factory = pa.decimal256 if match.group(1) == "256" else pa.decimal128
        return factory(int(match.group(2)), int(match.group(3)))
    try:
        return pa.type_for_alias(text)
    except ValueError:
        raise ValueError(
            f"unknown pyarrow type {spec!r}; use a name such as 'int64', 'double', 'string', 'bool', "
            "'date32', 'timestamp[us]', 'timestamp[us, tz=UTC]' or 'decimal128(10, 2)'"
        ) from None


def _normalize_columns(columns: Any) -> list[str]:
    """Return ``columns`` (a name or a list of names) as a de-duplicated list of names."""
    if isinstance(columns, str):
        columns = [columns]
    if isinstance(columns, Mapping) or not isinstance(columns, Iterable):
        raise TypeError(f"columns must be a column name or a list of names, got {columns!r}")
    names = list(columns)
    if not names:
        raise ValueError("columns must name at least one column")
    for name in names:
        _normalize_column(name)
    return list(dict.fromkeys(names))


def _normalize_column(column: Any) -> str:
    if not isinstance(column, str) or not column:
        raise TypeError(f"a column name must be a non-empty string, got {column!r}")
    return column


def _require_count(label: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, numbers.Integral):
        raise TypeError(f"{label} must be a non-negative integer, got {value!r}")
    if value < 0:
        raise ValueError(f"{label} must be a non-negative integer, got {value!r}")
    return int(value)


def _count_true(mask: pa.ChunkedArray | pa.Array) -> int:
    """Count the true values in a boolean array; nulls count as false."""
    return int(pc.sum(mask).as_py() or 0)


def _rows(count: int) -> str:
    return f"{count} row" if count == 1 else f"{count} rows"


def _fmt_value(value: Any) -> str:
    return repr(value) if isinstance(value, str) else str(to_jsonable(value))


def _fmt_list(values: Sequence[Any], limit: int = 10) -> str:
    shown = ", ".join(_fmt_value(value) for value in values[:limit])
    more = f", ... (+{len(values) - limit} more)" if len(values) > limit else ""
    return f"[{shown}{more}]"


def _to_pylist(values: pa.Array | pa.ChunkedArray) -> list[Any]:
    """``to_pylist`` that survives nanosecond timestamps when pandas is not installed."""
    try:
        return values.to_pylist()
    except (ValueError, pa.ArrowException):
        return pc.cast(values, pa.string()).to_pylist()


def _scalar_py(scalar: pa.Scalar) -> Any:
    try:
        return scalar.as_py()
    except (ValueError, pa.ArrowException):
        return pc.cast(scalar, pa.string()).as_py()


def _column_problem(name: str, table: pa.Table, columns: Sequence[str]) -> CheckResult | None:
    """Return a failed result if a column is missing from ``table`` or appears more than once."""
    names = table.column_names
    missing = [column for column in columns if column not in names]
    ambiguous = [column for column in columns if names.count(column) > 1]
    if not missing and not ambiguous:
        return None
    problems = []
    metrics: dict[str, Any] = {"row_count": table.num_rows, "missing_columns": missing}
    if missing:
        problems.append(f"missing column(s) {_fmt_list(missing)}")
    if ambiguous:
        problems.append(f"column(s) {_fmt_list(ambiguous)} appear more than once")
        metrics["ambiguous_columns"] = ambiguous
    details = "; ".join(problems) + f"; the table has {_fmt_list(names, limit=20)}"
    return CheckResult(name, False, details, 0, metrics)


def _utc(value: dt.datetime) -> dt.datetime:
    """Return an aware UTC datetime, reading a naive one as UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=_UTC)
    return value.astimezone(_UTC)


def _to_datetime(value: Any) -> dt.datetime:
    if isinstance(value, str):
        return dt.datetime.fromisoformat(value.strip())
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    raise TypeError(f"expected a datetime, a date or an ISO-8601 string, got {value!r}")


def _to_date(value: Any) -> dt.date:
    if isinstance(value, str):
        text = value.strip()
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            return dt.datetime.fromisoformat(text).date()
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise TypeError(f"expected a date, a datetime or an ISO-8601 string, got {value!r}")


def _coerce_bound(value: Any, column_type: pa.DataType) -> pa.Scalar:
    """Turn a Python bound into a scalar pyarrow can compare with a column of ``column_type``."""
    if isinstance(value, pa.Scalar):
        return value
    if pa.types.is_timestamp(column_type):
        instant = _utc(_to_datetime(value))
        if column_type.tz is None:
            instant = instant.replace(tzinfo=None)
        # Keep microseconds rather than casting to the column's unit, so a bound is never truncated.
        return pa.scalar(instant, type=pa.timestamp("us", tz=column_type.tz))
    if pa.types.is_date(column_type):
        return pa.scalar(_to_date(value), type=column_type)
    numeric = (pa.types.is_integer(column_type) or pa.types.is_floating(column_type)
               or pa.types.is_decimal(column_type))
    if isinstance(value, numbers.Number):
        if not numeric:
            # Casting 10 to "10" would compare strings, where "10" < "9"; refuse instead.
            raise TypeError(f"a numeric bound needs a numeric column, not {column_type}")
        # Let the comparison kernel promote the types, so 0.5 is not truncated to 0 on an int column.
        return pa.scalar(value)
    scalar = pa.scalar(value)
    if scalar.type != column_type:
        try:
            return scalar.cast(column_type)
        except (pa.ArrowException, TypeError, ValueError) as exc:
            raise TypeError(f"cannot use {value!r} as a {column_type} bound") from exc
    return scalar


def _value_set(values: list[Any], column_type: pa.DataType) -> pa.Array:
    """Build the ``is_in`` value set, typed like the column when the values allow it."""
    target = column_type.value_type if pa.types.is_dictionary(column_type) else column_type
    try:
        return pa.array(values, type=target)
    except (pa.ArrowException, TypeError, ValueError):
        pass
    try:
        return pa.array(values).cast(target)
    except (pa.ArrowException, TypeError, ValueError):
        return pa.array(values)


def _epoch_us(scalar: pa.Scalar, column_type: pa.DataType) -> int:
    """Microseconds since the epoch for a non-null timestamp or date scalar."""
    if pa.types.is_timestamp(column_type):
        raw = scalar.value
        unit = column_type.unit
        if unit == "s":
            return raw * 1_000_000
        if unit == "ms":
            return raw * 1_000
        if unit == "us":
            return raw
        return raw // 1_000
    day = dt.datetime.combine(scalar.as_py(), dt.time(), tzinfo=_UTC)
    return (day - _EPOCH) // _MICROSECOND


def _iso_from_epoch_us(value: int) -> str | None:
    try:
        return (_EPOCH + dt.timedelta(microseconds=value)).isoformat()
    except OverflowError:
        return None


# --------------------------------------------------------------------------- checks


@dataclass
class Check(ABC):
    """Base class for data quality checks.

    Subclasses are dataclasses: they validate their parameters in ``__post_init__`` and
    implement :meth:`_evaluate`. Every check also takes a keyword-only ``name`` that replaces
    the default name (e.g. ``"not_null(ride_id)"``) in results.

    Attributes:
        check_type: The ``check`` key that names this check in a config.
        name: The name used in results.
    """

    check_type: ClassVar[str] = ""
    name: str | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.name is None:
            self.name = self._default_name()
        elif not isinstance(self.name, str) or not self.name.strip():
            raise ValueError(f"name must be a non-empty string, got {self.name!r}")

    def _default_name(self) -> str:
        return self.check_type

    def _required_columns(self) -> list[str]:
        """Columns that must exist; if one is missing the check fails without evaluating."""
        return []

    def run(self, df: Any, context: Mapping[str, Any] | None = None) -> CheckResult:
        """Run the check.

        Args:
            df: The data, as a ``pyarrow.Table`` (see :func:`as_table` for other inputs).
            context: Extra facts about the load, e.g. ``{"source_rows": 1000}``.

        Returns:
            The result. Problems with the data give a failed result, never an exception.
        """
        table = as_table(df)
        problem = _column_problem(self.name, table, self._required_columns())
        if problem is not None:
            return problem
        try:
            return self._evaluate(table, dict(context or {}))
        except pa.ArrowException as exc:
            logger.debug("check %s could not be evaluated: %s", self.name, exc)
            return CheckResult(self.name, False, f"could not be evaluated: {exc}", 0,
                               {"row_count": table.num_rows, "error": type(exc).__name__})

    @abstractmethod
    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        """Evaluate the check on a table that has every required column."""


@dataclass
class RowCount(Check):
    """The table's row count is within bounds, or equals a number.

    Args:
        min: The fewest rows allowed (inclusive).
        max: The most rows allowed (inclusive).
        equals: An exact row count, or ``"source"`` to compare with ``context["source_rows"]``
            (the rows read from the source). Without that context the check fails.
        name: Replaces the default name ``"row_count"``.
    """

    check_type: ClassVar[str] = "row_count"
    min: int | None = None
    max: int | None = None
    equals: int | str | None = None

    def __post_init__(self) -> None:
        if self.min is None and self.max is None and self.equals is None:
            raise ValueError("row_count needs at least one of min, max or equals")
        if self.min is not None:
            self.min = _require_count("min", self.min)
        if self.max is not None:
            self.max = _require_count("max", self.max)
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min ({self.min}) is greater than max ({self.max})")
        if self.equals is not None and self.equals != "source":
            if isinstance(self.equals, str):
                raise ValueError(f"equals must be a non-negative integer or 'source', got {self.equals!r}")
            self.equals = _require_count("equals", self.equals)
        super().__post_init__()

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        rows = table.num_rows
        metrics: dict[str, Any] = {"row_count": rows}
        expectations: list[tuple[str, bool]] = []
        if self.min is not None:
            metrics["min"] = self.min
            expectations.append((f">= {self.min}", rows >= self.min))
        if self.max is not None:
            metrics["max"] = self.max
            expectations.append((f"<= {self.max}", rows <= self.max))
        if self.equals is not None:
            if self.equals == "source":
                source_rows = context.get("source_rows")
                if isinstance(source_rows, bool) or not isinstance(source_rows, numbers.Integral):
                    return CheckResult(
                        self.name, False,
                        f"{_rows(rows)}; equals='source' needs an integer context['source_rows'], "
                        f"got {source_rows!r}",
                        0, metrics,
                    )
                expected = int(source_rows)
                label = f"== {expected} (source rows)"
                metrics["source_rows"] = expected
            else:
                expected = self.equals
                label = f"== {expected}"
            metrics["expected"] = expected
            metrics["difference"] = rows - expected
            expectations.append((label, rows == expected))
        passed = all(ok for _, ok in expectations)
        wanted = " and ".join(label for label, _ in expectations)
        details = f"{_rows(rows)} (expected {wanted})" if passed else f"{_rows(rows)}, expected {wanted}"
        return CheckResult(self.name, passed, details, 0, metrics)


@dataclass
class NotNull(Check):
    """No null values in the given columns.

    ``failing_rows`` counts rows with a null in at least one of the columns; per-column counts
    are in ``metrics["null_counts"]``. A floating-point NaN is not a null.

    Args:
        columns: A column name or a list of names.
        name: Replaces the default name, e.g. ``"not_null(ride_id, pickup_ts)"``.
    """

    check_type: ClassVar[str] = "not_null"
    columns: str | Sequence[str]

    def __post_init__(self) -> None:
        self.columns = _normalize_columns(self.columns)
        super().__post_init__()

    def _default_name(self) -> str:
        return f"not_null({', '.join(self.columns)})"

    def _required_columns(self) -> list[str]:
        return list(self.columns)

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        null_counts = {column: table.column(column).null_count for column in self.columns}
        masks = [pc.is_null(table.column(column)) for column, count in null_counts.items() if count]
        failing = _count_true(reduce(pc.or_, masks)) if masks else 0
        metrics = {"row_count": table.num_rows, "null_counts": null_counts}
        columns = ", ".join(self.columns)
        if not failing:
            return CheckResult(self.name, True, f"no nulls in {columns} ({_rows(table.num_rows)})", 0, metrics)
        per_column = ", ".join(f"{column}={count}" for column, count in null_counts.items() if count)
        details = f"{failing} of {_rows(table.num_rows)} have a null in {columns} ({per_column})"
        return CheckResult(self.name, False, details, failing, metrics)


@dataclass
class Unique(Check):
    """Each combination of the given columns appears at most once.

    Rows with a null in any key column are left out of the test (SQL ``UNIQUE`` semantics) and
    counted in ``metrics["null_key_rows"]``. ``failing_rows`` counts every row whose key is
    duplicated, including the first copy. ``metrics["sample"]`` lists up to five duplicated keys
    with their counts, in the order they first appear; a key is a value for one column and a
    ``{column: value}`` dict for several.

    Args:
        columns: A column name or a list of names forming a composite key.
        name: Replaces the default name, e.g. ``"unique(ride_id)"``.
    """

    check_type: ClassVar[str] = "unique"
    columns: str | Sequence[str]

    def __post_init__(self) -> None:
        self.columns = _normalize_columns(self.columns)
        super().__post_init__()

    def _default_name(self) -> str:
        return f"unique({', '.join(self.columns)})"

    def _required_columns(self) -> list[str]:
        return list(self.columns)

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        keys = table.select(self.columns)
        valid_masks = [pc.is_valid(keys.column(column)) for column in self.columns if keys.column(column).null_count]
        if valid_masks:
            keys = keys.filter(reduce(pc.and_, valid_masks))
        null_key_rows = table.num_rows - keys.num_rows
        # Rename the keys so no key column can collide with the "count_all" output column.
        aliases = [f"k{index}" for index in range(len(self.columns))]
        counts = keys.rename_columns(aliases).group_by(aliases, use_threads=False).aggregate([([], "count_all")])
        duplicates = counts.filter(pc.greater(counts.column("count_all"), 1))
        failing = int(pc.sum(duplicates.column("count_all")).as_py() or 0)
        metrics: dict[str, Any] = {
            "row_count": table.num_rows,
            "null_key_rows": null_key_rows,
            "distinct_keys": counts.num_rows,
            "duplicate_keys": duplicates.num_rows,
            "duplicate_rows": failing,
        }
        columns = ", ".join(self.columns)
        null_note = f"; {_rows(null_key_rows)} with a null key skipped" if null_key_rows else ""
        if not failing:
            details = f"{columns} is unique over {_rows(keys.num_rows)}{null_note}"
            return CheckResult(self.name, True, details, 0, metrics)
        head = duplicates.slice(0, SAMPLE_SIZE)
        key_values = [_to_pylist(head.column(alias)) for alias in aliases]
        if len(self.columns) == 1:
            sample_keys: list[Any] = key_values[0]
        else:
            sample_keys = [dict(zip(self.columns, row)) for row in zip(*key_values)]
        sample = [{"key": key, "count": count}
                  for key, count in zip(sample_keys, head.column("count_all").to_pylist())]
        metrics["sample"] = to_jsonable(sample)
        examples = ", ".join(f"{_fmt_value(item['key'])} x{item['count']}" for item in sample)
        details = (f"{duplicates.num_rows} duplicated key(s) in ({columns}) across {_rows(failing)}"
                   f"{null_note}; e.g. {examples}")
        return CheckResult(self.name, False, details, failing, metrics)


@dataclass
class AcceptedValues(Check):
    """Every non-null value of a column is one of the accepted values.

    Nulls are skipped; pair with :class:`NotNull` to forbid them. The values are converted to
    the column's type where possible, so ``[1, 2]`` works on an ``int32`` column and ISO strings
    work on a date column. ``metrics["invalid_values"]`` lists up to five distinct offending
    values, in the order they first appear.

    Args:
        column: The column to check.
        values: The accepted values, as a list.
        name: Replaces the default name, e.g. ``"accepted_values(city)"``.
    """

    check_type: ClassVar[str] = "accepted_values"
    column: str
    values: Sequence[Any]

    def __post_init__(self) -> None:
        self.column = _normalize_column(self.column)
        values = self.values
        if isinstance(values, (str, bytes, Mapping)) or not isinstance(values, Iterable):
            raise TypeError(f"values must be a list of accepted values, e.g. ['NYC', 'BKK'], got {values!r}")
        if isinstance(values, (set, frozenset)):
            try:
                values = sorted(values)
            except TypeError:
                values = sorted(values, key=repr)
        values = list(values)
        if not values:
            raise ValueError("values must list at least one accepted value")
        try:
            pa.array(values)
        except (pa.ArrowException, TypeError, ValueError) as exc:
            raise ValueError(f"values must all have the same type: {exc}") from exc
        self.values = values
        super().__post_init__()

    def _default_name(self) -> str:
        return f"accepted_values({self.column})"

    def _required_columns(self) -> list[str]:
        return [self.column]

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        column = table.column(self.column)
        accepted = pc.is_in(column, value_set=_value_set(self.values, column.type))
        invalid = pc.and_(pc.is_valid(column), pc.invert(accepted))
        failing = _count_true(invalid)
        metrics: dict[str, Any] = {
            "row_count": table.num_rows,
            "null_count": column.null_count,
            "invalid_count": failing,
            "accepted_values": to_jsonable(self.values),
        }
        if not failing:
            details = f"every non-null value of {self.column} is in {_fmt_list(self.values)}"
            return CheckResult(self.name, True, details, 0, metrics)
        offenders = _to_pylist(pc.unique(column.filter(invalid)).slice(0, SAMPLE_SIZE))
        metrics["invalid_values"] = to_jsonable(offenders)
        details = (f"{failing} of {_rows(table.num_rows)} have a {self.column} outside {_fmt_list(self.values)}; "
                   f"e.g. {_fmt_list(offenders)}")
        return CheckResult(self.name, False, details, failing, metrics)


@dataclass
class Range(Check):
    """Every non-null value of a column lies within inclusive bounds.

    Nulls are skipped; NaN counts as out of range. Bounds may be numbers, dates, datetimes or
    ISO-8601 strings (for date and timestamp columns; naive values are read as UTC).

    Args:
        column: The column to check.
        min: The smallest allowed value (inclusive), or ``None`` for no lower bound.
        max: The largest allowed value (inclusive), or ``None`` for no upper bound.
        name: Replaces the default name, e.g. ``"range(fare)"``.
    """

    check_type: ClassVar[str] = "range"
    column: str
    min: Any = None
    max: Any = None

    def __post_init__(self) -> None:
        self.column = _normalize_column(self.column)
        if self.min is None and self.max is None:
            raise ValueError("range needs at least one of min or max")
        for label, bound in (("min", self.min), ("max", self.max)):
            if isinstance(bound, bool):
                raise TypeError(f"{label} must be a number, a date or a string, got {bound!r}")
            if isinstance(bound, float) and math.isnan(bound):
                raise ValueError(f"{label} must not be NaN")
        if self.min is not None and self.max is not None:
            try:
                inverted = self.min > self.max
            except TypeError:
                inverted = False
            if inverted:
                raise ValueError(f"min ({self.min!r}) is greater than max ({self.max!r})")
        super().__post_init__()

    def _default_name(self) -> str:
        return f"range({self.column})"

    def _required_columns(self) -> list[str]:
        return [self.column]

    def _constraint(self) -> str:
        if self.min is not None and self.max is not None:
            return f"{_fmt_value(self.min)} <= {self.column} <= {_fmt_value(self.max)}"
        if self.min is not None:
            return f"{self.column} >= {_fmt_value(self.min)}"
        return f"{self.column} <= {_fmt_value(self.max)}"

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        column = table.column(self.column)
        metrics: dict[str, Any] = {"row_count": table.num_rows, "column_type": str(column.type)}
        try:
            low = None if self.min is None else _coerce_bound(self.min, column.type)
            high = None if self.max is None else _coerce_bound(self.max, column.type)
        except (pa.ArrowException, TypeError, ValueError, OverflowError) as exc:
            details = f"cannot compare {self.column} ({column.type}) with the bounds of {self._constraint()}: {exc}"
            return CheckResult(self.name, False, details, 0, metrics)
        masks = {}
        if low is not None:
            masks["below_min"] = pc.less(column, low)
        if high is not None:
            masks["above_max"] = pc.greater(column, high)
        if pa.types.is_float32(column.type) or pa.types.is_float64(column.type):
            masks["nan_count"] = pc.is_nan(column)
        for key, mask in masks.items():
            metrics[key] = _count_true(mask)
        failing = _count_true(reduce(pc.or_, masks.values()))
        observed = pc.min_max(column)
        metrics["observed_min"] = to_jsonable(_scalar_py(observed["min"]))
        metrics["observed_max"] = to_jsonable(_scalar_py(observed["max"]))
        metrics["null_count"] = column.null_count
        if not failing:
            details = f"every non-null value satisfies {self._constraint()}"
            if column.null_count:
                details += f" ({column.null_count} nulls skipped)"
            return CheckResult(self.name, True, details, 0, metrics)
        parts = [f"{metrics.get('below_min', 0)} below min", f"{metrics.get('above_max', 0)} above max"]
        if metrics.get("nan_count"):
            parts.append(f"{metrics['nan_count']} NaN")
        details = f"{failing} of {_rows(table.num_rows)} violate {self._constraint()}: {', '.join(parts)}"
        return CheckResult(self.name, False, details, failing, metrics)


@dataclass
class Freshness(Check):
    """The newest value of a timestamp or date column is at most ``max_age_hours`` old.

    The check fails when the column has no non-null values, or is not a timestamp or date.
    Timestamps of any unit work; a column without a time zone, and a naive ``now``, are read
    as UTC. A date counts as midnight UTC. Values in the future pass.

    Args:
        column: The timestamp or date column.
        max_age_hours: The oldest the newest value may be, in hours (greater than 0).
        now: The reference time: a ``datetime`` or an ISO-8601 string. ``None`` means the
            current time, read each time the check runs.
        name: Replaces the default name, e.g. ``"freshness(pickup_ts)"``.
    """

    check_type: ClassVar[str] = "freshness"
    column: str
    max_age_hours: float
    now: dt.datetime | str | None = None

    def __post_init__(self) -> None:
        self.column = _normalize_column(self.column)
        hours = self.max_age_hours
        if isinstance(hours, bool) or not isinstance(hours, numbers.Real):
            raise TypeError(f"max_age_hours must be a number, got {hours!r}")
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError(f"max_age_hours must be a positive number, got {hours!r}")
        if isinstance(self.now, str):
            try:
                self.now = dt.datetime.fromisoformat(self.now.strip())
            except ValueError as exc:
                raise ValueError(f"now must be an ISO-8601 datetime, got {self.now!r}") from exc
        elif self.now is not None and not isinstance(self.now, dt.datetime):
            raise TypeError(f"now must be a datetime, an ISO-8601 string or None, got {self.now!r}")
        super().__post_init__()

    def _default_name(self) -> str:
        return f"freshness({self.column})"

    def _required_columns(self) -> list[str]:
        return [self.column]

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        column = table.column(self.column)
        column_type = column.type
        metrics: dict[str, Any] = {
            "row_count": table.num_rows,
            "null_count": column.null_count,
            "max_age_hours": self.max_age_hours,
            "column_type": str(column_type),
        }
        if not (pa.types.is_timestamp(column_type) or pa.types.is_date(column_type)):
            details = f"freshness needs a timestamp or date column; {self.column} is {column_type}"
            return CheckResult(self.name, False, details, 0, metrics)
        latest = pc.max(column)
        if not latest.is_valid:
            details = f"{self.column} has no non-null values, so the data can't be shown to be fresh"
            return CheckResult(self.name, False, details, 0, metrics)
        now = _utc(self.now) if self.now is not None else dt.datetime.now(_UTC)
        latest_us = _epoch_us(latest, column_type)
        age_hours = ((now - _EPOCH) // _MICROSECOND - latest_us) / _US_PER_HOUR
        latest_iso = _iso_from_epoch_us(latest_us)
        metrics.update({"latest": latest_iso, "now": now.isoformat(), "age_hours": round(age_hours, 4)})
        passed = age_hours <= self.max_age_hours
        if age_hours < 0:
            age_text = f"{-age_hours:.1f}h in the future"
        else:
            age_text = f"{age_hours:.1f}h old"
        limit = "within" if passed else "older than"
        details = f"newest {self.column} ({latest_iso}) is {age_text}, {limit} the {self.max_age_hours}h limit"
        return CheckResult(self.name, passed, details, 0, metrics)


@dataclass
class SchemaHas(Check):
    """The table has the given columns and, optionally, the given types.

    Extra columns are allowed. Types match exactly: ``"string"`` does not match
    ``large_string`` and ``"timestamp[us]"`` does not match ``timestamp[ns]``.

    Args:
        columns: A list of column names, or a dict of name to pyarrow type string (see
            :func:`parse_type`) or ``pyarrow.DataType``.
        name: Replaces the default name ``"schema"``.
    """

    check_type: ClassVar[str] = "schema"
    columns: Sequence[str] | Mapping[str, str | pa.DataType]

    def __post_init__(self) -> None:
        if isinstance(self.columns, Mapping):
            if not self.columns:
                raise ValueError("columns must name at least one column")
            self._types = {_normalize_column(name): parse_type(spec) for name, spec in self.columns.items()}
            self.columns = dict(self.columns)
        else:
            self.columns = _normalize_columns(self.columns)
            self._types = dict.fromkeys(self.columns)
        super().__post_init__()

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        schema = table.schema
        missing = [name for name in self._types if name not in schema.names]
        ambiguous = [name for name in self._types if schema.names.count(name) > 1]
        mismatches: dict[str, dict[str, str]] = {}
        for name, expected in self._types.items():
            if expected is None or name in missing:
                continue
            actual = [f.type for f in schema if f.name == name]
            if not all(typ.equals(expected) for typ in actual):
                mismatches[name] = {"expected": str(expected), "actual": ", ".join(str(typ) for typ in actual)}
        metrics: dict[str, Any] = {
            "row_count": table.num_rows,
            "missing_columns": missing,
            "type_mismatches": mismatches,
        }
        if ambiguous:
            metrics["ambiguous_columns"] = ambiguous
        if not missing and not mismatches and not ambiguous:
            typed = " with the expected types" if any(t is not None for t in self._types.values()) else ""
            details = f"has the expected column(s) {_fmt_list(list(self._types))}{typed}"
            return CheckResult(self.name, True, details, 0, metrics)
        problems = []
        if missing:
            problems.append(f"missing column(s) {_fmt_list(missing)}")
        problems += [f"{name} is {item['actual']}, expected {item['expected']}" for name, item in mismatches.items()]
        if ambiguous:
            problems.append(f"column(s) {_fmt_list(ambiguous)} appear more than once")
        return CheckResult(self.name, False, "; ".join(problems), 0, metrics)
