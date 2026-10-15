"""Which dbt models are published to Iceberg, how, and what is written.

Everything here runs without dbt: the plan is built from a ``manifest.json`` and ``run_results.json`` held
as dicts, shaped like the files dbt 1.12 writes. The tests that use dbt end to end are in ``test_dbt_cli.py``.
"""

import itertools
import json
import os
import time
from pathlib import Path

import duckdb
import pytest

from local_data_platform.dbt_layer.project import DbtProject
from local_data_platform.dbt_layer.publish import failed_nodes, plan_publish, publish, read_artifacts
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.spec import spec_hash, validate_spec
from tests.support import dbt_artifacts as artifacts
from tests.support import GOLD_META, TOTALS_DOCS
from tests.support import dbt_model as model

LAKE_CATALOG = {"identifier": "lake", "warehouse_path": "../lake/warehouse"}


@pytest.fixture
def project(tmp_path) -> DbtProject:
    """A project in ``tmp/proj`` that publishes to the lake in ``tmp/lake`` and writes its configs there."""
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "ldp_dbt.json").write_text(json.dumps({
        "publish": {"catalog": LAKE_CATALOG, "config_dir": "../lake/config"}}))
    return DbtProject.load(folder)


def plan(project, *models, **kwargs):
    manifest, run_results = artifacts(*models, **kwargs)
    return plan_publish(project, manifest, run_results)


def problems(error: pytest.ExceptionInfo) -> list[str]:
    return [line.strip()[2:] for line in str(error.value).splitlines()[1:]]


PUBLISH = {"ldp_publish": True}

# --------------------------------------------------------------------------- which models


def test_only_models_marked_ldp_publish_are_published(project):
    items = plan(project, model("marked", meta=PUBLISH), model("plain"), model("off", meta={"ldp_publish": False}))

    assert [item.model for item in items] == ["marked"]


def test_a_model_that_did_not_succeed_is_not_published(project):
    items = plan(project, model("good", meta=PUBLISH), model("bad", meta=PUBLISH), model("skipped", meta=PUBLISH),
                 statuses={"bad": "error", "skipped": "skipped"})

    assert [item.model for item in items] == ["good"]


def test_a_model_that_was_not_part_of_this_invocation_is_not_published(project):
    # --select left "other" out, so run_results.json has no result for it, even though it is marked.
    manifest, run_results = artifacts(model("selected", meta=PUBLISH), model("other", meta=PUBLISH))
    run_results["results"] = [result for result in run_results["results"] if result["unique_id"].endswith("selected")]

    items = plan_publish(project, manifest, run_results)

    assert [item.model for item in items] == ["selected"]


def test_resources_that_are_not_models_are_ignored(project):
    manifest, run_results = artifacts(model("marked", meta=PUBLISH))
    manifest["nodes"]["seed.shop.countries"] = {"unique_id": "seed.shop.countries", "resource_type": "seed",
                                                "name": "countries", "meta": PUBLISH, "config": {"meta": PUBLISH}}
    run_results["results"].append({"unique_id": "seed.shop.countries", "status": "success", "message": "INSERT 3"})

    assert [item.model for item in plan_publish(project, manifest, run_results)] == ["marked"]


def test_a_project_with_nothing_marked_publishes_nothing(project):
    assert plan(project, model("a"), model("b")) == []


def test_meta_set_under_config_counts_the_same_as_meta_on_the_node(project):
    node = model("only_in_config")
    node["meta"] = {}
    node["config"]["meta"] = dict(PUBLISH)

    assert [item.model for item in plan(project, node)] == ["only_in_config"]


# --------------------------------------------------------------------------- how: the write mode


@pytest.mark.parametrize("materialized", ["table", "view"])
def test_a_table_or_a_view_overwrites(project, materialized):
    [item] = plan(project, model("totals", materialized=materialized, meta=PUBLISH))

    assert item.mode == "overwrite" and item.join_cols == ()


@pytest.mark.parametrize("unique_key, join_cols", [("order_id", ("order_id",)), (["a", "b"], ("a", "b"))])
def test_an_incremental_model_with_a_unique_key_upserts_on_it(project, unique_key, join_cols):
    [item] = plan(project, model("orders", materialized="incremental", unique_key=unique_key, meta=PUBLISH))

    assert item.mode == "upsert" and item.join_cols == join_cols
    assert item.config["metadata"]["target"]["join_cols"] == list(join_cols)


def test_an_incremental_model_without_a_unique_key_is_an_error_that_names_the_way_out(project):
    with pytest.raises(ConfigError) as error:
        plan(project, model("events", materialized="incremental", meta=PUBLISH))

    [problem] = problems(error)
    assert problem.startswith("model events: an incremental model without a unique_key")
    assert "ldp_write_mode" in problem and "overwrite" in problem


@pytest.mark.parametrize("mode", ["append", "overwrite"])
def test_ldp_write_mode_overrides_the_default(project, mode):
    [item] = plan(project, model("events", materialized="incremental", meta={**PUBLISH, "ldp_write_mode": mode}))

    assert item.mode == mode


def test_ldp_write_mode_is_read_without_regard_to_case_and_spaces(project):
    [item] = plan(project, model("totals", meta={**PUBLISH, "ldp_write_mode": " Append "}))

    assert item.mode == "append"


def test_an_unknown_write_mode_is_an_error(project):
    with pytest.raises(ConfigError) as error:
        plan(project, model("totals", meta={**PUBLISH, "ldp_write_mode": "merge"}))

    assert problems(error) == ["model totals: meta ldp_write_mode must be one of ['append', 'overwrite', 'upsert'], "
                               "got 'merge'"]


def test_ldp_join_cols_gives_an_upsert_its_key(project):
    [item] = plan(project, model("totals", meta={**PUBLISH, "ldp_write_mode": "upsert", "ldp_join_cols": ["a", "b"]}))
    [other] = plan(project, model("orders", materialized="incremental", unique_key="order_id",
                                  meta={**PUBLISH, "ldp_join_cols": "id"}))

    assert item.mode == "upsert" and item.join_cols == ("a", "b")
    assert other.join_cols == ("id",)  # ldp_join_cols wins over unique_key


def test_an_upsert_without_any_key_is_an_error(project):
    with pytest.raises(ConfigError) as error:
        plan(project, model("totals", meta={**PUBLISH, "ldp_write_mode": "upsert"}))

    assert problems(error) == ["model totals: an upsert needs key columns; set unique_key, or meta ldp_join_cols"]


@pytest.mark.parametrize("materialized", ["ephemeral", "materialized_view", "external"])
def test_a_model_with_no_relation_to_read_is_an_error(project, materialized):
    with pytest.raises(ConfigError) as error:
        plan(project, model("m", materialized=materialized, meta=PUBLISH))

    [problem] = problems(error)
    assert f"a '{materialized}' model has no relation to publish" in problem


# --------------------------------------------------------------------------- where: namespace and table


def test_the_table_is_named_after_the_alias_in_the_namespace_of_the_publish_catalog(project):
    [item] = plan(project, model("totals", alias="revenue_totals", schema="analytics", meta=PUBLISH))

    assert item.table == "lake.revenue_totals"
    assert item.relation == "analytics.revenue_totals"  # the relation dbt built, in the model's schema
    assert item.config["metadata"]["source"]["relation"] == "analytics.revenue_totals"
    assert item.config["metadata"]["target"]["name"] == "revenue_totals"


def test_ldp_namespace_puts_a_model_in_its_own_local_catalog(project):
    [item] = plan(project, model("orders", meta={**PUBLISH, "ldp_namespace": "silver"}))

    assert item.table == "silver.orders"
    catalog = item.config["metadata"]["target"]["catalog"]
    assert catalog == {"identifier": "silver", "warehouse_path": "../warehouse"}  # one catalog file per namespace


def test_ldp_namespace_sets_the_namespace_of_a_catalog_that_is_not_local(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    sql_catalog = {"type": "sql", "uri": "sqlite:///../lake/catalog.db", "warehouse": "../lake/warehouse",
                   "namespace": "lake"}
    (folder / "ldp_dbt.json").write_text(json.dumps({"publish": {"catalog": sql_catalog, "config_dir": "gen"}}))
    project = DbtProject.load(folder)

    [plain] = plan(project, model("a", meta=PUBLISH))
    [moved] = plan(project, model("b", meta={**PUBLISH, "ldp_namespace": "silver"}))

    assert plain.table == "lake.a" and moved.table == "silver.b"
    assert moved.config["metadata"]["target"]["catalog"]["namespace"] == "silver"
    assert "identifier" not in moved.config["metadata"]["target"]["catalog"]


@pytest.mark.parametrize("value", ["", "  ", 3, ["a"]])
def test_a_namespace_that_is_not_a_name_is_an_error(project, value):
    with pytest.raises(ConfigError) as error:
        plan(project, model("a", meta={**PUBLISH, "ldp_namespace": value}))

    assert problems(error) == [f"model a: meta ldp_namespace must be a non-empty string, got {value!r}"]


def test_two_models_that_would_write_one_table_are_an_error(project):
    with pytest.raises(ConfigError) as error:
        plan(project, model("a", alias="same", schema="one", meta=PUBLISH),
             model("b", alias="same", schema="two", meta=PUBLISH))

    assert any("models a, b would all write the table lake.same" in problem for problem in problems(error))


# --------------------------------------------------------------------------- the generated config


def test_the_generated_config_is_an_ordinary_dataset_config(project, tmp_path):
    [item] = plan(project, model("totals", description="Orders and revenue.", meta=PUBLISH))

    assert item.path == tmp_path / "lake" / "config" / "totals.json"
    assert item.config == {
        "identifier": "totals", "what": "Orders and revenue.", "where": "dbt project shop", "how": "ldp dbt build",
        "metadata": {
            "source": {"name": "totals", "format": "DUCKDB", "path": "../../proj/target/ldp_build.duckdb",
                       "relation": "main.totals"},
            "target": {"name": "totals", "format": "ICEBERG", "write_mode": "overwrite",
                       "catalog": {"identifier": "lake", "warehouse_path": "../warehouse"}},
            "docs": {"description": "Orders and revenue."},
        },
    }
    assert validate_spec(item.config) == []


def test_a_model_without_a_description_has_no_what(project):
    [item] = plan(project, model("totals", meta=PUBLISH))

    assert "what" not in item.config


def test_partition_by_quality_and_observability_are_copied_into_the_config(tmp_path):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "ldp_dbt.json").write_text(json.dumps({
        "publish": {"catalog": LAKE_CATALOG}, "observability": {"sinks": ["jsonl"]}}))
    project = DbtProject.load(folder)
    quality = {"checks": [{"check": "not_null", "columns": ["order_id"]}], "on_failure": "fail"}
    partition = [{"column": "order_date", "transform": "month"}]

    [item] = plan(project, model("orders", meta={**PUBLISH, "ldp_quality": quality, "ldp_partition_by": partition}))

    metadata = item.config["metadata"]
    assert metadata["quality"] == quality and metadata["target"]["partition_by"] == partition
    assert metadata["observability"] == {"sinks": ["jsonl"]}
    assert validate_spec(item.config) == []


def test_the_project_name_names_where_the_table_came_from(project):
    manifest, run_results = artifacts(model("totals", meta=PUBLISH))
    del manifest["metadata"]["project_name"]

    [item] = plan_publish(project, manifest, run_results)

    assert item.config["where"] == "a dbt project"


@pytest.mark.parametrize("catalog, expected", [
    ({"identifier": "g", "warehouse_path": "../lake/warehouse"}, {"identifier": "g", "warehouse_path": "../warehouse"}),
    ({"identifier": "g", "warehouse_path": "/abs/warehouse"}, {"identifier": "g", "warehouse_path": "/abs/warehouse"}),
    ({"type": "sql", "uri": "sqlite:///../lake/c.db", "warehouse": "../lake/w", "namespace": "g"},
     {"type": "sql", "uri": "sqlite:///../c.db", "warehouse": "../w", "namespace": "g"}),
    ({"type": "sql", "uri": "sqlite:////abs/c.db", "warehouse": "s3://bucket/w", "namespace": "g"},
     {"type": "sql", "uri": "sqlite:////abs/c.db", "warehouse": "s3://bucket/w", "namespace": "g"}),
    ({"type": "rest", "uri": "https://catalog.example/api", "namespace": "g"},
     {"type": "rest", "uri": "https://catalog.example/api", "namespace": "g"}),
])
def test_relative_paths_in_the_catalog_are_rewritten_for_the_folder_of_the_config(tmp_path, catalog, expected):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "ldp_dbt.json").write_text(json.dumps({"publish": {"catalog": catalog, "config_dir": "../lake/config"}}))
    project = DbtProject.load(folder)

    [item] = plan(project, model("a", meta=PUBLISH))

    assert item.config["metadata"]["target"]["catalog"] == expected
    assert project.publish_catalog == catalog  # the project's own copy is left as it was written


# --------------------------------------------------------------------------- all problems at once, and the guards


def test_every_problem_is_reported_at_once_with_the_name_of_its_model(project):
    with pytest.raises(ConfigError) as error:
        plan(project, model("a", materialized="ephemeral", meta=PUBLISH),
             model("b", meta={**PUBLISH, "ldp_write_mode": "merge"}),
             model("c", meta={**PUBLISH, "ldp_publis": True}),
             model("fine", meta=PUBLISH))

    text = str(error.value)
    assert text.startswith("cannot publish, 3 problems in the dbt models' meta, and nothing was published:")
    assert [problem.split(":")[0] for problem in problems(error)] == ["model a", "model b", "model c"]


def test_unknown_ldp_meta_keys_are_errors_but_other_meta_is_left_alone(project):
    [fine] = plan(project, model("fine", meta={**PUBLISH, "owner": "data team", "contains_pii": False}))
    with pytest.raises(ConfigError) as error:
        plan(project, model("typo", meta={**PUBLISH, "ldp_namespce": "silver"}))

    assert fine.model == "fine"
    [problem] = problems(error)
    assert problem.startswith("model typo: unknown meta keys ['ldp_namespce']; known: ['ldp_publish'")


@pytest.mark.parametrize("flag", ["yes", "true", 1, "False"])
def test_ldp_publish_must_be_a_real_boolean(project, flag):
    with pytest.raises(ConfigError) as error:
        plan(project, model("a", meta={"ldp_publish": flag}))

    assert problems(error) == [f"model a: meta ldp_publish must be true or false, got {flag!r}"]


def test_a_config_that_ldp_plan_would_reject_is_an_error_naming_the_model(project):
    bad_quality = {"checks": [{"check": "no_such_check"}]}

    with pytest.raises(ConfigError) as error:
        plan(project, model("orders", meta={**PUBLISH, "ldp_quality": bad_quality}))

    [problem] = problems(error)
    assert problem.startswith("model orders: ") and "quality" in problem


def test_a_dbt_empty_build_is_never_published(project):
    manifest, run_results = artifacts(model("totals", meta=PUBLISH), args={"empty": True})

    with pytest.raises(ConfigError, match="dbt ran with --empty") as error:
        plan_publish(project, manifest, run_results)

    assert "overwrite Iceberg tables with nothing" in str(error.value)


def test_a_dbt_empty_build_that_has_nothing_to_publish_is_not_an_error(project):
    manifest, run_results = artifacts(model("plain"), args={"empty": True})

    assert plan_publish(project, manifest, run_results) == []


# --------------------------------------------------------------------------- order


def test_models_are_published_after_the_models_they_depend_on(project):
    # The manifest lists the models in the opposite order of their dependencies.
    items = plan(project,
                 model("gold", meta=PUBLISH, depends_on=["silver"]),
                 model("silver", meta=PUBLISH, depends_on=["stg"]),
                 model("stg"),
                 model("independent", meta=PUBLISH))

    names = [item.model for item in items]
    assert set(names) == {"gold", "silver", "independent"}
    assert names.index("silver") < names.index("gold")


def test_models_that_do_not_depend_on_each_other_are_published_by_name_in_any_manifest_order(project):
    # dbt lists models in the order it found their files, which differs between file systems: the order that the
    # restaurant example's tests expect held on macOS and not on the Linux runner. The order must not follow it.
    names = ["gold_totals", "churn", "revenue", "alpha"]

    for permutation in itertools.permutations(names):
        items = plan(project, *(model(name, meta=PUBLISH) for name in permutation))

        assert [item.model for item in items] == sorted(names), permutation


def test_dependencies_come_first_in_any_manifest_order(project):
    depends = {"a_last": ["b_middle"], "b_middle": ["c_first"], "c_first": [], "d_other": []}

    for permutation in itertools.permutations(depends):
        items = plan(project, *(model(name, meta=PUBLISH, depends_on=depends[name]) for name in permutation))

        assert [item.model for item in items] == ["c_first", "b_middle", "a_last", "d_other"], permutation


def test_the_order_goes_through_models_that_are_not_published(project):
    items = plan(project, model("gold", meta=PUBLISH, depends_on=["middle"]),
                 model("middle", depends_on=["silver"]), model("silver", meta=PUBLISH))

    assert [item.model for item in items] == ["silver", "gold"]


# --------------------------------------------------------------------------- dbt's artifacts


def test_failed_nodes_lists_what_dbt_reported_as_failed():
    run_results = {"results": [
        {"unique_id": "model.shop.a", "status": "success", "message": "OK"},
        {"unique_id": "model.shop.b", "status": "error", "message": "Database Error in model b\n  Binder Error"},
        {"unique_id": "test.shop.unique_b_id.1f", "status": "fail", "message": "Got 2 results, configured to fail"},
        {"unique_id": "test.shop.warn.2a", "status": "warn", "message": "Got 1 result, configured to warn"},
        {"unique_id": "model.shop.c", "status": "skipped", "message": None},
        {"unique_id": "model.shop.d", "status": "runtime error", "message": ""},
    ]}

    assert failed_nodes(run_results) == [
        {"node": "model.shop.b", "status": "error", "message": "Database Error in model b"},
        {"node": "test.shop.unique_b_id.1f", "status": "fail", "message": "Got 2 results, configured to fail"},
        {"node": "model.shop.d", "status": "runtime error", "message": ""},
    ]


def test_read_artifacts_reads_both_files_from_the_target_folder(project):
    target = project.directory / "target"
    target.mkdir()
    (target / "manifest.json").write_text(json.dumps({"nodes": {"a": 1}}))
    (target / "run_results.json").write_text(json.dumps({"results": []}))

    assert read_artifacts(project) == ({"nodes": {"a": 1}}, {"results": []})
    assert read_artifacts(project, target) == read_artifacts(project)  # an absolute target path works too


def test_read_artifacts_says_what_is_missing(project):
    with pytest.raises(ConfigError, match=r"dbt did not write .*manifest\.json, so there is nothing to publish"):
        read_artifacts(project)
    (project.directory / "target").mkdir()
    (project.directory / "target" / "manifest.json").write_text("{}")
    with pytest.raises(ConfigError, match=r"dbt did not write .*run_results\.json"):
        read_artifacts(project)


def test_read_artifacts_refuses_files_that_an_earlier_run_wrote(project):
    target = project.directory / "target"
    target.mkdir()
    for name in ("manifest.json", "run_results.json"):
        (target / name).write_text("{}")
    long_ago = time.time() - 3600
    os.utime(target / "run_results.json", (long_ago, long_ago))

    read_artifacts(project)  # without a start time, anything goes
    with pytest.raises(ConfigError, match=r"run_results\.json was not written by this dbt run.*--no-write-json"):
        read_artifacts(project, since=time.time() - 60)
    read_artifacts(project, since=long_ago - 1)


# --------------------------------------------------------------------------- lineage


def lake_block(name, namespace="raw", warehouse="../../lake/warehouse"):
    return {"name": name, "format": "ICEBERG", "catalog": {"identifier": namespace, "warehouse_path": warehouse}}


def source_tables_of(project, *names):
    """The Iceberg blocks of the sources, as ``dbt_layer.sources.source_tables`` finds them in ``lake/config``."""
    return {f"raw.{name}": (lake_block(name, warehouse="../warehouse"), project.directory.parent / "lake" / "config")
            for name in names}


def lineage_of(project, *models, sources=("raw.orders", "raw.customers"), tables=("orders", "customers"), **keywords):
    manifest, results = artifacts(*models, sources=sources)
    items = plan_publish(project, manifest, results, source_tables=source_tables_of(project, *tables), **keywords)
    return {item.model: item.config["metadata"].get("lineage") for item in items}


def test_a_published_model_lists_its_sources_and_the_published_models_it_reads_through_unpublished_ones(project):
    models = (model("stg_orders", reads=["raw.orders"]),
              model("silver", alias="fct_orders", meta={**PUBLISH, "ldp_namespace": "silver"},
                    depends_on=["stg_orders"]),
              model("report", meta={**PUBLISH, "ldp_namespace": "lake"}, depends_on=["silver"],
                    reads=["raw.customers"]))

    lineage = lineage_of(project, *models)

    assert lineage["silver"] == [{"name": "orders", "format": "ICEBERG", "catalog": {
        "identifier": "raw", "warehouse_path": "../warehouse"}}]
    assert [block["name"] for block in lineage["report"]] == ["customers", "fct_orders"]
    assert lineage["report"][1]["catalog"] == {"identifier": "silver", "warehouse_path": "../warehouse"}


def test_the_paths_of_a_source_block_are_made_relative_to_the_folder_of_the_generated_config(project):
    [block] = lineage_of(project, model("m", meta=PUBLISH, reads=["raw.orders"]))["m"]

    assert block["catalog"]["warehouse_path"] == "../warehouse", "from lake/config, which is next to lake/warehouse"
    warehouse = (project.config_dir / block["catalog"]["warehouse_path"]).resolve()
    assert warehouse == (project.directory.parent / "lake" / "warehouse").resolve()


def test_a_source_in_another_folder_than_the_generated_configs_gets_a_path_from_the_generated_config(project):
    elsewhere = project.directory.parent / "elsewhere" / "config"
    manifest, results = artifacts(model("m", meta=PUBLISH, reads=["raw.orders"]), sources=["raw.orders"])
    tables = {"raw.orders": (lake_block("orders", warehouse="../warehouse"), elsewhere)}

    [item] = plan_publish(project, manifest, results, source_tables=tables)

    [block] = item.config["metadata"]["lineage"]
    assert block["catalog"]["warehouse_path"] == "../../elsewhere/warehouse", "from lake/config to elsewhere/warehouse"
    warehouse = (project.config_dir / block["catalog"]["warehouse_path"]).resolve()
    assert warehouse == (project.directory.parent / "elsewhere" / "warehouse").resolve()


def test_without_the_source_tables_only_the_published_models_are_in_the_lineage(project):
    manifest, results = artifacts(model("up", meta=PUBLISH, reads=["raw.orders"]),
                                  model("down", meta=PUBLISH, depends_on=["up"]), sources=["raw.orders"])

    items = {item.model: item for item in plan_publish(project, manifest, results)}

    assert "lineage" not in items["up"].config["metadata"]
    assert [block["name"] for block in items["down"].config["metadata"]["lineage"]] == ["up"]


def test_a_source_that_the_project_does_not_know_is_left_out_and_the_model_is_still_published(project):
    lineage = lineage_of(project, model("m", meta=PUBLISH, reads=["raw.orders", "raw.unknown"]),
                         sources=("raw.orders", "raw.unknown"))

    assert [block["name"] for block in lineage["m"]] == ["orders"]


def test_a_table_that_is_read_by_two_paths_is_listed_once_in_order_of_its_name(project):
    models = (model("a", reads=["raw.orders", "raw.customers"]), model("b", reads=["raw.orders"]),
              model("m", meta=PUBLISH, depends_on=["b", "a"]))

    assert [block["name"] for block in lineage_of(project, *models)["m"]] == ["customers", "orders"]


def test_a_published_model_that_was_not_built_in_this_run_is_still_a_table_that_the_next_one_reads(project):
    models = (model("earlier", meta={**PUBLISH, "ldp_namespace": "silver"}),
              model("later", meta=PUBLISH, depends_on=["earlier"]))
    manifest, results = artifacts(*models)
    results["results"] = [row for row in results["results"] if row["unique_id"].endswith("later")]

    [item] = plan_publish(project, manifest, results)

    assert [block["name"] for block in item.config["metadata"]["lineage"]] == ["earlier"]


def test_a_manifest_that_cannot_be_followed_gives_no_lineage_and_the_model_is_published(project):
    manifest, results = artifacts(model("m", meta=PUBLISH, reads=["raw.orders"]), sources=["raw.orders"])
    manifest["sources"]["source.shop.raw.orders"] = "not a source"

    [item] = plan_publish(project, manifest, results, source_tables=source_tables_of(project, "orders"))

    assert "lineage" not in item.config["metadata"]


def test_models_that_read_each_other_do_not_send_the_walk_round_in_circles(project):
    models = (model("a", meta=PUBLISH, depends_on=["b"]), model("b", depends_on=["c"]),
              model("c", depends_on=["b"], reads=["raw.orders"]))

    assert [block["name"] for block in lineage_of(project, *models)["a"]] == ["orders"]


def test_the_generated_config_with_a_lineage_is_valid_and_the_hash_leaves_the_lineage_out(project):
    manifest, results = artifacts(model("m", meta=PUBLISH, reads=["raw.orders"]), sources=["raw.orders"])

    [item] = plan_publish(project, manifest, results, source_tables=source_tables_of(project, "orders"))

    assert validate_spec(item.config) == []
    assert spec_hash(item.config) == spec_hash({**item.config, "metadata": {
        key: value for key, value in item.config["metadata"].items() if key != "lineage"}})


def test_source_tables_finds_the_iceberg_tables_that_configs_name_and_the_folder_of_each(tmp_path):
    from local_data_platform.dbt_layer.sources import source_tables

    folder = tmp_path / "config"
    folder.mkdir()
    for name in ("orders", "customers"):
        (folder / f"{name}.json").write_text(json.dumps({"identifier": name, "metadata": {
            "source": {"name": name, "format": "CSV", "path": f"{name}.csv"},
            "target": {"name": name, "format": "ICEBERG", "catalog": {"identifier": "raw",
                                                                      "warehouse_path": "../warehouse"}}}}))
    (folder / "twice.json").write_text(json.dumps({"identifier": "twice", "metadata": {
        "source": {"name": "x", "format": "CSV", "path": "x.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "catalog": {"identifier": "raw",
                                                                      "warehouse_path": "../other"}}}}))
    (folder / "export.json").write_text(json.dumps({"identifier": "export", "metadata": {
        "source": {"name": "totals", "format": "ICEBERG", "catalog": {"identifier": "raw",
                                                                      "warehouse_path": "../warehouse"}},
        "target": {"name": "totals", "format": "CSV", "path": "totals.csv"}}}))
    (folder / "broken.json").write_text(json.dumps({"identifier": "broken", "metadata": {"source": {}}}))
    (folder / "notes.json").write_text("[1, 2]")

    found = source_tables([folder])

    assert sorted(found) == ["raw.customers", "raw.orders", "raw.totals"], "a table that a config reads counts too"
    block, where = found["raw.orders"]
    assert where == folder and block == {"name": "orders", "format": "ICEBERG", "catalog": {
        "identifier": "raw", "warehouse_path": "../warehouse"}}, "the first config that names a table wins"


# --------------------------------------------------------------------------- the table contract


def test_the_generated_config_carries_the_docs_of_the_model(project):
    [item] = plan(project, model("orders", materialized="incremental", unique_key="order_id", tags=["core"],
                                 meta={**PUBLISH, "ldp_namespace": "silver", "owner": "ops"},
                                 description="Every order.",
                                 columns={"order_id": "The key.", "amount": "What it cost."}))

    assert item.config["metadata"]["docs"] == {
        "layer": "silver", "description": "Every order.", "owner": "ops", "primary_key": ["order_id"],
        "columns": {"order_id": "The key.", "amount": "What it cost."}, "tags": ["core"]}
    assert validate_spec(item.config) == []


def test_a_model_with_nothing_to_document_gets_no_block(project):
    [item] = plan(project, model("totals", meta=PUBLISH))

    assert "docs" not in item.config["metadata"]


def test_ldp_layer_sets_the_layer_and_is_a_meta_key_that_ldp_knows(project):
    [item] = plan(project, model("totals", meta={**PUBLISH, "ldp_layer": "silver", "ldp_namespace": "gold"}))

    assert item.config["metadata"]["docs"] == {"layer": "silver"}


def test_a_model_for_the_gold_namespace_needs_what_a_gold_table_needs_and_every_gap_is_named(project):
    with pytest.raises(ConfigError) as error:
        plan(project, model("totals", meta={**PUBLISH, "ldp_namespace": "gold"}),
             model("daily", meta={**PUBLISH, "ldp_namespace": "gold", "owner": "ops"}, description="Daily."))

    assert str(error.value).startswith("cannot publish, 4 problems in the dbt models' meta, and nothing was published:")
    assert problems(error) == [
        "model daily: metadata.docs.grain: is required for a gold table, to say what one row is",
        "model totals: metadata.docs.description: is required for a gold table, to say what the table means",
        "model totals: metadata.docs.grain: is required for a gold table, to say what one row is",
        "model totals: metadata.docs.owner: is required for a gold table, to say who owns it. "
        "Set it here, or set 'who' in the config",
    ]


def test_a_gold_model_that_has_all_of_it_is_planned(project):
    [item] = plan(project, model("totals", meta=GOLD_META, **TOTALS_DOCS))

    assert item.table == "gold.totals"
    assert item.config["metadata"]["docs"] == {
        "layer": "gold", "description": "Orders and revenue.", "owner": "shop analytics", "grain": "one row per day",
        "columns": {"orders": "How many orders.", "revenue": "What they cost."}}


def test_a_published_model_carries_its_contract_in_its_table(project, build_file, tmp_path):
    outcome = publish(plan(project, *two_models()))

    assert outcome.failed is None
    totals = table(tmp_path, "gold", "totals").table()
    assert totals.properties["comment"] == "Orders and revenue." and totals.properties["ldp.layer"] == "gold"
    assert totals.properties["ldp.owner"] == "shop analytics" and totals.properties["ldp.grain"] == "one row per day"
    assert [(item.name, item.doc) for item in totals.schema().fields] == [("orders", "How many orders."),
                                                                          ("revenue", "What they cost.")]
    orders = table(tmp_path, "silver", "orders").table()
    assert orders.properties["ldp.layer"] == "silver" and orders.properties["ldp.primary-key"] == "order_id"
    assert len(totals.snapshots()) == 1, "the contract adds no snapshot"


def test_a_gold_model_with_a_column_that_has_no_description_is_stopped_at_the_write(project, build_file, tmp_path):
    half = {"description": "Orders and revenue.", "columns": {"orders": "How many orders."}}
    items = plan(project, model("orders", materialized="incremental", unique_key="order_id",
                                meta={**PUBLISH, "ldp_namespace": "silver"}),
                 model("totals", meta=GOLD_META, depends_on=["orders"], **half))

    outcome = publish(items)

    assert [item.model for item in outcome.published] == ["orders"]
    failed_model, error = outcome.failed
    assert failed_model == "totals" and "1 column written without a description" in str(error)
    assert "revenue" in str(error)
    assert not table(tmp_path, "gold", "totals").exists()


# --------------------------------------------------------------------------- writing to Iceberg


@pytest.fixture
def build_file(project) -> Path:
    """The DuckDB file dbt would have built: ``main.orders`` (3 rows) and ``main.totals`` (1 row)."""
    project.database.parent.mkdir(parents=True)
    with duckdb.connect(str(project.database)) as connection:
        connection.execute("CREATE TABLE main.orders AS SELECT i AS order_id, i * 1.5 AS amount "
                           "FROM range(1, 4) t(i)")
        connection.execute("CREATE TABLE main.totals AS SELECT 3 AS orders, 7.5 AS revenue")
    return project.database


def table(tmp_path, namespace, name) -> Iceberg:
    return Iceberg(name, {"identifier": namespace, "warehouse_path": str(tmp_path / "lake" / "warehouse")})


def two_models():
    return (model("orders", materialized="incremental", unique_key="order_id",
                  meta={**PUBLISH, "ldp_namespace": "silver"}),
            model("totals", meta=GOLD_META, depends_on=["orders"], **TOTALS_DOCS))


def test_publish_writes_every_config_and_runs_them_in_order(project, build_file, tmp_path):
    items = plan(project, *two_models())
    seen = []

    outcome = publish(items, report=lambda published: seen.append(published.table))

    assert outcome.failed is None and outcome.pending is None
    assert [item.table for item in outcome.published] == ["silver.orders", "gold.totals"] == seen
    orders, totals = outcome.published
    assert (orders.mode, orders.rows_before, orders.rows_after) == ("upsert", 0, 3)
    assert (totals.mode, totals.rows_before, totals.rows_after) == ("overwrite", 0, 1)
    assert isinstance(orders.snapshot_id, int)
    assert sorted(table(tmp_path, "silver", "orders").get().column("order_id").to_pylist()) == [1, 2, 3]
    assert table(tmp_path, "gold", "totals").get().to_pylist() == [{"orders": 3, "revenue": 7.5}]
    assert orders.config == tmp_path / "lake" / "config" / "orders.json" and orders.config.is_file()
    assert json.loads(orders.config.read_text()) == items[0].config


def test_an_upsert_that_changes_nothing_adds_no_snapshot_but_an_overwrite_always_does(project, build_file, tmp_path):
    publish(plan(project, *two_models()))
    tables = (("silver", "orders"), ("gold", "totals"))
    before = {name: len(table(tmp_path, ns, name).snapshots()) for ns, name in tables}

    again = publish(plan(project, *two_models()))

    assert again.failed is None
    assert len(table(tmp_path, "silver", "orders").snapshots()) == before["orders"]
    assert len(table(tmp_path, "gold", "totals").snapshots()) > before["totals"]
    assert [(item.rows_before, item.rows_after) for item in again.published] == [(3, 3), (1, 1)]


def test_the_generated_config_runs_again_with_ldp_run(project, build_file, tmp_path, capsys):
    from local_data_platform.cli import main

    [first] = publish(plan(project, model("totals", meta=PUBLISH))).published
    with duckdb.connect(str(build_file)) as connection:
        connection.execute("UPDATE main.totals SET orders = 4")

    assert main(["run", str(first.config)]) == 0

    assert table(tmp_path, "lake", "totals").get().to_pylist() == [{"orders": 4, "revenue": 7.5}]
    assert "lake.totals: overwrite, 1 -> 1 rows" in capsys.readouterr().out


def test_a_failing_write_stops_the_run_and_says_what_was_and_was_not_published(project, build_file, tmp_path):
    blocked = {"checks": [{"check": "row_count", "min": 100}], "on_failure": "fail"}
    items = plan(project,
                 model("orders", materialized="incremental", unique_key="order_id",
                       meta={**PUBLISH, "ldp_namespace": "silver"}),
                 model("totals", meta={**GOLD_META, "ldp_quality": blocked}, depends_on=["orders"], **TOTALS_DOCS),
                 model("after", alias="orders", schema="main", meta={**PUBLISH, "ldp_namespace": "last"},
                       depends_on=["totals"]))

    outcome = publish(items)

    assert [item.model for item in outcome.published] == ["orders"]
    failed_model, error = outcome.failed
    assert failed_model == "totals" and error is not None
    assert outcome.pending == ["after"]
    assert outcome.failed_config == tmp_path / "lake" / "config" / "totals.json"
    assert table(tmp_path, "silver", "orders").exists() and not table(tmp_path, "gold", "totals").exists()
    # Every config was written before the first write, so the later ones can be fixed and run by hand.
    assert sorted(path.name for path in (tmp_path / "lake" / "config").iterdir()) == [
        "after.json", "orders.json", "totals.json"]


def test_a_model_that_gains_a_column_is_overwritten_once_and_then_upserts_again(project, build_file, tmp_path):
    from local_data_platform.cli import main

    publish(plan(project, *two_models()))
    with duckdb.connect(str(build_file)) as connection:
        connection.execute("CREATE OR REPLACE TABLE main.orders AS SELECT i AS order_id, i * 1.5 AS amount, "
                           "'x' AS note FROM range(1, 5) t(i)")

    failed = publish(plan(project, *two_models()))  # an upsert cannot add a column

    assert [item.model for item in failed.published] == [] and failed.failed[0] == "orders"
    assert failed.pending == ["totals"] and failed.failed_config == tmp_path / "lake" / "config" / "orders.json"
    assert main(["run", str(failed.failed_config), "--mode", "overwrite"]) == 0  # the way out the design gives
    assert table(tmp_path, "silver", "orders").get().column_names == ["order_id", "amount", "note"]
    again = publish(plan(project, *two_models()))
    assert again.failed is None and [(item.mode, item.rows_after) for item in again.published] == [
        ("upsert", 4), ("overwrite", 1)]


def test_a_model_that_loses_a_column_cannot_be_upserted_either(project, build_file):
    publish(plan(project, *two_models()))
    with duckdb.connect(str(build_file)) as connection:
        connection.execute("CREATE OR REPLACE TABLE main.orders AS SELECT i AS order_id FROM range(1, 5) t(i)")

    outcome = publish(plan(project, *two_models()))

    assert outcome.failed[0] == "orders" and "missing columns ['amount']" in str(outcome.failed[1])


def test_quality_warnings_are_kept_with_the_model(project, build_file):
    warn = {"checks": [{"check": "row_count", "min": 100}], "on_failure": "warn"}

    [published] = publish(plan(project, model("orders", meta={**PUBLISH, "ldp_quality": warn}))).published

    assert published.quality and any("row_count" in line for line in published.quality)
