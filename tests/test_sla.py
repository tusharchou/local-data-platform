"""The SLA settings of a config: the ``metadata.sla`` block, its problems, its defaults and its summary."""

import copy

import pytest

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError
from local_data_platform.monitoring.sla import (
    DEFAULT_LOOKBACK,
    DEFAULT_LOOKBACK_RUNS,
    GRAINS,
    METRIC_KEYS,
    NAME_PATTERN,
    Duration,
    Freshness,
    MetricRule,
    SchemaDrift,
    Sla,
    Volume,
    figure,
    parse_sla,
    sla_problems,
    sla_schema,
)

FULL = {
    "freshness": {"warn_after": {"count": 6, "period": "hour"}, "error_after": {"count": 26, "period": "hour"},
                  "column": "ordered_at"},
    "volume": {"min_rows": 1, "max_rows": 1000000, "max_change_pct": 50, "lookback_runs": 7},
    "schema": {"on_change": "warn", "allow": ["added_column", "widened_type"]},
    "metrics": [{"metric": "revenue", "grain": "day", "min": 0, "max": 1000000, "max_change_pct": 50,
                 "filter": {"city": "Pune"}, "lookback": 7}],
}


def _block(**sections) -> dict:
    block = copy.deepcopy(FULL)
    for key, value in sections.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return block


GRAIN_LIST = "['day', 'week', 'month', 'quarter', 'year']"
SHARED_NAME = "'revenue' is also used by metrics[0], so set a name that tells the two entries apart"
NAME_RULE = "must be lower case letters, digits and underscores that start with a letter, got "
FILTER_VALUE = "must be a value or a list of values, each text, a number or a bool, got "


def _entry(**changes) -> dict:
    """A valid entry of the ``metrics`` section with some keys replaced. A value of ``None`` removes the key."""
    entry = {"metric": "revenue", "grain": "day", "min": 0}
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


METRIC_PROBLEMS = [
    ({"metrics": []}, [(".metrics", "has nothing to watch, list at least one metric")]),
    ({"metrics": "revenue"}, [(".metrics", "must be a list of the metrics to watch, got 'revenue'")]),
    ({"metrics": {"metric": "revenue"}},
     [(".metrics", "must be a list of the metrics to watch, got {'metric': 'revenue'}")]),
    ({"metrics": [5]}, [(".metrics[0]", "must be an object with metric or sql, grain and a rule, got 5")]),
    ({"metrics": [_entry(metric=None)]}, [(".metrics[0]", "has neither metric nor sql, set one of them")]),
    ({"metrics": [_entry(sql="SELECT 1", name="one")]}, [(".metrics[0]", "has both metric and sql, set one of them")]),
    ({"metrics": [_entry(metric="")]}, [(".metrics[0].metric", "must be the name of a metric, got ''")]),
    ({"metrics": [_entry(metric=7)]}, [(".metrics[0].metric", "must be the name of a metric, got 7")]),
    ({"metrics": [_entry(metric=None, sql="SELECT 1")]},
     [(".metrics[0].name", "is required with sql, because a query has no name of its own")]),
    ({"metrics": [_entry(metric=None, sql="  ", name="one")]},
     [(".metrics[0].sql", "must be a query that returns one number, got '  '")]),
    ({"metrics": [_entry(metric=None, sql="SELECT 1", name="one", filter={"city": "Pune"})]},
     [(".metrics[0].filter", "is for a metric, and a query says in its own text what to keep")]),
    ({"metrics": [_entry(metric=None, sql="SELECT 1", name="one", filter={})]},
     [(".metrics[0].filter", "is for a metric, and a query says in its own text what to keep")]),
    ({"metrics": [_entry(name="Revenue")]}, [(".metrics[0].name", NAME_RULE + "'Revenue'")]),
    ({"metrics": [_entry(name="1st")]}, [(".metrics[0].name", NAME_RULE + "'1st'")]),
    ({"metrics": [_entry(name="a" * 64)]}, [(".metrics[0].name", NAME_RULE + repr("a" * 64))]),
    ({"metrics": [_entry(name=5)]}, [(".metrics[0].name", NAME_RULE + "5")]),
    ({"metrics": [_entry(grain=None)]}, [(".metrics[0].grain", f"is required, one of {GRAIN_LIST}")]),
    ({"metrics": [_entry(grain="hour")]}, [(".metrics[0].grain", f"must be one of {GRAIN_LIST}, got 'hour'")]),
    ({"metrics": [_entry(grain=["day"])]}, [(".metrics[0].grain", f"must be one of {GRAIN_LIST}, got ['day']")]),
    ({"metrics": [_entry(min=None)]}, [(".metrics[0]", "has no rule, set min, max or max_change_pct")]),
    ({"metrics": [_entry(min=None, lookback=5)]}, [(".metrics[0]", "has no rule, set min, max or max_change_pct")]),
    ({"metrics": [_entry(min="0")]}, [(".metrics[0].min", "must be a number, got '0'")]),
    ({"metrics": [_entry(min=True)]}, [(".metrics[0].min", "must be a number, got True")]),
    ({"metrics": [_entry(max=float("nan"))]}, [(".metrics[0].max", "must be a number, got nan")]),
    ({"metrics": [_entry(max=float("inf"))]}, [(".metrics[0].max", "must be a number, got inf")]),
    ({"metrics": [_entry(min=10, max=5)]}, [(".metrics[0].min", "must not be more than max, got 10 and 5")]),
    ({"metrics": [_entry(max_change_pct=0)]}, [(".metrics[0].max_change_pct", "must be a number above 0, got 0")]),
    ({"metrics": [_entry(max_change_pct=-5)]}, [(".metrics[0].max_change_pct", "must be a number above 0, got -5")]),
    ({"metrics": [_entry(max_change_pct="50")]},
     [(".metrics[0].max_change_pct", "must be a number above 0, got '50'")]),
    ({"metrics": [_entry(max_change_pct=True)]},
     [(".metrics[0].max_change_pct", "must be a number above 0, got True")]),
    ({"metrics": [_entry(lookback=0)]}, [(".metrics[0].lookback", "must be a whole number from 1, got 0")]),
    ({"metrics": [_entry(lookback=2.5)]}, [(".metrics[0].lookback", "must be a whole number from 1, got 2.5")]),
    ({"metrics": [_entry(lookback=True)]}, [(".metrics[0].lookback", "must be a whole number from 1, got True")]),
    ({"metrics": [_entry(filter={})]},
     [(".metrics[0].filter", "must be an object with a dimension and its value or values, got {}")]),
    ({"metrics": [_entry(filter="Pune")]},
     [(".metrics[0].filter", "must be an object with a dimension and its value or values, got 'Pune'")]),
    ({"metrics": [_entry(filter={"city": []})]}, [(".metrics[0].filter.city", FILTER_VALUE + "[]")]),
    ({"metrics": [_entry(filter={"city": None})]}, [(".metrics[0].filter.city", FILTER_VALUE + "None")]),
    ({"metrics": [_entry(filter={"city": ["Pune", None]})]},
     [(".metrics[0].filter.city", FILTER_VALUE + "['Pune', None]")]),
    ({"metrics": [_entry(filter={"city": {"a": 1}})]}, [(".metrics[0].filter.city", FILTER_VALUE + "{'a': 1}")]),
    ({"metrics": [_entry(filter={"city": float("nan")})]}, [(".metrics[0].filter.city", FILTER_VALUE + "nan")]),
    ({"metrics": [_entry(filter={"": "Pune"})]},
     [(".metrics[0].filter", "has a key that is not the name of a dimension, got ''")]),
    ({"metrics": [_entry(), _entry(grain="week")]}, [(".metrics[1].metric", SHARED_NAME)]),
    ({"metrics": [_entry(), _entry(grain="week", name="revenue")]}, [(".metrics[1].name", SHARED_NAME)]),
    ({"metrics": [_entry(), _entry(metric="orders", name="revenue")]}, [(".metrics[1].name", SHARED_NAME)]),
    ({"metrics": [_entry(), _entry(grain="week", name="weekly"), _entry(filter={"city": "Pune"})]},
     [(".metrics[2].metric", SHARED_NAME)]),
]
"""A block with a bad ``metrics`` section, and the problems that it has, with the paths of the entries."""


# ---------------------------------------------------------------------- the block


def test_a_full_block_is_normalised():
    assert parse_sla(FULL) == Sla(
        freshness=Freshness(warn_after=Duration(6, "hour"), error_after=Duration(26, "hour"), column="ordered_at"),
        volume=Volume(min_rows=1, max_rows=1000000, max_change_pct=50, lookback_runs=7),
        schema=SchemaDrift(on_change="warn", allow=("added_column", "widened_type")),
        metrics=(MetricRule(grain="day", metric="revenue", min=0, max=1000000, max_change_pct=50,
                            filter={"city": "Pune"}, lookback=7),))
    assert parse_sla(FULL).to_dict() == FULL


def test_the_defaults_are_applied_and_a_missing_section_stays_missing():
    sla = parse_sla({"volume": {"max_rows": 10}, "schema": {}})
    assert sla.freshness is None
    assert sla.volume == Volume(max_rows=10, lookback_runs=DEFAULT_LOOKBACK_RUNS) and DEFAULT_LOOKBACK_RUNS == 7
    assert sla.schema == SchemaDrift(on_change="warn", allow=())
    assert sla.metrics == ()
    assert sla.to_dict() == {"volume": {"max_rows": 10, "lookback_runs": 7},
                             "schema": {"on_change": "warn", "allow": []}}


def test_a_block_that_is_taken_out_and_put_back_is_the_same_block():
    for block in (FULL, _block(freshness=None), {"freshness": {"error_after": {"count": 1, "period": "day"}}},
                  {"volume": {"max_change_pct": 12.5}}, {"schema": {"on_change": "ignore"}},
                  {"metrics": [{"metric": "orders", "grain": "week", "max": 9}]}):
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
                                 "error_after": {"count": 61, "period": "minute"}}},
                  {"metrics": [_entry(min=5, max=5)]}, {"metrics": [_entry(min=-10, max=-5, lookback=1)]},
                  {"metrics": [_entry(min=None, max=0.5)]}, {"metrics": [_entry(min=None, max_change_pct=0.1)]},
                  {"metrics": [_entry(filter={"city": "Pune", "store": 12, "open": True, "tax": 0.5,
                                              "kitchen": ["Thai", "Italian"], "floor": [1, 2]})]},
                  {"metrics": [_entry(metric=None, sql="SELECT count(*) FROM orders", name="orders_seen")]},
                  {"metrics": [_entry(), _entry(name="weekly", grain="week"), _entry(metric="orders")]}):
        assert sla_problems(block) == [], block
    for grain in GRAINS:
        assert sla_problems({"metrics": [_entry(grain=grain)]}) == [], grain


def test_a_metric_entry_is_normalised_and_a_query_entry_keeps_its_name():
    sla = parse_sla({"metrics": [
        {"metric": " revenue ", "grain": "week", "min": 0, "filter": {"city": ["Pune", "Delhi"]}},
        {"name": "late_orders", "sql": "  SELECT count(*) FROM orders  ", "grain": "day", "max": 9,
         "max_change_pct": 12.5, "lookback": 4}]})
    metric, query = sla.metrics
    assert metric == MetricRule(grain="week", metric="revenue", min=0, filter={"city": ["Pune", "Delhi"]},
                                lookback=DEFAULT_LOOKBACK) and DEFAULT_LOOKBACK == 7
    assert (metric.key, metric.sql, metric.name) == ("revenue", None, None)
    assert query == MetricRule(grain="day", sql="SELECT count(*) FROM orders", name="late_orders", max=9,
                               max_change_pct=12.5, lookback=4)
    assert (query.key, query.metric) == ("late_orders", None)
    assert sla.to_dict()["metrics"][0] == {"metric": "revenue", "grain": "week", "min": 0,
                                           "filter": {"city": ["Pune", "Delhi"]}, "lookback": 7}


def test_the_filter_of_a_rule_is_a_copy_of_the_block():
    block = {"metrics": [_entry(filter={"city": ["Pune"]})]}
    rule = parse_sla(block).metrics[0]
    block["metrics"][0]["filter"]["city"].append("Delhi")
    assert rule.filter == {"city": ["Pune"]}
    rule.to_dict()["filter"]["city"].append("Goa")
    assert rule.filter == {"city": ["Pune"]}
    assert hash(rule) == hash(parse_sla({"metrics": [_entry(filter={"city": ["Pune"]})]}).metrics[0])


def test_the_definition_follows_what_is_measured_and_not_the_limits_or_the_name():
    base = MetricRule(grain="day", metric="revenue", min=0, max_change_pct=50)
    for same in (MetricRule(grain="day", metric="revenue", min=5, max=9, name="sales", lookback=3),
                 MetricRule(grain="day", metric="revenue", max_change_pct=10)):
        assert same.definition() == base.definition()
    other = [MetricRule(grain="week", metric="revenue", min=0), MetricRule(grain="day", metric="orders", min=0),
             MetricRule(grain="day", metric="revenue", min=0, filter={"city": "Pune"}),
             MetricRule(grain="day", sql="SELECT 1", name="revenue", min=0)]
    assert len({base.definition(), *(rule.definition() for rule in other)}) == 5
    assert len(base.definition()) == 12
    one_query = MetricRule(grain="day", sql="SELECT 1", name="count", min=0)
    assert one_query.definition() != MetricRule(grain="day", sql="SELECT 2", name="count", min=0).definition()
    assert one_query.definition() == MetricRule(grain="day", sql="SELECT 1", name="other", max=3).definition()
    one = MetricRule(grain="day", metric="revenue", min=0, filter={"city": "Pune", "kitchen": "Thai"})
    two = MetricRule(grain="day", metric="revenue", min=0, filter={"kitchen": "Thai", "city": "Pune"})
    assert one.definition() == two.definition()


def test_the_summaries_of_a_rule_say_the_limits_in_a_few_words():
    rule = parse_sla({"metrics": [_entry(grain="week", max=5000, max_change_pct=50, lookback=4)]}).metrics[0]
    assert (rule.value_summary(), rule.change_summary(), rule.summary()) == (
        "min 0 and max 5000", "change over 50 percent of the last 4 weeks", "revenue by week")
    single = parse_sla({"metrics": [{"metric": "revenue", "grain": "month", "max_change_pct": 12.5,
                                     "lookback": 1}]}).metrics[0]
    assert (single.value_summary(), single.change_summary()) == ("", "change over 12.5 percent of the last 1 month")
    only_max = parse_sla({"metrics": [{"metric": "revenue", "grain": "year", "max": 2.5}]}).metrics[0]
    assert (only_max.value_summary(), only_max.change_summary()) == ("max 2.5", "")
    named = parse_sla({"metrics": [_entry(), {"name": "late_orders", "sql": "SELECT 1", "grain": "day", "max": 1}]})
    assert named.summary() == "metrics revenue by day and late_orders by day"


@pytest.mark.parametrize("value, shown", [(5, "5"), (0, "0"), (-3, "-3"), (2.5, "2.5"), (100.0, "100"),
                                          (1234567.89, "1234567.89"), (0.1 + 0.2, "0.3"), (1e-7, "1e-07"),
                                          (1000000, "1000000")])
def test_a_figure_shows_a_whole_number_as_it_is_and_any_other_with_at_most_10_digits(value, shown):
    assert figure(value) == shown


def test_the_grains_and_the_name_pattern_are_those_of_the_metrics_package():
    from local_data_platform.metrics import model

    assert GRAINS == model.GRAINS
    assert NAME_PATTERN == model.NAME_PATTERN


def test_the_json_schema_names_the_keys_the_grains_and_the_default_of_an_entry():
    items = sla_schema()["properties"]["metrics"]["items"]
    assert tuple(items["properties"]) == METRIC_KEYS
    assert items["properties"]["grain"]["enum"] == list(GRAINS)
    assert items["properties"]["lookback"]["default"] == DEFAULT_LOOKBACK == 7
    assert items["required"] == ["grain"] and sla_schema()["properties"]["metrics"]["minItems"] == 1


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
        "schema warn except added_column and widened_type, metrics revenue by day")
    assert parse_sla({"volume": {"min_rows": 2, "max_change_pct": 12.5, "lookback_runs": 1}}).summary() == \
        "volume min 2 rows and change over 12.5 percent of the last 1 run"
    assert parse_sla({"volume": {"max_change_pct": 50.0}}).summary() == \
        "volume change over 50 percent of the last 7 runs"
    assert parse_sla({"schema": {"on_change": "error"}}).summary() == "schema error"


def test_the_summary_of_the_volume_has_a_part_for_the_rows_and_a_part_for_the_change():
    volume = parse_sla({"volume": {"min_rows": 1, "max_rows": 9, "max_change_pct": 50, "lookback_runs": 4}}).volume
    assert volume.rows_summary() == "min 1 row and max 9 rows"
    assert volume.change_summary() == "change over 50 percent of the last 4 runs"
    assert volume.summary() == "min 1 row and max 9 rows and change over 50 percent of the last 4 runs"
    rows_only = parse_sla({"volume": {"min_rows": 5}}).volume
    assert (rows_only.rows_summary(), rows_only.change_summary(), rows_only.summary()) == (
        "min 5 rows", "", "min 5 rows")
    change_only = parse_sla({"volume": {"max_change_pct": 25}}).volume
    assert (change_only.rows_summary(), change_only.change_summary()) == (
        "", "change over 25 percent of the last 7 runs")


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
    ({}, [("", "has nothing to check, set at least one of ['freshness', 'volume', 'schema', 'metrics']")]),
    ({"alerts": []}, [("", "has unknown keys ['alerts']; allowed: ['freshness', 'volume', 'schema', 'metrics']")]),
    ("daily", [("", "must be an object with some of ['freshness', 'volume', 'schema', 'metrics'], got 'daily'")]),
    (["freshness"],
     [("", "must be an object with some of ['freshness', 'volume', 'schema', 'metrics'], got ['freshness']")]),
] + METRIC_PROBLEMS)
def test_every_malformed_value_is_reported_with_its_path(block, expected):
    assert sla_problems(block) == expected


def test_unknown_keys_are_errors_at_every_level():
    block = {"alerts": [],
             "freshness": {"error_after": {"count": 1, "period": "day", "hours": 3}, "max_age": 1},
             "volume": {"min_rows": 1, "max_rows_pct": 3}, "schema": {"strict": True},
             "metrics": [{"metric": "revenue", "grain": "day", "min": 0, "limit": 3}]}
    assert sla_problems(block) == [
        ("", "has unknown keys ['alerts']; allowed: ['freshness', 'volume', 'schema', 'metrics']"),
        (".freshness", "has unknown keys ['max_age']; allowed: ['warn_after', 'error_after', 'column']"),
        (".freshness.error_after", "has unknown keys ['hours']; allowed: ['count', 'period']"),
        (".volume", "has unknown keys ['max_rows_pct']; allowed: ['min_rows', 'max_rows', 'max_change_pct', "
                    "'lookback_runs']"),
        (".schema", "has unknown keys ['strict']; allowed: ['on_change', 'allow']"),
        (".metrics[0]", "has unknown keys ['limit']; allowed: ['name', 'metric', 'sql', 'grain', 'min', 'max', "
                        "'max_change_pct', 'filter', 'lookback']"),
    ]


def test_every_problem_is_reported_and_not_only_the_first():
    block = {"freshness": {"error_after": {"count": 0, "period": "week"}}, "volume": {"min_rows": 0},
             "schema": {"on_change": "fail", "allow": ["nope"]},
             "metrics": [{"metric": "revenue", "grain": "hour", "min": "0"}]}
    paths = [path for path, _ in sla_problems(block)]
    assert paths == [".freshness.error_after.count", ".freshness.error_after.period", ".volume.min_rows",
                     ".schema.on_change", ".schema.allow[0]", ".metrics[0].grain", ".metrics[0].min"]


def test_junk_gives_problems_and_never_an_exception():
    junk = [1, 2.5, True, "", [], [1], {"freshness": None}, {"freshness": {"warn_after": None}},
            {"freshness": {"warn_after": [], "error_after": 3}}, {"volume": {"min_rows": [1], "max_change_pct": {}}},
            {"schema": {"on_change": [], "allow": [[], {}, None]}},
            {"freshness": {"error_after": {"count": [], "period": {}}}},
            {"metrics": None}, {"metrics": {}}, {"metrics": [None]}, {"metrics": [[]]}, {"metrics": [{}]},
            {"metrics": [{"metric": [], "grain": {}, "min": [], "filter": 1, "name": 3}]},
            {"metrics": [{"sql": {}, "grain": [], "max": {}, "filter": [], "lookback": []}]}]
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
    with pytest.raises(ConfigError, match=r"^metadata\.sla: has unknown keys \['alerts'\]"):
        Config.from_dict(_config({"alerts": []}), base_dir=tmp_path)
    with pytest.raises(ConfigError, match=r"^metadata\.sla\.metrics\[0\]\.grain: is required"):
        Config.from_dict(_config({"metrics": [{"metric": "revenue", "min": 0}]}), base_dir=tmp_path)
