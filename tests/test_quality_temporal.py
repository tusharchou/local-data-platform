"""Tests for the temporal quality checks: Monotonic, MaxSkew, RateBelow and MaxGap."""

import datetime as dt
import json

import pyarrow as pa
import pytest

from local_data_platform.exceptions import ConfigError, DataQualityError
from local_data_platform.quality import (
    CHECKS,
    MaxGap,
    MaxSkew,
    Monotonic,
    RateBelow,
    checks_from_config,
    run_checks,
)
from local_data_platform.quality.temporal import TEMPORAL_CHECKS

T0 = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.timezone.utc)


def stamps(offsets_ms, unit="us", tz="UTC"):
    """Timestamps ``T0 + offset`` for each offset in milliseconds (``None`` stays null)."""
    values = [None if ms is None else T0 + dt.timedelta(milliseconds=ms) for ms in offsets_ms]
    return pa.array(values, pa.timestamp("us", tz="UTC")).cast(pa.timestamp(unit, tz=tz))


def frames(offsets_ms, episodes=None, frame_idx=None):
    count = len(offsets_ms)
    return pa.table({
        "episode_id": episodes or ["e1"] * count,
        "frame_idx": frame_idx or list(range(count)),
        "ts": stamps(offsets_ms),
    })


# --------------------------------------------------------------------------- registry


def test_temporal_checks_are_registered_by_config_name():
    assert {cls.check_type for cls in TEMPORAL_CHECKS} == {"monotonic", "max_skew", "rate_below", "max_gap"}
    for cls in TEMPORAL_CHECKS:
        assert CHECKS[cls.check_type] is cls


def test_checks_from_config_builds_every_temporal_check():
    checks = checks_from_config([
        {"check": "monotonic", "column": "ts", "group_by": "episode_id", "strict": True, "order_by": "frame_idx"},
        {"check": "max_skew", "column_a": "rgb_ts", "column_b": "depth_ts", "max_ms": 20},
        {"check": "rate_below", "predicate_column": "dropped", "max_rate": 0.02},
        {"check": "max_gap", "column": "ts", "max_ms": 100, "group_by": ["episode_id"], "name": "no holes"},
    ])
    assert checks == [
        Monotonic("ts", group_by=["episode_id"], strict=True, order_by="frame_idx"),
        MaxSkew("rgb_ts", "depth_ts", 20),
        RateBelow("dropped", 0.02),
        MaxGap("ts", 100, ["episode_id"], name="no holes"),
    ]


@pytest.mark.parametrize("item, fragment", [
    ({"check": "max_gap", "column": "ts"}, "missing required parameter(s) ['max_ms']"),
    ({"check": "max_skew", "column_a": "a", "max_ms": 1}, "missing required parameter(s) ['column_b']"),
    ({"check": "rate_below", "predicate_column": "d", "max_rate": 1.5}, "between 0 and 1"),
    ({"check": "rate_below", "predicate_column": "d", "max_rate": True}, "must be a number"),
    ({"check": "max_gap", "column": "ts", "max_ms": 0}, "positive number"),
    ({"check": "max_skew", "column_a": "a", "column_b": "b", "max_ms": -1}, "non-negative"),
    ({"check": "max_skew", "column_a": "a", "column_b": "b", "max_ms": 1, "unit": "min"}, "unit must be one of"),
    ({"check": "monotonic", "column": "ts", "strict": "yes"}, "strict must be true or false"),
    ({"check": "monotonic", "column": "ts", "group_by": []}, "at least one column"),
    ({"check": "monotonic", "column": "ts", "direction": "up"}, "unknown parameter(s) ['direction']"),
])
def test_bad_parameters_are_config_errors(item, fragment):
    with pytest.raises(ConfigError) as error:
        checks_from_config([item])
    assert fragment in str(error.value)


def test_max_ms_accepts_float_and_nan_is_rejected():
    assert MaxSkew("a", "b", 0.5).max_ms == 0.5
    with pytest.raises(ValueError):
        MaxGap("ts", float("nan"))


# --------------------------------------------------------------------------- monotonic


class TestMonotonic:
    def test_increasing_passes(self):
        result = Monotonic("ts").run(frames([0, 33, 66, 66, 100]))
        assert result.passed, result.details
        assert result.name == "monotonic(ts)"
        assert result.metrics["checked_rows"] == 5

    def test_strict_rejects_repeated_values(self):
        result = Monotonic("ts", strict=True).run(frames([0, 33, 66, 66, 100]))
        assert not result.passed
        assert result.failing_rows == 1
        assert result.metrics["sample"][0]["row"] == 3
        assert "is not greater than" in result.details

    def test_clock_jump_backwards_is_found_with_the_row(self):
        result = Monotonic("ts").run(frames([0, 33, 66, 20, 53, 86]))
        assert not result.passed
        assert result.failing_rows == 1
        sample = result.metrics["sample"][0]
        assert sample["row"] == 3
        assert sample["value"] < sample["previous"]
        assert "row 3" in result.details

    def test_groups_are_independent(self):
        # Two episodes back to back: e2 starts before e1 ends, which is fine per episode.
        table = frames([0, 33, 66, 10, 43, 76], episodes=["e1"] * 3 + ["e2"] * 3)
        assert not Monotonic("ts").run(table).passed
        result = Monotonic("ts", group_by="episode_id").run(table)
        assert result.passed, result.details
        assert result.metrics["groups"] == 2
        assert result.name == "monotonic(ts by episode_id)"

    def test_interleaved_groups_keep_table_order_within_a_group(self):
        table = frames([0, 5, 33, 38, 20, 71], episodes=["e1", "e2", "e1", "e2", "e1", "e2"])
        result = Monotonic("ts", group_by="episode_id").run(table)
        assert not result.passed
        assert result.metrics["sample"] == [{
            "row": 4, "value": (T0 + dt.timedelta(milliseconds=20)).isoformat(),
            "previous": (T0 + dt.timedelta(milliseconds=33)).isoformat(), "group": "e1",
        }]

    def test_order_by_sorts_shuffled_rows_first(self):
        shuffled = frames([66, 0, 100, 33], frame_idx=[2, 0, 3, 1])
        assert not Monotonic("ts").run(shuffled).passed
        assert Monotonic("ts", order_by="frame_idx", strict=True).run(shuffled).passed
        bad = frames([66, 0, 100, 70], frame_idx=[2, 0, 3, 1])
        result = Monotonic("ts", order_by="frame_idx", strict=True).run(bad)
        assert not result.passed and result.metrics["sample"][0]["row"] == 0

    def test_composite_group_key(self):
        table = pa.table({"robot": ["a", "a", "b", "b"], "cam": [1, 1, 1, 1], "seq": [1, 2, 1, 0]})
        result = Monotonic("seq", group_by=["robot", "cam"]).run(table)
        assert not result.passed
        assert result.metrics["sample"][0]["group"] == {"robot": "b", "cam": 1}

    def test_null_group_keys_form_one_group(self):
        table = pa.table({"g": [None, None, "x"], "v": [2, 1, 5]})
        result = Monotonic("v", group_by="g").run(table)
        assert not result.passed and result.failing_rows == 1

    def test_nulls_and_nan_are_skipped(self):
        table = pa.table({"v": pa.array([1.0, None, float("nan"), 2.0, 3.0])})
        result = Monotonic("v", strict=True).run(table)
        assert result.passed, result.details
        assert result.metrics["skipped_rows"] == 2

    def test_empty_and_single_row_pass(self):
        assert Monotonic("ts").run(frames([])).passed
        assert Monotonic("ts").run(frames([5])).passed

    def test_missing_column_fails_without_raising(self):
        result = Monotonic("ts", group_by="nope").run(frames([0, 1]))
        assert not result.passed
        assert result.metrics["missing_columns"] == ["nope"]

    def test_strings_and_integers_work(self):
        assert Monotonic("s").run(pa.table({"s": ["a", "b", "b", "c"]})).passed
        assert not Monotonic("n", strict=True).run(pa.table({"n": [3, 2]})).passed

    def test_vectorised_on_a_large_table(self):
        count = 200_000
        table = pa.table({"g": [i // 1000 for i in range(count)], "v": [i % 1000 for i in range(count)]})
        result = Monotonic("v", group_by="g", strict=True).run(table)
        assert result.passed
        assert result.metrics["groups"] == 200


# --------------------------------------------------------------------------- max_skew


class TestMaxSkew:
    def pair(self, rgb_ms, depth_ms, depth_unit="us"):
        return pa.table({"rgb_ts": stamps(rgb_ms), "depth_ts": stamps(depth_ms, unit=depth_unit)})

    def test_within_limit_passes(self):
        result = MaxSkew("rgb_ts", "depth_ts", 20).run(self.pair([0, 33, 66], [3, 30, 86]))
        assert result.passed, result.details
        assert result.metrics["max_skew_ms"] == 20.0
        assert result.metrics["mean_skew_ms"] == pytest.approx(26 / 3, abs=1e-3)

    def test_over_limit_fails_with_rows(self):
        result = MaxSkew("rgb_ts", "depth_ts", 20).run(self.pair([0, 33, 66, 99], [3, 60, 66, 140]))
        assert not result.passed
        assert result.failing_rows == 2
        assert [item["row"] for item in result.metrics["sample"]] == [1, 3]
        assert result.metrics["sample"][1]["skew_ms"] == 41.0
        assert result.name == "max_skew(rgb_ts, depth_ts)"

    def test_mixed_units_and_time_zones_compare_instants(self):
        rgb = stamps([0, 33])
        depth = stamps([0.5, 33.25], unit="ns", tz=None)
        result = MaxSkew("rgb_ts", "depth_ts", 0.4).run(pa.table({"rgb_ts": rgb, "depth_ts": depth}))
        assert not result.passed and result.failing_rows == 1
        assert result.metrics["max_skew_ms"] == 0.5

    def test_nulls_are_skipped(self):
        result = MaxSkew("rgb_ts", "depth_ts", 5).run(self.pair([0, None, 66], [1, 40, None]))
        assert result.passed
        assert result.metrics["checked_rows"] == 1 and result.metrics["skipped_rows"] == 2

    def test_numeric_columns_use_the_unit(self):
        table = pa.table({"a": [1_000_000_000, 2_000_000_000], "b": [1_015_000_000, 2_030_000_000]})
        assert MaxSkew("a", "b", 20, unit="ns").run(table).failing_rows == 1
        assert MaxSkew("a", "b", 20, unit="ms").run(table).failing_rows == 2  # read as ms, they're hours apart
        table = pa.table({"a": [0.0, 1.0], "b": [0.01, 1.03]})
        assert MaxSkew("a", "b", 20, unit="s").run(table).failing_rows == 1

    def test_timestamp_against_number_fails_cleanly(self):
        table = pa.table({"a": stamps([0]), "b": [5]})
        result = MaxSkew("a", "b", 20).run(table)
        assert not result.passed and "cannot compare" in result.details

    def test_non_time_column_fails_cleanly(self):
        result = MaxSkew("a", "b", 20).run(pa.table({"a": ["x"], "b": ["y"]}))
        assert not result.passed and "timestamp, date, duration or numeric" in result.details

    def test_dates_and_durations(self):
        dates = pa.table({"a": pa.array([dt.date(2026, 1, 1)], pa.date32()),
                          "b": pa.array([dt.date(2026, 1, 2)], pa.date32())})
        assert MaxSkew("a", "b", 86_400_000).run(dates).passed
        assert not MaxSkew("a", "b", 86_399_999).run(dates).passed
        durations = pa.table({"a": pa.array([1000], pa.duration("ms")), "b": pa.array([1_010_000], pa.duration("us"))})
        assert MaxSkew("a", "b", 10).run(durations).passed


# --------------------------------------------------------------------------- rate_below


class TestRateBelow:
    def test_rate_at_limit_passes(self):
        table = pa.table({"dropped": [True] + [False] * 49})
        result = RateBelow("dropped", 0.02).run(table)
        assert result.passed, result.details
        assert result.metrics["rate"] == 0.02

    def test_rate_over_limit_fails(self):
        table = pa.table({"dropped": [True, True] + [False] * 48})
        result = RateBelow("dropped", 0.02).run(table)
        assert not result.passed
        assert result.failing_rows == 2
        assert "4.00%" in result.details

    def test_nulls_are_left_out_of_the_rate(self):
        table = pa.table({"dropped": [True, None, None, False]})
        result = RateBelow("dropped", 0.5).run(table)
        assert result.passed
        assert result.metrics["rate"] == 0.5 and result.metrics["null_count"] == 2
        assert RateBelow("dropped", 0.0).run(pa.table({"dropped": pa.array([None], pa.bool_())})).passed

    def test_non_boolean_column_fails_cleanly(self):
        result = RateBelow("dropped", 0.1).run(pa.table({"dropped": [0, 1]}))
        assert not result.passed and "needs a boolean column" in result.details

    def test_grouped_rates_find_the_bad_group(self):
        table = pa.table({
            "episode_id": ["a"] * 10 + ["b"] * 10 + ["c"] * 10,
            "dropped": [False] * 10 + [True] * 3 + [False] * 7 + [True] + [False] * 9,
        })
        result = RateBelow("dropped", 0.1, group_by="episode_id").run(table)
        assert not result.passed
        assert result.failing_rows == 3
        assert result.metrics["groups"] == 3 and result.metrics["failing_groups"] == 1
        assert result.metrics["sample"] == [{"group": "b", "rate": 0.3, "true_count": 3, "count": 10}]
        assert result.name == "rate_below(dropped by episode_id)"
        assert RateBelow("dropped", 0.3, group_by="episode_id").run(table).passed

    def test_group_named_like_an_aggregate_does_not_collide(self):
        table = pa.table({"p_sum": ["x", "x", "y"], "rate": [True, True, False]})
        result = RateBelow("rate", 0.5, group_by="p_sum").run(table)
        assert not result.passed and result.metrics["sample"][0]["group"] == "x"


# --------------------------------------------------------------------------- max_gap


class TestMaxGap:
    def test_regular_stream_passes(self):
        result = MaxGap("ts", 40).run(frames([0, 33, 66, 100]))
        assert result.passed, result.details
        assert result.metrics["max_gap_ms"] == 34.0

    def test_hole_is_found(self):
        result = MaxGap("ts", 100, "episode_id").run(frames([0, 33, 66, 500, 533]))
        assert not result.passed
        assert result.failing_rows == 1
        sample = result.metrics["sample"][0]
        assert sample["gap_ms"] == 434.0 and sample["row"] == 3 and sample["group"] == "e1"
        assert result.name == "max_gap(ts by episode_id)"

    def test_row_order_does_not_matter(self):
        assert MaxGap("ts", 40).run(frames([66, 0, 100, 33])).passed

    def test_gaps_between_groups_are_not_gaps(self):
        table = frames([0, 33, 5000, 5033], episodes=["e1", "e1", "e2", "e2"])
        assert not MaxGap("ts", 100).run(table).passed
        result = MaxGap("ts", 100, group_by="episode_id").run(table)
        assert result.passed and result.metrics["groups"] == 2

    def test_nulls_skipped_and_numeric_units(self):
        table = pa.table({"t": [0, None, 1_000_000, 2_500_000]})
        result = MaxGap("t", 1000, unit="us").run(table)
        assert not result.passed and result.metrics["skipped_rows"] == 1
        assert result.metrics["max_gap_ms"] == 1500.0

    def test_non_time_column_fails_cleanly(self):
        result = MaxGap("t", 10).run(pa.table({"t": ["a", "b"]}))
        assert not result.passed and "numeric column" in result.details


# --------------------------------------------------------------------------- integration


def test_reports_serialise_to_json():
    table = pa.table({
        "episode_id": ["e1"] * 4,
        "rgb_ts": stamps([0, 33, 20, 400]),
        "depth_ts": stamps([1, 80, 21, 401]),
        "dropped": [False, True, False, False],
    })
    report = run_checks(table, [
        Monotonic("rgb_ts", group_by="episode_id"),
        MaxSkew("rgb_ts", "depth_ts", 20),
        RateBelow("dropped", 0.1, group_by="episode_id"),
        MaxGap("rgb_ts", 100, group_by="episode_id"),
    ])
    assert [result.passed for result in report] == [False, False, False, False]
    json.dumps(report.to_dict())
    with pytest.raises(DataQualityError) as error:
        report.raise_for_failures()
    assert "4 of 4 data quality checks failed" in str(error.value)


def test_pipeline_blocks_a_batch_with_temporal_checks(tmp_path):
    from local_data_platform.format.parquet import Parquet
    from local_data_platform.pipeline import Pipeline

    class Source:
        def get(self):
            return frames([0, 33, 20])

    target = Parquet("frames", tmp_path / "frames.parquet")
    pipeline = Pipeline(source=Source(), target=target,
                        checks=[{"check": "monotonic", "column": "ts", "group_by": "episode_id"}])
    with pytest.raises(DataQualityError):
        pipeline.run()
    assert not (tmp_path / "frames.parquet").exists()
