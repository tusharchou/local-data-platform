"""Temporal data quality checks for time series and sensor streams.

These checks catch the faults that make recorded time series unusable: clocks that jump backwards,
two sensors that drift apart, dropped samples and holes in a stream. They follow the rules of
:mod:`local_data_platform.quality.checks`: ``run(df, context=None)`` never raises because of the
data, a missing column is a failed result, and every computation is vectorised with
``pyarrow.compute``.

| Check | Config name | Passes when |
|---|---|---|
| :class:`Monotonic` | ``monotonic`` | a column never decreases (``strict``: always increases), per group |
| :class:`MaxSkew` | ``max_skew`` | two time columns are at most ``max_ms`` apart on every row |
| :class:`RateBelow` | ``rate_below`` | the share of true values in a boolean column is at most ``max_rate`` |
| :class:`MaxGap` | ``max_gap`` | consecutive values of a time column, in time order, are at most ``max_ms`` apart |

Semantics:

* **Time columns.** ``MaxSkew`` and ``MaxGap`` accept timestamp (any unit, with or without a time
  zone), date, duration and numeric columns. A timestamp without a time zone is read as UTC, as in
  ``Freshness``. Numeric columns are read in ``unit`` (``"ms"`` by default), so epoch nanoseconds
  from a robot log work with ``unit="ns"``. A timestamp compared with a number fails the check.
* **Nulls and NaN.** A row with a null (or a floating-point NaN) in a checked value is skipped and
  counted in ``metrics["skipped_rows"]``; pair with ``NotNull`` to forbid nulls. ``RateBelow``
  divides by the non-null values only.
* **Groups.** ``group_by`` names one or more key columns. Rows are compared only with rows of the
  same key; null keys form one group, as in SQL ``GROUP BY``.
* **Order.** ``Monotonic`` compares rows in table order, or in ``order_by`` order when it is given
  (a stable sort, so ties keep table order). ``MaxGap`` always sorts by the time column itself.
* **failing_rows.** ``Monotonic`` counts rows smaller than (or, with ``strict``, not greater than)
  the row before them; ``MaxSkew`` counts rows over the limit; ``MaxGap`` counts the rows that come
  after a gap; ``RateBelow`` counts the true rows of the groups (or the table) over the limit.
"""

import math
import numbers
from collections.abc import Sequence
from dataclasses import dataclass
from functools import reduce
from typing import Any, ClassVar

import pyarrow as pa
import pyarrow.compute as pc

from local_data_platform.quality.checks import (
    SAMPLE_SIZE,
    Check,
    _count_true,
    _fmt_value,
    _normalize_column,
    _normalize_columns,
    _rows,
    _scalar_py,
)
from local_data_platform.quality.report import CheckResult, to_jsonable

_UNIT_NS = {"D": 86_400_000_000_000, "s": 1_000_000_000, "ms": 1_000_000, "us": 1_000, "ns": 1}
_NS_PER_MS = 1_000_000
NUMERIC_UNITS = ("s", "ms", "us", "ns")
"""Units a numeric time column can be read in."""


# --------------------------------------------------------------------------- helpers


class _NotTemporal(TypeError):
    """A column that can't be read as time."""


def _true_positions(mask: pa.ChunkedArray | pa.Array) -> pa.Array:
    """Indices of the true values of a boolean mask.

    The chunks are combined first: ``indices_nonzero`` on a ChunkedArray with no chunks crashes
    pyarrow (seen on 25.0).
    """
    if isinstance(mask, pa.ChunkedArray):
        mask = mask.combine_chunks()
    return pc.indices_nonzero(mask)


def _group_columns(group_by: Any) -> list[str] | None:
    return None if group_by is None else _normalize_columns(group_by)


def _require_number(label: str, value: Any, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise TypeError(f"{label} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError(f"{label} must be a {'positive' if positive else 'non-negative'} number, got {value!r}")
    return value


def _require_unit(unit: Any) -> str:
    if unit not in NUMERIC_UNITS:
        raise ValueError(f"unit must be one of {list(NUMERIC_UNITS)}, got {unit!r}")
    return unit


def _time_kind(data_type: pa.DataType) -> str:
    if pa.types.is_timestamp(data_type) or pa.types.is_date(data_type):
        return "instant"
    if pa.types.is_duration(data_type):
        return "duration"
    if pa.types.is_integer(data_type) or pa.types.is_floating(data_type):
        return "number"
    raise _NotTemporal(f"needs a timestamp, date, duration or numeric column, not {data_type}")


def _time_values(column: pa.ChunkedArray, numeric_unit: str) -> tuple[pa.ChunkedArray, str]:
    """Return ``column`` as counts of a unit (int64, or float64 for floats) and that unit's name."""
    data_type = column.type
    _time_kind(data_type)
    if pa.types.is_timestamp(data_type) or pa.types.is_duration(data_type):
        return pc.cast(column, pa.int64()), data_type.unit
    if pa.types.is_date32(data_type):
        return pc.cast(pc.cast(column, pa.int32()), pa.int64()), "D"
    if pa.types.is_date64(data_type):
        return pc.cast(column, pa.int64()), "ms"
    if pa.types.is_floating(data_type):
        return pc.cast(column, pa.float64()), numeric_unit
    return pc.cast(column, pa.int64()), numeric_unit


def _rescale(values: pa.ChunkedArray, unit: str, target: str) -> pa.ChunkedArray:
    """Convert counts of ``unit`` to counts of the finer ``target`` unit."""
    factor = _UNIT_NS[unit] // _UNIT_NS[target]
    if factor == 1:
        return values
    if pa.types.is_floating(values.type):
        return pc.multiply(values, float(factor))
    return pc.multiply_checked(values, factor)


def _subtract(left: pa.ChunkedArray, right: pa.ChunkedArray) -> pa.ChunkedArray:
    if pa.types.is_floating(left.type) or pa.types.is_floating(right.type):
        return pc.subtract(pc.cast(left, pa.float64()), pc.cast(right, pa.float64()))
    return pc.subtract_checked(left, right)


def _to_ms(value: Any, unit: str) -> float | None:
    if value is None:
        return None
    return round(float(value) * _UNIT_NS[unit] / _NS_PER_MS, 3)


def _limit_in(max_ms: float, unit: str) -> float:
    return float(max_ms) * _NS_PER_MS / _UNIT_NS[unit]


def _checkable(values: pa.ChunkedArray) -> pa.ChunkedArray:
    """True where a value is neither null nor NaN."""
    keep = pc.is_valid(values)
    if pa.types.is_floating(values.type):
        keep = pc.fill_null(pc.and_kleene(keep, pc.invert(pc.is_nan(values))), False)
    return keep


def _same_as_previous(table: pa.Table, columns: Sequence[str]) -> pa.ChunkedArray | None:
    """For rows 1..n-1 of a sorted table: whether each row has the same key as the row before it.

    Null keys equal each other, so they form one group. Returns ``None`` when there are no key
    columns, meaning every row is in the same group.
    """
    if not columns:
        return None
    count = table.num_rows
    masks = []
    for column in columns:
        values = table.column(column)
        current, previous = values.slice(1), values.slice(0, count - 1)
        equal = pc.equal(current, previous)
        if values.null_count:
            equal = pc.or_kleene(equal, pc.and_(pc.is_null(current), pc.is_null(previous)))
        masks.append(pc.fill_null(equal, False))
    return reduce(pc.and_, masks)


def _group_key(table: pa.Table, columns: Sequence[str], index: int, labels: Sequence[str] | None = None) -> Any:
    """The key of row ``index``: a value for one key column, a ``{label: value}`` dict for several."""
    values = [to_jsonable(_scalar_py(table.column(column)[index])) for column in columns]
    return values[0] if len(values) == 1 else dict(zip(labels or columns, values))


def _by(columns: Sequence[str] | None) -> str:
    return f" by {', '.join(columns)}" if columns else ""


def _within(columns: Sequence[str] | None) -> str:
    return f" within each {', '.join(columns)}" if columns else ""


def _prepare(table: pa.Table, value_columns: Sequence[str], sort_keys: Sequence[str]
             ) -> tuple[pa.Table, pa.Array, int]:
    """Drop rows with a null/NaN value, then stable-sort by ``sort_keys``.

    Returns:
        The rows kept, sorted; each kept row's index in ``table``; and how many rows were dropped.
    """
    needed = list(dict.fromkeys([*sort_keys, *value_columns]))
    data = table.select(needed)
    keep = reduce(pc.and_, [_checkable(data.column(column)) for column in value_columns])
    positions = _true_positions(keep)
    data = data.filter(keep)
    if sort_keys and data.num_rows > 1:
        order = pc.sort_indices(data, sort_keys=[(key, "ascending") for key in sort_keys])
        data = data.take(order)
        positions = positions.take(order)
    return data, positions, table.num_rows - data.num_rows


# --------------------------------------------------------------------------- checks


@dataclass
class Monotonic(Check):
    """A column never decreases (``strict``: always increases), optionally within each group.

    Rows are compared in table order, or in ``order_by`` order when given. Use ``order_by`` for
    data that may arrive shuffled: ``Monotonic("rgb_ts", group_by="episode_id",
    order_by="frame_idx", strict=True)`` says a camera's timestamps rise with the frame index in
    every episode. Works on any column pyarrow can compare (numbers, timestamps, dates, strings).

    Args:
        column: The column that must increase.
        group_by: A key column or list of key columns; rows are compared only within a key.
        strict: Require each value to be greater than the one before it, not merely not smaller.
        order_by: A column giving the row order. Rows with a null ``order_by`` are skipped.
        name: Replaces the default name, e.g. ``"monotonic(rgb_ts by episode_id)"``.
    """

    check_type: ClassVar[str] = "monotonic"
    column: str
    group_by: str | Sequence[str] | None = None
    strict: bool = False
    order_by: str | None = None

    def __post_init__(self) -> None:
        self.column = _normalize_column(self.column)
        self.group_by = _group_columns(self.group_by)
        if not isinstance(self.strict, bool):
            raise TypeError(f"strict must be true or false, got {self.strict!r}")
        if self.order_by is not None:
            self.order_by = _normalize_column(self.order_by)
        super().__post_init__()

    def _default_name(self) -> str:
        return f"monotonic({self.column}{_by(self.group_by)})"

    def _required_columns(self) -> list[str]:
        return list(dict.fromkeys([self.column, *(self.group_by or []), *([self.order_by] if self.order_by else [])]))

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        groups = self.group_by or []
        values_needed = [self.column] + ([self.order_by] if self.order_by else [])
        sort_keys = [*groups, self.order_by] if self.order_by else list(groups)
        data, positions, skipped = _prepare(table, values_needed, sort_keys)
        rows = data.num_rows
        wanted = "strictly increasing" if self.strict else "non-decreasing"
        order = f" in {self.order_by} order" if self.order_by else ""
        metrics: dict[str, Any] = {"row_count": table.num_rows, "checked_rows": rows, "skipped_rows": skipped,
                                   "strict": self.strict}
        if rows < 2:
            metrics.update({"groups": rows, "violations": 0})
            return CheckResult(self.name, True, f"{self.column} has {_rows(rows)} to compare, nothing to check",
                               0, metrics)
        values = data.column(self.column)
        current, previous = values.slice(1), values.slice(0, rows - 1)
        bad = pc.less_equal(current, previous) if self.strict else pc.less(current, previous)
        same = _same_as_previous(data, groups)
        if same is not None:
            bad = pc.and_(bad, same)
        bad = pc.fill_null(bad, False)
        failing = _count_true(bad)
        metrics["groups"] = 1 if same is None else rows - _count_true(same)
        metrics["violations"] = failing
        if not failing:
            details = f"{self.column} is {wanted}{order}{_within(groups)} over {_rows(rows)}"
            return CheckResult(self.name, True, details, 0, metrics)
        sample = []
        for hit in _true_positions(bad).slice(0, SAMPLE_SIZE).to_pylist():
            item = {"row": positions[hit + 1].as_py(), "value": to_jsonable(_scalar_py(values[hit + 1])),
                    "previous": to_jsonable(_scalar_py(values[hit]))}
            if groups:
                item["group"] = _group_key(data, groups, hit + 1)
            sample.append(item)
        metrics["sample"] = sample
        first = sample[0]
        where = f" in group {_fmt_value(first['group'])}" if groups else ""
        verb = "is not greater than" if self.strict else "is less than"
        details = (f"{failing} of {_rows(rows)} break the {wanted} order of {self.column}{order}{_within(groups)}; "
                   f"e.g. row {first['row']}{where}: {_fmt_value(first['value'])} {verb} "
                   f"{_fmt_value(first['previous'])}")
        return CheckResult(self.name, False, details, failing, metrics)


@dataclass
class MaxSkew(Check):
    """Two time columns are at most ``max_ms`` milliseconds apart on every row.

    Use it for sensors that must be synchronised, e.g. an RGB and a depth camera stamping the
    same frame. Rows where either value is null are skipped.

    Args:
        column_a: The first time column.
        column_b: The second time column. Both must be instants (timestamps or dates), both
            durations, or both numbers.
        max_ms: The largest allowed ``|column_a - column_b|``, in milliseconds (0 or more).
        unit: The unit numeric columns are in: ``"s"``, ``"ms"`` (default), ``"us"`` or ``"ns"``.
        name: Replaces the default name, e.g. ``"max_skew(rgb_ts, depth_ts)"``.
    """

    check_type: ClassVar[str] = "max_skew"
    column_a: str
    column_b: str
    max_ms: float
    unit: str = "ms"

    def __post_init__(self) -> None:
        self.column_a = _normalize_column(self.column_a)
        self.column_b = _normalize_column(self.column_b)
        self.max_ms = _require_number("max_ms", self.max_ms, positive=False)
        self.unit = _require_unit(self.unit)
        super().__post_init__()

    def _default_name(self) -> str:
        return f"max_skew({self.column_a}, {self.column_b})"

    def _required_columns(self) -> list[str]:
        return list(dict.fromkeys([self.column_a, self.column_b]))

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        column_a, column_b = table.column(self.column_a), table.column(self.column_b)
        metrics: dict[str, Any] = {"row_count": table.num_rows, "max_ms": self.max_ms,
                                   "column_types": [str(column_a.type), str(column_b.type)]}
        try:
            kinds = (_time_kind(column_a.type), _time_kind(column_b.type))
        except _NotTemporal as exc:
            return CheckResult(self.name, False, f"max_skew {exc}", 0, metrics)
        if kinds[0] != kinds[1]:
            details = (f"cannot compare {self.column_a} ({column_a.type}) with {self.column_b} ({column_b.type}): "
                       "both must be timestamps or dates, both durations, or both numbers")
            return CheckResult(self.name, False, details, 0, metrics)
        values_a, unit_a = _time_values(column_a, self.unit)
        values_b, unit_b = _time_values(column_b, self.unit)
        unit = min(unit_a, unit_b, key=_UNIT_NS.__getitem__)
        skew = _subtract(_rescale(values_a, unit_a, unit), _rescale(values_b, unit_b, unit))
        skew = pc.abs(skew) if pa.types.is_floating(skew.type) else pc.abs_checked(skew)
        checked = pc.fill_null(pc.and_kleene(_checkable(values_a), _checkable(values_b)), False)
        over = pc.fill_null(pc.and_(checked, pc.greater(skew, _limit_in(self.max_ms, unit))), False)
        failing = _count_true(over)
        rows = _count_true(checked)
        checked_skew = skew.filter(checked)
        metrics.update({
            "checked_rows": rows,
            "skipped_rows": table.num_rows - rows,
            "max_skew_ms": _to_ms(pc.max(checked_skew).as_py(), unit),
            "mean_skew_ms": _to_ms(pc.mean(checked_skew).as_py(), unit),
            "rows_over_limit": failing,
        })
        pair = f"|{self.column_a} - {self.column_b}|"
        if not failing:
            details = f"{pair} <= {self.max_ms} ms on {_rows(rows)} (max {metrics['max_skew_ms']} ms)"
            return CheckResult(self.name, True, details, 0, metrics)
        hits = _true_positions(over).slice(0, SAMPLE_SIZE).to_pylist()
        metrics["sample"] = [{"row": hit, "skew_ms": _to_ms(skew[hit].as_py(), unit)} for hit in hits]
        details = (f"{failing} of {_rows(rows)} have {pair} > {self.max_ms} ms "
                   f"(max {metrics['max_skew_ms']} ms); e.g. row {hits[0]}: {metrics['sample'][0]['skew_ms']} ms")
        return CheckResult(self.name, False, details, failing, metrics)


@dataclass
class RateBelow(Check):
    """The share of true values in a boolean column is at most ``max_rate``.

    The rate is ``true / non-null``; an all-null column or group has rate 0. With ``group_by``
    every group must be at or under the limit, e.g. the dropped-frame rate of each episode.

    Args:
        predicate_column: A boolean column, such as a ``dropped`` flag.
        max_rate: The largest allowed rate, from 0 to 1 (inclusive).
        group_by: A key column or list of key columns; the rate is checked per key.
        name: Replaces the default name, e.g. ``"rate_below(dropped)"``.
    """

    check_type: ClassVar[str] = "rate_below"
    predicate_column: str
    max_rate: float
    group_by: str | Sequence[str] | None = None

    def __post_init__(self) -> None:
        self.predicate_column = _normalize_column(self.predicate_column)
        self.max_rate = _require_number("max_rate", self.max_rate, positive=False)
        if self.max_rate > 1:
            raise ValueError(f"max_rate must be between 0 and 1, got {self.max_rate!r}")
        self.group_by = _group_columns(self.group_by)
        super().__post_init__()

    def _default_name(self) -> str:
        return f"rate_below({self.predicate_column}{_by(self.group_by)})"

    def _required_columns(self) -> list[str]:
        return list(dict.fromkeys([self.predicate_column, *(self.group_by or [])]))

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        column = table.column(self.predicate_column)
        known = table.num_rows - column.null_count
        true = _count_true(column) if pa.types.is_boolean(column.type) else 0
        rate = true / known if known else 0.0
        metrics: dict[str, Any] = {"row_count": table.num_rows, "null_count": column.null_count,
                                   "true_count": true, "rate": round(rate, 6), "max_rate": self.max_rate}
        if not pa.types.is_boolean(column.type):
            details = f"rate_below needs a boolean column; {self.predicate_column} is {column.type}"
            return CheckResult(self.name, False, details, 0, metrics)
        label = f"{self.predicate_column} rate"
        if not self.group_by:
            passed = rate <= self.max_rate
            relation = "<=" if passed else ">"
            details = f"{label} {rate:.2%} ({true} of {known}) {relation} {self.max_rate:.2%}"
            return CheckResult(self.name, passed, details, 0 if passed else true, metrics)
        return self._evaluate_groups(table, metrics, label)

    def _evaluate_groups(self, table: pa.Table, metrics: dict[str, Any], label: str) -> CheckResult:
        groups = list(self.group_by)
        # Alias the columns so no key can collide with the aggregate output names.
        aliases = [f"k{index}" for index in range(len(groups))]
        data = table.select([*groups, self.predicate_column]).rename_columns([*aliases, "p"])
        counts = data.group_by(aliases, use_threads=False).aggregate([("p", "sum"), ("p", "count")])
        true = pc.cast(pc.fill_null(counts.column("p_sum"), 0), pa.float64())
        known = pc.cast(counts.column("p_count"), pa.float64())
        rates = pc.if_else(pc.greater(known, 0), pc.divide(true, pc.max_element_wise(known, 1.0)), 0.0)
        counts = counts.append_column("rate", rates)
        failing_mask = pc.greater(rates, self.max_rate)
        failing = counts.filter(failing_mask)
        failing_rows = int(pc.sum(failing.column("p_sum")).as_py() or 0)
        worst = pc.max(rates).as_py()
        metrics.update({"groups": counts.num_rows, "failing_groups": failing.num_rows,
                        "max_group_rate": round(worst, 6) if worst is not None else 0.0})
        if not failing.num_rows:
            details = (f"{label} <= {self.max_rate:.2%} in all {counts.num_rows} {', '.join(groups)} groups "
                       f"(worst {metrics['max_group_rate']:.2%}, overall {metrics['rate']:.2%})")
            return CheckResult(self.name, True, details, 0, metrics)
        sort_keys = [("rate", "descending"), *[(alias, "ascending") for alias in aliases]]
        order = pc.sort_indices(failing, sort_keys=sort_keys)
        head = failing.take(order).slice(0, SAMPLE_SIZE)
        sample = [{"group": _group_key(head, aliases, index, labels=groups),
                   "rate": round(head.column("rate")[index].as_py(), 6),
                   "true_count": head.column("p_sum")[index].as_py(), "count": head.column("p_count")[index].as_py()}
                  for index in range(head.num_rows)]
        metrics["sample"] = sample
        examples = ", ".join(f"{_fmt_value(item['group'])} {item['rate']:.2%}" for item in sample)
        details = (f"{failing.num_rows} of {counts.num_rows} {', '.join(groups)} groups have a {label} over "
                   f"{self.max_rate:.2%}; e.g. {examples}")
        return CheckResult(self.name, False, details, failing_rows, metrics)


@dataclass
class MaxGap(Check):
    """Consecutive values of a time column, in time order, are at most ``max_ms`` apart.

    It finds holes in a stream, such as frames missing from a recording. Values are sorted
    within each group, so the table's row order doesn't matter; duplicates are a gap of 0.

    Args:
        column: The time column.
        max_ms: The largest allowed gap, in milliseconds (greater than 0).
        group_by: A key column or list of key columns, e.g. ``"episode_id"``; gaps are measured
            within each key. ``None`` treats the whole table as one stream.
        unit: The unit a numeric column is in: ``"s"``, ``"ms"`` (default), ``"us"`` or ``"ns"``.
        name: Replaces the default name, e.g. ``"max_gap(rgb_ts by episode_id)"``.
    """

    check_type: ClassVar[str] = "max_gap"
    column: str
    max_ms: float
    group_by: str | Sequence[str] | None = None
    unit: str = "ms"

    def __post_init__(self) -> None:
        self.column = _normalize_column(self.column)
        self.max_ms = _require_number("max_ms", self.max_ms, positive=True)
        self.group_by = _group_columns(self.group_by)
        self.unit = _require_unit(self.unit)
        super().__post_init__()

    def _default_name(self) -> str:
        return f"max_gap({self.column}{_by(self.group_by)})"

    def _required_columns(self) -> list[str]:
        return list(dict.fromkeys([self.column, *(self.group_by or [])]))

    def _evaluate(self, table: pa.Table, context: dict[str, Any]) -> CheckResult:
        groups = self.group_by or []
        metrics: dict[str, Any] = {"row_count": table.num_rows, "max_ms": self.max_ms,
                                   "column_type": str(table.column(self.column).type)}
        try:
            _time_kind(table.column(self.column).type)
        except _NotTemporal as exc:
            return CheckResult(self.name, False, f"max_gap {exc}", 0, metrics)
        data, positions, skipped = _prepare(table, [self.column], [*groups, self.column])
        rows = data.num_rows
        metrics.update({"checked_rows": rows, "skipped_rows": skipped})
        if rows < 2:
            metrics.update({"groups": rows, "max_gap_ms": None, "gaps_over_limit": 0})
            return CheckResult(self.name, True, f"{self.column} has {_rows(rows)} to compare, nothing to check",
                               0, metrics)
        values, unit = _time_values(data.column(self.column), self.unit)
        gaps = _subtract(values.slice(1), values.slice(0, rows - 1))
        same = _same_as_previous(data, groups)
        over = pc.greater(gaps, _limit_in(self.max_ms, unit))
        if same is not None:
            over, gaps_in_groups = pc.and_(same, over), gaps.filter(same)
        else:
            gaps_in_groups = gaps
        over = pc.fill_null(over, False)
        failing = _count_true(over)
        metrics.update({
            "groups": 1 if same is None else rows - _count_true(same),
            "max_gap_ms": _to_ms(pc.max(gaps_in_groups).as_py(), unit),
            "gaps_over_limit": failing,
        })
        if not failing:
            details = (f"no gap in {self.column} over {self.max_ms} ms{_within(groups)} "
                       f"(max {metrics['max_gap_ms']} ms over {_rows(rows)})")
            return CheckResult(self.name, True, details, 0, metrics)
        stamps = data.column(self.column)
        sample = []
        for hit in _true_positions(over).slice(0, SAMPLE_SIZE).to_pylist():
            item = {"row": positions[hit + 1].as_py(), "gap_ms": _to_ms(gaps[hit].as_py(), unit),
                    "from": to_jsonable(_scalar_py(stamps[hit])), "to": to_jsonable(_scalar_py(stamps[hit + 1]))}
            if groups:
                item["group"] = _group_key(data, groups, hit + 1)
            sample.append(item)
        metrics["sample"] = sample
        first = sample[0]
        where = f" in group {_fmt_value(first['group'])}" if groups else ""
        details = (f"{failing} gap(s) in {self.column} over {self.max_ms} ms{_within(groups)} "
                   f"(max {metrics['max_gap_ms']} ms); e.g. {first['gap_ms']} ms{where} before row {first['row']}")
        return CheckResult(self.name, False, details, failing, metrics)


TEMPORAL_CHECKS: tuple[type[Check], ...] = (Monotonic, MaxSkew, RateBelow, MaxGap)
"""The temporal check classes, registered in :data:`local_data_platform.quality.CHECKS`."""

__all__ = ["NUMERIC_UNITS", "TEMPORAL_CHECKS", "MaxGap", "MaxSkew", "Monotonic", "RateBelow"]
