# Humanoid robot episodes

A robot-learning data pipeline on a laptop: teleoperated demonstrations from a fleet of eight
humanoids land as Parquet, flow through bronze, silver and gold Iceberg tables, are gated by
temporal quality checks, and end up as a pinned, reproducible training split that DuckDB (and,
optionally, Spark) can query. Everything is synthetic and deterministic, runs offline, and takes
about a second.

```bash
pip install -e ".[duckdb]"                      # from the repo root
python examples/robot_episodes/run.py           # writes to examples/robot_episodes/data/ (git-ignored)
python examples/robot_episodes/run.py --workdir /tmp/robots --spark   # plus the Scala Spark aggregate
```

`--spark` needs [scala-cli](https://scala-cli.virtuslab.org); the first run downloads a JDK, Spark
4.1 and Iceberg 1.12, later runs take about 6 seconds. See [docs/spark.md](../../docs/spark.md).

## What happens

| Step | What the platform does | Library API |
|---|---|---|
| 1. Land | `generate.py` writes an upload batch to `landing/episodes.parquet` and `landing/frames.parquet` | pyarrow |
| 2. Bronze | Each batch is appended to `bronze_episodes` and `bronze_frames`, with structural checks (schema, keys) | `create_pipeline(Config.from_json(...)).run()` (`ParquetToIceberg`) |
| 3. Silver gate | Temporal and label checks run on the batch's episodes **and** frames; both are written only if both pass | `Pipeline(config, source=..., target=...)`: `extract`, `transform`, `validate`, `write` |
| 4. Gold | `gold_episode_stats` (one row per episode) and `gold_robot_daily`, built with DuckDB SQL from `queries.sql`, written through quality-checked pipelines (`overwrite`) | `DuckDBEngine`, `Pipeline.run()` |
| 5. Pin | After day 1, `humanoid_train`, `humanoid_eval` (robot `h08` held out) and `humanoid_frames` are pinned | `datasets.pin` |
| 6. Reproduce | After day 3 lands and gold is rewritten, the pinned versions still load the identical rows; `humanoid_train@v2` adds day 3 | `datasets.load`, `list_versions`, `export` |
| 7. Research | Success rate by task and robot, frame-drop rate by robot and batch, clips with sync skew under 20 ms | `DuckDBEngine.register_iceberg`, `queries.sql` |
| 8. Spark (optional) | A per-robot aggregate written by Spark to `gold_robot_summary_spark`, read back with pyiceberg | `spark/robot_aggregate.scala`, `spark_catalog_conf` |

Three batches arrive: `b01` and `b03` are clean, `b02` carries injected sensor faults. The run prints
each step; the gate's output for `b02` looks like this:

```text
== 3. Batch b02 (2026-09-02): 48 episodes, 6536 frames - with injected sensor faults
   bronze: appended 48 episodes and 6536 frames (raw, as uploaded)
   silver: BLOCKED by the quality gate - 6 checks failed, nothing written:
     x accepted_values(task): 1 of 48 rows have a task outside [...]; e.g. ['dance']
     x frame timestamps rise: 1 of 6536 rows break the strictly increasing order of rgb_ts ...
     x rgb/depth sync < 20 ms: 636 of 6536 rows have |rgb_ts - depth_ts| > 20 ms (max 76.744 ms) ...
     x batch frame-drop rate <= 1%: dropped rate 1.97% (129 of 6536) > 1.00%
     x episode frame-drop rate <= 5%: 6 of 48 episode_id groups have a dropped rate over 5.00% ...
     x no stream gap > 100 ms: 1 gap(s) in rgb_ts over 100 ms within each episode_id (max 534.458 ms) ...
   b02 stays in bronze for forensics; silver and gold are unchanged.
```

## Data

**Episodes** (one per teleoperated attempt): `batch_id`, `episode_id`, `robot_id`, `task`, `operator`,
`start_ts`, `end_ts`, `success`, `frame_count`.

**Frames** (30 Hz): `batch_id`, `episode_id`, `frame_idx`, `rgb_ts`, `depth_ts`, `joint_state`
(28 doubles: two 7-DoF arms, two 6-DoF legs, a 2-DoF neck), `rgb_uri`, `depth_uri` (null when the
frame was dropped) and `dropped`.

| Table | Partitioned by | Written by |
|---|---|---|
| `robots.bronze_episodes` | `day(start_ts)`, `bucket(8, robot_id)` | `configs/bronze_episodes.json`, append |
| `robots.bronze_frames` | `bucket(8, episode_id)` | `configs/bronze_frames.json`, append |
| `robots.silver_episodes` | `day(start_ts)`, `bucket(8, robot_id)` | the silver gate, append; adds `duration_s` |
| `robots.silver_frames` | `bucket(8, episode_id)` | the silver gate, append; adds `robot_id` and `skew_ms` |
| `robots.gold_episode_stats` | `day(start_ts)`, `bucket(8, robot_id)` | `queries.sql` `gold_episode_stats`, overwrite |
| `robots.gold_robot_daily` | - | `queries.sql` `gold_robot_daily`, overwrite |

The faults in `b02`, and the check in `configs/silver_frames.json` or `silver_episodes.json` that
catches each one:

| Fault | Where | Check |
|---|---|---|
| Loose camera cable, 15% of frames dropped | every `h06` episode | `rate_below` on `dropped`, per batch (1%) and per episode (5%) |
| Depth-camera clock drift, skew up to ~77 ms | every `h03` episode | `max_skew` of `rgb_ts` and `depth_ts`, 20 ms |
| NTP step, the clock jumps back 250 ms | `b02-h07-00` | `monotonic` of `rgb_ts` by `episode_id` in `frame_idx` order, strict |
| Recorder stall, 15 frames never written | `b02-h02-01` | `max_gap` of `rgb_ts` by `episode_id`, 100 ms |
| Unknown task label `dance` | `b02-h04-02` | `accepted_values` on `task` |

## The pinned training split

```python
from local_data_platform.datasets import get_version, load

train = get_version("humanoid_train", 1, warehouse="examples/robot_episodes/data/warehouse")
episodes = load(train)       # the same 32 rows every time, whatever was written since
print(train.snapshot_id, train.row_filter, train.tag)
```

Each version is a JSON manifest in `<warehouse>/.ldp/datasets/<name>/`, and its snapshot is tagged
(`ldp_ds_humanoid_train_v1`) so snapshot expiry keeps it. `run.py` also exports
`exports/humanoid_train_v1.parquet` (with the manifest in its schema metadata) and
`exports/humanoid_eval_v1.jsonl`.

## Poke at it

`run.py` copies the configs into the work folder, so the CLI works on them:

```bash
ldp snapshots examples/robot_episodes/data/gold_episode_stats.json
ldp query examples/robot_episodes/data/silver_frames.json \
    "SELECT robot_id, count(*) AS frames, max(abs(skew_ms)) AS worst_skew_ms FROM silver_frames GROUP BY 1"
ldp datasets list --warehouse examples/robot_episodes/data/warehouse
ldp datasets export humanoid_train@v1 /tmp/train.jsonl --format jsonl --warehouse examples/robot_episodes/data/warehouse
```

`ldp run` works on the two bronze configs. The silver and gold configs describe their tables and
quality gates, but they are driven by `run.py`: there is no built-in Iceberg-to-Iceberg pipeline, so
`ldp run` on them reports `PipelineNotFound`.

## Files

| File | What it is |
|---|---|
| `run.py` | The walkthrough; `run_robotics(workdir, seed=7, spark=False, quiet=False)` returns a summary dict |
| `generate.py` | The deterministic generator; `python generate.py --out DIR --batch b02 --faulty` writes one batch |
| `configs/*.json` | Table layouts and quality gates, with paths relative to the work folder |
| `queries.sql` | Named DuckDB queries: the gold builds and the research questions |
| `spark/robot_aggregate.scala` | The optional Scala Spark per-robot aggregate |

The work folder is cleared at the start of each run, but only if this example created it (it holds a
`.robot_episodes_workdir` marker); a non-empty folder without the marker is refused.
