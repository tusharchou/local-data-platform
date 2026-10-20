"""The kind of a file: ``Pipeline`` (the default), ``Workflow`` and ``Metrics``, the registry that holds them, and the
code that reads a folder of JSON files and has to tell them apart."""

import argparse
import copy
import json
from pathlib import Path

import pytest

from local_data_platform import Config
from local_data_platform import spec as spec_module
from local_data_platform.cli import main
from local_data_platform.exceptions import ConfigError
from local_data_platform.lake import open_lake
from local_data_platform.mcp_server.discovery import config_files, discover_tables
from local_data_platform.spec import (
    DEFAULT_KIND,
    RESERVED_KINDS,
    add_cli,
    canonical_spec,
    kind_of,
    kind_of_file,
    kind_schema,
    plan,
    register_kind,
    registered_kinds,
    spec_hash,
    validate_spec,
)
from tests.support import add_table

PIPELINE = {
    "identifier": "rides",
    "metadata": {
        "source": {"name": "rides", "format": "CSV", "path": "rides.csv"},
        "target": {"name": "rides", "format": "ICEBERG", "write_mode": "overwrite",
                   "catalog": {"identifier": "nyc", "warehouse_path": "warehouse"}},
    },
}
WORKFLOW = {
    "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders",
    "metadata": {"schedule": "@daily"},
    "tasks": [{"id": "load", "pipeline": "orders.json"},
              {"id": "model", "maintain": {"config": "orders.json"}, "depends_on": ["load"]}],
}


def _with(base: dict, **changes) -> dict:
    data = copy.deepcopy(base)
    for key, value in changes.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def _write(folder: Path, name: str, data) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
    return path


def _messages(data) -> list[str]:
    return [str(error) for error in validate_spec(data)]


def _cli(argv):
    parser = argparse.ArgumentParser(prog="ldp")
    add_cli(parser.add_subparsers())
    args = parser.parse_args(argv)
    return args.handler(args)


@pytest.fixture
def fresh_registry(monkeypatch):
    """A registry that a test can add kinds to without changing it for the other tests."""
    monkeypatch.setattr(spec_module, "_KINDS", dict(spec_module._KINDS))


# --------------------------------------------------------------------------- what a file says it is


def test_a_file_without_a_kind_is_a_pipeline_and_the_kinds_are_pipeline_workflow_and_metrics():
    assert DEFAULT_KIND == "Pipeline"
    assert kind_of(PIPELINE) == "Pipeline"
    assert kind_of(_with(PIPELINE, kind="Pipeline")) == "Pipeline"
    assert kind_of(WORKFLOW) == "Workflow"
    assert kind_of(Config.from_dict(PIPELINE)) == "Pipeline"
    assert registered_kinds() == ["Pipeline", "Workflow", "Metrics"]


def test_a_pipeline_has_the_same_spec_hash_with_and_without_its_kind(tmp_path):
    plain, named = PIPELINE, _with(PIPELINE, kind="Pipeline", apiVersion="ldp/v1")

    assert spec_hash(plain, base_dir=tmp_path) == spec_hash(named, base_dir=tmp_path)
    assert "kind" not in canonical_spec(named, base_dir=tmp_path)
    assert spec_hash(Config.from_dict(named, base_dir=tmp_path)) == spec_hash(plain, base_dir=tmp_path)


@pytest.mark.parametrize("written, right", [("pipeline", "Pipeline"), ("workflow", "Workflow"),
                                            ("WORKFLOW", "Workflow"), (" Workflow", "Workflow")])
def test_a_kind_in_the_wrong_case_fails_and_names_the_right_spelling(written, right):
    data = _with(PIPELINE, kind=written)

    assert _messages(data) == [f"kind: unknown kind {written!r}; did you mean {right!r}? Kind names are written "
                               f"exactly as in ['Pipeline', 'Workflow', 'Metrics']"]
    with pytest.raises(ConfigError, match=f"did you mean {right!r}"):
        kind_of(data)


def test_an_unknown_kind_lists_the_kinds_and_a_kind_must_be_a_string():
    assert _messages(_with(PIPELINE, kind="Banana")) == ["kind: unknown kind 'Banana'; the kinds are "
                                                         "['Pipeline', 'Workflow', 'Metrics']"]
    assert _messages(_with(PIPELINE, kind=5)) == ["kind: kind must be a string, got 5"]
    assert _messages(_with(PIPELINE, kind=["Pipeline"])) == ["kind: kind must be a string, got ['Pipeline']"]


@pytest.mark.parametrize("name, version", sorted(RESERVED_KINDS.items()))
def test_a_kind_that_a_later_milestone_adds_says_which_version_adds_it(name, version):
    assert _messages(_with(PIPELINE, kind=name)) == [
        f"kind: kind {name!r} is reserved for {version} and is not available yet. The kinds are "
        f"['Pipeline', 'Workflow', 'Metrics']"]
    assert f"did you mean {name!r}? That kind is reserved for {version}" in _messages(
        _with(PIPELINE, kind=name.lower()))[0]


def test_kind_of_file_reads_the_kind_and_says_none_when_the_file_cannot_be_read_as_a_spec(tmp_path):
    assert kind_of_file(_write(tmp_path, "pipeline.json", PIPELINE)) == "Pipeline"
    assert kind_of_file(_write(tmp_path, "workflow.json", WORKFLOW)) == "Workflow"
    assert kind_of_file(_write(tmp_path, "broken.json", "{not json")) is None
    assert kind_of_file(_write(tmp_path, "list.json", [1, 2])) is None
    assert kind_of_file(_write(tmp_path, "casing.json", _with(PIPELINE, kind="pipeline"))) is None
    assert kind_of_file(tmp_path / "missing.json") is None


# --------------------------------------------------------------------------- validating a workflow


def test_a_workflow_and_a_pipeline_with_a_kind_are_valid():
    assert validate_spec(WORKFLOW) == []
    assert validate_spec(_with(PIPELINE, kind="Pipeline", apiVersion="ldp/v1")) == []


@pytest.mark.parametrize("changes, expected", [
    ({"tasks": None}, "tasks: is required and must be a list of at least one task"),
    ({"tasks": []}, "tasks: is required and must be a list of at least one task"),
    ({"tasks": "load"}, "tasks: is required and must be a list of at least one task"),
    ({"tasks": ["load"]}, "tasks[0]: must be an object with an 'id'"),
    ({"tasks": [{"pipeline": "a.json"}]}, "tasks[0].id: is required and must be a non-empty string"),
    ({"tasks": [{"id": "a", "pipeline": "a.json"}, {"id": "b", "pipeline": "b.json"},
                {"id": "a", "pipeline": "a.json"}]}, "tasks[2].id: 'a' is the id of an earlier task"),
    ({"identifier": None}, "identifier: is required"),
    ({"apiVersion": "ldp/v9"}, "apiVersion: must be 'ldp/v1', got 'ldp/v9'"),
    ({"metadata": "daily"}, "metadata: must be an object, got str"),
    ({"schedule": "@daily"}, "unknown top-level keys ['schedule']; allowed: "),
    ({"target": {}}, "unknown top-level keys ['target']; allowed: "),
    ({"who": 5}, "who: must be a string, got int"),
])
def test_a_workflow_is_checked_for_its_envelope(changes, expected):
    messages = _messages(_with(WORKFLOW, **changes))

    assert len(messages) == 1 and messages[0].startswith(expected), messages


def test_a_workflow_with_an_inline_secret_is_refused_and_the_secret_is_not_repeated():
    data = _with(WORKFLOW, what="AKIAABCDEFGHIJKLMNOP")

    messages = _messages(data)

    assert len(messages) == 1 and messages[0].startswith("what: looks like an inline secret")
    assert "AKIAABCDEFGHIJKLMNOP" not in messages[0]


def test_a_pipeline_cannot_have_tasks_and_a_pipeline_file_that_says_workflow_is_checked_as_one():
    assert _messages(_with(PIPELINE, tasks=[{"id": "a"}]))[0].startswith("unknown top-level keys ['tasks']")
    messages = _messages(_with(PIPELINE, kind="Workflow"))
    assert "tasks: is required and must be a list of at least one task" in messages
    assert messages[0].startswith("metadata: has unknown keys ['source', 'target']")


def test_validate_spec_still_never_raises_for_junk():
    for junk in (None, 5, "kind", [], [WORKFLOW], {"kind": {}}, {"kind": None}, {"kind": "Workflow", "tasks": None}):
        assert all(isinstance(error, ConfigError) for error in validate_spec(junk)), junk


# --------------------------------------------------------------------------- plan and schema


def test_plan_describes_a_workflow_by_its_tasks_and_a_pipeline_as_before(tmp_path):
    workflow = plan(_write(tmp_path, "daily.json", WORKFLOW))
    pipeline = plan(_write(tmp_path, "rides.json", PIPELINE))

    assert (workflow["valid"], workflow["kind"], workflow["identifier"]) == (True, "Workflow", "daily_orders")
    assert workflow["tasks"] == ["load", "model"]
    assert (workflow["route"], workflow["spec_hash"], workflow["target"]) == (None, None, None)
    assert (pipeline["valid"], pipeline["kind"], pipeline["route"]) == (True, "Pipeline", "CSV -> ICEBERG")
    assert "tasks" not in pipeline
    assert plan(_write(tmp_path, "bad.json", _with(PIPELINE, kind="workflow")))["kind"] is None


def test_ldp_plan_of_a_folder_that_mixes_a_pipeline_and_a_workflow_checks_each_as_its_kind(tmp_path, capsys):
    folder = tmp_path / "configs"
    _write(folder, "rides.json", PIPELINE)
    _write(folder, "daily.json", WORKFLOW)

    assert _cli(["plan", str(folder)]) == 0

    out = capsys.readouterr().out
    assert "kind" in out.splitlines()[0] and "(2 rows)" in out
    rows = {line.split()[0]: line.split() for line in out.splitlines() if line.endswith((".json",)) or ".json " in line}
    assert rows["daily.json"][1] == "Workflow" and rows["rides.json"][1] == "Pipeline"


def test_ldp_plan_exits_with_1_for_a_broken_workflow_in_a_folder_and_names_the_file(tmp_path, capsys):
    folder = tmp_path / "configs"
    _write(folder, "rides.json", PIPELINE)
    twice = [{"id": "a", "pipeline": "a.json"}, {"id": "a", "pipeline": "a.json"}]
    _write(folder, "daily.json", _with(WORKFLOW, tasks=twice))

    assert _cli(["plan", str(folder)]) == 1

    captured = capsys.readouterr()
    assert "daily.json: tasks[1].id: 'a' is the id of an earlier task" in captured.err
    assert "rides.json" in captured.out


def test_ldp_plan_of_one_workflow_prints_its_kind_and_tasks_and_json_has_the_kind(tmp_path, capsys):
    path = _write(tmp_path, "daily.json", WORKFLOW)

    assert _cli(["plan", str(path)]) == 0
    out = capsys.readouterr().out
    assert "identifier  daily_orders" in out and "kind        Workflow" in out and "tasks       load, model" in out
    assert "spec_hash" not in out

    assert _cli(["plan", str(path), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["kind"], printed["tasks"]) == ("Workflow", ["load", "model"])


def test_ldp_schema_prints_the_pipeline_schema_by_default_and_the_schema_of_a_kind_with_kind(capsys):
    assert _cli(["schema"]) == 0
    default = json.loads(capsys.readouterr().out)
    assert default["properties"]["kind"]["const"] == "Pipeline" and "tasks" not in default["properties"]

    assert _cli(["schema", "--kind", "Pipeline"]) == 0
    assert json.loads(capsys.readouterr().out) == default

    assert _cli(["schema", "--kind", "Workflow"]) == 0
    workflow = json.loads(capsys.readouterr().out)
    assert workflow["required"] == ["kind", "identifier", "tasks"]
    assert workflow["properties"]["kind"]["const"] == "Workflow" and "tasks" in workflow["properties"]
    assert kind_schema("Workflow") == workflow


def test_ldp_schema_with_a_kind_that_is_wrong_fails_with_a_hint(capsys):
    with pytest.raises(ConfigError, match="did you mean 'Workflow'"):
        _cli(["schema", "--kind", "workflow"])
    with pytest.raises(ConfigError,
                       match=r"unknown kind 'Banana'; the kinds are \['Pipeline', 'Workflow', 'Metrics'\]"):
        _cli(["schema", "--kind", "Banana"])
    with pytest.raises(ConfigError, match="reserved for 0.1.6"):
        _cli(["schema", "--kind", "Report"])


def test_the_pipeline_schema_accepts_the_kind_and_the_workflow_schema_is_valid():
    jsonschema = pytest.importorskip("jsonschema")
    pipeline_schema, workflow_schema = kind_schema("Pipeline"), kind_schema("Workflow")
    jsonschema.Draft202012Validator.check_schema(pipeline_schema)
    jsonschema.Draft202012Validator.check_schema(workflow_schema)

    assert not list(jsonschema.Draft202012Validator(pipeline_schema).iter_errors(_with(PIPELINE, kind="Pipeline")))
    assert list(jsonschema.Draft202012Validator(pipeline_schema).iter_errors(_with(PIPELINE, kind="Workflow")))
    assert not list(jsonschema.Draft202012Validator(workflow_schema).iter_errors(WORKFLOW))
    assert list(jsonschema.Draft202012Validator(workflow_schema).iter_errors(_with(WORKFLOW, tasks=[])))


# --------------------------------------------------------------------------- the registry


def test_a_kind_that_a_module_registers_is_validated_planned_and_printed_like_the_others(fresh_registry, tmp_path):
    seen = []

    def validate(spec):
        seen.append(spec["kind"])
        return [ConfigError("title: is required")] if "title" not in spec else []

    register_kind("Report", validate=validate, json_schema=lambda: {"title": "report schema"},
                  describe=lambda spec: {"title": spec["title"]})

    assert registered_kinds() == ["Pipeline", "Workflow", "Metrics", "Report"]
    assert _messages({"kind": "Report"}) == ["title: is required"] and seen == ["Report"]
    assert plan(_write(tmp_path, "r.json", {"kind": "Report", "title": "Sales"}))["title"] == "Sales"
    assert kind_schema("Report") == {"title": "report schema"}
    assert "Report" in _messages(_with(PIPELINE, kind="Banana"))[0]
    assert kind_of_file(_write(tmp_path, "report.json", {"kind": "Report"})) == "Report"


def test_a_registered_kind_is_no_longer_reserved(fresh_registry):
    register_kind("Profiles", validate=lambda spec: [], json_schema=dict)

    assert _messages(_with(PIPELINE, kind="profiles"))[0].startswith("kind: unknown kind 'profiles'; did you mean "
                                                                     "'Profiles'? Kind names are written exactly")
    assert kind_of({"kind": "Profiles"}) == "Profiles"


def test_registering_a_kind_twice_needs_replace_and_a_kind_name_is_checked(fresh_registry):
    with pytest.raises(ValueError, match="already registered; pass replace=True"):
        register_kind("Workflow", validate=lambda spec: [], json_schema=dict)
    register_kind("Workflow", validate=lambda spec: [ConfigError("replaced")], json_schema=dict, replace=True)
    assert _messages(WORKFLOW) == ["replaced"]
    for bad in ("workflow", "Work flow", "", "9Lives", 5):
        with pytest.raises(ValueError, match="starts with a capital letter"):
            register_kind(bad, validate=lambda spec: [], json_schema=dict)


# --------------------------------------------------------------------------- Config and ldp run


def test_a_config_may_say_its_api_version_and_its_kind(tmp_path):
    config = Config.from_dict(_with(PIPELINE, apiVersion="ldp/v1", kind="Pipeline"), base_dir=tmp_path)

    assert (config.api_version, config.kind) == ("ldp/v1", "Pipeline")
    assert (Config.from_dict(PIPELINE).api_version, Config.from_dict(PIPELINE).kind) == (None, "Pipeline")
    assert config.source["name"] == "rides" and config.identifier == "rides"


def test_a_config_with_another_api_version_or_an_unknown_key_is_still_refused():
    with pytest.raises(ConfigError, match=r"apiVersion must be 'ldp/v1', got 'ldp/v9'"):
        Config.from_dict(_with(PIPELINE, apiVersion="ldp/v9"))
    with pytest.raises(ConfigError, match=r"unknown top-level keys: \['schedule'\]"):
        Config.from_dict(_with(PIPELINE, schedule="@daily"))


def test_a_workflow_is_not_a_dataset_config_and_the_message_says_so():
    with pytest.raises(ConfigError, match=r"'daily_orders' is a Workflow, not a dataset config \(kind Pipeline\)"):
        Config.from_dict(WORKFLOW)
    with pytest.raises(ConfigError, match="did you mean 'Pipeline'"):
        Config.from_dict(_with(PIPELINE, kind="pipeline"))


def test_ldp_run_runs_a_config_that_has_an_api_version_and_a_kind(tmp_path, capsys):
    (tmp_path / "rides.csv").write_text("ride_id,fare\n1,10.5\n2,8\n3,12\n")
    path = _write(tmp_path, "rides.json", _with(PIPELINE, apiVersion="ldp/v1", kind="Pipeline"))

    assert main(["run", str(path)]) == 0

    assert "rides: read 3 rows, wrote 3 rows" in capsys.readouterr().out
    assert (tmp_path / "warehouse").is_dir()


def test_ldp_run_of_a_workflow_file_exits_with_1_and_one_friendly_line(tmp_path, capsys):
    path = _write(tmp_path, "daily.json", WORKFLOW)

    assert main(["run", str(path)]) == 1

    err = capsys.readouterr().err
    assert err == "ldp: error: 'daily_orders' is a Workflow, not a dataset config (kind Pipeline)\n"


# --------------------------------------------------------------------------- folders of configs


def test_a_folder_of_configs_gives_the_dataset_configs_and_leaves_out_the_other_kinds(tmp_path):
    folder = tmp_path / "configs"
    pipeline = _write(folder, "a_rides.json", PIPELINE)
    _write(folder, "b_daily.json", WORKFLOW)
    broken = _write(folder, "c_broken.json", "{not json")
    casing = _write(folder, "d_casing.json", _with(PIPELINE, kind="pipeline"))
    _write(folder, "notes.txt", "not a config")

    assert config_files(folder) == [pipeline, broken, casing]  # the files that report their own problem stay
    assert config_files(folder / "b_daily.json") == [folder / "b_daily.json"]  # a file that is named is not filtered


def test_the_tables_of_a_folder_are_the_tables_of_its_pipelines_only(tmp_path):
    add_table(tmp_path, "raw", "orders", 3)
    _write(tmp_path / "config", "daily.json", WORKFLOW)
    _write(tmp_path / "config", "casing.json", _with(PIPELINE, kind="pipeline"))

    discovery = discover_tables([tmp_path / "config"])
    try:
        assert [table.identifier for table in discovery.tables] == ["raw.orders"]
        assert discovery.unavailable == []
    finally:
        discovery.close()
    with open_lake(tmp_path / "config") as lake:
        assert lake.query("SELECT count(*) AS n FROM raw.orders").to_pylist() == [{"n": 3}]


def test_a_workflow_file_that_is_named_is_an_error_and_not_skipped(tmp_path):
    add_table(tmp_path, "raw", "orders", 3)
    workflow = _write(tmp_path / "config", "daily.json", WORKFLOW)

    with pytest.raises(ConfigError, match="is a Workflow, not a dataset config"):
        discover_tables([workflow])
