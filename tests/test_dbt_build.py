"""``dbt_layer.build``, the steps that ``ldp dbt build``, ``ldp dbt test`` and the ``dbt`` task of a workflow share.

The commands themselves are tested through ``main`` in ``test_dbt_cli.py``, and the task in ``test_workflow_dbt.py``.
Here are the parts that a caller reads directly: the result, the two errors and what makes one of them a data gate, the
summary of the sources and what ``prepare`` does and does not touch.
"""

import json
import os

import pytest

from local_data_platform.dbt_layer import build
from local_data_platform.dbt_layer.publish import PublishOutcome
from local_data_platform.dbt_layer.sources import RegisteredSource, SourceRegistration
from local_data_platform.exceptions import ConfigError, DataQualityError, EngineNotFound
from tests.support import GOLD_META, TOTALS_DOCS, FakeDbt, build_lake, copy_fixture_project, dbt_artifacts, dbt_model


@pytest.fixture
def lake(tmp_path):
    return build_lake(tmp_path / "lake")


@pytest.fixture
def project(tmp_path, lake):
    return build.DbtProject.load(copy_fixture_project(tmp_path / "proj"))


@pytest.fixture
def ready(project):
    """The project with its sources registered, as a run of dbt finds it, so that the build file exists."""
    build.register(project, mode="copy")
    return project


@pytest.fixture
def fake(tmp_path) -> FakeDbt:
    return FakeDbt(tmp_path / "fake")


DATA_TEST, UNIT_TEST, MODEL = "test.shop.not_null_x.abc123", "unit_test.shop.fct.adds_up", "model.shop.fct"


@pytest.mark.parametrize("failed, gate", [
    ([], False),
    ([{"node": DATA_TEST, "status": "fail"}], True),
    ([{"node": DATA_TEST, "status": "fail"}, {"node": "test.shop.unique_y.def456", "status": "fail"}], True),
    ([{"node": MODEL, "status": "error"}], False),
    ([{"node": DATA_TEST, "status": "fail"}, {"node": MODEL, "status": "error"}], False),
    ([{"node": MODEL, "status": "runtime error"}], False),
    ([{"node": DATA_TEST, "status": "error"}], False),
    ([{"node": UNIT_TEST, "status": "fail"}], False),
    ([{"node": DATA_TEST, "status": "fail"}, {"node": UNIT_TEST, "status": "fail"}], False),
])
def test_only_failed_data_tests_are_a_data_gate(failed, gate):
    error = build.DbtFailed("dbt failed", command="build", status=1, failed=failed, nodes={"fail": 1})

    assert error.data_gate is gate
    assert error.command == "build" and error.status == 1 and error.failed == failed and error.nodes == {"fail": 1}


def test_a_blocked_write_is_a_data_gate_and_any_other_failed_write_is_not():
    blocked = PublishOutcome([], ("m", DataQualityError("held")), None, [])
    broken = PublishOutcome([], ("m", RuntimeError("disk")), None, [])

    assert build.PublishFailed("x", blocked).data_gate is True
    assert build.PublishFailed("x", broken).data_gate is False
    assert build.PublishFailed("x", PublishOutcome([])).data_gate is False


def _source(kind="view", name="orders"):
    return RegisteredSource(schema="raw", name=name, kind=kind, snapshot_id=1, columns=(("id", "BIGINT"),))


@pytest.mark.parametrize("sources, dropped, expected", [
    ([], [], "registered no sources (no Iceberg tables were found)"),
    ([_source()], [], "registered 1 source as views over Iceberg snapshots"),
    ([_source(), _source(name="b")], [], "registered 2 sources as views over Iceberg snapshots"),
    ([_source("table")], [], "registered 1 source as copies of Iceberg snapshots"),
    ([_source(), _source("table", "b"), _source("table", "c")], [], "registered 3 sources (1 view, 2 copies)"),
    ([_source()], ["raw.gone"], "registered 1 source as views over Iceberg snapshots, removed 1 that no longer exist"),
])
def test_the_summary_of_the_sources(sources, dropped, expected, tmp_path):
    registration = SourceRegistration(database=tmp_path / "b.duckdb", sources=sources, dropped=dropped)

    assert build.registration_summary(registration) == expected


def test_prepare_finds_everything_before_it_writes_anything(tmp_path, project, fake):
    with pytest.raises(EngineNotFound, match="dbt was not found at"):
        build.prepare(project.directory, dbt_bin=tmp_path / "nowhere" / "dbt")

    assert not project.sources_file.exists() and not project.database.exists()
    prepared = build.prepare(project.directory, dbt_bin=fake.path)
    assert prepared.project.directory == project.directory and prepared.binary == str(fake.path)
    assert not project.sources_file.exists(), "prepare registers no sources; register does"


def test_prepare_reports_a_project_that_is_not_there(tmp_path, fake):
    with pytest.raises(ConfigError, match="dbt project folder not found"):
        build.prepare(tmp_path / "nowhere", dbt_bin=fake.path)


def test_register_writes_the_sources_and_checks_the_mode(project):
    registration = build.register(project, mode="copy")

    identifiers = sorted(source.identifier for source in registration.sources)
    assert identifiers == ["raw.customers", "raw.orders", "ref.cities"]
    assert {source.kind for source in registration.sources} == {"table"} and project.sources_file.is_file()
    with pytest.raises(ConfigError, match="the sources mode must be one of"):
        build.register(project, mode="mirror")


def _will(fake, project, *, exit=0, statuses=None, meta=True, artifacts=True):
    models = [dbt_model("gold_totals", meta=GOLD_META if meta else {}, **TOTALS_DOCS)]
    manifest, results = dbt_artifacts(*models, statuses=statuses)
    results["elapsed_time"] = 1.5
    plan = {"database": str(project.database), "exit": exit,
            "sql": ["CREATE OR REPLACE TABLE main.gold_totals AS SELECT 3::BIGINT AS orders, 7.5 AS revenue"]}
    if artifacts:
        plan["artifacts"] = {"manifest.json": manifest, "run_results.json": results}
    fake.will(**plan)


def test_a_build_returns_what_it_ran_and_published(ready, fake):
    _will(fake, ready)

    result = build.run_build(ready, str(fake.path), ["--select", "gold_totals"])

    assert result.command == "build" and result.nodes == {"success": 1} and result.elapsed_s == 1.5
    assert [item.model for item in result.planned] == ["gold_totals"]
    assert [(item.table, item.mode, item.rows_after) for item in result.published] == [
        ("gold.gold_totals", "overwrite", 1)]
    assert fake.calls[0]["argv"][2:5] == ["build", "--select", "gold_totals"]


def test_a_build_that_publishes_nothing_says_so_in_its_result(ready, fake):
    _will(fake, ready, meta=False)

    result = build.run_build(ready, str(fake.path))

    assert result.planned == () and result.published == () and result.nodes == {"success": 1}


def test_a_failed_build_raises_with_the_nodes_that_failed(ready, fake):
    _will(fake, ready, exit=1, statuses={"gold_totals": "error"})

    with pytest.raises(build.DbtFailed) as caught:
        build.run_build(ready, str(fake.path))

    error = caught.value
    assert error.status == 1 and error.command == "build" and error.nodes == {"error": 1} and not error.data_gate
    assert str(error).splitlines()[0] == "dbt build failed with exit status 1, so nothing was published"
    assert error.failed == [{"node": "model.shop.gold_totals", "status": "error", "message": "OK"}]


def test_a_failed_build_without_artifacts_has_no_nodes(ready, fake):
    _will(fake, ready, exit=2, artifacts=False)

    with pytest.raises(build.DbtFailed) as caught:
        build.run_build(ready, str(fake.path))

    assert caught.value.failed == [] and caught.value.nodes == {} and caught.value.status == 2


def test_a_test_run_returns_its_node_counts_and_publishes_nothing(ready, fake, tmp_path):
    _will(fake, ready)

    result = build.run_test(ready, str(fake.path), ["--select", "stg_orders"])

    assert result.command == "test" and result.nodes == {"success": 1} and result.planned == ()
    assert fake.calls[0]["argv"][2:5] == ["test", "--select", "stg_orders"]


def test_a_test_run_without_artifacts_is_still_a_success(ready, fake):
    fake.will(exit=0)

    result = build.run_test(ready, str(fake.path))

    assert result.nodes == {} and result.elapsed_s is None


def test_a_failed_test_run_keeps_the_short_message_and_the_nodes(ready, fake):
    manifest, results = dbt_artifacts(dbt_model("gold_totals"))
    results["results"].append({"unique_id": DATA_TEST, "status": "fail", "message": "Got 2 results"})
    fake.will(exit=1, artifacts={"manifest.json": manifest, "run_results.json": results})

    with pytest.raises(build.DbtFailed) as caught:
        build.run_test(ready, str(fake.path))

    assert str(caught.value) == "dbt test exited with status 1"
    assert caught.value.data_gate is True and caught.value.failed == [
        {"node": DATA_TEST, "status": "fail", "message": "Got 2 results"}]


def test_an_artifact_that_dbt_did_not_write_this_time_is_not_read(ready, fake):
    target = ready.directory / "target"
    (target / "run_results.json").write_text(json.dumps({"results": [{"unique_id": "x", "status": "fail"}]}))
    os.utime(target / "run_results.json", (1_000_000, 1_000_000))
    fake.will(exit=1)

    with pytest.raises(build.DbtFailed) as caught:
        build.run_test(ready, str(fake.path))

    assert caught.value.failed == [] and caught.value.nodes == {}
