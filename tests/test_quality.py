"""Tests for local_data_platform.quality: checks, config mapping, runner and report."""

import datetime as dt
import decimal
import json
import math

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError, DataQualityError, LDPError
from local_data_platform.quality import (
    CHECKS,
    AcceptedValues,
    Check,
    CheckResult,
    Freshness,
    NotNull,
    QualityReport,
    Range,
    RowCount,
    SchemaHas,
    Unique,
    checks_from_config,
    parse_type,
    run_checks,
)

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def rides() -> pa.Table:
    """Five clean rides, split over two chunks so every check sees a real ChunkedArray."""
    batch_1 = pa.record_batch({
        "ride_id": pa.array([1, 2, 3], pa.int64()),
        "city": ["NYC", "BKK", "NYC"],
        "fare": [10.0, 0.0, 25.5],
        "pickup_ts": pa.array([dt.datetime(2026, 9, 29, 12), dt.datetime(2026, 9, 30, 6),
                               dt.datetime(2026, 9, 30, 11)], pa.timestamp("us")),
    })
    batch_2 = pa.record_batch({
        "ride_id": pa.array([4, 5], pa.int64()),
        "city": ["BKK", "NYC"],
        "fare": [7.25, 99.0],
        "pickup_ts": pa.array([dt.datetime(2026, 9, 28, 0), dt.datetime(2026, 9, 30, 10)], pa.timestamp("us")),
    })
    table = pa.Table.from_batches([batch_1, batch_2])
    assert table.column("ride_id").num_chunks == 2
    return table


def empty(table: pa.Table) -> pa.Table:
    return table.schema.empty_table()


# --------------------------------------------------------------------------- report


class TestReport:
    def test_check_result_defaults(self):
        result = CheckResult("x", True, "ok")
        assert result.failing_rows == 0
        assert result.metrics == {}
        assert CheckResult("y", True, "ok").metrics is not result.metrics

    def test_check_result_to_dict_is_json_safe(self):
        result = CheckResult("x", False, "bad", 3, {
            "when": dt.datetime(2026, 1, 1, tzinfo=UTC), "day": dt.date(2026, 1, 2),
            "amount": decimal.Decimal("1.50"), "nan": float("nan"), "inf": float("inf"),
            "nested": {"values": (1, "a", None)}, "tags": {"b", "a"}, "raw": b"\x00a",
            "scalar": pa.scalar(7),
        })
        data = result.to_dict()
        json.dumps(data, allow_nan=False)
        assert data["metrics"]["when"] == "2026-01-01T00:00:00+00:00"
        assert data["metrics"]["day"] == "2026-01-02"
        assert data["metrics"]["amount"] == "1.50"
        assert data["metrics"]["nan"] == "nan"
        assert data["metrics"]["inf"] == "inf"
        assert data["metrics"]["nested"] == {"values": [1, "a", None]}
        assert data["metrics"]["tags"] == ["a", "b"]
        assert data["metrics"]["scalar"] == 7
        assert data["failing_rows"] == 3

    def test_empty_report_passes(self):
        report = QualityReport([])
        assert report.passed
        assert report.failures == []
        assert report.summary() == "Data quality: no checks run"
        report.raise_for_failures()
        assert report.to_dict() == {"passed": True, "checks_run": 0, "checks_failed": 0, "results": []}

    def test_report_passed_failures_and_iteration(self):
        ok = CheckResult("a", True, "fine")
        bad = CheckResult("b", False, "broken", 2)
        report = QualityReport((ok, bad))
        assert isinstance(report.results, list)
        assert not report.passed
        assert report.failures == [bad]
        assert list(report) == [ok, bad]
        assert len(report) == 2

    def test_raise_for_failures(self):
        report = QualityReport([CheckResult("a", True, "fine"), CheckResult("b", False, "broken", 2),
                                CheckResult("c", False, "also broken")])
        with pytest.raises(DataQualityError) as info:
            report.raise_for_failures()
        assert info.value.report is report
        assert isinstance(info.value, LDPError)
        message = str(info.value)
        assert message.startswith("2 of 3 data quality checks failed:")
        assert "  - b: broken" in message
        assert "  - c: also broken" in message
        assert " a:" not in message

    def test_raise_for_failures_returns_none_when_all_pass(self):
        assert QualityReport([CheckResult("a", True, "fine")]).raise_for_failures() is None

    def test_summary(self):
        report = QualityReport([CheckResult("a", True, "fine"), CheckResult("b", False, "broken", 2)])
        lines = report.summary().splitlines()
        assert lines == [
            "Data quality: 1 of 2 checks passed, 1 failed",
            "  [PASS] a: fine",
            "  [FAIL] b: broken [failing rows: 2]",
        ]
        assert QualityReport([CheckResult("a", True, "fine")]).summary().splitlines()[0] == (
            "Data quality: 1 of 1 checks passed")

    def test_to_dict(self):
        report = QualityReport([CheckResult("a", True, "fine"), CheckResult("b", False, "broken", 2, {"k": 1})])
        data = report.to_dict()
        json.dumps(data, allow_nan=False)
        assert data["passed"] is False
        assert data["checks_run"] == 2
        assert data["checks_failed"] == 1
        assert data["results"][1] == {"name": "b", "passed": False, "details": "broken", "failing_rows": 2,
                                      "metrics": {"k": 1}}


# --------------------------------------------------------------------------- row_count


class TestRowCount:
    def test_min_pass_and_fail(self):
        result = RowCount(min=5).run(rides())
        assert result.passed
        assert result.name == "row_count"
        assert result.details == "5 rows (expected >= 5)"
        assert result.metrics == {"row_count": 5, "min": 5}
        failed = RowCount(min=6).run(rides())
        assert not failed.passed
        assert failed.details == "5 rows, expected >= 6"
        assert failed.failing_rows == 0

    def test_max_pass_and_fail(self):
        assert RowCount(max=5).run(rides()).passed
        assert not RowCount(max=4).run(rides()).passed

    def test_min_and_max(self):
        result = RowCount(min=1, max=4).run(rides())
        assert not result.passed
        assert result.details == "5 rows, expected >= 1 and <= 4"

    def test_equals_number(self):
        assert RowCount(equals=5).run(rides()).passed
        result = RowCount(equals=7).run(rides())
        assert not result.passed
        assert result.metrics["expected"] == 7
        assert result.metrics["difference"] == -2

    def test_equals_source(self):
        result = RowCount(equals="source").run(rides(), {"source_rows": 5})
        assert result.passed
        assert result.details == "5 rows (expected == 5 (source rows))"
        assert result.metrics["source_rows"] == 5
        failed = RowCount(equals="source").run(rides(), {"source_rows": 6})
        assert not failed.passed
        assert failed.metrics["difference"] == -1

    @pytest.mark.parametrize("context", [None, {}, {"source_rows": None}, {"source_rows": "5"},
                                         {"source_rows": True}])
    def test_equals_source_without_context_fails(self, context):
        result = RowCount(equals="source").run(rides(), context)
        assert not result.passed
        assert "context['source_rows']" in result.details

    def test_empty_table(self):
        table = empty(rides())
        assert not RowCount(min=1).run(table).passed
        assert RowCount(min=0).run(table).passed
        assert RowCount(equals=0).run(table).passed
        assert RowCount(max=0).run(table).details == "0 rows (expected <= 0)"
        assert RowCount(equals="source").run(table, {"source_rows": 0}).passed

    def test_one_row_wording(self):
        assert RowCount(min=1).run(rides().slice(0, 1)).details == "1 row (expected >= 1)"

    @pytest.mark.parametrize("kwargs, error", [
        ({}, ValueError),
        ({"min": -1}, ValueError),
        ({"max": -1}, ValueError),
        ({"min": 3, "max": 2}, ValueError),
        ({"min": True}, TypeError),
        ({"min": 1.0}, TypeError),
        ({"equals": "target"}, ValueError),
        ({"equals": 2.5}, TypeError),
    ])
    def test_bad_params(self, kwargs, error):
        with pytest.raises(error):
            RowCount(**kwargs)


# --------------------------------------------------------------------------- not_null


class TestNotNull:
    def test_pass(self):
        result = NotNull(["ride_id", "pickup_ts"]).run(rides())
        assert result.passed
        assert result.name == "not_null(ride_id, pickup_ts)"
        assert result.metrics == {"row_count": 5, "null_counts": {"ride_id": 0, "pickup_ts": 0}}

    def test_single_column_string(self):
        check = NotNull("ride_id")
        assert check.columns == ["ride_id"]
        assert check.run(rides()).passed

    def test_fail_counts_each_row_once_across_differently_chunked_columns(self):
        table = pa.table({
            "a": pa.chunked_array([[1, None], [3, None, 5]]),
            "b": pa.chunked_array([["x", None, "z"], ["w", None]]),
        })
        result = NotNull(["a", "b"]).run(table)
        assert not result.passed
        # Row 1 is null in both columns, row 3 in a only and row 4 in b only: three rows fail.
        assert result.failing_rows == 3
        assert result.metrics["null_counts"] == {"a": 2, "b": 2}
        assert result.details == "3 of 5 rows have a null in a, b (a=2, b=2)"

    def test_nan_is_not_null(self):
        assert NotNull("x").run(pa.table({"x": [1.0, float("nan")]})).passed

    def test_null_typed_column(self):
        result = NotNull("x").run(pa.table({"x": pa.nulls(3)}))
        assert not result.passed
        assert result.failing_rows == 3

    def test_missing_column_is_a_failed_result(self):
        result = NotNull(["ride_id", "nope"]).run(rides())
        assert not result.passed
        assert result.failing_rows == 0
        assert result.metrics["missing_columns"] == ["nope"]
        assert "missing column(s) ['nope']" in result.details

    def test_empty_table(self):
        assert NotNull("ride_id").run(empty(rides())).passed

    def test_duplicate_column_names_in_params_are_merged(self):
        assert NotNull(["a", "a"]).columns == ["a"]

    def test_ambiguous_column_in_table(self):
        table = pa.Table.from_arrays([pa.array([1]), pa.array([2])], names=["a", "a"])
        result = NotNull("a").run(table)
        assert not result.passed
        assert result.metrics["ambiguous_columns"] == ["a"]

    @pytest.mark.parametrize("columns, error", [([], ValueError), ("", TypeError), ([1], TypeError),
                                                (None, TypeError), ({"a": 1}, TypeError)])
    def test_bad_params(self, columns, error):
        with pytest.raises(error):
            NotNull(columns)


# --------------------------------------------------------------------------- unique


class TestUnique:
    def test_pass(self):
        result = Unique("ride_id").run(rides())
        assert result.passed
        assert result.name == "unique(ride_id)"
        assert result.metrics["distinct_keys"] == 5
        assert result.metrics["duplicate_keys"] == 0

    def test_fail_counts_every_copy_and_samples_in_first_seen_order(self):
        table = pa.table({"id": pa.chunked_array([[9, 7, 9], [1, 7, 7]])})
        result = Unique("id").run(table)
        assert not result.passed
        assert result.failing_rows == 5
        assert result.metrics["duplicate_keys"] == 2
        assert result.metrics["duplicate_rows"] == 5
        assert result.metrics["distinct_keys"] == 3
        assert result.metrics["sample"] == [{"key": 9, "count": 2}, {"key": 7, "count": 3}]
        assert result.details == "2 duplicated key(s) in (id) across 5 rows; e.g. 9 x2, 7 x3"

    def test_composite_key_pass_even_when_each_column_repeats(self):
        table = pa.table({"a": [1, 1, 2], "b": ["x", "y", "x"]})
        assert Unique(["a", "b"]).run(table).passed
        assert not Unique("a").run(table).passed

    def test_composite_key_fail_sample_is_dicts(self):
        table = pa.table({"a": [1, 1, 2, 1], "b": ["x", "y", "x", "x"]})
        result = Unique(["a", "b"]).run(table)
        assert not result.passed
        assert result.name == "unique(a, b)"
        assert result.failing_rows == 2
        assert result.metrics["sample"] == [{"key": {"a": 1, "b": "x"}, "count": 2}]

    def test_nulls_are_left_out_of_the_test(self):
        # SQL UNIQUE semantics: (1, null) twice and null twice are not duplicates.
        table = pa.table({"a": [1, 1, None, None, 2], "b": [None, None, "x", "x", "y"]})
        result = Unique(["a", "b"]).run(table)
        assert result.passed
        assert result.metrics["null_key_rows"] == 4
        assert result.details == "a, b is unique over 1 row; 4 rows with a null key skipped"
        single = Unique("a").run(pa.table({"a": [None, None, 3]}))
        assert single.passed
        assert single.metrics["null_key_rows"] == 2

    def test_nulls_do_not_hide_real_duplicates(self):
        result = Unique("a").run(pa.table({"a": pa.chunked_array([[None, 4], [4, None]])}))
        assert not result.passed
        assert result.failing_rows == 2
        assert result.metrics["null_key_rows"] == 2

    def test_all_null_key(self):
        result = Unique("a").run(pa.table({"a": pa.array([None, None], pa.int64())}))
        assert result.passed
        assert result.metrics["distinct_keys"] == 0

    def test_empty_table(self):
        assert Unique(["ride_id", "city"]).run(empty(rides())).passed

    def test_key_column_named_like_the_count_output(self):
        result = Unique("count_all").run(pa.table({"count_all": [1, 1, 2]}))
        assert not result.passed
        assert result.failing_rows == 2

    def test_nanosecond_timestamp_keys_without_pandas(self):
        value = 1_704_070_923_456_789_123
        table = pa.table({"ts": pa.array([value, value], pa.timestamp("ns"))})
        result = Unique("ts").run(table)
        assert not result.passed
        assert result.metrics["sample"][0]["count"] == 2
        json.dumps(result.to_dict())

    def test_matches_a_python_count_on_a_many_chunk_table(self):
        ids = [(i * 7919) % 1500 for i in range(3000)]
        table = pa.Table.from_batches([pa.record_batch({"id": ids[i:i + 250]}) for i in range(0, 3000, 250)])
        counts = {}
        for value in ids:
            counts[value] = counts.get(value, 0) + 1
        result = Unique("id").run(table)
        assert result.failing_rows == sum(c for c in counts.values() if c > 1)
        assert result.metrics["duplicate_keys"] == sum(1 for c in counts.values() if c > 1)

    def test_unsupported_key_type_is_a_failed_result(self):
        result = Unique("tags").run(pa.table({"tags": [[1], [1]]}))
        assert not result.passed
        assert result.details.startswith("could not be evaluated")
        assert "error" in result.metrics

    def test_missing_column(self):
        result = Unique(["ride_id", "nope"]).run(rides())
        assert not result.passed
        assert result.metrics["missing_columns"] == ["nope"]


# --------------------------------------------------------------------------- accepted_values


class TestAcceptedValues:
    def test_pass(self):
        result = AcceptedValues("city", ["NYC", "BKK"]).run(rides())
        assert result.passed
        assert result.name == "accepted_values(city)"
        assert result.metrics["invalid_count"] == 0

    def test_fail_samples_distinct_offenders(self):
        table = pa.table({"city": pa.chunked_array([["NYC", "LAX"], ["SF", "LAX", "BKK"]])})
        result = AcceptedValues("city", ["NYC", "BKK"]).run(table)
        assert not result.passed
        assert result.failing_rows == 3
        assert result.metrics["invalid_values"] == ["LAX", "SF"]
        assert result.details == "3 of 5 rows have a city outside ['NYC', 'BKK']; e.g. ['LAX', 'SF']"

    def test_nulls_are_skipped(self):
        result = AcceptedValues("city", ["NYC"]).run(pa.table({"city": ["NYC", None, None]}))
        assert result.passed
        assert result.metrics["null_count"] == 2

    def test_values_are_cast_to_the_column_type(self):
        table = pa.table({"code": pa.array([1, 2, 3], pa.int32())})
        result = AcceptedValues("code", [1, 2]).run(table)
        assert result.failing_rows == 1
        assert result.metrics["invalid_values"] == [3]
        assert AcceptedValues("code", ["1", "2", "3"]).run(table).passed

    def test_dictionary_encoded_column(self):
        column = pa.array(["NYC", "BKK", "LAX", None]).dictionary_encode()
        result = AcceptedValues("city", ["NYC", "BKK"]).run(pa.table({"city": column}))
        assert result.failing_rows == 1
        assert result.metrics["invalid_values"] == ["LAX"]

    def test_set_of_values_is_sorted(self):
        assert AcceptedValues("city", {"NYC", "BKK"}).values == ["BKK", "NYC"]

    def test_empty_table(self):
        assert AcceptedValues("city", ["NYC"]).run(empty(rides())).passed

    def test_missing_column(self):
        result = AcceptedValues("nope", ["NYC"]).run(rides())
        assert not result.passed
        assert result.metrics["missing_columns"] == ["nope"]

    def test_unsupported_column_type_is_a_failed_result(self):
        result = AcceptedValues("tags", ["a"]).run(pa.table({"tags": [[1], [2]]}))
        assert not result.passed
        assert result.details.startswith("could not be evaluated")

    @pytest.mark.parametrize("values, error", [("NYC", TypeError), ([], ValueError), ([1, "a"], ValueError),
                                               (None, TypeError), ({"NYC": 1}, TypeError)])
    def test_bad_params(self, values, error):
        with pytest.raises(error):
            AcceptedValues("city", values)


# --------------------------------------------------------------------------- range


class TestRange:
    def test_min_only(self):
        assert Range("fare", min=0).run(rides()).passed
        result = Range("fare", min=5).run(rides())
        assert not result.passed
        assert result.failing_rows == 1
        assert result.metrics["below_min"] == 1
        assert result.details == "1 of 5 rows violate fare >= 5: 1 below min, 0 above max"

    def test_max_only(self):
        result = Range("fare", max=50).run(rides())
        assert result.failing_rows == 1
        assert result.metrics["above_max"] == 1
        assert result.metrics["observed_max"] == 99.0
        assert result.metrics["observed_min"] == 0.0

    def test_bounds_are_inclusive(self):
        assert Range("fare", min=0, max=99).run(rides()).passed

    def test_both_bounds_and_nan(self):
        table = pa.table({"x": pa.chunked_array([[-1.0, 5.0], [float("nan"), 200.0, None]])})
        result = Range("x", min=0, max=100).run(table)
        assert not result.passed
        assert result.failing_rows == 3
        assert result.metrics["below_min"] == 1
        assert result.metrics["above_max"] == 1
        assert result.metrics["nan_count"] == 1
        assert result.metrics["null_count"] == 1
        assert result.details == "3 of 5 rows violate 0 <= x <= 100: 1 below min, 1 above max, 1 NaN"

    def test_nulls_are_skipped(self):
        result = Range("x", min=0).run(pa.table({"x": [1, None, 3]}))
        assert result.passed
        assert result.details == "every non-null value satisfies x >= 0 (1 nulls skipped)"

    def test_float_bound_on_int_column_is_not_truncated(self):
        result = Range("x", min=0.5).run(pa.table({"x": pa.array([0, 1], pa.int32())}))
        assert result.failing_rows == 1

    def test_decimal_column(self):
        table = pa.table({"d": pa.array([decimal.Decimal("1.50"), decimal.Decimal("-0.01")], pa.decimal128(10, 2))})
        result = Range("d", min=0).run(table)
        assert result.failing_rows == 1
        assert result.metrics["observed_min"] == "-0.01"
        assert Range("d", min=decimal.Decimal("-0.01")).run(table).passed

    def test_timestamp_column_with_iso_bounds(self):
        table = rides()
        assert Range("pickup_ts", min="2026-09-28", max="2026-09-30T11:00:00").run(table).passed
        result = Range("pickup_ts", min="2026-09-29T00:00:00").run(table)
        assert result.failing_rows == 1

    def test_tz_aware_column_with_naive_and_aware_bounds(self):
        table = pa.table({"ts": pa.array([dt.datetime(2026, 9, 30, 5, tzinfo=UTC)],
                                         pa.timestamp("ms", tz="Asia/Bangkok"))})
        assert Range("ts", min="2026-09-30T04:00:00Z", max=dt.datetime(2026, 9, 30, 6)).run(table).passed
        # 12:00 in Bangkok is 05:00 UTC, so the value sits exactly on the bound.
        bangkok = dt.timezone(dt.timedelta(hours=7))
        assert Range("ts", max=dt.datetime(2026, 9, 30, 12, tzinfo=bangkok)).run(table).passed
        assert not Range("ts", max="2026-09-30T04:59:59").run(table).passed

    def test_nanosecond_column_with_sub_microsecond_values(self):
        table = pa.table({"ts": pa.array([1_704_070_923_456_789_123], pa.timestamp("ns"))})
        result = Range("ts", max="2024-01-01T01:02:03.456789").run(table)
        assert result.failing_rows == 1
        json.dumps(result.to_dict())

    def test_date_column(self):
        table = pa.table({"d": pa.array([dt.date(2026, 9, 1), dt.date(2026, 9, 29)], pa.date32())})
        assert Range("d", min=dt.date(2026, 9, 1)).run(table).passed
        assert Range("d", min="2026-09-02").run(table).failing_rows == 1

    def test_numeric_bound_on_string_column_fails_instead_of_comparing_text(self):
        result = Range("s", min=0).run(pa.table({"s": ["10", "9"]}))
        assert not result.passed
        assert "numeric bound needs a numeric column" in result.details

    def test_unparseable_bound_is_a_failed_result(self):
        result = Range("pickup_ts", min="yesterday").run(rides())
        assert not result.passed
        assert result.details.startswith("cannot compare pickup_ts")

    def test_string_column_with_string_bounds(self):
        assert Range("city", min="A", max="Z").run(rides()).passed

    def test_empty_table(self):
        result = Range("fare", min=0).run(empty(rides()))
        assert result.passed
        assert result.metrics["observed_min"] is None

    def test_missing_column(self):
        result = Range("nope", min=0).run(rides())
        assert not result.passed
        assert result.metrics["missing_columns"] == ["nope"]

    @pytest.mark.parametrize("kwargs, error", [
        ({}, ValueError),
        ({"min": float("nan")}, ValueError),
        ({"min": True}, TypeError),
        ({"min": 5, "max": 1}, ValueError),
    ])
    def test_bad_params(self, kwargs, error):
        with pytest.raises(error):
            Range("fare", **kwargs)


# --------------------------------------------------------------------------- freshness


class TestFreshness:
    def test_pass_and_fail(self):
        result = Freshness("pickup_ts", 2, now=NOW).run(rides())
        assert result.passed
        assert result.name == "freshness(pickup_ts)"
        assert result.metrics["latest"] == "2026-09-30T11:00:00+00:00"
        assert result.metrics["age_hours"] == 1.0
        failed = Freshness("pickup_ts", 0.5, now=NOW).run(rides())
        assert not failed.passed
        assert failed.failing_rows == 0
        assert failed.details == ("newest pickup_ts (2026-09-30T11:00:00+00:00) is 1.0h old, "
                                  "older than the 0.5h limit")

    def test_naive_column_is_read_as_utc(self):
        # The column is naive; "now" is 19:00 in Bangkok, which is 12:00 UTC.
        bangkok = dt.timezone(dt.timedelta(hours=7))
        result = Freshness("pickup_ts", 1, now=dt.datetime(2026, 9, 30, 19, tzinfo=bangkok)).run(rides())
        assert result.metrics["age_hours"] == 1.0
        assert result.passed

    def test_tz_aware_column_with_aware_and_naive_now(self):
        table = pa.table({"ts": pa.array([dt.datetime(2026, 9, 30, 5, tzinfo=UTC)],
                                         pa.timestamp("ms", tz="Asia/Bangkok"))})
        aware = Freshness("ts", 7, now=dt.datetime(2026, 9, 30, 19, tzinfo=dt.timezone(dt.timedelta(hours=7))))
        assert aware.run(table).metrics["age_hours"] == 7.0
        naive = Freshness("ts", 7, now=dt.datetime(2026, 9, 30, 12))
        assert naive.run(table).metrics["age_hours"] == 7.0
        assert naive.run(table).passed
        assert not Freshness("ts", 6.9, now=dt.datetime(2026, 9, 30, 12)).run(table).passed

    @pytest.mark.parametrize("unit, raw", [
        ("s", 1_790_763_600),
        ("ms", 1_790_763_600_000),
        ("us", 1_790_763_600_000_000),
        ("ns", 1_790_763_600_000_000_123),
    ])
    def test_every_timestamp_unit(self, unit, raw):
        # raw is 2026-09-30T10:20:00Z in the given unit; the ns value has sub-microsecond digits.
        table = pa.table({"ts": pa.array([raw], pa.timestamp(unit))})
        result = Freshness("ts", 2, now=NOW).run(table)
        assert result.passed
        assert result.metrics["latest"] == "2026-09-30T10:20:00+00:00"
        assert result.metrics["age_hours"] == pytest.approx(100 / 60, abs=1e-4)
        json.dumps(result.to_dict())

    def test_date_columns(self):
        for typ in (pa.date32(), pa.date64()):
            table = pa.table({"d": pa.array([dt.date(2026, 9, 1), dt.date(2026, 9, 29)], typ)})
            result = Freshness("d", 48, now=NOW).run(table)
            assert result.passed, typ
            assert result.metrics["age_hours"] == 36.0
            assert not Freshness("d", 35, now=NOW).run(table).passed

    def test_future_values_pass(self):
        table = pa.table({"ts": pa.array([dt.datetime(2026, 10, 1)], pa.timestamp("us"))})
        result = Freshness("ts", 1, now=NOW).run(table)
        assert result.passed
        assert result.metrics["age_hours"] == -12.0
        assert "12.0h in the future" in result.details

    def test_now_as_iso_string(self):
        check = Freshness("pickup_ts", 2, now="2026-09-30T12:00:00Z")
        assert check.now == NOW
        assert check.run(rides()).passed

    def test_now_defaults_to_the_current_time_at_run(self):
        recent = dt.datetime.now(UTC) - dt.timedelta(minutes=30)
        table = pa.table({"ts": pa.array([recent], pa.timestamp("us", tz="UTC"))})
        check = Freshness("ts", 1)
        assert check.now is None
        assert check.run(table).passed
        stale = pa.table({"ts": pa.array([recent - dt.timedelta(hours=2)], pa.timestamp("us", tz="UTC"))})
        assert not check.run(stale).passed

    def test_nulls_use_the_latest_non_null_value(self):
        table = pa.table({"ts": pa.array([None, dt.datetime(2026, 9, 30, 11), None], pa.timestamp("us"))})
        result = Freshness("ts", 2, now=NOW).run(table)
        assert result.passed
        assert result.metrics["null_count"] == 2

    def test_empty_and_all_null_fail(self):
        assert not Freshness("pickup_ts", 48, now=NOW).run(empty(rides())).passed
        result = Freshness("ts", 48, now=NOW).run(pa.table({"ts": pa.array([None], pa.timestamp("us"))}))
        assert not result.passed
        assert "no non-null values" in result.details

    def test_non_temporal_column_fails(self):
        result = Freshness("city", 48, now=NOW).run(rides())
        assert not result.passed
        assert result.details == "freshness needs a timestamp or date column; city is string"

    def test_missing_column(self):
        result = Freshness("nope", 48, now=NOW).run(rides())
        assert not result.passed
        assert result.metrics["missing_columns"] == ["nope"]

    @pytest.mark.parametrize("kwargs, error", [
        ({"max_age_hours": 0}, ValueError),
        ({"max_age_hours": -1}, ValueError),
        ({"max_age_hours": float("inf")}, ValueError),
        ({"max_age_hours": True}, TypeError),
        ({"max_age_hours": "48"}, TypeError),
        ({"max_age_hours": 48, "now": "not a time"}, ValueError),
        ({"max_age_hours": 48, "now": dt.date(2026, 9, 30)}, TypeError),
        ({"max_age_hours": 48, "now": 1_790_763_600}, TypeError),
    ])
    def test_bad_params(self, kwargs, error):
        with pytest.raises(error):
            Freshness("pickup_ts", **kwargs)


# --------------------------------------------------------------------------- schema


class TestSchemaHas:
    def test_list_of_names(self):
        result = SchemaHas(["ride_id", "fare"]).run(rides())
        assert result.passed
        assert result.name == "schema"
        assert result.details == "has the expected column(s) ['ride_id', 'fare']"
        failed = SchemaHas(["ride_id", "tip", "toll"]).run(rides())
        assert not failed.passed
        assert failed.metrics["missing_columns"] == ["tip", "toll"]
        assert failed.details == "missing column(s) ['tip', 'toll']"

    def test_dict_of_types(self):
        check = SchemaHas({"ride_id": "int64", "fare": "double", "city": "string",
                           "pickup_ts": "timestamp[us]"})
        result = check.run(rides())
        assert result.passed
        assert result.details.endswith("with the expected types")

    def test_type_mismatch_and_missing(self):
        result = SchemaHas({"ride_id": "int32", "fare": "float64", "tip": "double"}).run(rides())
        assert not result.passed
        assert result.metrics["type_mismatches"] == {"ride_id": {"expected": "int32", "actual": "int64"}}
        assert result.metrics["missing_columns"] == ["tip"]
        assert result.details == "missing column(s) ['tip']; ride_id is int64, expected int32"

    def test_types_match_exactly(self):
        table = pa.table({"s": pa.array(["a"], pa.large_string()),
                          "ts": pa.array([0], pa.timestamp("ns", tz="UTC"))})
        assert not SchemaHas({"s": "string"}).run(table).passed
        assert SchemaHas({"s": "large_string"}).run(table).passed
        assert not SchemaHas({"ts": "timestamp[ns]"}).run(table).passed
        assert SchemaHas({"ts": "timestamp[ns, tz=UTC]"}).run(table).passed

    def test_pyarrow_types_and_decimal_strings(self):
        table = pa.table({"d": pa.array([decimal.Decimal("1.5")], pa.decimal128(10, 2)), "n": [1]})
        assert SchemaHas({"d": "decimal128(10, 2)", "n": pa.int64()}).run(table).passed
        assert SchemaHas({"d": "decimal(10,2)"}).run(table).passed

    def test_empty_table_is_checked_on_its_schema(self):
        assert SchemaHas({"ride_id": "int64"}).run(empty(rides())).passed

    def test_ambiguous_column(self):
        table = pa.Table.from_arrays([pa.array([1]), pa.array(["x"])], names=["a", "a"])
        result = SchemaHas({"a": "int64"}).run(table)
        assert not result.passed
        assert result.metrics["ambiguous_columns"] == ["a"]

    @pytest.mark.parametrize("columns, error", [({"a": "int46"}, ValueError), ({"a": 5}, TypeError),
                                                ({}, ValueError), ([], ValueError), ({"": "int64"}, TypeError)])
    def test_bad_params(self, columns, error):
        with pytest.raises(error):
            SchemaHas(columns)


@pytest.mark.parametrize("spec, expected", [
    ("int64", pa.int64()),
    ("INT64", pa.int64()),
    ("double", pa.float64()),
    ("float64", pa.float64()),
    ("string", pa.string()),
    ("str", pa.string()),
    ("bool", pa.bool_()),
    ("date32", pa.date32()),
    ("timestamp[ms]", pa.timestamp("ms")),
    ("timestamp[us, tz=UTC]", pa.timestamp("us", tz="UTC")),
    ("timestamp[ns,tz=Asia/Bangkok]", pa.timestamp("ns", tz="Asia/Bangkok")),
    ("decimal256(40, 3)", pa.decimal256(40, 3)),
    (pa.int8(), pa.int8()),
])
def test_parse_type(spec, expected):
    assert parse_type(spec).equals(expected)


# --------------------------------------------------------------------------- config mapping


DESIGN_DOC_CHECKS = [
    {"check": "row_count", "min": 1},
    {"check": "not_null", "columns": ["ride_id", "pickup_ts"]},
    {"check": "unique", "columns": ["ride_id"]},
    {"check": "accepted_values", "column": "city", "values": ["NYC", "BKK"]},
    {"check": "range", "column": "fare", "min": 0},
    {"check": "freshness", "column": "pickup_ts", "max_age_hours": 48},
    {"check": "schema", "columns": {"ride_id": "int64", "fare": "double"}},
]


class TestChecksFromConfig:
    def test_maps_every_config_name(self):
        checks = checks_from_config(DESIGN_DOC_CHECKS)
        assert checks == [
            RowCount(min=1),
            NotNull(["ride_id", "pickup_ts"]),
            Unique(["ride_id"]),
            AcceptedValues("city", ["NYC", "BKK"]),
            Range("fare", min=0),
            Freshness("pickup_ts", 48),
            SchemaHas({"ride_id": "int64", "fare": "double"}),
        ]
        assert set(CHECKS) == {"row_count", "not_null", "unique", "accepted_values", "range", "freshness", "schema",
                               # Platform contract C7: the temporal checks in quality/temporal.py
                               "monotonic", "max_skew", "rate_below", "max_gap"}

    def test_design_doc_example_runs_green_on_clean_data(self):
        checks = checks_from_config(DESIGN_DOC_CHECKS)
        checks[5] = Freshness("pickup_ts", 48, now=NOW)
        assert run_checks(rides(), checks).passed

    def test_config_quality_block(self):
        config = Config.from_dict({
            "identifier": "nyc_taxi",
            "metadata": {
                "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
                "target": {"name": "rides", "format": "ICEBERG"},
                "quality": {"on_failure": "warn", "checks": DESIGN_DOC_CHECKS},
            },
        })
        assert len(checks_from_config(config.quality["checks"])) == 7

    def test_name_override_and_case_insensitive_check(self):
        (check,) = checks_from_config([{"check": " Not_Null ", "columns": "a", "name": "ids present"}])
        assert isinstance(check, NotNull)
        assert check.run(pa.table({"a": [1]})).name == "ids present"

    def test_none_and_empty(self):
        assert checks_from_config(None) == []
        assert checks_from_config([]) == []

    def test_check_instances_pass_through(self):
        check = RowCount(min=1)
        assert checks_from_config([check, {"check": "row_count", "max": 5}])[0] is check

    @pytest.mark.parametrize("items, fragment", [
        ([{"check": "no_such_check"}], "unknown check 'no_such_check'"),
        ([{"check": 5}], "unknown check 5"),
        ([{"columns": ["a"]}], "missing 'check'"),
        (["not_null"], "must be an object"),
        ([{"check": "not_null", "column": "a"}], "unknown parameter(s) ['column']"),
        ([{"check": "range", "min": 0}], "missing required parameter(s) ['column']"),
        ([{"check": "freshness", "column": "ts"}], "missing required parameter(s) ['max_age_hours']"),
        ([{"check": "row_count"}], "at least one of min, max or equals"),
        ([{"check": "row_count", "min": -1}], "non-negative"),
        ([{"check": "schema", "columns": {"a": "int46"}}], "unknown pyarrow type 'int46'"),
        ([{"check": "accepted_values", "column": "c", "values": "NYC"}], "must be a list"),
        ({"check": "row_count", "min": 1}, "must be a list"),
        ("row_count", "must be a list"),
        (5, "must be a list"),
    ])
    def test_bad_config_raises_config_error(self, items, fragment):
        with pytest.raises(ConfigError) as info:
            checks_from_config(items)
        assert fragment in str(info.value)

    def test_error_names_the_item_and_chains_the_cause(self):
        with pytest.raises(ConfigError) as info:
            checks_from_config([{"check": "row_count", "min": 1}, {"check": "range", "column": "x"}])
        assert str(info.value).startswith("quality check #2 (range)")
        assert isinstance(info.value.__cause__, ValueError)


# --------------------------------------------------------------------------- run_checks


class TestRunChecks:
    def test_mixed_checks_and_dicts_keep_order(self):
        report = run_checks(rides(), [RowCount(min=1), {"check": "unique", "columns": "ride_id"}])
        assert [result.name for result in report] == ["row_count", "unique(ride_id)"]
        assert report.passed

    def test_context_reaches_checks(self):
        assert run_checks(rides(), [RowCount(equals="source")], context={"source_rows": 5}).passed
        assert not run_checks(rides(), [RowCount(equals="source")]).passed

    def test_single_check_and_no_checks(self):
        assert len(run_checks(rides(), RowCount(min=1))) == 1
        assert run_checks(rides(), None).passed
        assert run_checks(rides(), []).summary() == "Data quality: no checks run"

    def test_accepts_record_batches_and_dicts(self):
        batch = rides().to_batches()[0]
        assert run_checks(batch, [RowCount(equals=3)]).passed
        assert run_checks({"a": [1, 2]}, [Unique("a")]).passed

    @pytest.mark.parametrize("df", [None, 42, "not a table"])
    def test_bad_input_raises_type_error(self, df):
        with pytest.raises(TypeError):
            run_checks(df, [RowCount(min=1)])

    def test_bad_batch_is_reported_not_raised(self):
        bad = pa.table({
            "ride_id": [1, 1, None],
            "city": ["NYC", "LAX", "BKK"],
            "fare": [-5.0, 10.0, float("nan")],
            "pickup_ts": pa.array([dt.datetime(2026, 9, 1)] * 3, pa.timestamp("us")),
        })
        checks = checks_from_config(DESIGN_DOC_CHECKS)
        checks[5] = Freshness("pickup_ts", 48, now=NOW)
        report = run_checks(bad, checks)
        assert not report.passed
        assert [r.name for r in report.failures] == [
            "not_null(ride_id, pickup_ts)", "unique(ride_id)", "accepted_values(city)", "range(fare)",
            "freshness(pickup_ts)",
        ]
        assert report.results[4].failing_rows == 2
        with pytest.raises(DataQualityError) as info:
            report.raise_for_failures()
        assert info.value.report.to_dict()["checks_failed"] == 5
        summary = report.summary()
        assert summary.splitlines()[0] == "Data quality: 2 of 7 checks passed, 5 failed"
        assert "[FAIL] range(fare): 2 of 3 rows violate fare >= 0: 1 below min, 0 above max, 1 NaN" in summary
        json.dumps(report.to_dict(), allow_nan=False)

    def test_checks_do_not_modify_the_table(self):
        table = rides()
        before = table.to_pylist()
        run_checks(table, checks_from_config(DESIGN_DOC_CHECKS))
        assert table.to_pylist() == before


def test_check_is_abstract():
    with pytest.raises(TypeError):
        Check()


def test_blank_name_is_rejected():
    with pytest.raises(ValueError):
        RowCount(min=1, name="  ")


def test_results_are_deterministic():
    table = pa.table({"id": [3, 1, 3, 2, 1, 3], "v": [1.0, math.nan, 2.0, -1.0, 5.0, 9.0]})
    checks = [Unique("id"), Range("v", min=0), AcceptedValues("id", [1, 2])]
    first = run_checks(table, checks).to_dict()
    for _ in range(5):
        assert run_checks(table, checks).to_dict() == first
