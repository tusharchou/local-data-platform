"""The ldp/v1 spec: JSON Schema, validation, spec hashing, idempotency keys, and the plan and schema commands."""

import argparse
import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform.exceptions import ConfigError
from local_data_platform.spec import (
    API_VERSION,
    add_cli,
    canonical_spec,
    idempotency_key,
    json_schema,
    parse_window,
    plan,
    spec_hash,
    target_identity,
    validate_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIGS = sorted((REPO_ROOT / "examples").glob("*/config/*.json"))

SPEC = {
    "apiVersion": "ldp/v1",
    "identifier": "rides",
    "who": "analyst", "what": "rides", "where": "NYC", "when": "daily", "how": "batch",
    "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
        "target": {
            "name": "rides", "format": "ICEBERG",
            "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"},
            "write_mode": "upsert", "join_cols": ["ride_id"],
            "partition_by": [{"column": "pickup_ts", "transform": "day"}],
        },
        "quality": {"on_failure": "fail", "checks": [
            {"check": "row_count", "min": 1},
            {"check": "unique", "columns": ["ride_id"]},
        ]},
        "observability": {"sinks": ["iceberg", "jsonl"]},
    },
}


def _spec(**changes) -> dict:
    data = copy.deepcopy(SPEC)
    for dotted, value in changes.items():
        node = data
        *parents, leaf = dotted.split("__")
        for key in parents:
            node = node[key]
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return data


def _messages(data) -> list[str]:
    errors = validate_spec(data)
    assert all(isinstance(error, ConfigError) for error in errors)
    return [str(error) for error in errors]


# ---------------------------------------------------------------------- JSON Schema


def test_api_version():
    assert API_VERSION == "ldp/v1"


def test_the_json_schema_is_valid_and_accepts_the_spec_and_every_example():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(SPEC)
    assert EXAMPLE_CONFIGS, "the repository's example configs are missing"
    for path in EXAMPLE_CONFIGS:
        data = json.loads(path.read_text())
        validator.validate(data)
        assert _messages(data) == [], path


def test_the_json_schema_rejects_what_validate_spec_rejects():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json_schema())
    for bad in (_spec(schedule="0 2 * * *"), _spec(apiVersion="ldp/v2"),
                _spec(metadata__target__write_mode="merge"), _spec(metadata__source__format="XLSX"),
                _spec(metadata__quality__checks=[{"check": "no_such_check"}]),
                _spec(metadata__observability={"sinks": ["kafka"]})):
        assert list(validator.iter_errors(bad)), bad
        assert _messages(bad), bad


# ---------------------------------------------------------------------- validate_spec


def test_a_valid_spec_and_a_config_object_have_no_problems(tmp_path):
    assert validate_spec(SPEC) == []
    config = Config.from_dict({k: v for k, v in SPEC.items() if k != "apiVersion"}, base_dir=tmp_path)
    assert validate_spec(config) == []


def test_validate_spec_reports_every_problem_with_its_path():
    data = _spec(identifier=None, apiVersion="ldp/v9", schedule="daily",
                 metadata__target__write_mode="merge", metadata__source__path=None,
                 metadata__quality__on_failure="ignore")
    messages = _messages(data)
    assert any(m.startswith("unknown top-level keys ['schedule']") for m in messages)
    assert "apiVersion: must be 'ldp/v1', got 'ldp/v9'" in messages
    assert "identifier: is required" in messages
    assert "metadata.source.path: is required" in messages
    assert any(m.startswith("metadata.target.write_mode: must be one of") for m in messages)
    assert any(m.startswith("metadata.quality.on_failure") for m in messages)
    assert len(messages) == 6


@pytest.mark.parametrize("changes, expected", [
    ({"metadata__target__join_cols": None}, "metadata.target.join_cols: is required when write_mode is 'upsert'"),
    ({"metadata__target__join_cols": [1]}, "metadata.target.join_cols: must be a column name"),
    ({"metadata__target__partition_by": [{"column": "pickup_ts", "transform": "week"}]},
     "metadata.target.partition_by[0].transform: unknown transform 'week'"),
    ({"metadata__target__partition_by": [{"column": "x", "transform": "bucket[0]"}]},
     "metadata.target.partition_by[0].transform"),
    ({"metadata__target__catalog": {"type": "hive", "identifier": "nyc"}},
     "metadata.target.catalog.type: unknown catalog type 'hive'"),
    ({"metadata__target__catalog": {"identifier": "nyc"}}, "metadata.target.catalog.warehouse_path: is required"),
    ({"metadata__target__catalog": None}, "metadata.target.catalog: is required for an Iceberg table"),
    ({"metadata__target__name": "nyc.rides"}, "metadata.target.name: must not contain '.'"),
    ({"metadata__source__format": "XLSX"}, "metadata.source.format: must be one of"),
    ({"metadata__quality__checks": [{"check": "row_count", "min": 1}, {"check": "unique"}]},
     "metadata.quality.checks[1]: check (unique) is missing required parameter(s) ['columns']"),
    ({"metadata__observability": {"sinks": ["kafka"]}}, "metadata.observability.sinks[0] has unknown sink type"),
    ({"metadata": None}, "metadata: is required"),
])
def test_validate_spec_catches(changes, expected):
    messages = _messages(_spec(**changes))
    assert any(message.startswith(expected) for message in messages), messages


def test_validate_spec_accepts_lower_case_formats_and_legacy_catalog_types():
    data = _spec(metadata__source__format="csv", metadata__target__format="iceberg")
    data["metadata"]["target"]["catalog"]["type"] = "LocalIceberg"
    assert validate_spec(data) == []


def test_validate_spec_rejects_inline_secrets_without_repeating_them():
    data = _spec()
    data["metadata"]["target"]["catalog"] = {"type": "rest", "uri": "https://catalog", "namespace": "nyc",
                                             "token": "tok-123", "token_env": "CATALOG_TOKEN",
                                             "properties": {"s3.secret-access-key": "wJalr"}}
    data["metadata"]["source"]["path"] = "AKIAIOSFODNN7EXAMPLE.csv"
    messages = _messages(data)
    flagged = sorted(m.split(":")[0] for m in messages if "inline secret" in m)
    assert flagged == ["metadata.source.path", "metadata.target.catalog.properties.s3.secret-access-key",
                       "metadata.target.catalog.token"]
    assert not any(secret in " ".join(messages) for secret in ("tok-123", "wJalr", "AKIAIOSFODNN7EXAMPLE"))


def test_validate_spec_never_raises_for_junk():
    assert "spec must be a JSON object" in str(validate_spec([1, 2])[0])
    assert _messages({"identifier": "x", "metadata": {"source": "csv", "target": {}}})


# ---------------------------------------------------------------------- spec_hash


def test_spec_hash_ignores_key_order_and_whitespace():
    compact = json.dumps(SPEC, separators=(",", ":"))
    shuffled = json.dumps(dict(reversed(list(SPEC.items()))), indent=4, sort_keys=True)
    first, second = spec_hash(json.loads(compact)), spec_hash(json.loads(shuffled))
    assert first == second
    assert len(first) == 64 and int(first, 16) >= 0


def test_spec_hash_is_the_same_wherever_the_project_is_checked_out(tmp_path):
    relative = [spec_hash(Config.from_dict({k: v for k, v in SPEC.items() if k != "apiVersion"},
                                           base_dir=tmp_path / name)) for name in ("a", "b")]
    assert relative[0] == relative[1] == spec_hash(SPEC)
    absolute = _spec(metadata__source__path=str(tmp_path / "a" / "data" / "rides.csv"))
    assert spec_hash(absolute, base_dir=tmp_path / "a") == spec_hash(SPEC)
    assert spec_hash(_spec(metadata__source__path="./data/../data/rides.csv")) == spec_hash(SPEC)


def test_spec_hash_ignores_what_does_not_change_a_run():
    same = [
        _spec(metadata__observability=None),
        _spec(who="someone else", what="x", where="y"),
        _spec(apiVersion=None),
        _spec(metadata__source__format="csv", metadata__target__write_mode="UPSERT"),
        _spec(metadata__quality=[{"check": "row_count", "min": 1}, {"check": "unique", "columns": ["ride_id"]}]),
    ]
    assert {spec_hash(item) for item in same} == {spec_hash(SPEC)}
    assert "observability" not in canonical_spec(SPEC)["metadata"]


def test_spec_hash_changes_when_the_pipeline_changes():
    different = [
        _spec(metadata__target__write_mode="overwrite"),
        _spec(metadata__quality__checks=[{"check": "row_count", "min": 2}]),
        _spec(identifier="rides_v2"),
        _spec(when="hourly"),
        _spec(metadata__source__path="data/other.csv"),
    ]
    hashes = {spec_hash(item) for item in different}
    assert len(hashes) == len(different) and spec_hash(SPEC) not in hashes


# ---------------------------------------------------------------------- idempotency keys


def test_idempotency_key_is_stable_per_pipeline_target_and_window():
    window = ("2026-09-01", "2026-09-02")
    key = idempotency_key(SPEC, window)
    assert len(key) == 64 and key == idempotency_key(copy.deepcopy(SPEC), window)
    equivalent = ["2026-09-01/2026-09-02", "2026-09-01T00:00:00Z/2026-09-02T00:00:00+00:00",
                  (dt.date(2026, 9, 1), dt.datetime(2026, 9, 2)),
                  {"start": dt.datetime(2026, 9, 1, 7, tzinfo=dt.timezone(dt.timedelta(hours=7))),
                   "end": "2026-09-02T00:00:00"}]
    assert {idempotency_key(SPEC, item) for item in equivalent} == {key}
    assert idempotency_key(SPEC, ("2026-09-02", "2026-09-03")) != key
    assert idempotency_key(SPEC) != key


def test_the_spec_hash_is_not_part_of_the_idempotency_key():
    window = "2026-09-01/2026-09-02"
    redeployed = _spec(metadata__quality__checks=[{"check": "row_count", "min": 5}], when="hourly")
    assert spec_hash(redeployed) != spec_hash(SPEC)
    assert idempotency_key(redeployed, window) == idempotency_key(SPEC, window)


def test_the_idempotency_key_changes_with_the_pipeline_and_the_table():
    window = "2026-09-01/2026-09-02"
    key = idempotency_key(SPEC, window)
    assert idempotency_key(_spec(identifier="other"), window) != key
    assert idempotency_key(_spec(metadata__target__name="rides2"), window) != key
    assert idempotency_key(SPEC, window, table_uuid="0b5e...") != key
    assert target_identity(SPEC) == "iceberg:nyc.rides"
    assert target_identity(_spec(metadata__target={"name": "x", "format": "CSV", "path": "./out/x.csv"})) == \
        "csv:out/x.csv"


@pytest.mark.parametrize("window, message", [
    ("2026-09-02/2026-09-01", "must be after"),
    ("2026-09-01", "START/END"),
    (("yesterday", "today"), "not an ISO-8601"),
    ({"from": "2026-09-01"}, "start"),
    (42, "must be"),
])
def test_a_malformed_window_is_a_config_error(window, message):
    with pytest.raises(ConfigError, match=message):
        parse_window(window)


def test_parse_window_returns_utc_bounds():
    start, end = parse_window("2026-09-01T07:00:00+07:00/2026-09-01T01:00:00Z")
    assert start == dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc) and end.hour == 1
    assert parse_window(None) is None


def test_an_idempotency_key_needs_an_identifier_and_a_target():
    with pytest.raises(ConfigError, match="identifier"):
        idempotency_key({"metadata": SPEC["metadata"]})
    with pytest.raises(ConfigError, match="target"):
        idempotency_key({"identifier": "x", "metadata": {}})


# ---------------------------------------------------------------------- plan, schema CLI


def _cli(argv):
    parser = argparse.ArgumentParser(prog="ldp")
    add_cli(parser.add_subparsers())
    args = parser.parse_args(argv)
    return args.handler(args)


def _write(folder: Path, name: str, data: dict) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(data, indent=2))
    return path


def test_plan_describes_a_valid_config_without_creating_anything(tmp_path):
    path = _write(tmp_path, "rides.json", SPEC)
    result = plan(path, window="2026-09-01/2026-09-02")
    assert result["valid"] is True and result["errors"] == []
    assert result["spec_hash"] == spec_hash(SPEC)
    assert (result["route"], result["pipeline"]) == ("CSV -> ICEBERG", "CSVToIceberg")
    assert (result["target"], result["write_mode"], result["catalog_type"]) == ("nyc.rides", "upsert", "local")
    assert (result["checks"], result["on_failure"], result["sinks"]) == (2, "fail", ["iceberg", "jsonl"])
    assert result["idempotency_key"] == idempotency_key(SPEC, ("2026-09-01", "2026-09-02"))
    assert result["window"] == "2026-09-01T00:00:00Z/2026-09-02T00:00:00Z"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rides.json"]


def test_plan_reports_an_unregistered_route_and_bad_files(tmp_path):
    no_route = _spec(metadata__target={"name": "t", "format": "JSON", "path": "t.json"}, metadata__observability=None)
    result = plan(no_route)
    assert result["valid"] is False and "no pipeline is registered for CSV -> JSON" in result["errors"][0]
    (tmp_path / "broken.json").write_text("{not json")
    assert "not valid JSON" in plan(tmp_path / "broken.json")["errors"][0]
    assert "not found" in plan(tmp_path / "missing.json")["errors"][0]


def test_ldp_plan_prints_the_plan_and_exits_1_for_an_invalid_config(tmp_path, capsys):
    good = _write(tmp_path / "configs", "rides.json", SPEC)
    assert _cli(["plan", str(good), "--window", "2026-09-01/2026-09-02"]) == 0
    out = capsys.readouterr().out
    assert f"spec_hash        {spec_hash(SPEC)}" in out
    assert "route            CSV -> ICEBERG [CSVToIceberg]" in out and "idempotency_key" in out

    assert _cli(["plan", str(good), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["pipeline"] == "CSVToIceberg"

    bad = _write(tmp_path / "configs", "bad.json", _spec(metadata__target__write_mode="merge"))
    assert _cli(["plan", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "1 problem:" in err and "metadata.target.write_mode" in err

    assert _cli(["plan", str(tmp_path / "configs")]) == 1
    captured = capsys.readouterr()
    assert "bad.json" in captured.out and "rides.json" in captured.out and "(2 rows)" in captured.out
    assert "bad.json: metadata.target.write_mode" in captured.err


def test_ldp_plan_of_an_empty_folder_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no \\*.json configs"):
        _cli(["plan", str(tmp_path)])


def test_ldp_schema_prints_the_json_schema(capsys):
    assert _cli(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["apiVersion"]["const"] == API_VERSION
    assert "unique" in schema["$defs"]["check"]["properties"]["check"]["enum"]
