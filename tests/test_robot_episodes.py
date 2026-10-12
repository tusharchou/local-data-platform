"""The humanoid robot-episode example (examples/robot_episodes) runs offline, fast, and tells the truth.

The full walkthrough runs once per module (about a second). The Scala Spark aggregate is opt-in:
set ``LDP_RUN_SPARK=1`` with scala-cli installed (it downloads Spark and Iceberg on first use).
"""

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from local_data_platform import Config
from local_data_platform.datasets import get_version, list_datasets, list_versions, load
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline.registry import get_pipeline_class
from local_data_platform.quality import checks_from_config, run_checks

pytest.importorskip("duckdb")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "robot_episodes"
CONFIGS = sorted((EXAMPLE / "configs").glob("*.json"))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load("robot_episodes_run", EXAMPLE / "run.py")
generate = run.generate


@pytest.fixture(scope="module")
def walkthrough(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("robots") / "work"
    return workdir, run.run_robotics(workdir, quiet=True)


def _frame_checks():
    return checks_from_config(Config.from_json(EXAMPLE / "configs" / "silver_frames.json").quality["checks"])


# --------------------------------------------------------------------------- generator


def test_generator_is_deterministic_and_seeded():
    first = generate.generate_batch("b01", dt.date(2026, 9, 1))
    again = generate.generate_batch("b01", dt.date(2026, 9, 1))
    other = generate.generate_batch("b01", dt.date(2026, 9, 1), seed=8)
    assert first[0].equals(again[0]) and first[1].equals(again[1])
    assert not first[1].equals(other[1])
    episodes, frames = first
    assert episodes.schema.equals(generate.EPISODE_SCHEMA) and frames.schema.equals(generate.FRAME_SCHEMA)
    assert episodes.num_rows == 8 * 6
    assert frames.num_rows == sum(episodes.column("frame_count").to_pylist())
    assert set(episodes.column("robot_id").to_pylist()) == set(generate.ROBOTS)
    assert all(len(joints) == generate.JOINTS for joints in frames.column("joint_state").to_pylist()[:50])
    dropped = frames.filter(frames.column("dropped"))
    assert dropped.column("rgb_uri").null_count == dropped.num_rows


def test_clean_batches_pass_the_frame_gate():
    for batch_id, day in (("b01", dt.date(2026, 9, 1)), ("b03", dt.date(2026, 9, 3))):
        _, frames = generate.generate_batch(batch_id, day)
        report = run_checks(frames, _frame_checks())
        assert report.passed, report.summary()


def test_each_injected_fault_trips_its_check():
    episodes, frames = generate.generate_batch("b02", dt.date(2026, 9, 2), faulty=True)
    results = {result.name: result for result in run_checks(frames, _frame_checks())}
    assert not results["frame timestamps rise"].passed
    assert results["frame timestamps rise"].metrics["sample"][0]["group"] == "b02-h07-00"
    assert not results["rgb/depth sync < 20 ms"].passed
    assert results["rgb/depth sync < 20 ms"].metrics["max_skew_ms"] > 50
    assert not results["batch frame-drop rate <= 1%"].passed
    worst = results["episode frame-drop rate <= 5%"].metrics["sample"]
    assert {item["group"].split("-")[1] for item in worst} == {"h06"}
    assert results["no stream gap > 100 ms"].metrics["sample"][0]["group"] == "b02-h02-01"
    assert results["unique(episode_id, frame_idx)"].passed
    assert "dance" in episodes.column("task").to_pylist()


def test_write_batch_and_the_generator_cli(tmp_path, capsys):
    assert generate.main(["--out", str(tmp_path), "--batch", "b09", "--episodes-per-robot", "1", "--faulty"]) == 0
    assert "Wrote 8 episodes" in capsys.readouterr().out
    assert pq.read_table(tmp_path / "episodes.parquet").num_rows == 8
    assert pq.read_table(tmp_path / "frames.parquet").column("batch_id").to_pylist()[0] == "b09"


# --------------------------------------------------------------------------- configs and queries


@pytest.mark.parametrize("path", CONFIGS, ids=lambda path: path.name)
def test_configs_load_with_known_checks(path):
    config = Config.from_json(path)
    assert config.quality["on_failure"] == "fail"
    assert len(checks_from_config(config.quality["checks"])) == len(config.quality["checks"])
    assert config.target["catalog"] == {"identifier": "robots", "warehouse_path": "warehouse"}


def test_bronze_configs_are_config_driven_parquet_pipelines():
    for name in ("bronze_episodes", "bronze_frames"):
        config = Config.from_json(EXAMPLE / "configs" / f"{name}.json")
        assert get_pipeline_class(config.source["format"], config.target["format"]).__name__ == "ParquetToIceberg"


def test_silver_frames_gate_uses_every_temporal_check():
    kinds = {check.check_type for check in _frame_checks()}
    assert {"monotonic", "max_skew", "rate_below", "max_gap"} <= kinds


def test_named_queries():
    queries = run.load_queries()
    assert set(run.RESEARCH_QUERIES) | {"gold_episode_stats", "gold_robot_daily"} <= set(queries)
    assert all(not sql.rstrip().endswith(";") for sql in queries.values())


def test_the_example_does_not_use_the_scanned_config_folder_name():
    # tests/test_examples.py checks every examples/**/config/*.json against the built-in routes.
    assert not (EXAMPLE / "config").exists()


# --------------------------------------------------------------------------- the walkthrough


def test_runs_offline_in_under_a_minute(walkthrough):
    _, summary = walkthrough
    assert summary["duration_s"] < 60


def test_the_faulty_batch_is_blocked_and_the_rest_promoted(walkthrough):
    workdir, summary = walkthrough
    batches = summary["batches"]
    assert [batch for batch, info in batches.items() if info["promoted"]] == ["b01", "b03"]
    assert set(batches["b02"]["failed_checks"]) == {
        "accepted_values(task)", "frame timestamps rise", "rgb/depth sync < 20 ms",
        "batch frame-drop rate <= 1%", "episode frame-drop rate <= 5%", "no stream gap > 100 ms",
    }
    assert batches["b01"]["failed_checks"] == [] and batches["b03"]["failed_checks"] == []
    tables = summary["tables"]
    assert tables["bronze_episodes"] == 3 * 48, "bronze keeps every upload, faults included"
    assert tables["silver_episodes"] == 2 * 48
    assert tables["silver_frames"] == batches["b01"]["frames"] + batches["b03"]["frames"]
    assert tables["gold_episode_stats"] == 96
    silver = Iceberg("silver_episodes", {"identifier": "robots", "warehouse_path": str(workdir / "warehouse")})
    assert set(silver.get(selected_fields=["batch_id"]).column("batch_id").to_pylist()) == {"b01", "b03"}


def test_episodes_are_partitioned_by_day_and_robot_bucket(walkthrough):
    workdir, _ = walkthrough
    for name in ("bronze_episodes", "silver_episodes", "gold_episode_stats"):
        table = Iceberg(name, {"identifier": "robots", "warehouse_path": str(workdir / "warehouse")}).table()
        assert [str(field.transform) for field in table.spec().fields] == ["day", "bucket[8]"], name
    bronze = Iceberg("bronze_episodes", {"identifier": "robots", "warehouse_path": str(workdir / "warehouse")})
    assert bronze.table().inspect.partitions().num_rows > 3  # 3 days x several robot buckets


def test_the_pinned_split_is_reproducible(walkthrough):
    workdir, summary = walkthrough
    warehouse = workdir / "warehouse"
    datasets = summary["datasets"]
    assert set(datasets) == {"humanoid_train", "humanoid_eval", "humanoid_frames"}
    assert all(item["v1_identical_after_later_writes"] for item in datasets.values())
    assert datasets["humanoid_train"]["latest_version"] == 2
    assert datasets["humanoid_train"]["latest_rows"] > datasets["humanoid_train"]["v1_rows"]
    assert list_datasets(warehouse=warehouse) == ["humanoid_eval", "humanoid_frames", "humanoid_train"]
    v1, v2 = list_versions("humanoid_train", warehouse=warehouse)
    train = load(v1)
    assert train.num_rows == datasets["humanoid_train"]["v1_rows"]
    assert "h08" not in train.column("robot_id").to_pylist(), "h08 is held out"
    assert set(load(get_version("humanoid_eval", warehouse=warehouse)).column("robot_id").to_pylist()) == {"h08"}
    assert v1.properties == {"split": "train", "held_out_robot": "h08"}
    assert v1.tag == "ldp_ds_humanoid_train_v1"
    assert v2.snapshot_id != v1.snapshot_id


def test_exports(walkthrough):
    workdir, summary = walkthrough
    parquet = pq.read_table(workdir / "exports" / "humanoid_train_v1.parquet")
    assert parquet.num_rows == summary["datasets"]["humanoid_train"]["v1_rows"]
    assert json.loads(parquet.schema.metadata[b"ldp.dataset"])["name"] == "humanoid_train"
    lines = (workdir / "exports" / "humanoid_eval_v1.jsonl").read_text().splitlines()
    assert len(lines) == summary["exports"]["humanoid_eval_v1.jsonl"]
    assert json.loads(lines[0])["robot_id"] == "h08"


def test_research_queries_find_the_injected_faults(walkthrough):
    _, summary = walkthrough
    queries = summary["queries"]
    drops = {row["robot_id"]: row for row in queries["frame_drop_by_robot"]}
    assert drops["h06"]["b02"] > 0.1
    assert all(row["b01"] < 0.02 and row["b03"] < 0.02 for row in drops.values())
    sync = {row["robot_id"]: row for row in queries["clips_in_sync"]}
    assert sync["h03"]["clips_in_sync"] == 12 and sync["h03"]["clips"] == 18
    assert all(row["share_in_sync"] == 1 for robot, row in sync.items() if robot != "h03")
    tasks = {row["task"]: row for row in queries["success_by_task"]}
    assert set(tasks) == set(generate.TASKS)
    assert all(0 <= row["success_rate"] <= 1 for row in tasks.values())
    robots = [row["robot_id"] for row in queries["success_by_robot"]]
    assert robots[0] == "h05", "h05's worn gripper makes it the weakest robot"
    split = queries["training_split"]
    assert sum(row["episodes"] for row in split) == summary["datasets"]["humanoid_train"]["v1_rows"]


def test_the_cli_reads_the_workdir_configs(walkthrough, capsys):
    from local_data_platform.cli import main

    workdir, _ = walkthrough
    assert main(["snapshots", str(workdir / "gold_episode_stats.json")]) == 0
    assert "robots.gold_episode_stats" in capsys.readouterr().out
    assert main(["query", str(workdir / "silver_frames.json"),
                 "SELECT count(DISTINCT episode_id) AS n FROM silver_frames"]) == 0
    assert "96" in capsys.readouterr().out


def test_rerun_in_the_same_workdir_is_fresh_and_identical(walkthrough):
    workdir, summary = walkthrough
    again = run.run_robotics(workdir, quiet=True)
    assert again["tables"] == summary["tables"]
    assert again["queries"] == summary["queries"]
    assert again["batches"] == summary["batches"]
    assert [v.version for v in list_versions("humanoid_train", warehouse=workdir / "warehouse")] == [1, 2]


def test_a_foreign_workdir_is_refused(tmp_path):
    (tmp_path / "precious.txt").write_text("keep me")
    with pytest.raises(ValueError, match="not created by this example"):
        run.prepare_workdir(tmp_path)
    assert (tmp_path / "precious.txt").read_text() == "keep me"
    assert run.main(["--workdir", str(tmp_path), "--quiet"]) == 1


def test_main_prints_the_narrative(tmp_path, capsys):
    assert run.main(["--workdir", str(tmp_path / "work"), "--episodes-per-robot", "2"]) == 0
    out = capsys.readouterr().out
    assert "BLOCKED by the quality gate" in out
    assert "identical to the day they were pinned: True" in out
    assert "Summary" in out


# --------------------------------------------------------------------------- Spark (opt-in)


@pytest.mark.spark
def test_scala_spark_per_robot_aggregate(walkthrough):
    if os.environ.get("LDP_RUN_SPARK") != "1":
        pytest.skip("set LDP_RUN_SPARK=1 to run the Scala Spark aggregate (needs scala-cli; downloads Spark once)")
    from local_data_platform.engine.spark import find_scala_cli
    from local_data_platform.exceptions import EngineNotFound

    try:
        find_scala_cli()
    except EngineNotFound as error:
        pytest.skip(str(error))
    workdir, _ = walkthrough
    config = Config.from_json(workdir / "gold_episode_stats.json")
    tables = {"gold_episode_stats": run.iceberg_from_config(config, "target")}
    result = run.spark_aggregate(workdir, tables)
    rows = result["rows"]
    assert rows.num_rows == len(generate.ROBOTS)
    assert rows.column("robot_id").to_pylist() == list(generate.ROBOTS)
    assert sum(rows.column("episodes").to_pylist()) == 96
    assert all(0 <= float(rate) <= 1 for rate in rows.column("success_rate").to_pylist())
    assert result["result"]["target_rows"] == "8"
