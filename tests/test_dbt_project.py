"""``ldp_dbt.json``, ``ldp dbt init``, and finding and running dbt. None of it needs dbt installed."""

import json
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from local_data_platform.dbt_layer import project as project_module
from local_data_platform.dbt_layer.project import (
    INSTALL_HINT,
    DbtProject,
    check_profile,
    find_dbt,
    find_target_path,
    init_project,
    run_dbt,
    validate_project,
)
from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.paths import resolve_path
from tests.support import build_lake

CATALOG = {"identifier": "gold", "warehouse_path": "warehouse"}


def write_project_file(folder: Path, data) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "ldp_dbt.json").write_text(json.dumps(data))
    return folder


# --------------------------------------------------------------------------- ldp_dbt.json


def test_a_minimal_project_file_gets_its_defaults(tmp_path):
    folder = write_project_file(tmp_path / "proj", {"publish": {"catalog": CATALOG}})

    project = DbtProject.load(folder)

    assert project.directory == folder
    assert project.database == folder / "target" / "ldp_build.duckdb"
    assert project.config_dir == folder / "target" / "ldp"
    assert project.sources_file == folder / "models" / "_ldp_sources.yml"
    assert project.source_configs == () and project.source_catalogs == ()
    assert project.publish_catalog == CATALOG and project.observability is None


def test_paths_resolve_against_the_project_folder_and_a_plain_list_means_configs(tmp_path):
    folder = write_project_file(tmp_path / "proj", {
        "database": "build/my.duckdb",
        "sources": ["../config", str(tmp_path / "elsewhere.json")],
        "publish": {"catalog": CATALOG, "config_dir": "gen"},
        "observability": {"sinks": ["jsonl"]},
    })

    project = DbtProject.load(folder)

    assert project.database == folder / "build" / "my.duckdb"
    assert project.source_configs == (resolve_path("../config", folder), tmp_path / "elsewhere.json")
    assert project.config_dir == folder / "gen"
    assert project.observability == {"sinks": ["jsonl"]}


def test_sources_can_name_catalog_files_too(tmp_path):
    folder = write_project_file(tmp_path / "proj", {
        "sources": {"configs": ["cfg"], "catalogs": ["catalog.json"]}, "publish": {"catalog": CATALOG}})

    project = DbtProject.load(folder)

    assert project.source_configs == (folder / "cfg",) and project.source_catalogs == (folder / "catalog.json",)


def test_a_missing_folder_or_file_says_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="dbt project folder not found"):
        DbtProject.load(tmp_path / "nope")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ConfigError, match=r"has no ldp_dbt\.json\. Create a project with: ldp dbt init"):
        DbtProject.load(tmp_path / "empty")


def test_a_file_that_is_not_json_is_reported(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "ldp_dbt.json").write_text("{not json")

    with pytest.raises(ConfigError, match="is not valid JSON"):
        DbtProject.load(folder)


def test_problems_are_reported_together_each_with_its_json_path(tmp_path):
    folder = write_project_file(tmp_path / "proj", {
        "colour": 1, "database": "", "sources": {"configs": "x"},
        "publish": {"catalog": {"identifier": "g", "type": "rest"}}})

    with pytest.raises(ConfigError) as error:
        DbtProject.load(folder)

    text = str(error.value)
    assert "has 4 problems:" in text
    for expected in ("unknown keys ['colour']", "database: must be a non-empty string",
                     "sources.configs: must be a list of non-empty path strings", "publish.catalog.uri: is required"):
        assert f"  - {expected}" in text, text


@pytest.mark.parametrize("data, problem", [
    ([], "must be a JSON object, got list"),
    ({}, "publish: is required and must be an object with a 'catalog'"),
    ({"publish": {}}, "publish.catalog: is required"),
    ({"publish": {"catalog": "nope"}}, "publish.catalog: must be an object, got str"),
    ({"publish": {"catalog": {"warehouse_path": "w"}}}, "publish.catalog.identifier: is required"),
    ({"publish": {"catalog": CATALOG, "config_dir": ""}}, "publish.config_dir: must be a non-empty string"),
    ({"publish": {"catalog": CATALOG, "extra": 1}}, "publish: unknown keys ['extra']"),
    ({"publish": {"catalog": CATALOG}, "sources": 3}, "sources: must be a list of config paths or an object"),
    ({"publish": {"catalog": CATALOG}, "sources": {"tables": []}}, "sources: unknown keys ['tables']"),
    ({"publish": {"catalog": CATALOG}, "sources": {"catalogs": [""]}},
     "sources.catalogs: must be a list of non-empty path strings"),
    ({"publish": {"catalog": CATALOG}, "observability": []}, "observability: must be an object, got list"),
])
def test_validate_project_names_each_problem(data, problem):
    problems = validate_project(data)

    assert any(item.startswith(problem) for item in problems), problems


def test_validate_project_accepts_a_complete_file():
    assert validate_project({"database": "d.duckdb", "sources": {"configs": ["a"], "catalogs": ["b"]},
                             "publish": {"catalog": CATALOG, "config_dir": "gen"},
                             "observability": {"sinks": ["jsonl"]}}) == []


# --------------------------------------------------------------------------- finding dbt


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def no_dbt_anywhere(tmp_path, monkeypatch):
    """The interpreter has no dbt beside it and PATH holds none."""
    monkeypatch.setattr(sys, "executable", str(tmp_path / "interpreter" / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty_path"))


def test_the_dbt_next_to_the_interpreter_is_preferred(tmp_path, monkeypatch):
    beside = make_executable(tmp_path / "venv" / "bin" / "dbt")
    make_executable(tmp_path / "other" / "dbt")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "bin" / "python"))
    monkeypatch.setenv("PATH", str(tmp_path / "other"))

    assert find_dbt() == str(beside)


def test_the_dbt_on_path_is_the_fallback(tmp_path, monkeypatch, no_dbt_anywhere):
    on_path = make_executable(tmp_path / "bin" / "dbt")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    assert find_dbt() == str(on_path)


def test_a_given_dbt_wins_over_the_others(tmp_path, monkeypatch):
    chosen = make_executable(tmp_path / "chosen" / "dbt")
    make_executable(tmp_path / "venv" / "bin" / "dbt")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "venv" / "bin" / "python"))

    assert find_dbt(chosen) == str(chosen)


def test_a_given_dbt_that_does_not_exist_names_the_path_and_the_install_command(tmp_path):
    with pytest.raises(EngineNotFound) as error:
        find_dbt(tmp_path / "no" / "dbt")

    assert str(tmp_path / "no" / "dbt") in str(error.value) and INSTALL_HINT in str(error.value)


def test_no_dbt_at_all_names_the_extra(no_dbt_anywhere):
    with pytest.raises(EngineNotFound) as error:
        find_dbt()

    assert 'pip install "local-data-platform[dbt]"' in str(error.value)


# --------------------------------------------------------------------------- running dbt


class Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 3)


@pytest.fixture
def captured(monkeypatch):
    capture = Capture()
    monkeypatch.setattr(project_module.subprocess, "run", capture)
    return capture


@pytest.fixture
def project(tmp_path) -> DbtProject:
    return DbtProject.load(write_project_file(tmp_path / "proj", {"publish": {"catalog": CATALOG}}))


def test_run_dbt_puts_the_project_flags_last_and_runs_in_the_project_folder(captured, project):
    status = run_dbt(project, "/bin/dbt", ["build", "--select", "orders"])

    assert status == 3  # the exit status of dbt, unchanged
    [(argv, kwargs)] = captured.calls
    assert argv == ["/bin/dbt", "--no-version-check", "--no-use-colors", "build", "--select", "orders",
                    "--project-dir", str(project.directory), "--profiles-dir", str(project.directory)]
    assert kwargs["cwd"] == project.directory and kwargs["check"] is False


def test_run_dbt_keeps_colour_when_stdout_is_a_terminal(captured, project, monkeypatch):
    class Terminal:
        def isatty(self):
            return True

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", Terminal())

    run_dbt(project, "/bin/dbt", ["ls"])

    assert "--no-use-colors" not in captured.calls[0][0]


def test_run_dbt_flushes_what_ldp_printed_before_dbt_writes_to_the_same_stream(captured, project, monkeypatch):
    flushed = []

    class Stream:
        def isatty(self):
            return False

        def flush(self):
            flushed.append(True)

    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", Stream())

    run_dbt(project, "/bin/dbt", ["ls"])

    assert len(flushed) == 2


def test_run_dbt_can_send_the_output_of_dbt_to_another_descriptor(captured, project):
    run_dbt(project, "/bin/dbt", ["ls"], stdout=2)

    assert captured.calls[0][1]["stdout"] == 2
    assert "stdout" in captured.calls[0][1] and run_dbt(project, "/bin/dbt", ["ls"]) == 3
    assert captured.calls[1][1]["stdout"] is None, "by default dbt writes where ldp writes"


def test_run_dbt_decides_about_colour_from_where_the_output_goes(captured, project, monkeypatch):
    class Terminal:
        def isatty(self):
            return True

        def flush(self):
            pass

    monkeypatch.setattr(sys, "stdout", Terminal())
    monkeypatch.setattr("local_data_platform.dbt_layer.project.os.isatty", lambda descriptor: False)

    run_dbt(project, "/bin/dbt", ["ls"], stdout=2)

    assert "--no-use-colors" in captured.calls[0][0], "standard output is a terminal, but dbt writes to descriptor 2"


# --------------------------------------------------------------------------- where dbt writes its artifacts


def test_the_target_path_comes_from_the_arguments_first(project, monkeypatch):
    monkeypatch.setenv("DBT_TARGET_PATH", "from_env")

    assert find_target_path(project, ["--select", "x", "--target-path", "out"]) == "out"
    assert find_target_path(project, ["--target-path=elsewhere"]) == "elsewhere"
    assert find_target_path(project, ["--target-path"]) == "from_env"  # a flag with no value is ignored


def test_the_target_path_then_comes_from_the_environment(project, monkeypatch):
    monkeypatch.setenv("DBT_TARGET_PATH", "from_env")

    assert find_target_path(project) == "from_env"


def test_the_target_path_then_comes_from_dbt_project_yml(project, monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.delenv("DBT_TARGET_PATH", raising=False)
    (project.directory / "dbt_project.yml").write_text('name: shop\ntarget-path: "build"\n')

    assert find_target_path(project) == "build"


def test_the_target_path_defaults_to_target(project, monkeypatch):
    monkeypatch.delenv("DBT_TARGET_PATH", raising=False)

    assert find_target_path(project) == "target"  # with no dbt_project.yml to read, or none that sets it
    (project.directory / "dbt_project.yml").write_text("name: shop\n")
    assert find_target_path(project) == "target"


# --------------------------------------------------------------------------- the profile and ldp_dbt.json agree


def write_dbt_files(project: DbtProject, path: str, *, target: str = "dev") -> None:
    (project.directory / "dbt_project.yml").write_text("name: shop\nprofile: shop\n")
    (project.directory / "profiles.yml").write_text(
        f"shop:\n  target: {target}\n  outputs:\n    dev:\n      type: duckdb\n      path: {path}\n")


def test_a_profile_that_points_at_the_build_file_passes(project):
    pytest.importorskip("yaml")
    write_dbt_files(project, "target/ldp_build.duckdb")
    check_profile(project)
    write_dbt_files(project, "./target/../target/ldp_build.duckdb")
    check_profile(project)
    write_dbt_files(project, str(project.database))
    check_profile(project)


def test_a_profile_that_points_elsewhere_fails_with_both_paths(project):
    pytest.importorskip("yaml")
    write_dbt_files(project, "other/build.duckdb")

    with pytest.raises(ConfigError) as error:
        check_profile(project)

    text = str(error.value)
    assert str(project.database) in text and str(project.directory.resolve() / "other" / "build.duckdb") in text
    assert "Make the two match" in text


def test_the_profile_check_stands_aside_when_it_cannot_decide(project):
    pytest.importorskip("yaml")
    check_profile(project)  # no dbt files at all
    write_dbt_files(project, "{{ env_var('BUILD') }}")  # a Jinja path
    check_profile(project)
    write_dbt_files(project, "x.duckdb", target="missing")  # a target the profile does not have
    check_profile(project)
    (project.directory / "profiles.yml").write_text("- not: a mapping\n")
    check_profile(project)


# --------------------------------------------------------------------------- ldp dbt init


@pytest.fixture
def lake(tmp_path) -> Path:
    return build_lake(tmp_path / "lake")


def test_init_writes_a_project_that_loads_and_points_at_the_lake(tmp_path, lake):
    written = init_project(tmp_path / "shop", lake, tmp_path / "lake" / "gold")

    folder = tmp_path / "shop"
    assert sorted(path.relative_to(folder).as_posix() for path in written) == [
        ".gitignore", "dbt_project.yml", "ldp_dbt.json", "models/gold/gold_example.sql", "models/schema.yml",
        "models/staging/stg_example.sql", "profiles.yml"]
    project = DbtProject.load(folder)
    assert project.source_configs == (lake,)
    assert project.publish_catalog["identifier"] == "gold"
    assert resolve_path(project.publish_catalog["warehouse_path"], folder) == tmp_path / "lake" / "gold"
    assert "../lake/config" in (folder / "ldp_dbt.json").read_text()  # relative, so the project can move with its lake
    assert (folder / ".gitignore").read_text().splitlines() == ["target/", "logs/", "dbt_packages/"]


def test_init_names_the_dbt_project_after_the_folder_and_keeps_dbt_off_the_network(tmp_path, lake):
    init_project(tmp_path / "shop", lake, tmp_path / "gold")

    dbt_project = (tmp_path / "shop" / "dbt_project.yml").read_text()
    profiles = (tmp_path / "shop" / "profiles.yml").read_text()
    assert "name: shop\n" in dbt_project and "profile: shop\n" in dbt_project
    assert "send_anonymous_usage_stats: false" in dbt_project
    assert profiles.startswith("shop:\n") and "path: target/ldp_build.duckdb" in profiles
    assert "autoinstall_known_extensions: false" in profiles


@pytest.mark.parametrize("folder, name", [
    ("My Shop-1", "my_shop_1"), ("2024 data", "ldp_2024_data"), ("###", "ldp_dbt")])
def test_init_makes_the_dbt_project_name_valid(tmp_path, folder, name):
    init_project(tmp_path / folder, tmp_path / "none", tmp_path / "gold")

    assert (tmp_path / folder / "dbt_project.yml").read_text().startswith(f"name: {name}\n")


def test_init_builds_the_staging_model_on_a_table_of_the_lake(tmp_path, lake):
    init_project(tmp_path / "shop", lake, tmp_path / "gold")

    staging = (tmp_path / "shop" / "models" / "staging" / "stg_example.sql").read_text()
    gold = (tmp_path / "shop" / "models" / "gold" / "gold_example.sql").read_text()
    assert re.search(r"select \* from \{\{ source\('(raw|ref)', '\w+'\) \}\}", staging), staging
    assert "ref('stg_example')" in gold and "'ldp_publish': true" in gold


def test_init_with_no_tables_yet_builds_the_staging_model_on_a_constant(tmp_path):
    init_project(tmp_path / "shop", tmp_path / "not_there_yet", tmp_path / "gold")

    staging = (tmp_path / "shop" / "models" / "staging" / "stg_example.sql").read_text()
    assert "select 1 as id" in staging and "source(" not in staging
    assert "There were no tables yet" in staging


def test_init_accepts_an_empty_folder(tmp_path, lake):
    (tmp_path / "shop").mkdir()

    init_project(tmp_path / "shop", lake, tmp_path / "gold")

    assert (tmp_path / "shop" / "ldp_dbt.json").is_file()


def test_init_never_overwrites_anything(tmp_path, lake):
    folder = tmp_path / "shop"
    init_project(folder, lake, tmp_path / "gold")
    model = folder / "models" / "gold" / "gold_example.sql"
    model.write_text("select 'mine' as edited")

    with pytest.raises(ConfigError, match="is not empty"):
        init_project(folder, lake, tmp_path / "gold")

    assert model.read_text() == "select 'mine' as edited"
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "notes.txt").write_text("keep me")
    with pytest.raises(ConfigError, match="is not empty"):
        init_project(tmp_path / "other", lake, tmp_path / "gold")
    assert [path.name for path in (tmp_path / "other").iterdir()] == ["notes.txt"]


def test_init_refuses_a_path_that_is_a_file(tmp_path, lake):
    (tmp_path / "shop").write_text("a file")

    with pytest.raises(ConfigError, match="is a file, not a folder"):
        init_project(tmp_path / "shop", lake, tmp_path / "gold")
