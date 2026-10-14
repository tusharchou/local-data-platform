"""Backfills: ``ldp backfill`` and ``orchestration.backfill`` run a workflow for every window of a range, and resume.

The projects here are daily drop files in ``tmp_path``, one ``orders_YYYY-MM-DD.csv`` for each day, loaded by a config
whose source path has the ``{window_start:%Y-%m-%d}`` placeholder and whose ``window.column`` selects the rows of the
window. Everything runs offline, and a window takes a fraction of a second.
"""

import dataclasses
import datetime as dt
import hashlib
import json
import logging
from collections import Counter
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import commit_log, published_keys
from local_data_platform.orchestration import (
    BackfillResult,
    BackfillWindow,
    backfill,
    load_workflow,
    plan_workflow,
    run_workflow,
    windows,
)
from local_data_platform.orchestration import backfills as backfill_module
from local_data_platform.orchestration.backfills import (
    DEFAULT_MAX_WINDOWS,
    _ledger_status,
    _outcome_of,
    _read_ledger,
)
from local_data_platform.orchestration.runner import TaskResult, WorkflowResult
from local_data_platform.pipeline import Pipeline
from local_data_platform.spec import (
    API_VERSION,
    canonical_json,
    idempotency_key,
    plan,
    replace_key,
    spec_hash,
)
from tests.support import FakeDbt, dbt_artifacts, dbt_model, write_dbt_project

UTC = dt.timezone.utc
START = dt.date(2026, 6, 1)
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}


# ====================================================================== helpers


def _day(n: int) -> dt.date:
    """Day number ``n`` of the range, from 1: 2026-06-01 is day 1."""
    return START + dt.timedelta(days=n - 1)


def _span(n: int) -> str:
    """The window of day ``n``, as ``START/END`` in UTC."""
    return f"{_day(n).isoformat()}T00:00:00Z/{_day(n + 1).isoformat()}T00:00:00Z"


def _write(path: Path, content) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
    return path


def _drop(root: Path, n: int, rows: int = 2, base: int = 0, start: dt.date = START) -> Path:
    """The drop file of day ``n``: ``rows`` rows with ids ``base + 100 * n + i`` at 10:00, 11:00, ..."""
    day = (start + dt.timedelta(days=n - 1)).isoformat()
    lines = ["id,at"] + [f"{base + n * 100 + i},{day}T{10 + i:02d}:00:00" for i in range(rows)]
    return _write(root / "landing" / f"orders_{day}.csv", "\n".join(lines) + "\n")


def _config(root: Path, *, mode: str = "append", column: str | None = "at", schedule: str | None = "@daily",
            size: str | None = "day", timezone: str | None = None, min_rows: int | None = 2,
            name: str = "orders.json", identifier: str = "orders", extra: dict | None = None,
            path: str = "../landing/orders_{window_start:%Y-%m-%d}.csv", table: str = "orders") -> Path:
    window: dict = {}
    if size:
        window["size"] = size
    if column:
        window["column"] = column
    if timezone:
        window["timezone"] = timezone
    metadata: dict = {
        "source": {"name": "orders", "format": "CSV", "path": path},
        "target": {"name": table, "format": "ICEBERG", "write_mode": mode, "catalog": CATALOG}}
    if window:
        metadata["window"] = window
    if schedule:
        metadata["schedule"] = schedule
    if min_rows:
        metadata["quality"] = [{"check": "row_count", "min": min_rows}]
    metadata.update(extra or {})
    return _write(root / "config" / name, {"identifier": identifier, "metadata": metadata})


def _project(root: Path, days: int = 3, start: dt.date = START, **config) -> Path:
    """Write the drop files of days 1 to ``days`` and the config ``config/orders.json``; return the config."""
    for n in range(1, days + 1):
        _drop(root, n, start=start)
    return _config(root, **config)


def _orders(root: Path) -> Iceberg:
    return Iceberg("orders", {"identifier": "shop", "warehouse_path": str(root / "warehouse")})


def _rows_per_day(root: Path) -> dict[str, int]:
    values = _orders(root).get().column("at").to_pylist()
    return dict(sorted(Counter(value.date().isoformat() for value in values).items()))


def _ledger(config: Path) -> Path:
    return config.parent / ".ldp" / "events.jsonl"


def _ran_windows(config: Path) -> list[str]:
    """The windows that were run, in the order they started, from the ledger."""
    spans = []
    for line in _ledger(config).read_text().splitlines():
        event = json.loads(line)
        if event["type"] == "workflow.started":
            spans.append("/".join(event["payload"]["window"]))
    return spans


class ExtractSpy:
    """Counts the reads of a source, and raises ``error`` on the reads whose number is in ``fail_on``."""

    def __init__(self, monkeypatch, *, fail_on=(), error: BaseException | None = None):
        self.calls, self.fail_on, self.error = 0, set(fail_on), error
        real = Pipeline.extract

        def extract(pipeline):
            self.calls += 1
            if self.calls in self.fail_on:
                raise self.error
            return real(pipeline)

        monkeypatch.setattr(Pipeline, "extract", extract)


# ====================================================================== the windows of a range


@pytest.mark.parametrize("value, zone, expected", [
    ("2026-06-01", None, "2026-06-01T00:00:00+00:00"),
    ("2026-06-01", "Europe/Paris", "2026-05-31T22:00:00+00:00"),
    ("2026-01-15", "Europe/Paris", "2026-01-14T23:00:00+00:00"),
    ("2026-06-01T06:30:00", "Europe/Paris", "2026-06-01T04:30:00+00:00"),
    ("2026-06-01T06:30:00+05:30", "Europe/Paris", "2026-06-01T01:00:00+00:00"),
    ("2026-06-01T06:30:00Z", "Europe/Paris", "2026-06-01T06:30:00+00:00"),
    ("2026-06-01T06:30:00z", None, "2026-06-01T06:30:00+00:00"),
    (dt.date(2026, 6, 1), "Asia/Bangkok", "2026-05-31T17:00:00+00:00"),
    (dt.datetime(2026, 6, 1, 6), "Asia/Bangkok", "2026-05-31T23:00:00+00:00"),
    (dt.datetime(2026, 6, 1, 6, tzinfo=dt.timezone(dt.timedelta(hours=2))), "Asia/Bangkok",
     "2026-06-01T04:00:00+00:00"),
])
def test_a_boundary_with_no_offset_is_read_in_the_time_zone_of_the_workflow(value, zone, expected):
    assert windows.parse_boundary(value, zone).isoformat() == expected


@pytest.mark.parametrize("value", ["yesterday", "2026-13-01", "01/06/2026"])
def test_a_text_that_is_not_an_iso_date_is_refused_with_an_example(value):
    with pytest.raises(ConfigError, match=r"the start of the range '.*' is not an ISO-8601 date or datetime, such as "
                                          r"2026-06-01 or 2026-06-01T06:00:00\+02:00"):
        windows.parse_boundary(value, None, "the start of the range")


@pytest.mark.parametrize("value", ["", "  ", 20260601, None, ["2026-06-01"]])
def test_a_value_that_is_neither_text_nor_a_date_is_refused(value):
    with pytest.raises(ConfigError, match=r"the start of the range must be a date, a datetime or ISO-8601 text, got"):
        windows.parse_boundary(value, None, "the start of the range")


def test_an_unknown_time_zone_is_refused_when_a_boundary_is_read():
    with pytest.raises(ConfigError, match="unknown time zone"):
        windows.parse_boundary("2026-06-01", "Mars/Olympus")


@pytest.mark.parametrize("text, size, zone, aligned", [
    ("2026-06-01T00:00:00Z", "day", None, True),
    ("2026-06-01T06:00:00Z", "day", None, False),
    ("2026-06-01T00:00:01Z", "day", None, False),
    ("2026-05-31T22:00:00Z", "day", "Europe/Paris", True),
    ("2026-06-01T00:00:00Z", "day", "Europe/Paris", False),
    ("2026-01-14T23:00:00Z", "day", "Europe/Paris", True),
    ("2026-06-01T05:00:00Z", "hour", None, True),
    ("2026-06-01T05:30:00Z", "hour", None, False),
    ("2026-06-01T05:00:30Z", "hour", None, False),
    ("2026-06-01T05:30:00Z", "hour", "Asia/Kolkata", True),   # +05:30, so :30 UTC is the start of a local hour
    ("2026-06-01T05:00:00Z", "hour", "Asia/Kolkata", False),
    ("2026-06-01T05:15:00Z", "15m", None, True),
    ("2026-06-01T05:20:00Z", "15m", None, False),
    ("2026-06-01T05:45:00Z", "15m", "Asia/Kathmandu", True),  # +05:45
])
def test_an_instant_starts_a_window_when_it_is_a_whole_day_hour_or_quarter_in_the_zone(text, size, zone, aligned):
    moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))

    assert windows.is_aligned(moment, size, zone) is aligned


def _grid(start: str, end: str, size: str, zone: str | None = None) -> list[tuple[str, str]]:
    first, last = windows.parse_boundary(start, zone), windows.parse_boundary(end, zone)
    return [(a.isoformat(), b.isoformat()) for a, b in windows.iter_windows(first, last, size, zone)]


def test_days_are_laid_end_to_end_and_each_is_whole():
    found = _grid("2026-06-01", "2026-06-04", "day")

    assert found == [("2026-06-01T00:00:00+00:00", "2026-06-02T00:00:00+00:00"),
                     ("2026-06-02T00:00:00+00:00", "2026-06-03T00:00:00+00:00"),
                     ("2026-06-03T00:00:00+00:00", "2026-06-04T00:00:00+00:00")]
    assert _grid("2026-06-01", "2026-06-01", "day") == [] and _grid("2026-06-02", "2026-06-01", "day") == []


def test_an_end_that_does_not_start_a_window_leaves_the_last_part_out():
    assert _grid("2026-06-01", "2026-06-03T18:00:00", "day") == _grid("2026-06-01", "2026-06-03", "day")
    assert _grid("2026-06-01T00:00:00", "2026-06-01T02:30:00", "hour")[-1][1] == "2026-06-01T02:00:00+00:00"


def test_a_day_is_23_or_25_hours_long_across_a_clock_change():
    spring = _grid("2026-03-28", "2026-03-31", "day", "Europe/Paris")
    autumn = _grid("2026-10-24", "2026-10-27", "day", "Europe/Paris")

    def lengths(grid):
        return [(dt.datetime.fromisoformat(b) - dt.datetime.fromisoformat(a)).total_seconds() / 3600 for a, b in grid]

    assert lengths(spring) == [24, 23, 24] and lengths(autumn) == [24, 25, 24]
    assert spring[1] == ("2026-03-28T23:00:00+00:00", "2026-03-29T22:00:00+00:00"), "local midnights, in UTC"
    assert all(windows.is_aligned(dt.datetime.fromisoformat(a), "day", "Europe/Paris") for a, _ in spring + autumn)


def test_hours_and_quarters_are_as_long_as_they_say_even_across_a_clock_change():
    assert len(_grid("2026-06-01", "2026-06-02", "hour")) == 24
    assert len(_grid("2026-06-01", "2026-06-02", "15m")) == 96
    assert len(_grid("2026-10-25", "2026-10-26", "hour", "Europe/Paris")) == 25, "the repeated hour is two windows"
    assert len(_grid("2026-03-29", "2026-03-30", "hour", "Europe/Paris")) == 23
    quarter = _grid("2026-06-01T10:00:00", "2026-06-01T10:45:00", "15m")
    assert quarter[1] == ("2026-06-01T10:15:00+00:00", "2026-06-01T10:30:00+00:00") and len(quarter) == 3


@pytest.mark.parametrize("moment, size, zone, expected", [
    ("2026-08-05T02:05:00Z", "day", None, "2026-08-05T00:00:00+00:00"),
    ("2026-08-05T02:05:00Z", "day", "Europe/Paris", "2026-08-04T22:00:00+00:00"),
    ("2026-08-05T02:47:31Z", "hour", None, "2026-08-05T02:00:00+00:00"),
    ("2026-08-05T02:47:31Z", "15m", None, "2026-08-05T02:45:00+00:00"),
    ("2026-08-05T02:47:31Z", "hour", "Asia/Kolkata", "2026-08-05T02:30:00+00:00"),
    ("2026-08-05T00:00:00Z", "day", None, "2026-08-05T00:00:00+00:00"),
])
def test_the_last_start_of_a_window_before_an_instant(moment, size, zone, expected):
    found = windows.floor_boundary(dt.datetime.fromisoformat(moment.replace("Z", "+00:00")), size, zone)

    assert found.isoformat() == expected and windows.is_aligned(found, size, zone)


@pytest.mark.parametrize("metadata, when, expected", [
    ({"window": {"size": "hour"}, "schedule": "@daily"}, None, "hour"),
    ({"schedule": "@daily"}, None, "day"),
    ({"schedule": "@hourly"}, None, "hour"),
    ({"schedule": "0 0 * * *"}, None, "day"),
    ({"schedule": "0 * * * *"}, None, "hour"),
    ({"schedule": "5 2 * * *"}, None, None),
    ({"schedule": "@weekly"}, None, None),
    ({"schedule": "@monthly"}, None, None),
    ({}, "daily", "day"),
    ({}, "hourly", "hour"),
    ({}, "whenever", None),
    ({"window": "none", "schedule": "@daily"}, None, "day"),
    ({"window": {"size": "15m"}}, None, "15m"),
    ({}, None, None),
])
def test_the_size_of_a_window_is_the_declared_one_or_the_one_a_daily_or_hourly_schedule_means(metadata, when, expected):
    assert windows.window_size_of(metadata, when) == expected


def test_a_window_declaration_that_is_not_valid_is_reported_when_the_size_is_asked():
    with pytest.raises(ConfigError, match=r"metadata\.window\.size: must be one of"):
        windows.window_size_of({"window": {"size": "week"}})


# ====================================================================== the replace key


def test_the_replace_key_is_the_sha256_of_the_key_the_word_replace_and_the_spec_hash():
    key, digest = "a" * 64, "b" * 64

    found = replace_key(key, digest)

    assert found == hashlib.sha256(canonical_json([API_VERSION, "replace", key, digest])).hexdigest()
    assert len(found) == 64 and found not in (key, digest) and found == replace_key(key, digest)


def test_the_replace_key_changes_with_the_key_and_with_the_spec_but_not_with_anything_else():
    base = replace_key("a" * 64, "b" * 64)

    assert replace_key("c" * 64, "b" * 64) != base and replace_key("a" * 64, "d" * 64) != base
    assert replace_key("b" * 64, "a" * 64) != base, "the order of the two matters"
    assert replace_key("a" * 64, "b" * 64) == base


@pytest.mark.parametrize("key, digest", [("", "b"), ("a", ""), ("  ", "b"), (None, "b"), ("a", None), (5, "b")])
def test_the_replace_key_needs_a_key_and_a_spec_hash(key, digest):
    with pytest.raises(ConfigError, match="must be a non-empty string"):
        replace_key(key, digest)


def test_the_replace_key_of_a_window_is_the_one_the_dry_run_prints_and_not_the_original_key(tmp_path):
    config = _project(tmp_path, days=1)
    loaded = Config.from_json(config)

    original = idempotency_key(loaded, _span(1))
    replaced = replace_key(original, spec_hash(loaded))

    assert original == plan(config, window=_span(1))["idempotency_key"]
    assert replaced != original
    dry = backfill(config, "2026-06-01", "2026-06-02", dry_run=True, replace=True)
    assert dry.windows[0].keys == {"orders": replaced}


# ====================================================================== the keys of a table


def test_the_keys_of_a_table_are_found_in_one_walk(tmp_path):
    config = _project(tmp_path, days=3)
    backfill(config, "2026-06-01", "2026-06-04")

    table = _orders(tmp_path).table()

    keys = published_keys(table)
    assert keys == {row["idempotency_key"] for row in commit_log(table)} and len(keys) == 3
    assert keys == {idempotency_key(Config.from_json(config), _span(n)) for n in (1, 2, 3)}


def test_a_table_with_no_keyed_snapshot_has_no_keys(tmp_path):
    _project(tmp_path, days=1)
    plain = Iceberg("plain", {"identifier": "shop", "warehouse_path": str(tmp_path / "warehouse")})
    import pyarrow as pa

    plain.put(pa.table({"id": [1]}))

    assert published_keys(plain.table()) == set()


# ====================================================================== a dry run


def test_a_dry_run_lists_every_window_with_its_key_and_state_and_runs_nothing(tmp_path, monkeypatch):
    config = _project(tmp_path, days=3)
    spy = ExtractSpy(monkeypatch)

    result = backfill(config, "2026-06-01", "2026-06-04", dry_run=True)

    assert [entry.window for entry in result.windows] == [_span(1), _span(2), _span(3)]
    assert [entry.state for entry in result.windows] == ["missing"] * 3
    assert all(entry.outcome is None and entry.result is None for entry in result.windows)
    assert [entry.keys["orders"] for entry in result.windows] == [
        plan(config, window=_span(n))["idempotency_key"] for n in (1, 2, 3)], "the keys of ldp plan"
    assert result.counts() == {"published": 0, "skipped": 0, "blocked": 0, "failed": 0}
    assert result.states() == {"published": 0, "partial": 0, "missing": 3, "failed": 0, "blocked": 0}
    assert (result.dry_run, result.status, result.exit_code) == (True, "dry_run", 0)
    assert spy.calls == 0
    assert not (tmp_path / "warehouse").exists() and not (config.parent / ".ldp").exists(), "nothing was created"


def test_a_dry_run_tells_published_failed_blocked_partial_and_missing_windows_apart(tmp_path):
    config = _project(tmp_path, days=6)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()  # day 2 has not landed
    _drop(tmp_path, 3, rows=1)  # day 3 has one row, and the check wants two
    backfill(config, "2026-06-01", "2026-06-04")  # day 1 publishes, day 2 fails, day 3 is blocked
    backfill(config, "2026-06-05", "2026-06-07")  # days 5 and 6 publish

    result = backfill(config, "2026-06-01", "2026-06-07", dry_run=True)

    assert [entry.state for entry in result.windows] == ["published", "failed", "blocked", "missing", "published",
                                                         "published"]
    assert result.states() == {"published": 3, "partial": 0, "missing": 1, "failed": 1, "blocked": 1}


def test_a_dry_run_changes_nothing_in_a_table_that_exists(tmp_path):
    config = _project(tmp_path, days=3)
    backfill(config, "2026-06-01", "2026-06-02")
    table = _orders(tmp_path).table()
    before = (table.metadata_location, _ledger(config).read_bytes())

    backfill(config, "2026-06-01", "2026-06-04", dry_run=True)

    assert (_orders(tmp_path).table().metadata_location, _ledger(config).read_bytes()) == before


def test_a_dry_run_with_replace_prints_the_replace_keys(tmp_path):
    config = _project(tmp_path, days=2)
    loaded = Config.from_json(config)

    result = backfill(config, "2026-06-01", "2026-06-03", dry_run=True, replace=True)

    assert [entry.keys["orders"] for entry in result.windows] == [
        replace_key(idempotency_key(loaded, _span(n)), spec_hash(loaded)) for n in (1, 2)]
    assert result.replace is True


# ====================================================================== a run, and a run again


def test_a_backfill_runs_every_window_oldest_first_each_under_its_own_key(tmp_path):
    config = _project(tmp_path, days=3)

    result = backfill(config, "2026-06-01", "2026-06-04")

    assert result.counts() == {"published": 3, "skipped": 0, "blocked": 0, "failed": 0}
    assert (result.status, result.exit_code, result.order) == ("succeeded", 0, "oldest first")
    assert [entry.outcome for entry in result.windows] == ["published"] * 3
    assert _ran_windows(config) == [_span(1), _span(2), _span(3)]
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 2, "2026-06-03": 2}
    published = {row["idempotency_key"]: row["run_id"] for row in commit_log(_orders(tmp_path).table())}
    assert set(published) == {entry.keys["orders"] for entry in result.windows} and len(published) == 3
    assert all(entry.state == "missing" for entry in result.windows), "the state is what the window was before"
    assert all(entry.result.status == "succeeded" for entry in result.windows)


def test_a_30_day_backfill_that_fails_at_window_10_is_resumed_and_loads_every_day_exactly_once(tmp_path):
    config = _project(tmp_path, days=30)
    (tmp_path / "landing" / f"orders_{_day(10)}.csv").unlink()  # the file of day 10 has not landed

    first = backfill(config, "2026-06-01", "2026-07-01")

    assert first.counts() == {"published": 29, "skipped": 0, "blocked": 0, "failed": 1}
    assert [number for number, entry in enumerate(first.windows, start=1) if entry.outcome == "failed"] == [10]
    assert first.exit_code == 1 and "FileNotFoundError" in first.windows[9].problems()[0]
    assert len(_rows_per_day(tmp_path)) == 29
    _drop(tmp_path, 10)

    second = backfill(config, "2026-06-01", "2026-07-01")

    assert second.counts() == {"published": 1, "skipped": 29, "blocked": 0, "failed": 0} and second.exit_code == 0
    assert [entry.outcome for entry in second.windows][9] == "published"
    assert _ran_windows(config)[30:] == [_span(10)], "the 30 windows of the first run, then only window 10"
    assert _rows_per_day(tmp_path) == {_day(n).isoformat(): 2 for n in range(1, 31)}
    keys = [row["idempotency_key"] for row in commit_log(_orders(tmp_path).table())]
    assert len(keys) == 30 and len(set(keys)) == 30, "every day was published once"
    third = backfill(config, "2026-06-01", "2026-07-01")
    assert third.counts() == {"published": 0, "skipped": 30, "blocked": 0, "failed": 0}


def test_running_a_finished_backfill_again_does_nothing_and_never_reads_a_source(tmp_path, monkeypatch):
    config = _project(tmp_path, days=4)
    backfill(config, "2026-06-01", "2026-06-05")
    ledger = _ledger(config).read_bytes()
    snapshot = _orders(tmp_path).table().current_snapshot().snapshot_id
    for n in range(1, 5):
        (tmp_path / "landing" / f"orders_{_day(n)}.csv").unlink()  # the sources are gone
    spy = ExtractSpy(monkeypatch)
    seen = []

    again = backfill(config, "2026-06-01", "2026-06-05", on_window=lambda w, n, t: seen.append((n, t, w.outcome)))

    assert again.counts() == {"published": 0, "skipped": 4, "blocked": 0, "failed": 0} and again.exit_code == 0
    assert all(entry.state == "published" and entry.result is None for entry in again.windows)
    assert spy.calls == 0, "no source was read"
    assert _ledger(config).read_bytes() == ledger, "no event was written"
    assert _orders(tmp_path).table().current_snapshot().snapshot_id == snapshot
    assert seen == [(1, 4, "skipped"), (2, 4, "skipped"), (3, 4, "skipped"), (4, 4, "skipped")]


def test_a_backfill_that_is_interrupted_resumes_at_the_window_it_stopped_at(tmp_path, monkeypatch):
    config = _project(tmp_path, days=6)
    ExtractSpy(monkeypatch, fail_on={4}, error=KeyboardInterrupt())  # the machine is stopped while window 4 reads

    with pytest.raises(KeyboardInterrupt):
        backfill(config, "2026-06-01", "2026-06-07")

    monkeypatch.undo()
    assert sorted(_rows_per_day(tmp_path)) == ["2026-06-01", "2026-06-02", "2026-06-03"]
    dry = backfill(config, "2026-06-01", "2026-06-07", dry_run=True)
    states = [entry.state for entry in dry.windows]
    assert states == ["published"] * 3 + ["missing"] * 3, "an unfinished run is not a failure"
    resumed = backfill(config, "2026-06-01", "2026-06-07")
    assert resumed.counts() == {"published": 3, "skipped": 3, "blocked": 0, "failed": 0}
    assert _ran_windows(config)[3:] == [_span(4), _span(4), _span(5), _span(6)], "window 4 again, then the rest"
    assert _rows_per_day(tmp_path) == {_day(n).isoformat(): 2 for n in range(1, 7)}


def test_run_backwards_runs_the_newest_window_first_and_loads_the_same_rows(tmp_path):
    config = _project(tmp_path, days=3)

    result = backfill(config, "2026-06-01", "2026-06-04", run_backwards=True)

    assert result.order == "newest first"
    assert [entry.window for entry in result.windows] == [_span(3), _span(2), _span(1)]
    assert _ran_windows(config) == [_span(3), _span(2), _span(1)]
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 2, "2026-06-03": 2}
    assert backfill(config, "2026-06-01", "2026-06-04").counts()["skipped"] == 3, "the order is no matter to a resume"


def test_the_callback_hears_of_each_window_that_ran_with_its_number_and_the_total(tmp_path):
    config = _project(tmp_path, days=3)
    seen = []

    backfill(config, "2026-06-01", "2026-06-04", on_window=lambda w, n, t: seen.append((n, t, w.window, w.outcome)))

    assert seen == [(1, 3, _span(1), "published"), (2, 3, _span(2), "published"), (3, 3, _span(3), "published")]


# ====================================================================== a window that does not get through


def test_a_window_that_a_quality_check_blocks_is_counted_and_does_not_stop_the_others(tmp_path):
    config = _project(tmp_path, days=4)
    _drop(tmp_path, 2, rows=1)  # one row, and the check wants two

    result = backfill(config, "2026-06-01", "2026-06-05")

    assert result.counts() == {"published": 3, "skipped": 0, "blocked": 1, "failed": 0}
    assert (result.status, result.exit_code) == ("blocked_quality", 3)
    assert [entry.outcome for entry in result.windows] == ["published", "blocked", "published", "published"]
    assert result.windows[1].problems() == ["orders: DataQualityError: 1 of 1 data quality checks failed:",
                                            "  - row_count: 1 row, expected >= 2"]
    _drop(tmp_path, 2)

    fixed = backfill(config, "2026-06-01", "2026-06-05")

    assert fixed.counts() == {"published": 1, "skipped": 3, "blocked": 0, "failed": 0} and fixed.exit_code == 0
    assert _rows_per_day(tmp_path) == {_day(n).isoformat(): 2 for n in range(1, 5)}


def test_a_backfill_with_a_failed_window_and_a_blocked_one_exits_with_one(tmp_path):
    config = _project(tmp_path, days=4)
    _drop(tmp_path, 2, rows=1)
    (tmp_path / "landing" / f"orders_{_day(3)}.csv").unlink()

    result = backfill(config, "2026-06-01", "2026-06-05")

    assert result.counts() == {"published": 2, "skipped": 0, "blocked": 1, "failed": 1}
    assert (result.status, result.exit_code) == ("failed", 1)


def test_stop_on_failure_leaves_the_windows_after_the_first_failed_one_undone_but_a_gate_does_not_stop(tmp_path):
    config = _project(tmp_path, days=5)
    _drop(tmp_path, 1, rows=1)  # blocked, which does not stop the backfill
    (tmp_path / "landing" / f"orders_{_day(3)}.csv").unlink()  # failed, which does
    seen = []

    result = backfill(config, "2026-06-01", "2026-06-06", stop_on_failure=True,
                      on_window=lambda w, n, t: seen.append(w.outcome))

    assert [entry.outcome for entry in result.windows] == ["blocked", "published", "failed", "not_run", "not_run"]
    assert result.counts() == {"published": 1, "skipped": 0, "blocked": 1, "failed": 1, "not_run": 2}
    assert result.exit_code == 1 and seen == ["blocked", "published", "failed"], "windows not run are not announced"
    assert sorted(_rows_per_day(tmp_path)) == ["2026-06-02"]
    assert all(entry.result is None for entry in result.windows[3:])
    rest = backfill(config, "2026-06-01", "2026-06-06")
    assert rest.counts() == {"published": 2, "skipped": 1, "blocked": 1, "failed": 1}, "without it, the rest goes on"


def test_a_window_that_cannot_even_start_fails_alone_and_the_others_go_on(tmp_path, monkeypatch, caplog):
    config = _project(tmp_path, days=3)
    caplog.set_level(logging.WARNING, logger="local_data_platform.orchestration.backfills")
    real, calls = backfill_module.run_workflow, []

    def refuse_the_second_window(source, **kwargs):
        calls.append(kwargs["window"])
        if len(calls) == 2:
            raise ConfigError("workflow orders is not valid, so no task ran:\n  - task 'orders': the file is wrong")
        return real(source, **kwargs)

    monkeypatch.setattr(backfill_module, "run_workflow", refuse_the_second_window)

    result = backfill(config, "2026-06-01", "2026-06-04")

    assert [entry.outcome for entry in result.windows] == ["published", "failed", "published"]
    assert result.windows[1].error.startswith("workflow orders is not valid") and result.windows[1].result is None
    assert result.windows[1].problems() == ["workflow orders is not valid, so no task ran:",
                                            "  - task 'orders': the file is wrong"]
    assert result.exit_code == 1 and sorted(_rows_per_day(tmp_path)) == ["2026-06-01", "2026-06-03"]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "could not start" in warning.getMessage() and _span(2) in warning.getMessage()


def test_a_programming_error_in_a_window_is_not_hidden(tmp_path, monkeypatch):
    config = _project(tmp_path, days=2)

    def broken(source, **kwargs):
        raise ZeroDivisionError("a bug")

    monkeypatch.setattr(backfill_module, "run_workflow", broken)

    with pytest.raises(ZeroDivisionError):
        backfill(config, "2026-06-01", "2026-06-03")


# ====================================================================== the range


def test_a_range_with_more_windows_than_the_limit_is_refused_before_anything_runs(tmp_path):
    config = _project(tmp_path, days=3)

    with pytest.raises(ConfigError, match=r"holds more than 5 windows of size day.*--max-windows"):
        backfill(config, "2026-06-01", "2026-06-10", max_windows=5)

    assert not (tmp_path / "warehouse").exists() and not _ledger(config).exists()
    exactly = backfill(config, "2026-06-01", "2026-06-04", max_windows=3, dry_run=True)
    assert len(exactly.windows) == 3, "a range of exactly the limit is fine"


def test_the_default_limit_is_a_thousand_windows(tmp_path):
    config = _project(tmp_path, days=1)

    assert DEFAULT_MAX_WINDOWS == 1000
    assert len(backfill(config, "2026-01-01", "2028-09-27", dry_run=True).windows) == 1000
    with pytest.raises(ConfigError, match="more than 1000 windows"):
        backfill(config, "2026-01-01", "2028-09-28", dry_run=True)


@pytest.mark.parametrize("limit", [0, -1, True, "5", 2.0, None])
def test_the_limit_is_a_whole_number_from_one(tmp_path, limit):
    config = _project(tmp_path, days=1)

    with pytest.raises(ConfigError, match="max_windows must be a whole number from 1"):
        backfill(config, "2026-06-01", "2026-06-02", max_windows=limit)


@pytest.mark.parametrize("start, end, message", [
    ("2026-06-01T06:00:00", "2026-06-04", r"the start of the range, 2026-06-01T06:00:00Z, does not start a window of "
                                          r"size day in UTC. Windows of that size start at midnight"),
    ("2026-06-01", "2026-06-04T12:00:00", r"the end of the range, 2026-06-04T12:00:00Z, does not start a window"),
    ("2026-06-04", "2026-06-04", "the range is empty: its start 2026-06-04T00:00:00Z is not before its end"),
    ("2026-06-05", "2026-06-01", "the range is empty"),
    ("June 1st", "2026-06-04", "the start of the range 'June 1st' is not an ISO-8601 date or datetime"),
    ("2026-06-01", "soon", "the end of the range 'soon' is not an ISO-8601"),
])
def test_a_range_that_is_empty_or_does_not_start_a_window_is_refused_and_nothing_runs(tmp_path, start, end, message):
    config = _project(tmp_path, days=3)

    with pytest.raises(ConfigError, match=message):
        backfill(config, start, end)

    assert not (tmp_path / "warehouse").exists() and not _ledger(config).exists()


def test_without_an_end_the_range_ends_with_the_last_complete_window(tmp_path):
    config = _project(tmp_path, days=3)
    now = dt.datetime(2026, 6, 4, 2, 5, tzinfo=UTC)

    result = backfill(config, "2026-06-01", now=now, dry_run=True)

    assert [entry.window for entry in result.windows] == [_span(1), _span(2), _span(3)]
    assert result.end == dt.datetime(2026, 6, 4, tzinfo=UTC)
    with pytest.raises(ConfigError, match="as no end was given"):
        backfill(config, "2026-06-04", now=now, dry_run=True)


def test_the_range_is_counted_in_the_time_zone_of_the_workflow_across_a_clock_change(tmp_path):
    march = dt.date(2026, 3, 28)
    config = _project(tmp_path, days=3, start=march, timezone="Europe/Paris")

    result = backfill(config, "2026-03-28", "2026-03-31")

    assert [entry.window for entry in result.windows] == [
        "2026-03-27T23:00:00Z/2026-03-28T23:00:00Z", "2026-03-28T23:00:00Z/2026-03-29T22:00:00Z",
        "2026-03-29T22:00:00Z/2026-03-30T22:00:00Z"], "local days, and the second is 23 hours long"
    assert [entry.keys["orders"] for entry in result.windows] == [
        plan(config, window=entry.window)["idempotency_key"] for entry in result.windows]
    assert result.counts()["published"] == 3 and result.timezone == "Europe/Paris"
    assert _rows_per_day(tmp_path) == {"2026-03-28": 2, "2026-03-29": 2, "2026-03-30": 2}


def test_a_backfill_by_the_hour_of_a_config_that_has_a_window_and_no_schedule_loads_each_row_once(tmp_path):
    rows = [f"{h * 10 + i},2026-06-01T{h:02d}:{15 * i:02d}:00" for h in range(6) for i in range(2)]
    _write(tmp_path / "landing" / "all.csv", "id,at\n" + "\n".join(rows) + "\n")
    config = _config(tmp_path, size="hour", schedule=None, min_rows=1, path="../landing/all.csv")

    result = backfill(config, "2026-06-01T00:00:00", "2026-06-01T06:00:00")

    assert len(result.windows) == 6 and result.window_size == "hour"
    assert result.counts()["published"] == 6 and result.exit_code == 0
    values = _orders(tmp_path).get().column("at").to_pylist()
    assert Counter(value.hour for value in values) == {h: 2 for h in range(6)} and len(values) == 12
    assert backfill(config, "2026-06-01T00:00:00", "2026-06-01T06:00:00").counts()["skipped"] == 6


def test_a_config_that_says_neither_a_window_nor_a_schedule_has_no_windows(tmp_path):
    config = _project(tmp_path, days=1, size=None, schedule=None, column=None)

    with pytest.raises(ConfigError, match=r"no metadata\.window and no schedule.*Set metadata\.window\.size"):
        backfill(config, "2026-06-01", "2026-06-02")


def test_a_config_with_a_schedule_that_does_not_say_how_long_a_window_is_asks_for_the_size(tmp_path):
    config = _project(tmp_path, days=1, size=None, schedule="5 2 * * *", column=None)

    with pytest.raises(ConfigError, match=r"does not say how long a window is. Set metadata\.window\.size"):
        backfill(config, "2026-06-01", "2026-06-02")


@pytest.mark.parametrize("schedule, when", [("@daily", None), ("0 0 * * *", None), (None, "daily")])
def test_a_daily_schedule_means_windows_of_a_day(tmp_path, schedule, when):
    config = _project(tmp_path, days=2, size=None, schedule=schedule, column=None, min_rows=None,
                      extra={"window": "none"})
    if when:
        data = json.loads(config.read_text())
        data["when"] = when
        config.write_text(json.dumps(data))

    result = backfill(config, "2026-06-01", "2026-06-03", dry_run=True)

    assert result.window_size == "day" and len(result.windows) == 2


# ====================================================================== replace


def test_replace_without_a_window_column_is_refused_before_anything_changes(tmp_path):
    config = _project(tmp_path, days=3, column=None)
    backfill(config, "2026-06-01", "2026-06-04")
    before = (_orders(tmp_path).table().metadata_location, _ledger(config).read_bytes(), _rows_per_day(tmp_path))

    for dry_run in (False, True):
        with pytest.raises(ConfigError, match=r"has no metadata\.window\.column, so replacing a window would add"):
            backfill(config, "2026-06-01", "2026-06-04", replace=True, dry_run=dry_run)

    after = (_orders(tmp_path).table().metadata_location, _ledger(config).read_bytes(), _rows_per_day(tmp_path))
    assert after == before


def test_replace_writes_every_window_again_under_the_replace_key_and_leaves_the_other_days_alone(tmp_path):
    config = _project(tmp_path, days=3)
    backfill(config, "2026-06-01", "2026-06-04")
    _drop(tmp_path, 2, rows=3, base=5000)  # day 2 changed at the source
    assert backfill(config, "2026-06-01", "2026-06-04").counts()["skipped"] == 3, "a plain run skips a window it has"
    assert _rows_per_day(tmp_path)["2026-06-02"] == 2

    replaced = backfill(config, "2026-06-02", "2026-06-03", replace=True)

    assert replaced.counts() == {"published": 1, "skipped": 0, "blocked": 0, "failed": 0}
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 3, "2026-06-03": 2}
    ids = sorted(_orders(tmp_path).get().column("id").to_pylist())
    assert ids == [100, 101, 300, 301, 5200, 5201, 5202], "the rows of day 2 were replaced, not added to"
    loaded = Config.from_json(config)
    keys = {row["idempotency_key"] for row in commit_log(_orders(tmp_path).table())}
    assert keys == {idempotency_key(loaded, _span(n)) for n in (1, 2, 3)} | {
        replace_key(idempotency_key(loaded, _span(2)), spec_hash(loaded))}


def test_a_window_that_was_replaced_under_the_same_spec_is_skipped_and_one_under_a_new_spec_is_replaced_again(tmp_path):
    config = _project(tmp_path, days=2)
    backfill(config, "2026-06-01", "2026-06-03")
    first = backfill(config, "2026-06-01", "2026-06-03", replace=True)
    snapshot = _orders(tmp_path).table().current_snapshot().snapshot_id

    again = backfill(config, "2026-06-01", "2026-06-03", replace=True)

    assert first.counts()["published"] == 2
    assert again.counts() == {"published": 0, "skipped": 2, "blocked": 0, "failed": 0}
    assert _orders(tmp_path).table().current_snapshot().snapshot_id == snapshot
    _drop(tmp_path, 1, rows=3, base=7000)
    _config(tmp_path, min_rows=1)  # a change to the spec: the quality check

    after_the_fix = backfill(config, "2026-06-01", "2026-06-03", replace=True)

    assert after_the_fix.counts()["published"] == 2, "the spec hash is part of the replace key"
    assert _rows_per_day(tmp_path) == {"2026-06-01": 3, "2026-06-02": 2}
    assert len(published_keys(_orders(tmp_path).table())) == 2 + 2 + 2, "the original, the first and the second keys"


def test_a_plain_backfill_after_a_replace_still_finds_the_original_keys(tmp_path):
    config = _project(tmp_path, days=2)
    backfill(config, "2026-06-01", "2026-06-03")
    backfill(config, "2026-06-01", "2026-06-03", replace=True)

    plain = backfill(config, "2026-06-01", "2026-06-03")

    assert plain.counts() == {"published": 0, "skipped": 2, "blocked": 0, "failed": 0}
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 2}


def test_replace_replaces_rows_whose_key_is_not_on_the_table_instead_of_adding_to_them(tmp_path):
    config = _project(tmp_path, days=2)
    before_the_rename = _config(tmp_path, identifier="orders_before_the_rename", name="old.json")
    backfill(before_the_rename, "2026-06-01", "2026-06-03")  # the rows are there, under the keys of another identifier
    assert backfill(config, "2026-06-01", "2026-06-02").counts()["published"] == 1  # a plain run adds day 1 again
    assert _rows_per_day(tmp_path) == {"2026-06-01": 4, "2026-06-02": 2}

    replaced = backfill(config, "2026-06-01", "2026-06-03", replace=True)

    assert replaced.counts()["published"] == 2
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 2}, "the duplicates are gone"


def test_replace_loads_a_window_that_has_no_rows_yet(tmp_path):
    config = _project(tmp_path, days=3)
    backfill(config, "2026-06-01", "2026-06-02")

    result = backfill(config, "2026-06-01", "2026-06-04", replace=True)

    assert result.counts()["published"] == 3 and _rows_per_day(tmp_path) == {
        "2026-06-01": 2, "2026-06-02": 2, "2026-06-03": 2}


def test_the_expired_key_warning_is_for_a_run_that_appends_and_not_for_one_that_replaces(tmp_path, caplog):
    config = _project(tmp_path, days=3)
    _drop(tmp_path, 0)  # day 0, the day before the first
    backfill(config, "2026-06-02", "2026-06-04")  # days 2 and 3 are on the table, so day 1 is older than every key
    caplog.set_level(logging.WARNING, logger="local_data_platform.pipeline")

    backfill(config, "2026-06-01", "2026-06-02", replace=True)

    assert not [r for r in caplog.records if "may have expired" in r.getMessage()], "a replace does not add rows"
    backfill(config, "2026-05-31", "2026-06-01")  # an append of an older window still warns
    assert [r for r in caplog.records if "may have expired" in r.getMessage()]


@pytest.mark.parametrize("mode, warns", [("append", True), ("overwrite", False)])
def test_the_expired_key_warning_follows_the_write_mode_that_the_config_sets(tmp_path, caplog, mode, warns):
    config = _project(tmp_path, days=3, mode=mode)
    _drop(tmp_path, 0)
    backfill(config, "2026-06-02", "2026-06-04")  # days 2 and 3 are on the table, so day 1 is older than every key
    caplog.set_level(logging.WARNING, logger="local_data_platform.pipeline")

    backfill(config, "2026-06-01", "2026-06-02")

    assert bool([r for r in caplog.records if "may have expired" in r.getMessage()]) is warns


def test_run_workflow_with_replace_writes_under_the_replace_key_and_the_plan_shows_it(tmp_path):
    config = _project(tmp_path, days=1)
    loaded = Config.from_json(config)
    expected = replace_key(idempotency_key(loaded, _span(1)), spec_hash(loaded))
    run_workflow(config, window=_span(1))

    planned = plan_workflow(config, window=_span(1), replace=True)
    replaced = run_workflow(config, window=_span(1), replace=True)
    again = run_workflow(config, window=_span(1), replace=True)

    [task] = planned["tasks"]
    assert task["idempotency_key"] == expected and task["replace"] is True and planned["valid"]
    assert replaced.tasks["orders"].status == "published"
    assert replaced.tasks["orders"].detail["idempotency_key"] == expected
    assert again.tasks["orders"].status == "skipped_duplicate"
    started = [json.loads(line) for line in _ledger(config).read_text().splitlines()
               if '"workflow.started"' in line]
    assert [event["payload"].get("replace") for event in started] == [None, True, True]


def test_the_plan_of_a_replace_needs_a_window_column_and_a_plain_plan_does_not(tmp_path):
    config = _project(tmp_path, days=1, column=None)

    assert plan_workflow(config, window=_span(1))["valid"] is True
    planned = plan_workflow(config, window=_span(1), replace=True)
    assert planned["valid"] is False and "metadata.window.column" in planned["errors"][0]
    assert "so replacing a window would add its rows again. Set window.column to the column that holds the time of " \
           "each row" in planned["errors"][0]
    with pytest.raises(ConfigError, match="is not valid, so no task ran"):
        run_workflow(config, window=_span(1), replace=True)
    assert not (tmp_path / "warehouse").exists()


# ====================================================================== workflows


def _export_config(root: Path) -> Path:
    """A pipeline that writes the orders table to a CSV file, which a file cannot take a key for."""
    return _write(root / "config" / "export.json", {"identifier": "export_orders", "metadata": {
        "source": {"name": "orders", "format": "ICEBERG", "catalog": CATALOG},
        "target": {"name": "orders_export", "format": "CSV", "path": "../out/orders.csv"}}})


def _workflow(root: Path, tasks: list | None = None, **metadata) -> Path:
    _export_config(root)
    tasks = tasks if tasks is not None else [
        {"id": "orders", "pipeline": "config/orders.json"},
        {"id": "export", "pipeline": "config/export.json", "depends_on": ["orders"]},
        {"id": "tidy", "maintain": {"config": "config/orders.json"}, "depends_on": ["export"]}]
    return _write(root / "daily.json", {
        "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders",
        "metadata": {"schedule": "@daily", "window": {"size": "day"}, **metadata}, "tasks": tasks})


def test_a_backfill_of_a_workflow_runs_every_task_for_every_window_and_skips_a_window_the_ledger_says_is_done(tmp_path):
    _project(tmp_path, days=3)
    workflow = _workflow(tmp_path)

    first = backfill(workflow, "2026-06-01", "2026-06-04")

    assert first.counts()["published"] == 3 and first.exit_code == 0
    assert [list(entry.result.tasks) for entry in first.windows] == [["orders", "export", "tidy"]] * 3
    assert list(first.windows[0].keys) == ["orders"], "only the pipeline that writes to Iceberg has a key"
    assert (tmp_path / "out" / "orders.csv").is_file()
    ledger = _ledger(workflow).read_bytes()

    again = backfill(workflow, "2026-06-01", "2026-06-04")

    assert again.counts() == {"published": 0, "skipped": 3, "blocked": 0, "failed": 0}
    assert _ledger(workflow).read_bytes() == ledger, "the keys and the ledger say the windows are done"


def test_a_window_whose_ledger_is_gone_runs_again_and_every_task_that_has_a_key_is_skipped(tmp_path):
    _project(tmp_path, days=2)
    workflow = _workflow(tmp_path)
    backfill(workflow, "2026-06-01", "2026-06-03")
    _ledger(workflow).unlink()

    dry = backfill(workflow, "2026-06-01", "2026-06-03", dry_run=True)
    run = backfill(workflow, "2026-06-01", "2026-06-03")

    assert [entry.state for entry in dry.windows] == ["partial", "partial"], "the keys are there, the ledger is not"
    assert run.counts()["published"] == 2, "the export writes its file again"
    assert [entry.result.tasks["orders"].status for entry in run.windows] == ["skipped_duplicate"] * 2
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 2}
    assert backfill(workflow, "2026-06-01", "2026-06-03").counts()["skipped"] == 2, "the ledger is back"


def test_a_maintain_task_is_not_something_the_ledger_has_to_vouch_for(tmp_path):
    _project(tmp_path, days=2)
    workflow = _workflow(tmp_path, tasks=[{"id": "orders", "pipeline": "config/orders.json"},
                                          {"id": "tidy", "maintain": {"config": "config/orders.json"},
                                           "depends_on": ["orders"]}])
    first = backfill(workflow, "2026-06-01", "2026-06-03")
    _ledger(workflow).unlink()

    dry = backfill(workflow, "2026-06-01", "2026-06-03", dry_run=True)
    again = backfill(workflow, "2026-06-01", "2026-06-03")

    assert first.counts()["published"] == 2
    assert [entry.state for entry in dry.windows] == ["published", "published"], "the keys are all it needs"
    assert again.counts() == {"published": 0, "skipped": 2, "blocked": 0, "failed": 0}
    assert not _ledger(workflow).exists(), "a window that is skipped writes nothing"


def test_a_task_that_failed_after_the_load_is_run_again_alone(tmp_path):
    _project(tmp_path, days=2)
    workflow = _workflow(tmp_path)
    (tmp_path / "out").write_text("a file where the folder of the export should be")

    first = backfill(workflow, "2026-06-01", "2026-06-03")

    assert first.counts()["failed"] == 2 and first.exit_code == 1
    assert [entry.result.tasks["orders"].status for entry in first.windows] == ["published", "published"]
    assert [entry.result.tasks["export"].status for entry in first.windows] == ["failed", "failed"]
    assert [entry.result.tasks["tidy"].status for entry in first.windows] == ["upstream_failed"] * 2
    (tmp_path / "out").unlink()
    dry = backfill(workflow, "2026-06-01", "2026-06-03", dry_run=True)
    assert [entry.state for entry in dry.windows] == ["failed", "failed"], "the load is on main, the ledger says failed"

    fixed = backfill(workflow, "2026-06-01", "2026-06-03")

    assert fixed.counts() == {"published": 2, "skipped": 0, "blocked": 0, "failed": 0}
    assert [entry.result.tasks["orders"].status for entry in fixed.windows] == ["skipped_duplicate"] * 2
    assert (tmp_path / "out" / "orders.csv").is_file() and _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 2}


def test_a_window_where_only_some_of_the_tables_were_loaded_is_partial_and_the_rest_is_loaded(tmp_path):
    config = _project(tmp_path, days=2)
    copy = _config(tmp_path, name="copy.json", identifier="orders_copy", table="orders_copy")
    workflow = _workflow(tmp_path, tasks=[{"id": "orders", "pipeline": "config/orders.json"},
                                          {"id": "copy", "pipeline": "config/copy.json"}])
    backfill(config, "2026-06-01", "2026-06-03")  # only the first table

    dry = backfill(workflow, "2026-06-01", "2026-06-03", dry_run=True)
    run = backfill(workflow, "2026-06-01", "2026-06-03")

    assert [entry.state for entry in dry.windows] == ["partial", "partial"]
    assert set(dry.windows[0].keys) == {"orders", "copy"}
    assert [entry.result.tasks["orders"].status for entry in run.windows] == ["skipped_duplicate"] * 2
    assert [entry.result.tasks["copy"].status for entry in run.windows] == ["published"] * 2
    copied = Iceberg("orders_copy", {"identifier": "shop", "warehouse_path": str(tmp_path / "warehouse")})
    assert copied.row_count() == 4 and backfill(workflow, "2026-06-01", "2026-06-03").counts()["skipped"] == 2
    assert copy.exists()


def test_a_replace_of_a_workflow_replaces_the_rows_and_writes_the_file_again(tmp_path):
    _project(tmp_path, days=2)
    workflow = _workflow(tmp_path)
    backfill(workflow, "2026-06-01", "2026-06-03")
    _drop(tmp_path, 2, rows=3, base=5000)

    result = backfill(workflow, "2026-06-01", "2026-06-03", replace=True)

    assert result.counts()["published"] == 2
    assert _rows_per_day(tmp_path) == {"2026-06-01": 2, "2026-06-02": 3}
    assert result.windows[0].keys != backfill(workflow, "2026-06-01", "2026-06-03", dry_run=True).windows[0].keys
    assert backfill(workflow, "2026-06-01", "2026-06-03", replace=True).counts()["skipped"] == 2


def test_a_replace_of_a_workflow_with_a_pipeline_that_has_no_window_column_does_not_start(tmp_path):
    _project(tmp_path, days=1, column=None)
    workflow = _workflow(tmp_path)
    backfill(workflow, "2026-06-01", "2026-06-02")
    ledger = _ledger(workflow).read_bytes()

    with pytest.raises(ConfigError, match=r"task 'orders': config/orders\.json has no metadata\.window\.column"):
        backfill(workflow, "2026-06-01", "2026-06-02", replace=True)

    assert _ledger(workflow).read_bytes() == ledger


def test_a_backfill_of_a_workflow_that_is_not_valid_does_not_start(tmp_path):
    _project(tmp_path, days=1)
    workflow = _workflow(tmp_path, tasks=[{"id": "orders", "pipeline": "config/missing.json"}])

    with pytest.raises(ConfigError, match=r"workflow daily_orders is not valid, so no window ran"):
        backfill(workflow, "2026-06-01", "2026-06-02")

    assert not _ledger(workflow).exists()


def _dbt_workflow(root: Path, fake: FakeDbt, *, with_orders: bool = True) -> Path:
    project = write_dbt_project(root / "dbt", {"m": "select 1 as x"})
    _write(project / "ldp_dbt.json", {"sources": {"configs": ["../config"]},
                                      "publish": {"catalog": {"identifier": "gold", "warehouse_path": "../warehouse"},
                                                  "config_dir": "target/ldp"}})
    manifest, results = dbt_artifacts(dbt_model("m"))  # a model that is not marked for publishing
    fake.will(database=str(root / "dbt" / "target" / "ldp_build.duckdb"),
              sql=["create or replace table main.m as select 1 as x"],
              artifacts={"manifest.json": manifest, "run_results.json": results})
    build = {"id": "build", "dbt": {"project": "dbt", "dbt_bin": str(fake.path)}}
    if with_orders:
        return _workflow(root, tasks=[{"id": "orders", "pipeline": "config/orders.json"},
                                      {**build, "depends_on": ["orders"]}])
    return _workflow(root, tasks=[build])


def test_a_backfill_builds_a_dbt_task_for_every_window_and_a_replace_builds_it_again(tmp_path):
    _project(tmp_path, days=2)
    fake = FakeDbt(tmp_path / "fake")
    workflow = _dbt_workflow(tmp_path, fake)

    first = backfill(workflow, "2026-06-01", "2026-06-03")

    starts = [json.loads(call["argv"][call["argv"].index("--vars") + 1])["window_start"] for call in fake.calls]
    assert starts == ["2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"] and first.counts()["published"] == 2
    again = backfill(workflow, "2026-06-01", "2026-06-03")
    assert again.counts()["skipped"] == 2 and len(fake.calls) == 2, "built windows are skipped"

    replaced = backfill(workflow, "2026-06-01", "2026-06-03", replace=True)

    assert replaced.counts()["published"] == 2 and len(fake.calls) == 4, "a replace builds the models again"
    assert [entry.result.tasks["build"].status for entry in replaced.windows] == ["succeeded"] * 2


def test_a_workflow_with_no_key_at_all_is_done_by_its_ledger_and_a_replace_runs_it_every_time(tmp_path):
    _project(tmp_path, days=2)
    fake = FakeDbt(tmp_path / "fake")
    workflow = _dbt_workflow(tmp_path, fake, with_orders=False)

    def states(**kwargs):
        return [entry.state for entry in backfill(workflow, "2026-06-01", "2026-06-03", dry_run=True, **kwargs).windows]

    assert states() == ["missing", "missing"]

    first = backfill(workflow, "2026-06-01", "2026-06-03")

    assert first.counts()["published"] == 2 and len(fake.calls) == 2 and first.windows[0].keys == {}
    assert states() == ["published", "published"], "the ledger says that every task succeeded"
    assert backfill(workflow, "2026-06-01", "2026-06-03").counts()["skipped"] == 2 and len(fake.calls) == 2
    assert states(replace=True) == ["missing", "missing"], "nothing shows that a replace was done"
    assert backfill(workflow, "2026-06-01", "2026-06-03", replace=True).counts()["published"] == 2
    assert len(fake.calls) == 4
    assert backfill(workflow, "2026-06-01", "2026-06-03", replace=True).counts()["published"] == 2
    assert len(fake.calls) == 6, "a workflow with no key cannot tell that it replaced a window, so it does every time"


def test_a_window_that_ldp_workflow_run_published_is_done_for_a_backfill(tmp_path):
    config = _project(tmp_path, days=2)
    run_workflow(config, window=_span(1))

    dry = backfill(config, "2026-06-01", "2026-06-03", dry_run=True)
    run = backfill(config, "2026-06-01", "2026-06-03")

    assert [entry.state for entry in dry.windows] == ["published", "missing"]
    assert [entry.outcome for entry in run.windows] == ["skipped", "published"]


# ====================================================================== the command line


def _ldp(capsys, *argv) -> tuple[int, str, str]:
    status = main(["backfill", *[str(item) for item in argv]])
    out, err = capsys.readouterr()
    return status, out, err


def test_ldp_backfill_prints_a_line_for_each_window_that_ran_and_a_summary(tmp_path, capsys):
    config = _project(tmp_path, days=3)

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04")

    lines = out.splitlines()
    assert status == 0 and err == ""
    assert lines[:3] == [f"[1/3] {_span(1)}  published", f"[2/3] {_span(2)}  published",
                         f"[3/3] {_span(3)}  published"]
    assert lines[3].startswith("backfill orders: 3 published, 0 skipped, 0 blocked, 0 failed in ")
    assert lines[3].endswith(f"s. Events: {_ledger(config)}") and len(lines) == 4


def test_ldp_backfill_run_again_lists_only_the_windows_it_did_and_counts_the_ones_it_skipped(tmp_path, capsys):
    config = _project(tmp_path, days=3)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()
    _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04")
    _drop(tmp_path, 2)

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04")

    lines = out.splitlines()
    assert status == 0 and lines[0] == f"[2/3] {_span(2)}  published", "the window that was done is not listed"
    assert lines[1].startswith("backfill orders: 1 published, 2 skipped, 0 blocked, 0 failed in ") and err == ""


def test_ldp_backfill_names_what_went_wrong_in_a_failed_window_and_exits_with_one(tmp_path, capsys):
    config = _project(tmp_path, days=3)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04")

    assert status == 1
    assert f"[2/3] {_span(2)}  failed" in out.splitlines()
    assert "  orders: FileNotFoundError: CSV file not found" in err
    assert err.splitlines()[-1] == "Fix the cause and run the same command again: the windows that are done are skipped"
    assert "2 published, 0 skipped, 0 blocked, 1 failed" in out


def test_ldp_backfill_exits_with_three_when_only_a_data_gate_held_a_window_back(tmp_path, capsys):
    config = _project(tmp_path, days=3)
    _drop(tmp_path, 3, rows=1)

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04")

    assert status == 3 and f"[3/3] {_span(3)}  blocked" in out.splitlines()
    assert ("  orders: DataQualityError: 1 of 1 data quality checks failed:\n"
            "    - row_count: 1 row, expected >= 2\n") in err
    assert "2 published, 0 skipped, 1 blocked, 0 failed" in out


def test_ldp_backfill_dry_run_prints_a_table_and_runs_nothing(tmp_path, capsys):
    config = _project(tmp_path, days=3)
    backfill(config, "2026-06-01", "2026-06-02")

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04", "--dry-run")

    lines = out.splitlines()
    assert status == 0 and err == ""
    assert lines[0].split() == ["window", "key", "state"]
    keys = [plan(config, window=_span(n))["idempotency_key"][:12] for n in (1, 2, 3)]
    assert [line.split() for line in lines[2:5]] == [
        [_span(1), keys[0], "published"], [_span(2), keys[1], "missing"], [_span(3), keys[2], "missing"]]
    assert lines[-1] == "3 windows of size day, oldest first: 1 published, 2 missing. Nothing was run."
    assert len(_ran_windows(config)) == 1, "only the first backfill ran a window"


def test_ldp_backfill_dry_run_of_several_keys_shows_the_first_and_how_many_more(tmp_path, capsys):
    _project(tmp_path, days=1)
    _config(tmp_path, name="copy.json", identifier="orders_copy", table="orders_copy")
    workflow = _workflow(tmp_path, tasks=[{"id": "orders", "pipeline": "config/orders.json"},
                                          {"id": "copy", "pipeline": "config/copy.json"}])

    status, out, _ = _ldp(capsys, workflow, "--from", "2026-06-01", "--to", "2026-06-02", "--dry-run")

    row = out.splitlines()[2].split()
    assert status == 0 and row[0] == _span(1) and row[2:] == ["(+1)", "missing"]


def test_ldp_backfill_json_prints_the_result_and_nothing_else(tmp_path, capsys):
    config = _project(tmp_path, days=2)

    status, out, _ = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-03", "--json")

    printed = json.loads(out)
    assert status == 0 and printed["status"] == "succeeded" and printed["exit_code"] == 0
    assert printed["counts"] == {"published": 2, "skipped": 0, "blocked": 0, "failed": 0}
    assert (printed["workflow"], printed["window_size"], printed["timezone"], printed["order"]) == (
        "orders", "day", "UTC", "oldest first")
    assert (printed["from"], printed["to"]) == ("2026-06-01T00:00:00Z", "2026-06-03T00:00:00Z")
    assert [w["window"] for w in printed["windows"]] == [_span(1), _span(2)]
    assert printed["windows"][0]["tasks"] == [{"id": "orders", "kind": "pipeline", "status": "published",
                                              "attempts": 1, "error": None}]
    assert len(printed["windows"][0]["keys"]["orders"]) == 64 and printed["replace"] is False
    status, out, _ = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-03", "--json", "--dry-run")
    assert json.loads(out)["status"] == "dry_run" and json.loads(out)["states"]["published"] == 2


def test_ldp_backfill_run_backwards_lists_the_newest_window_first(tmp_path, capsys):
    config = _project(tmp_path, days=3)

    status, out, _ = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04", "--run-backwards")

    assert status == 0 and out.splitlines()[:3] == [f"[1/3] {_span(3)}  published", f"[2/3] {_span(2)}  published",
                                                    f"[3/3] {_span(1)}  published"]


def test_ldp_backfill_replace_and_stop_on_failure(tmp_path, capsys):
    config = _project(tmp_path, days=3)
    _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04")
    _drop(tmp_path, 3, rows=3, base=5000)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-04", "--replace",
                            "--stop-on-failure")

    assert status == 1
    assert out.splitlines()[:2] == [f"[1/3] {_span(1)}  published", f"[2/3] {_span(2)}  failed"]
    assert "1 published, 0 skipped, 0 blocked, 1 failed, 1 not run" in out
    assert _rows_per_day(tmp_path)["2026-06-03"] == 2, "the third day was not reached"


def test_ldp_backfill_refuses_a_range_that_is_too_large_with_one_line(tmp_path, capsys):
    config = _project(tmp_path, days=1)

    status, out, err = _ldp(capsys, config, "--from", "2026-01-01", "--to", "2026-06-01", "--max-windows", "10")

    assert status == 1 and out == ""
    assert err.splitlines() == ["ldp: error: the range from 2026-01-01T00:00:00Z to 2026-06-01T00:00:00Z holds more "
                                "than 10 windows of size day. Check the start and the end, or pass a larger "
                                "max_windows (--max-windows)"]


@pytest.mark.parametrize("argv", [["--from", "2026-06-01", "--max-windows", "0"], ["--max-windows", "ten"],
                                  ["--to", "2026-06-02"], []])
def test_ldp_backfill_reports_a_wrong_command_line_with_exit_status_one(tmp_path, capsys, argv):
    config = _project(tmp_path, days=1)

    status, out, err = _ldp(capsys, config, *argv)

    assert status == 1 and "usage: ldp backfill" in err and not (tmp_path / "warehouse").exists()


def test_ldp_backfill_of_a_file_that_is_not_there_exits_with_one(tmp_path, capsys):
    status, out, err = _ldp(capsys, tmp_path / "nothing.json", "--from", "2026-06-01", "--to", "2026-06-02")

    assert status == 1 and "not found" in err


def test_ldp_backfill_says_how_far_it_got_when_it_is_interrupted(tmp_path, capsys, monkeypatch):
    config = _project(tmp_path, days=5)
    ExtractSpy(monkeypatch, fail_on={3}, error=KeyboardInterrupt())

    status, out, err = _ldp(capsys, config, "--from", "2026-06-01", "--to", "2026-06-06")

    assert status == 130
    assert out.splitlines() == [f"[1/5] {_span(1)}  published", f"[2/5] {_span(2)}  published"]
    assert ("interrupted after 2 windows (2 published, 0 skipped, 0 blocked, 0 failed). Run the same command again to "
            "resume") in err
    assert "ldp: interrupted" in err


def test_ldp_backfill_takes_a_workflow_file_and_a_replace_of_it(tmp_path, capsys):
    _project(tmp_path, days=2)
    workflow = _workflow(tmp_path)

    status, out, _ = _ldp(capsys, workflow, "--from", "2026-06-01", "--to", "2026-06-03")
    again, out_again, _ = _ldp(capsys, workflow, "--from", "2026-06-01", "--to", "2026-06-03")
    replaced, out_replaced, _ = _ldp(capsys, workflow, "--from", "2026-06-01", "--to", "2026-06-03", "--replace")

    assert (status, again, replaced) == (0, 0, 0)
    assert "backfill daily_orders: 2 published" in out and "2 skipped" in out_again and "2 published" in out_replaced


def test_ldp_help_lists_the_backfill_command_and_its_options(capsys):
    assert main(["backfill", "--help"]) == 0

    help_text = capsys.readouterr().out
    for option in ("--from", "--to", "--dry-run", "--replace", "--run-backwards", "--max-windows", "--stop-on-failure",
                   "--json"):
        assert option in help_text
    assert "Exit status is 0 when no window failed or was blocked, 3 when a data gate" in help_text


# ====================================================================== the Python API


def test_orchestration_backfill_is_the_function_however_often_it_is_read():
    import local_data_platform.orchestration as orchestration

    first = orchestration.backfill
    import local_data_platform.orchestration.backfills  # noqa: F401 - importing the module must not shadow the function

    assert callable(first) and orchestration.backfill is first is backfill_module.backfill
    assert orchestration.BackfillResult is BackfillResult and orchestration.BackfillWindow is BackfillWindow
    assert "backfill" in dir(orchestration) and "backfill" in orchestration.__all__


def _result(outcomes, *, dry_run=False, states=None) -> BackfillResult:
    start = dt.datetime(2026, 6, 1, tzinfo=UTC)
    entries = [BackfillWindow(start + dt.timedelta(days=n), start + dt.timedelta(days=n + 1),
                              (states or {}).get(n, "missing"), {}, outcome) for n, outcome in enumerate(outcomes)]
    end = start + dt.timedelta(days=len(entries))
    return BackfillResult("w", Path("/x/.ldp/events.jsonl"), "day", "UTC", start, end, "oldest first", False, dry_run,
                          entries)


@pytest.mark.parametrize("outcomes, status, code", [
    (["published", "skipped"], "succeeded", 0),
    ([], "succeeded", 0),
    (["published", "blocked"], "blocked_quality", 3),
    (["blocked", "blocked"], "blocked_quality", 3),
    (["failed"], "failed", 1),
    (["blocked", "failed", "published"], "failed", 1),
    (["failed", "not_run"], "failed", 1),
])
def test_the_result_maps_its_windows_to_a_status_and_an_exit_code(outcomes, status, code):
    result = _result(outcomes)

    assert (result.status, result.exit_code) == (status, code)


def test_a_dry_run_exits_with_zero_whatever_the_states_are():
    result = _result([None, None], dry_run=True, states={0: "failed", 1: "blocked"})

    assert (result.status, result.exit_code) == ("dry_run", 0)
    assert result.states() == {"published": 0, "partial": 0, "missing": 0, "failed": 1, "blocked": 1}


def test_the_counts_name_the_windows_that_were_not_run_only_when_there_are_some():
    assert _result(["published", "skipped", "skipped"]).counts() == {"published": 1, "skipped": 2, "blocked": 0,
                                                                     "failed": 0}
    assert _result(["failed", "not_run", "not_run"]).counts() == {"published": 0, "skipped": 0, "blocked": 0,
                                                                  "failed": 1, "not_run": 2}


def test_the_result_is_json_serialisable_and_says_what_it_holds(tmp_path):
    config = _project(tmp_path, days=2)
    (tmp_path / "landing" / f"orders_{_day(2)}.csv").unlink()
    result = backfill(config, "2026-06-01", "2026-06-03")

    printed = json.loads(json.dumps(result.to_dict()))

    assert printed["counts"]["failed"] == 1 and printed["windows"][1]["outcome"] == "failed"
    assert printed["windows"][1]["tasks"][0]["error"].startswith("FileNotFoundError")
    assert printed["ledger"] == str(_ledger(config)) and printed["duration_s"] >= 0
    assert printed["windows"][0]["workflow_run_id"] == result.windows[0].result.run_id
    assert printed["windows"][0]["tasks"] == [{"id": "orders", "kind": "pipeline", "status": "published",
                                               "attempts": 1, "error": None}]


def test_the_problems_of_a_window_are_the_lines_of_each_task_that_failed_or_was_blocked(tmp_path):
    config = _project(tmp_path, days=3)
    _drop(tmp_path, 2, rows=1)
    (tmp_path / "landing" / f"orders_{_day(3)}.csv").unlink()

    result = backfill(config, "2026-06-01", "2026-06-04")

    assert result.windows[0].problems() == []
    assert result.windows[1].problems() == ["orders: DataQualityError: 1 of 1 data quality checks failed:",
                                            "  - row_count: 1 row, expected >= 2"]
    assert result.windows[2].problems()[0].startswith("orders: FileNotFoundError: CSV file not found")
    assert all("\n" not in problem for window in result.windows for problem in window.problems())


@pytest.mark.parametrize("message, expected", [
    ("boom", ["boom"]),
    ("boom\n  at line 3\n  at line 9", ["boom"]),
    ("2 checks failed:\n  - a: 1\n  - b: 2", ["2 checks failed:", "  - a: 1", "  - b: 2"]),
    ("2 checks failed:\n  - a: 1\nsee the log", ["2 checks failed:", "  - a: 1"]),
    ("2 checks failed: \n  - a: 1", ["2 checks failed: ", "  - a: 1"]),
    ("failed:", ["failed:"]),
    ("", [""]),
])
def test_a_message_gives_its_headline_and_the_list_under_a_headline_that_ends_in_a_colon(message, expected):
    start = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)

    assert BackfillWindow(start, start + dt.timedelta(days=1), "missing", error=message).problems() == expected


# ====================================================================== the search of a batch


def test_a_backfill_gives_its_windows_a_key_search_that_fits_the_whole_batch(tmp_path, monkeypatch):
    from local_data_platform.format.iceberg.commit import CommitPolicy

    config = _project(tmp_path, days=4)
    real, seen = backfill_module.run_workflow, []

    def spy(source, **kwargs):
        seen.append(kwargs)
        return real(source, **kwargs)

    monkeypatch.setattr(backfill_module, "run_workflow", spy)

    backfill(config, "2026-06-01", "2026-06-05", policy=CommitPolicy(max_search_depth=40))

    assert len(seen) == 4
    assert len({(kw["policy"], kw["search_since_ms"]) for kw in seen}) == 1, "one search for the whole batch"
    assert seen[0]["policy"].max_search_depth == 40 + 4 * 2 + 1, "the base depth, two snapshots a window, and the init"
    now_ms = dt.datetime.now(UTC).timestamp() * 1000
    assert abs(seen[0]["search_since_ms"] - (now_ms - 600_000)) < 120_000, "the start of the batch less the skew"
    _drop(tmp_path, 5)
    seen.clear()
    backfill(config, "2026-06-01", "2026-06-06")  # one window is left to run, and four are skipped
    assert len(seen) == 1 and seen[0]["policy"].max_search_depth == CommitPolicy().max_search_depth + 2 + 1


def test_every_window_is_run_with_the_arguments_of_the_backfill(tmp_path, monkeypatch):
    config = _project(tmp_path, days=2)
    real, seen = backfill_module.run_workflow, []
    waits = []
    monkeypatch.setattr(backfill_module, "run_workflow", lambda source, **kw: (seen.append(kw), real(source, **kw))[1])

    backfill(config, "2026-06-01", "2026-06-03", sleep=waits.append, rng="the generator", replace=True)

    assert [kw["window"] for kw in seen] == [(dt.datetime(2026, 6, 1, tzinfo=UTC), dt.datetime(2026, 6, 2, tzinfo=UTC)),
                                             (dt.datetime(2026, 6, 2, tzinfo=UTC), dt.datetime(2026, 6, 3, tzinfo=UTC))]
    assert all(kw["sleep"] == waits.append and kw["rng"] == "the generator" and kw["replace"] is True for kw in seen)


def test_a_task_that_is_retried_inside_a_backfilled_window_waits_as_the_workflow_says(tmp_path, monkeypatch):
    config = _project(tmp_path, days=2, extra={"retries": {"max_attempts": 3, "delay_s": 10}})
    ExtractSpy(monkeypatch, fail_on={1}, error=OSError("a blip"))
    waits = []

    result = backfill(config, "2026-06-01", "2026-06-03", sleep=waits.append)

    assert result.counts()["published"] == 2 and len(waits) == 1 and 0 <= waits[0] <= 10
    assert result.windows[0].result.tasks["orders"].attempts == 2
    assert [entry.to_dict()["tasks"][0]["attempts"] for entry in result.windows] == [2, 1]


# ====================================================================== the ledger


def _event(kind, run_id, workflow="w", window=None, tasks=None):
    payload = {"workflow": workflow}
    if window is not None:
        payload["window"] = window
    if tasks is not None:
        payload["tasks"] = tasks
    return json.dumps({"type": kind, "run_id": run_id, "payload": payload})


A_WINDOW = ["2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"]
ANOTHER = ["2026-06-02T00:00:00Z", "2026-06-03T00:00:00Z"]


def _workflow_with_ledger(tmp_path, *lines):
    path = _workflow(tmp_path, tasks=[{"id": "orders", "pipeline": "config/orders.json"}])
    _config(tmp_path)
    data = json.loads(path.read_text())
    data["identifier"] = "w"
    path.write_text(json.dumps(data))
    _write(tmp_path / ".ldp" / "events.jsonl", "\n".join(lines) + "\n")
    return load_workflow(path)


def test_the_ledger_gives_the_last_run_of_each_window_of_this_workflow_only(tmp_path):
    workflow = _workflow_with_ledger(
        tmp_path,
        _event("workflow.started", "a", window=A_WINDOW),
        _event("workflow.finished", "a", tasks={"orders": "failed"}),
        _event("workflow.started", "b", window=A_WINDOW),
        _event("workflow.finished", "b", tasks={"orders": "published"}),
        _event("workflow.started", "c", workflow="other", window=ANOTHER),
        _event("workflow.finished", "c", workflow="other", tasks={"orders": "failed"}),
        _event("workflow.started", "d", window=ANOTHER),  # a run that never finished
        _event("run.finished", "e", tasks={"orders": "failed"}),  # an event of a task
    )

    assert _read_ledger(workflow) == {tuple(A_WINDOW): {"orders": "published"}}


def test_a_ledger_with_junk_in_it_is_read_as_far_as_it_goes(tmp_path):
    workflow = _workflow_with_ledger(
        tmp_path, "", "not json at all but it says workflow.started", '{"payload": 5} "workflow.finished"',
        '{"type": "workflow.started"} "workflow.', _event("workflow.started", "a", window=A_WINDOW)[:40],
        '["workflow.started"]', '"workflow.finished"', '{"type": "workflow.started", "run_id": "x"}',
        '{"type": "workflow.finished", "payload": {"workflow": "w"}}',
        '{"type": "workflow.started", "run_id": "y", "payload": 5}',
        _event("workflow.started", "b", window=["only one end"]),
        _event("workflow.finished", "b", tasks={"orders": "failed"}),
        _event("workflow.started", "g", window=A_WINDOW), _event("workflow.finished", "g", tasks="not a mapping"),
        _event("workflow.started", "ok", window=ANOTHER),
        _event("workflow.finished", "ok", tasks={"orders": "published"}))

    assert _read_ledger(workflow) == {tuple(ANOTHER): {"orders": "published"}}


def test_a_workflow_with_no_ledger_has_no_runs(tmp_path):
    workflow = load_workflow(_workflow(tmp_path))

    assert _read_ledger(workflow) == {}


@pytest.mark.parametrize("tasks, expected", [
    (None, None),
    ({"a": "published", "b": "succeeded"}, "succeeded"),
    ({"a": "skipped_duplicate", "b": "succeeded"}, "succeeded"),
    ({"a": "published", "b": "failed"}, "failed"),
    ({"a": "failed", "b": "blocked_quality"}, "failed"),
    ({"a": "published", "b": "blocked_quality"}, "blocked"),
    ({"a": "published", "b": "upstream_failed"}, "incomplete"),
    ({"a": "published"}, "incomplete"),
    ({}, "incomplete"),
])
def test_what_the_last_run_of_a_window_says(tasks, expected):
    assert _ledger_status(tasks, ["a", "b"]) == expected


@pytest.mark.parametrize("tasks, expected", [
    ([("orders", "pipeline", "published")], "published"),
    ([("orders", "pipeline", "published"), ("export", "pipeline", "succeeded")], "published"),
    ([("orders", "pipeline", "skipped_duplicate"), ("export", "pipeline", "succeeded")], "published"),
    ([("orders", "pipeline", "skipped_duplicate")], "skipped"),
    ([("orders", "pipeline", "skipped_duplicate"), ("tidy", "maintain", "succeeded")], "skipped"),
    ([("orders", "pipeline", "published"), ("export", "pipeline", "failed")], "failed"),
    ([("orders", "pipeline", "blocked_quality"), ("export", "pipeline", "upstream_failed")], "blocked"),
    ([("orders", "pipeline", "failed"), ("export", "pipeline", "blocked_quality")], "failed"),
])
def test_how_a_window_ended_is_told_from_the_results_of_its_tasks(tasks, expected):
    start = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    result = WorkflowResult("orders_daily", "run-1", (start, start + dt.timedelta(days=1)), start,
                            tasks={name: TaskResult(name, kind, status) for name, kind, status in tasks})

    assert _outcome_of(result) == expected


def test_a_window_run_before_a_task_was_added_to_the_workflow_is_not_done(tmp_path):
    _project(tmp_path, days=1)
    workflow = _workflow(tmp_path, tasks=[{"id": "orders", "pipeline": "config/orders.json"},
                                          {"id": "export", "pipeline": "config/export.json",
                                           "depends_on": ["orders"]}])
    backfill(workflow, "2026-06-01", "2026-06-02")
    data = json.loads(workflow.read_text())
    data["tasks"].append({"id": "export_again", "pipeline": "config/export.json", "depends_on": ["orders"]})
    workflow.write_text(json.dumps(data))

    dry = backfill(workflow, "2026-06-01", "2026-06-02", dry_run=True)

    assert [entry.state for entry in dry.windows] == ["partial"]
    assert backfill(workflow, "2026-06-01", "2026-06-02").windows[0].result.tasks["export_again"].status == "published"


def test_the_config_of_a_workflow_with_a_window_and_no_schedule_is_a_workflow_for_a_backfill_only(tmp_path):
    config = _project(tmp_path, days=1, schedule=None)

    assert load_workflow(config, require_schedule=False).schedule is None
    with pytest.raises(ConfigError, match="is a dataset config with no schedule, so it is not a workflow"):
        load_workflow(config)
    assert dataclasses.is_dataclass(backfill(config, "2026-06-01", "2026-06-02", dry_run=True))
