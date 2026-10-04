"""Tests for local_data_platform.paths.resolve_path, the path rules every config uses."""

import os
import subprocess
import sys
import uuid
import warnings
from pathlib import Path

import pytest

from local_data_platform import resolve_path
from local_data_platform.format.csv import CSV


def _missing_top_level_name() -> str:
    name = f"ldp_paths_{uuid.uuid4().hex}"
    assert not os.path.exists(f"/{name}")
    return name


def test_relative_path_resolves_against_base_dir(tmp_path):
    assert resolve_path("data/rides.csv", tmp_path) == (tmp_path / "data" / "rides.csv").resolve()


def test_relative_path_without_base_dir_resolves_against_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert resolve_path("rides.csv") == (tmp_path / "rides.csv").resolve()


def test_existing_absolute_path_is_kept(tmp_path):
    target = tmp_path / "rides.csv"
    target.write_text("id\n1\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_path(str(target), base_dir="/somewhere/else") == target


def test_new_absolute_output_with_existing_parent_is_kept(tmp_path):
    target = tmp_path / "out" / "rides.csv"
    target.parent.mkdir()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_path(str(target)) == target


def test_new_absolute_output_in_a_new_folder_is_kept(tmp_path):
    target = tmp_path / "new" / "deeper" / "rides.csv"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert resolve_path(str(target), tmp_path) == target


def test_empty_path_raises():
    with pytest.raises(ValueError):
        resolve_path("")


def test_legacy_existing_file_falls_back_with_warning(tmp_path):
    name = _missing_top_level_name()
    (tmp_path / name).write_text("id\n1\n")
    with pytest.warns(DeprecationWarning, match="leading slash"):
        assert resolve_path(f"/{name}", tmp_path) == (tmp_path / name).resolve()


def test_legacy_new_output_in_existing_relative_folder_falls_back(tmp_path):
    folder = _missing_top_level_name()
    (tmp_path / folder).mkdir()
    with pytest.warns(DeprecationWarning, match="its folder only exists relative to"):
        resolved = resolve_path(f"/{folder}/rides.csv", tmp_path)
    assert resolved == (tmp_path / folder / "rides.csv").resolve()


def test_legacy_new_top_level_output_falls_back_to_base_dir(tmp_path):
    name = _missing_top_level_name()
    with pytest.warns(DeprecationWarning, match="leading slash"):
        assert resolve_path(f"/{name}", tmp_path) == (tmp_path / name).resolve()


def test_legacy_csv_target_can_be_written(tmp_path, sample_table):
    folder = _missing_top_level_name()
    (tmp_path / folder).mkdir()
    with pytest.warns(DeprecationWarning):
        target = CSV("out", f"/{folder}/rides.csv", base_dir=tmp_path)
    assert target.put(sample_table) == sample_table.num_rows
    assert (tmp_path / folder / "rides.csv").is_file()
    assert not os.path.exists(f"/{folder}")


def test_deprecation_warning_is_attributed_to_the_caller_outside_the_package(tmp_path):
    """Python's default filters show a DeprecationWarning only when it points at __main__."""
    name = _missing_top_level_name()
    (tmp_path / name).write_text("id\n1\n")
    script = tmp_path / "legacy_script.py"
    script.write_text(
        "from local_data_platform.format.csv import CSV\n"
        f"CSV('rides', '/{name}', base_dir={str(tmp_path)!r})\n"
    )
    env = {key: value for key, value in os.environ.items() if key != "PYTHONWARNINGS"}
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env,
                          cwd=tmp_path, check=True)
    assert "DeprecationWarning" in done.stderr
    assert f"{Path(script).name}:2" in done.stderr
