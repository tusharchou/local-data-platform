# Robotics data

Robot-learning teams record *episodes* (one attempt at a task) made of *frames* (one sensor tick:
camera images, depth, joint positions). Two things decide whether that data is worth training on:

1. **Is it physically consistent?** Clocks that jump backwards, an RGB and a depth camera that drift
   apart, dropped frames and holes in a stream all produce demonstrations a policy can't learn from.
   You want to catch them *before* a batch reaches the tables researchers read.
2. **Can you get the same training set back?** A model trained last month must be traceable to the
   exact rows it saw, even though the tables have been appended to, rewritten and re-partitioned since.

local-data-platform answers the first with temporal quality checks that run before a write, and the
second with pinned dataset versions on top of Iceberg snapshots. The runnable walkthrough is
`examples/robot_episodes/` (`make demo-robotics`, or `python examples/robot_episodes/run.py`; about
a second, offline).

## Temporal quality checks

Four checks in `local_data_platform.quality.temporal` join the checks from the hardening contract.
Like them, they run on a `pyarrow.Table`, never raise because of the data, are vectorised with
`pyarrow.compute`, and are available in configs by name.

| Check | Config | Fails when |
|---|---|---|
| `Monotonic(column, group_by=None, strict=False, order_by=None)` | `monotonic` | a value is smaller than the one before it (`strict`: not greater), within each group |
| `MaxSkew(column_a, column_b, max_ms, unit="ms")` | `max_skew` | two time columns are more than `max_ms` apart on a row |
| `RateBelow(predicate_column, max_rate, group_by=None)` | `rate_below` | the share of true values is over `max_rate` (per group, with `group_by`) |
| `MaxGap(column, max_ms, group_by=None, unit="ms")` | `max_gap` | consecutive values, in time order, are more than `max_ms` apart, within each group |

A frame gate from the example:

```json
"quality": {"on_failure": "fail", "checks": [
  {"check": "monotonic", "column": "rgb_ts", "group_by": "episode_id", "order_by": "frame_idx", "strict": true},
  {"check": "max_skew", "column_a": "rgb_ts", "column_b": "depth_ts", "max_ms": 20},
  {"check": "rate_below", "predicate_column": "dropped", "max_rate": 0.01},
  {"check": "rate_below", "predicate_column": "dropped", "max_rate": 0.05, "group_by": "episode_id"},
  {"check": "max_gap", "column": "rgb_ts", "max_ms": 100, "group_by": "episode_id"}
]}
```

The same checks in Python:

```python
from local_data_platform.quality import MaxGap, MaxSkew, Monotonic, RateBelow, run_checks

report = run_checks(frames, [
    Monotonic("rgb_ts", group_by="episode_id", order_by="frame_idx", strict=True),
    MaxSkew("rgb_ts", "depth_ts", max_ms=20),
    RateBelow("dropped", max_rate=0.05, group_by="episode_id"),
    MaxGap("rgb_ts", max_ms=100, group_by="episode_id"),
])
print(report.summary())
```

How they treat the data:

- **Time columns.** `MaxSkew` and `MaxGap` take timestamps of any unit, with or without a time zone
  (a naive timestamp is read as UTC), dates, durations and numbers. Numbers are read in `unit`, so
  epoch nanoseconds from a robot log work with `unit="ns"`. Two columns of different units are
  compared exactly; a timestamp compared with a number fails the check.
- **Order.** `Monotonic` compares rows in table order, or in `order_by` order when given: use
  `order_by="frame_idx"` when rows may arrive shuffled. `MaxGap` sorts by the time column itself.
- **Nulls.** A null (or NaN) value is skipped and counted in `metrics["skipped_rows"]`; pair the
  checks with `not_null` to forbid nulls. `RateBelow` divides by the non-null values. Null group keys
  form one group, as in SQL `GROUP BY`.
- **Results.** `failing_rows` counts the offending rows (for `MaxGap`, the rows right after a gap).
  `metrics` has the measurements (`max_skew_ms`, `mean_skew_ms`, `max_gap_ms`, `rate`, `groups`) and a
  `sample` of up to five offenders with their row index and group, so a blocked batch says where to
  look.

## Gate a whole batch

A batch of episodes and its frames should be promoted together or not at all. `Pipeline` exposes its
steps, so validate every table first and write only when all passed:

```python
from local_data_platform.exceptions import DataQualityError

staged, blocked = [], False
for pipeline in (silver_episodes, silver_frames):
    df = pipeline.transform(pipeline.extract())
    try:
        pipeline.validate(df, {"source_rows": df.num_rows})
    except DataQualityError as error:
        blocked = True
        print(error.report.summary())
        continue
    staged.append((pipeline, df))
if not blocked:
    for pipeline, df in staged:
        pipeline.write(df)
```

In the example, bronze keeps every upload (the audit trail), and a faulty batch stops at the silver
gate: nothing of it reaches silver or gold.

## Reproducible datasets

`local_data_platform.datasets` pins what to read (a table, one of its snapshots, a row filter and a
column list) as a numbered version of a named dataset:

```python
from local_data_platform.datasets import export, get_version, list_versions, load, pin

train = pin(episode_stats, "humanoid_train",
            row_filter="clean = true AND success = true AND robot_id != 'h08'",
            selected_fields=["episode_id", "robot_id", "task", "start_ts"],
            properties={"split": "train"})
rows = load(train)                                   # identical rows, any time later
export(train, "exports/train.parquet")               # or format="jsonl"
list_versions("humanoid_train", warehouse="warehouse")
get_version("humanoid_train@v1", warehouse="warehouse")
```

| Function | Does |
|---|---|
| `pin(iceberg, name, row_filter=None, selected_fields=None, *, snapshot_id=None, properties=None, tag=True, warehouse=None)` | Writes version N+1 of `name` and returns its `DatasetVersion` |
| `load(version, *, catalog=None, verify=True)` | Reads the pinned rows as a `pyarrow.Table` |
| `list_versions(name, *, warehouse=None)` / `list_datasets(*, warehouse=None)` | Lists what is pinned |
| `get_version(name, version=None, *, warehouse=None)` | One version (the latest by default); `name` may be `"name@vN"` |
| `export(version, uri, format="parquet", *, catalog=None, base_dir=None)` | Writes the rows to Parquet or JSONL; returns the row count |

A `DatasetVersion` holds `name`, `version`, `table_identifier`, `snapshot_id`, `row_filter`,
`selected_fields`, `row_count`, `schema_fingerprint` and `created_at`, plus the catalog spec, the
table's metadata file, the tag and your `properties`. It is stored as JSON in
`<warehouse>/.ldp/datasets/<name>/v<NNNNNN>.json`. When a function isn't given a `warehouse`, it uses
the table's local warehouse, then the `LDP_WAREHOUSE` environment variable.

Why the rows can't change:

- **Snapshots are immutable.** Iceberg never rewrites a snapshot's files, and reading a snapshot uses
  that snapshot's schema, so appends, overwrites, upserts and schema changes made later don't show.
- **The snapshot is protected.** `pin` tags it (`ldp_ds_<name>_v<N>`). Snapshot expiry in pyiceberg
  and in Iceberg Java never removes a tagged snapshot. With `tag=False` an expired snapshot makes
  `load` raise `DatasetError` rather than return other rows. To release a version, remove its tag
  (`table.manage_snapshots().remove_tag(tag).commit()`).
- **Loads are verified.** `load` checks the schema fingerprint and the row count against the manifest
  and raises `DatasetError` on a mismatch.
- **The catalog is optional.** `load` reopens the table through the catalog recorded in the manifest
  (secrets are never recorded), and falls back to the metadata file recorded at pin time. Pass
  `catalog=` for a remote catalog whose credentials live in the environment.
- **Exports say where they came from.** A Parquet export carries the manifest in its schema metadata
  under `ldp.dataset`.

Versions are numbered per name, and concurrent `pin` calls get distinct numbers: a manifest is
written to a temporary file and hard-linked into place, which fails if the number is taken.

`ldp datasets pin|list|export` do the same from the command line:

```bash
ldp datasets pin examples/robot_episodes/data/gold_episode_stats.json successes \
    --filter "success = true" --fields episode_id,robot_id,task
ldp datasets list --warehouse examples/robot_episodes/data/warehouse
ldp datasets export humanoid_train@v1 /tmp/train.jsonl --format jsonl --warehouse examples/robot_episodes/data/warehouse
```

`pin` takes a dataset config and pins its Iceberg target (else its source); `list` and `export` find
the manifests through `--config CONFIG` or `--warehouse DIR`.

## The walkthrough

`examples/robot_episodes/run.py` puts it together with three upload batches from eight synthetic
humanoids (28 joints, 30 Hz RGB and depth):

1. Batches land as Parquet and the config-driven `ParquetToIceberg` pipeline appends them to bronze.
   Episodes are partitioned by `day(start_ts)` and `bucket(8, robot_id)`.
2. The silver gate runs the frame checks above plus task and robot labels. Batch `b02` has five
   injected faults (dropped frames on `h06`, depth-clock drift on `h03`, a clock step on `h07`, a
   recorder stall on `h02`, an unknown task label) and is blocked.
3. DuckDB SQL builds `gold_episode_stats` and `gold_robot_daily` from silver.
4. `humanoid_train`, `humanoid_eval` (robot `h08` held out) and `humanoid_frames` are pinned after
   day 1, still load identically after day 3 rewrites gold, and `humanoid_train@v2` adds day 3.
5. Research queries in `queries.sql`: success rate by task and by robot, frame-drop rate by robot and
   batch, clips whose cameras stayed within 20 ms, and what the training split holds.
6. With `--spark`, a Scala Spark job writes a per-robot aggregate to the same catalog (see
   [Spark](spark.md)).

The example's README lists every table, check and fault.

## Limits

- The checks look at one batch at a time. A clock that drifts slowly across batches, or a frame
  index that restarts in a later upload of the same episode, needs a check over the table instead.
- `load` returns the whole version in memory. For versions larger than memory, read the pinned
  snapshot in batches with pyiceberg: `table.scan(snapshot_id=v.snapshot_id, row_filter=v.row_filter,
  selected_fields=v.selected_fields).to_arrow_batch_reader()`.
- The row filter is stored as a string, so `pin` takes pyiceberg filter strings, not expression
  objects.
- Manifests are written to a local folder. Exports can go to `s3://` and `gs://` through
  `local_data_platform.fs`.
