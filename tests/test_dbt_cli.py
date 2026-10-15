"""The ``ldp dbt`` command, driven through ``main`` with a stand-in for the dbt executable.

The stand-in (``FakeDbt``) is a small script that records how it was called and writes the files and
relations a dbt run would, so everything LDP does around dbt is tested without dbt: registering the
sources first, the arguments dbt gets, reading its artifacts, publishing, and every way a run can fail.
The tests that run the real dbt are in ``test_dbt_end_to_end.py``.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from local_data_platform.cli import build_parser, main
from local_data_platform.dbt_layer import cli as dbt_cli
from local_data_platform.format.iceberg import Iceberg
from tests.support import (
    GOLD_META,
    TOTALS_DOCS,
    FakeDbt,
    build_lake,
    copy_fixture_project,
    dataset_config,
    dbt_artifacts,
    dbt_model,
    relations,
    requires_iceberg_extension,
    without_log_warnings,
)


@pytest.fixture
def lake(tmp_path) -> Path:
    return build_lake(tmp_path / "lake")


@pytest.fixture
def project(tmp_path, lake) -> Path:
    """The fixture dbt project in ``tmp/proj``, which reads the lake and publishes into it."""
    return copy_fixture_project(tmp_path / "proj")


@pytest.fixture
def fake(tmp_path) -> FakeDbt:
    return FakeDbt(tmp_path / "fake")


def ldp(capfd, *argv) -> tuple[int, str, str]:
    status = main([str(item) for item in argv])
    out, err = capfd.readouterr()
    return status, out, without_log_warnings(err)


def build(capfd, fake, project, *options, dbt_args=()):
    """``ldp dbt build`` with the stand-in, the way a user would type it."""
    return ldp(capfd, "dbt", "build", "--dbt-bin", fake.path, *options, project, *dbt_args)


ORDERS = {"materialized": "incremental", "unique_key": "order_id",
          "meta": {"ldp_publish": True, "ldp_namespace": "silver"}}
TOTALS = {"meta": GOLD_META, "depends_on": ["fct_orders"], **TOTALS_DOCS}
BUILT = [
    "CREATE OR REPLACE TABLE main.fct_orders AS SELECT i::BIGINT AS order_id, 'o' || i AS label, i * 1.5 AS amount "
    "FROM range(1, 4) t(i)",
    "CREATE OR REPLACE TABLE main.gold_totals AS SELECT 3::BIGINT AS orders, 7.5 AS revenue",
]


def succeed(fake, project, *, models=None, statuses=None, args=None, exit=0, **plan) -> None:
    """Make the stand-in build ``fct_orders`` and ``gold_totals`` and write the artifacts for them."""
    manifest, run_results = dbt_artifacts(
        *(models or (dbt_model("fct_orders", **ORDERS), dbt_model("gold_totals", **TOTALS))),
        statuses=statuses, args=args)
    fake.will(database=str(project / "target" / "ldp_build.duckdb"), sql=BUILT, exit=exit,
              artifacts={"manifest.json": manifest, "run_results.json": run_results}, **plan)


def iceberg(tmp_path, namespace, name) -> Iceberg:
    return Iceberg(name, {"identifier": namespace, "warehouse_path": str(tmp_path / "lake" / "warehouse")})


# --------------------------------------------------------------------------- the command line


def test_ldp_help_lists_dbt_without_importing_dbt():
    code = ("import sys\n"
            "from local_data_platform.cli import main\n"
            "assert main(['--help']) == 0 and main(['dbt', '--help']) == 0\n"
            "assert 'local_data_platform.dbt_layer.cli' in sys.modules\n"
            "loaded = sorted(name for name in sys.modules if name == 'dbt' or name.startswith('dbt.'))\n"
            "assert not loaded, loaded\n")

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert re.search(r"^ +dbt +run a dbt project over LDP tables", result.stdout, re.MULTILINE), result.stdout


def test_dbt_help_lists_its_commands(capfd):
    status, out, _ = ldp(capfd, "dbt", "--help")

    assert status == 0
    for command in ("init", "build", "test", "docs", "exec"):
        assert f"\n    {command} " in out, out
    # argparse wraps the description, even at the hyphens of "local-data-platform".
    assert 'pipinstall"local-data-platform[dbt]"' in "".join(out.split())


def test_dbt_without_a_command_prints_the_usage_and_fails(capfd):
    status, out, err = ldp(capfd, "dbt")

    assert status == 1 and out == "" and "COMMAND" in err and "build" in err


@pytest.mark.parametrize("argv, expected", [
    (["dbt", "build"], "PROJECT_DIR"),
    (["dbt", "init", "proj"], "--sources"),
    (["dbt", "docs", "--port", "70000", "proj"], "expected a port from 1 to 65535"),
    (["dbt", "docs", "--port", "web", "proj"], "expected a port number"),
    (["dbt", "build", "--sources-mode", "link", "proj"], "invalid choice: 'link'"),
])
def test_usage_errors_exit_with_1_and_say_what_is_wrong(capfd, argv, expected):
    status, _, err = ldp(capfd, *argv)

    assert status == 1 and expected in err


@pytest.mark.parametrize("argv", [
    ["dbt", "build", "proj", "--select", "x", "--threads", "2"],
    ["dbt", "build", "proj", "--", "--select", "x", "--threads", "2"],
])
def test_everything_after_the_project_goes_to_dbt_with_or_without_a_separator(argv):
    args = build_parser().parse_args(argv)

    assert dbt_cli._dbt_arguments(args) == ["--select", "x", "--threads", "2"]
    assert args.project_dir == "proj"


def test_options_of_ldp_go_before_the_project_and_everything_after_it_is_dbts():
    args = build_parser().parse_args(["dbt", "build", "--sources-mode", "copy", "--no-sources", "proj",
                                      "--sources-mode", "x", "--dbt-bin", "y"])

    assert args.sources_mode == "copy" and args.no_sources is True and args.dbt_bin is None
    assert dbt_cli._dbt_arguments(args) == ["--sources-mode", "x", "--dbt-bin", "y"]


# --------------------------------------------------------------------------- ldp dbt init


def test_init_creates_the_project_and_says_what_to_do_next(capfd, tmp_path, lake):
    status, out, err = ldp(capfd, "dbt", "init", tmp_path / "shop", "--sources", lake, "--warehouse", tmp_path / "gold")

    assert status == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == f"ldp: created a dbt project in {tmp_path / 'shop'}"
    assert "  ldp_dbt.json" in lines and "  models/gold/gold_example.sql" in lines
    assert lines[-1] == f"Next: ldp dbt build {tmp_path / 'shop'}"


def test_init_refuses_a_folder_that_is_not_empty(capfd, tmp_path, lake, project):
    status, out, err = ldp(capfd, "dbt", "init", project, "--sources", lake, "--warehouse", tmp_path / "gold")

    assert status == 1 and out == ""
    assert err.startswith(f"ldp: error: {project} is not empty.")


# --------------------------------------------------------------------------- ldp dbt build: what runs and in what order


def test_build_registers_the_sources_then_runs_dbt_in_the_project_folder(capfd, fake, project, tmp_path):
    succeed(fake, project)

    status, out, err = build(capfd, fake, project, dbt_args=["--select", "gold_totals"])

    assert status == 0, out + err
    [call] = fake.calls
    assert Path(call["cwd"]).resolve() == project.resolve()  # dbt runs with the project as its working directory
    assert call["argv"] == ["--no-version-check", "--no-use-colors", "build", "--select", "gold_totals",
                            "--project-dir", str(project), "--profiles-dir", str(project)]
    assert call["sources_yaml_existed"] is True  # the sources were registered before dbt started
    assert "orders" in (project / "models" / "_ldp_sources.yml").read_text()
    assert {"raw.orders", "raw.customers", "ref.cities"} <= set(relations(project / "target" / "ldp_build.duckdb"))
    assert out.index("ldp: registered 3 sources") < out.index("fake dbt ran") < out.index("ldp: published")


def test_build_publishes_the_marked_models_and_prints_a_line_for_each(capfd, fake, project, tmp_path):
    succeed(fake, project)

    status, out, err = build(capfd, fake, project)

    assert status == 0 and err == ""
    published = [line for line in out.splitlines() if line.startswith("ldp: published")]
    assert len(published) == 2
    assert published[0].startswith("ldp: published silver.fct_orders: upsert, 0 -> 3 rows, snapshot ")
    assert published[1].startswith("ldp: published gold.gold_totals: overwrite, 0 -> 1 rows, snapshot ")
    assert sorted(iceberg(tmp_path, "silver", "fct_orders").get().column("order_id").to_pylist()) == [1, 2, 3]
    assert iceberg(tmp_path, "gold", "gold_totals").get().to_pylist() == [{"orders": 3, "revenue": 7.5}]


def test_the_generated_configs_work_with_the_other_commands(capfd, fake, project, tmp_path, lake):
    succeed(fake, project)
    build(capfd, fake, project)

    status, out, _ = ldp(capfd, "snapshots", lake / "gold_totals.json")
    assert status == 0 and "Snapshots of Iceberg table gold.gold_totals" in out
    status, out, _ = ldp(capfd, "plan", lake / "fct_orders.json")
    assert status == 0, out
    config = json.loads((lake / "fct_orders.json").read_text())
    assert config["how"] == "ldp dbt build" and config["where"] == "dbt project shop"
    assert config["metadata"]["source"]["format"] == "DUCKDB"
    assert config["metadata"]["source"]["relation"] == "main.fct_orders"


def test_a_second_build_does_not_register_its_own_published_tables_as_sources(capfd, fake, project, lake):
    # The generated configs are in the same folder as the source configs, and name tables that this project built.
    succeed(fake, project)
    build(capfd, fake, project)

    status, out, err = build(capfd, fake, project)

    assert status == 0, out + err
    assert "ldp: registered 3 sources" in out  # raw.orders, raw.customers and ref.cities, not silver or gold
    assert "silver" not in " ".join(relations(project / "target" / "ldp_build.duckdb"))


def test_nothing_marked_means_nothing_is_published_and_the_build_still_succeeds(capfd, fake, project, tmp_path):
    succeed(fake, project, models=(dbt_model("fct_orders"), dbt_model("gold_totals")))

    status, out, err = build(capfd, fake, project)

    assert status == 0 and err == ""
    assert "ldp: no model marked ldp_publish: true ran in this build, so nothing was published" in out
    assert not (tmp_path / "lake" / "gold_totals.json").exists()


def test_dbt_is_told_to_print_no_colour_codes_when_its_output_is_not_a_terminal(capfd, fake, project):
    succeed(fake, project)

    build(capfd, fake, project)

    assert "--no-use-colors" in fake.calls[0]["argv"]  # capfd is not a terminal, and nor is a pipe or a log file


# --------------------------------------------------------------------------- ldp dbt build: how it fails


def test_a_failed_dbt_run_publishes_nothing_and_lists_the_failed_nodes(capfd, fake, project, tmp_path):
    manifest, run_results = dbt_artifacts(dbt_model("fct_orders", **ORDERS), dbt_model("gold_totals", **TOTALS),
                                          statuses={"gold_totals": "skipped"})
    run_results["results"].append({"unique_id": "test.shop.unique_fct_orders_order_id.1f", "status": "fail",
                                   "message": "Got 1 result, configured to fail if != 0\nsecond line"})
    fake.will(database=str(project / "target" / "ldp_build.duckdb"), sql=BUILT, exit=1,
              artifacts={"manifest.json": manifest, "run_results.json": run_results})

    status, out, err = build(capfd, fake, project)

    assert status == 1 and "ldp: published" not in out
    assert err.splitlines() == [
        "ldp: error: dbt build failed with exit status 1, so nothing was published",
        "  - test.shop.unique_fct_orders_order_id.1f: fail, Got 1 result, configured to fail if != 0"]
    assert not iceberg(tmp_path, "silver", "fct_orders").exists()
    assert not (tmp_path / "lake" / "fct_orders.json").exists()  # nothing was even planned


def test_a_dbt_run_that_failed_before_writing_artifacts_still_fails_cleanly(capfd, fake, project):
    fake.will(exit=2, say="Compilation Error")

    status, out, err = build(capfd, fake, project)

    assert status == 1 and "Compilation Error" in out
    assert err.splitlines() == ["ldp: error: dbt build failed with exit status 2, so nothing was published"]


def test_artifacts_left_by_an_earlier_run_are_not_trusted_after_a_failure(capfd, fake, project):
    manifest, run_results = dbt_artifacts(dbt_model("fct_orders", **ORDERS), statuses={"fct_orders": "error"})
    target = project / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    (target / "run_results.json").write_text(json.dumps(run_results))
    for name in ("manifest.json", "run_results.json"):
        os.utime(target / name, (time.time() - 3600, time.time() - 3600))
    fake.will(exit=2)  # this run wrote nothing

    status, _, err = build(capfd, fake, project)

    assert status == 1 and err.splitlines() == [
        "ldp: error: dbt build failed with exit status 2, so nothing was published"]  # not "fct_orders: error"


def test_a_successful_dbt_run_that_wrote_no_artifacts_is_an_error(capfd, fake, project):
    fake.will()

    status, _, err = build(capfd, fake, project)

    assert status == 1 and "manifest.json, so there is nothing to publish" in err


def test_artifacts_from_an_earlier_run_are_never_published(capfd, fake, project, tmp_path):
    manifest, run_results = dbt_artifacts(dbt_model("fct_orders", **ORDERS))
    target = project / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps(manifest))
    (target / "run_results.json").write_text(json.dumps(run_results))
    for name in ("manifest.json", "run_results.json"):
        os.utime(target / name, (time.time() - 3600, time.time() - 3600))
    fake.will()  # a run with --no-write-json

    status, _, err = build(capfd, fake, project)

    assert status == 1 and "was not written by this dbt run" in err and "--no-write-json" in err
    assert not iceberg(tmp_path, "silver", "fct_orders").exists()


def test_a_bad_meta_key_fails_after_dbt_ran_and_before_anything_is_written(capfd, fake, project, tmp_path):
    bad = dbt_model("gold_totals", meta={"ldp_publish": True, "ldp_write_mode": "merge"})
    succeed(fake, project, models=(dbt_model("fct_orders", **ORDERS), bad))

    status, out, err = build(capfd, fake, project)

    assert status == 1 and "ldp: published" not in out
    assert err.startswith("ldp: error: cannot publish, 1 problem in the dbt models' meta, and nothing was published:")
    assert "model gold_totals: meta ldp_write_mode must be one of" in err
    assert not iceberg(tmp_path, "silver", "fct_orders").exists()  # the good model was not published either


def test_a_blocked_quality_check_stops_the_run_and_says_what_was_and_was_not_published(capfd, fake, project, tmp_path):
    blocked = {"checks": [{"check": "row_count", "min": 100}], "on_failure": "fail"}
    totals = dbt_model("gold_totals", meta={**GOLD_META, "ldp_quality": blocked}, depends_on=["fct_orders"],
                       **TOTALS_DOCS)
    succeed(fake, project, models=(dbt_model("fct_orders", **ORDERS), totals))

    status, out, err = build(capfd, fake, project)

    assert status == 1
    assert "ldp: published silver.fct_orders: upsert" in out and "gold.gold_totals" not in out
    lines = err.splitlines()
    assert lines[0] == "ldp: error: publishing gold_totals failed, so the models after it were not tried"
    failed_config = tmp_path / "lake" / "config" / "gold_totals.json"
    assert lines[-3:] == ["  published: silver.fct_orders", "  not published: gold_totals",
                          f"  to retry gold_totals by hand: ldp run {failed_config}"]
    assert iceberg(tmp_path, "silver", "fct_orders").exists() and not iceberg(tmp_path, "gold", "gold_totals").exists()
    assert (tmp_path / "lake" / "config" / "gold_totals.json").is_file()  # written, so it can be fixed and run by hand


def test_a_dbt_empty_build_is_not_published(capfd, fake, project, tmp_path):
    succeed(fake, project, args={"empty": True})

    status, _, err = build(capfd, fake, project)

    assert status == 1 and "dbt ran with --empty" in err
    assert not iceberg(tmp_path, "gold", "gold_totals").exists()


def test_a_target_path_given_to_dbt_is_where_the_artifacts_are_read(capfd, fake, project, tmp_path):
    succeed(fake, project, target="out")

    status, out, err = build(capfd, fake, project, dbt_args=["--target-path", "out"])

    assert status == 0, out + err
    assert fake.calls[0]["argv"][:5] == ["--no-version-check", "--no-use-colors", "build", "--target-path", "out"]
    assert iceberg(tmp_path, "gold", "gold_totals").exists()


# --------------------------------------------------------------------------- setting up: dbt, the profile, the sources


def test_without_dbt_it_names_the_extra_and_changes_nothing(capfd, project, tmp_path):
    status, out, err = ldp(capfd, "dbt", "build", "--dbt-bin", tmp_path / "no" / "dbt", project)

    assert status == 1 and out == ""
    assert err.startswith("ldp: error: dbt was not found at ") and 'pip install "local-data-platform[dbt]"' in err
    assert not (project / "target").exists() and not (project / "models" / "_ldp_sources.yml").exists()


def test_a_project_without_ldp_dbt_json_says_how_to_create_one(capfd, fake, tmp_path):
    (tmp_path / "bare").mkdir()

    status, _, err = ldp(capfd, "dbt", "build", "--dbt-bin", fake.path, tmp_path / "bare")

    assert status == 1 and "has no ldp_dbt.json. Create a project with: ldp dbt init" in err
    assert fake.calls == []


def test_a_profile_for_another_build_file_fails_before_anything_runs(capfd, fake, project):
    pytest.importorskip("yaml")
    profiles = project / "profiles.yml"
    profiles.write_text(profiles.read_text().replace("target/ldp_build.duckdb", "other/build.duckdb"))

    status, _, err = build(capfd, fake, project)

    assert status == 1 and "profiles.yml sets path to" in err and "Make the two match" in err
    assert fake.calls == [] and not (project / "target").exists()


def test_a_table_that_is_not_there_yet_is_a_warning_and_the_build_goes_on(capfd, fake, project, lake):
    (lake / "raw.later.json").write_text(json.dumps(dataset_config("raw", "later")))
    succeed(fake, project)

    status, out, err = build(capfd, fake, project)

    assert status == 0 and "ldp: registered 3 sources" in out
    assert err.startswith("ldp: warning: raw.later is not a source yet: ")


def test_sources_can_be_registered_as_copies(capfd, fake, project):
    succeed(fake, project)

    status, out, _ = build(capfd, fake, project, "--sources-mode", "copy")

    assert status == 0 and "ldp: registered 3 sources as copies of Iceberg snapshots" in out
    assert set(relations(project / "target" / "ldp_build.duckdb").values()) == {"BASE TABLE"}  # no views anywhere


@requires_iceberg_extension
def test_sources_can_be_registered_as_views(capfd, fake, project):
    succeed(fake, project)

    status, out, _ = build(capfd, fake, project, "--sources-mode", "view")

    assert status == 0 and "ldp: registered 3 sources as views over Iceberg snapshots" in out


def test_with_no_source_tables_it_says_so(capfd, fake, project, lake):
    for config in lake.glob("*.json"):
        config.unlink()
    succeed(fake, project)

    status, out, _ = build(capfd, fake, project)

    assert status == 0 and "ldp: registered no sources (no Iceberg tables were found)" in out


def test_no_sources_skips_registering(capfd, fake, project):
    fake.will()

    status, out, _ = ldp(capfd, "dbt", "exec", "--dbt-bin", fake.path, "--no-sources", project, "ls")

    assert status == 0 and "registered" not in out
    assert fake.calls[0]["sources_yaml_existed"] is False
    assert not (project / "target").exists()


# --------------------------------------------------------------------------- test, docs and exec


def test_test_runs_dbt_test_and_publishes_nothing(capfd, fake, project, tmp_path):
    succeed(fake, project)

    status, out, _ = ldp(capfd, "dbt", "test", "--dbt-bin", fake.path, project, "--select", "fct_orders")

    assert status == 0 and "ldp: published" not in out
    assert fake.calls[0]["argv"][2:5] == ["test", "--select", "fct_orders"]
    assert fake.calls[0]["sources_yaml_existed"] is True
    assert not iceberg(tmp_path, "gold", "gold_totals").exists()


@pytest.mark.parametrize("command, shows", [("test", "dbt test exited with status 1"),
                                            ("exec", "dbt exited with status 1")])
def test_a_failing_dbt_command_exits_with_1(capfd, fake, project, command, shows):
    fake.will(exit=1)
    extra = ["ls"] if command == "exec" else []

    status, _, err = ldp(capfd, "dbt", command, "--dbt-bin", fake.path, project, *extra)

    assert status == 1 and err.splitlines() == [f"ldp: error: {shows}"]


def test_docs_generates_the_docs(capfd, fake, project):
    status, _, _ = ldp(capfd, "dbt", "docs", "--dbt-bin", fake.path, project, "--no-compile")

    assert status == 0
    [call] = fake.calls
    assert call["argv"][2:5] == ["docs", "generate", "--no-compile"] and call["sources_yaml_existed"] is True


def test_docs_serve_serves_on_localhost_only(capfd, fake, project):
    status, out, _ = ldp(capfd, "dbt", "docs", "--dbt-bin", fake.path, "--serve", "--port", "9000", project)

    assert status == 0 and "ldp: serving the docs on http://127.0.0.1:9000, stop with Ctrl-C" in out
    generate, serve = fake.calls
    assert generate["argv"][2:4] == ["docs", "generate"]
    assert serve["argv"][2:8] == ["docs", "serve", "--host", "127.0.0.1", "--port", "9000"]


def test_docs_do_not_serve_when_generating_failed(capfd, fake, project):
    fake.will(exit=1)

    status, out, err = ldp(capfd, "dbt", "docs", "--dbt-bin", fake.path, "--serve", project)

    assert status == 1 and err.splitlines() == ["ldp: error: dbt docs generate exited with status 1"]
    assert "serving" not in out and len(fake.calls) == 1


def test_ctrl_c_stops_the_docs_server_normally(capfd, fake, project, monkeypatch):
    real = dbt_cli.run_dbt

    def run(project_, binary, arguments):
        if arguments[:2] == ["docs", "serve"]:
            raise KeyboardInterrupt
        return real(project_, binary, arguments)

    monkeypatch.setattr(dbt_cli, "run_dbt", run)

    status, out, _ = ldp(capfd, "dbt", "docs", "--dbt-bin", fake.path, "--serve", project)

    assert status == 0 and "serving the docs" in out


def test_exec_runs_any_dbt_command_and_the_project_flags_come_last(capfd, fake, project):
    status, _, _ = ldp(capfd, "dbt", "exec", "--dbt-bin", fake.path, project, "source", "freshness", "--select", "raw")

    assert status == 0
    assert fake.calls[0]["argv"] == ["--no-version-check", "--no-use-colors", "source", "freshness", "--select", "raw",
                                     "--project-dir", str(project), "--profiles-dir", str(project)]


def test_exec_publishes_nothing_even_when_models_are_marked(capfd, fake, project, tmp_path):
    succeed(fake, project)

    status, out, _ = ldp(capfd, "dbt", "exec", "--dbt-bin", fake.path, project, "build")

    assert status == 0 and "ldp: published" not in out
    assert not iceberg(tmp_path, "gold", "gold_totals").exists()


def test_exec_needs_a_dbt_command(capfd, fake, project):
    status, _, err = ldp(capfd, "dbt", "exec", "--dbt-bin", fake.path, project)

    assert status == 1 and "exec needs a dbt command to run, such as: ldp dbt exec PROJECT_DIR ls" in err
    assert fake.calls == []
