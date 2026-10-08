"""Humanoid robot episodes on a local Iceberg lakehouse: bronze, silver, gold, and a pinned training split.

Three upload batches from a fleet of eight humanoids go through the platform, offline, in seconds:

1. **Land and ingest (bronze).** Each batch lands as Parquet and the config-driven
   ``ParquetToIceberg`` pipeline appends it to ``bronze_episodes`` (partitioned by
   ``day(start_ts)`` and ``bucket(8, robot_id)``) and ``bronze_frames``. Bronze keeps everything
   that was uploaded, faults included, as the audit trail.
2. **Gate and promote (silver).** Promotion runs temporal quality checks (frame timestamps rise,
   RGB/depth sync under 20 ms, frame-drop rate, no stream gaps) plus task and robot labels. The
   gate is all-or-nothing per batch: both silver tables are validated before either is written.
   Batch ``b02`` carries injected sensor faults and is blocked; nothing of it reaches silver.
3. **Build gold.** DuckDB SQL (``queries.sql``) over silver builds ``gold_episode_stats`` and
   ``gold_robot_daily``, written through quality-checked pipelines.
4. **Pin a training split.** After day 1, ``humanoid_train`` (clean, successful episodes; robot
   ``h08`` held out as ``humanoid_eval``) and ``humanoid_frames`` are pinned as dataset versions.
   Day 3's data changes gold, yet the pinned versions still load the identical rows.
5. **Research.** DuckDB answers the questions a robot-learning team asks: success rate by task and
   robot, frame-drop rate per robot and batch, clips whose cameras stayed in sync.

Run it from anywhere::

    python examples/robot_episodes/run.py                  # workdir: examples/robot_episodes/data
    python examples/robot_episodes/run.py --workdir /tmp/robots --spark   # + the Scala Spark aggregate

``--spark`` also runs ``spark/robot_aggregate.scala`` with scala-cli (JDK 17 and the Maven jars are
fetched on first use), a per-robot aggregate written by Spark to ``gold_robot_summary_spark``.
"""

import argparse
import datetime as dt
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.compute as pc

from local_data_platform import Config
from local_data_platform.cli import format_table
from local_data_platform.datasets import export, list_versions, load, pin
from local_data_platform.engine.duckdb import DuckDBEngine
from local_data_platform.exceptions import DataQualityError
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.pipeline import Pipeline
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline

HERE = Path(__file__).resolve().parent
CONFIG_DIR = HERE / "configs"
QUERIES_PATH = HERE / "queries.sql"
SPARK_SCRIPT = HERE / "spark" / "robot_aggregate.scala"
DEFAULT_WORKDIR = HERE / "data"
MARKER = ".robot_episodes_workdir"
WORKDIR_FOLDERS = ("landing", "warehouse", "exports")

TABLES = ("bronze_episodes", "bronze_frames", "silver_episodes", "silver_frames", "gold_episode_stats",
          "gold_robot_daily")
BATCHES = (
    ("b01", dt.date(2026, 9, 1), False),
    ("b02", dt.date(2026, 9, 2), True),   # injected sensor faults: the gate must block it
    ("b03", dt.date(2026, 9, 3), False),
)
RESEARCH_QUERIES = ("success_by_task", "success_by_robot", "frame_drop_by_robot", "clips_in_sync",
                    "training_split")
TRAIN_FILTER = "clean = true AND success = true AND robot_id != 'h08'"
EVAL_FILTER = "clean = true AND robot_id = 'h08'"
SPLIT_FIELDS = ["episode_id", "robot_id", "task", "operator", "start_ts", "duration_s", "frame_count"]
FRAME_FIELDS = ["episode_id", "frame_idx", "rgb_ts", "joint_state", "rgb_uri", "depth_uri"]
SPARK_TABLE = "gold_robot_summary_spark"


def _load_generator():
    spec = importlib.util.spec_from_file_location("robot_episodes_generate", HERE / "generate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


generate = _load_generator()


# --------------------------------------------------------------------------- building blocks


def load_queries(path: Path = QUERIES_PATH) -> dict[str, str]:
    """Read the named queries of ``queries.sql``: each starts at a ``-- name: <name>`` line."""
    queries: dict[str, str] = {}
    name, lines = None, []
    for line in Path(path).read_text().splitlines() + ["-- name: __end__"]:
        match = re.fullmatch(r"--\s*name:\s*(\w+)\s*", line)
        if match:
            if name is not None:
                queries[name] = "\n".join(lines).strip().rstrip(";")
            name, lines = match.group(1), []
        elif name is not None:
            lines.append(line)
    return queries


class BronzeBatch:
    """A pipeline source: one upload batch of a bronze Iceberg table."""

    def __init__(self, table: Any, batch_id: str):
        self.table = table
        self.batch_id = batch_id

    def get(self) -> pa.Table:
        return self.table.get(row_filter=f"batch_id = '{self.batch_id}'")

    def __repr__(self) -> str:
        return f"BronzeBatch({self.table.identifier}, {self.batch_id})"


class SqlSource:
    """A pipeline source: the result of a DuckDB query."""

    def __init__(self, engine: DuckDBEngine, sql: str, name: str):
        self.engine = engine
        self.sql = sql
        self.name = name

    def get(self) -> pa.Table:
        return self.engine.query(self.sql)

    def __repr__(self) -> str:
        return f"SqlSource({self.name})"


def with_duration(episodes: pa.Table) -> pa.Table:
    """Silver transform: add ``duration_s`` from ``start_ts`` and ``end_ts``."""
    micros = pc.subtract(pc.cast(episodes["end_ts"], pa.int64()), pc.cast(episodes["start_ts"], pa.int64()))
    return episodes.append_column("duration_s", pc.divide(pc.cast(micros, pa.float64()), 1e6))


def with_robot_and_skew(episodes: pa.Table) -> Callable[[pa.Table], pa.Table]:
    """Silver transform for frames: add the episode's ``robot_id`` and the signed RGB/depth ``skew_ms``."""
    episode_ids = episodes["episode_id"].combine_chunks()

    def transform(frames: pa.Table) -> pa.Table:
        robot = pc.take(episodes["robot_id"], pc.index_in(frames["episode_id"], value_set=episode_ids))
        skew_us = pc.subtract(pc.cast(frames["depth_ts"], pa.int64()), pc.cast(frames["rgb_ts"], pa.int64()))
        return (frames.append_column("robot_id", robot)
                .append_column("skew_ms", pc.divide(pc.cast(skew_us, pa.float64()), 1000.0)))

    return transform


def prepare_workdir(workdir: str | Path) -> Path:
    """Create (or clear) the work folder and copy the configs into it.

    The configs' relative paths (``landing/…``, ``warehouse``) resolve against the folder they are
    in, so copies in the workdir also work with the CLI: ``ldp snapshots <workdir>/silver_frames.json``.

    Raises:
        ValueError: If the folder is not empty and was not made by this example.
    """
    workdir = Path(workdir).expanduser().resolve()
    if workdir.is_dir() and any(workdir.iterdir()):
        if not (workdir / MARKER).is_file():
            raise ValueError(f"{workdir} is not empty and was not created by this example; pass an empty --workdir")
        for folder in WORKDIR_FOLDERS:
            shutil.rmtree(workdir / folder, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / MARKER).write_text("Created by examples/robot_episodes/run.py, which clears this folder on each run.\n")
    for config in sorted(CONFIG_DIR.glob("*.json")):
        shutil.copy2(config, workdir / config.name)
    return workdir


class Narrator:
    """Prints the walkthrough; silent when ``quiet``."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.step = 0

    def section(self, title: str) -> None:
        self.step += 1
        self.say(f"\n== {self.step}. {title}")

    def say(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    def table(self, rows: Any, max_rows: int | None = 12) -> None:
        if not self.quiet:
            print("\n".join("   " + line for line in format_table(rows, max_rows=max_rows, float_digits=None)
                            .splitlines()))


# --------------------------------------------------------------------------- the steps


def ingest_bronze(configs: dict[str, Config]) -> dict[str, Any]:
    """Append the landed batch to bronze with the config-driven Parquet-to-Iceberg pipelines."""
    return {name: create_pipeline(configs[name]).run() for name in ("bronze_episodes", "bronze_frames")}


def promote_to_silver(batch_id: str, configs: dict[str, Config], tables: dict[str, Any]) -> dict[str, Any]:
    """Validate the batch's episodes and frames, then write both to silver, or neither.

    Returns:
        ``{"promoted": bool, "reports": {pipeline: QualityReport}, "rows": {table: rows written}}``.
    """
    bronze_episodes = BronzeBatch(tables["bronze_episodes"], batch_id)
    pipelines = [
        Pipeline(configs["silver_episodes"], source=bronze_episodes, target=tables["silver_episodes"],
                 transforms=[with_duration], name="silver_episodes"),
        Pipeline(configs["silver_frames"], source=BronzeBatch(tables["bronze_frames"], batch_id),
                 target=tables["silver_frames"], transforms=[with_robot_and_skew(bronze_episodes.get())],
                 name="silver_frames"),
    ]
    staged, reports, blocked = [], {}, False
    for pipeline in pipelines:
        df = pipeline.transform(pipeline.extract())
        try:
            reports[pipeline.name] = pipeline.validate(df, {"source_rows": df.num_rows})
        except DataQualityError as error:
            reports[pipeline.name], blocked = error.report, True
            continue
        staged.append((pipeline, df))
    rows = {}
    if not blocked:
        for pipeline, df in staged:
            rows[pipeline.name] = pipeline.write(df).rows_written
    return {"promoted": not blocked, "reports": reports, "rows": rows}


def build_gold(configs: dict[str, Config], tables: dict[str, Any], queries: dict[str, str]) -> dict[str, Any]:
    """Rebuild the gold tables from silver with DuckDB SQL, through quality-checked pipelines."""
    results = {}
    with DuckDBEngine() as duck:
        duck.query("SET TimeZone = 'UTC'")
        for name in ("silver_episodes", "silver_frames"):
            duck.register_iceberg(tables[name], name)
        for name in ("gold_episode_stats", "gold_robot_daily"):
            source = SqlSource(duck, queries[name], name)
            results[name] = Pipeline(configs[name], source=source, target=tables[name], name=name).run()
            duck.register_iceberg(tables[name], name)
    return results


def run_research(tables: dict[str, Any], queries: dict[str, str], split: Any, frames: Any) -> dict[str, pa.Table]:
    """Run the research queries with every table registered, and the pinned split as ``train_*``."""
    with DuckDBEngine() as duck:
        duck.query("SET TimeZone = 'UTC'")
        for name, table in tables.items():
            duck.register_iceberg(table, name)
        duck.register_arrow(load(split), "train_split")
        duck.register_arrow(load(frames), "train_frames")
        return {name: duck.query(queries[name]) for name in RESEARCH_QUERIES}


def spark_aggregate(workdir: Path, tables: dict[str, Any], timeout: float = 1800) -> dict[str, Any]:
    """Run ``spark/robot_aggregate.scala`` with scala-cli: a per-robot aggregate of ``gold_episode_stats``.

    Raises:
        EngineNotFound: If scala-cli is not installed.
        RuntimeError: If the Spark job fails.
    """
    from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
    from local_data_platform.engine.spark import find_scala_cli, spark_catalog_conf

    namespace = tables["gold_episode_stats"].namespace
    warehouse = workdir / "warehouse"
    conf = spark_catalog_conf(namespace, LocalIcebergCatalog.database_path(namespace, warehouse), warehouse)
    # LocalIcebergCatalog names the catalog after the namespace; Spark needs catalog.namespace.table.
    command = [find_scala_cli(), "run", str(SPARK_SCRIPT), "-q", "--suppress-outdated-dependency-warning", "--",
               "--source", f"{namespace}.{namespace}.gold_episode_stats",
               "--target", f"{namespace}.{namespace}.{SPARK_TABLE}"]
    for key, value in conf.items():
        command += ["--conf", f"{key}={value}"]
    started = time.perf_counter()
    done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"Spark job failed ({done.returncode}): {(done.stderr or done.stdout)[-2000:]}")
    line = next((text for text in done.stdout.splitlines() if text.startswith("LDP_SPARK_RESULT ")), "")
    result = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
    summary = Iceberg(SPARK_TABLE, {"identifier": namespace, "warehouse_path": str(warehouse)}).get()
    return {"result": result, "rows": summary.sort_by("robot_id"), "seconds": round(time.perf_counter() - started, 1)}


# --------------------------------------------------------------------------- the walkthrough


def run_robotics(workdir: str | Path = DEFAULT_WORKDIR, *, seed: int = 7, episodes_per_robot: int = 6,
                 spark: bool = False, quiet: bool = False) -> dict[str, Any]:
    """Run the whole example and return a summary of what happened.

    Args:
        workdir: The work folder (created, or cleared if this example made it).
        seed: The generator's random seed.
        episodes_per_robot: Episodes each robot records per batch.
        spark: Also run the Scala Spark aggregate (needs scala-cli).
        quiet: Print nothing.

    Returns:
        A JSON-friendly summary: per batch, per table, per dataset and per query.
    """
    started = time.perf_counter()
    out = Narrator(quiet)
    workdir = prepare_workdir(workdir)
    configs = {name: Config.from_json(workdir / f"{name}.json") for name in TABLES}
    tables = {name: iceberg_from_config(configs[name], "target") for name in TABLES}
    queries = load_queries()
    summary: dict[str, Any] = {"workdir": str(workdir), "batches": {}, "datasets": {}}
    out.say(f"Humanoid robot episodes -> Iceberg lakehouse in {workdir}")
    out.say(f"Fleet: {len(generate.ROBOTS)} humanoids, {len(generate.TASKS)} tasks, {generate.JOINTS} joints, "
            f"{generate.FPS} Hz RGB + depth; {len(BATCHES)} upload batches (seed {seed}).")

    pinned: dict[str, Any] = {}
    for batch_id, day, faulty in BATCHES:
        episodes, frames = generate.generate_batch(batch_id, day, seed=seed, episodes_per_robot=episodes_per_robot,
                                                   faulty=faulty)
        generate.write_batch(workdir / "landing", episodes, frames)
        out.section(f"Batch {batch_id} ({day}): {episodes.num_rows} episodes, {frames.num_rows} frames"
                    + (" - with injected sensor faults" if faulty else ""))
        bronze = ingest_bronze(configs)
        out.say(f"   bronze: appended {bronze['bronze_episodes'].rows_written} episodes and "
                f"{bronze['bronze_frames'].rows_written} frames (raw, as uploaded)")
        gate = promote_to_silver(batch_id, configs, tables)
        failed = [result.name for report in gate["reports"].values() for result in report.failures]
        batch = {"episodes": episodes.num_rows, "frames": frames.num_rows, "faulty": faulty,
                 "promoted": gate["promoted"], "failed_checks": failed}
        summary["batches"][batch_id] = batch
        if not gate["promoted"]:
            out.say(f"   silver: BLOCKED by the quality gate - {len(failed)} checks failed, nothing written:")
            for report in gate["reports"].values():
                for result in report.failures:
                    out.say(f"     x {result.name}: {result.details}")
            out.say(f"   {batch_id} stays in bronze for forensics; silver and gold are unchanged.")
            continue
        checks = sum(len(report) for report in gate["reports"].values())
        out.say(f"   silver: all {checks} quality checks passed; promoted {gate['rows']['silver_episodes']} episodes "
                f"and {gate['rows']['silver_frames']} frames")
        gold = build_gold(configs, tables, queries)
        stats = gold["gold_episode_stats"].write_result
        batch["gold_episode_rows"] = stats.rows_after
        out.say(f"   gold: gold_episode_stats now {stats.rows_after} episodes, gold_robot_daily "
                f"{gold['gold_robot_daily'].write_result.rows_after} robot-days (snapshot {stats.snapshot_id})")

        if not pinned:
            out.section("Pin the training split")
            pinned["humanoid_train"] = pin(tables["gold_episode_stats"], "humanoid_train", TRAIN_FILTER, SPLIT_FIELDS,
                                           properties={"split": "train", "held_out_robot": "h08"})
            pinned["humanoid_eval"] = pin(tables["gold_episode_stats"], "humanoid_eval", EVAL_FILTER, SPLIT_FIELDS,
                                          properties={"split": "eval"})
            pinned["humanoid_frames"] = pin(tables["silver_frames"], "humanoid_frames", "dropped = false",
                                            FRAME_FIELDS, properties={"split": "train+eval"})
            pinned_rows = {name: load(version) for name, version in pinned.items()}
            for version in pinned.values():
                out.say(f"   {version.ref}: {version.row_count} rows of {version.table_identifier} "
                        f"@ snapshot {version.snapshot_id}, filter {version.row_filter!r}, tag {version.tag}")
            out.say(f"   manifests in {workdir / 'warehouse' / '.ldp' / 'datasets'}")

    out.section("Reproducibility: the pinned versions after more data landed")
    for name, version in pinned.items():
        again = load(version)
        identical = again.equals(pinned_rows[name])
        summary["datasets"][name] = {"v1_rows": version.row_count, "v1_snapshot": version.snapshot_id,
                                     "v1_identical_after_later_writes": identical, "tag": version.tag}
        out.say(f"   {version.ref}: {again.num_rows} rows, identical to the day they were pinned: {identical}")
    current = tables["gold_episode_stats"]
    then = current.get(snapshot_id=pinned["humanoid_train"].snapshot_id).num_rows
    out.say(f"   gold_episode_stats has {current.row_count()} rows now and had {then} at the pinned snapshot")
    latest = pin(current, "humanoid_train", TRAIN_FILTER, SPLIT_FIELDS,
                 properties={"split": "train", "held_out_robot": "h08"})
    summary["datasets"]["humanoid_train"]["latest_version"] = latest.version
    summary["datasets"]["humanoid_train"]["latest_rows"] = latest.row_count
    out.say(f"   pinned {latest.ref} with day 3 included: {latest.row_count} rows")
    out.table([{"version": v.ref, "snapshot_id": v.snapshot_id, "rows": v.row_count,
                "created_at_utc": v.created_at.strftime("%H:%M:%S")}
               for v in list_versions("humanoid_train", warehouse=workdir / "warehouse")])
    exports = workdir / "exports"
    summary["exports"] = {
        "humanoid_train_v1.parquet": export(pinned["humanoid_train"], exports / "humanoid_train_v1.parquet"),
        "humanoid_eval_v1.jsonl": export(pinned["humanoid_eval"], exports / "humanoid_eval_v1.jsonl", format="jsonl"),
    }
    out.say(f"   exported {', '.join(f'{name} ({rows} rows)' for name, rows in summary['exports'].items())} "
            f"to {exports}")

    out.section("Research queries (DuckDB over Iceberg; queries.sql)")
    results = run_research(tables, queries, pinned["humanoid_train"], pinned["humanoid_frames"])
    titles = {
        "success_by_task": "Success rate by task (gold)",
        "success_by_robot": "Success rate by robot (gold); h05 has a worn gripper",
        "frame_drop_by_robot": "Frame-drop rate by robot and batch (bronze, quarantined b02 included)",
        "clips_in_sync": "Clips with RGB/depth sync skew < 20 ms (bronze)",
        "training_split": "What the pinned training split humanoid_train@v1 holds",
    }
    summary["queries"] = {}
    for name, result in results.items():
        summary["queries"][name] = result.to_pylist()
        out.say(f"\n   -- {titles[name]}")
        out.table(result)

    if spark:
        out.section("Scala Spark: per-robot aggregate (spark/robot_aggregate.scala)")
        spark_result = spark_aggregate(workdir, tables)
        summary["spark"] = {"target_rows": spark_result["rows"].num_rows, "seconds": spark_result["seconds"]}
        out.say(f"   Spark wrote {spark_result['result'].get('target', SPARK_TABLE)} in {spark_result['seconds']}s; "
                "read back with pyiceberg:")
        out.table(spark_result["rows"])

    summary["tables"] = {name: table.row_count() for name, table in tables.items()}
    summary["duration_s"] = round(time.perf_counter() - started, 2)
    out.section("Summary")
    out.table([{"table": f"{tables[name].identifier}", "rows": rows,
                "snapshots": len(tables[name].snapshots())} for name, rows in summary["tables"].items()])
    promoted = [batch for batch, info in summary["batches"].items() if info["promoted"]]
    blocked = [batch for batch, info in summary["batches"].items() if not info["promoted"]]
    out.say(f"   batches promoted: {', '.join(promoted)}; blocked by the gate: {', '.join(blocked) or 'none'}")
    out.say(f"   pinned datasets reproducible: "
            f"{all(item['v1_identical_after_later_writes'] for item in summary['datasets'].values())}")
    out.say(f"   finished in {summary['duration_s']}s. Try: ldp snapshots {workdir / 'gold_episode_stats.json'}")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Humanoid robot episodes: bronze -> silver -> gold on Iceberg, "
                                                 "temporal quality gates, a pinned training split, DuckDB research.")
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR),
                        help="work folder; cleared on each run if this example created it (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=7, help="random seed of the synthetic fleet (default 7)")
    parser.add_argument("--episodes-per-robot", type=int, default=6, help="episodes per robot per batch (default 6)")
    parser.add_argument("--spark", action="store_true",
                        help="also run the Scala Spark per-robot aggregate (needs scala-cli; downloads Spark once)")
    parser.add_argument("--quiet", action="store_true", help="print only the final line")
    args = parser.parse_args(argv)
    try:
        summary = run_robotics(args.workdir, seed=args.seed, episodes_per_robot=args.episodes_per_robot,
                               spark=args.spark, quiet=args.quiet)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.quiet:
        print(f"robot_episodes: {summary['tables']} in {summary['duration_s']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
