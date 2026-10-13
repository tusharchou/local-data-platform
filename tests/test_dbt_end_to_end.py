"""``ldp dbt`` with the real dbt, on the three-model fixture project (``tests/fixtures/dbt_project``).

Every test is marked ``dbt``, so it skips without the extra and the ``dbt`` CI job runs it. Offline is
checked for each one: dbt runs as a subprocess of ``ldp`` with an HTTP proxy that is a local socket, and the
test fails if anything connects to it.

The project reads ``raw.orders`` through a staging view, builds ``fct_orders`` incrementally on ``order_id``
(published as ``silver.fct_orders``, an upsert) and ``gold_totals`` as a table (``gold.gold_totals``, an
overwrite), and has a ``unique`` test on ``fct_orders.order_id``.
"""

import re
from pathlib import Path

import pytest

from local_data_platform.cli import main
from local_data_platform.format.iceberg import Iceberg
from tests.support import (
    add_table,
    build_lake,
    copy_fixture_project,
    requires_iceberg_extension,
    run_results,
    without_log_warnings,
)

pytestmark = pytest.mark.dbt


@pytest.fixture(autouse=True)
def offline(network_trap, monkeypatch):
    """Point every child process at a proxy that records connections, and fail the test if there is one."""
    env, connections = network_trap
    for name, value in env.items():
        if "proxy" in name.lower():
            monkeypatch.setenv(name, value)
    yield
    assert connections == [], f"something opened a network connection: {connections}"


@pytest.fixture
def lake(tmp_path) -> Path:
    return build_lake(tmp_path / "lake")


@pytest.fixture
def project(tmp_path, lake) -> Path:
    return copy_fixture_project(tmp_path / "proj")


def ldp(capfd, *argv) -> tuple[int, str, str]:
    status = main([str(item) for item in argv])
    out, err = capfd.readouterr()
    return status, out, without_log_warnings(err)


def iceberg(tmp_path, namespace, name) -> Iceberg:
    return Iceberg(name, {"identifier": namespace, "warehouse_path": str(tmp_path / "lake" / "warehouse")})


def published(out: str) -> dict[str, str]:
    """``{table: "<mode>, <before> -> <after> rows"}`` from the lines ``ldp dbt build`` printed."""
    lines = re.findall(r"^ldp: published (\S+): (.+), snapshot \d+$", out, re.MULTILINE)
    return {table: what for table, what in lines}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} is not in {path}"
    path.write_text(text.replace(old, new))


def test_build_runs_dbt_and_publishes_the_marked_models(capfd, project, tmp_path):
    status, out, err = ldp(capfd, "dbt", "build", project)

    assert status == 0, out + err
    assert run_results(project) == {
        "stg_orders": "success", "fct_orders": "success", "gold_totals": "success",
        "not_null_stg_orders_order_id": "pass", "unique_fct_orders_order_id": "pass",
        "not_null_fct_orders_order_id": "pass"}
    assert published(out) == {"silver.fct_orders": "upsert, 0 -> 3 rows", "gold.gold_totals": "overwrite, 0 -> 1 rows"}
    assert sorted(iceberg(tmp_path, "silver", "fct_orders").get().column("order_id").to_pylist()) == [1, 2, 3]
    assert iceberg(tmp_path, "gold", "gold_totals").get().to_pylist() == [{"orders": 3, "revenue": 9.0}]
    assert "stg_orders" not in "".join(published(out)), "the staging view is not marked, so it is not published"
    assert "\x1b[" not in out, "dbt was not told to leave colour codes out of a pipe"
    assert out.index("ldp: registered 3 sources") < out.index("Running with dbt") < out.index("ldp: published")


def test_the_published_tables_are_described_by_ordinary_configs(capfd, project, tmp_path, lake):
    ldp(capfd, "dbt", "build", project)

    status, out, _ = ldp(capfd, "snapshots", lake / "fct_orders.json")

    assert status == 0 and "Snapshots of Iceberg table silver.fct_orders" in out
    status, out, _ = ldp(capfd, "run", lake / "gold_totals.json")  # publishes the model again, from the build file
    assert status == 0 and "gold.gold_totals: overwrite, 1 -> 1 rows" in out
    status, out, _ = ldp(capfd, "query", lake / "gold_totals.json", "select orders, revenue from gold_totals")
    assert status == 0 and re.search(r"orders\s+revenue\s*\n-+\s+-+\s*\n\s*3\s+9", out), out
    # The lake folder now holds the ingest configs and the generated ones, so a raw table and a model built from
    # it can be joined in one query.
    sql = "SELECT count(*) AS n FROM raw.orders r JOIN silver.fct_orders s ON s.order_id = r.id"
    status, out, _ = ldp(capfd, "query", lake, sql)
    assert status == 0 and re.search(r"n\s*\n-+\s*\n\s*3\n", out), out
    status, out, _ = ldp(capfd, "maintain", lake / "fct_orders.json")  # a dry run
    assert status == 0, out
    status, out, _ = ldp(capfd, "runs", lake / "fct_orders.json")  # the run events of the publish, from ldp_dbt.json
    assert status == 0 and re.search(r"\bfct_orders\s.*\bpublished\b", out), out


def test_a_second_build_with_no_new_data_adds_no_snapshot_to_an_upserted_table(capfd, project, tmp_path):
    ldp(capfd, "dbt", "build", project)
    orders, totals = iceberg(tmp_path, "silver", "fct_orders"), iceberg(tmp_path, "gold", "gold_totals")
    before = (len(orders.snapshots()), len(totals.snapshots()))

    status, out, err = ldp(capfd, "dbt", "build", project)

    assert status == 0, out + err
    assert published(out) == {"silver.fct_orders": "upsert, 3 -> 3 rows", "gold.gold_totals": "overwrite, 1 -> 1 rows"}
    assert len(orders.snapshots()) == before[0]  # nothing changed, so nothing was written
    assert len(totals.snapshots()) > before[1]  # an overwrite is always a new commit


def test_an_incremental_second_build_upserts_only_the_new_rows(capfd, project, tmp_path):
    ldp(capfd, "dbt", "build", project)
    add_table(tmp_path / "lake", "raw", "orders", 2, start=4)  # a later ingest adds orders 4 and 5

    status, out, err = ldp(capfd, "dbt", "build", project)

    assert status == 0, out + err
    assert published(out) == {"silver.fct_orders": "upsert, 3 -> 5 rows", "gold.gold_totals": "overwrite, 1 -> 1 rows"}
    orders = iceberg(tmp_path, "silver", "fct_orders")
    assert sorted(orders.get().column("order_id").to_pylist()) == [1, 2, 3, 4, 5]
    assert orders.snapshots()[-1]["summary"]["added-records"] == "2"  # dbt handed over two new rows, not five
    assert iceberg(tmp_path, "gold", "gold_totals").get().to_pylist() == [{"orders": 5, "revenue": 22.5}]


def test_a_failing_data_test_publishes_nothing(capfd, project, tmp_path, lake):
    add_table(tmp_path / "lake", "raw", "orders", 1, start=1)  # order 1 twice, so unique_fct_orders_order_id fails

    status, out, err = ldp(capfd, "dbt", "build", project)

    assert status == 1 and "ldp: published" not in out
    assert err.startswith("ldp: error: dbt build failed with exit status 1, so nothing was published\n")
    assert "  - test.shop.unique_fct_orders_order_id." in err and ": fail, Got 1 result" in err
    assert run_results(project)["fct_orders"] == "success"  # the model itself ran, and still was not published
    assert not iceberg(tmp_path, "silver", "fct_orders").exists()
    assert not iceberg(tmp_path, "gold", "gold_totals").exists()
    assert not (lake / "fct_orders.json").exists() and not (lake / "gold_totals.json").exists()


def test_a_model_that_fails_to_run_publishes_nothing(capfd, project, tmp_path):
    edit(project / "models" / "gold" / "gold_totals.sql", "sum(amount)", "sum(no_such_column)")

    status, out, err = ldp(capfd, "dbt", "build", project)

    assert status == 1 and "ldp: published" not in out
    assert "  - model.shop.gold_totals: error, Runtime Error in model gold_totals" in err or "gold_totals: error" in err
    assert not iceberg(tmp_path, "silver", "fct_orders").exists()  # fct_orders built fine, and is still not published


def test_select_publishes_only_the_models_that_ran(capfd, project, tmp_path):
    ldp(capfd, "dbt", "build", project)
    add_table(tmp_path / "lake", "raw", "orders", 2, start=4)
    silver = iceberg(tmp_path, "silver", "fct_orders")
    before = len(silver.snapshots())

    status, out, err = ldp(capfd, "dbt", "build", project, "--select", "gold_totals")

    assert status == 0, out + err
    assert published(out) == {"gold.gold_totals": "overwrite, 1 -> 1 rows"}
    assert len(silver.snapshots()) == before and silver.row_count() == 3  # fct_orders was left out, as was its test
    assert iceberg(tmp_path, "gold", "gold_totals").get().to_pylist() == [{"orders": 3, "revenue": 9.0}]

    status, out, _ = ldp(capfd, "dbt", "build", project, "--", "--select", "fct_orders")  # with the separator
    assert status == 0 and list(published(out)) == ["silver.fct_orders"]
    assert silver.row_count() == 5


def test_a_project_with_no_published_models_builds_and_publishes_nothing(capfd, project, tmp_path, lake):
    for model in (project / "models" / "silver" / "fct_orders.sql", project / "models" / "gold" / "gold_totals.sql"):
        model.write_text(re.sub(r",\s*meta=\{[^}]*\}", "", model.read_text()))  # no more ldp_publish

    status, out, err = ldp(capfd, "dbt", "build", project)

    assert status == 0, out + err
    assert "ldp: no model marked ldp_publish: true ran in this build, so nothing was published" in out
    assert run_results(project)["gold_totals"] == "success"
    assert not iceberg(tmp_path, "gold", "gold_totals").exists() and not (lake / "gold_totals.json").exists()


@pytest.mark.parametrize("mode", ["copy", pytest.param("view", marks=requires_iceberg_extension)])
def test_a_view_over_the_sources_can_be_published_too(capfd, project, tmp_path, mode):
    edit(project / "models" / "staging" / "stg_orders.sql", "{{ config(materialized='view') }}",
         "{{ config(materialized='view', meta={'ldp_publish': true, 'ldp_namespace': 'staging'}) }}")

    status, out, err = ldp(capfd, "dbt", "build", "--sources-mode", mode, project)

    assert status == 0, out + err
    assert published(out)["staging.stg_orders"] == "overwrite, 0 -> 3 rows"
    rows = iceberg(tmp_path, "staging", "stg_orders").get().to_pylist()
    assert sorted(row["order_id"] for row in rows) == [1, 2, 3] and set(rows[0]) == {"order_id", "label", "amount"}


def test_init_then_build_works_on_a_new_project(capfd, tmp_path, lake):
    status, out, err = ldp(capfd, "dbt", "init", tmp_path / "shop", "--sources", lake, "--warehouse", tmp_path / "gold")
    assert status == 0, out + err
    staging = (tmp_path / "shop" / "models" / "staging" / "stg_example.sql").read_text()
    rows = {"orders": 3, "customers": 2, "cities": 2}[re.search(r"source\('\w+', '(\w+)'\)", staging).group(1)]

    status, out, err = ldp(capfd, "dbt", "build", tmp_path / "shop")

    assert status == 0, out + err
    assert published(out) == {"gold.gold_example": "overwrite, 0 -> 1 rows"}
    gold = Iceberg("gold_example", {"identifier": "gold", "warehouse_path": str(tmp_path / "gold")})
    assert gold.get().to_pylist() == [{"row_count": rows}]
    assert (tmp_path / "shop" / "target" / "ldp" / "gold_example.json").is_file()  # the default config_dir


def test_test_runs_dbt_test_on_the_registered_sources(capfd, project, tmp_path):
    ldp(capfd, "dbt", "build", project)

    status, out, err = ldp(capfd, "dbt", "test", project)
    assert status == 0, out + err
    assert "PASS=3" in out and "ldp: published" not in out

    # A duplicate order arrives in the lake. dbt test does not build, but it registers the sources again,
    # so the staging view sees the new snapshot and a test on it can fail.
    add_table(tmp_path / "lake", "raw", "orders", 1, start=1)
    status, out, err = ldp(capfd, "dbt", "test", project, "--select", "stg_orders")  # not_null: still passes
    assert status == 0, out + err
    edit(project / "models" / "schema.yml", "data_tests: [not_null]", "data_tests: [not_null, unique]")
    status, out, err = ldp(capfd, "dbt", "test", project, "--select", "stg_orders")
    assert status == 1 and err.splitlines() == ["ldp: error: dbt test exited with status 1"]
    assert "unique_stg_orders_order_id" in out


def test_docs_generates_the_catalog_and_the_site(capfd, project):
    status, out, err = ldp(capfd, "dbt", "docs", project)

    assert status == 0, out + err
    for name in ("catalog.json", "manifest.json", "index.html"):
        assert (project / "target" / name).is_file(), name


def test_exec_runs_any_other_dbt_command(capfd, project, tmp_path):
    status, out, err = ldp(capfd, "dbt", "exec", project, "ls", "--resource-type", "model")

    assert status == 0, out + err
    assert {"shop.staging.stg_orders", "shop.silver.fct_orders", "shop.gold.gold_totals"} <= set(out.split())
    assert not iceberg(tmp_path, "gold", "gold_totals").exists()  # exec never publishes
    status, out, err = ldp(capfd, "dbt", "exec", project, "build")
    assert status == 0, out + err
    assert "ldp: published" not in out and not iceberg(tmp_path, "silver", "fct_orders").exists()


def test_a_command_dbt_does_not_know_fails_with_1(capfd, project):
    status, _, err = ldp(capfd, "dbt", "exec", project, "no-such-command")

    assert status == 1 and err.splitlines()[-1] == "ldp: error: dbt exited with status 2"
