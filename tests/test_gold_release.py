"""Gold releases: every gold table of a build, published together as one consistent version.

``ldp gold build`` cuts release ``rN`` when every table passed its gate. The release names the snapshot of each table,
and readers that read a release see tables that agree. Everything here is offline. A workflow with a ``gold`` task runs
the build for a window and cuts the release as part of the run.
"""

import datetime as dt
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from local_data_platform import Config
from local_data_platform.cli import main
from local_data_platform.datasets import list_versions
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.etl import run_config
from local_data_platform.events import (
    EVENT_TYPES, MemorySink, OpenLineageSink, RunEvent, read_iceberg_events, summarize_runs, uuid7)
from local_data_platform.exceptions import ConfigError
from local_data_platform.gold import release as releases
from local_data_platform.lake import _release_snapshots, open_lake
from local_data_platform.gold.build import BLOCKED, BUILT, CHECKED, build_gold, find_gold, with_events
from local_data_platform.gold.release import (
    Release,
    ReleaseError,
    choose_release,
    current_release,
    cut_release,
    get_release,
    list_releases,
    register_release,
    release_scope,
    release_snapshots,
    snapshot_for,
)
from local_data_platform.maintenance import expire_snapshots
from local_data_platform.orchestration import run_workflow
from local_data_platform.orchestration.gold_task import _table_run_id
from local_data_platform.orchestration.tasks import RunContext, get_task_kind, task_kinds
from local_data_platform.orchestration.workflow import plan_workflow, workflow_schema
from local_data_platform.pipeline.builders import iceberg_from_config
from tests.test_gold_build import ORDERS, chain, external, gold, reading, silver, table, write  # noqa: F401

pytest.importorskip("duckdb")


def release_file(root: Path, number: int, namespace: str = "gold") -> Path:
    return root / "warehouse" / ".ldp" / "releases" / namespace / f"r{number}.json"


def gold_count(root: Path, release: str | None = None, *, live: bool = False, name: str = "gold_revenue") -> int:
    """How many rows a reader sees in a gold table when it reads the release, as ``register_release`` does."""
    with DuckDBEngine() as engine:
        register_release(engine, root, release=release, live=live)
        return engine.query(f"SELECT count(*) AS n FROM gold.{name}").to_pylist()[0]["n"]


def more_orders(root: Path, extra: int = 1) -> None:
    """Load more orders into silver, so that the next build of gold gives different rows."""
    csv = ORDERS + "".join(f"{10 + number},extra{number},1.0\n" for number in range(extra))
    silver(root, csv=csv)


# --------------------------------------------------------------------------- cutting a release


def test_a_build_that_passes_every_gate_cuts_r1_and_names_the_snapshot_of_each_table(tmp_path):
    chain(tmp_path)

    result = build_gold(tmp_path, release=True)

    assert result.exit_code == 0 and result.release.cut and result.release.release.name == "r1"
    document = json.loads(release_file(tmp_path, 1).read_text())
    assert (document["ldp_release"], document["release"], document["number"], document["namespace"]) == (
        1, "r1", 1, "gold")
    assert [item["table"] for item in document["tables"]] == ["gold.a", "gold.b", "gold.c", "gold.d", "gold.z"]
    for item, built in zip(document["tables"], sorted(result.tables, key=lambda outcome: outcome.gold.table)):
        assert item["snapshot_id"] == built.snapshot_id == int(
            table(tmp_path, built.gold.path.stem).table().current_snapshot().snapshot_id)
        assert item["run_id"] == built.result.run_id and item["rows"] == 2 and item["status"] == BUILT
        assert (item["dataset"], item["dataset_version"]) == (item["table"], 1)


def test_a_build_prints_the_release_it_cut_after_its_summary(tmp_path, capsys):
    chain(tmp_path)

    assert main(["gold", "build", str(tmp_path)]) == 0

    out = capsys.readouterr().out.splitlines()
    assert len(out) == 7 and out[-2].startswith(f"gold build {tmp_path}: 5 built, ")
    assert out[-1] == "release r1 cut with 5 tables and 10 rows in gold"
    assert main(["gold", "build", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "release r2 cut with 5 tables and 10 rows in gold"


def test_each_table_is_pinned_as_a_dataset_with_the_release_property_and_a_tag(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)

    [version] = list_versions("gold.gold_revenue", warehouse=tmp_path / "warehouse")

    assert dict(version.properties) == {"release": "r1"} and version.tag is not None
    assert version.snapshot_id == snapshot_for("gold.gold_revenue", current_release(tmp_path))
    refs = table(tmp_path, "gold_revenue").table().metadata.refs
    assert version.tag in refs and refs[version.tag].snapshot_id == version.snapshot_id


def test_the_next_build_cuts_r2_and_r1_still_names_its_snapshots(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    first = release_file(tmp_path, 1).read_bytes()

    more_orders(tmp_path)
    result = build_gold(tmp_path, release=True)

    assert result.release.release.name == "r2"
    assert [item.name for item in list_releases(tmp_path)] == ["r2", "r1"]
    assert current_release(tmp_path).name == "r2" and release_file(tmp_path, 1).read_bytes() == first
    assert get_release(tmp_path, "r1").table("gold.gold_revenue").snapshot_id != \
        get_release(tmp_path, "r2").table("gold.gold_revenue").snapshot_id


def test_a_release_file_appears_whole_or_not_at_all_and_a_failed_cut_does_not_use_the_number(tmp_path, monkeypatch):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables
    folder = release_file(tmp_path, 1).parent

    def crash(descriptor):
        raise OSError("the disk went away")

    monkeypatch.setattr(releases.os, "fsync", crash)
    with pytest.raises(OSError, match="the disk went away"):
        cut_release(built)
    monkeypatch.undo()

    assert not folder.exists() or sorted(path.name for path in folder.iterdir()) == []
    assert current_release(tmp_path) is None
    cut = cut_release(built)
    assert cut.cut and cut.release.name == "r1" and sorted(path.name for path in folder.iterdir()) == ["r1.json"]
    assert cut.release.table("gold.gold_revenue").dataset_version == 2, "the pin of the failed try is a dataset " \
        "version of its own, and the release file is what readers go by"


def test_a_cut_does_not_replace_a_release_that_another_process_cut_at_the_same_time(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables
    cut_release(built)
    scope = releases.folder_scope(tmp_path)
    release = current_release(tmp_path)

    with pytest.raises(ReleaseError, match="cut by another process"):
        releases._write_release(scope, release)

    assert [item.name for item in list_releases(tmp_path)] == ["r1"]
    assert not [path for path in release_file(tmp_path, 1).parent.iterdir() if path.name.endswith(".tmp")]


def test_a_filesystem_without_hard_links_gets_the_release_by_a_rename(tmp_path, monkeypatch):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables

    def no_links(source, destination):
        raise OSError("this filesystem has no hard links")

    monkeypatch.setattr(releases.os, "link", no_links)
    cut = cut_release(built)

    assert cut.cut and sorted(path.name for path in release_file(tmp_path, 1).parent.iterdir()) == ["r1.json"]
    assert current_release(tmp_path).name == "r1"
    scope = releases.folder_scope(tmp_path)
    with pytest.raises(ReleaseError, match="cut by another process"):
        releases._write_release(scope, current_release(tmp_path))
    assert [item.name for item in list_releases(tmp_path)] == ["r1"]


def test_a_table_that_cannot_be_pinned_stops_the_release_with_the_reason(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables
    built[0].snapshot_id = 424242

    with pytest.raises(ReleaseError, match=r"cannot pin gold\.gold_revenue for release r1: .*424242"):
        cut_release(built)

    assert list_releases(tmp_path) == []


def test_an_event_that_cannot_even_be_built_does_not_fail_the_release(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables

    def broken(gold_config):
        raise RuntimeError("no sink for this table")

    outcome = cut_release(built, sink_for=broken)

    assert outcome.cut and current_release(tmp_path).name == "r1"


def test_a_build_that_changes_nothing_cuts_no_new_release(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables

    first = cut_release(built)
    again = cut_release(built)

    assert first.cut and not again.cut and again.release.name == "r1" and "snapshots" in again.note
    assert [item.name for item in list_releases(tmp_path)] == ["r1"]
    assert len(list_versions("gold.gold_revenue", warehouse=tmp_path / "warehouse")) == 1


def test_two_builds_that_cut_at_the_same_time_take_two_numbers(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    first = build_gold(tmp_path).tables
    more_orders(tmp_path)
    second = build_gold(tmp_path).tables

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(cut_release, [first, second]))

    assert all(outcome.cut for outcome in outcomes)
    assert sorted(outcome.release.name for outcome in outcomes) == ["r1", "r2"]
    assert sorted(path.name for path in release_file(tmp_path, 1).parent.iterdir()) == ["r1.json", "r2.json"]
    assert [item.table("gold.gold_revenue").dataset_version for item in reversed(list_releases(tmp_path))] == [1, 2]


def test_a_snapshot_that_cannot_be_tagged_stops_the_release_because_expiry_could_remove_it(tmp_path, monkeypatch):
    silver(tmp_path)
    gold(tmp_path)
    built = build_gold(tmp_path).tables

    def no_tags(reload, snapshot_id, tag):
        raise RuntimeError("the catalog refused the tag")

    monkeypatch.setattr("local_data_platform.datasets._create_tag", no_tags)
    with pytest.raises(ReleaseError, match=r"could not be tagged, so snapshot expiry could remove it"):
        cut_release(built)

    assert list_releases(tmp_path) == [] and not release_file(tmp_path, 1).exists()


def test_a_release_that_cannot_be_cut_after_the_build_is_an_error_and_the_tables_stay_built(
        tmp_path, monkeypatch, capsys):
    silver(tmp_path)
    gold(tmp_path)
    assert main(["gold", "build", str(tmp_path)]) == 0
    capsys.readouterr()

    def refuse(tables, **keywords):
        raise ReleaseError("the warehouse is read only")

    monkeypatch.setattr(releases, "cut_release", refuse)
    assert main(["gold", "build", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert "1 built" in captured.out and "no release cut. Readers still see r1." in captured.out
    assert "ldp: the release was not cut" in captured.err and "the warehouse is read only" in captured.err
    assert [item.name for item in list_releases(tmp_path)] == ["r1"]


def test_a_release_file_that_says_something_else_than_its_name_is_skipped(tmp_path, caplog):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    release_file(tmp_path, 2).write_text(release_file(tmp_path, 1).read_text())
    release_file(tmp_path, 3).write_text(json.dumps({"release": "r3"}))

    with caplog.at_level("WARNING"):
        listed = list_releases(tmp_path)

    assert [item.name for item in listed] == ["r1"]
    assert "it says r1 of gold" in caplog.text and "invalid release" in caplog.text


def test_a_release_file_that_cannot_be_read_still_holds_its_number(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    release_file(tmp_path, 2).write_text("{not json")
    more_orders(tmp_path)

    result = build_gold(tmp_path, release=True)

    assert result.release.cut and result.release.release.name == "r3"
    assert release_file(tmp_path, 2).read_text() == "{not json"
    assert [item.name for item in list_releases(tmp_path)] == ["r3", "r1"]


def test_there_is_nothing_to_release_without_a_gold_table():
    with pytest.raises(ReleaseError, match="there is no gold table to release"):
        release_scope([])


def test_a_table_that_is_built_elsewhere_is_in_the_release_at_the_snapshot_that_was_checked(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    external(tmp_path)

    result = build_gold(tmp_path, release=True)

    assert [item.status for item in result.tables] == [CHECKED, BUILT]
    checked = current_release(tmp_path).table("gold.elsewhere")
    assert checked.status == CHECKED and checked.run_id is None and checked.rows == 3
    assert checked.snapshot_id == int(table(tmp_path, "elsewhere").table().current_snapshot().snapshot_id)


def test_the_release_names_the_snapshot_the_build_ended_with_and_not_one_that_a_writer_added_after(tmp_path):
    silver(tmp_path)
    gold(tmp_path)

    def run(gold_config):
        done = run_config(with_events(gold_config.config))
        table(tmp_path, "gold_revenue").put(pa.table({"restaurant": ["west"], "orders": [1], "revenue": [2.0]}),
                                            mode="append")
        return done

    result = build_gold(tmp_path, run=run, release=True)

    released = current_release(tmp_path).table("gold.gold_revenue")
    now = int(table(tmp_path, "gold_revenue").table().current_snapshot().snapshot_id)
    assert released.snapshot_id == result.tables[0].snapshot_id != now
    assert released.rows == 2 and gold_count(tmp_path) == 2 and gold_count(tmp_path, live=True) == 3


# --------------------------------------------------------------------------- a blocked build


def blocking(root: Path, name: str = "b") -> None:
    document = json.loads((root / f"{name}.json").read_text())
    document["metadata"]["quality"] = {"checks": [{"check": "row_count", "min": 100}]}
    write(root / f"{name}.json", document)


def test_a_blocked_gate_cuts_no_release_and_the_output_names_the_release_readers_still_see(tmp_path, capsys):
    chain(tmp_path)
    assert main(["gold", "build", str(tmp_path)]) == 0
    capsys.readouterr()
    blocking(tmp_path)

    assert main(["gold", "build", str(tmp_path)]) == 3

    out = capsys.readouterr().out
    assert "no release cut, because b was blocked. Readers still see r1." in out
    assert [item.name for item in list_releases(tmp_path)] == ["r1"] and current_release(tmp_path).name == "r1"


def test_a_blocked_first_build_says_that_there_is_no_release_yet(tmp_path, capsys):
    chain(tmp_path)
    blocking(tmp_path)

    assert main(["gold", "build", str(tmp_path)]) == 3

    assert "no release cut, because b was blocked. There is no release yet." in capsys.readouterr().out
    assert not release_file(tmp_path, 1).exists() and current_release(tmp_path) is None


def test_a_table_that_fails_for_another_reason_cuts_no_release_either(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path, "a_first")
    gold(tmp_path, "b_broken", sql="SELECT restaurant FROM silver_orders WHERE nope = 1")

    assert main(["gold", "build", str(tmp_path)]) == 1

    assert "no release cut, because b_broken failed. There is no release yet." in capsys.readouterr().out
    assert list_releases(tmp_path) == []


def test_no_release_builds_the_tables_and_cuts_nothing(tmp_path, capsys):
    chain(tmp_path)

    assert main(["gold", "build", str(tmp_path), "--no-release"]) == 0

    out = capsys.readouterr().out
    assert "5 built" in out and "no release cut" not in out and " cut with " not in out
    assert list_releases(tmp_path) == [] and not release_file(tmp_path, 1).parent.exists()
    assert not list_versions("gold.a", warehouse=tmp_path / "warehouse")


def test_the_json_result_says_what_the_build_did_about_its_release(tmp_path, capsys):
    chain(tmp_path)

    assert main(["gold", "build", str(tmp_path), "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    first = printed["release"]
    assert main(["gold", "build", str(tmp_path), "--json", "--no-release"]) == 0
    second = json.loads(capsys.readouterr().out)["release"]

    assert (first["cut"], first["release"], first["namespace"], first["tables"]) == (True, "r1", "gold", 5)
    assert first["path"].endswith("r1.json") and second is None
    assert {item["run_status"] for item in printed["tables"]} == {"published"}


def test_gold_tables_in_two_namespaces_are_refused_before_anything_is_built(tmp_path, capsys):
    chain(tmp_path)
    document = json.loads((tmp_path / "d.json").read_text())
    document["metadata"]["target"]["catalog"]["identifier"] = "finance"
    write(tmp_path / "d.json", document)

    with pytest.raises(ReleaseError, match=r"more than one place \(gold.a, gold.b, gold.c, \.\.\. in gold of .*; "
                                           r"finance.d in finance of"):
        build_gold(tmp_path, release=True)
    assert not table(tmp_path, "a").exists()
    assert main(["gold", "build", str(tmp_path)]) == 1
    assert "Build with --no-release to build without a release" in capsys.readouterr().err
    assert main(["gold", "build", str(tmp_path), "--no-release"]) == 0
    assert table(tmp_path, "a").exists()


def test_a_catalog_with_no_warehouse_folder_has_nowhere_to_keep_releases_unless_ldp_warehouse_says(
        tmp_path, monkeypatch):
    silver(tmp_path)
    document = json.loads(gold(tmp_path).read_text())
    document["metadata"]["target"]["catalog"] = {"type": "sql", "uri": "sqlite:///elsewhere.db",
                                                 "warehouse": "s3://bucket/warehouse", "namespace": "gold"}
    write(tmp_path / "gold_revenue.json", document)
    monkeypatch.delenv("LDP_WAREHOUSE", raising=False)

    with pytest.raises(ReleaseError, match="no warehouse folder, so there is nowhere to keep its releases"):
        release_scope(find_gold(tmp_path))

    monkeypatch.setenv("LDP_WAREHOUSE", str(tmp_path / "keep"))
    scope = release_scope(find_gold(tmp_path))
    assert scope.namespace == "gold" and scope.root.resolve() == (tmp_path / "keep").resolve()


@pytest.mark.parametrize("scheme", ["", "file://"])
def test_a_sql_catalog_with_a_warehouse_folder_keeps_its_releases_there_and_builds_through_it(tmp_path, scheme):
    silver(tmp_path)
    document = json.loads(gold(tmp_path).read_text())
    document["metadata"]["target"]["catalog"] = {
        "type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}", "warehouse": f"{scheme}{tmp_path / 'wh'}",
        "namespace": "gold", "name": "gold"}
    write(tmp_path / "gold_revenue.json", document)

    result = build_gold(tmp_path, release=True)

    assert result.exit_code == 0 and result.release.cut
    assert (tmp_path / "wh" / ".ldp" / "releases" / "gold" / "r1.json").is_file()
    assert release_scope(find_gold(tmp_path)).root.resolve() == (tmp_path / "wh").resolve()
    assert current_release(tmp_path).table("gold.gold_revenue").rows == 2


def test_a_namespace_that_cannot_name_a_folder_is_refused(tmp_path):
    silver(tmp_path)
    document = json.loads(gold(tmp_path).read_text())
    document["metadata"]["target"]["catalog"]["identifier"] = "a/b"
    write(tmp_path / "gold_revenue.json", document)

    with pytest.raises(ReleaseError, match="cannot name a folder of releases"):
        release_scope(find_gold(tmp_path))


def test_a_release_cannot_be_cut_from_a_table_that_was_not_built_or_checked(tmp_path):
    chain(tmp_path)
    blocking(tmp_path)
    result = build_gold(tmp_path)

    with pytest.raises(ReleaseError, match=r"gold\.b, gold\.c, gold\.d, gold\.z were not built or checked"):
        cut_release(result.tables)


# --------------------------------------------------------------------------- what readers see


@pytest.mark.usefixtures("reading")
def test_a_reader_gets_the_same_rows_from_r1_after_gold_is_rebuilt(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    before = gold_count(tmp_path, "r1")

    more_orders(tmp_path, extra=2)
    build_gold(tmp_path, release=True)

    assert before == 2 and gold_count(tmp_path, "r1") == 2
    assert gold_count(tmp_path) == gold_count(tmp_path, "r2") == gold_count(tmp_path, live=True) == 4


@pytest.mark.usefixtures("reading")
def test_the_current_release_is_what_a_reader_reads_by_default_and_live_reads_main(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    more_orders(tmp_path)

    build_gold(tmp_path, release=False)

    assert gold_count(tmp_path) == 2 and gold_count(tmp_path, "r1") == 2 and gold_count(tmp_path, live=True) == 3
    assert release_snapshots(tmp_path) == {"gold.gold_revenue": current_release(tmp_path).table(
        "gold.gold_revenue").snapshot_id} and release_snapshots(tmp_path, live=True) == {}


def test_register_release_says_which_snapshots_it_used(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    more_orders(tmp_path)
    build_gold(tmp_path, release=False)
    r1 = current_release(tmp_path).table("gold.gold_revenue").snapshot_id

    with DuckDBEngine() as engine:
        used = register_release(engine, tmp_path, release="r1")
    with DuckDBEngine() as engine:
        live = register_release(engine, tmp_path, live=True)

    assert used == {"gold.gold_revenue": r1} and live == {}


def test_a_folder_with_no_release_yet_is_read_live(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path)

    assert choose_release(tmp_path) is None and release_snapshots(tmp_path) == {}
    assert gold_count(tmp_path) == 2


def test_a_table_that_the_release_does_not_hold_is_read_at_its_current_snapshot(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    gold(tmp_path, "gold_late")
    build_gold(tmp_path, release=False)

    release = current_release(tmp_path)

    assert snapshot_for("gold.gold_late", release) is None and snapshot_for("gold.gold_revenue", release) is not None
    assert snapshot_for("gold.gold_revenue", None) is None
    assert gold_count(tmp_path, "r1", name="gold_late") == 2


@pytest.mark.parametrize("text", ["r0", "r01", "1", "release1", "", "r1.5"])
def test_only_r_and_a_whole_number_from_1_names_a_release(text):
    with pytest.raises(ReleaseError, match="is not a release name"):
        releases.parse_release_name(text)
    assert releases.parse_release_name("r12") == 12 and releases.parse_release_name(" R3 ") == 3


def test_the_rule_for_which_release_a_reader_reads(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)

    assert choose_release(tmp_path).name == "r1" and choose_release(tmp_path, release="R1").name == "r1"
    assert choose_release(tmp_path, live=True) is None
    with pytest.raises(ConfigError, match="either a release or --live"):
        choose_release(tmp_path, release="r1", live=True)
    with pytest.raises(ReleaseError, match="there is no release r4 of gold in .*It has r1"):
        choose_release(tmp_path, release="r4")
    with pytest.raises(ReleaseError, match="'x1' is not a release name"):
        choose_release(tmp_path, release="x1")


def test_a_folder_with_no_gold_config_has_no_releases_to_read(tmp_path):
    silver(tmp_path)

    with pytest.raises(ConfigError, match="has no gold config"):
        current_release(tmp_path)


def test_a_snapshot_that_a_release_names_and_the_table_lost_says_so(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    release = current_release(tmp_path)
    document = json.loads(release.path.read_text())
    document["tables"][0]["snapshot_id"] = 12345
    release.path.write_text(json.dumps(document))

    with pytest.raises(ReleaseError, match=r"names a snapshot of gold\.gold_revenue that no longer exists"):
        gold_count(tmp_path, "r1")


@pytest.mark.usefixtures("reading")
def test_ldp_query_reads_the_gold_tables_at_a_release_and_live_by_default(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    more_orders(tmp_path)
    build_gold(tmp_path, release=False)
    sql = "SELECT count(*) AS n FROM gold.gold_revenue"

    assert main(["query", str(tmp_path), sql, "--release", "r1"]) == 0
    assert capsys.readouterr().out.splitlines()[2].strip() == "2"
    assert main(["query", str(tmp_path), sql]) == 0
    assert capsys.readouterr().out.splitlines()[2].strip() == "3"
    assert main(["query", str(tmp_path), sql, "--live"]) == 0
    assert capsys.readouterr().out.splitlines()[2].strip() == "3"
    assert main(["query", str(tmp_path), sql, "--live", "--release", "r1"]) == 1


def test_ldp_query_reads_the_gold_table_of_one_config_at_a_release_too(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    more_orders(tmp_path)
    build_gold(tmp_path, release=False)
    sql = "SELECT count(*) AS n FROM gold_revenue"

    assert main(["query", str(tmp_path / "gold_revenue.json"), sql, "--release", "r1"]) == 0
    assert capsys.readouterr().out.splitlines()[2].strip() == "2"
    assert main(["query", str(tmp_path / "gold_revenue.json"), sql]) == 0
    assert capsys.readouterr().out.splitlines()[2].strip() == "3"


def test_open_lake_takes_a_release_or_a_snapshot_or_a_time_and_not_two_of_them(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)

    with pytest.raises(ConfigError, match="pass one of a release, a snapshot or a time"):
        open_lake(tmp_path / "gold_revenue.json", release="r1", snapshot_id=1)
    with pytest.raises(ConfigError, match="pass one of a release, a snapshot or a time"):
        open_lake(tmp_path / "gold_revenue.json", release="r1", as_of="2030-01-01")


def test_a_table_in_a_catalog_with_no_warehouse_folder_is_left_alone_when_a_release_is_read(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    local = SimpleNamespace(catalog=SimpleNamespace(warehouse=tmp_path / "warehouse"), namespace=("gold",),
                            identifier="gold.gold_revenue")
    remote = SimpleNamespace(catalog=SimpleNamespace(warehouse=None), namespace=("raw",), identifier="raw.orders")

    found = _release_snapshots([remote, local], "r1")

    assert found == {"gold.gold_revenue": current_release(tmp_path).table("gold.gold_revenue").snapshot_id}


def test_ldp_query_names_the_releases_that_exist_when_the_one_asked_for_does_not(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)

    assert main(["query", str(tmp_path), "SELECT 1", "--release", "r7"]) == 1

    assert "there is no release r7 of gold" in capsys.readouterr().err
    assert main(["query", str(tmp_path), "SELECT 1", "--release", "r1", "--snapshot", "5"]) == 1


def test_ldp_query_with_a_release_needs_a_namespace_that_has_releases(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path)

    assert main(["query", str(tmp_path), "SELECT 1", "--release", "r1"]) == 1

    assert "none of the tables is in a release" in capsys.readouterr().err


def test_ldp_query_leaves_a_table_in_a_namespace_without_releases_alone(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)

    assert main(["query", str(tmp_path), "SELECT count(*) AS n FROM lake.silver_orders", "--release", "r1"]) == 0

    assert capsys.readouterr().out.splitlines()[2].strip() == "3"


# --------------------------------------------------------------------------- snapshot expiry


def test_a_released_snapshot_is_never_expired_and_one_that_is_not_released_is(tmp_path):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    released = current_release(tmp_path).table("gold.gold_revenue").snapshot_id
    more_orders(tmp_path)
    build_gold(tmp_path, release=False)
    unreleased = int(table(tmp_path, "gold_revenue").table().current_snapshot().snapshot_id)
    more_orders(tmp_path, extra=2)
    build_gold(tmp_path, release=True)
    iceberg = table(tmp_path, "gold_revenue")

    report = expire_snapshots(iceberg, older_than=dt.timedelta(0), retain_last=1)

    assert unreleased in report["expired_snapshot_ids"] and released not in report["expired_snapshot_ids"]
    assert iceberg.table().snapshot_by_id(released) is not None and iceberg.table().snapshot_by_id(unreleased) is None
    assert gold_count(tmp_path, "r1") == 2


def test_ldp_maintain_keeps_the_released_snapshots_too(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    released = current_release(tmp_path).table("gold.gold_revenue").snapshot_id
    more_orders(tmp_path)
    build_gold(tmp_path, release=True)

    assert main(["maintain", str(tmp_path / "gold_revenue.json"), "--expire", "0", "--retain-last", "1",
                 "--apply"]) == 0
    capsys.readouterr()

    assert table(tmp_path, "gold_revenue").table().snapshot_by_id(released) is not None
    assert gold_count(tmp_path, "r1") == 2


# --------------------------------------------------------------------------- ldp gold releases


def test_ldp_gold_releases_lists_the_releases_newest_first(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    more_orders(tmp_path)
    build_gold(tmp_path, release=True)
    capsys.readouterr()

    assert main(["gold", "releases", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Releases of gold in ") and lines[0].endswith("newest first. r2 is current.")
    assert [line.split()[0] for line in lines[3:5]] == ["r2", "r1"]
    assert lines[3].split()[-2:] == ["1", "3"] and lines[4].split()[-2:] == ["1", "2"]


def test_ldp_gold_releases_show_lists_the_tables_of_one_release(tmp_path, capsys):
    chain(tmp_path)
    build_gold(tmp_path, release=True)
    capsys.readouterr()

    assert main(["gold", "releases", str(tmp_path), "--show", "r1"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("Release r1 of gold, cut ") and lines[0].endswith("with 5 tables:")
    assert [line.split()[0] for line in lines[3:8]] == [f"gold.{name}" for name in "abcdz"]
    assert all("built" in line and "@v1" in line for line in lines[3:8])


def test_ldp_gold_releases_says_so_when_there_is_none_and_names_the_ones_there_are_when_asked_for_another(
        tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)

    assert main(["gold", "releases", str(tmp_path)]) == 0
    assert "No releases of gold in " in capsys.readouterr().out
    build_gold(tmp_path, release=True)
    assert main(["gold", "releases", str(tmp_path), "--show", "r5"]) == 1
    assert "there is no release r5 of gold" in capsys.readouterr().err
    assert main(["gold", "releases", str(tmp_path / "nowhere")]) == 1


def test_ldp_gold_releases_prints_json(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    capsys.readouterr()

    assert main(["gold", "releases", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert main(["gold", "releases", str(tmp_path), "--show", "r1", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)

    assert listed["namespace"] == "gold" and listed["current"] == "r1" and listed["releases"] == [shown]
    assert Release.from_dict(shown).table("gold.gold_revenue").rows == 2


def test_a_release_file_that_cannot_be_read_is_skipped_with_a_warning(tmp_path, caplog):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    more_orders(tmp_path)
    build_gold(tmp_path, release=True)
    release_file(tmp_path, 2).write_text("{not json")

    with caplog.at_level("WARNING"):
        listed = list_releases(tmp_path)

    assert [item.name for item in listed] == ["r1"] and "unreadable release file" in caplog.text
    folder = release_file(tmp_path, 1).parent
    (folder / "notes.json").write_text("{}")
    (folder / "r01.json").write_text(release_file(tmp_path, 1).read_text())
    (folder / "7.json").write_text(release_file(tmp_path, 1).read_text())
    assert [item.name for item in list_releases(tmp_path)] == ["r1"]


# --------------------------------------------------------------------------- run events


def test_a_release_is_recorded_as_an_event_for_each_table_and_ldp_runs_shows_it(tmp_path, capsys):
    chain(tmp_path)
    build_gold(tmp_path, release=True)

    events = [event for event in read_iceberg_events(
        {"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")}, pipeline="b") if
        event.type == "release.cut"]

    [event] = events
    entry = current_release(tmp_path).table("gold.b")
    assert event.type in EVENT_TYPES and releases.RELEASE_EVENT == "release.cut"
    assert event.payload["release"] == "r1" and event.payload["snapshot_id"] == entry.snapshot_id
    assert (event.payload["table"], event.payload["pipeline"], event.payload["tables"]) == ("gold.b", "b", 5)
    assert main(["runs", str(tmp_path / "b.json")]) == 0
    out = capsys.readouterr().out
    released = [line for line in out.splitlines() if " released " in line]
    assert len(released) == 1 and "r1" in released[0] and str(entry.snapshot_id) in released[0]
    assert [run["status"] for run in summarize_runs(events)] == ["released"]


def test_ldp_runs_shows_the_events_of_a_release_with_the_release_and_the_snapshot(tmp_path, capsys):
    silver(tmp_path)
    gold(tmp_path)
    build_gold(tmp_path, release=True)
    entry = current_release(tmp_path).table("gold.gold_revenue")
    event = next(item for item in read_iceberg_events(
        {"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")}, pipeline="gold_revenue")
        if item.type == "release.cut")

    assert main(["runs", str(tmp_path / "gold_revenue.json"), "--run", event.run_id]) == 0

    out = capsys.readouterr().out
    assert "release.cut" in out and f"release=r1 snapshot={entry.snapshot_id} rows=2" in out


def test_the_release_event_goes_to_the_sinks_the_config_names_and_not_to_the_iceberg_sink(tmp_path):
    silver(tmp_path)
    log = tmp_path / "events.jsonl"
    gold(tmp_path, observability={"sinks": [{"type": "jsonl", "path": str(log)}]})

    build_gold(tmp_path, release=True)

    types = [json.loads(line)["type"] for line in log.read_text().splitlines()]
    assert types[0] == "run.started" and types[-1] == "release.cut"
    assert read_iceberg_events({"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")}) == []


def test_a_sink_that_fails_does_not_fail_the_release(tmp_path):
    chain(tmp_path)
    built = build_gold(tmp_path).tables

    class Broken:
        def emit(self, event):
            raise RuntimeError("no room")

        def flush(self):
            raise RuntimeError("no room")

    outcome = cut_release(built, sink_for=lambda gold_config: Broken())

    assert outcome.cut and current_release(tmp_path).name == "r1"


def test_the_openlineage_sink_leaves_release_events_alone(tmp_path):
    path = tmp_path / "lineage.jsonl"
    sink = OpenLineageSink(path)
    event = RunEvent(event_id=uuid7(), type="release.cut", schema_version=1, ts=dt.datetime.now(dt.timezone.utc),
                     run_id=uuid7(), attempt=1, seq=0, payload={"pipeline": "a", "release": "r1"})

    sink.emit(event)
    sink.flush()

    assert not path.exists() and sink._runs == {}


# --------------------------------------------------------------------------- the gold task of a workflow


def workflow(root: Path, *, depends_on=("orders",), options=None, extra=()) -> Path:
    """A workflow that loads the orders and then runs the gold task over ``config``."""
    tasks = [{"id": "orders", "pipeline": "config/silver_orders.json"},
             {"id": "gold", "depends_on": list(depends_on), "gold": {"dir": "config"} if options is None else options},
             *extra]
    return write(root / "daily.json", {"apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_gold",
                                       "tasks": tasks})


def flow(tmp_path: Path) -> tuple[Path, Path]:
    """``config/`` with silver and two gold tables, one reading the other, and the workflow over it."""
    config = tmp_path / "config"
    silver(config, build=False)
    gold(config, "gold_revenue")
    gold(config, "gold_top", sql="SELECT restaurant, revenue FROM gold_revenue ORDER BY revenue DESC LIMIT 1",
         inputs=["gold_revenue.json"], columns={"restaurant": "The restaurant.", "revenue": "What they cost."})
    return config, workflow(tmp_path)


DAY_1, DAY_2 = "2026-08-01/2026-08-02", "2026-08-02/2026-08-03"


def test_a_workflow_with_a_gold_task_builds_the_tables_for_the_window_and_cuts_a_release(tmp_path):
    config, file = flow(tmp_path)

    result = run_workflow(file, window=DAY_1)

    assert result.exit_code == 0 and [item.status for item in result.tasks.values()] == ["published", "succeeded"]
    task = result.tasks["gold"]
    assert task.kind == "gold" and task.detail["release"] == "r1" and task.detail["release_cut"] is True
    assert [item["table"] for item in task.detail["tables"]] == ["gold.gold_revenue", "gold.gold_top"]
    assert task.detail["window"] == ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"]
    release = current_release(config)
    assert release.name == "r1" and release.window == tuple(task.detail["window"])
    assert dict(release.workflow) == {"identifier": "daily_gold", "run_id": result.run_id, "task": "gold"}
    assert gold_count(config, "r1") == 2 and gold_count(config, "r1", name="gold_top") == 1


def test_the_events_of_the_gold_tables_carry_the_workflow_and_the_task(tmp_path):
    config, file = flow(tmp_path)

    result = run_workflow(file, window=DAY_1)

    events = [json.loads(line) for line in (tmp_path / ".ldp" / "events.jsonl").read_text().splitlines()]
    mine = [event for event in events if event["payload"].get("task_id") == "gold"]
    assert {event["payload"]["pipeline"] for event in mine if event["type"] == "run.published"} == {
        "gold_revenue", "gold_top"}
    cut = [event for event in mine if event["type"] == "release.cut"]
    assert sorted(event["payload"]["table"] for event in cut) == ["gold.gold_revenue", "gold.gold_top"]
    assert {event["payload"]["workflow_run_id"] for event in cut} == {result.run_id}
    assert {tuple(event["payload"]["window"]) for event in cut} == {("2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")}
    started = [event for event in mine if event["type"] == "run.started" and "mode" not in event["payload"]]
    assert [event["payload"]["task_kind"] for event in started] == ["gold"]


def test_running_the_same_window_again_writes_nothing_and_cuts_no_second_release(tmp_path):
    config, file = flow(tmp_path)
    run_workflow(file, window=DAY_1)

    again = run_workflow(file, window=DAY_1)

    assert [item.status for item in again.tasks.values()] == ["skipped_duplicate", "skipped_duplicate"]
    assert again.tasks["gold"].detail["release_cut"] is False and again.tasks["gold"].detail["release"] == "r1"
    assert [item.name for item in list_releases(config)] == ["r1"]


def test_the_next_window_builds_again_and_cuts_the_next_release(tmp_path):
    config, file = flow(tmp_path)
    run_workflow(file, window=DAY_1)

    second = run_workflow(file, window=DAY_2)

    assert [item.status for item in second.tasks.values()] == ["published", "succeeded"]
    assert [item.name for item in list_releases(config)] == ["r2", "r1"]
    assert current_release(config).window == ("2026-08-02T00:00:00Z", "2026-08-03T00:00:00Z")


def test_a_gold_task_that_a_gate_blocks_ends_blocked_quality_and_cuts_no_release(tmp_path):
    config, file = flow(tmp_path)
    run_workflow(file, window=DAY_1)
    blocking(config, "gold_top")

    result = run_workflow(file, window=DAY_2)

    task = result.tasks["gold"]
    assert task.status == "blocked_quality" and result.exit_code == 3 and "gold_top blocked" in task.error
    assert task.detail["release"] == "r1" and task.detail["release_cut"] is False
    assert [item["status"] for item in task.detail["tables"]] == [BUILT, BLOCKED]
    assert [item.name for item in list_releases(config)] == ["r1"]


def test_a_gold_task_whose_release_cannot_be_cut_ends_failed_and_the_tables_stay_built(tmp_path, monkeypatch):
    config, file = flow(tmp_path)

    def refuse(tables, **keywords):
        raise ReleaseError("the warehouse is read only")

    monkeypatch.setattr(releases, "cut_release", refuse)
    result = run_workflow(file, window=DAY_1)

    task = result.tasks["gold"]
    assert task.status == "failed" and result.exit_code == 1
    assert "the gold tables were built, and no release was cut: the warehouse is read only" in task.error
    assert task.detail["release"] is None and task.detail["release_cut"] is False
    assert table(config, "gold_revenue").exists() and list_releases(config) == []


def test_a_gold_task_with_a_table_that_fails_ends_failed(tmp_path):
    config, file = flow(tmp_path)
    gold(config, "gold_broken", sql="SELECT nope FROM silver_orders")

    result = run_workflow(file, window=DAY_1)

    assert result.tasks["gold"].status == "failed" and result.exit_code == 1
    assert "gold_broken failed" in result.tasks["gold"].error and list_releases(config) == []


def test_a_gold_task_does_not_run_when_the_task_it_depends_on_did_not_succeed(tmp_path):
    config, file = flow(tmp_path)
    (config / "orders.csv").unlink()

    result = run_workflow(file, window=DAY_1)

    assert [item.status for item in result.tasks.values()] == ["failed", "upstream_failed"]
    assert list_releases(config) == []


def test_the_plan_of_a_gold_task_lists_its_tables_and_what_it_reads(tmp_path):
    config, file = flow(tmp_path)

    plan = plan_workflow(file, window=DAY_1)

    assert plan["valid"] and plan["warnings"] == []
    entry = plan["tasks"][1]
    assert (entry["kind"], entry["dir"], entry["target"]) == ("gold", "config", "gold build, release in gold")
    assert [item["table"] for item in entry["tables"]] == ["gold.gold_revenue", "gold.gold_top"]
    assert all(item["built"] and len(item["idempotency_key"]) == 64 for item in entry["tables"])


def test_the_plan_warns_when_the_gold_task_reads_a_table_that_nothing_orders_before_it(tmp_path):
    config, file = flow(tmp_path)
    workflow(tmp_path, depends_on=())

    plan = plan_workflow(file, window=DAY_1)

    assert plan["valid"]
    assert any("task 'gold' reads the table lake.silver_orders, which task 'orders' writes" in message
               for message in plan["warnings"])


@pytest.mark.parametrize("make, fragment", [
    (lambda root: workflow(root, options={"dir": "nowhere"}), "is not a folder of dataset configs"),
    (lambda root: workflow(root, options={"dir": "config", "release": False}), "has unknown keys ['release']"),
    (lambda root: workflow(root, options={}), ".dir: is required"),
    (lambda root: workflow(root, options={"dir": ""}), ".dir: is required"),
])
def test_the_plan_of_a_gold_task_says_what_is_wrong_with_it(tmp_path, make, fragment):
    config, _ = flow(tmp_path)
    (tmp_path / "empty").mkdir()
    file = make(tmp_path)

    plan = plan_workflow(file, window=DAY_1)

    assert not plan["valid"] and any(fragment in message for message in plan["errors"]), plan["errors"]


def test_a_folder_with_no_gold_config_yet_is_a_warning_in_the_plan_and_a_failure_when_the_task_runs(tmp_path):
    config, _ = flow(tmp_path)
    (tmp_path / "empty").mkdir()
    file = workflow(tmp_path, options={"dir": "empty"})

    plan = plan_workflow(file, window=DAY_1)
    result = run_workflow(file, window=DAY_1)

    assert plan["valid"] and any("has no gold config yet" in message for message in plan["warnings"])
    assert result.tasks["gold"].status == "failed" and "has no gold config" in result.tasks["gold"].error


def test_a_task_that_only_checks_tables_and_finds_them_where_the_release_has_them_is_skipped(tmp_path):
    config = tmp_path / "config"
    silver(config, build=False)
    external(config)
    file = workflow(tmp_path)

    first = run_workflow(file, window=DAY_1)
    again = run_workflow(file, window=DAY_1)

    assert first.tasks["gold"].status == "succeeded" and first.tasks["gold"].detail["release_cut"] is True
    assert again.tasks["gold"].status == "skipped_duplicate" and again.tasks["gold"].detail["release"] == "r1"
    assert [item.name for item in list_releases(config)] == ["r1"]


def test_the_plan_says_so_when_the_gold_tables_are_in_two_namespaces(tmp_path):
    config, file = flow(tmp_path)
    document = json.loads((config / "gold_top.json").read_text())
    document["metadata"]["target"]["catalog"]["identifier"] = "finance"
    write(config / "gold_top.json", document)

    plan = plan_workflow(file, window=DAY_1)

    assert not plan["valid"] and any("more than one place" in message for message in plan["errors"])


def test_a_replace_run_needs_a_window_column_in_each_gold_config_as_a_pipeline_task_does(tmp_path):
    config, file = flow(tmp_path)

    plan = plan_workflow(file, window=DAY_1, replace=True)

    assert not plan["valid"]
    assert any("config/gold_revenue.json has no metadata.window.column" in message for message in plan["errors"])
    assert any("config/gold_top.json has no metadata.window.column" in message for message in plan["errors"])


def test_a_gold_table_that_is_built_elsewhere_is_only_checked_by_the_task(tmp_path):
    config, file = flow(tmp_path)
    external(config)

    plan = plan_workflow(file, window=DAY_1)
    result = run_workflow(file, window=DAY_1)

    assert any("is built elsewhere, so it is only checked" in message for message in plan["warnings"])
    assert result.exit_code == 0
    assert [item["status"] for item in result.tasks["gold"].detail["tables"]] == [CHECKED, BUILT, BUILT]
    assert current_release(config).table("gold.elsewhere").status == CHECKED


@pytest.mark.dbt
def test_a_workflow_runs_ingest_dbt_and_the_gold_build_with_its_release_as_one_unit(
        tmp_path, network_trap, monkeypatch):
    env, connections = network_trap
    for name, value in env.items():
        if "proxy" in name.lower():
            monkeypatch.setenv(name, value)
    shutil.copytree(Path(__file__).parent / "fixtures" / "dbt_gold", tmp_path / "dbt")
    write(tmp_path / "dbt" / "ldp_dbt.json", {
        "database": "target/ldp_build.duckdb", "sources": {"configs": ["../config"]},
        "publish": {"catalog": {"identifier": "gold", "warehouse_path": "../warehouse"}, "config_dir": "../config"}})
    write(tmp_path / "landing" / "orders.csv", "id,at,amount\n1,2026-08-01T10:00:00,10.0\n2,2026-08-01T11:00:00,20.0\n"
          "3,2026-08-02T09:00:00,5.0\n4,2026-08-02T10:00:00,7.5\n")
    write(tmp_path / "config" / "orders.json", {"identifier": "orders", "metadata": {
        "source": {"name": "orders", "format": "CSV", "path": "../landing/orders.csv"},
        "target": {"name": "orders", "format": "ICEBERG", "write_mode": "append",
                   "catalog": {"identifier": "shop", "warehouse_path": "../warehouse"}},
        "window": {"size": "day", "column": "at"}}})
    file = write(tmp_path / "daily.json", {
        "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily_orders",
        "metadata": {"schedule": "@daily", "window": {"size": "day"}},
        "tasks": [{"id": "orders", "pipeline": "config/orders.json"},
                  {"id": "models", "depends_on": ["orders"], "dbt": {"project": "dbt"}},
                  {"id": "gold", "depends_on": ["models"], "gold": {"dir": "config"}}]})
    config = tmp_path / "config"

    first = run_workflow(file, window=DAY_1)

    assert first.exit_code == 0 and {task.id: task.status for task in first.tasks.values()} == {
        "orders": "published", "models": "succeeded", "gold": "succeeded"}
    release = current_release(config)
    assert release.name == "r1" and [(item.table, item.status) for item in release.tables] == [
        ("gold.daily_revenue", CHECKED)]
    assert release.workflow["identifier"] == "daily_orders" and release.rows == 1
    again = run_workflow(file, window=DAY_1)
    assert {task.id: task.status for task in again.tasks.values()} == {
        "orders": "skipped_duplicate", "models": "skipped_duplicate", "gold": "skipped_duplicate"}
    second = run_workflow(file, window=DAY_2)
    assert second.exit_code == 0 and second.tasks["gold"].detail["release_cut"] is True
    assert [item.name for item in list_releases(config)] == ["r2", "r1"]
    assert get_release(config, "r1").table("gold.daily_revenue").rows == 1
    assert current_release(config).table("gold.daily_revenue").rows == 2
    assert connections == [], f"something opened a network connection: {connections}"


def test_the_gold_kind_is_registered_with_a_schema_and_a_description():
    kind = get_task_kind("gold")

    assert "gold" in task_kinds() and kind.name == "gold" and not kind.emits_events and kind.changes_data
    assert "cut a release" in kind.description or "cut" in kind.description
    schema = workflow_schema()["$defs"]["task"]["properties"]["gold"]
    assert schema["required"] == ["dir"] and schema["additionalProperties"] is False


def test_ldp_workflow_run_prints_the_tables_and_the_release_of_a_gold_task(tmp_path, capsys):
    config, file = flow(tmp_path)

    assert main(["workflow", "run", str(file), "--window", DAY_1]) == 0
    first = capsys.readouterr().out
    assert main(["workflow", "run", str(file), "--window", DAY_1]) == 0
    again = capsys.readouterr().out

    assert "gold built gold.gold_revenue: 2 rows, snapshot " in first and "gold cut release r1" in first
    assert "gold skipped gold.gold_revenue: the window was already published, snapshot " in again
    assert "gold cut no release, and r1 is current" in again
    assert main(["workflow", "plan", str(file), "--window", DAY_1]) == 0
    assert "gold build, release in gold" in capsys.readouterr().out


def test_each_gold_table_has_a_run_id_of_its_own_that_is_the_same_for_every_attempt_of_the_task(tmp_path):
    config, _ = flow(tmp_path)
    revenue, top = find_gold(config)
    sink = MemorySink()

    def context(task_run_id: str, attempt: int) -> RunContext:
        return RunContext(workflow=None, bounds=(None, None), run_id="workflow-run", sink=sink,
                          task_run_id=task_run_id, attempt=attempt)

    first = _table_run_id(context("task-run", 1), revenue)

    assert first == _table_run_id(context("task-run", 2), revenue)
    assert first != _table_run_id(context("task-run", 1), top) != _table_run_id(context("other-run", 1), top)


def test_the_gold_tables_of_the_same_run_have_events_that_ldp_runs_keeps_apart(tmp_path):
    config, file = flow(tmp_path)
    run_workflow(file, window=DAY_1)

    from local_data_platform.events import read_jsonl

    runs = summarize_runs(read_jsonl(tmp_path / ".ldp" / "events.jsonl"))
    released = [run for run in runs if run["status"] == "released"]

    assert sorted(run["pipeline"] for run in released) == ["gold_revenue", "gold_top"]
    assert len({run["run_id"] for run in released}) == 2


def test_every_snapshot_of_a_release_is_in_its_table_with_the_rows_the_release_says(tmp_path):
    chain(tmp_path)
    build_gold(tmp_path, release=True)
    release = current_release(tmp_path)

    for item in release.tables:
        iceberg = iceberg_from_config(Config.from_json(tmp_path / item.config), "target", must_exist=True)
        assert iceberg.table().snapshot_by_id(item.snapshot_id) is not None
        assert item.rows == iceberg.table().scan(snapshot_id=item.snapshot_id).to_arrow().num_rows
    assert os.path.samefile(release.path, release_file(tmp_path, 1))
