"""Tests of ``ldp maintain --system``: the cutoff, the delete of whole days, and the trim of the four ``_ldp`` tables.

Every test runs on a real local catalog under ``tmp_path`` that holds 120 days of synthetic history, written through the
``iceberg`` sink as ``ldp run``, ``ldp monitor`` and the MCP server write it. The clock of the cutoff is pinned to
``NOW``, and the rows are dated from ``NOW`` back, so the numbers do not depend on the day that the test runs.
"""

import datetime as dt
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException, NoSuchNamespaceError
from pyiceberg.table import Table

import local_data_platform.maintenance.cli as maintain_cli
import local_data_platform.maintenance.system as system_module
from local_data_platform.catalog.provider import create_catalog
from local_data_platform.cli import main
from local_data_platform.events import IcebergSink, RunEvent, read_iceberg_events, summarize_runs
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.maintenance import MaintenanceError, delete_old_rows, maintain_system_tables
from local_data_platform.maintenance.cli import maintenance_failed, run_system_maintenance
from local_data_platform.maintenance.system import DEFAULT_KEEP_DAYS, SYSTEM_TABLES, trim_cutoff
from local_data_platform.monitoring.results import MonitorResult
from local_data_platform.monitoring.status import rows

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 18, 15, 30, 0, tzinfo=UTC)
DAYS = 120
CUTOFF_90 = dt.datetime(2026, 8, 20, 0, 0, 0, tzinfo=UTC)
TABLES = ("runs", "quality_results", "monitor_results", "audit")
PER_DAY = {"runs": 3, "quality_results": 2, "monitor_results": 2, "audit": 1}
"""How many rows each day of synthetic history adds to each table."""
FRESHNESS = {"warn_after": {"count": 1, "period": "day"}, "error_after": {"count": 30, "period": "day"}}


def _at(days_ago: int, minutes: float = 0) -> dt.datetime:
    """The moment of the synthetic events of a day, which is 15:30 UTC that many days before ``NOW``."""
    return (NOW - dt.timedelta(days=days_ago)).replace(hour=15, minute=30) + dt.timedelta(minutes=minutes)


def _spec(root: Path, name: str = "shop", warehouse: str = "warehouse") -> dict:
    return {"identifier": name, "warehouse_path": str(root / warehouse)}


def _events(k: int) -> list[RunEvent]:
    base = {"pipeline": "orders", "table": "shop.orders"}
    run = f"orders-run-{k:03d}"
    results = [{"name": "not_null", "column": "id", "passed": True, "failing_rows": 0},
               {"name": "unique", "column": "id", "passed": True, "failing_rows": 0}]
    return [
        RunEvent(f"{run}-0", "run.started", 1, _at(k), run, 1, 0,
                 payload={**base, "mode": "overwrite", "publish": True}),
        RunEvent(f"{run}-1", "quality.evaluated", 1, _at(k, 0.5), run, 1, 1,
                 payload={**base, "checks_run": 2, "checks_failed": 0, "passed": True, "on_failure": "fail",
                          "results": results}),
        RunEvent(f"{run}-9", "run.finished", 1, _at(k, 1), run, 1, 9,
                 payload={**base, "status": "published", "rows_written": 1, "duration_s": 1.0}),
    ]


def _write_history(spec: dict, *, days: range = range(DAYS), batches: int = 4, namespace: str = "_ldp",
                   tables: tuple[str, ...] = TABLES, base_dir: Path | None = None) -> None:
    """Append the history of ``days`` (days ago) to the ``_ldp`` tables of a catalog, in ``batches`` commits each."""
    ordered = sorted(days, reverse=True)
    size = -(-len(ordered) // batches)
    for start in range(0, len(ordered), size):
        sink = IcebergSink(spec, base_dir, namespace=namespace, batch_size=100_000)
        for k in ordered[start:start + size]:
            if "runs" in tables or "quality_results" in tables:
                for event in _events(k):
                    sink.emit(event)
            if "monitor_results" in tables:
                for name in ("freshness", "volume.rows"):
                    sink.emit_monitor(MonitorResult(name, name.split(".")[0], "ok", _at(k), "orders",
                                                    "shop.orders").to_dict())
            if "audit" in tables:
                sink.emit_audit({"tool": "describe_table", "status": "ok", "duration_ms": 1.5, "table": "shop.orders",
                                 "ts": _at(k).isoformat(), "event_id": f"audit-{k:03d}"})
        sink.flush()


def _config(folder: Path, name: str = "orders", *, observability: dict | None = None, catalog: dict | None = None,
            target_format: str = "ICEBERG") -> Path:
    folder.mkdir(exist_ok=True)
    target = ({"name": name, "format": "ICEBERG", "write_mode": "overwrite",
               "catalog": catalog or {"identifier": "shop", "warehouse_path": "../warehouse"}}
              if target_format == "ICEBERG" else {"name": name, "format": "CSV", "path": f"../{name}-out.csv"})
    metadata = {"source": {"name": name, "format": "CSV", "path": f"../{name}.csv"}, "target": target,
                "sla": {"freshness": FRESHNESS}}
    if observability is not None:
        metadata["observability"] = observability
    path = folder / f"{name}.json"
    path.write_text(json.dumps({"identifier": name, "who": "ops team", "metadata": metadata}))
    return path


@pytest.fixture
def lake(tmp_path, monkeypatch):
    """A catalog with the table ``shop.orders`` and 120 days of history in the four ``_ldp`` tables."""
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    spec = _spec(tmp_path)
    catalog = create_catalog(spec)
    catalog.create_namespace_if_not_exists("shop")
    schema = pa.schema([("id", pa.int64()), ("ordered_at", pa.timestamp("us"))])
    orders = catalog.create_table("shop.orders", schema=schema)
    orders.append(pa.Table.from_pylist([{"id": 1, "ordered_at": NOW.replace(tzinfo=None)}], schema=schema))
    _write_history(spec)
    config = _config(tmp_path / "config", observability={"sinks": ["iceberg"]})
    return SimpleNamespace(root=tmp_path, spec=spec, folder=tmp_path / "config", config=str(config),
                           warehouse=tmp_path / "warehouse")


def _ldp(argv, capsys):
    code = main([str(item) for item in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _table(lake, name: str, namespace: str = "_ldp"):
    return create_catalog(lake.spec).load_table(f"{namespace}.{name}")


def _counts(lake) -> dict[str, int]:
    return {name: _table(lake, name).scan().count() for name in TABLES}


def _kept(days: int) -> dict[str, int]:
    return {name: rows_per_day * days for name, rows_per_day in PER_DAY.items()}


def _locations(lake) -> dict[str, str]:
    return {name: _table(lake, name).metadata_location for name in TABLES}


def _data_files(table) -> set[str]:
    return set(table.inspect.files().column("file_path").to_pylist())


def _oldest(table) -> dt.datetime:
    return min(table.scan(selected_fields=("ts",)).to_arrow().column("ts").to_pylist())


def _days(table) -> set[dt.date]:
    return {moment.date() for moment in table.scan(selected_fields=("ts",)).to_arrow().column("ts").to_pylist()}


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under ``root`` with its size and modification time, to tell that nothing was written or removed."""
    return {str(path): (path.stat().st_size, path.stat().st_mtime_ns) for path in root.rglob("*") if path.is_file()}


def _age_everything(root: Path, hours: float = 100) -> None:
    old = time.time() - hours * 3600
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


def _referenced_files_exist(table) -> None:
    """Every manifest list, manifest and data file of every snapshot is still on disk."""
    for snapshot in table.snapshots():
        assert Path(urlparse(snapshot.manifest_list).path).is_file()
        for manifest in snapshot.manifests(table.io):
            assert Path(urlparse(manifest.manifest_path).path).is_file()
            for entry in manifest.fetch_manifest_entry(table.io):
                assert Path(urlparse(entry.data_file.file_path).path).is_file()


# ---------------------------------------------------------------------- the cutoff


@pytest.mark.parametrize("now, keep_days, expected", [
    (dt.datetime(2026, 11, 18, 15, 30, tzinfo=UTC), 90, dt.datetime(2026, 8, 20, tzinfo=UTC)),
    (dt.datetime(2026, 11, 18, 0, 0, 0, tzinfo=UTC), 90, dt.datetime(2026, 8, 20, tzinfo=UTC)),
    (dt.datetime(2026, 11, 18, 23, 59, 59, 999999, tzinfo=UTC), 90, dt.datetime(2026, 8, 20, tzinfo=UTC)),
    (dt.datetime(2026, 11, 18, 15, 30, tzinfo=UTC), 0, dt.datetime(2026, 11, 18, tzinfo=UTC)),
    (dt.datetime(2026, 11, 18, 15, 30, tzinfo=UTC), 1, dt.datetime(2026, 11, 17, tzinfo=UTC)),
    (dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 1, dt.datetime(2025, 12, 31, tzinfo=UTC)),
    (dt.datetime(2028, 3, 1, 10, 0, tzinfo=UTC), 366, dt.datetime(2027, 3, 1, tzinfo=UTC)),
])
def test_the_cutoff_is_the_start_of_the_day_that_holds_the_moment_keep_days_back(now, keep_days, expected):
    cutoff = trim_cutoff(keep_days, now)
    assert cutoff == expected and cutoff.tzinfo is not None and cutoff.utcoffset() == dt.timedelta(0)
    assert (cutoff.hour, cutoff.minute, cutoff.second, cutoff.microsecond) == (0, 0, 0, 0)


def test_the_cutoff_counts_in_utc_days_whatever_the_zone_of_now_is():
    zone = dt.timezone(dt.timedelta(hours=5, minutes=30))
    assert trim_cutoff(1, dt.datetime(2026, 11, 19, 2, 0, tzinfo=zone)) == dt.datetime(2026, 11, 17, tzinfo=UTC)
    assert trim_cutoff(1, dt.datetime(2026, 11, 18, 20, 30)) == dt.datetime(2026, 11, 17, tzinfo=UTC)


def test_the_cutoff_reads_the_clock_when_it_is_not_given_a_moment(monkeypatch):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    assert trim_cutoff(90) == CUTOFF_90
    assert DEFAULT_KEEP_DAYS == 90


@pytest.mark.parametrize("keep_days, error", [(-1, ValueError), (1.5, TypeError), (True, TypeError), ("3", TypeError)])
def test_the_cutoff_wants_a_whole_number_of_days_that_is_not_negative(keep_days, error):
    with pytest.raises(error):
        trim_cutoff(keep_days, NOW)


# ---------------------------------------------------------------------- delete_old_rows


def test_a_delete_drops_whole_files_and_rewrites_none(lake):
    table = _table(lake, "monitor_results")
    before = _data_files(table)
    report = delete_old_rows(table, CUTOFF_90)

    after = _data_files(table)
    assert report["committed"] is True and report["dry_run"] is False
    assert (report["rows_before"], report["rows_deleted"], report["files_deleted"]) == (240, 58, 29)
    assert after < before and len(before - after) == 29
    assert after - before == set(), "a data file was written, so a file was rewritten"
    summary = table.current_snapshot().summary.additional_properties
    assert summary["deleted-data-files"] == "29" and "added-data-files" not in summary
    assert table.scan().count() == 182 and _oldest(table) >= CUTOFF_90


def test_a_dry_run_says_what_would_go_and_commits_nothing(lake):
    table = _table(lake, "monitor_results")
    before = table.metadata_location
    report = delete_old_rows(table, CUTOFF_90, dry_run=True)

    assert report["committed"] is False and report["dry_run"] is True
    assert (report["rows_before"], report["rows_deleted"], report["files_deleted"]) == (240, 58, 29)
    assert report["table"] == "_ldp.monitor_results" and report["cutoff"] == "2026-08-20T00:00:00Z"
    assert _table(lake, "monitor_results").metadata_location == before
    assert table.metadata_location == before and table.scan().count() == 240


def test_the_oldest_and_the_newest_row_to_go_come_from_the_files(lake):
    report = delete_old_rows(_table(lake, "monitor_results"), CUTOFF_90, dry_run=True)
    assert report["oldest"] == "2026-07-22T15:30:00Z"   # 119 days before NOW
    assert report["newest"] == "2026-08-19T15:30:00Z"   # 91 days before NOW, the last day before the cutoff


def test_the_oldest_is_the_lowest_lower_bound_and_the_newest_the_highest_upper_bound(lake):
    report = delete_old_rows(_table(lake, "runs"), CUTOFF_90, dry_run=True)
    # a day of runs holds events at 15:30:00, 15:30:30 and 15:31:00, so the two bounds of a file differ
    assert report["oldest"] == "2026-07-22T15:30:00Z"
    assert report["newest"] == "2026-08-19T15:31:00Z"


def _plain_table(tmp_path, properties=None):
    """A table that is not partitioned, with one file that holds a row on each side of the cutoff."""
    catalog = create_catalog(_spec(tmp_path))
    catalog.create_namespace_if_not_exists("_ldp")
    schema = pa.schema([("ts", pa.timestamp("us", tz="UTC")), ("v", pa.int64())])
    table = catalog.create_table("_ldp.runs", schema=schema, properties=properties or {})
    table.append(pa.Table.from_pylist([{"ts": CUTOFF_90 - dt.timedelta(days=2), "v": 1},
                                       {"ts": CUTOFF_90 + dt.timedelta(days=1), "v": 2}], schema=schema))
    return table


@pytest.mark.parametrize("properties", [{"write.metadata.metrics.default": "none"},
                                        {"write.metadata.metrics.column.ts": "none"}])
def test_a_file_that_records_no_bound_of_ts_has_no_oldest_and_newest_but_the_rows_are_counted(tmp_path, properties):
    report = delete_old_rows(_plain_table(tmp_path, properties), CUTOFF_90, dry_run=True)
    assert report["oldest"] is None and report["newest"] is None
    assert (report["rows_before"], report["rows_deleted"], report["files_deleted"]) == (2, 1, 1)


def test_a_table_that_keeps_no_statistics_and_has_no_row_before_the_cutoff_commits_nothing(tmp_path):
    catalog = create_catalog(_spec(tmp_path))
    catalog.create_namespace_if_not_exists("_ldp")
    schema = pa.schema([("ts", pa.timestamp("us", tz="UTC")), ("v", pa.int64())])
    table = catalog.create_table("_ldp.runs", schema=schema, properties={"write.metadata.metrics.default": "none"})
    table.append(pa.Table.from_pylist([{"ts": CUTOFF_90 + dt.timedelta(days=1), "v": 1}], schema=schema))
    before = table.metadata_location

    report = delete_old_rows(table, CUTOFF_90)

    # Without statistics the plan cannot leave the file out, and reading it finds that no row goes.
    assert (report["rows_deleted"], report["files_deleted"], report["committed"]) == (0, 1, False)
    assert catalog.load_table("_ldp.runs").metadata_location == before


def test_a_file_whose_statistics_are_missing_altogether_has_no_oldest_and_newest(lake):
    table = _table(lake, "runs")
    tasks = [SimpleNamespace(file=SimpleNamespace(lower_bounds=None, upper_bounds=None))]
    assert system_module._bound(table, tasks, "lower_bounds", min) is None
    assert system_module._bound(table, tasks, "upper_bounds", max) is None


def test_a_delete_that_matches_no_row_commits_nothing(lake):
    table = _table(lake, "audit")
    before = table.metadata_location
    report = delete_old_rows(table, dt.datetime(2020, 1, 1, tzinfo=UTC))

    assert (report["rows_before"], report["rows_deleted"], report["files_deleted"]) == (120, 0, 0)
    assert report["committed"] is False and report["oldest"] is None and report["newest"] is None
    assert _table(lake, "audit").metadata_location == before


def test_a_row_on_the_cutoff_stays_and_a_row_just_before_it_goes(tmp_path):
    spec = _spec(tmp_path)
    sink = IcebergSink(spec)
    for moment in (CUTOFF_90 - dt.timedelta(microseconds=1), CUTOFF_90, CUTOFF_90 + dt.timedelta(hours=1)):
        sink.emit_monitor(MonitorResult("freshness", "freshness", "ok", moment, "orders", "shop.orders").to_dict())
    sink.flush()
    table = create_catalog(spec).load_table("_ldp.monitor_results")

    dry = delete_old_rows(table, CUTOFF_90, dry_run=True)
    assert dry["oldest"] == dry["newest"] == "2026-08-19T23:59:59Z", "a part of a second is cut off, not rounded up"

    report = delete_old_rows(table, CUTOFF_90)

    assert (report["rows_before"], report["rows_deleted"], report["files_deleted"]) == (3, 1, 1)
    assert _oldest(table) == CUTOFF_90 and table.scan().count() == 2


def test_a_naive_cutoff_is_taken_to_be_utc(lake):
    table = _table(lake, "audit")
    report = delete_old_rows(table, CUTOFF_90.replace(tzinfo=None), dry_run=True)
    assert report["cutoff"] == "2026-08-20T00:00:00Z" and report["rows_deleted"] == 29


def test_a_cutoff_in_another_zone_is_the_same_moment_in_utc(lake):
    zone = dt.timezone(dt.timedelta(hours=2))
    report = delete_old_rows(_table(lake, "audit"), dt.datetime(2026, 8, 20, 2, 0, tzinfo=zone), dry_run=True)
    assert report["cutoff"] == "2026-08-20T00:00:00Z" and report["rows_deleted"] == 29


def test_a_conflicting_commit_makes_the_delete_plan_again(lake, monkeypatch):
    original, calls = Table.delete, []

    def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise CommitFailedException("a concurrent commit")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Table, "delete", flaky)
    table = _table(lake, "runs")
    report = delete_old_rows(table, CUTOFF_90)

    assert len(calls) == 2 and report["committed"] is True
    assert (report["rows_deleted"], table.scan().count()) == (87, 273)
    assert _table(lake, "runs").scan().count() == 273


def test_a_delete_that_keeps_conflicting_gives_up_and_deletes_nothing(lake, monkeypatch):
    calls = []

    def always(self, *args, **kwargs):
        calls.append(1)
        raise CommitFailedException("a concurrent commit")

    monkeypatch.setattr(Table, "delete", always)
    table = _table(lake, "runs")
    before = table.metadata_location
    with pytest.raises(MaintenanceError, match=r"_ldp\.runs conflicted with concurrent commits 3 times"):
        delete_old_rows(table, CUTOFF_90)
    assert len(calls) == system_module.MAX_COMMIT_ATTEMPTS == 3
    assert _table(lake, "runs").metadata_location == before and _table(lake, "runs").scan().count() == 360


def test_a_conflict_that_a_later_attempt_survives_reloads_the_table_from_the_catalog(lake, monkeypatch):
    original, seen = Table.delete, []

    def flaky(self, *args, **kwargs):
        seen.append(self.metadata_location)
        if len(seen) == 1:
            # a writer appends to the table before the delete commits, as ldp monitor can
            IcebergSink(lake.spec, batch_size=1).emit_audit(
                {"tool": "late", "status": "ok", "duration_ms": 1.0, "ts": NOW.isoformat(), "event_id": "late"})
            raise CommitFailedException("a concurrent commit")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Table, "delete", flaky)
    table = _table(lake, "audit")
    report = delete_old_rows(table, CUTOFF_90)

    assert report["committed"] is True and seen[0] != seen[1], "the second attempt did not reload the table"
    assert _table(lake, "audit").scan().count() == 120 - 29 + 1
    assert table.scan().count() == 120 - 29 + 1, "the table that the caller passed was not refreshed"


def test_a_table_that_is_not_partitioned_by_day_is_still_trimmed_right(tmp_path):
    spec = _spec(tmp_path)
    catalog = create_catalog(spec)
    catalog.create_namespace_if_not_exists("_ldp")
    schema = pa.schema([("ts", pa.timestamp("us", tz="UTC")), ("v", pa.int64())])
    table = catalog.create_table("_ldp.runs", schema=schema)
    assert not table.spec().fields

    def append(moments):
        table.append(pa.Table.from_pylist([{"ts": moment, "v": number} for number, moment in enumerate(moments)],
                                          schema=schema))

    before_the_cutoff = CUTOFF_90 - dt.timedelta(days=3)
    append([before_the_cutoff, CUTOFF_90 + dt.timedelta(hours=10)])     # one file that holds both sides
    append([before_the_cutoff, before_the_cutoff + dt.timedelta(hours=1)])  # one file of old rows only
    append([CUTOFF_90 + dt.timedelta(days=1)])                              # one file of new rows only
    files = _data_files(table)

    dry = delete_old_rows(table, CUTOFF_90, dry_run=True)
    assert (dry["rows_before"], dry["rows_deleted"], dry["files_deleted"]) == (5, 3, 2)

    report = delete_old_rows(table, CUTOFF_90)
    assert report["committed"] is True and (report["rows_deleted"], report["files_deleted"]) == (3, 2)
    assert table.scan().count() == 2 and _oldest(table) >= CUTOFF_90
    assert len(_data_files(table) - files) == 1, "the file that held both sides is rewritten without its old row"


# ---------------------------------------------------------------------- maintain_system_tables


def _maintain(lake, **options):
    settings = {"cutoff": CUTOFF_90, "expire_days": None, "retain_last": 20, "orphan_age_hours": None,
                "keep_metadata_versions": 50, "dry_run": True}
    settings.update(options)
    return maintain_system_tables(create_catalog(lake.spec), **settings)


def test_the_four_tables_are_trimmed_in_order_and_the_others_are_left_alone(lake):
    orders = create_catalog(lake.spec).load_table("shop.orders").metadata_location
    result = _maintain(lake, dry_run=False)

    assert SYSTEM_TABLES == ("runs", "quality_results", "monitor_results", "audit")
    assert list(result["tables"]) == ["_ldp.runs", "_ldp.quality_results", "_ldp.monitor_results", "_ldp.audit"]
    assert result["namespace"] == "_ldp" and result["dry_run"] is False and result["cutoff"] == "2026-08-20T00:00:00Z"
    assert _counts(lake) == _kept(91)
    for name, entry in result["tables"].items():
        assert entry["exists"] is True and entry["delete"]["committed"] is True
        assert set(entry) == {"exists", "delete"}, "no part ran that was not asked for"
        assert entry["delete"]["table"] == name
    assert create_catalog(lake.spec).load_table("shop.orders").metadata_location == orders


def test_a_dry_run_changes_no_file_of_the_lake(lake):
    before = _tree(lake.root)
    result = _maintain(lake, expire_days=0, orphan_age_hours=72)
    assert all(entry["delete"]["dry_run"] is True for entry in result["tables"].values())
    assert _tree(lake.root) == before, "a dry run wrote or removed a file"


def test_a_table_that_is_not_there_is_reported_and_not_created(tmp_path, monkeypatch):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    spec = _spec(tmp_path)
    _write_history(spec, tables=("runs",), batches=1)
    catalog = create_catalog(spec)
    # only runs and quality_results are written by a sink that has only run events
    names = {identifier[1] for identifier in catalog.list_tables("_ldp")}
    assert names == {"runs", "quality_results"}

    result = maintain_system_tables(catalog, cutoff=CUTOFF_90, expire_days=None, retain_last=20, orphan_age_hours=None,
                                    keep_metadata_versions=50, dry_run=False)
    assert result["tables"]["_ldp.monitor_results"] == {"exists": False}
    assert result["tables"]["_ldp.audit"] == {"exists": False}
    assert result["tables"]["_ldp.runs"]["exists"] is True and result["tables"]["_ldp.quality_results"]["exists"]
    assert {identifier[1] for identifier in catalog.list_tables("_ldp")} == names


def test_a_catalog_with_no_system_namespace_gets_none_created(tmp_path):
    catalog = create_catalog(_spec(tmp_path))
    catalog.create_namespace_if_not_exists("shop")
    result = maintain_system_tables(catalog, cutoff=CUTOFF_90, expire_days=7, retain_last=20, orphan_age_hours=72,
                                    keep_metadata_versions=50, dry_run=False)
    assert result["tables"] == {f"_ldp.{name}": {"exists": False} for name in TABLES}
    assert catalog.list_namespaces() == [("shop",)]


def test_a_catalog_that_says_the_namespace_is_missing_has_none_of_the_tables_either():
    """A REST or Glue catalog says so with its own error, where a local one says that the table is missing."""
    def load_table(identifier):
        raise NoSuchNamespaceError(f"Namespace does not exist: {identifier}")

    result = maintain_system_tables(SimpleNamespace(load_table=load_table), cutoff=CUTOFF_90, expire_days=7,
                                    retain_last=20, orphan_age_hours=72, keep_metadata_versions=50, dry_run=False)
    assert result["tables"] == {f"_ldp.{name}": {"exists": False} for name in TABLES}


def test_the_parts_that_are_not_asked_for_do_not_run(lake):
    only_delete = _maintain(lake)["tables"]["_ldp.runs"]
    with_expiry = _maintain(lake, expire_days=0)["tables"]["_ldp.runs"]
    with_orphans = _maintain(lake, orphan_age_hours=72)["tables"]["_ldp.runs"]
    assert set(only_delete) == {"exists", "delete"}
    assert set(with_expiry) == {"exists", "delete", "expire"}
    assert set(with_orphans) == {"exists", "delete", "orphans"}


def test_the_options_reach_the_expiry_and_the_orphan_finder(lake):
    entry = _maintain(lake, expire_days=3, retain_last=2, orphan_age_hours=30, keep_metadata_versions=7)["tables"][
        "_ldp.runs"]
    assert entry["expire"]["retain_last"] == 2 and entry["expire"]["dry_run"] is True
    assert entry["orphans"]["older_than_hours"] == 30 and entry["orphans"]["dry_run"] is True
    older_than = dt.datetime.fromisoformat(entry["expire"]["older_than"].replace("Z", "+00:00"))
    assert abs(older_than - (dt.datetime.now(UTC) - dt.timedelta(days=3))) < dt.timedelta(minutes=5)


def test_a_zero_still_asks_for_the_part_that_it_belongs_to(lake):
    entry = _maintain(lake, expire_days=0, orphan_age_hours=0)["tables"]["_ldp.runs"]
    assert set(entry) == {"exists", "delete", "expire", "orphans"}
    assert entry["orphans"]["older_than_hours"] == 0


def test_the_cutoff_of_a_report_is_in_utc_whatever_zone_it_was_given_in(lake):
    zone = dt.timezone(dt.timedelta(hours=2))
    assert _maintain(lake, cutoff=dt.datetime(2026, 8, 20, 2, 0, tzinfo=zone))["cutoff"] == "2026-08-20T00:00:00Z"


def test_a_namespace_that_a_sink_names_is_the_one_that_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    spec = _spec(tmp_path)
    _write_history(spec, namespace="history", batches=2)
    catalog = create_catalog(spec)
    result = maintain_system_tables(catalog, namespace="history", cutoff=CUTOFF_90, expire_days=None, retain_last=20,
                                    orphan_age_hours=None, keep_metadata_versions=50, dry_run=False)
    assert list(result["tables"]) == [f"history.{name}" for name in TABLES]
    assert catalog.load_table("history.runs").scan().count() == 273
    assert result["namespace"] == "history"


# ---------------------------------------------------------------------- ldp maintain --system


def test_120_days_of_history_keep_90_days_and_ldp_runs_and_ldp_status_still_work(lake, capsys):
    assert _counts(lake) == _kept(120)
    before = _locations(lake)

    code, out, err = _ldp(["maintain", lake.config, "--system"], capsys)
    assert code == 0 and err == ""
    assert "dry run; pass --apply to make the changes" in out
    assert "Rows before 2026-08-20T00:00:00Z, the start of the day 90 days back:" in out
    for line in ("_ldp.runs             would delete 87 of 360 rows in 29 files, 2026-07-22 to 2026-08-19",
                 "_ldp.quality_results  would delete 58 of 240 rows in 29 files, 2026-07-22 to 2026-08-19",
                 "_ldp.monitor_results  would delete 58 of 240 rows in 29 files, 2026-07-22 to 2026-08-19",
                 "_ldp.audit            would delete 29 of 120 rows in 29 files, 2026-07-22 to 2026-08-19"):
        assert line in out
    assert _counts(lake) == _kept(120) and _locations(lake) == before, "a dry run changed a table"

    code, out, err = _ldp(["maintain", lake.config, "--system", "--apply"], capsys)
    assert code == 0 and err == ""
    assert "(applied)" in out and "deleted 87 of 360 rows in 29 files" in out and "would delete" not in out
    assert _counts(lake) == _kept(91)   # 90 days back plus the day that holds that moment, which is a whole day
    for name in TABLES:
        table = _table(lake, name)
        assert _oldest(table) >= CUTOFF_90
        days = _days(table)
        assert CUTOFF_90.date() in days and (CUTOFF_90 - dt.timedelta(days=1)).date() not in days
        assert len(days) == 91

    code, out, err = _ldp(["runs", lake.config, "--limit", "500"], capsys)
    assert code == 0 and "orders-run-000" in out and "orders-run-090" in out and "orders-run-091" not in out
    events = read_iceberg_events(create_catalog(lake.spec), pipeline="orders")
    assert len(summarize_runs(events)) == 91 and len(events) == 273

    later = NOW + dt.timedelta(hours=1)   # after the last run ended, which is a minute after NOW
    code, out, err = _ldp(["status", lake.folder, "--now", later.isoformat()], capsys)
    assert code == 0 and err == "" and "orders" in out and "published" in out
    assert "History goes back to 2026-08-20T15:30:00Z, the oldest event of any pipeline here." in out
    [row] = rows(str(lake.folder), now=later)
    assert row.last_run is not None and row.last_run.status == "published" and row.problem is None


def test_the_default_is_90_days_and_the_result_says_which_settings_were_used(lake, capsys):
    code, out, _ = _ldp(["maintain", lake.config, "--system", "--json"], capsys)
    result = json.loads(out)
    assert code == 0 and result["keep_days"] == 90 and result["cutoff"] == "2026-08-20T00:00:00Z"
    assert (result["expire_days"], result["retain_last"], result["orphan_age_hours"],
            result["keep_metadata_versions"]) == (7.0, 20, 72.0, 50)
    assert result["catalog"] == "shop" and result["namespace"] == "_ldp" and result["dry_run"] is True
    runs = result["tables"]["_ldp.runs"]
    assert runs["exists"] is True
    delete = runs["delete"]
    assert (delete["rows_before"], delete["rows_deleted"], delete["files_deleted"]) == (360, 87, 29)
    assert set(runs) == {"exists", "delete", "expire", "orphans"}


@pytest.mark.parametrize("keep_days, days_kept", [(30, 31), (1, 2), (0, 1), (365, 120)])
def test_keep_days_counts_whole_utc_days_and_keeps_the_day_that_holds_the_moment(lake, capsys, keep_days, days_kept):
    code, _, err = _ldp(["maintain", lake.config, "--system", "--keep-days", keep_days, "--apply"], capsys)
    assert code == 0 and err == ""
    assert _counts(lake) == _kept(days_kept)


def test_an_apply_with_nothing_old_enough_deletes_and_commits_nothing(lake, capsys):
    before = _locations(lake)
    code, out, _ = _ldp(["maintain", lake.config, "--system", "--keep-days", 365, "--apply"], capsys)
    assert code == 0 and "nothing to delete, 360 rows" in out and "nothing to delete, 120 rows" in out
    assert _locations(lake) == before


def _exists(location: str) -> bool:
    return Path(urlparse(location).path).exists()


def test_the_files_of_the_days_that_went_leave_the_disk_once_no_snapshot_and_no_metadata_version_names_them(
        lake, capsys):
    _age_everything(lake.warehouse)
    dropped = {name: _data_files(_table(lake, name)) for name in TABLES}
    trim = ["maintain", lake.config, "--system", "--apply", "--expire", 0, "--retain-last", 1, "--orphans"]

    code, out, err = _ldp(trim, capsys)
    assert code == 0 and err == ""
    assert "Snapshots older than 0 days, keeping the last 1:" in out and "Orphan files older than 72 hours:" in out
    gone = {}
    for name in TABLES:
        table = _table(lake, name)
        gone[name] = dropped[name] - _data_files(table)
        assert len(gone[name]) == 29 and len(table.snapshots()) == 1
        assert all(_exists(path) for path in gone[name]), "the snapshot of the delete names them as deleted entries"
        _referenced_files_exist(table)

    # A later commit of a run, of ldp monitor or of the MCP server. The metadata versions from before the expiry still
    # name the snapshots that named the files, and the default keeps the last 50 of them.
    _write_history(lake.spec, days=range(1), batches=1)
    _age_everything(lake.warehouse)
    code, out, err = _ldp(trim, capsys)
    assert code == 0 and err == ""
    assert all(_exists(path) for name in TABLES for path in gone[name]), "the last 50 metadata versions name them"

    code, out, err = _ldp([*trim, "--keep-metadata-versions", 0], capsys)
    assert code == 0 and err == ""
    for name in TABLES:
        table = _table(lake, name)
        assert not any(_exists(path) for path in gone[name]), f"{name} kept the files of the days that went"
        _referenced_files_exist(table)
        assert table.scan().count() == PER_DAY[name] * 92 and len(table.snapshots()) == 1


def test_the_expiry_alone_leaves_the_orphans_and_the_orphans_alone_leave_the_snapshots(lake, capsys):
    _age_everything(lake.warehouse)
    snapshots = {name: len(_table(lake, name).snapshots()) for name in TABLES}

    code, out, _ = _ldp(["maintain", lake.config, "--system", "--apply", "--expire", 0, "--retain-last", 2], capsys)
    assert code == 0 and "Orphan files" not in out and "Snapshots older than 0 days, keeping the last 2:" in out
    assert "expired 3 of 5 snapshots" in out
    assert {name: len(_table(lake, name).snapshots()) for name in TABLES} == {name: 2 for name in TABLES}
    assert all(snapshots[name] == 4 for name in TABLES)

    code, out, _ = _ldp(["maintain", lake.config, "--system", "--orphans", "--keep-days", 365], capsys)
    assert code == 0 and "Snapshots older" not in out and "Orphan files older than 72 hours:" in out


def test_an_orphan_file_of_a_system_table_is_listed_by_a_dry_run_and_removed_by_an_apply(lake, capsys):
    table = _table(lake, "runs")
    stray = Path(urlparse(table.location()).path) / "data" / "stray.parquet"
    stray.write_bytes(b"not iceberg")
    _age_everything(lake.warehouse)

    code, out, _ = _ldp(["maintain", lake.config, "--system", "--orphans", "--keep-days", 365], capsys)
    assert code == 0 and "would delete 1 files (11 B)" in out and stray.is_file()

    code, out, _ = _ldp(["maintain", lake.config, "--system", "--orphans", "--keep-days", 365, "--apply"], capsys)
    assert code == 0 and "deleted 1 files (11 B)" in out and not stray.exists()


def test_an_orphan_age_of_zero_hours_still_asks_for_the_orphans_on_a_dry_run(lake, capsys):
    argv = ["maintain", lake.config, "--system", "--orphans", "--orphan-age-hours", 0, "--keep-days", 365]
    code, out, _ = _ldp(argv, capsys)
    assert code == 0 and "Orphan files older than 0 hours:" in out and "would delete 0 files (0 B)" in out
    assert "Snapshots older" not in out


def test_the_dry_run_says_that_a_delete_is_a_snapshot_of_its_own_and_when_the_files_show_up(lake, capsys):
    _age_everything(lake.warehouse)
    _, out, _ = _ldp(["maintain", lake.config, "--system", "--expire", 0, "--retain-last", 1, "--orphans"], capsys)
    assert ("a delete is a snapshot of its own, so a real run can expire one more snapshot of a table than this "
            "lists") in out
    assert "files only the expiring snapshots reference show up once the expiry is applied and 50 newer metadata" in out
    _, out, _ = _ldp(["maintain", lake.config, "--system", "--keep-days", 365], capsys)
    assert "a delete is a snapshot" not in out, "nothing is deleted, so no snapshot is added"
    assert "show up once the expiry" not in out, "nothing would expire"
    _, out, _ = _ldp(["maintain", lake.config, "--system", "--expire", 0, "--retain-last", 1, "--orphans", "--apply"],
                     capsys)
    assert "a delete is a snapshot" not in out and "show up once the expiry" not in out


def test_the_table_of_the_config_is_left_alone(lake, capsys):
    orders = create_catalog(lake.spec).load_table("shop.orders")
    before = (orders.metadata_location, len(orders.snapshots()))
    code, _, _ = _ldp(["maintain", lake.config, "--system", "--apply", "--expire", 0, "--retain-last", 1], capsys)
    orders = create_catalog(lake.spec).load_table("shop.orders")
    assert code == 0 and (orders.metadata_location, len(orders.snapshots())) == before


def test_a_table_that_is_not_there_is_said_so_and_the_run_goes_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    spec = _spec(tmp_path)
    catalog = create_catalog(spec)
    catalog.create_namespace_if_not_exists("shop")
    schema = pa.schema([("id", pa.int64())])
    catalog.create_table("shop.orders", schema=schema)
    _write_history(spec, tables=("runs",), batches=1)
    config = _config(tmp_path / "config", observability={"sinks": ["iceberg"]})

    code, out, _ = _ldp(["maintain", config, "--system"], capsys)
    assert code == 0
    assert "_ldp.monitor_results  not there" in out and "_ldp.audit            not there" in out
    assert "would delete 87 of 360 rows" in out and "would delete 58 of 240 rows" in out
    assert "_ldp.audit" not in out.split("Snapshots older")[1]


def test_the_catalog_of_the_iceberg_sink_is_the_one_that_is_trimmed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    own = _spec(tmp_path, name="history", warehouse="history-warehouse")
    _write_history(own, batches=2)
    shop = create_catalog(_spec(tmp_path))
    shop.create_namespace_if_not_exists("shop")
    shop.create_table("shop.orders", schema=pa.schema([("id", pa.int64())]))
    config = _config(tmp_path / "config", observability={"sinks": [
        {"type": "iceberg", "catalog": {"identifier": "history", "warehouse_path": "../history-warehouse"}}]})

    code, out, _ = _ldp(["maintain", config, "--system", "--apply", "--keep-days", 30], capsys)

    assert code == 0 and "of catalog history" in out
    assert create_catalog(own).load_table("_ldp.runs").scan().count() == 93
    assert ("_ldp",) not in shop.list_namespaces()


def test_a_config_without_a_sink_uses_the_catalog_of_its_table(lake, tmp_path, capsys):
    plain = _config(lake.folder, "plain")
    code, out, _ = _ldp(["maintain", plain, "--system", "--apply"], capsys)
    assert code == 0 and "of catalog shop" in out
    assert _counts(lake) == _kept(91), "the history that other configs and the MCP server wrote was trimmed"


def test_a_sink_that_names_a_namespace_has_that_namespace_trimmed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    spec = _spec(tmp_path)
    _write_history(spec, namespace="history", batches=2)
    create_catalog(spec).create_namespace_if_not_exists("shop")
    create_catalog(spec).create_table("shop.orders", schema=pa.schema([("id", pa.int64())]))
    config = _config(tmp_path / "config", observability={"sinks": [{"type": "iceberg", "namespace": "history"}]})

    code, out, _ = _ldp(["maintain", config, "--system", "--apply"], capsys)

    assert code == 0 and "Maintenance of the history tables of catalog shop" in out and "history.runs" in out
    assert create_catalog(spec).load_table("history.runs").scan().count() == 273


def test_a_config_with_no_iceberg_catalog_is_an_error(tmp_path):
    config = _config(tmp_path / "config", target_format="CSV")
    with pytest.raises(ConfigError, match="has no Iceberg catalog, so it has no _ldp tables"):
        run_system_maintenance(str(config))


def test_a_missing_catalog_is_an_error_and_creates_nothing(tmp_path):
    config = _config(tmp_path / "config", observability={"sinks": ["iceberg"]})
    with pytest.raises(TableNotFound, match="no catalog at"):
        run_system_maintenance(str(config))
    assert not (tmp_path / "warehouse").exists()


def test_keep_days_needs_system(lake, capsys):
    code, out, err = _ldp(["maintain", lake.config, "--keep-days", 30, "--apply"], capsys)
    assert code == 1 and "ldp: error: --keep-days only applies with --system" in err and out == ""
    assert _counts(lake) == _kept(120)


def test_a_keep_days_of_zero_needs_system_too(lake, capsys):
    code, out, err = _ldp(["maintain", lake.config, "--keep-days", 0], capsys)
    assert code == 1 and "--keep-days only applies with --system" in err and out == ""


def test_the_notes_of_a_dry_run_appear_when_any_one_table_would_delete_or_expire(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(system_module, "utc_now", lambda: NOW)
    spec = _spec(tmp_path)
    _write_history(spec, tables=("runs", "quality_results", "monitor_results"), batches=2)
    _write_history(spec, days=range(30), tables=("audit",), batches=1)   # nothing in it is old enough to go
    create_catalog(spec).create_namespace_if_not_exists("shop")
    config = _config(tmp_path / "config", observability={"sinks": ["iceberg"]})
    _age_everything(tmp_path / "warehouse")

    _, out, _ = _ldp(["maintain", config, "--system", "--expire", 0, "--retain-last", 1, "--orphans"], capsys)

    assert "_ldp.audit            nothing to delete, 30 rows" in out
    assert "a delete is a snapshot of its own" in out, "three of the four tables would delete"
    assert "show up once the expiry is applied" in out, "the audit table has one snapshot and the others two"


def test_a_catalog_with_none_of_the_tables_prints_no_section_for_the_parts(tmp_path, capsys):
    catalog = create_catalog(_spec(tmp_path))
    catalog.create_namespace_if_not_exists("shop")
    catalog.create_table("shop.orders", schema=pa.schema([("id", pa.int64())]))
    config = _config(tmp_path / "config", observability={"sinks": ["iceberg"]})
    code, out, _ = _ldp(["maintain", config, "--system"], capsys)
    assert code == 0 and out.count("not there") == 4
    assert "Snapshots older" not in out and "Orphan files" not in out


def test_a_report_of_a_catalog_that_has_no_identifier_names_no_catalog(capsys):
    result = {"namespace": "_ldp", "dry_run": True, "cutoff": "2026-08-20T00:00:00Z", "keep_days": 90, "catalog": None,
              "expire_days": None, "orphan_age_hours": None, "retain_last": 20, "keep_metadata_versions": 50,
              "tables": {"_ldp.runs": {"exists": False}}}
    maintain_cli._print_system(result)
    assert capsys.readouterr().out.splitlines()[0] == (
        "Maintenance of the _ldp tables (dry run; pass --apply to make the changes)")
    result["catalog"] = "history"
    maintain_cli._print_system(result)
    assert capsys.readouterr().out.splitlines()[0].startswith("Maintenance of the _ldp tables of catalog history (dry")


def test_a_table_whose_plan_names_files_and_no_row_is_printed_as_nothing_to_delete(capsys):
    delete = {"rows_before": 10, "rows_deleted": 0, "files_deleted": 2, "oldest": None, "newest": None}
    expire = {"expired_snapshot_ids": [], "snapshots_before": 3}
    result = {"namespace": "_ldp", "dry_run": True, "cutoff": "2026-08-20T00:00:00Z", "keep_days": 90,
              "catalog": "shop", "expire_days": 7, "orphan_age_hours": None, "retain_last": 20,
              "keep_metadata_versions": 50,
              "tables": {"_ldp.runs": {"exists": True, "delete": delete, "expire": expire}}}
    maintain_cli._print_system(result)
    out = capsys.readouterr().out
    assert "  _ldp.runs  nothing to delete, 10 rows\n" in out
    assert "a delete is a snapshot" not in out, "no row goes, so the delete adds no snapshot"


def test_a_table_whose_files_record_no_bounds_is_printed_without_a_span_of_days(capsys):
    delete = {"rows_before": 10, "rows_deleted": 4, "files_deleted": 2, "oldest": None, "newest": None}
    result = {"namespace": "_ldp", "dry_run": True, "cutoff": "2026-08-20T00:00:00Z", "keep_days": 90,
              "catalog": "shop", "expire_days": None, "orphan_age_hours": None, "retain_last": 20,
              "keep_metadata_versions": 50, "tables": {"_ldp.runs": {"exists": True, "delete": delete}}}
    maintain_cli._print_system(result)
    assert "  _ldp.runs  would delete 4 of 10 rows in 2 files\n" in capsys.readouterr().out + "\n"
    delete.update(oldest="2026-07-22T15:30:00Z", newest="2026-08-19T15:31:00Z")
    maintain_cli._print_system(result)
    assert "  _ldp.runs  would delete 4 of 10 rows in 2 files, 2026-07-22 to 2026-08-19\n" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["-1", "x", "1.5"])
def test_keep_days_wants_a_whole_number_that_is_not_negative(lake, capsys, value):
    code, out, err = _ldp(["maintain", lake.config, "--system", "--keep-days", value], capsys)
    assert code == 1 and "--keep-days" in err
    assert _counts(lake) == _kept(120)


def test_the_help_says_what_system_and_keep_days_do(capsys):
    code, out, _ = _ldp(["maintain", "--help"], capsys)
    flat = " ".join(out.split())
    assert code == 0 and "--system" in flat and "--keep-days N" in flat
    assert "With --system the tables are the _ldp history tables of the config's catalog" in flat
    assert "(default 90)" in flat


def test_the_settings_default_to_those_of_ldp_maintain_and_a_part_that_is_not_asked_for_is_none(lake):
    everything = run_system_maintenance(lake.config)
    assert (everything["keep_days"], everything["expire_days"], everything["retain_last"],
            everything["orphan_age_hours"], everything["keep_metadata_versions"]) == (90, 7.0, 20, 72.0, 50)
    expiry = run_system_maintenance(lake.config, expire=3, retain_last=5, keep_days=10)
    assert (expiry["keep_days"], expiry["expire_days"], expiry["retain_last"], expiry["orphan_age_hours"]) == (
        10, 3, 5, None)
    orphans = run_system_maintenance(lake.config, orphans=True, orphan_age_hours=30, keep_metadata_versions=2)
    assert (orphans["expire_days"], orphans["orphan_age_hours"], orphans["keep_metadata_versions"]) == (None, 30, 2)
    assert orphans["dry_run"] is True
    assert run_system_maintenance(lake.config, apply=True, keep_days=365)["dry_run"] is False


def test_a_zero_is_a_setting_and_not_a_missing_one(lake):
    result = run_system_maintenance(lake.config, expire=0, orphans=True, orphan_age_hours=0, keep_metadata_versions=0,
                                    keep_days=0)
    assert (result["keep_days"], result["expire_days"], result["orphan_age_hours"],
            result["keep_metadata_versions"]) == (0, 0, 0, 0)
    assert result["cutoff"] == "2026-11-18T00:00:00Z"
    with pytest.raises(ValueError, match="retain_last must be at least 1"):
        run_system_maintenance(lake.config, retain_last=0)


def test_an_object_with_a_table_method_is_trimmed_as_the_table_that_it_gives(lake):
    table = _table(lake, "audit")
    report = delete_old_rows(SimpleNamespace(table=lambda: table), CUTOFF_90, dry_run=True)
    assert (report["table"], report["rows_deleted"]) == ("_ldp.audit", 29)


def test_the_cutoff_of_a_run_counts_back_from_now_when_it_is_given(lake):
    result = run_system_maintenance(lake.config, keep_days=1, now=dt.datetime(2026, 12, 1, 9, 0, tzinfo=UTC))
    assert result["cutoff"] == "2026-11-30T00:00:00Z"


def test_a_file_that_could_not_be_deleted_makes_the_exit_status_1_and_is_printed(lake, capsys, monkeypatch):
    real = maintain_cli.run_system_maintenance

    def failing(*args, **kwargs):
        result = real(*args, **kwargs)
        result["tables"]["_ldp.audit"]["orphans"]["failed"] = [{"path": "file:///x/stray.parquet", "error": "denied"}]
        return result

    monkeypatch.setattr(maintain_cli, "run_system_maintenance", failing)
    code, out, _ = _ldp(["maintain", lake.config, "--system"], capsys)
    assert code == 1 and "    failed: file:///x/stray.parquet: denied" in out
    code, out, _ = _ldp(["maintain", lake.config, "--system", "--json"], capsys)
    assert code == 1 and json.loads(out)["tables"]["_ldp.audit"]["orphans"]["failed"][0]["error"] == "denied"


def test_maintenance_failed_reads_both_shapes_of_result():
    assert maintenance_failed({"orphans": {"failed": [{"path": "x", "error": "y"}]}}) is True
    assert maintenance_failed({"orphans": {"failed": []}}) is False
    assert maintenance_failed({"expire": {}}) is False
    failed = {"tables": {"_ldp.runs": {"exists": True, "orphans": {"failed": []}},
                         "_ldp.audit": {"exists": True, "orphans": {"failed": [{"path": "x", "error": "y"}]}}}}
    assert maintenance_failed(failed) is True
    clean = {"tables": {"_ldp.runs": {"exists": True, "orphans": {"failed": []}}, "_ldp.audit": {"exists": False},
                        "_ldp.quality_results": {"exists": True, "delete": {}}}}
    assert maintenance_failed(clean) is False


def test_ldp_maintain_without_system_still_maintains_the_table_of_the_config(tmp_path, capsys):
    spec = _spec(tmp_path)
    catalog = create_catalog(spec)
    catalog.create_namespace_if_not_exists("shop")
    table = catalog.create_table("shop.orders", schema=pa.schema([("id", pa.int64())]))
    table.append(pa.table({"id": pa.array([1], pa.int64())}))
    config = _config(tmp_path / "config")
    code, out, _ = _ldp(["maintain", config], capsys)
    assert code == 0 and "Maintenance of Iceberg table shop.orders" in out and "Rows before" not in out
