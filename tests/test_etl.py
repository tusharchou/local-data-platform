"""etl.run_config: run a config from a path, a Config or a dict."""

import json
import subprocess
import sys
from pathlib import Path

import pyarrow.csv as pa_csv
import pytest

from local_data_platform import Config
from local_data_platform.etl import load_config, run_config
from local_data_platform.events import MemorySink, uuid7
from local_data_platform.exceptions import ConfigError, DataQualityError, PipelineNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import CommitContext
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
