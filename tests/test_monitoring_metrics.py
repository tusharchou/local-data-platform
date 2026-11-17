"""The metric monitors: ``metadata.sla.metrics``, the number of the latest complete period and its history."""

import builtins
import copy
import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import Config, metrics
from local_data_platform.catalog.provider import create_catalog
from local_data_platform.cli import main
from local_data_platform.events import MONITOR_TABLE
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.monitoring import MonitorResult, evaluate
from local_data_platform.monitoring.history import JSONL_PATH, read_jsonl, read_results, record
from local_data_platform.monitoring.metric import (
    MIN_EARLIER_VALUES,
    QUERY_NEEDS_NO_METRICS_FILE,
    _label,
    _why,
    folder_problems,
    latest_period,
    monitor_name,
)
from local_data_platform.monitoring.sla import MetricRule
from local_data_platform.spec import plan

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 28, 6, 0, 0, tzinfo=UTC)
"""The moment of most tests, so the latest complete day is 2026-10-27."""
CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}
SCHEMA = pa.schema([("order_date", pa.date32()), ("city", pa.string()), ("cuisine", pa.string()),
                    ("orders", pa.int64()), ("revenue", pa.decimal128(18, 2)), ("rating", pa.float64())])
FIRST_DAY = dt.date(2025, 12, 1)
LAST_DAY = dt.date(2026, 10, 27)
DAYS = (LAST_DAY - FIRST_DAY).days + 1
REVENUE = {"metric": "revenue", "grain": "day", "min": 0, "max_change_pct": 50}
MODEL = {
    "name": "daily_sales", "table": "gold.gold_sales", "description": "Sales by day.",
    "time_dimension": {"column": "order_date", "grains": ["day", "week", "month", "quarter", "year"]},
    "dimensions": ["city", {"name": "kitchen", "column": "cuisine"}],
    "measures": [{"name": "revenue_sum", "agg": "sum", "expr": "revenue"},
                 {"name": "order_sum", "agg": "sum", "expr": "orders"},
                 {"name": "rating_avg", "agg": "avg", "expr": "rating"}],
}
DEFINITIONS = {
    "apiVersion": "ldp/v1", "kind": "Metrics", "identifier": "sales_metrics", "who": "analytics@example.com",
    "semantic_models": [MODEL],
    "metrics": [{"name": "revenue", "type": "simple", "measure": "revenue_sum", "format": "currency",
                 "description": "Money taken."},
                {"name": "orders", "type": "simple", "measure": "order_sum", "description": "Orders taken."},
                {"name": "avg_ticket", "type": "ratio", "numerator": "revenue_sum", "denominator": "order_sum",
                 "description": "Revenue divided by orders."},
                {"name": "avg_rating", "type": "simple", "measure": "rating_avg", "description": "Mean rating."}],
}
QUERY = {"name": "orders_seen", "sql": "SELECT count(*) FROM gold_sales WHERE order_date = DATE '2026-10-27'",
         "grain": "day", "min": 1, "max_change_pct": 50}
REFUSED = "only read-only SELECT or WITH queries are allowed; this statement starts with 'DELETE'"
ENGINE_WARNING = "WARNING local_data_platform.engine.duckdb: Registering Iceberg tables in memory"
"""What the engine says on stderr when DuckDB has no iceberg extension, as in an environment with an empty HOME."""


def _rows(last: float = 1.0) -> list[dict]:
    """Two rows for each day from 2025-12-01 to 2026-10-27 that add up to 100.00 and 10 orders, the last day scaled."""
    rows, day = [], FIRST_DAY
    while day <= LAST_DAY:
        scale = last if day == LAST_DAY else 1.0
        rows.append({"order_date": day, "city": "Pune", "cuisine": "Thai", "orders": 6,
                     "revenue": (Decimal("60.00") * Decimal(str(scale))).quantize(Decimal("0.01")), "rating": 4.0})
        rows.append({"order_date": day, "city": "Delhi", "cuisine": "Italian", "orders": 4,
                     "revenue": (Decimal("40.00") * Decimal(str(scale))).quantize(Decimal("0.01")), "rating": None})
        day += dt.timedelta(days=1)
    return rows


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _lake(tmp_path: Path, entries="default", *, rows=None, definitions="default", table: bool = True, sinks=None,
          sla_extra=None) -> Config:
    """A folder with the gold table ``gold.gold_sales``, its config that carries the SLA, and a metrics file."""
    if table:
        Iceberg("gold_sales", {"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")}).put(
            pa.Table.from_pylist(_rows() if rows is None else rows, schema=SCHEMA))
    metadata = {"source": {"name": "gold_sales", "format": "ICEBERG", "catalog": CATALOG},
                "target": {"name": "gold_sales", "format": "ICEBERG", "catalog": CATALOG},
                "docs": {"layer": "gold", "description": "Sales by day and city.", "grain": "one row per day and city",
                         "columns": {column: f"The {column}." for column in SCHEMA.names}}}
    sla = dict(sla_extra or {})
    if entries is not None:
        sla["metrics"] = [dict(REVENUE)] if entries == "default" else entries
    if sla:
        metadata["sla"] = sla
    if sinks:
        metadata["observability"] = {"sinks": sinks}
    _write(tmp_path / "gold_sales.json", {"identifier": "gold_sales", "who": "ops", "metadata": metadata})
    if definitions is not None:
        _write(tmp_path / "metrics.json", DEFINITIONS if definitions == "default" else definitions)
    return Config.from_json(tmp_path / "gold_sales.json")


def _by_name(config: Config, now: dt.datetime = NOW) -> dict[str, MonitorResult]:
    return {result.monitor: result for result in evaluate(config, now)}


def _check(tmp_path: Path, entries="default", now: dt.datetime = NOW, **options) -> dict[str, MonitorResult]:
    """The results of the entries over a lake, by the name of the monitor."""
    return _by_name(_lake(tmp_path, entries, **options), now)


def _seed(config: Config, values, *, first: dt.date = dt.date(2026, 10, 20), index: int = 0, part: str = "change",
          status: str = "ok", hour: int = 6, later: int = 1, monitor: str | None = None, **overrides) -> None:
    """Record the looks of earlier days, one for each value, as ``ldp monitor`` left them in its history.

    The days start at ``first``, and a look is made at ``hour`` o'clock UTC, ``later`` days after its period. A look is
    of the monitor ``monitor``, and by default of the one of the entry at ``index``.
    """
    rule = config.sla.metrics[index]
    found = []
    for number, value in enumerate(values):
        period = first + dt.timedelta(days=number)
        look = {"value": value, "period": period.isoformat(), "grain": rule.grain, "definition": rule.definition()}
        found.append(MonitorResult(
            monitor=monitor or monitor_name(rule, part), kind="metric", status=status, pipeline=config.identifier,
            evaluated_at=dt.datetime.combine(period + dt.timedelta(days=later), dt.time(hour), tzinfo=UTC),
            metrics={**look, **overrides}))
    record([(config, found)])


def _change(tmp_path: Path, values, entries="default", **options) -> MonitorResult:
    """The change result for the latest day over a history of earlier days that had ``values``."""
    config = _lake(tmp_path, entries, **options)
    _seed(config, values)
    return _by_name(config)["metric.revenue.change"]


def _ldp(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _refuse_duckdb(monkeypatch) -> None:
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("no duckdb here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)


def _snapshots(tmp_path: Path, name: str = "gold.gold_sales") -> list[int]:
    catalog = create_catalog({"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")})
    return [item.snapshot_id for item in catalog.load_table(name).snapshots()]


# ---------------------------------------------------------------------- the period


@pytest.mark.parametrize("now, grain, first, last", [
    ("2026-10-28T06:00:00Z", "day", "2026-10-27", "2026-10-27"),
    ("2026-10-28T00:00:00Z", "day", "2026-10-27", "2026-10-27"),
    ("2026-10-27T23:59:59Z", "day", "2026-10-26", "2026-10-26"),
    ("2026-10-28T01:00:00+05:30", "day", "2026-10-26", "2026-10-26"),
    ("2026-10-28T23:00:00-05:00", "day", "2026-10-28", "2026-10-28"),
    ("2026-03-01T10:00:00Z", "day", "2026-02-28", "2026-02-28"),
    ("2026-10-28T06:00:00Z", "week", "2026-10-19", "2026-10-25"),
    ("2026-10-26T00:00:00Z", "week", "2026-10-19", "2026-10-25"),
    ("2026-10-25T23:00:00Z", "week", "2026-10-12", "2026-10-18"),
    ("2026-01-01T09:00:00Z", "week", "2025-12-22", "2025-12-28"),
    ("2026-10-28T06:00:00Z", "month", "2026-09-01", "2026-09-30"),
    ("2026-01-01T00:00:00Z", "month", "2025-12-01", "2025-12-31"),
    ("2026-03-01T00:00:00Z", "month", "2026-02-01", "2026-02-28"),
    ("2028-03-01T00:00:00Z", "month", "2028-02-01", "2028-02-29"),
    ("2026-10-28T06:00:00Z", "quarter", "2026-07-01", "2026-09-30"),
    ("2026-01-15T06:00:00Z", "quarter", "2025-10-01", "2025-12-31"),
    ("2026-04-01T00:00:00Z", "quarter", "2026-01-01", "2026-03-31"),
    ("2026-07-01T00:00:00Z", "quarter", "2026-04-01", "2026-06-30"),
    ("2026-12-31T23:59:59Z", "quarter", "2026-07-01", "2026-09-30"),
    ("2026-10-28T06:00:00Z", "year", "2025-01-01", "2025-12-31"),
    ("2026-01-01T00:00:00Z", "year", "2025-01-01", "2025-12-31"),
    ("2026-12-31T23:59:59Z", "year", "2025-01-01", "2025-12-31"),
])
def test_the_latest_complete_period_is_the_one_before_the_one_that_now_is_in_counted_in_utc(now, grain, first, last):
    moment = dt.datetime.fromisoformat(now.replace("Z", "+00:00"))
    assert latest_period(moment, grain) == (dt.date.fromisoformat(first), dt.date.fromisoformat(last))


@pytest.mark.parametrize("grain, day, label", [
    ("day", dt.date(2026, 3, 9), "2026-03-09"), ("week", dt.date(2026, 3, 9), "2026-03-09"),
    ("month", dt.date(2026, 3, 9), "2026-03"), ("month", dt.date(2026, 11, 1), "2026-11"),
    ("quarter", dt.date(2026, 1, 1), "2026-Q1"), ("quarter", dt.date(2026, 3, 31), "2026-Q1"),
    ("quarter", dt.date(2026, 4, 1), "2026-Q2"), ("quarter", dt.date(2026, 9, 30), "2026-Q3"),
    ("quarter", dt.date(2026, 12, 1), "2026-Q4"), ("year", dt.date(2025, 6, 1), "2025")])
def test_a_period_is_named_by_its_first_day_in_the_words_of_its_grain(grain, day, label):
    assert _label(grain, day) == label


# ---------------------------------------------------------------------- the value


def test_the_value_of_a_normal_day_is_inside_the_limits_and_the_result_says_what_it_saw(tmp_path):
    results = _check(tmp_path, [{"metric": "revenue", "grain": "day", "min": 0, "max": 500}])
    assert list(results) == ["metric.revenue.value"]
    value = results["metric.revenue.value"]
    assert (value.status, value.kind, value.observed, value.expected) == ("ok", "metric", "100", "min 0 and max 500")
    assert value.details == "revenue for day 2026-10-27 was 100, inside the limits"
    assert value.metrics == {
        "grain": "day", "period": "2026-10-27", "definition": value.metrics["definition"], "table": "gold.gold_sales",
        "read": "the tables as they are now, because no release has been cut yet", "value": 100.0, "min": 0,
        "max": 500}
    assert (value.pipeline, value.table_identifier, value.evaluated_at) == ("gold_sales", "gold.gold_sales", NOW)
    assert value.table_uuid


@pytest.mark.parametrize("entry, status, details", [
    ({"min": 100}, "ok", "inside the limits"),
    ({"max": 100}, "ok", "inside the limits"),
    ({"min": 100.01}, "error", "below the 100.01 that min asks for"),
    ({"max": 99.99}, "error", "above the 99.99 that max allows"),
    ({"min": 0, "max": 1000}, "ok", "inside the limits"),
    ({"min": 150, "max": 200}, "error", "below the 150 that min asks for"),
    ({"min": -50, "max": -10}, "error", "above the -10 that max allows"),
])
def test_the_limits_on_the_value_hold_at_the_edge(tmp_path, entry, status, details):
    result = _check(tmp_path, [{"metric": "revenue", "grain": "day", **entry}])["metric.revenue.value"]
    assert result.status == status
    assert result.details == f"revenue for day 2026-10-27 was 100, {details}"


def test_a_section_with_one_rule_makes_one_result_and_two_rules_make_two(tmp_path):
    only_change = _check(tmp_path / "a", [{"metric": "revenue", "grain": "day", "max_change_pct": 50}])
    assert list(only_change) == ["metric.revenue.change"]
    both = _check(tmp_path / "b", [REVENUE, {"metric": "orders", "grain": "day", "max": 99}])
    assert list(both) == ["metric.revenue.value", "metric.revenue.change", "metric.orders.value"]


def test_the_number_is_the_one_that_the_metrics_package_gives_the_reports(tmp_path):
    config = _lake(tmp_path, [{"metric": "avg_ticket", "grain": "week", "min": 0}])
    shown = metrics.query(tmp_path, "avg_ticket", grain="week", start="2026-10-19", end="2026-10-25")
    result = evaluate(config, NOW)[0]
    assert shown.num_rows == 1 and result.metrics["value"] == pytest.approx(shown.column("avg_ticket")[0].as_py())
    assert result.metrics["value"] == pytest.approx(10.0)
    assert result.details == "avg_ticket for week 2026-10-19 was 10, inside the limits"


@pytest.mark.parametrize("grain, label, first, value", [
    ("day", "day 2026-10-27", "2026-10-27", 100), ("week", "week 2026-10-19", "2026-10-19", 700),
    ("month", "month 2026-09", "2026-09-01", 3000), ("quarter", "quarter 2026-Q3", "2026-07-01", 9200),
    ("year", "year 2025", "2025-01-01", 3100)])
def test_each_grain_adds_up_its_own_period_and_names_it(tmp_path, grain, label, first, value):
    result = _check(tmp_path, [{"metric": "revenue", "grain": grain, "min": 0}])["metric.revenue.value"]
    assert (result.status, result.observed) == ("ok", str(value))
    assert result.details == f"revenue for {label} was {value}, inside the limits"
    assert (result.metrics["grain"], result.metrics["period"]) == (grain, first)


def test_the_number_is_read_from_the_current_release_and_from_the_tables_as_they_are_when_there_is_none(tmp_path):
    config = _lake(tmp_path, [{"metric": "revenue", "grain": "day", "min": 0}])
    catalog = create_catalog({"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")})
    table = catalog.load_table("gold.gold_sales")
    release = {"ldp_release": 1, "release": "r1", "number": 1, "namespace": "gold",
               "created_at": "2026-10-28T02:00:00+00:00",
               "tables": [{"table": "gold.gold_sales", "config": "gold_sales.json", "status": "checked",
                           "snapshot_id": table.current_snapshot().snapshot_id, "rows": DAYS * 2,
                           "dataset": "gold.gold_sales", "dataset_version": 1, "run_id": None}]}
    path = _write(tmp_path / "warehouse" / ".ldp" / "releases" / "gold" / "r1.json", release)
    table.append(pa.Table.from_pylist(
        [{"order_date": LAST_DAY, "city": "Goa", "cuisine": "Thai", "orders": 1, "revenue": Decimal("500.00"),
          "rating": 3.0}], schema=SCHEMA))
    released = evaluate(config, NOW)[0]
    assert (released.observed, released.metrics["value"]) == ("100", 100.0)
    assert released.metrics["read"] == "release r1, cut 2026-10-28 02:00:00 UTC"
    path.unlink()
    live = evaluate(config, NOW)[0]
    assert (live.observed, live.metrics["value"]) == ("600", 600.0)
    assert live.metrics["read"] == "the tables as they are now, because no release has been cut yet"


def test_a_filter_narrows_the_number_and_its_values_are_not_kept(tmp_path):
    results = _check(tmp_path, [
        {"metric": "revenue", "grain": "day", "min": 0, "filter": {"city": "Pune"}, "name": "pune"},
        {"metric": "revenue", "grain": "day", "min": 0, "filter": {"city": ["Pune", "Delhi"]}, "name": "both"},
        {"metric": "revenue", "grain": "day", "min": 0, "filter": {"city": "Delhi", "kitchen": "Italian"},
         "name": "delhi"}])
    assert [results[f"metric.{name}.value"].observed for name in ("pune", "both", "delhi")] == ["60", "100", "40"]
    kept = json.dumps([result.to_dict() for result in results.values()])
    assert "Pune" not in kept and "Delhi" not in kept and "Italian" not in kept


def test_a_value_that_is_a_decimal_is_kept_as_a_number_that_json_can_hold(tmp_path):
    result = _check(tmp_path, [REVENUE], rows=_rows(last=0.4256))["metric.revenue.value"]
    assert result.metrics["value"] == pytest.approx(42.56) and isinstance(result.metrics["value"], float)
    assert json.loads(json.dumps(result.to_dict()))["metrics"]["value"] == pytest.approx(42.56)
    assert result.observed == "42.56"


def test_a_day_with_no_rows_is_unknown_and_has_no_value(tmp_path):
    results = _check(tmp_path, [REVENUE], now=NOW + dt.timedelta(days=1))
    assert set(results) == {"metric.revenue.value", "metric.revenue.change"}
    for result in results.values():
        assert (result.status, result.observed) == ("unknown", None)
        assert result.details == "gold.gold_sales has no rows in that day, so there is no value"
        assert "value" not in result.metrics and result.metrics["period"] == "2026-10-28"


def test_a_value_that_is_null_is_unknown(tmp_path):
    rows = [row for row in _rows() if row["city"] == "Delhi"]
    result = _check(tmp_path, [{"metric": "avg_rating", "grain": "day", "min": 1}], rows=rows)[
        "metric.avg_rating.value"]
    assert (result.status, result.details) == ("unknown", "the value is NULL, so there is no number to check")
    assert "value" not in result.metrics


# ---------------------------------------------------------------------- the change


def test_a_fall_of_60_percent_against_the_median_is_an_error_that_says_how_far_it_fell(tmp_path):
    change = _change(tmp_path, [100] * 6, rows=_rows(last=0.4))
    assert (change.status, change.observed, change.kind) == ("error", "-60.0 percent", "metric")
    assert change.expected == "change over 50 percent of the last 7 days"
    assert change.details == ("revenue for day 2026-10-27 was 40, 60.0 percent lower than the median of the 6 earlier "
                              "days, 100")
    assert change.metrics == {
        "grain": "day", "period": "2026-10-27", "definition": change.metrics["definition"], "table": "gold.gold_sales",
        "read": change.metrics["read"], "value": 40.0, "max_change_pct": 50, "lookback": 7, "earlier_values": 6,
        "baseline_median": 100, "change_pct": -60.0}


@pytest.mark.parametrize("last, status, observed", [
    (1.5, "ok", "+50.0 percent"), (1.51, "error", "+51.0 percent"), (0.5, "ok", "-50.0 percent"),
    (0.49, "error", "-51.0 percent"), (0.1, "error", "-90.0 percent"), (3.0, "error", "+200.0 percent"),
    (1.0, "ok", "+0.0 percent")])
def test_a_day_is_off_when_it_is_further_from_the_median_than_the_limit(tmp_path, last, status, observed):
    change = _change(tmp_path, [100] * 6, rows=_rows(last=last))
    assert (change.status, change.observed) == (status, observed)


def test_a_spike_is_an_error_too_and_says_that_it_rose(tmp_path):
    change = _change(tmp_path, [100] * 6, rows=_rows(last=3.0))
    assert change.details == ("revenue for day 2026-10-27 was 300, 200.0 percent higher than the median of the 6 "
                              "earlier days, 100")


def test_a_day_that_is_the_same_as_the_median_says_so(tmp_path):
    change = _change(tmp_path, [100] * 6)
    assert (change.status, change.observed) == ("ok", "+0.0 percent")
    assert change.details == "revenue for day 2026-10-27 was 100, the same as the median of the 6 earlier days, 100"


@pytest.mark.parametrize("values", [[], [100], [100, 100]])
def test_with_fewer_than_3_earlier_values_the_change_is_unknown_and_never_an_alert(tmp_path, values):
    change = _change(tmp_path, values, rows=_rows(last=0.01))
    assert MIN_EARLIER_VALUES == 3
    assert (change.status, change.observed, change.details) == ("unknown", None, "not enough history")
    assert change.metrics["earlier_values"] == len(values) and change.metrics["needed_values"] == 3
    assert change.metrics["value"] == pytest.approx(1.0)


def test_with_3_earlier_values_the_change_reports(tmp_path):
    change = _change(tmp_path, [100, 100, 100], rows=_rows(last=0.01))
    assert (change.status, change.observed) == ("error", "-99.0 percent")
    assert change.details.endswith("the median of the 3 earlier days, 100")


def test_the_median_is_taken_over_the_lookback_and_over_the_earlier_days_only(tmp_path):
    values = [10, 10, 10, 100, 100, 100]
    recent = _change(tmp_path / "a", values, [{**REVENUE, "lookback": 4}])
    assert (recent.status, recent.observed) == ("ok", "+0.0 percent")
    assert recent.metrics["earlier_values"] == 3 and recent.expected == "change over 50 percent of the last 4 days"
    wide = _change(tmp_path / "b", values)
    assert (wide.status, wide.observed) == ("error", "+81.8 percent")
    assert wide.metrics["earlier_values"] == 6 and wide.metrics["baseline_median"] == 55


@pytest.mark.parametrize("lookback", [1, 2, 3])
def test_a_lookback_of_3_or_less_leaves_too_few_earlier_values_and_never_reports(tmp_path, lookback):
    change = _change(tmp_path, [100] * 6, [{**REVENUE, "lookback": lookback}], rows=_rows(last=0.01))
    assert (change.status, change.details) == ("unknown", "not enough history")
    assert change.metrics["earlier_values"] == lookback - 1


def test_one_value_counts_for_each_earlier_day_whatever_the_number_of_looks_and_the_last_look_wins(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [100, 100, 100])  # the days 20, 21 and 22
    _seed(config, [1, 999], hour=9, status="error")  # later looks of the days 20 and 21
    change = _by_name(config)["metric.revenue.change"]
    assert change.metrics["earlier_values"] == 3 and change.metrics["baseline_median"] == 100
    _seed(config, [5], first=dt.date(2026, 10, 20), hour=12)  # a later look still, of the day 20
    assert _by_name(config)["metric.revenue.change"].metrics["baseline_median"] == 100
    _seed(config, [100, 100, 100], first=dt.date(2026, 10, 23))
    again = _by_name(config)["metric.revenue.change"]
    assert again.metrics["earlier_values"] == 6 and again.metrics["baseline_median"] == 100


def test_the_last_look_of_a_day_is_the_value_that_counts(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [10, 20, 30, 40], first=dt.date(2026, 10, 20))
    _seed(config, [11, 22], first=dt.date(2026, 10, 20), hour=9)
    assert _by_name(config)["metric.revenue.change"].metrics["baseline_median"] == 26  # 11, 22, 30 and 40


def test_looks_of_the_period_that_is_measured_are_not_earlier_values(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [100, 100, 100, 100, 100, 500], first=dt.date(2026, 10, 22))  # the days 22 to 27
    change = _by_name(config)["metric.revenue.change"]
    assert change.metrics["earlier_values"] == 5 and change.metrics["baseline_median"] == 100
    assert change.status == "ok"


def test_only_looks_of_the_same_metric_grain_and_filter_are_the_history_of_an_entry(tmp_path):
    config = _lake(tmp_path, [{**REVENUE, "filter": {"city": "Pune"}}])
    for other in (MetricRule(grain="week", metric="revenue", min=0, filter={"city": "Pune"}),
                  MetricRule(grain="day", metric="orders", min=0, filter={"city": "Pune"}),
                  MetricRule(grain="day", metric="revenue", min=0, filter={"city": "Delhi"}),
                  MetricRule(grain="day", metric="revenue", min=0)):
        _seed(config, [60] * 6, definition=other.definition())
    change = _by_name(config)["metric.revenue.change"]
    assert (change.status, change.details) == ("unknown", "not enough history")
    assert change.metrics["earlier_values"] == 0
    _seed(config, [60] * 4)
    assert _by_name(config)["metric.revenue.change"].status == "ok"


def test_an_entry_that_is_renamed_keeps_its_baseline_and_two_entries_of_one_definition_share_it(tmp_path):
    config = _lake(tmp_path, [{**REVENUE, "name": "takings"}, {**REVENUE, "name": "sales", "max_change_pct": 10}])
    _seed(config, [100] * 6, monitor="metric.revenue.change")  # the looks that the entry had before it had a name
    results = _by_name(config)
    assert results["metric.takings.change"].metrics["earlier_values"] == 6
    assert results["metric.sales.change"].metrics["earlier_values"] == 6
    assert results["metric.takings.change"].status == results["metric.sales.change"].status == "ok"


def test_a_metric_and_a_query_side_by_side_in_a_folder_with_a_metrics_file_the_query_is_refused(tmp_path):
    results = evaluate(_lake(tmp_path, [QUERY, REVENUE]), NOW)
    assert [result.monitor for result in results] == [
        "metric.orders_seen.value", "metric.orders_seen.change", "metric.revenue.value", "metric.revenue.change"]
    assert [result.status for result in results] == ["error", "error", "ok", "unknown"]


def test_an_entry_with_no_change_rule_does_not_stop_another_entry_from_reading_the_history(tmp_path):
    config = _lake(tmp_path, [{"metric": "orders", "grain": "day", "min": 0, "name": "orders_seen"}, REVENUE])
    _seed(config, [100] * 6, index=1)
    results = _by_name(config)
    assert list(results) == ["metric.orders_seen.value", "metric.revenue.value", "metric.revenue.change"]
    assert results["metric.revenue.change"].status == "ok"
    assert results["metric.revenue.change"].metrics["earlier_values"] == 6


def test_a_look_that_was_made_after_now_is_not_history(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [10, 20, 30, 40, 50, 60], first=dt.date(2026, 10, 20))  # the days 20 to 25
    _seed(config, [9999], first=dt.date(2026, 10, 22), later=4, hour=12)  # the day 22 again, on the 26th
    back = _by_name(config, dt.datetime(2026, 10, 25, 6, 0, tzinfo=UTC))["metric.revenue.change"]
    assert back.metrics["period"] == "2026-10-24" and back.metrics["earlier_values"] == 4
    assert back.metrics["baseline_median"] == 25  # 10, 20, 30 and 40, and not the look of the 26th
    now = _by_name(config, NOW)["metric.revenue.change"]
    assert now.metrics["earlier_values"] == 6 and now.metrics["baseline_median"] == 45  # 10, 20, 9999, 40, 50, 60
    early = _by_name(config, dt.datetime(2026, 10, 20, 12, 0, tzinfo=UTC))["metric.revenue.change"]
    assert early.metrics["earlier_values"] == 0


def test_a_look_that_was_made_at_exactly_now_is_history(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [100, 100])  # the days 20 and 21
    _seed(config, [100], first=dt.date(2026, 10, 25), later=3)  # the day 25, looked at on the 28th at 06:00, now
    change = _by_name(config)["metric.revenue.change"]
    assert change.metrics["earlier_values"] == 3 and change.status == "ok"


def test_the_values_of_the_value_monitor_are_history_too(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [100] * 5, part="value")
    change = _by_name(config)["metric.revenue.change"]
    assert (change.status, change.metrics["earlier_values"]) == ("ok", 5)


def test_a_look_that_broke_a_limit_or_was_unknown_still_has_its_value_in_the_baseline(tmp_path):
    config = _lake(tmp_path)
    _seed(config, [100, 100], status="error")
    _seed(config, [100], first=dt.date(2026, 10, 22), status="unknown")
    change = _by_name(config)["metric.revenue.change"]
    assert change.metrics["earlier_values"] == 3 and change.status == "ok"


@pytest.mark.parametrize("look", [{"value": None}, {"value": "100"}, {"value": True}, {"period": None},
                                  {"period": 5}, {"definition": None}])
def test_a_look_without_a_usable_value_period_or_definition_is_not_history(tmp_path, look):
    config = _lake(tmp_path)
    _seed(config, [100] * 3, **look)
    change = _by_name(config)["metric.revenue.change"]
    assert change.details == "not enough history" and change.metrics["earlier_values"] == 0


def test_one_odd_day_among_the_earlier_ones_does_not_move_the_median(tmp_path):
    change = _change(tmp_path, [100, 100, 100, 5, 100, 100], rows=_rows(last=1.2))
    assert change.status == "ok" and change.metrics["baseline_median"] == 100


def test_earlier_days_that_took_nothing_have_no_change_in_percent_unless_this_day_took_nothing_too(tmp_path):
    change = _change(tmp_path, [0, 0, 0, 0, 0, 0])
    assert (change.status, change.observed) == ("unknown", None)
    assert change.details == "the median of the earlier values is 0, so a change in percent has no meaning"
    assert change.metrics["baseline_median"] == 0 and change.metrics["value"] == 100
    zero = [{**row, "revenue": Decimal("0.00")} if row["order_date"] == LAST_DAY else row for row in _rows()]
    same = _change(tmp_path / "zero", [0, 0, 0, 0, 0, 0], rows=zero)
    assert (same.status, same.observed) == ("ok", "+0.0 percent")
    assert same.details == "revenue for day 2026-10-27 was 0, the same as the median of the 6 earlier days, 0"


def test_a_change_is_measured_against_the_size_of_the_median_so_a_smaller_loss_is_a_rise(tmp_path):
    loss = [{**row, "revenue": Decimal("-50.00") if row["city"] == "Pune" else Decimal("0.00")} for row in _rows()]
    config = _lake(tmp_path, [{"metric": "revenue", "grain": "day", "max_change_pct": 40}], rows=loss)
    _seed(config, [-100] * 6)
    change = evaluate(config, NOW)[0]
    assert (change.status, change.observed) == ("error", "+50.0 percent")
    assert change.details == ("revenue for day 2026-10-27 was -50, 50.0 percent higher than the median of the 6 "
                              "earlier days, -100")


# ---------------------------------------------------------------------- the history


def test_the_monitor_builds_its_own_baseline_day_after_day_and_waits_for_3_earlier_values(tmp_path):
    config = _lake(tmp_path, [REVENUE], rows=_rows(last=0.4))
    seen = []
    for day in range(21, 29):
        now = dt.datetime(2026, 10, day, 6, tzinfo=UTC) + dt.timedelta(hours=day % 3)
        results = evaluate(config, now)
        record([(config, results)])
        record([(config, evaluate(config, now + dt.timedelta(hours=7)))])  # more than one look a day
        seen.append({result.monitor: result.status for result in results}["metric.revenue.change"])
    assert seen == ["unknown", "unknown", "unknown", "ok", "ok", "ok", "ok", "error"]
    last = read_results(config, monitor="metric.revenue.change")[-1]
    assert last.metrics["earlier_values"] == 6 and last.metrics["change_pct"] == -60.0


def test_the_history_keeps_the_value_and_never_a_row(tmp_path):
    config = _lake(tmp_path, [{**REVENUE, "filter": {"city": "Pune"}, "max": 9999}])
    _seed(config, [60] * 4)
    record([(config, evaluate(config, NOW))])
    allowed = {"grain", "period", "definition", "table", "read", "value", "min", "max", "max_change_pct", "lookback",
               "earlier_values", "baseline_median", "change_pct", "needed_values"}
    stored = read_jsonl(config.resolve(JSONL_PATH))
    assert len(stored) == 4 + 2
    assert all(set(result.metrics) <= allowed for result in stored)
    text = config.resolve(JSONL_PATH).read_text()
    for word in ("Pune", "Delhi", "Thai", "Italian"):
        assert word not in text


def test_the_results_go_to_the_iceberg_history_of_a_config_that_has_a_sink_and_are_read_back_from_it(tmp_path):
    config = _lake(tmp_path, sinks=["iceberg"])
    for day in range(21, 26):
        record([(config, evaluate(config, dt.datetime(2026, 10, day, 6, tzinfo=UTC)))])
    assert not config.resolve(JSONL_PATH).exists()
    catalog = create_catalog({"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")})
    rows = catalog.load_table(f"_ldp.{MONITOR_TABLE}").scan().to_arrow().to_pylist()
    assert {row["monitor"] for row in rows} == {"metric.revenue.value", "metric.revenue.change"}
    assert all(json.loads(row["metrics"])["definition"] for row in rows)
    change = _by_name(config, dt.datetime(2026, 10, 26, 6, tzinfo=UTC))["metric.revenue.change"]
    assert (change.status, change.metrics["earlier_values"]) == ("ok", 5)


def test_the_history_is_read_only_when_a_rule_compares_with_it(tmp_path, monkeypatch):
    from local_data_platform.monitoring import history

    def refuse(*args, **kwargs):
        raise AssertionError("the history was read")

    monkeypatch.setattr(history, "read_results", refuse)
    assert evaluate(_lake(tmp_path / "a", [{"metric": "revenue", "grain": "day", "min": 0}]), NOW)[0].status == "ok"
    with pytest.raises(AssertionError, match="the history was read"):
        evaluate(_lake(tmp_path / "b", [REVENUE]), NOW)


# ---------------------------------------------------------------------- what cannot be measured


def test_a_metric_that_is_not_defined_is_an_error_that_lists_the_valid_names(tmp_path):
    results = _check(tmp_path, [{"metric": "revnue", "grain": "day", "min": 0, "max_change_pct": 50}])
    assert list(results) == ["metric.revnue.value", "metric.revnue.change"]
    for result in results.values():
        assert result.status == "error" and result.observed is None
        assert result.details == ("unknown metric 'revnue'. Did you mean 'revenue'? The metrics are "
                                  "['revenue', 'orders', 'avg_ticket', 'avg_rating']")
        assert "value" not in result.metrics


def test_a_grain_that_the_model_does_not_allow_is_an_error_that_says_which_do(tmp_path):
    definitions = copy.deepcopy(DEFINITIONS)
    definitions["semantic_models"][0]["time_dimension"]["grains"] = ["day", "month"]
    result = _check(tmp_path, [{"metric": "revenue", "grain": "week", "min": 0}], definitions=definitions)[
        "metric.revenue.value"]
    assert result.status == "error"
    assert result.details == ("'revenue' cannot be reported by week. The time dimension of daily_sales allows the "
                              "grains ['day', 'month']")


def test_a_filter_on_a_dimension_that_is_not_there_is_an_error_that_lists_the_dimensions(tmp_path):
    result = _check(tmp_path, [{**REVENUE, "filter": {"town": "Pune"}}])["metric.revenue.value"]
    assert result.status == "error"
    assert result.details.startswith("unknown dimension 'town'.") and "['city', 'kitchen']" in result.details


def test_a_folder_with_no_metrics_file_is_an_error_that_says_so(tmp_path):
    result = _check(tmp_path, definitions=None)["metric.revenue.value"]
    assert result.status == "error"
    assert result.details.startswith("the metrics of ") and "there is no metrics file in" in result.details


def test_a_metrics_file_that_is_not_valid_is_an_error_that_names_a_problem(tmp_path):
    bad = copy.deepcopy(DEFINITIONS)
    bad["metrics"][0]["measure"] = "no_such_measure"
    result = _check(tmp_path, definitions=bad)["metric.revenue.value"]
    assert result.status == "error" and result.details.startswith("the metrics of ")
    assert "no_such_measure" in result.details


def test_a_config_that_was_built_by_hand_reads_the_metrics_of_the_working_folder(tmp_path, monkeypatch):
    config = _lake(tmp_path)
    hand = Config.from_dict({"identifier": "gold_sales", "who": "ops", "metadata": config.metadata})
    assert hand.base_dir is None
    monkeypatch.chdir(tmp_path)
    assert evaluate(hand, NOW)[0].status == "ok"


def test_a_folder_that_is_not_there_is_an_error_of_the_metric_and_not_a_crash(tmp_path):
    config = _lake(tmp_path)
    gone = Config.from_dict({"identifier": "gold_sales", "who": "ops", "metadata": config.metadata},
                            base_dir=tmp_path / "gone")
    result = evaluate(gone, NOW)[0]
    assert result.status == "error"
    assert result.details.startswith("the metrics of ") and "is not a folder of configs" in result.details


def test_a_gold_table_that_is_not_built_yet_is_unknown_and_not_an_error(tmp_path):
    results = _check(tmp_path, table=False)
    assert set(results) == {"metric.revenue.value", "metric.revenue.change"}
    for result in results.values():
        assert result.status == "unknown" and "does not exist" in result.details


def test_the_other_monitors_still_run_and_a_broken_entry_does_not_hide_them(tmp_path):
    config = _lake(tmp_path, [{"metric": "revnue", "grain": "day", "min": 0}, REVENUE],
                   sla_extra={"volume": {"min_rows": 1}})
    results = _by_name(config)
    assert list(results) == ["volume.rows", "metric.revnue.value", "metric.revenue.value", "metric.revenue.change"]
    assert results["metric.revnue.value"].status == "error"
    assert results["metric.revenue.value"].status == "ok"
    assert results["volume.rows"].status == "ok" and results["volume.rows"].observed == f"{DAYS * 2} rows"


def test_without_duckdb_a_metric_monitor_is_unknown_and_says_how_to_install_it_and_the_others_still_run(
        tmp_path, monkeypatch):
    config = _lake(tmp_path, sla_extra={"schema": {}, "freshness": {"error_after": {"count": 1, "period": "day"}}})
    _refuse_duckdb(monkeypatch)
    results = _by_name(config)
    assert list(results) == ["freshness", "schema", "metric.revenue.value", "metric.revenue.change"]
    assert results["schema"].status == "ok" and results["freshness"].status == "ok"
    for name in ("metric.revenue.value", "metric.revenue.change"):
        assert results[name].status == "unknown"
        assert results[name].details == ("a metric monitor needs the duckdb package, install it with "
                                         'pip install "local-data-platform[duckdb]"')


def test_without_duckdb_a_query_is_unknown_too_whatever_it_says(tmp_path, monkeypatch):
    entries = [{"name": "orders_seen", "sql": "DELETE FROM gold_sales", "grain": "day", "min": 1}]
    config = _lake(tmp_path, entries, definitions=None)
    _refuse_duckdb(monkeypatch)
    result = evaluate(config, NOW)[0]
    assert result.status == "unknown" and 'pip install "local-data-platform[duckdb]"' in result.details


def _duckdb_error(kind: str, message: str) -> Exception:
    """An exception that is, to the monitor, an error of DuckDB of this kind."""
    error = type(kind, (Exception,), {})
    error.__module__ = "_duckdb"
    return error(message)


@pytest.mark.parametrize("kind, said", [
    ("BinderException", 'Binder Error: Referenced column "income" not found in FROM clause!'),
    ("CatalogException", "Catalog Error: Table with name orders does not exist!"),
    ("ParserException", 'Parser Error: syntax error at or near "FROM"')])
def test_an_error_of_duckdb_about_the_text_of_a_query_is_said_as_it_is(kind, said):
    assert _why(_duckdb_error(kind, said + "\nLINE 1: SELECT sum(income) FROM orders\n    ^")) == said


@pytest.mark.parametrize("kind", ["ConversionException", "InvalidInputException", "OutOfRangeException"])
def test_any_other_error_of_duckdb_is_said_by_its_kind_only(kind):
    said = _why(_duckdb_error(kind, "Conversion Error: Could not convert string 'Pune' to INT32"))
    assert said == f"DuckDB said {kind}, and the query has to be run by hand to see why"


def test_the_cause_of_an_error_counts_as_well_as_the_error_and_other_errors_are_said_on_one_line():
    cause = _duckdb_error("ConversionException", "Could not convert string 'Pune' to INT32")
    with pytest.raises(ConfigError) as wrapped:
        raise ConfigError("the query could not run on gold.gold_sales. Could not convert string 'Pune'.") from cause
    assert _why(wrapped.value) == "DuckDB said ConversionException, and the query has to be run by hand to see why"
    named = _duckdb_error("CatalogException", "Catalog Error: Table with name x does not exist!")
    with pytest.raises(ConfigError) as plain:
        raise ConfigError("the table x\n  is not there") from named
    assert _why(plain.value) == "Catalog Error: Table with name x does not exist!"
    wide = ConfigError("unknown metric 'x'.\n  The metrics are ['a']")
    assert _why(wide) == "unknown metric 'x'. The metrics are ['a']"
    other = type("ConversionException", (Exception,), {})
    assert _why(other("a value 'Pune' of ours")) == "a value 'Pune' of ours"


def _export(tmp_path: Path, config: Config) -> Config:
    """A second config in the folder whose target is a CSV file, with the SLA of ``config`` and no docs."""
    data = copy.deepcopy(config.metadata)
    data.pop("docs")
    data["target"] = {"name": "sales_export", "format": "CSV", "path": "sales.csv"}
    _write(tmp_path / "export.json", {"identifier": "export", "who": "ops", "metadata": data})
    return Config.from_json(tmp_path / "export.json")


def test_a_config_whose_target_is_a_file_still_measures_a_metric_of_its_folder(tmp_path):
    result = evaluate(_export(tmp_path, _lake(tmp_path)), NOW)[0]
    assert (result.status, result.observed, result.table_identifier) == ("ok", "100", "sales.csv")


# ---------------------------------------------------------------------- a query instead of a metric


def _query(tmp_path: Path, sql: str, **entry) -> MonitorResult:
    return evaluate(_lake(tmp_path, [{**QUERY, "sql": sql, **entry}], definitions=None), NOW)[0]


def test_a_query_runs_on_the_table_of_the_config_by_its_name_and_its_number_is_checked(tmp_path):
    results = _check(tmp_path, [QUERY], definitions=None)
    assert list(results) == ["metric.orders_seen.value", "metric.orders_seen.change"]
    value = results["metric.orders_seen.value"]
    assert (value.status, value.observed, value.expected) == ("ok", "2", "min 1")
    assert value.details == "orders_seen for day 2026-10-27 was 2, inside the limits"
    assert value.metrics["table"] == "gold.gold_sales" and value.metrics["value"] == 2 and "read" not in value.metrics
    assert results["metric.orders_seen.change"].details == "not enough history"


@pytest.mark.parametrize("sql, value", [
    ("SELECT sum(revenue) FROM gold_sales WHERE order_date = DATE '2026-10-27'", 100.0),
    ("WITH pune AS (SELECT * FROM gold_sales WHERE city = 'Pune') SELECT max(orders) FROM pune", 6),
    ("SELECT avg(rating) FROM gold_sales", 4.0),
    ("select 41 + 1", 42),
    ("(SELECT count(DISTINCT city) FROM gold_sales)", 2)])
def test_a_query_may_be_a_select_or_a_with_and_return_an_integer_a_float_or_a_decimal(tmp_path, sql, value):
    result = _query(tmp_path, sql)
    assert result.status == "ok" and result.metrics["value"] == pytest.approx(value)


def test_a_query_that_the_guard_rejects_is_an_error_with_the_message_of_the_guard_and_nothing_is_written(tmp_path):
    config = _lake(tmp_path, [{**QUERY, "sql": "DELETE FROM gold_sales"}], definitions=None)
    snapshots = _snapshots(tmp_path)
    results = evaluate(config, NOW)
    assert [result.status for result in results] == ["error", "error"]
    for result in results:
        assert result.details == f"the query was rejected, {REFUSED}"
        assert "value" not in result.metrics
    assert _snapshots(tmp_path) == snapshots
    assert Iceberg("gold_sales", CATALOG | {"warehouse_path": str(tmp_path / "warehouse")}).get().num_rows == DAYS * 2


@pytest.mark.parametrize("sql, message", [
    ("SELECT 1; DELETE FROM gold_sales", "exactly one statement is allowed; found 2"),
    ("INSERT INTO gold_sales SELECT * FROM gold_sales", "starts with 'INSERT'"),
    ("PRAGMA database_list", "starts with 'PRAGMA'"),
    ("WITH gone AS (DELETE FROM gold_sales RETURNING *) SELECT count(*) FROM gone", None),
    ("SELECT", "does not parse")])
def test_a_query_that_writes_or_is_not_one_statement_is_an_error_whatever_it_hides(tmp_path, sql, message):
    config = _lake(tmp_path, [{**QUERY, "sql": sql}], definitions=None)
    snapshots = _snapshots(tmp_path)
    result = evaluate(config, NOW)[0]
    assert result.status == "error" and "value" not in result.metrics
    assert result.details.startswith("the query ")
    assert message is None or message in result.details
    assert _snapshots(tmp_path) == snapshots


@pytest.mark.parametrize("sql, details", [
    ("SELECT 1, 2", "the query has to return one number, and this one returned 1 row and 2 columns"),
    ("SELECT city FROM gold_sales",
     "the query has to return one number, and this one returned {rows} rows and 1 column"),
    ("SELECT city FROM gold_sales WHERE false", "the query has to return one number, and this one returned 0 rows "
                                                "and 1 column"),
    ("SELECT 'Pune'", "the value is of the type str, and it has to be a number"),
    ("SELECT DATE '2026-10-27'", "the value is of the type date, and it has to be a number"),
    ("SELECT true", "the value is of the type bool, and it has to be a number"),
])
def test_a_query_has_to_return_one_number_and_the_details_never_repeat_a_value(tmp_path, sql, details):
    result = _query(tmp_path, sql)
    assert (result.status, result.details) == ("error", details.format(rows=DAYS * 2))
    assert "Pune" not in result.details


def test_a_query_that_returns_null_or_infinity_is_unknown(tmp_path):
    null = _query(tmp_path / "a", "SELECT max(orders) FROM gold_sales WHERE false")
    assert (null.status, null.details) == ("unknown", "the value is NULL, so there is no number to check")
    for number in ("inf", "-inf", "nan"):
        odd = _query(tmp_path / number, f"SELECT '{number}'::DOUBLE")
        assert (odd.status, odd.details) == (
            "unknown", "the value is not a finite number, so there is nothing to check")


def test_a_query_that_names_a_table_a_column_or_a_syntax_that_is_not_there_says_which(tmp_path):
    missing = _query(tmp_path / "a", "SELECT count(*) FROM orders")
    assert missing.status == "error" and "Table with name orders does not exist" in missing.details
    column = _query(tmp_path / "b", "SELECT sum(income) FROM gold_sales")
    assert column.status == "error" and '"income"' in column.details
    syntax = _query(tmp_path / "c", "SELECT count(* FROM gold_sales")
    assert syntax.status == "error" and "syntax error" in syntax.details


def test_a_failure_that_quotes_a_value_of_a_row_says_only_what_kind_of_failure_it_was(tmp_path):
    result = _query(tmp_path, "SELECT sum(CAST(city AS INTEGER)) FROM gold_sales")
    assert result.status == "error"
    assert result.details == ("the query could not run, DuckDB said ConversionException, and the query has to be run "
                              "by hand to see why")
    assert "Pune" not in result.details and "Delhi" not in result.details


def test_a_query_is_refused_in_a_folder_that_has_a_metrics_file(tmp_path):
    result = evaluate(_lake(tmp_path, [QUERY]), NOW)[0]
    assert (result.status, result.details) == ("error", QUERY_NEEDS_NO_METRICS_FILE)


def test_a_query_on_a_table_that_does_not_exist_is_unknown_and_a_rejected_one_is_still_an_error(tmp_path):
    missing = evaluate(_lake(tmp_path / "a", [QUERY], definitions=None, table=False), NOW)[0]
    assert missing.status == "unknown"
    assert missing.details == "there is no table to run the query on, the catalog does not exist yet"
    rejected = evaluate(_lake(tmp_path / "b", [{**QUERY, "sql": "DELETE FROM gold_sales"}], definitions=None,
                              table=False), NOW)[0]
    assert rejected.status == "error" and REFUSED in rejected.details


def test_a_query_on_a_config_whose_target_is_a_file_is_unknown(tmp_path):
    config = _lake(tmp_path, [QUERY], definitions=None)
    result = evaluate(_export(tmp_path, config), NOW)[0]
    assert result.status == "unknown"
    assert result.details == "there is no table to run the query on, the target is a CSV file, not an Iceberg table"


def test_the_baseline_of_a_query_comes_from_its_own_looks(tmp_path):
    sql = "SELECT sum(revenue) FROM gold_sales WHERE order_date = DATE '2026-10-27'"
    config = _lake(tmp_path, [{**QUERY, "sql": sql}], definitions=None)
    _seed(config, [100] * 6)
    change = next(result for result in evaluate(config, NOW) if result.monitor.endswith(".change"))
    assert (change.status, change.observed) == ("ok", "+0.0 percent")


# ---------------------------------------------------------------------- ldp monitor


def test_ldp_monitor_prints_a_line_for_each_metric_monitor_and_exits_with_2_for_a_fall(tmp_path, capsys):
    config = _lake(tmp_path, rows=_rows(last=0.4))
    _seed(config, [100] * 6)
    code, out, err = _ldp(["monitor", str(tmp_path / "gold_sales.json"), "--now", NOW.isoformat()], capsys)
    assert code == 2
    assert not [line for line in err.splitlines() if not line.startswith(ENGINE_WARNING)]
    lines = out.splitlines()
    assert any(line.split()[:4] == ["gold_sales", "gold.gold_sales", "metric.revenue.value", "ok"] for line in lines)
    change = next(line for line in lines if "metric.revenue.change" in line)
    assert " error " in change and "-60.0 percent" in change and "change over 50 percent of the last 7 days" in change
    assert "1 ok, 1 error" in out


def test_ldp_monitor_exits_with_0_while_a_metric_has_too_little_history_and_records_the_look(tmp_path, capsys):
    config = _lake(tmp_path)
    code, out, _ = _ldp(["monitor", str(tmp_path), "--now", NOW.isoformat()], capsys)
    assert code == 0 and "1 ok, 1 unknown" in out
    assert [result.monitor for result in read_jsonl(config.resolve(JSONL_PATH))] == [
        "metric.revenue.value", "metric.revenue.change"]


def test_ldp_monitor_json_has_the_value_and_the_period_of_each_result(tmp_path, capsys):
    _lake(tmp_path)
    code, out, _ = _ldp(["monitor", str(tmp_path / "gold_sales.json"), "--json", "--now", NOW.isoformat()], capsys)
    results = json.loads(out)
    assert code == 0 and [item["monitor"] for item in results] == ["metric.revenue.value", "metric.revenue.change"]
    assert all(item["metrics"]["period"] == "2026-10-27" and item["kind"] == "metric" for item in results)
    assert results[0]["metrics"]["value"] == 100.0


def test_ldp_monitor_does_not_write_to_the_gold_table(tmp_path, capsys):
    _lake(tmp_path)
    snapshots = _snapshots(tmp_path)
    _ldp(["monitor", str(tmp_path / "gold_sales.json"), "--now", NOW.isoformat()], capsys)
    assert _snapshots(tmp_path) == snapshots


def test_ldp_monitor_help_says_that_a_metric_runs_a_query_and_still_writes_to_no_table(capsys):
    code, out, _ = _ldp(["monitor", "--help"], capsys)
    text = " ".join(out.split())
    assert code == 0
    assert "aggregate query" in text and "writes to no table" in text and "scans no rows" not in text


# ---------------------------------------------------------------------- ldp plan


def _plan(tmp_path: Path, entries, **options) -> dict:
    """The plan of the config of a raw table, with an SLA of ``entries``, in a folder with the gold table."""
    _lake(tmp_path, None, **options)
    _write(tmp_path / "orders.json", {"identifier": "orders", "who": "ops", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append",
                   "catalog": {"identifier": "shop", "warehouse_path": "warehouse"}},
        "sla": {"metrics": entries}}})
    return plan(tmp_path / "orders.json")


def test_plan_has_the_metrics_in_the_sla_and_nothing_wrong_with_a_defined_metric(tmp_path):
    result = _plan(tmp_path, [{**REVENUE, "filter": {"city": "Pune"}},
                              {"metric": "orders", "grain": "week", "max": 9, "name": "weekly_orders"}])
    assert result["valid"] is True, result["errors"]
    assert result["sla"]["metrics"] == [
        {"metric": "revenue", "grain": "day", "min": 0, "max_change_pct": 50, "filter": {"city": "Pune"},
         "lookback": 7},
        {"name": "weekly_orders", "metric": "orders", "grain": "week", "max": 9, "lookback": 7}]


def test_plan_reports_a_metric_that_is_not_defined_and_lists_the_valid_names(tmp_path):
    result = _plan(tmp_path, [REVENUE, {"metric": "revnue", "grain": "day", "min": 0}])
    assert result["valid"] is False
    assert result["errors"] == [
        "metadata.sla.metrics[1].metric: unknown metric 'revnue'. Did you mean 'revenue'? The metrics are "
        "['revenue', 'orders', 'avg_ticket', 'avg_rating']"]


def test_plan_reports_a_grain_and_a_filter_that_the_metric_does_not_allow(tmp_path):
    definitions = copy.deepcopy(DEFINITIONS)
    definitions["semantic_models"][0]["time_dimension"]["grains"] = ["day", "month"]
    result = _plan(tmp_path, [{"metric": "revenue", "grain": "week", "min": 0, "filter": {"town": "Pune"}}],
                   definitions=definitions)
    assert result["errors"] == [
        "metadata.sla.metrics[0].grain: revenue cannot be reported by week, the time dimension of daily_sales allows "
        "['day', 'month']",
        "metadata.sla.metrics[0].filter.town: is not a dimension of daily_sales, the dimensions are "
        "['city', 'kitchen']"]


def test_plan_reports_a_metric_of_a_model_with_no_time_dimension(tmp_path):
    definitions = copy.deepcopy(DEFINITIONS)
    del definitions["semantic_models"][0]["time_dimension"]
    result = _plan(tmp_path, [REVENUE], definitions=definitions)
    assert result["errors"] == ["metadata.sla.metrics[0].grain: revenue has no period to measure, because "
                                "daily_sales has no time dimension"]


def test_plan_says_that_a_metric_cannot_be_checked_in_a_folder_with_no_metrics_file(tmp_path):
    result = _plan(tmp_path, [REVENUE], definitions=None)
    assert result["valid"] is False and len(result["errors"]) == 1
    assert result["errors"][0].startswith(
        "metadata.sla.metrics[0].metric: cannot be checked, there is no metrics file in")


def test_plan_refuses_a_query_in_a_folder_that_has_a_metrics_file_and_allows_one_without(tmp_path):
    refused = _plan(tmp_path / "a", [QUERY, REVENUE])
    assert refused["errors"] == [f"metadata.sla.metrics[0].sql: {QUERY_NEEDS_NO_METRICS_FILE}"]
    allowed = _plan(tmp_path / "b", [QUERY], definitions=None)
    assert allowed["valid"] is True, allowed["errors"]


def test_plan_notes_a_lookback_that_leaves_too_few_earlier_values(tmp_path):
    result = _plan(tmp_path, [{**REVENUE, "lookback": 3}, {**REVENUE, "name": "four", "lookback": 4},
                              {"metric": "orders", "grain": "day", "min": 0, "lookback": 1}])
    assert result["notes"] == ["metrics[0].lookback is 3, which leaves fewer than 3 earlier values, so the change "
                               "rule never reports"]


def test_plan_of_a_spec_given_as_a_dict_has_no_folder_and_so_no_names_to_check():
    spec = {"identifier": "orders", "who": "ops", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG},
        "sla": {"metrics": [{"metric": "no_such_metric", "grain": "day", "min": 0}]}}}
    assert plan(spec)["errors"] == []


def test_ldp_plan_prints_the_metrics_of_the_sla_in_a_line_and_exits_with_1_for_an_unknown_metric(tmp_path, capsys):
    _plan(tmp_path / "a", [REVENUE, {"metric": "orders", "grain": "week", "min": 0}])
    code, out, _ = _ldp(["plan", str(tmp_path / "a" / "orders.json")], capsys)
    assert code == 0
    assert next(line for line in out.splitlines() if line.startswith("sla ")).split(None, 1)[1] == (
        "metrics revenue by day and orders by week")
    _plan(tmp_path / "b", [{"metric": "revnue", "grain": "day", "min": 0}])
    code, out, err = _ldp(["plan", str(tmp_path / "b" / "orders.json")], capsys)
    assert code == 1 and "unknown metric 'revnue'" in out + err


# ---------------------------------------------------------------------- the checks of a folder


def test_the_folder_checks_find_every_problem_of_every_entry_and_none_in_a_good_one(tmp_path):
    config = _lake(tmp_path, [
        REVENUE, {"metric": "orders", "grain": "week", "min": 0, "name": "weekly"},
        {"metric": "revnue", "grain": "day", "min": 0},
        {"metric": "orders", "grain": "day", "min": 0, "filter": {"town": "Pune"}, "name": "odd"}, {**QUERY}])
    rules = config.sla.metrics
    assert folder_problems(rules[:2], tmp_path) == []
    assert folder_problems((), tmp_path) == []
    assert folder_problems(rules, tmp_path) == [
        (".metrics[4].sql", QUERY_NEEDS_NO_METRICS_FILE),
        (".metrics[2].metric", "unknown metric 'revnue'. Did you mean 'revenue'? The metrics are "
                               "['revenue', 'orders', 'avg_ticket', 'avg_rating']"),
        (".metrics[3].filter.town", "is not a dimension of daily_sales, the dimensions are ['city', 'kitchen']")]
    bare = _lake(tmp_path / "bare", [REVENUE, {**QUERY}], definitions=None)
    [(path, message)] = folder_problems(bare.sla.metrics, tmp_path / "bare")
    assert path == ".metrics[0].metric" and message.startswith("cannot be checked, there is no metrics file in")
    assert folder_problems(rules, tmp_path / "gone") == [
        (".metrics", f"cannot be checked, {tmp_path / 'gone'} is not a folder of configs")]
