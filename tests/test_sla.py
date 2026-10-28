"""The SLA settings of a config: the ``metadata.sla`` block, its problems, its defaults and its summary."""

import copy

import pytest

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError
from local_data_platform.monitoring.sla import (
    DEFAULT_LOOKBACK_RUNS,
    Duration,
    Freshness,
    SchemaDrift,
    Sla,
    Volume,
    parse_sla,
    sla_problems,
)

FULL = {
    "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"},
                  "column": "ordered_at"},
    "volume": {"min_rows": 1, "max_rows": 1000000, "max_change_pct": 50, "lookback_runs": 7},
    "schema": {"on_change": "warn", "allow": ["added_column", "widened_type"]},
}


def _block(**sections) -> dict:
    block = copy.deepcopy(FULL)
    for key, value in sections.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return block


# ---------------------------------------------------------------------- the block


def test_a_full_block_is_normalised():
    assert parse_sla(FULL) == Sla(
        freshness=Freshness(warn_after=Duration(6, "hour"), error_after=Duration(26, "hour"), column="ordered_at"),
        volume=Volume(min_rows=1, max_rows=1000000, max_change_pct=50, lookback_runs=7),
        schema=SchemaDrift(on_change="warn", allow=("added_column", "widened_type")))
    assert parse_sla(FULL).to_dict() == FULL


def test_the_defaults_are_applied_and_a_missing_section_stays_missing():
    sla = parse_sla({"volume": {"max_rows": 10}, "schema": {}})
    assert sla.freshness is None
    assert sla.volume == Volume(max_rows=10, lookback_runs=DEFAULT_LOOKBACK_RUNS) and DEFAULT_LOOKBACK_RUNS == 7
    assert sla.schema == SchemaDrift(on_change="warn", allow=())
    assert sla.to_dict() == {"volume": {"max_rows": 10, "lookback_runs": 7},
                             "schema": {"on_change": "warn", "allow": []}}


def test_a_block_that_is_taken_out_and_put_back_is_the_same_block():
    for block in (FULL, _block(freshness=None), {"freshness": {"error_after": {"count": 1, "period": "day"}}},
                  {"volume": {"max_change_pct": 12.5}}, {"schema": {"on_change": "ignore"}}):
        sla = parse_sla(block)
        assert sla_problems(sla.to_dict()) == []
        assert parse_sla(sla.to_dict()) == sla


def test_no_block_is_no_sla():
    assert parse_sla(None) is None
    assert sla_problems(None) == []


def test_values_at_the_edge_are_fine():
    for block in ({"volume": {"min_rows": 5, "max_rows": 5}}, {"volume": {"min_rows": 1}},
                  {"freshness": {"error_after": {"count": 1, "period": "minute"}}},
                  {"freshness": {"warn_after": {"count": 1, "period": "hour"},
                                 "error_after": {"count": 61, "period": "minute"}}}):
        assert sla_problems(block) == [], block


@pytest.mark.parametrize("count, period, seconds", [(1, "minute", 60), (90, "minute", 5400), (26, "hour", 93600),
                                                    (1, "day", 86400), (3, "day", 259200)])
def test_a_duration_counts_in_seconds_and_a_day_is_24_hours(count, period, seconds):
    assert Duration(count, period).seconds == seconds
    assert str(Duration(count, period)) == f"{count} {period}"


def test_the_summary_is_one_short_line():
    short = {"freshness": {"error_after": {"count": 26, "period": "hour"}}, "volume": {"min_rows": 1}, "schema": {}}
    assert parse_sla(short).summary() == "freshness error after 26 hour, volume min 1 row, schema warn"
    assert parse_sla(FULL).summary() == (
        "freshness warn after 6 hour and error after 26 hour on column ordered_at, "
        "volume min 1 row and max 1000000 rows and change over 50 percent of the last 7 runs, "
        "schema warn except added_column and widened_type")
    assert parse_sla({"volume": {"min_rows": 2, "max_change_pct": 12.5, "lookback_runs": 1}}).summary() == \
        "volume min 2 rows and change over 12.5 percent of the last 1 run"
    assert parse_sla({"volume": {"max_change_pct": 50.0}}).summary() == \
        "volume change over 50 percent of the last 7 runs"
    assert parse_sla({"schema": {"on_change": "error"}}).summary() == "schema error"


# ---------------------------------------------------------------------- the problems


@pytest.mark.parametrize("block, expected", [
    ({"freshness": {"error_after": {"count": 26, "period": "week"}}},
     [(".freshness.error_after.period", "must be one of ['minute', 'hour', 'day'], got 'week'")]),
    ({"freshness": {"warn_after": {"count": 0, "period": "hour"}}},
     [(".freshness.warn_after.count", "must be a whole number from 1, got 0")]),
    ({"freshness": {"warn_after": {"count": True, "period": "hour"}}},
     [(".freshness.warn_after.count", "must be a whole number from 1, got True")]),
    ({"freshness": {"warn_after": {"count": 1.5, "period": "hour"}}},
     [(".freshness.warn_after.count", "must be a whole number from 1, got 1.5")]),
    ({"freshness": {"warn_after": {"count": "26", "period": "hour"}}},
     [(".freshness.warn_after.count", "must be a whole number from 1, got '26'")]),
    ({"freshness": {"error_after": {"period": "hour"}}}, [(".freshness.error_after.count", "is required")]),
    ({"freshness": {"error_after": {"count": 3}}},
     [(".freshness.error_after.period", "is required, one of ['minute', 'hour', 'day']")]),
    ({"freshness": {"error_after": "26 hours"}},
     [(".freshness.error_after", "must be an object such as {'count': 26, 'period': 'hour'}, got '26 hours'")]),
    ({"freshness": {"warn_after": {"count": 2, "period": "day"}, "error_after": {"count": 26, "period": "hour"}}},
     [(".freshness.warn_after", "must be shorter than error_after, got 2 day and 26 hour")]),
    ({"freshness": {"warn_after": {"count": 1, "period": "day"}, "error_after": {"count": 24, "period": "hour"}}},
     [(".freshness.warn_after", "must be shorter than error_after, got 1 day and 24 hour")]),
    ({"freshness": {"column": "ordered_at"}},
     [(".freshness", "has no threshold, set warn_after, error_after or both")]),
    ({"freshness": {"error_after": {"count": 1, "period": "day"}, "column": ""}},
     [(".freshness.column", "must be a column name, got ''")]),
    ({"freshness": {"error_after": {"count": 1, "period": "day"}, "column": 7}},
     [(".freshness.column", "must be a column name, got 7")]),
    ({"freshness": "late"}, [(".freshness", "must be an object with warn_after, error_after and column, got 'late'")]),
    ({"volume": {"min_rows": 0}}, [(".volume.min_rows", "must be a whole number from 1, got 0")]),
    ({"volume": {"max_rows": -5}}, [(".volume.max_rows", "must be a whole number from 1, got -5")]),
    ({"volume": {"min_rows": 1, "lookback_runs": 0}},
     [(".volume.lookback_runs", "must be a whole number from 1, got 0")]),
    ({"volume": {"min_rows": 1.5}}, [(".volume.min_rows", "must be a whole number from 1, got 1.5")]),
    ({"volume": {"max_change_pct": 0}}, [(".volume.max_change_pct", "must be a number above 0, got 0")]),
    ({"volume": {"max_change_pct": -10}}, [(".volume.max_change_pct", "must be a number above 0, got -10")]),
    ({"volume": {"max_change_pct": "50"}}, [(".volume.max_change_pct", "must be a number above 0, got '50'")]),
    ({"volume": {"max_change_pct": True}}, [(".volume.max_change_pct", "must be a number above 0, got True")]),
    ({"volume": {"max_change_pct": float("nan")}}, [(".volume.max_change_pct", "must be a number above 0, got nan")]),
    ({"volume": {"max_change_pct": float("inf")}}, [(".volume.max_change_pct", "must be a number above 0, got inf")]),
    ({"volume": {"min_rows": 10, "max_rows": 5}},
     [(".volume.min_rows", "must not be more than max_rows, got 10 and 5")]),
    ({"volume": {"lookback_runs": 5}}, [(".volume", "has no rule, set min_rows, max_rows or max_change_pct")]),
    ({"volume": []},
     [(".volume", "must be an object with min_rows, max_rows, max_change_pct and lookback_runs, got []")]),
    ({"schema": {"on_change": "fail"}},
     [(".schema.on_change", "must be one of ['ignore', 'warn', 'error'], got 'fail'")]),
    ({"schema": {"allow": "added_column"}},
     [(".schema.allow", "must be a list of changes from ['added_column', 'widened_type'], got 'added_column'")]),
    ({"schema": {"allow": ["added_column", "dropped_column"]}},
     [(".schema.allow[1]", "must be one of ['added_column', 'widened_type'], got 'dropped_column'")]),
    ({"schema": {"allow": ["added_column", "added_column"]}}, [(".schema.allow", "lists a change twice")]),
    ({"schema": 3}, [(".schema", "must be an object with on_change and allow, got 3")]),
    ({}, [("", "has nothing to check, set at least one of ['freshness', 'volume', 'schema']")]),
    ({"metrics": []}, [("", "has unknown keys ['metrics']; allowed: ['freshness', 'volume', 'schema']")]),
    ("daily", [("", "must be an object with some of ['freshness', 'volume', 'schema'], got 'daily'")]),
    (["freshness"], [("", "must be an object with some of ['freshness', 'volume', 'schema'], got ['freshness']")]),
])
def test_every_malformed_value_is_reported_with_its_path(block, expected):
    assert sla_problems(block) == expected


def test_unknown_keys_are_errors_at_every_level():
    block = {"metrics": [],
             "freshness": {"error_after": {"count": 1, "period": "day", "hours": 3}, "max_age": 1},
             "volume": {"min_rows": 1, "max_rows_pct": 3}, "schema": {"strict": True}}
    assert sla_problems(block) == [
        ("", "has unknown keys ['metrics']; allowed: ['freshness', 'volume', 'schema']"),
        (".freshness", "has unknown keys ['max_age']; allowed: ['warn_after', 'error_after', 'column']"),
        (".freshness.error_after", "has unknown keys ['hours']; allowed: ['count', 'period']"),
        (".volume", "has unknown keys ['max_rows_pct']; allowed: ['min_rows', 'max_rows', 'max_change_pct', "
                    "'lookback_runs']"),
        (".schema", "has unknown keys ['strict']; allowed: ['on_change', 'allow']"),
    ]


def test_every_problem_is_reported_and_not_only_the_first():
    block = {"freshness": {"error_after": {"count": 0, "period": "week"}}, "volume": {"min_rows": 0},
             "schema": {"on_change": "fail", "allow": ["nope"]}}
    paths = [path for path, _ in sla_problems(block)]
    assert paths == [".freshness.error_after.count", ".freshness.error_after.period", ".volume.min_rows",
                     ".schema.on_change", ".schema.allow[0]"]


def test_junk_gives_problems_and_never_an_exception():
    junk = [1, 2.5, True, "", [], [1], {"freshness": None}, {"freshness": {"warn_after": None}},
            {"freshness": {"warn_after": [], "error_after": 3}}, {"volume": {"min_rows": [1], "max_change_pct": {}}},
            {"schema": {"on_change": [], "allow": [[], {}, None]}},
            {"freshness": {"error_after": {"count": [], "period": {}}}}]
    for block in junk:
        assert sla_problems(block), block
        with pytest.raises(ConfigError, match=r"^metadata\.sla"):
            parse_sla(block)


def test_parse_sla_names_the_first_problem():
    with pytest.raises(ConfigError) as caught:
        parse_sla({"freshness": {"error_after": {"count": 26, "period": "week"}}, "volume": {"min_rows": 0}})
    assert str(caught.value) == ("metadata.sla.freshness.error_after.period: must be one of ['minute', 'hour', 'day'], "
                                 "got 'week'")


# ---------------------------------------------------------------------- the config


def _config(sla=None, **extra) -> dict:
    metadata = {"source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
                "target": {"name": "orders", "format": "ICEBERG", "write_mode": "overwrite",
                           "catalog": {"identifier": "shop", "warehouse_path": "warehouse"}}, **extra}
    if sla is not None:
        metadata["sla"] = sla
    return {"identifier": "orders", "who": "ops", "metadata": metadata}


def test_a_config_reads_its_sla(tmp_path):
    config = Config.from_dict(_config(FULL), base_dir=tmp_path)
    assert config.sla == parse_sla(FULL)
    assert config.sla.freshness.error_after.seconds == 26 * 3600
    assert config.sla.schema.allow == ("added_column", "widened_type")


def test_a_config_without_an_sla_has_none(tmp_path):
    assert Config.from_dict(_config(), base_dir=tmp_path).sla is None


def test_a_config_with_a_bad_sla_does_not_load(tmp_path):
    with pytest.raises(ConfigError, match=r"^metadata\.sla\.volume\.min_rows: must be a whole number from 1, got 0"):
        Config.from_dict(_config({"volume": {"min_rows": 0}}), base_dir=tmp_path)
    with pytest.raises(ConfigError, match=r"^metadata\.sla: has unknown keys \['metrics'\]"):
        Config.from_dict(_config({"metrics": []}), base_dir=tmp_path)
