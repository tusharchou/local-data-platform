"""etl.run_config: run a config from a path, a Config or a dict."""

import dataclasses
import datetime as dt
import json
import logging
import subprocess
import sys
from pathlib import Path

import pyarrow.csv as pa_csv
import pytest

from local_data_platform import Config
from local_data_platform.etl import batch_search, commit_context, load_config, run_config
from local_data_platform.events import MemorySink, summarize_runs, uuid7
from local_data_platform.exceptions import ConfigError, DataQualityError, PipelineNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import CommitContext, CommitPolicy, CommitSearchExhausted
from local_data_platform.pipeline import PipelineResult

REPO_ROOT = Path(__file__).resolve().parents[1]


def _metadata(write_mode: str = "append", checks: list | None = None) -> dict:
    return {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"},
                   "write_mode": write_mode},
        "quality": {"on_failure": "fail", "checks": checks or []},
    }


@pytest.fixture
def config_path(tmp_path, sample_table) -> Path:
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    path = tmp_path / "rides.json"
    path.write_text(json.dumps({"identifier": "rides", "metadata": _metadata()}))
    return path


def _row_count(folder: Path) -> int:
    return Iceberg("rides", {"identifier": "ns", "warehouse_path": "wh"}, base_dir=folder).row_count()


def test_run_config_from_a_path(config_path):
    result = run_config(config_path)
    assert isinstance(result, PipelineResult)
    assert result.rows_read == result.rows_written == 6
    assert _row_count(config_path.parent) == 6


def test_run_config_accepts_a_string_path(config_path):
    assert run_config(str(config_path)).rows_written == 6


def test_run_config_from_a_config_object(config_path):
    config = Config.from_json(config_path)
    assert run_config(config).write_result.rows_after == 6


def test_run_config_from_a_dict_resolves_against_the_cwd(config_path, monkeypatch):
    monkeypatch.chdir(config_path.parent)
    result = run_config({"identifier": "rides", "metadata": _metadata()})
    assert result.rows_written == 6


def test_mode_overrides_the_config(config_path):
    assert run_config(config_path).write_result.rows_after == 6
    assert run_config(config_path).write_result.rows_after == 12  # the config appends
    assert run_config(config_path, mode="overwrite").write_result.rows_after == 6


def test_a_failing_check_raises_and_writes_nothing(tmp_path, sample_table):
    pa_csv.write_csv(sample_table, tmp_path / "rides.csv")
    path = tmp_path / "rides.json"
    path.write_text(json.dumps({"identifier": "rides",
                                "metadata": _metadata(checks=[{"check": "row_count", "min": 100}])}))
    with pytest.raises(DataQualityError, match="row_count"):
        run_config(path)
    assert not Iceberg("rides", {"identifier": "ns", "warehouse_path": "wh"}, base_dir=tmp_path).exists()


WINDOW = "2026-09-01/2026-09-02"


def test_run_config_takes_a_run_id_and_an_attempt_for_a_staged_run(config_path):
    run = uuid7()
    sink = MemorySink()

    result = run_config(config_path, window=WINDOW, run_id=run, attempt=2, sink=sink)

    assert (result.run_id, result.status, result.rows_written) == (run, "published", 6)
    summary = Iceberg("rides", {"identifier": "ns", "warehouse_path": "wh"}, base_dir=config_path.parent) \
        .table().current_snapshot().summary
    assert summary["ldp.run-id"] == run and summary["ldp.attempt"] == "2"
    assert {(event.run_id, event.attempt) for event in sink.events} == {(run, 2)}


def test_a_retry_with_the_same_window_skips_what_the_first_attempt_published(config_path):
    run = uuid7()
    first = run_config(config_path, window=WINDOW, run_id=run)

    retry = run_config(config_path, window=WINDOW, run_id=run, attempt=2)

    assert (first.status, retry.status) == ("published", "skipped_duplicate")
    assert retry.published_snapshot_id == first.published_snapshot_id and _row_count(config_path.parent) == 6


def test_run_config_without_a_run_id_still_makes_a_new_one_for_every_run(config_path):
    first, second = run_config(config_path, window=WINDOW), run_config(config_path, window=WINDOW)

    assert first.run_id and second.run_id and first.run_id != second.run_id


@pytest.mark.parametrize("attempt", [0, -1, True, 1.5, "2", None])
def test_an_attempt_must_be_a_whole_number_from_one(config_path, attempt):
    with pytest.raises(ConfigError, match="attempt must be a whole number from 1"):
        run_config(config_path, window=WINDOW, run_id=uuid7(), attempt=attempt)


def test_an_attempt_above_one_needs_the_run_id_of_the_attempt_before_it(config_path):
    with pytest.raises(ConfigError, match="attempt needs a run_id"):
        run_config(config_path, window=WINDOW, attempt=2)
    assert not Iceberg("rides", {"identifier": "ns", "warehouse_path": "wh"}, base_dir=config_path.parent).exists()


@pytest.mark.parametrize("kwargs", [{"run_id": "r-1"}, {"run_id": "r-1", "attempt": 3}])
def test_a_run_id_without_a_window_or_a_key_is_refused_because_only_a_staged_run_has_one(config_path, kwargs):
    with pytest.raises(ConfigError, match="belong to a staged run, so give a window or an idempotency_key"):
        run_config(config_path, **kwargs)


def test_a_commit_context_cannot_come_with_a_window_a_key_a_run_id_or_an_attempt(config_path):
    context = CommitContext.create("k" * 64)
    for extra in ({"window": WINDOW}, {"idempotency_key": "k"}, {"run_id": "r"}, {"run_id": "r", "attempt": 2}):
        with pytest.raises(ConfigError, match="pass either commit= or"):
            run_config(config_path, commit=context, **extra)
    assert run_config(config_path, commit=context).idempotency_key == "k" * 64


def test_a_missing_config_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        run_config(tmp_path / "missing.json")


def test_an_unregistered_route_raises(tmp_path):
    metadata = _metadata()
    metadata["target"] = {"name": "t", "format": "JSON", "path": "t.json"}
    with pytest.raises(PipelineNotFound):
        run_config(Config.from_dict({"identifier": "x", "metadata": metadata}, base_dir=tmp_path))


@pytest.mark.parametrize("value", [42, None, ["rides.json"]])
def test_load_config_rejects_other_types(value):
    with pytest.raises(TypeError):
        load_config(value)


def test_load_config_validates_a_config_object():
    config = Config(identifier="x", metadata={"source": {"format": "CSV"}})
    with pytest.raises(ConfigError, match="target"):
        load_config(config)


def test_importing_etl_is_cheap_and_silent():
    probe = (
        "import contextlib, io, json, sys\n"
        "out = io.StringIO()\n"
        "with contextlib.redirect_stdout(out):\n"
        "    import local_data_platform.etl\n"
        "print(json.dumps({'stdout': out.getvalue(),\n"
        "                  'pyiceberg': 'pyiceberg' in sys.modules,\n"
        "                  'registry': 'local_data_platform.pipeline.registry' in sys.modules}))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=REPO_ROOT,
                               timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "stdout": "", "pyiceberg": False, "registry": False,
    }


# ---------------------------------------------------------------------- keys for long backfills and late re-runs

DAYS_CATALOG = {"identifier": "ns", "warehouse_path": "wh"}


def _days(folder: Path, days: int = 2, per_day: int = 2, target: str = "orders") -> Path:
    """A config that appends ``orders.csv`` to ``target``, with ``per_day`` orders for each of ``days`` days of
    August 2026, and the window column ``at`` that gives each window its own day."""
    rows = [(day * 100 + n, f"2026-08-{day:02d}T{9 + n:02d}:00:00") for day in range(1, days + 1)
            for n in range(per_day)]
    (folder / "orders.csv").write_text("id,at\n" + "".join(f"{i},{stamp}\n" for i, stamp in rows))
    config = folder / "orders.json"
    config.write_text(json.dumps({"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "orders.csv"},
        "target": {"name": target, "format": "ICEBERG", "catalog": DAYS_CATALOG, "write_mode": "append"},
        "window": {"size": "day", "column": "at"}}}))
    return config


def _day(number: int) -> str:
    return f"2026-08-{number:02d}/2026-08-{number + 1:02d}"


def _count(folder: Path, target: str = "orders") -> int:
    return Iceberg(target, DAYS_CATALOG, base_dir=folder).row_count()


def test_a_window_that_ran_eight_days_ago_is_skipped_and_the_table_does_not_change(tmp_path):
    config = _days(tmp_path)
    first = run_config(config, window=_day(1))
    run_config(config, window=_day(2))
    assert _count(tmp_path) == 4

    # The clock 8 days later. The publish loop looks back 7 days, so before the key was looked for in the whole
    # history it stopped at the newest snapshot, found nothing, and loaded the window again: 4 rows became 6.
    context = commit_context(Config.from_json(config), window=_day(1),
                             now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=8))
    again = run_config(config, commit=context)

    assert again.status == "skipped_duplicate" and again.published_snapshot_id == first.published_snapshot_id
    assert _count(tmp_path) == 4


def test_a_run_that_finds_its_key_does_not_read_its_source_and_emits_three_events(tmp_path):
    config = _days(tmp_path)
    first = run_config(config, window=_day(1))
    (tmp_path / "orders.csv").unlink()  # a run that read its source would fail now
    sink = MemorySink()

    again = run_config(config, window=_day(1), sink=sink)

    assert (again.status, again.rows_read, again.rows_written, again.rows_kept) == ("skipped_duplicate", 0, 0, None)
    assert again.published_snapshot_id == first.published_snapshot_id
    assert again.write_result.skipped_duplicate is True and again.write_result.attempts == 0
    assert str(again).startswith("orders: skipped: idempotency key already published in snapshot")
    assert again.idempotency_key == first.idempotency_key and again.run_id != first.run_id
    assert sink.types == ["run.started", "run.skipped_duplicate", "run.finished"]
    payloads = {event.type: event.payload for event in sink.events}
    assert payloads["run.skipped_duplicate"]["snapshot_id"] == first.published_snapshot_id
    assert payloads["run.finished"]["status"] == "skipped_duplicate" and payloads["run.finished"]["rows_read"] is None
    run = summarize_runs(sink.events)[0]
    assert (run["status"], run["snapshot_id"], run["rows_written"]) == (
        "skipped_duplicate", first.published_snapshot_id, 0)


def test_a_batch_longer_than_the_search_depth_fails_closed_when_it_has_no_batch_options(tmp_path):
    config = _days(tmp_path, days=8, per_day=1)
    small = CommitPolicy(max_search_depth=3, base_delay_s=0, max_delay_s=0)
    published = 0

    with pytest.raises(CommitSearchExhausted, match="max_search_depth"):
        for number in range(1, 9):
            run_config(config, window=_day(number), policy=small)
            published += 1

    assert 0 < published < 8 and _count(tmp_path) == published  # it gave up part way and wrote nothing twice


def test_a_batch_of_more_windows_than_the_search_depth_publishes_every_window_and_a_second_run_publishes_none(tmp_path):
    config = _days(tmp_path, days=8, per_day=1)
    small = CommitPolicy(max_search_depth=3, base_delay_s=0, max_delay_s=0)
    since_ms, policy = batch_search(8, policy=small, snapshots_per_run=1)
    assert policy.max_search_depth == 3 + 8 + 1 and policy.max_publish_attempts == small.max_publish_attempts

    first = [run_config(config, window=_day(n), policy=policy, search_since_ms=since_ms) for n in range(1, 9)]
    assert [result.status for result in first] == ["published"] * 8 and _count(tmp_path) == 8

    again = [run_config(config, window=_day(n), policy=policy, search_since_ms=since_ms) for n in range(1, 9)]
    assert [result.status for result in again] == ["skipped_duplicate"] * 8 and _count(tmp_path) == 8
    assert [r.published_snapshot_id for r in again] == [r.published_snapshot_id for r in first]


def test_the_policy_of_run_config_reaches_the_publish_loop(tmp_path):
    config = _days(tmp_path, days=3, per_day=1)
    run_config(config, window=_day(1))
    run_config(config, window=_day(2))

    with pytest.raises(CommitSearchExhausted):
        run_config(config, window=_day(3), policy=CommitPolicy(max_search_depth=1))
    assert _count(tmp_path) == 2
    assert run_config(config, window=_day(3)).status == "published"  # the default depth is 500


def test_run_config_hands_its_search_start_and_its_policy_to_the_run(config_path, monkeypatch):
    from local_data_platform.pipeline import Pipeline

    seen = {}

    def spy(self, mode=None, *, commit=None, sink=None, policy=None):
        seen.update(commit=commit, policy=policy)
        return "result"

    monkeypatch.setattr(Pipeline, "run", spy)
    policy = CommitPolicy(max_search_depth=77)

    assert run_config(config_path, window=WINDOW, search_since_ms=42, policy=policy) == "result"
    assert seen["commit"].search_since_ms == 42 and seen["policy"] is policy
    run_config(config_path)
    assert seen["commit"] is None and seen["policy"] is None


def test_a_policy_is_left_alone_by_a_target_that_has_no_use_for_one(config_path):
    run_config(config_path)  # rides, 6 rows, in an Iceberg table
    export = Config.from_dict({"identifier": "export", "metadata": {
        "source": {"name": "rides", "format": "ICEBERG", "catalog": {"identifier": "ns", "warehouse_path": "wh"}},
        "target": {"name": "out", "format": "CSV", "path": "out.csv"}}}, base_dir=config_path.parent)

    result = run_config(export, policy=CommitPolicy(max_search_depth=7))

    assert result.rows_written == 6 and (config_path.parent / "out.csv").exists()


def test_the_expired_key_warning_copes_with_windows_that_were_written_without_a_time_zone(tmp_path, caplog):
    config = _days(tmp_path, days=8, per_day=1)
    naive = (dt.datetime(2026, 8, 5), dt.datetime(2026, 8, 6))
    run_config(config, commit=CommitContext.create("naive-key", logical_window=naive))
    caplog.set_level(logging.DEBUG, logger="local_data_platform.pipeline")

    result = run_config(config, window=_day(1))

    assert result.status == "published"
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "may have expired" in warned[0]
    assert not [r for r in caplog.records if "could not tell" in r.getMessage()]


def test_batch_search_starts_ten_minutes_before_the_batch_and_holds_every_snapshot_it_adds():
    started = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc)
    since_ms, policy = batch_search(730, started=started)

    assert since_ms == int(started.timestamp() * 1000) - 600_000
    assert policy.max_search_depth == 500 + 730 * 2 + 1  # the usual depth, 2 snapshots a window, and the init
    assert policy.deadline_s == CommitPolicy().deadline_s
    assert batch_search(10, started=started, snapshots_per_run=1, skew_s=0)[0] == int(started.timestamp() * 1000)
    assert batch_search(0, started=dt.datetime(2026, 8, 1))[1].max_search_depth == 501  # a naive start is UTC
    custom = CommitPolicy(max_search_depth=40, max_publish_attempts=2)
    assert batch_search(5, policy=custom)[1] == dataclasses.replace(custom, max_search_depth=40 + 5 * 2 + 1)
    assert batch_search(3)[0] > int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)).timestamp() * 1000)


@pytest.mark.parametrize("kwargs, message", [
    ({"windows": -1}, "windows must be a whole number from 0"),
    ({"windows": 1.5}, "windows must be a whole number from 0"),
    ({"windows": True}, "windows must be a whole number from 0"),
    ({"windows": 3, "snapshots_per_run": 0}, "snapshots_per_run must be a whole number from 1"),
    ({"windows": 3, "skew_s": -1}, "skew_s must be a number of seconds from 0"),
    ({"windows": 3, "policy": "fast"}, "policy must be a CommitPolicy"),
])
def test_batch_search_checks_what_it_is_given(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        batch_search(**kwargs)


def test_commit_context_takes_a_search_start_of_its_own_and_checks_it(config_path):
    config = Config.from_json(config_path)
    assert commit_context(config, window=WINDOW, search_since_ms=123).search_since_ms == 123
    assert commit_context(config, window=WINDOW, search_since_ms=0).search_since_ms == 0
    default = commit_context(config, window=WINDOW, now=dt.datetime(2026, 9, 10, tzinfo=dt.timezone.utc))
    assert default.search_since_ms == int(dt.datetime(2026, 9, 3, tzinfo=dt.timezone.utc).timestamp() * 1000)
    for bad in (-1, 1.5, True, "1"):
        with pytest.raises(ConfigError, match="search_since_ms must be a number of epoch milliseconds"):
            commit_context(config, window=WINDOW, search_since_ms=bad)


def test_a_search_start_belongs_to_a_staged_run_and_a_policy_must_be_a_commit_policy(config_path):
    with pytest.raises(ConfigError, match="search_since_ms belongs to a staged run"):
        run_config(config_path, search_since_ms=5)
    with pytest.raises(ConfigError, match="pass either commit= or"):
        run_config(config_path, commit=CommitContext.create("k"), search_since_ms=5)
    with pytest.raises(ConfigError, match="policy must be a CommitPolicy"):
        run_config(config_path, policy="fast")
    assert not Iceberg("rides", {"identifier": "ns", "warehouse_path": "wh"}, base_dir=config_path.parent).exists()


def test_a_window_older_than_every_key_the_table_holds_warns_that_its_key_may_have_expired(tmp_path, caplog):
    config = _days(tmp_path, days=8, per_day=1)
    caplog.set_level(logging.WARNING, logger="local_data_platform.pipeline")
    for number in (5, 6):
        run_config(config, window=_day(number))
    assert not caplog.records  # a new table, and then windows that are newer than the oldest one

    older = run_config(config, window=_day(1))

    assert older.status == "published"  # a warning is all it is
    warned = [record.getMessage() for record in caplog.records]
    assert len(warned) == 1
    assert "2026-08-01T00:00:00+00:00/2026-08-02T00:00:00+00:00" in warned[0]
    assert "starting 2026-08-05T00:00:00+00:00" in warned[0]
    assert "may have expired" in warned[0] and "ldp backfill --replace" in warned[0]

    caplog.clear()
    run_config(config, window=_day(6))  # its key is there: skipped, and a skip does not warn
    run_config(config, window=_day(7))  # newer than the oldest window
    run_config(config, window=_day(1))  # the oldest window now is this one, and its key is there
    run_config(config, idempotency_key="no-window")  # there is no window to compare
    assert not caplog.records
