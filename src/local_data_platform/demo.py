"""An offline, end-to-end tour of local-data-platform: ``ldp demo`` or :func:`run_demo`.

The demo generates synthetic NYC-taxi-like rides from a fixed seed, so every run
produces the same data and the same numbers. Using only the public API, it walks
through:

1. Loading a dataset config from JSON.
2. Appending the same batch twice (duplicates), then overwriting twice (idempotent).
3. Upserting changed and new rides on a key column.
4. A table partitioned by pickup day.
5. Quality checks that pass, then a bad batch blocked before the write.
6. DuckDB SQL over the Iceberg table.
7. Time travel to the first snapshot.
8. Exporting the table back to CSV.

Everything is written under the ``workdir`` folder. The demo only deletes what it
created there (it leaves a ``.ldp_demo`` marker file), so it can be run again safely,
and it refuses to use a non-empty folder it did not create.
"""

import datetime as dt
import json
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Any, TextIO

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv

from local_data_platform import Config, __version__
from local_data_platform.cli import format_table
from local_data_platform.exceptions import ConfigError, DataQualityError, EngineNotFound
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg, parse_transform
from local_data_platform.format.parquet import Parquet
from local_data_platform.pipeline import Pipeline
from local_data_platform.pipeline.registry import create_pipeline

MARKER = ".ldp_demo"
CITIES = ("NYC", "BKK", "LDN", "SFO")
START = dt.datetime(2024, 1, 1)
DAYS = 7
NAMESPACE = "demo"
CHANGED_RIDES = 50
NEW_RIDES = 10
MIN_ROWS = 20
STEPS = 8
# Everything the demo creates in its workdir; nothing else there is ever deleted.
_OWNED = ("data", "exports", "warehouse", "rides.json", MARKER)


def generate_rides(rows: int, seed: int, start_id: int = 1) -> pa.Table:
    """Generate deterministic synthetic taxi rides.

    Ride ``i`` is picked up on day ``i % 7`` of the week starting 2024-01-01, at a
    random second of that day, so every day has rides once ``rows >= 7``.

    Args:
        rows: Number of rides.
        seed: Seed for ``random.Random``; the same seed gives the same rides.
        start_id: The first ``ride_id``.

    Returns:
        A table with ``ride_id`` (int64), ``pickup_ts`` (timestamp[us]), ``city``
        (string), ``distance_km`` (double) and ``fare`` (double).
    """
    rng = random.Random(seed)
    ride_ids, pickups, cities, distances, fares = [], [], [], [], []
    for i in range(rows):
        distance = round(rng.uniform(0.5, 25.0), 2)
        ride_ids.append(start_id + i)
        pickups.append(START + dt.timedelta(days=i % DAYS, seconds=rng.randrange(86_400)))
        cities.append(rng.choice(CITIES))
        distances.append(distance)
        fares.append(round(3.0 + 1.75 * distance + rng.uniform(0.0, 4.0), 2))
    return pa.table({
        "ride_id": pa.array(ride_ids, pa.int64()),
        "pickup_ts": pa.array(pickups, pa.timestamp("us")),
        "city": pa.array(cities, pa.string()),
        "distance_km": pa.array(distances, pa.float64()),
        "fare": pa.array(fares, pa.float64()),
    })


def _prepare_workdir(workdir: str | Path) -> Path:
    """Create ``workdir``, or clear what an earlier demo run left in it."""
    root = Path(workdir).expanduser().resolve()
    if root.exists():
        if not root.is_dir():
            raise ConfigError(f"demo workdir {root} exists and is not a folder")
        if (root / MARKER).is_file():
            for name in _OWNED:
                path = root / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                elif path.exists() or path.is_symlink():
                    path.unlink()
        elif any(root.iterdir()):
            raise ConfigError(f"demo workdir {root} is not empty and was not created by ldp demo; "
                              "pass a new or empty folder with --workdir")
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text("Created by 'ldp demo'. Re-running the demo deletes and recreates: "
                               + ", ".join(_OWNED[:-1]) + "\n")
    return root


def _demo_config(catalog: dict[str, str]) -> dict[str, Any]:
    return {
        "identifier": "demo_rides",
        "who": "analyst", "what": "rides", "where": "NYC", "when": "daily", "how": "batch",
        "metadata": {
            "source": {"name": "rides", "format": "CSV", "path": "data/rides.csv"},
            "target": {
                "name": "rides",
                "format": "ICEBERG",
                "catalog": catalog,
                "write_mode": "upsert",
                "join_cols": ["ride_id"],
            },
            "quality": {
                "on_failure": "fail",
                "checks": [
                    {"check": "row_count", "min": 1},
                    {"check": "not_null", "columns": ["ride_id", "pickup_ts", "fare"]},
                    {"check": "unique", "columns": ["ride_id"]},
                    {"check": "accepted_values", "column": "city", "values": list(CITIES)},
                    {"check": "range", "column": "fare", "min": 0},
                    # Pinned to the synthetic week, so the check passes on any date you run the demo.
                    {"check": "freshness", "column": "pickup_ts", "max_age_hours": 48,
                     "now": (START + dt.timedelta(days=DAYS)).isoformat()},
                    {"check": "schema", "columns": {"ride_id": "int64", "city": "string", "fare": "double"}},
                ],
            },
        },
    }


def _day_partitioning_available() -> bool:
    """Whether this pyiceberg can write ``day()``-partitioned data.

    From pyiceberg 0.10, time transforms need the optional ``pyiceberg-core``
    package at write time. The check uses the public transform API and writes nothing.
    """
    from pyiceberg.exceptions import NotInstalledError
    from pyiceberg.types import TimestampType

    try:
        parse_transform("day").pyarrow_transform(TimestampType())
    except (NotInstalledError, NotImplementedError):
        return False
    return True


def _add_pickup_date(df: pa.Table) -> pa.Table:
    """Transform: add ``pickup_date`` (date32), the pickup day of each ride."""
    return df.append_column("pickup_date", pc.cast(df["pickup_ts"], pa.date32()))


def _display_path(path: Path) -> str:
    """``path`` relative to the cwd when it is inside it, else absolute; quoted if it has spaces."""
    try:
        text = str(path.relative_to(Path.cwd()))
    except ValueError:
        text = str(path)
    return f'"{text}"' if " " in text else text


def _money(value: float) -> str:
    return f"{value:,.2f}"


def run_demo(workdir: str | Path, rows: int = 1000, seed: int = 42, *, out: TextIO | None = None) -> dict[str, Any]:
    """Run the demo in ``workdir`` and print a narrative of each step.

    Args:
        workdir: Folder for the demo's data, catalog and warehouse. It is created
            if needed. A folder from an earlier demo run is reused (the demo's own
            files are recreated); any other non-empty folder is refused.
        rows: Number of synthetic rides, at least 20.
        seed: Seed for the synthetic data.
        out: Where to print the narrative. Defaults to ``sys.stdout``.

    Returns:
        The numbers each step produced. Keys: ``workdir``, ``config_path``,
        ``rows``, ``seed``, ``pipeline``, ``append_rows`` (row counts after each
        of two appends), ``duplicate_ride_ids``, ``overwrite_rows`` (after each of
        two overwrites), ``initial_rows``, ``upsert_updated``, ``upsert_inserted``,
        ``upsert_rows_before``, ``upsert_rows_after``, ``upsert_rerun_written``,
        ``partition_spec``, ``partitions``, ``partition_files_total``,
        ``partition_files_scanned``, ``quality_checks_run``, ``quality_passed``,
        ``bad_batch_blocked``, ``bad_batch_failed_checks``, ``rows_before_bad_batch``,
        ``rows_after_bad_batch``, ``sql_rows``, ``sql_total_rides``,
        ``sql_total_revenue`` (the three ``sql_*`` keys are ``None`` without DuckDB),
        ``snapshot_count``, ``first_snapshot_rows``, ``current_rows``,
        ``export_rows``, ``export_verified`` and ``duration_s``.

    Raises:
        ValueError: If ``rows`` is below 20.
        ConfigError: If ``workdir`` is a file, or a non-empty folder the demo did not create.
    """
    if not isinstance(rows, int) or rows < MIN_ROWS:
        raise ValueError(f"the demo needs at least {MIN_ROWS} rows, got {rows!r}")
    stream = out if out is not None else sys.stdout

    def say(text: str = "") -> None:
        print(text, file=stream)

    def step(number: int, title: str) -> None:
        say()
        say(f"[{number}/{STEPS}] {title}")
        say("-" * (len(title) + 6))

    started = time.perf_counter()
    root = _prepare_workdir(workdir)
    numbers: dict[str, Any] = {"workdir": str(root), "rows": rows, "seed": seed}

    say(f"local-data-platform {__version__} demo")
    say(f"workdir: {root}")
    say(f"data: {rows} synthetic taxi rides over {DAYS} days, seed {seed} (same seed, same rides)")

    rides = generate_rides(rows, seed)
    data_dir = root / "data"
    data_dir.mkdir()
    pa_csv.write_csv(rides, data_dir / "rides.csv")
    Parquet("rides", "data/rides.parquet", base_dir=root).put(rides)
    catalog = {"identifier": NAMESPACE, "warehouse_path": "warehouse"}
    config_path = root / "rides.json"
    config_path.write_text(json.dumps(_demo_config(catalog), indent=2) + "\n")
    numbers["config_path"] = str(config_path)

    # ------------------------------------------------------------------ 1. config
    step(1, "Config from JSON")
    config = Config.from_json(config_path)
    pipeline = create_pipeline(config)
    target = pipeline.target
    numbers["pipeline"] = type(pipeline).__name__
    say(f"Loaded {config_path.name} (identifier {config.identifier!r}). Its paths resolve against {config.base_dir}")
    say(f"  source  {config.source['format']} {config.source['path']}")
    say(f"  target  {config.target['format']} {target.identifier} (write_mode {target.write_mode}, "
        f"join_cols {target.join_cols})")
    say(f"  quality {len(pipeline.checks)} checks, on_failure={pipeline.on_failure}")
    say(f"create_pipeline(config) picked {type(pipeline).__name__} from the formats {config.source['format']} -> "
        f"{config.target['format']}")

    # ------------------------------------------------------------------ 2. append vs overwrite
    step(2, "Append twice, then overwrite twice")
    scratch = Pipeline(
        source=Parquet("rides", "data/rides.parquet", base_dir=root),
        target=Iceberg("rides_scratch", catalog, base_dir=root),
        name="scratch",
    )
    append_rows = [scratch.run(mode="append").write_result.rows_after for _ in range(2)]
    ids = scratch.target.get(selected_fields=["ride_id"])
    counts = ids.group_by("ride_id").aggregate([("ride_id", "count")])
    duplicate_ids = pc.sum(pc.greater(counts["ride_id_count"], 1).cast(pa.int64())).as_py() or 0
    say(f"append    #1 -> {append_rows[0]:>6} rows")
    say(f"append    #2 -> {append_rows[1]:>6} rows   {duplicate_ids} ride_ids now appear twice: "
        "append is not idempotent")
    overwrite_rows = [scratch.run(mode="overwrite").write_result.rows_after for _ in range(2)]
    say(f"overwrite #1 -> {overwrite_rows[0]:>6} rows")
    say(f"overwrite #2 -> {overwrite_rows[1]:>6} rows   same count again: overwrite is idempotent")
    numbers.update(append_rows=append_rows, duplicate_ride_ids=duplicate_ids, overwrite_rows=overwrite_rows)

    # ------------------------------------------------------------------ 3. upsert
    step(3, "Upsert changed and new rides")
    initial = pipeline.run()
    say(f"Initial load from the config ({target.write_mode} into a new table): "
        f"{initial.write_result.rows_after} rows")
    changed_count = min(CHANGED_RIDES, rows)
    changed = rides.slice(0, changed_count)
    changed = changed.set_column(changed.schema.get_field_index("fare"), "fare",
                                 pc.round(pc.add(changed["fare"], 2.5), 2))
    changes = pa.concat_tables([changed, generate_rides(NEW_RIDES, seed + 1, start_id=rows + 1)])
    Parquet("rides_changes", "data/rides_changes.parquet", base_dir=root).put(changes)
    batch = create_pipeline(config, source=Parquet("rides_changes", "data/rides_changes.parquet", base_dir=root))
    upserted = batch.run().write_result
    inserted = upserted.rows_after - upserted.rows_before
    updated = upserted.rows_written - inserted
    first_id = rides["ride_id"][0].as_py()
    old_fare = rides["fare"][0].as_py()
    new_fare = target.get(row_filter=f"ride_id == {first_id}", selected_fields=["fare"])["fare"][0].as_py()
    say(f"Batch of {changes.num_rows} rows ({changed_count} with a new fare, {NEW_RIDES} new rides), "
        f"upserted on {target.join_cols}:")
    say(f"  {updated} updated, {inserted} inserted: {upserted.rows_before} -> {upserted.rows_after} rows")
    say(f"  ride {first_id}: fare {old_fare:.2f} -> {new_fare:.2f}")
    rerun = batch.run().write_result
    say(f"The same batch again: {rerun.rows_written} rows changed, still {rerun.rows_after} rows")
    numbers.update(initial_rows=initial.write_result.rows_after, upsert_updated=updated, upsert_inserted=inserted,
                   upsert_rows_before=upserted.rows_before, upsert_rows_after=upserted.rows_after,
                   upsert_rerun_written=rerun.rows_written)

    # ------------------------------------------------------------------ 4. partitioning
    step(4, "A day-partitioned table")
    if _day_partitioning_available():
        partition_by = [{"column": "pickup_ts", "transform": "day"}]
        day_filter = (f"pickup_ts >= '{START + dt.timedelta(days=2):%Y-%m-%dT%H:%M:%S}' and "
                      f"pickup_ts < '{START + dt.timedelta(days=3):%Y-%m-%dT%H:%M:%S}'")
    else:
        partition_by = [{"column": "pickup_date", "transform": "identity"}]
        day_filter = f"pickup_date = '{(START + dt.timedelta(days=2)).date().isoformat()}'"
        say("pyiceberg-core is not installed, and this pyiceberg needs it to write day(pickup_ts).")
        say("  Partitioning by identity(pickup_date) instead, which also gives one partition per day.")
        say('  To use day(): pip install "pyiceberg[pyiceberg-core]"')
    spec = ", ".join(f"{item['transform']}({item['column']})" for item in partition_by)
    by_day = Pipeline(
        source=Parquet("rides", "data/rides.parquet", base_dir=root),
        target=Iceberg("rides_by_day", catalog, partition_by=partition_by, write_mode="overwrite", base_dir=root),
        transforms=[_add_pickup_date],
        name="rides_by_day",
    )
    by_day_result = by_day.run()
    table = by_day.target.table()
    partitions = table.inspect.partitions().num_rows
    files_total = len(list(table.scan().plan_files()))
    files_scanned = len(list(table.scan(row_filter=day_filter).plan_files()))
    say(f"Wrote {by_day_result.rows_written} rows to {by_day.target.identifier}, partitioned by {spec}")
    say("  (a transform added the pickup_date column before the write)")
    say(f"  {partitions} partitions, {files_total} data files")
    say(f"  a filter on one day ({day_filter}) reads {files_scanned} of {files_total} data files")
    numbers.update(partition_spec=spec, partitions=partitions, partition_files_total=files_total,
                   partition_files_scanned=files_scanned)

    # ------------------------------------------------------------------ 5. quality
    step(5, "Quality checks: a clean batch passes, a bad batch is blocked")
    say("The initial load in step 3 ran the config's checks before writing:")
    for line in initial.quality.summary().splitlines():
        say(f"  {line}")
    bad = generate_rides(MIN_ROWS, seed + 2, start_id=rows + NEW_RIDES + 1)
    bad_ids = bad["ride_id"].to_pylist()
    bad_ids[0:3] = [None, None, None]                  # missing keys
    bad_ids[3:6] = [bad_ids[6]] * 3                    # the same ride three more times
    bad_fares = bad["fare"].to_pylist()
    bad_fares[7] = -12.5                               # a negative fare
    bad = bad.set_column(0, "ride_id", pa.array(bad_ids, pa.int64()))
    bad = bad.set_column(bad.schema.get_field_index("fare"), "fare", pa.array(bad_fares, pa.float64()))
    Parquet("rides_bad", "data/rides_bad.parquet", base_dir=root).put(bad)
    rows_before_bad = target.row_count()
    snapshots_before_bad = len(target.snapshots())
    failed_checks: list[str] = []
    try:
        create_pipeline(config, source=Parquet("rides_bad", "data/rides_bad.parquet", base_dir=root)).run()
    except DataQualityError as error:
        failed_checks = [result.name for result in error.report.failures]
        say(f"A bad batch of {bad.num_rows} rows (3 null ride_ids, a ride_id repeated, a negative fare):")
        for line in str(error).splitlines():
            say(f"  {line}")
    else:
        say("The bad batch was NOT blocked; this is a bug.")
    rows_after_bad = target.row_count()
    blocked = bool(failed_checks) and rows_after_bad == rows_before_bad \
        and len(target.snapshots()) == snapshots_before_bad
    say(f"Blocked before the write: {target.identifier} still has {rows_after_bad} rows and "
        f"{snapshots_before_bad} snapshots")
    numbers.update(quality_checks_run=len(initial.quality), quality_passed=initial.quality.passed,
                   bad_batch_blocked=blocked, bad_batch_failed_checks=failed_checks,
                   rows_before_bad_batch=rows_before_bad, rows_after_bad_batch=rows_after_bad)

    # ------------------------------------------------------------------ 6. DuckDB
    step(6, "DuckDB SQL: revenue by city per day")
    sql = ("SELECT CAST(pickup_ts AS DATE) AS day, city, count(*) AS rides, round(sum(fare), 2) AS revenue "
           "FROM rides GROUP BY ALL ORDER BY day, city")
    try:
        from local_data_platform.engine.duckdb import DuckDBEngine

        with DuckDBEngine() as duck:
            duck.register_iceberg(target, "rides")
            revenue = duck.query(sql)
    except EngineNotFound as error:
        say(f"Skipped: {error}")
        numbers.update(sql_rows=None, sql_total_rides=None, sql_total_revenue=None)
    else:
        say(sql)
        grid: dict[Any, dict[str, Any]] = {}
        for row in revenue.to_pylist():
            grid.setdefault(row["day"], {"day": row["day"]})[row["city"]] = row["revenue"]
        pivot = []
        for day, cells in grid.items():
            total = sum(cells.get(city) or 0.0 for city in CITIES)
            pivot.append({"day": day, **{city: cells.get(city) for city in CITIES}, "total": round(total, 2)})
        for line in format_table(pivot, float_digits=2).splitlines():
            say(f"  {line}")
        total_rides = pc.sum(revenue["rides"]).as_py()
        total_revenue = round(pc.sum(revenue["revenue"]).as_py(), 2)
        say(f"  {total_rides} rides, revenue {_money(total_revenue)} ({revenue.num_rows} day x city groups)")
        numbers.update(sql_rows=revenue.num_rows, sql_total_rides=total_rides, sql_total_revenue=total_revenue)

    # ------------------------------------------------------------------ 7. time travel
    step(7, "Time travel")
    snapshots = target.snapshots()
    for snapshot in snapshots:
        say(f"  snapshot {snapshot['snapshot_id']:>20}  {snapshot['operation']:<9} "
            f"total-records {snapshot['summary'].get('total-records', '?')}")
    first_rows = target.get(snapshot_id=snapshots[0]["snapshot_id"]).num_rows
    current_rows = target.row_count()
    say("The first snapshot is the initial load. The upsert committed an overwrite that removed the changed rows,")
    say("then appends with their new values and the new rides. Re-running it changed nothing, so it added no snapshot.")
    say(f"As of the first snapshot the table had {first_rows} rows; now it has {current_rows}")
    numbers.update(snapshot_count=len(snapshots), first_snapshot_rows=first_rows, current_rows=current_rows)

    # ------------------------------------------------------------------ 8. export
    step(8, "Export back to CSV")
    export_config = Config.from_dict({
        "identifier": "demo_rides_export",
        "metadata": {
            "source": {"name": "rides", "format": "ICEBERG", "catalog": catalog},
            "target": {"name": "rides_export", "format": "CSV", "path": "exports/rides.csv"},
            "quality": {"checks": [{"check": "row_count", "equals": "source"}]},
        },
    }, base_dir=root)
    export = create_pipeline(export_config)
    exported = export.run()
    read_back = CSV("rides_export", "exports/rides.csv", base_dir=root).get().num_rows
    verified = read_back == exported.rows_written == current_rows
    say(f"{type(export).__name__} wrote {exported.rows_written} rows to {export.target.path}")
    say(f"Read back {read_back} rows: {'matches' if verified else 'DOES NOT match'} the table")
    numbers.update(export_rows=exported.rows_written, export_verified=verified)

    # ------------------------------------------------------------------ summary
    numbers["duration_s"] = round(time.perf_counter() - started, 2)
    say()
    say("Summary")
    say("-------")
    summary = [
        {"step": "1 config", "result": f"{numbers['pipeline']} from {config_path.name}"},
        {"step": "2 append x2", "result": f"{append_rows[0]} -> {append_rows[1]} rows ({duplicate_ids} duplicates)"},
        {"step": "2 overwrite x2", "result": f"{overwrite_rows[0]} -> {overwrite_rows[1]} rows (idempotent)"},
        {"step": "3 upsert", "result": f"{updated} updated, {inserted} inserted -> {upserted.rows_after} rows"},
        {"step": "4 partitions",
         "result": f"{partitions} by {spec}; one day reads {files_scanned}/{files_total} files"},
        {"step": "5 quality", "result": f"clean batch {len(initial.quality)}/{len(initial.quality)} passed; "
                                        f"bad batch {'blocked' if blocked else 'NOT blocked'} "
                                        f"({len(failed_checks)} checks failed)"},
        {"step": "6 duckdb", "result": (f"{numbers['sql_rows']} groups, revenue {_money(numbers['sql_total_revenue'])}"
                                        if numbers["sql_rows"] is not None else "skipped (duckdb not installed)")},
        {"step": "7 time travel", "result": f"first snapshot {first_rows} rows, now {current_rows} "
                                            f"({len(snapshots)} snapshots)"},
        {"step": "8 export", "result": f"{exported.rows_written} rows to CSV, "
                                       f"{'verified' if verified else 'MISMATCH'}"},
    ]
    for line in format_table(summary).splitlines()[:-1]:
        say(f"  {line}")
    say()
    shown = _display_path(config_path)
    say(f"Done in {numbers['duration_s']:.1f}s. Explore it yourself:")
    say(f"  ldp snapshots {shown}")
    say(f"  ldp query {shown} \"SELECT city, count(*) AS rides, round(avg(fare), 2) AS avg_fare "
        "FROM rides GROUP BY city\"")
    return numbers


__all__ = ["CITIES", "MARKER", "generate_rides", "run_demo"]
