"""etl.run_config: run a config from a path, a Config or a dict."""

import json
import subprocess
import sys
from pathlib import Path

import pyarrow.csv as pa_csv
import pytest

from local_data_platform import Config
from local_data_platform.etl import load_config, run_config
from local_data_platform.exceptions import ConfigError, DataQualityError, PipelineNotFound
from local_data_platform.format.iceberg import Iceberg
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
