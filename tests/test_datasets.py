"""Tests for reproducible dataset versions: pin, load, list_versions, get_version, export and the CLI."""

import datetime as dt
import json
import os
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from local_data_platform import datasets
from local_data_platform.cli import main
from local_data_platform.datasets import (
    DatasetError,
    DatasetVersion,
    export,
    get_version,
    list_datasets,
    list_versions,
    load,
    pin,
)
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.format.iceberg import Iceberg

T0 = dt.datetime(2026, 9, 1, 8, 0, tzinfo=dt.timezone.utc)


def episodes(start: int, count: int) -> pa.Table:
    ids = range(start, start + count)
    return pa.table({
        "episode_id": pa.array(ids, pa.int64()),
        "robot_id": [f"h{i % 3}" for i in ids],
        "success": [i % 2 == 0 for i in ids],
        "start_ts": pa.array([T0 + dt.timedelta(minutes=i) for i in ids], pa.timestamp("us", tz="UTC")),
        "joint_state": pa.array([[float(i), i / 2.0] for i in ids], pa.list_(pa.float64())),
    })


@pytest.fixture
def table(catalog_config):
    iceberg = Iceberg("episodes", catalog_config,
                      partition_by=[{"column": "robot_id", "transform": "bucket[4]"}])
    iceberg.put(episodes(0, 30))
    return iceberg


@pytest.fixture
def warehouse(catalog_config):
    return catalog_config["warehouse_path"]


# --------------------------------------------------------------------------- pin


def test_pin_writes_a_manifest_under_the_warehouse(table, warehouse):
    version = pin(table, "train", row_filter="success = true", selected_fields=["episode_id", "robot_id"],
                  properties={"split": "train"})
    assert isinstance(version, DatasetVersion)
    assert version.name == "train" and version.version == 1 and version.ref == "train@v1"
    assert version.table_identifier == "test_ns.episodes"
    assert version.snapshot_id == table.table().current_snapshot().snapshot_id
    assert version.row_filter == "success = true"
    assert version.selected_fields == ("episode_id", "robot_id")
    assert version.row_count == 15
    assert version.schema_fingerprint.startswith("sha256:")
    assert version.created_at.tzinfo is not None
    assert version.properties == {"split": "train"}
    manifest = os.path.join(warehouse, ".ldp", "datasets", "train", "v000001.json")
    assert version.manifest_path == manifest
    data = json.loads(open(manifest).read())
    assert data["ldp_dataset"] == 1
    assert data["catalog"] == {"type": "local", "identifier": "test_ns", "warehouse_path": warehouse}
    assert DatasetVersion.from_dict(data, manifest_path=manifest) == version


def test_pin_counts_every_row_without_a_filter(table):
    version = pin(table, "all")
    assert version.row_count == 30
    assert version.row_filter is None and version.selected_fields is None


def test_pin_accepts_comma_separated_fields_and_a_pyiceberg_table(table, warehouse):
    version = pin(table.table(), "raw", selected_fields="episode_id, success", warehouse=warehouse)
    assert version.selected_fields == ("episode_id", "success")
    assert load(version).column_names == ["episode_id", "success"]


def test_versions_increase_per_name(table, warehouse):
    first = pin(table, "train")
    table.put(episodes(30, 5))
    second = pin(table, "train")
    other = pin(table, "eval")
    assert (first.version, second.version, other.version) == (1, 2, 1)
    assert second.row_count == 35
    assert [v.version for v in list_versions("train", warehouse=warehouse)] == [1, 2]
    assert list_datasets(warehouse=warehouse) == ["eval", "train"]


def test_concurrent_pins_get_distinct_versions(table, warehouse):
    results, errors = [], []

    def work():
        try:
            results.append(pin(table, "race", tag=False).version)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert sorted(results) == [1, 2, 3, 4, 5, 6]


def test_pin_an_older_snapshot(table):
    first_snapshot = table.table().current_snapshot().snapshot_id
    table.put(episodes(30, 10))
    version = pin(table, "old", snapshot_id=first_snapshot)
    assert version.snapshot_id == first_snapshot and version.row_count == 30


@pytest.mark.parametrize("kwargs, error, fragment", [
    ({"name": "../escape"}, ConfigError, "dataset name"),
    ({"name": ".hidden"}, ConfigError, "dataset name"),
    ({"name": ""}, ConfigError, "dataset name"),
    ({"name": "ok", "row_filter": "success = = true"}, ConfigError, "invalid row_filter"),
    ({"name": "ok", "row_filter": "nope = 1"}, ConfigError, "cannot pin"),
    ({"name": "ok", "selected_fields": ["nope"]}, ConfigError, "cannot pin"),
    ({"name": "ok", "selected_fields": []}, ConfigError, "selected_fields"),
    ({"name": "ok", "snapshot_id": 123}, DatasetError, "has no snapshot 123"),
])
def test_pin_rejects_bad_arguments(table, kwargs, error, fragment):
    with pytest.raises(error) as raised:
        pin(table, **kwargs)
    assert fragment in str(raised.value)


def test_pin_rejects_expression_objects(table):
    from pyiceberg.expressions import EqualTo

    with pytest.raises(TypeError, match="row_filter must be a string"):
        pin(table, "expr", row_filter=EqualTo("success", True))


def test_pin_needs_a_snapshot_and_an_existing_table(catalog_config):
    with pytest.raises(TableNotFound):
        pin(Iceberg("missing", catalog_config), "x")


def test_pin_needs_a_warehouse_for_a_bare_table(table, monkeypatch):
    monkeypatch.delenv(datasets.WAREHOUSE_ENV, raising=False)
    monkeypatch.setattr(datasets, "_catalog_spec", lambda source, tbl: {})

    class Bare:
        def table(self):
            return table.table()

    with pytest.raises(ConfigError, match="LDP_WAREHOUSE"):
        pin(Bare(), "x")


def test_the_warehouse_env_var_is_the_default(table, warehouse, monkeypatch):
    pin(table, "env")
    monkeypatch.setenv(datasets.WAREHOUSE_ENV, warehouse)
    assert [v.ref for v in list_versions("env")] == ["env@v1"]
    assert get_version("env").version == 1


# --------------------------------------------------------------------------- load


def test_load_returns_identical_rows_after_later_writes(table):
    version = pin(table, "train", row_filter="success = true AND robot_id != 'h2'",
                  selected_fields=["episode_id", "robot_id", "start_ts", "joint_state"])
    pinned = load(version)
    assert pinned.num_rows == version.row_count == 10

    table.put(episodes(30, 10))                                  # append
    table.put(episodes(100, 4), mode="overwrite")                # replace everything
    evolved = episodes(200, 2).append_column("gripper", pa.array([0.1, 0.2]))
    table.put(evolved)                                           # schema evolution
    Iceberg("episodes", {"identifier": "test_ns", "warehouse_path": str(table.path)},
            join_cols=["episode_id"]).put(episodes(100, 2).append_column("gripper", pa.array([1.0, 2.0])),
                                          mode="upsert")
    assert table.row_count() == 6

    again = load(version)
    assert again.equals(pinned)
    assert again.schema.equals(pinned.schema)
    reloaded = load(get_version("train", warehouse=table.path))
    assert reloaded.equals(pinned)


def test_load_after_the_catalog_is_gone_uses_the_metadata_file(table, warehouse):
    version = pin(table, "train")
    rows = load(version)
    table.catalog.close()
    os.remove(os.path.join(warehouse, "test_ns_catalog.db"))
    assert load(version).equals(rows)
    assert not os.path.exists(os.path.join(warehouse, "test_ns_catalog.db")), "load must not create a catalog"


def test_load_with_an_explicit_catalog(table):
    version = pin(table, "train")
    assert load(version, catalog=table.catalog).num_rows == 30
    spec = {"identifier": "test_ns", "warehouse_path": str(table.path)}
    assert load(version, catalog=spec).num_rows == 30


def test_load_detects_tampered_manifests(table):
    version = pin(table, "train")
    from dataclasses import replace

    with pytest.raises(DatasetError, match="read 30 rows, the manifest pinned 31"):
        load(replace(version, row_count=31))
    with pytest.raises(DatasetError, match="schema fingerprint"):
        load(replace(version, schema_fingerprint="sha256:0"))
    assert load(replace(version, row_count=31), verify=False).num_rows == 30
    with pytest.raises(DatasetError, match="no longer exists"):
        load(replace(version, snapshot_id=42))


def test_load_fails_clearly_when_nothing_can_open_the_table(table):
    from dataclasses import replace

    version = replace(pin(table, "train"), catalog={}, metadata_location=None)
    with pytest.raises(DatasetError, match="Pass catalog="):
        load(version)
    with pytest.raises(TypeError, match="get_version"):
        load("train@v1")


def test_tag_protects_the_pinned_snapshot_from_expiry(table):
    tagged = pin(table, "kept")
    untagged_snapshot = table.table().current_snapshot().snapshot_id
    untagged = pin(table, "lost", tag=False)
    assert tagged.tag == "ldp_ds_kept_v1" and untagged.tag is None
    assert "ldp_ds_kept_v1" in table.table().metadata.refs
    table.put(episodes(30, 5))
    table.put(episodes(40, 5))
    pyiceberg_table = table.table()
    maintenance = getattr(pyiceberg_table, "maintenance", None)
    if maintenance is None:
        pytest.skip("pyiceberg < 0.10 has no snapshot expiry API")
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    maintenance.expire_snapshots().older_than(later).commit()
    assert table.table().snapshot_by_id(tagged.snapshot_id) is not None
    assert load(tagged).num_rows == 30
    assert untagged_snapshot == tagged.snapshot_id  # the same snapshot: the tag protects both pins
    assert load(untagged).num_rows == 30


def test_untagged_snapshot_can_expire_and_load_says_so(table):
    table.put(episodes(30, 5))
    old = table.table().snapshots()[0].snapshot_id
    version = pin(table, "gone", snapshot_id=old, tag=False)
    table.put(episodes(40, 5))
    maintenance = getattr(table.table(), "maintenance", None)
    if maintenance is None:
        pytest.skip("pyiceberg < 0.10 has no snapshot expiry API")
    maintenance.expire_snapshots().by_id(old).commit()
    with pytest.raises(DatasetError, match="no longer exists"):
        load(version)


def test_names_that_are_not_tag_safe_get_a_hashed_tag(table):
    first = pin(table, "a-b")
    second = pin(table, "a.b")
    assert first.tag != second.tag
    assert first.tag.startswith("ldp_ds_a_b_") and first.tag.endswith("_v1")


def test_a_failed_tag_is_logged_and_dropped_from_the_manifest(table, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(datasets, "_create_tag", fail)
    version = pin(table, "untagged")
    assert version.tag is None
    assert json.loads(open(version.manifest_path).read())["tag"] is None
    assert "could not tag snapshot" in caplog.text


# --------------------------------------------------------------------------- listing


def test_get_version_by_number_ref_and_latest(table, warehouse):
    pin(table, "train")
    table.put(episodes(30, 2))
    pin(table, "train")
    assert get_version("train", warehouse=warehouse).version == 2
    assert get_version("train", 1, warehouse=warehouse).row_count == 30
    assert get_version("train@v1", warehouse=warehouse).version == 1
    with pytest.raises(DatasetError, match="no version 7"):
        get_version("train", 7, warehouse=warehouse)
    with pytest.raises(DatasetError, match="no versions"):
        get_version("nothing", warehouse=warehouse)


def test_listing_an_empty_warehouse(tmp_path):
    assert list_datasets(warehouse=tmp_path) == []
    assert list_versions("train", warehouse=tmp_path) == []


def test_unreadable_manifests_are_skipped(table, warehouse, caplog):
    pin(table, "train")
    folder = os.path.join(warehouse, ".ldp", "datasets", "train")
    with open(os.path.join(folder, "v000002.json"), "w") as handle:
        handle.write("{not json")
    assert [v.version for v in list_versions("train", warehouse=warehouse)] == [1]
    assert "unreadable dataset manifest" in caplog.text


def test_datasets_dir_accepts_an_iceberg_table(table, warehouse):
    assert str(datasets.datasets_dir(table)) == os.path.join(warehouse, ".ldp", "datasets")


# --------------------------------------------------------------------------- export


def test_export_parquet_records_the_manifest(table, tmp_path):
    version = pin(table, "train", row_filter="success = true")
    written = export(version, tmp_path / "out" / "train.parquet")
    assert written == 15
    exported = pq.read_table(tmp_path / "out" / "train.parquet")
    assert exported.num_rows == 15
    manifest = json.loads(exported.schema.metadata[b"ldp.dataset"])
    assert manifest["snapshot_id"] == version.snapshot_id and manifest["row_filter"] == "success = true"
    assert exported.select(["episode_id"]).equals(load(version).select(["episode_id"]))


def test_export_jsonl(table, tmp_path):
    version = pin(table, "train", selected_fields=["episode_id", "start_ts", "joint_state"])
    assert export(version, "train.jsonl", format="jsonl", base_dir=tmp_path) == 30
    lines = (tmp_path / "train.jsonl").read_text().splitlines()
    assert len(lines) == 30
    first = json.loads(lines[0])
    assert first["episode_id"] == 0
    assert dt.datetime.fromisoformat(first["start_ts"]) == T0
    assert first["joint_state"] == [0.0, 0.0]


def test_export_jsonl_writes_null_for_nan(catalog_config, tmp_path):
    iceberg = Iceberg("nan", catalog_config)
    iceberg.put(pa.table({"x": [1.0, float("nan")], "v": pa.array([[float("inf")], [2.0]], pa.list_(pa.float64()))}))
    export(pin(iceberg, "nan"), tmp_path / "nan.jsonl", format="jsonl")
    rows = [json.loads(line) for line in (tmp_path / "nan.jsonl").read_text().splitlines()]
    assert rows == [{"x": 1.0, "v": [None]}, {"x": None, "v": [2.0]}]


def test_export_rejects_unknown_formats(table, tmp_path):
    with pytest.raises(ConfigError, match="unknown export format"):
        export(pin(table, "train"), tmp_path / "x.csv", format="csv")


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def cli_config(tmp_path, table, catalog_config):
    path = tmp_path / "episodes.json"
    path.write_text(json.dumps({
        "identifier": "episodes",
        "metadata": {
            "source": {"name": "episodes", "format": "PARQUET", "path": "episodes.parquet"},
            "target": {"name": "episodes", "format": "ICEBERG", "catalog": catalog_config},
        },
    }))
    return path


def run_cli(argv):
    import argparse

    parser = argparse.ArgumentParser(prog="ldp")
    datasets.add_cli(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    return args.handler(args)


def test_cli_pin_list_export(cli_config, tmp_path, capsys, warehouse):
    assert run_cli(["datasets", "pin", str(cli_config), "train", "--filter", "success = true",
                    "--fields", "episode_id,robot_id", "--property", "split=train"]) == 0
    out = capsys.readouterr().out
    assert "Pinned train@v1: 15 rows of test_ns.episodes" in out and "tag ldp_ds_train_v1" in out

    assert run_cli(["datasets", "list", "--config", str(cli_config)]) == 0
    listing = capsys.readouterr().out
    assert "train" in listing and "success = true" in listing

    assert run_cli(["datasets", "list", "train", "--warehouse", warehouse]) == 0
    assert "episode_id,robot_id" in capsys.readouterr().out

    dest = tmp_path / "train.jsonl"
    assert run_cli(["datasets", "export", "train@v1", str(dest), "--format", "jsonl", "--warehouse", warehouse]) == 0
    assert "Exported train@v1 (15 rows" in capsys.readouterr().out
    assert len(dest.read_text().splitlines()) == 15


def test_cli_errors(cli_config, tmp_path, warehouse):
    with pytest.raises(ConfigError, match="KEY=VALUE"):
        run_cli(["datasets", "pin", str(cli_config), "x", "--property", "nokey"])
    with pytest.raises(DatasetError, match="no versions"):
        run_cli(["datasets", "list", "missing", "--warehouse", warehouse])
    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({
        "identifier": "m",
        "metadata": {
            "source": {"name": "m", "format": "PARQUET", "path": "m.parquet"},
            "target": {"name": "m", "format": "ICEBERG",
                       "catalog": {"identifier": "nope", "warehouse_path": "nowhere"}},
        },
    }))
    with pytest.raises(TableNotFound, match="no catalog at"):
        run_cli(["datasets", "pin", str(missing), "x"])
    assert not (tmp_path / "nowhere").exists()


def test_cli_list_of_an_empty_warehouse(tmp_path, capsys):
    assert run_cli(["datasets", "list", "--warehouse", str(tmp_path)]) == 0
    assert "No datasets" in capsys.readouterr().out


def test_cli_datasets_without_an_action_prints_help(capsys):
    assert run_cli(["datasets"]) == 1
    assert "pin" in capsys.readouterr().out


def test_ldp_main_wires_the_datasets_command(cli_config, capsys):
    """cli.py registers add_cli (C9), so `ldp datasets` works end to end."""
    import local_data_platform.cli as cli

    cli.build_parser().parse_args(["datasets", "list", "--config", str(cli_config)])
    assert main(["datasets", "pin", str(cli_config), "cli"]) == 0
    assert "Pinned cli@v1" in capsys.readouterr().out
