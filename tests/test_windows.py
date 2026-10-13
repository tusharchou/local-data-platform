"""Windows: schedules, time zones, window shortcuts, source path placeholders and the filters of a windowed run.

The first half tests ``local_data_platform.orchestration.windows`` on its own, with a fixed clock where time matters.
The second half runs configs through ``plan``, ``run_config`` and ``ldp``: the rows of one window, the replacement
of one day, the source path of a window, the time zone and the freshness check.
"""

import datetime as dt
import json
import logging
import re
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.etl import run_config
from local_data_platform.events import MemorySink
from local_data_platform.exceptions import ConfigError, DataQualityError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import CommitContext
from local_data_platform.orchestration import windows
from local_data_platform.pipeline.registry import create_pipeline
from local_data_platform.spec import (
    HASH_EXCLUDED_METADATA,
    idempotency_key,
    json_schema,
    parse_window,
    plan,
    spec_hash,
    validate_spec,
    window_bounds,
)

UTC = dt.timezone.utc
CATALOG = {"identifier": "ns", "warehouse_path": "wh"}


def at(*args: int) -> dt.datetime:
    """A UTC instant."""
    return dt.datetime(*args, tzinfo=UTC)


# ====================================================================== time zones and window shortcuts


def test_a_time_zone_is_utc_by_default_or_an_iana_zone():
    assert windows.get_timezone(None) is UTC and windows.get_timezone("UTC") is UTC
    assert windows.get_timezone("Europe/Paris").key == "Europe/Paris"
    assert windows.get_timezone(UTC) is UTC


@pytest.mark.parametrize("name", ["Mars/Olympus", "", "   ", 5, "../etc/passwd", "America"])
def test_an_unknown_time_zone_is_a_config_error_that_names_it(name):
    with pytest.raises(ConfigError, match="time zone"):
        windows.get_timezone(name)


SHORTCUT_CASES = [
    # (name, time zone, now, expected start, expected end)
    ("yesterday", None, at(2026, 8, 5, 13), at(2026, 8, 4), at(2026, 8, 5)),
    ("yesterday", None, at(2026, 9, 1, 0, 30), at(2026, 8, 31), at(2026, 9, 1)),  # the day after a month end
    ("yesterday", None, at(2026, 8, 31, 23, 59, 59), at(2026, 8, 30), at(2026, 8, 31)),
    ("yesterday", None, at(2027, 1, 1, 8), at(2026, 12, 31), at(2027, 1, 1)),  # a year end
    ("yesterday", None, at(2024, 3, 1, 8), at(2024, 2, 29), at(2024, 3, 1)),  # a leap day
    ("last-month", None, at(2026, 3, 1), at(2026, 2, 1), at(2026, 3, 1)),
    ("last-month", None, at(2026, 1, 15, 10), at(2025, 12, 1), at(2026, 1, 1)),
    ("last-month", None, at(2024, 3, 31, 10), at(2024, 2, 1), at(2024, 3, 1)),  # February of a leap year
    ("last-month", None, at(2026, 5, 31, 10), at(2026, 4, 1), at(2026, 5, 1)),  # the 31st, after a 30-day month
    ("last-week", None, at(2026, 10, 14, 9), at(2026, 10, 5), at(2026, 10, 12)),  # a Wednesday
    ("last-week", None, at(2026, 10, 12), at(2026, 10, 5), at(2026, 10, 12)),  # a Monday: the week that just ended
    ("last-week", None, at(2026, 10, 11, 23, 59), at(2026, 9, 28), at(2026, 10, 5)),  # the Sunday before
    ("last-week", None, at(2027, 1, 7, 9), at(2026, 12, 28), at(2027, 1, 4)),  # across a year end
    # The time zone decides which day it is: 20:00 UTC is already the next day in Kolkata.
    ("yesterday", "UTC", at(2026, 8, 5, 20), at(2026, 8, 4), at(2026, 8, 5)),
    ("yesterday", "Asia/Kolkata", at(2026, 8, 5, 20), at(2026, 8, 4, 18, 30), at(2026, 8, 5, 18, 30)),
    # Clock changes: the day of the change is 23 or 25 hours long, and the bounds are local midnights.
    ("yesterday", "America/New_York", at(2026, 3, 9, 12), at(2026, 3, 8, 5), at(2026, 3, 9, 4)),
    ("yesterday", "America/New_York", at(2026, 11, 2, 12), at(2026, 11, 1, 4), at(2026, 11, 2, 5)),
    ("last-week", "America/New_York", at(2026, 3, 11, 15), at(2026, 3, 2, 5), at(2026, 3, 9, 4)),
    ("last-month", "America/New_York", at(2026, 4, 2, 12), at(2026, 3, 1, 5), at(2026, 4, 1, 4)),
    ("last-month", "Europe/Paris", at(2026, 11, 2, 12), at(2026, 9, 30, 22), at(2026, 10, 31, 23)),
    # A midnight that the clock skips starts the day at the change, and one that happens twice starts it the first time.
    ("yesterday", "America/Sao_Paulo", at(2018, 11, 5, 12), at(2018, 11, 4, 3), at(2018, 11, 5, 2)),
    ("yesterday", "America/Havana", at(2018, 11, 5, 12), at(2018, 11, 4, 4), at(2018, 11, 5, 5)),
]


@pytest.mark.parametrize("name, zone, now, start, end", SHORTCUT_CASES)
def test_a_shortcut_is_worked_out_from_a_fixed_clock_in_the_time_zone(name, zone, now, start, end):
    assert windows.shortcut_window(name, zone, now) == (start, end)


def test_a_shortcut_reads_a_naive_clock_as_utc_and_trims_blanks():
    naive_noon = dt.datetime(2026, 8, 5, 13)
    assert windows.shortcut_window("  yesterday ", None, naive_noon) == (at(2026, 8, 4), at(2026, 8, 5))


def test_a_shortcut_uses_the_clock_when_it_is_not_given(monkeypatch):
    monkeypatch.setattr(windows, "now_utc", lambda: at(2026, 8, 5, 13))
    assert windows.shortcut_window("yesterday") == (at(2026, 8, 4), at(2026, 8, 5))
    assert parse_window("yesterday") == (at(2026, 8, 4), at(2026, 8, 5))


def test_a_name_that_is_no_shortcut_is_a_config_error_that_lists_them():
    with pytest.raises(ConfigError, match="yesterday, last-week, last-month"):
        windows.shortcut_window("last-year")
    assert not windows.is_shortcut("last-year") and not windows.is_shortcut(None)
    assert all(windows.is_shortcut(name) for name in windows.SHORTCUTS)


def test_parse_window_takes_a_shortcut_a_time_zone_and_a_clock_and_still_takes_an_interval():
    assert parse_window("yesterday", timezone="Asia/Kolkata", now=at(2026, 8, 5, 20)) == (
        at(2026, 8, 4, 18, 30), at(2026, 8, 5, 18, 30))
    assert parse_window("2026-08-01/2026-08-02") == (at(2026, 8, 1), at(2026, 8, 2))
    with pytest.raises(ConfigError, match="START/END.*yesterday, last-week, last-month"):
        parse_window("tomorrow")


def test_window_bounds_counts_a_shortcut_in_the_time_zone_of_the_config():
    spec = {"identifier": "x", "metadata": {"window": {"size": "day", "timezone": "Asia/Kolkata"}}}
    assert window_bounds(spec, "yesterday", now=at(2026, 8, 5, 20)) == (at(2026, 8, 4, 18, 30), at(2026, 8, 5, 18, 30))
    assert window_bounds({"identifier": "x", "metadata": {}}, "yesterday", now=at(2026, 8, 5, 20)) == (
        at(2026, 8, 4), at(2026, 8, 5))
    assert window_bounds(None, "yesterday", now=at(2026, 8, 5, 20)) == (at(2026, 8, 4), at(2026, 8, 5))
    assert window_bounds(spec, None) is None
    # The zone is read only for a shortcut, so a bad zone does not break a run that gives its window in full.
    bad = {"identifier": "x", "metadata": {"window": {"size": "day", "timezone": "Mars/Olympus"}}}
    assert window_bounds(bad, "2026-08-01/2026-08-02") == (at(2026, 8, 1), at(2026, 8, 2))
    with pytest.raises(ConfigError, match="Mars/Olympus"):
        window_bounds(bad, "yesterday")


def test_the_idempotency_key_of_a_shortcut_is_the_key_of_the_window_it_stands_for(monkeypatch):
    spec = {"identifier": "orders", "metadata": {
        "source": {"name": "o", "format": "CSV", "path": "o.csv"},
        "target": {"name": "o", "format": "CSV", "path": "out.csv"},
        "window": {"size": "day", "timezone": "Asia/Kolkata"}}}
    monkeypatch.setattr(windows, "now_utc", lambda: at(2026, 8, 5, 20))
    assert idempotency_key(spec, "yesterday") == idempotency_key(spec, "2026-08-04T18:30:00Z/2026-08-05T18:30:00Z")
    assert idempotency_key(spec, "yesterday") != idempotency_key(spec, "2026-08-04/2026-08-05")


# ====================================================================== schedules


@pytest.mark.parametrize("text, cron", [
    ("@hourly", "0 * * * *"), ("@daily", "0 0 * * *"), ("@weekly", "0 0 * * 0"), ("@monthly", "0 0 1 * *"),
    ("5 2 * * *", "5 2 * * *"), ("*/15 * * * *", "*/15 * * * *"), ("0 0 * * MON-FRI", "0 0 * * MON-FRI"),
    ("  30   6  1,15 * * ", "30 6 1,15 * *"),
])
def test_a_schedule_is_a_cron_expression_or_one_of_four_aliases(text, cron):
    schedule = windows.parse_schedule(text)
    assert schedule.cron == cron and schedule.expression == " ".join(text.split())
    assert schedule.source == "metadata.schedule"


@pytest.mark.parametrize("text, message", [
    ("@yearly", "@hourly, @daily, @weekly, @monthly"),
    ("@reboot", "@hourly, @daily, @weekly, @monthly"),
    ("daily", "not a valid cron expression"),
    ("* * * *", "not a valid cron expression"),
    ("60 * * * *", "not a valid cron expression"),
    ("0 0 31 2 *", "not a valid cron expression"),
    ("", "cron expression"),
    ("   ", "cron expression"),
    (5, "cron expression"),
])
def test_a_bad_schedule_is_a_config_error(text, message):
    with pytest.raises(ConfigError, match=message):
        windows.parse_schedule(text)


def test_a_schedule_comes_from_metadata_first_and_from_when_second():
    explicit = windows.effective_schedule({"schedule": "5 2 * * *"}, "daily")
    assert (explicit.cron, explicit.source) == ("5 2 * * *", "metadata.schedule")
    for text, alias in [("hourly", "@hourly"), ("Daily", "@daily"), ("  WEEKLY ", "@weekly"), ("monthly", "@monthly")]:
        derived = windows.effective_schedule({}, text)
        assert (derived.expression, derived.source) == (alias, "when")
    assert windows.effective_schedule({}, "every batch") is None
    assert windows.effective_schedule({}, None) is None and windows.effective_schedule(None, "") is None
    with pytest.raises(ConfigError, match="cron"):
        windows.effective_schedule({"schedule": "nonsense"}, "daily")


def test_a_when_text_that_is_no_schedule_gets_a_note_and_the_others_do_not():
    note = windows.ignored_when({}, "after each upload batch")
    assert "'after each upload batch'" in note and "metadata.schedule" in note
    assert windows.ignored_when({}, "daily") is None
    assert windows.ignored_when({"schedule": "@daily"}, "after each upload batch") is None
    assert windows.ignored_when({}, "") is None and windows.ignored_when({}, None) is None


# ====================================================================== the window declaration


def test_a_window_declaration_is_none_or_a_size_with_an_optional_column_and_time_zone():
    assert windows.parse_window_spec(None) is None and windows.parse_window_spec("none") is None
    assert windows.parse_window_spec({"size": "15m"}) == windows.WindowSpec("15m", None, "UTC")
    full = windows.parse_window_spec({"size": "hour", "column": "ts", "timezone": "Europe/Paris"})
    assert full == windows.WindowSpec("hour", "ts", "Europe/Paris") and full.tz.key == "Europe/Paris"
    assert windows.WindowSpec("day").tz is UTC


@pytest.mark.parametrize("value, path, message", [
    ("daily", "", "'none' or an object"),
    (5, "", "'none' or an object"),
    ({"column": "ts"}, ".size", "is required"),
    ({"size": "week"}, ".size", "must be one of"),
    ({"size": "Day"}, ".size", "must be one of"),
    ({"size": "day", "colum": "ts"}, "", "unknown keys"),
    ({"size": "day", "column": ""}, ".column", "column name"),
    ({"size": "day", "column": 3}, ".column", "column name"),
    ({"size": "day", "timezone": "Mars/Olympus"}, ".timezone", "Mars/Olympus"),
])
def test_a_bad_window_declaration_names_the_path_of_the_problem(value, path, message):
    problems = windows.window_problems(value)
    assert [p for p, m in problems if p == path and re.search(message, m)], problems
    with pytest.raises(ConfigError, match=re.escape(f"metadata.window{problems[0][0]}")):
        windows.parse_window_spec(value)


# ====================================================================== placeholders in a source path


def test_placeholders_are_found_as_written_and_other_braces_are_left_alone():
    path = "d/{window_start:%Y}/o_{window_end:%m-%d}.csv"
    assert windows.placeholders(path) == ["{window_start:%Y}", "{window_end:%m-%d}"]
    assert windows.placeholders("d/{region}/o.csv") == [] and windows.placeholders(None) == []
    assert windows.mentions_placeholder("x{window_start") and not windows.mentions_placeholder("x{window}")


def test_placeholders_show_the_bounds_in_the_time_zone_of_the_window():
    bounds = (at(2026, 8, 4, 18, 30), at(2026, 8, 5, 18, 30))
    path = "drops/{window_start:%Y/%m}/orders_{window_start:%Y-%m-%d}_to_{window_end:%Y%m%d}.csv"
    assert windows.resolve_placeholders(path, bounds) == "drops/2026/08/orders_2026-08-04_to_20260805.csv"
    kolkata = windows.resolve_placeholders(path, bounds, "Asia/Kolkata")
    assert kolkata == "drops/2026/08/orders_2026-08-05_to_20260806.csv"
    assert windows.resolve_placeholders("plain.csv", bounds) == "plain.csv"
    assert windows.resolve_placeholders("h_{window_start:%H%M}.csv", bounds, "Asia/Kolkata") == "h_0000.csv"


@pytest.mark.parametrize("path, message", [
    ("o_{window_start}.csv", "needs a strftime format"),
    ("o_{window_start:}.csv", "needs a strftime format"),
    ("o_{window_end:YYYY-MM-DD}.csv", "no strftime directive"),
    ("o_{window_start:%Y.csv", "not closed"),
])
def test_a_malformed_placeholder_is_reported(path, message):
    assert any(re.search(message, problem) for problem in windows.placeholder_problems(path))


def test_a_good_path_has_no_placeholder_problems():
    assert windows.placeholder_problems("o_{window_start:%Y-%m-%d}.csv") == []
    assert windows.placeholder_problems("o.csv") == [] and windows.placeholder_problems(None) == []


def _config_object(path: str = "drops/o_{window_start:%Y-%m-%d}.csv", **metadata) -> Config:
    return Config.from_dict({"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": path},
        "target": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG},
        **metadata}})


def test_binding_fills_the_source_path_in_for_the_window_and_leaves_the_config_alone():
    config = _config_object(window={"size": "day", "timezone": "Asia/Kolkata"})
    bound = windows.bind_config(config, (at(2026, 8, 4, 18, 30), at(2026, 8, 5, 18, 30)))
    assert bound.source["path"] == "drops/o_2026-08-05.csv"
    assert config.source["path"] == "drops/o_{window_start:%Y-%m-%d}.csv"
    assert bound.target == config.target and bound.identifier == "orders"
    plain = _config_object("o.csv")
    assert windows.bind_config(plain, None) is plain and windows.bind_config(None, None) is None


def test_a_templated_path_without_a_window_is_a_config_error_that_names_the_placeholder():
    with pytest.raises(ConfigError) as caught:
        windows.bind_config(_config_object(), None)
    assert "{window_start:%Y-%m-%d}" in str(caught.value) and "window" in str(caught.value)


# ====================================================================== a scheduled config that has no window selection


def _metadata(path="o.csv", write_mode=None, fmt="ICEBERG", **extra) -> dict:
    target = {"name": "o", "format": fmt, "path": "out.csv"} if fmt != "ICEBERG" else {
        "name": "o", "format": "ICEBERG", "catalog": CATALOG}
    if write_mode:
        target["write_mode"] = write_mode
    return {"source": {"name": "o", "format": "CSV", "path": path}, "target": target, **extra}


@pytest.mark.parametrize("when, metadata, mode, lacks", [
    ("", _metadata(schedule="@daily"), None, True),  # a schedule, append by default, nothing selects rows
    ("daily", _metadata(), None, True),  # the schedule comes from when
    ("", _metadata(schedule="@daily", write_mode="append"), None, True),
    ("", _metadata(schedule="@daily", window="none"), None, True),
    ("", _metadata(schedule="@daily", window={"size": "day"}), None, True),  # a window without a column selects nothing
    ("", _metadata("o_{window_start:%Y-%m-%d}.csv", schedule="@daily"), None, False),
    ("", _metadata(schedule="@daily", window={"size": "day", "column": "ts"}), None, False),
    ("", _metadata(), None, False),  # no schedule
    ("every batch", _metadata(), None, False),  # a label is no schedule
    ("", _metadata(schedule="@daily", write_mode="overwrite"), None, False),
    ("", _metadata(schedule="@daily", write_mode="upsert"), None, False),
    ("", _metadata(schedule="@daily", fmt="CSV"), None, False),  # a file target is always replaced
    ("", _metadata(schedule="@daily", write_mode="overwrite"), "append", True),  # --mode append overrides
    ("", _metadata(schedule="@daily"), "overwrite", False),
])
def test_a_scheduled_config_that_appends_without_selecting_rows_is_found(when, metadata, mode, lacks):
    assert windows.lacks_window_selection(metadata, when, mode) is lacks


# ====================================================================== the rows of one window


def _events(*stamps: str, unit: str = "s", tz: str | None = None) -> pa.Table:
    """A table of ``id`` and ``at``, with one row per stamp."""
    moments = [dt.datetime.fromisoformat(stamp) for stamp in stamps]
    return pa.table({"id": list(range(1, len(stamps) + 1)), "at": pa.array(moments, pa.timestamp(unit, tz=tz))})


DAY_ONE = (at(2026, 8, 1), at(2026, 8, 2))


def test_the_rows_of_a_window_are_those_at_or_after_its_start_and_before_its_end():
    table = _events("2026-07-31T23:59:59", "2026-08-01T00:00:00", "2026-08-01T12:00:00", "2026-08-01T23:59:59",
                    "2026-08-02T00:00:00")
    assert windows.filter_rows(table, "at", DAY_ONE).column("id").to_pylist() == [2, 3, 4]


def test_a_row_with_no_time_is_in_no_window_and_an_empty_window_keeps_the_schema():
    table = pa.table({"id": [1, 2], "at": pa.array([dt.datetime(2026, 8, 1, 5), None], pa.timestamp("us"))})
    assert windows.filter_rows(table, "at", DAY_ONE).column("id").to_pylist() == [1]
    none = windows.filter_rows(table, "at", (at(2030, 1, 1), at(2030, 1, 2)))
    assert none.num_rows == 0 and none.schema == table.schema


@pytest.mark.parametrize("unit", ["s", "ms", "us", "ns"])
def test_a_timestamp_column_of_any_unit_is_filtered(unit):
    table = _events("2026-07-31T23:00:00", "2026-08-01T05:00:00", unit=unit)
    assert windows.filter_rows(table, "at", DAY_ONE).column("id").to_pylist() == [2]


def test_a_timestamp_with_a_time_zone_is_compared_as_an_instant():
    # 2026-08-01T01:00+02:00 is 2026-07-31T23:00Z, so it is before the window, whatever zone the column shows.
    moments = pa.array([at(2026, 7, 31, 23), at(2026, 8, 1, 0, 30)], pa.timestamp("us", tz="Europe/Paris"))
    table = pa.table({"id": [1, 2], "at": moments})
    assert windows.filter_rows(table, "at", DAY_ONE, "Asia/Kolkata").column("id").to_pylist() == [2]


def test_a_timestamp_without_a_time_zone_is_read_as_local_time_in_the_window_zone():
    table = _events("2026-08-01T00:30:00", "2026-08-01T23:30:00", "2026-08-02T00:10:00")
    local_day = (at(2026, 7, 31, 18, 30), at(2026, 8, 1, 18, 30))  # 1 August in Kolkata
    assert windows.filter_rows(table, "at", local_day, "Asia/Kolkata").column("id").to_pylist() == [1, 2]
    assert windows.filter_rows(table, "at", local_day).column("id").to_pylist() == [1]  # read as UTC


def test_a_date_column_is_compared_as_a_date_and_a_window_must_start_at_midnight():
    table = pa.table({"id": [1, 2, 3], "d": pa.array([dt.date(2026, 7, 31), dt.date(2026, 8, 1), dt.date(2026, 8, 2)])})
    assert windows.filter_rows(table, "d", DAY_ONE).column("id").to_pylist() == [2]
    week = (at(2026, 7, 31), at(2026, 8, 2))
    assert windows.filter_rows(table, "d", week).column("id").to_pylist() == [1, 2]
    with pytest.raises(ConfigError, match="start and end at midnight"):
        windows.filter_rows(table, "d", (at(2026, 8, 1, 6), at(2026, 8, 2, 6)))


def test_a_date_column_follows_the_time_zone_of_the_window():
    table = pa.table({"id": [1, 2], "d": pa.array([dt.date(2026, 8, 1), dt.date(2026, 8, 2)])})
    kolkata_day = (at(2026, 7, 31, 18, 30), at(2026, 8, 1, 18, 30))
    assert windows.filter_rows(table, "d", kolkata_day, "Asia/Kolkata").column("id").to_pylist() == [1]


@pytest.mark.parametrize("values, kind", [(["2026-08-01"], "string"), ([1], "int64"), ([1.5], "double")])
def test_a_column_that_is_not_a_date_or_timestamp_is_a_config_error(values, kind):
    with pytest.raises(ConfigError, match=f"date or timestamp column, but it is {kind}"):
        windows.filter_rows(pa.table({"at": values}), "at", DAY_ONE)


def test_a_missing_window_column_is_a_config_error_that_lists_the_columns():
    with pytest.raises(ConfigError, match=r"'when' is not a column of the source \(columns: id, at\)"):
        windows.filter_rows(_events("2026-08-01T05:00:00"), "when", DAY_ONE)


@pytest.mark.parametrize("kind, low, high", [
    ("naive", "2026-08-01T00:00:00", "2026-08-02T00:00:00"),
    ("aware", "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00"),
    ("date", "2026-08-01", "2026-08-02"),
])
def test_the_overwrite_filter_is_the_window_written_the_way_the_column_reads_it(kind, low, high):
    types = {"naive": pa.timestamp("us"), "aware": pa.timestamp("us", tz="Europe/Paris"), "date": pa.date32()}
    column_type = types[kind]
    assert _bounds_of(windows.overwrite_filter("at", DAY_ONE, "UTC", column_type)) == ("at", low, high)


def test_the_overwrite_filter_counts_a_naive_column_in_local_time():
    expression = windows.overwrite_filter("at", (at(2026, 7, 31, 18, 30), at(2026, 8, 1, 18, 30)), "Asia/Kolkata",
                                          pa.timestamp("us"))
    assert _bounds_of(expression) == ("at", "2026-08-01T00:00:00", "2026-08-02T00:00:00")
    with pytest.raises(ConfigError, match="date or timestamp column"):
        windows.overwrite_filter("at", DAY_ONE, "UTC", pa.string())


def _bounds_of(expression) -> tuple[str, str, str]:
    """The column and the two literals of ``column >= low AND column < high``."""
    from pyiceberg.expressions import And, GreaterThanOrEqual, LessThan

    assert isinstance(expression, And)
    assert isinstance(expression.left, GreaterThanOrEqual) and isinstance(expression.right, LessThan)
    assert expression.left.term.name == expression.right.term.name
    return expression.left.term.name, expression.left.literal.value, expression.right.literal.value


# ====================================================================== configs and runs

THREE_DAYS = [(1, "2026-08-01T10:00:00"), (2, "2026-08-02T10:00:00"), (3, "2026-08-03T10:00:00")]


def day(number: int) -> str:
    """The window of one day of August 2026, as ``START/END``."""
    return f"2026-08-0{number}/2026-08-0{number + 1}"


def _write(path: Path, rows: list[tuple]) -> Path:
    """A CSV of ``id``, ``at`` and ``note``; a row is ``(id, at)`` or ``(id, at, note)``."""
    path.write_text("id,at,note\n" + "".join(f"{row[0]},{row[1]},{row[2] if len(row) > 2 else 'v1'}\n" for row in rows))
    return path


def _config(folder: Path, *, path: str = "orders.csv", write_mode: str = "append", when: str | None = None,
            checks: list | None = None, target: str = "orders", **metadata) -> Path:
    """A config that loads ``path`` into the Iceberg table ``target``, with any extra ``metadata`` keys."""
    body = {
        "source": {"name": "orders", "format": "CSV", "path": path},
        "target": {"name": target, "format": "ICEBERG", "catalog": CATALOG, "write_mode": write_mode},
        **metadata,
    }
    if checks:
        body["quality"] = {"on_failure": "fail", "checks": checks}
    spec: dict = {"identifier": "orders", "metadata": body}
    if when:
        spec["when"] = when
    out = folder / "orders.json"
    out.write_text(json.dumps(spec))
    return out


def _table(folder: Path, name: str = "orders") -> Iceberg:
    return Iceberg(name, CATALOG, base_dir=folder)


def _rows(folder: Path) -> dict[int, str]:
    """The ``id`` to ``note`` of every row of the table."""
    data = _table(folder).table().scan().to_arrow().to_pylist()
    assert len(data) == len({row["id"] for row in data}), "an id appears twice"
    return {row["id"]: row["note"] for row in data}


def _count(folder: Path) -> int:
    return _table(folder).row_count()


BY_DAY = {"window": {"size": "day", "column": "at"}}


# ---------------------------------------------------------------------- the probe, and the window column


def test_three_daily_windows_over_one_file_write_three_rows_not_nine(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, **BY_DAY)

    results = [run_config(path, window=day(number)) for number in (1, 2, 3)]

    assert [(r.rows_read, r.rows_kept, r.rows_written) for r in results] == [(3, 1, 1)] * 3
    assert _count(tmp_path) == 3 and sorted(_rows(tmp_path)) == [1, 2, 3]


def test_without_a_window_column_a_window_is_only_an_idempotency_scope_and_loads_the_whole_file(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path)

    results = [run_config(path, window=day(number)) for number in (1, 2, 3)]

    assert [(r.rows_read, r.rows_kept, r.rows_written) for r in results] == [(3, None, 3)] * 3
    assert _count(tmp_path) == 9


def test_the_same_window_again_is_skipped_and_the_table_does_not_change(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, **BY_DAY)

    first, second = run_config(path, window=day(1)), run_config(path, window=day(1))

    assert (first.status, second.status) == ("published", "skipped_duplicate")
    assert second.published_snapshot_id == first.published_snapshot_id
    assert _count(tmp_path) == 1


def test_a_window_given_as_bounds_or_through_a_commit_context_selects_the_same_rows(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, **BY_DAY)
    bounds = (at(2026, 8, 1), at(2026, 8, 2))

    assert run_config(path, window=bounds).rows_kept == 1
    context = CommitContext(run_id="r-1", attempt=1, idempotency_key="k-2", spec_hash="h", search_since_ms=0,
                            logical_window=(at(2026, 8, 2), at(2026, 8, 3)))
    result = run_config(path, commit=context)

    assert result.rows_kept == 1 and sorted(_rows(tmp_path)) == [1, 2]


def test_a_pipeline_built_for_one_window_refuses_a_commit_context_for_another(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    config = Config.from_json(_config(tmp_path, **BY_DAY))
    context = CommitContext(run_id="r-1", attempt=1, idempotency_key="k", spec_hash="h", search_since_ms=0,
                            logical_window=(at(2026, 8, 2), at(2026, 8, 3)))

    pipeline = create_pipeline(config, window=day(1))

    assert pipeline.window == (at(2026, 8, 1), at(2026, 8, 2))
    with pytest.raises(ConfigError, match="2026-08-01.*2026-08-02T00:00:00.*2026-08-02.*2026-08-03"):
        pipeline.run(commit=context)
    assert not _table(tmp_path).exists()


def test_the_result_and_its_text_report_the_rows_kept_only_when_there_is_a_window_column(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    windowed = run_config(_config(tmp_path, **BY_DAY), window=day(2))
    assert windowed.to_dict()["rows_kept"] == 1
    assert str(windowed).startswith("orders: read 3 rows, kept 1 in the window, wrote 1 rows")

    plain = run_config(_config(tmp_path, target="plain"))
    assert plain.to_dict()["rows_kept"] is None
    assert str(plain).startswith("orders: read 3 rows, wrote 3 rows") and "kept" not in str(plain)


def test_the_extracted_event_reports_the_rows_read_and_the_rows_kept(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    sink = MemorySink()
    run_config(_config(tmp_path, **BY_DAY), window=day(3), sink=sink)
    payload = {event.type: event.payload for event in sink.events}["run.extracted"]
    assert (payload["rows_read"], payload["rows_kept"]) == (3, 1)

    sink = MemorySink()
    run_config(_config(tmp_path, target="plain"), sink=sink)
    assert "rows_kept" not in {event.type: event.payload for event in sink.events}["run.extracted"]


def test_the_checks_see_only_the_rows_of_the_window(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    checks = [{"check": "row_count", "equals": 1}, {"check": "row_count", "equals": "source"}]
    path = _config(tmp_path, checks=checks, **BY_DAY)

    result = run_config(path, window=day(1))  # the file holds 3 rows, and the window 1: both checks pass

    assert result.quality.passed and result.rows_written == 1


# ---------------------------------------------------------------------- replacing one day


def test_overwrite_with_a_window_column_replaces_only_the_rows_of_that_day(tmp_path):
    source = _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, write_mode="overwrite", **BY_DAY)
    for number in (1, 2, 3):
        run_config(path, window=day(number))
    assert _rows(tmp_path) == {1: "v1", 2: "v1", 3: "v1"}

    # Day 2 changes at the source: a correction and a new order. The other days change too, but are not re-run.
    _write(source, [(1, "2026-08-01T10:00:00", "v2"), (2, "2026-08-02T10:00:00", "v2"),
                    (20, "2026-08-02T11:00:00", "v2"), (3, "2026-08-03T10:00:00", "v2")])
    same = run_config(path, window=day(2))
    assert same.status == "skipped_duplicate" and _rows(tmp_path) == {1: "v1", 2: "v1", 3: "v1"}

    redo = run_config(path, window=day(2), idempotency_key="orders-2026-08-02-v2")

    assert redo.status == "published" and (redo.rows_read, redo.rows_kept, redo.rows_written) == (4, 2, 2)
    assert _rows(tmp_path) == {1: "v1", 2: "v2", 20: "v2", 3: "v1"}


@pytest.mark.parametrize("kind, stamp", [
    ("a timestamp without a time zone", "2026-08-0{d}T10:00:00"),
    ("a timestamp with a time zone", "2026-08-0{d}T10:00:00Z"),
    ("a date", "2026-08-0{d}"),
])
def test_a_window_overwrite_works_on_a_timestamp_a_zoned_timestamp_and_a_date(tmp_path, kind, stamp):
    source = _write(tmp_path / "orders.csv", [(number, stamp.format(d=number)) for number in (1, 2, 3)])
    path = _config(tmp_path, write_mode="overwrite", **BY_DAY)
    for number in (1, 2, 3):
        assert run_config(path, window=day(number)).rows_kept == 1, kind

    _write(source, [(number, stamp.format(d=number), "v2") for number in (1, 2, 3)])
    run_config(path, window=day(3), idempotency_key="again")

    assert _rows(tmp_path) == {1: "v1", 2: "v1", 3: "v2"}, kind


def test_overwrite_without_a_window_column_replaces_the_whole_table_with_the_run(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, write_mode="overwrite")
    run_config(path, window=day(1))
    _write(tmp_path / "orders.csv", [(9, "2026-08-02T10:00:00")])
    run_config(path, window=day(2))
    assert sorted(_rows(tmp_path)) == [9]


def test_a_mode_override_to_overwrite_replaces_the_window_too(tmp_path):
    source = _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, **BY_DAY)  # the config appends
    for number in (1, 2, 3):
        run_config(path, window=day(number))
    _write(source, [(2, "2026-08-02T10:00:00", "v2")])

    run_config(path, mode="overwrite", window=day(2), idempotency_key="redo")

    assert _rows(tmp_path) == {1: "v1", 2: "v2", 3: "v1"}


def test_a_window_overwrite_needs_the_window_column_in_the_data_it_writes(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    config = Config.from_json(_config(tmp_path, write_mode="overwrite", **BY_DAY))
    pipeline = create_pipeline(config, window=day(1), transforms=[lambda table: table.drop_columns(["at"])])

    with pytest.raises(ConfigError, match=r"'at' is not a column of the data to write.*dropped or renamed"):
        pipeline.run()
    assert not _table(tmp_path).exists()


# ---------------------------------------------------------------------- the source path of a window


def _drops(folder: Path) -> None:
    """One file per day, as an exporter drops them. The file of day 2 also holds two late rows of day 1."""
    (folder / "drops").mkdir()
    _write(folder / "drops" / "orders_2026-08-01.csv", [(1, "2026-08-01T10:00:00")])
    late = [(10, "2026-08-01T23:00:00"), (11, "2026-08-01T23:30:00")]
    _write(folder / "drops" / "orders_2026-08-02.csv", [(2, "2026-08-02T10:00:00"), *late])
    _write(folder / "drops" / "orders_2026-08-03.csv", [(3, "2026-08-03T10:00:00")])


DROP = "drops/orders_{window_start:%Y-%m-%d}.csv"


def test_a_run_reads_the_file_of_its_window(tmp_path):
    _drops(tmp_path)
    path = _config(tmp_path, path=DROP, schedule="@daily")

    first = run_config(path, window=day(1))
    third = run_config(path, window=day(3))

    assert (first.rows_read, third.rows_read) == (1, 1) and sorted(_rows(tmp_path)) == [1, 3]


def test_a_run_without_a_window_on_a_templated_path_names_the_placeholder_and_writes_nothing(tmp_path):
    _drops(tmp_path)
    path = _config(tmp_path, path=DROP, schedule="@daily")

    for kwargs in ({}, {"idempotency_key": "some-key"}):
        with pytest.raises(ConfigError) as caught:
            run_config(path, **kwargs)
        assert "{window_start:%Y-%m-%d}" in str(caught.value) and "window" in str(caught.value)
    assert not _table(tmp_path).exists()


def test_a_commit_context_that_carries_a_window_reads_the_file_of_that_window(tmp_path):
    _drops(tmp_path)
    path = _config(tmp_path, path=DROP, schedule="@daily")
    context = CommitContext(run_id="r-1", attempt=1, idempotency_key="k-3", spec_hash="h", search_since_ms=0,
                            logical_window=(at(2026, 8, 3), at(2026, 8, 4)))

    result = run_config(path, commit=context)

    assert result.rows_read == 1 and sorted(_rows(tmp_path)) == [3]


def test_a_path_placeholder_and_a_window_column_work_together(tmp_path):
    _drops(tmp_path)
    path = _config(tmp_path, path=DROP, schedule="@daily", **BY_DAY)

    result = run_config(path, window=day(2))

    assert (result.rows_read, result.rows_kept) == (3, 1) and sorted(_rows(tmp_path)) == [2]


def test_a_missing_file_of_a_window_is_the_usual_error(tmp_path):
    _drops(tmp_path)
    with pytest.raises(FileNotFoundError, match="orders_2026-08-09.csv"):
        run_config(_config(tmp_path, path=DROP, schedule="@daily"), window="2026-08-09/2026-08-10")


# ---------------------------------------------------------------------- time zones


def _kolkata_config(folder: Path, **extra) -> Path:
    return _config(folder, window={"size": "day", "column": "at", "timezone": "Asia/Kolkata"}, **extra)


KOLKATA_ROWS = [(1, "2026-08-01T00:30:00"), (2, "2026-08-01T23:30:00"), (3, "2026-08-02T00:10:00")]


def test_the_time_zone_of_the_config_decides_which_rows_belong_to_a_day(tmp_path):
    _write(tmp_path / "orders.csv", KOLKATA_ROWS)  # local times: the file knows no zone
    local_day = "2026-07-31T18:30:00Z/2026-08-01T18:30:00Z"  # 1 August in Kolkata

    in_kolkata = run_config(_kolkata_config(tmp_path), window=local_day)
    assert sorted(_rows(tmp_path)) == [1, 2] and in_kolkata.rows_kept == 2

    as_utc = run_config(_config(tmp_path, target="utc", **BY_DAY), window=local_day)
    assert as_utc.rows_kept == 1  # read as UTC, the second row is after 18:30


def test_yesterday_is_the_local_day_of_the_config_and_its_key_is_the_key_of_that_day(tmp_path, monkeypatch):
    _write(tmp_path / "orders.csv", KOLKATA_ROWS)
    path = _kolkata_config(tmp_path)
    monkeypatch.setattr(windows, "now_utc", lambda: at(2026, 8, 2, 3))  # 08:30 on 2 August in Kolkata

    result = run_config(path, window="yesterday")

    assert result.rows_kept == 2 and sorted(_rows(tmp_path)) == [1, 2]
    local_day = "2026-07-31T18:30:00Z/2026-08-01T18:30:00Z"
    assert result.idempotency_key == idempotency_key(Config.from_json(path), local_day)
    assert run_config(path, window="yesterday").status == "skipped_duplicate"


# ---------------------------------------------------------------------- freshness


def test_freshness_is_checked_against_the_end_of_the_window_so_a_backfill_passes_what_the_first_run_passed(tmp_path):
    (tmp_path / "orders.csv").write_text("id,at,note\n1,2020-08-02T10:00:00,v1\n")
    checks = [{"check": "freshness", "column": "at", "max_age_hours": 24}]
    path = _config(tmp_path, checks=checks, **BY_DAY)

    result = run_config(path, window="2020-08-02/2020-08-03")  # the newest row is 14 hours before the window ends

    freshness = result.quality.results[0].metrics
    assert result.quality.passed and freshness["now"] == "2020-08-03T00:00:00+00:00" and freshness["age_hours"] == 14.0
    with pytest.raises(DataQualityError, match="older than the 24"):
        run_config(_config(tmp_path, checks=checks, target="again", **BY_DAY))  # the clock is years later: stale


def test_a_freshness_check_with_its_own_now_keeps_it_in_a_windowed_run(tmp_path):
    (tmp_path / "orders.csv").write_text("id,at,note\n1,2020-08-02T10:00:00,v1\n")
    checks = [{"check": "freshness", "column": "at", "max_age_hours": 24, "now": "2020-08-10T00:00:00"}]
    with pytest.raises(DataQualityError, match="older than the 24"):
        run_config(_config(tmp_path, checks=checks, **BY_DAY), window="2020-08-02/2020-08-03")


# ---------------------------------------------------------------------- what goes wrong


def test_a_window_column_that_the_source_lacks_fails_naming_the_columns_and_writes_nothing(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    path = _config(tmp_path, window={"size": "day", "column": "created"})
    sink = MemorySink()

    with pytest.raises(ConfigError, match=r"'created' is not a column of the source \(columns: id, at, note\)"):
        run_config(path, window=day(1), sink=sink)

    assert not _table(tmp_path).exists() and sink.types[-2:] == ["run.failed", "run.finished"]


def test_a_window_column_that_is_not_a_date_or_timestamp_fails_before_anything_is_written(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    with pytest.raises(ConfigError, match="date or timestamp column, but it is int64"):
        run_config(_config(tmp_path, window={"size": "day", "column": "id"}), window=day(1))
    assert not _table(tmp_path).exists()


def test_a_window_that_keeps_no_rows_writes_nothing_and_a_row_count_check_holds_it_back(tmp_path):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    with pytest.raises(ValueError, match="No data to write"):
        run_config(_config(tmp_path, **BY_DAY), window="2026-08-09/2026-08-10")
    assert not _table(tmp_path).exists()

    gated = _config(tmp_path, checks=[{"check": "row_count", "min": 1}], **BY_DAY)
    with pytest.raises(DataQualityError, match="row_count"):
        run_config(gated, window="2026-08-09/2026-08-10")


def test_a_scheduled_append_with_no_way_to_select_a_window_warns_when_it_runs_with_one(tmp_path, caplog):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    caplog.set_level(logging.WARNING, logger="local_data_platform.etl")

    run_config(_config(tmp_path, schedule="@daily"), window=day(1))
    assert any("nothing selects the rows of a window" in record.getMessage() for record in caplog.records)

    caplog.clear()
    run_config(_config(tmp_path, target="b"), window=day(1))  # no schedule: a window is only an idempotency scope
    run_config(_config(tmp_path, target="c", schedule="@daily"))  # no window: nothing to warn about
    run_config(_config(tmp_path, target="d", schedule="@daily", **BY_DAY), window=day(1))  # a column selects the rows
    assert not caplog.records


# ====================================================================== the spec: schema, validation, hash and plan


def _spec(**metadata) -> dict:
    return {"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG}, **metadata}}


def test_the_json_schema_describes_the_schedule_the_window_and_the_source_path_placeholders():
    schema = json_schema()
    properties = schema["$defs"]["metadata"]["properties"]
    assert properties["schedule"]["examples"] == ["@daily", "5 2 * * *"]
    assert properties["window"] == {"$ref": "#/$defs/window"} and properties["source"] == {"$ref": "#/$defs/source"}
    window = schema["$defs"]["window"]["oneOf"]
    assert window[0] == {"const": "none"} and window[1]["required"] == ["size"]
    assert window[1]["additionalProperties"] is False
    assert window[1]["properties"]["size"] == {"enum": ["day", "hour", "15m"]}
    assert "{window_start:FORMAT}" in schema["$defs"]["source"]["properties"]["path"]["description"]
    json.dumps(schema)


@pytest.mark.parametrize("metadata", [
    {"schedule": "@daily"},
    {"schedule": "5 2 * * *", "window": "none"},
    {"window": {"size": "day"}},
    {"schedule": "@hourly", "window": {"size": "15m", "column": "ts", "timezone": "Europe/Paris"}},
])
def test_a_spec_with_a_schedule_and_a_window_is_valid(metadata):
    assert validate_spec(_spec(**metadata)) == []
    assert validate_spec({**_spec(**metadata), "when": "daily"}) == []


@pytest.mark.parametrize("metadata, where, message", [
    ({"schedule": "daily"}, "metadata.schedule", "not a valid cron expression"),
    ({"schedule": "@yearly"}, "metadata.schedule", "@hourly, @daily, @weekly, @monthly"),
    ({"schedule": 5}, "metadata.schedule", "cron expression"),
    ({"window": "daily"}, "metadata.window", "'none' or an object"),
    ({"window": {"size": "week"}}, "metadata.window.size", "must be one of"),
    ({"window": {}}, "metadata.window.size", "is required"),
    ({"window": {"size": "day", "column": ""}}, "metadata.window.column", "column name"),
    ({"window": {"size": "day", "timezone": "Mars/Olympus"}}, "metadata.window.timezone", "Mars/Olympus"),
    ({"window": {"size": "day", "extra": 1}}, "metadata.window", "unknown keys"),
])
def test_a_bad_schedule_or_window_is_reported_at_its_path(metadata, where, message):
    problems = [str(error) for error in validate_spec(_spec(**metadata))]
    assert any(problem.startswith(f"{where}: ") and re.search(message, problem) for problem in problems), problems


@pytest.mark.parametrize("path, message", [
    ("o_{window_start}.csv", "needs a strftime format"),
    ("o_{window_end:YYYY}.csv", "no strftime directive"),
    ("o_{window_start:%Y.csv", "not closed"),
])
def test_a_malformed_placeholder_in_the_source_path_is_reported(path, message):
    spec = _spec()
    spec["metadata"]["source"]["path"] = path
    problems = [str(error) for error in validate_spec(spec)]
    assert any(problem.startswith("metadata.source.path: ") and re.search(message, problem) for problem in problems)


def test_a_window_placeholder_anywhere_but_the_source_path_is_reported():
    spec = _spec(quality={"checks": [{"check": "freshness", "column": "at", "max_age_hours": 1,
                                      "now": "{window_end:%Y-%m-%dT%H:%M:%S}"}]})
    spec["metadata"]["target"]["path"] = "out_{window_start:%Y}.csv"
    spec["metadata"]["source"]["path"] = "in_{window_start:%Y}.csv"
    problems = [str(error) for error in validate_spec(spec)]
    assert len([p for p in problems if "works only in metadata.source.path" in p]) == 2, problems
    assert any(p.startswith("metadata.target.path: ") for p in problems)
    assert any(p.startswith("metadata.quality.checks[0].now: ") for p in problems)


def test_the_schedule_is_left_out_of_the_spec_hash_and_the_window_is_part_of_it():
    assert "schedule" in HASH_EXCLUDED_METADATA and "window" not in HASH_EXCLUDED_METADATA
    base = spec_hash(_spec())
    assert spec_hash(_spec(schedule="@daily")) == spec_hash(_spec(schedule="5 2 * * *")) == base
    windowed = spec_hash(_spec(window={"size": "day", "column": "at"}))
    assert windowed != base
    assert spec_hash(_spec(window={"size": "day", "column": "created"})) != windowed
    assert spec_hash(_spec(window={"size": "hour", "column": "at"})) != windowed
    assert spec_hash(_spec(schedule="@daily", window={"size": "day", "column": "at"})) == windowed


def test_existing_configs_keep_their_spec_hash():
    """The digest is what version 0.1.2 gives this config, so editing the hash code must not move it."""
    inline = {"identifier": "orders", "when": "daily", "how": "batch",
              "metadata": {"source": {"name": "orders", "format": "csv", "path": "drops/orders.csv"},
                           "target": {"name": "orders", "format": "iceberg", "write_mode": "Append",
                                      "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
                           "quality": [{"check": "row_count", "min": 1}],
                           "observability": {"sinks": ["jsonl"]}}}
    assert spec_hash(inline, base_dir="/x/y") == "2251d9f437db30d1ccf1b97ef0e9acb82db8ff5198611cc3dbf449b27b7ca5f6"


def test_a_placeholder_path_is_hashed_as_written_so_every_window_has_one_spec_hash():
    spec = _spec()
    spec["metadata"]["source"]["path"] = DROP
    assert spec_hash(spec) == spec_hash(json.loads(json.dumps(spec)))
    assert spec_hash(spec) != spec_hash(_spec())


def test_a_config_object_exposes_its_schedule_and_its_window_and_rejects_bad_ones():
    declared = {"size": "day", "column": "at", "timezone": "Asia/Tokyo"}
    config = Config.from_dict(_spec(schedule="5 2 * * *", window=declared))
    assert (config.schedule.cron, config.schedule.source) == ("5 2 * * *", "metadata.schedule")
    assert config.window == windows.WindowSpec("day", "at", "Asia/Tokyo")
    derived = Config.from_dict({**_spec(), "when": "weekly"})
    assert (derived.schedule.expression, derived.schedule.source) == ("@weekly", "when") and derived.window is None
    assert Config.from_dict(_spec()).schedule is None
    with pytest.raises(ConfigError, match=r"metadata\.schedule: schedule 'nope' is not a valid cron expression"):
        Config.from_dict(_spec(schedule="nope"))
    with pytest.raises(ConfigError, match=r"metadata\.window\.size: must be one of"):
        Config.from_dict(_spec(window={"size": "week"}))


def test_plan_shows_the_schedule_the_window_and_the_source_path_of_the_window(tmp_path):
    _drops(tmp_path)
    declared = {"size": "day", "column": "at", "timezone": "Asia/Kolkata"}
    path = _config(tmp_path, path=DROP, schedule="5 2 * * *", window=declared)
    result = plan(path, window="2026-08-01T18:30:00Z/2026-08-02T18:30:00Z")
    assert result["valid"], result["errors"]
    assert (result["schedule"], result["schedule_from"]) == ("5 2 * * *", "metadata.schedule")
    assert (result["window_size"], result["window_column"], result["timezone"]) == ("day", "at", "Asia/Kolkata")
    assert result["source_path_template"] == DROP and result["source_path"] == "drops/orders_2026-08-02.csv"
    assert result["notes"] == [] and result["window"] == "2026-08-01T18:30:00Z/2026-08-02T18:30:00Z"

    unresolved = plan(path)
    assert unresolved["source_path"] == DROP and unresolved["window"] is None
    assert any("shown as written" in note for note in unresolved["notes"])


def test_plan_says_when_the_file_of_the_window_is_not_there_yet(tmp_path):
    _drops(tmp_path)
    path = _config(tmp_path, path=DROP, schedule="@daily", **BY_DAY)
    assert plan(path, window=day(1))["notes"] == []
    missing = plan(path, window="2026-08-09/2026-08-10")
    assert missing["valid"] and any("orders_2026-08-09.csv does not exist yet" in note for note in missing["notes"])


def test_plan_resolves_a_shortcut_in_the_time_zone_of_the_config(tmp_path, monkeypatch):
    path = _config(tmp_path, window={"size": "day", "timezone": "Asia/Kolkata"})
    monkeypatch.setattr(windows, "now_utc", lambda: at(2026, 8, 5, 20))
    result = plan(path, window="yesterday")
    assert result["window"] == "2026-08-04T18:30:00Z/2026-08-05T18:30:00Z"
    assert result["idempotency_key"] == idempotency_key(Config.from_json(path), result["window"])


def test_plan_derives_the_schedule_from_when_and_notes_a_label(tmp_path):
    derived = plan(_config(tmp_path, when="Daily", write_mode="upsert", target="x"))
    assert derived["valid"] is False  # an upsert with no join_cols
    daily = plan(_config(tmp_path, when="daily", write_mode="overwrite"))
    assert daily["valid"] and (daily["schedule"], daily["schedule_from"]) == ("@daily", "when") and daily["notes"] == []
    label = plan(_config(tmp_path, when="after each upload batch", write_mode="overwrite"))
    assert label["valid"] and label["schedule"] is None
    assert any("'after each upload batch'" in note and "metadata.schedule" in note for note in label["notes"])
    explicit = plan(_config(tmp_path, when="after each upload batch", write_mode="overwrite", schedule="@hourly"))
    assert explicit["schedule"] == "@hourly" and explicit["notes"] == []


def test_plan_reports_an_error_for_a_scheduled_append_that_selects_no_rows_of_a_window(tmp_path):
    for config in (_config(tmp_path, schedule="@daily"), _config(tmp_path, when="daily"),
                   _config(tmp_path, schedule="@daily", window={"size": "day"})):
        result = plan(config)
        assert result["valid"] is False
        assert len(result["errors"]) == 1 and "every window would load the same rows again" in result["errors"][0]
        assert "{window_start:FORMAT}" in result["errors"][0] and "metadata.window.column" in result["errors"][0]
    for selected in (_config(tmp_path, schedule="@daily", **BY_DAY), _config(tmp_path, schedule="@daily", path=DROP),
                     _config(tmp_path, schedule="@daily", write_mode="overwrite"),
                     _config(tmp_path, schedule="@daily", write_mode="upsert", target="u", join_cols=None),
                     _config(tmp_path)):
        if json.loads(selected.read_text())["metadata"]["target"]["write_mode"] == "upsert":
            continue  # an upsert needs join_cols; the rule does not apply to it
        assert plan(selected)["valid"], plan(selected)["errors"]


def test_plan_notes_that_an_overwrite_by_the_file_of_a_window_replaces_the_whole_table(tmp_path):
    note = "replaces the whole table with the rows of each window"
    by_file = _config(tmp_path, schedule="@daily", write_mode="overwrite", path=DROP)
    assert any(note in n for n in plan(by_file)["notes"])
    by_file_and_column = _config(tmp_path, schedule="@daily", write_mode="overwrite", path=DROP, **BY_DAY)
    assert not any(note in n for n in plan(by_file_and_column)["notes"])


def test_plan_checks_the_workbook_of_the_window_and_skips_it_when_there_is_no_window(tmp_path):
    xlsx = "books_{window_start:%Y%m}.xlsx"
    spec = _spec(schedule="@monthly", write_mode=None)
    spec["metadata"]["source"] = {"name": "books", "format": "XLSX", "path": xlsx}
    spec["metadata"]["target"]["write_mode"] = "overwrite"
    path = tmp_path / "books.json"
    path.write_text(json.dumps(spec))

    assert plan(path)["valid"]  # the path cannot be resolved without a window, so it is not checked
    missing = plan(path, window="2026-08-01/2026-09-01")
    assert missing["valid"] is False and "books_202608.xlsx does not exist" in missing["errors"][0]


# ====================================================================== the command line


def test_ldp_plan_prints_the_schedule_the_window_and_the_source_path_and_notes(tmp_path, capsys):
    _drops(tmp_path)
    path = _config(tmp_path, path=DROP, schedule="@daily", write_mode="overwrite",
                   window={"size": "day", "timezone": "Asia/Kolkata"})

    assert main(["plan", str(path), "--window", "2026-08-01T18:30:00Z/2026-08-02T18:30:00Z"]) == 0

    out = capsys.readouterr().out
    fields = {line.split(None, 1)[0]: line.split(None, 1)[1].strip() for line in out.splitlines() if "  " in line}
    assert fields["schedule"] == "@daily (from metadata.schedule)"
    assert (fields["window_size"], fields["timezone"]) == ("day", "Asia/Kolkata") and "window_column" not in fields
    assert fields["source_path"] == "drops/orders_2026-08-02.csv"
    assert fields["window"] == "2026-08-01T18:30:00Z/2026-08-02T18:30:00Z"
    assert "note: write_mode overwrite replaces the whole table" in out


def test_ldp_plan_exits_one_and_prints_the_error_for_a_scheduled_append_that_selects_no_rows(tmp_path, capsys):
    assert main(["plan", str(_config(tmp_path, when="daily"))]) == 1
    assert "every window would load the same rows again" in capsys.readouterr().err


def test_ldp_run_takes_a_shortcut_and_prints_the_window_it_stands_for(tmp_path, capsys, monkeypatch):
    _write(tmp_path / "orders.csv", KOLKATA_ROWS)
    path = _kolkata_config(tmp_path)
    monkeypatch.setattr(windows, "now_utc", lambda: at(2026, 8, 2, 3))

    assert main(["run", str(path), "--window", "yesterday"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("orders: read 3 rows, kept 2 in the window, wrote 2 rows")
    assert "2026-07-31T18:30:00Z/2026-08-01T18:30:00Z" in out
    assert main(["run", str(path), "--window", "yesterday", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "skipped_duplicate"


def test_ldp_run_warns_on_stderr_about_a_scheduled_append_that_selects_no_rows(tmp_path, caplog):
    _write(tmp_path / "orders.csv", THREE_DAYS)
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        assert main(["run", str(_config(tmp_path, schedule="@daily")), "--window", day(1)]) == 0
    assert any("nothing selects the rows of a window" in record.getMessage() for record in caplog.records)
