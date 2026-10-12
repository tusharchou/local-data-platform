"""Tests for the JSON source."""

import json
import uuid

import pytest

from local_data_platform.exceptions import ConfigError
from local_data_platform.store.source import Source
from local_data_platform.store.source.json import Json

CONFIG = {"identifier": "nyc_taxi", "metadata": {"source": {"format": "CSV"}, "target": {"format": "ICEBERG"}}}


def test_get_returns_parsed_json_from_absolute_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG))

    source = Json(name="config", path=str(path), format="JSON")

    assert source.get() == CONFIG
    assert source.format == "JSON"
    assert isinstance(source, Source)


def test_relative_path_resolves_against_base_dir(tmp_path):
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "q.json").write_text('{"query": "SELECT 1"}')

    source = Json("query", "queries/q.json", base_dir=tmp_path)

    assert source.path == (tmp_path / "queries" / "q.json").resolve()
    assert source.get() == {"query": "SELECT 1"}


def test_relative_path_without_base_dir_resolves_against_cwd(tmp_path, monkeypatch):
    (tmp_path / "q.json").write_text("[1, 2, 3]")
    monkeypatch.chdir(tmp_path)

    assert Json("query", "q.json").get() == [1, 2, 3]


def test_legacy_leading_slash_path_warns(tmp_path):
    name = f"ldp_legacy_{uuid.uuid4().hex}.json"
    (tmp_path / name).write_text('{"ok": true}')

    with pytest.warns(DeprecationWarning, match="leading slash"):
        source = Json("legacy", f"/{name}", base_dir=tmp_path)

    assert source.get() == {"ok": True}


def test_missing_file_names_resolved_path(tmp_path):
    source = Json("missing", "nope.json", base_dir=tmp_path)

    with pytest.raises(FileNotFoundError, match=str(tmp_path / "nope.json")):
        source.get()


def test_invalid_json_error_names_the_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")

    with pytest.raises(json.JSONDecodeError, match="broken.json"):
        Json("broken", path).get()


@pytest.mark.parametrize("path", [None, ""])
def test_missing_path_is_a_config_error(path):
    with pytest.raises(ConfigError, match="needs a path"):
        Json("query", path)


def test_put_is_not_supported(tmp_path):
    from local_data_platform.exceptions import TableNotFound

    with pytest.raises(TableNotFound):
        Json("query", tmp_path / "q.json").put({"a": 1})
