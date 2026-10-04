"""Table maintenance (contract C8): snapshot expiry with the idempotency floor, and orphan files.

Every test runs on real local Iceberg tables under ``tmp_path``. The S3 test uses an in-process moto server
on 127.0.0.1 and skips when moto or boto3 isn't installed.
"""

import argparse
import datetime as dt
import json
import logging
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table.snapshots import ancestors_of

import local_data_platform.maintenance.orphans as orphans_module
import local_data_platform.maintenance.snapshots as snapshots_module
from local_data_platform.exceptions import ConfigError, TableNotFound
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.maintenance import (
    IDEMPOTENCY_KEY,
    MaintenanceError,
    add_cli,
    expire_snapshots,
    expiry_supported,
    find_orphans,
    plan_expiry,
    remove_orphans,
)
from local_data_platform.maintenance.orphans import normalize_location

CATALOG = {"identifier": "ns", "warehouse_path": "wh"}
SCHEMA = pa.schema([("id", pa.int64()), ("v", pa.string())])
OLD_HOURS = 100  # older than the default 72-hour orphan cut

needs_expiry = pytest.mark.skipif(not expiry_supported(),
                                  reason="this pyiceberg (< 0.10) cannot apply snapshot removal; dry runs still work")


def _rows(i: int) -> pa.Table:
    return pa.table({"id": pa.array([i], pa.int64()), "v": pa.array([f"v{i}"], pa.string())})


def _commit(table, i: int, key: str | None = None, mode: str = "append") -> None:
    """Commit one row as a new snapshot, optionally carrying an idempotency key."""
    properties = {IDEMPOTENCY_KEY: key} if key else {}
    if mode == "overwrite":
        table.overwrite(_rows(i), snapshot_properties=properties)
    else:
        table.append(_rows(i), snapshot_properties=properties)
    time.sleep(0.003)  # distinct millisecond timestamps, so floors can sit between snapshots


def _build(ice: Iceberg, n: int = 6, keys: dict[int, str] | None = None, mode: str = "append") -> list[int]:
    """Create the table and commit ``n`` snapshots; return their ids, oldest first."""
    table = ice.catalog.create_table(ice.identifier, schema=SCHEMA)
    for i in range(n):
        _commit(table, i, (keys or {}).get(i), mode)
    return [snapshot.snapshot_id for snapshot in ice.table().snapshots()]


def _future() -> dt.datetime:
    """An ``older_than`` that makes every snapshot old enough to expire."""
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)


def _at(table, snapshot_id: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(table.snapshot_by_id(snapshot_id).timestamp_ms / 1000, tz=dt.timezone.utc)


def _ids(table) -> list[int]:
    return [snapshot.snapshot_id for snapshot in table.snapshots()]


def _main_keys(table) -> set[str]:
    """Every idempotency key find_commit can reach by walking main's ancestry."""
    return {snapshot.summary.additional_properties.get(IDEMPOTENCY_KEY)
            for snapshot in ancestors_of(table.current_snapshot(), table.metadata)} - {None}


def _root(table) -> Path:
    return Path(urlparse(table.location()).path)


def _age_everything(root: Path, hours: float = OLD_HOURS) -> None:
    old = time.time() - hours * 3600
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (old, old))


def _plant(path: Path, hours: float = OLD_HOURS, content: bytes = b"not iceberg") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    old = time.time() - hours * 3600
    os.utime(path, (old, old))
    return "file://" + str(path)


def _referenced_files_exist(table) -> None:
    """Every manifest list, manifest and data file of every snapshot is still on disk."""
    for snapshot in table.snapshots():
        assert Path(urlparse(snapshot.manifest_list).path).is_file()
        for manifest in snapshot.manifests(table.io):
            assert Path(urlparse(manifest.manifest_path).path).is_file()
            for entry in manifest.fetch_manifest_entry(table.io):
                assert Path(urlparse(entry.data_file.file_path).path).is_file()


@pytest.fixture
def ice(tmp_path) -> Iceberg:
    return Iceberg("events", CATALOG, base_dir=tmp_path)


# --------------------------------------------------------------------------- expire_snapshots


@needs_expiry
def test_expiry_keeps_retain_last_and_the_current_snapshot(ice):
    ids = _build(ice)
    report = expire_snapshots(ice, older_than=_future(), retain_last=2, protect_keys_since=_future())

    table = ice.table()
    assert report["committed"] and not report["dry_run"]
    assert report["expired_snapshot_ids"] == sorted(ids[:4])
    assert _ids(table) == ids[4:]
    assert report["snapshots_before"] == 6 and report["snapshots_after"] == 2
    assert table.current_snapshot().snapshot_id == ids[-1]
    assert report["protected_snapshot_ids"]["current"] == [ids[-1]]
    assert sorted(report["protected_snapshot_ids"]["retain_last"]) == sorted(ids[4:])
    assert sorted(ice.get().column("id").to_pylist()) == list(range(6))
    assert ice.get(snapshot_id=ids[4]).num_rows == 5
    assert report["metadata_location"] == table.metadata_location


@needs_expiry
def test_the_current_snapshot_survives_even_with_retain_last_one(ice):
    ids = _build(ice)
    report = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_future())
    assert _ids(ice.table()) == [ids[-1]]
    assert len(report["expired_snapshot_ids"]) == 5
    assert ice.get().num_rows == 6


@needs_expiry
def test_keyed_snapshots_newer_than_the_floor_are_never_expired(ice):
    ids = _build(ice, n=6, keys={1: "K-old", 3: "K-new"})
    table = ice.table()
    floor = _at(table, ids[2])  # K-new (snapshot 3) is newer than the floor, K-old (snapshot 1) is older

    report = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=floor)

    table = ice.table()
    protected = report["protected_snapshot_ids"]
    assert protected["idempotency_key"] == [ids[3]]
    assert sorted(protected["key_ancestry"]) == sorted(ids[3:])  # the chain main -> K-new stays walkable
    assert _ids(table) == ids[3:]
    assert sorted(report["expired_snapshot_ids"]) == sorted(ids[:3])
    assert _main_keys(table) == {"K-new"}, "find_commit must still reach every key newer than the floor"
    assert report["protect_keys_since"] == floor.isoformat(timespec="milliseconds").replace("+00:00", "Z")


@needs_expiry
def test_a_keyed_snapshot_deep_in_history_keeps_its_whole_ancestry(ice):
    ids = _build(ice, n=8, keys={2: "K2"})
    table = ice.table()
    report = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_at(table, ids[0]))
    assert _ids(ice.table()) == ids[2:]
    assert _main_keys(ice.table()) == {"K2"}
    assert sorted(report["protected_snapshot_ids"]["key_ancestry"]) == sorted(ids[2:])


@needs_expiry
def test_the_default_floor_is_the_table_horizon(ice):
    ids = _build(ice, n=5, keys={0: "K0", 2: "K2"})
    # Default horizon: 7 days, so both keys (committed just now) are protected.
    report = expire_snapshots(ice, older_than=_future(), retain_last=1)
    assert sorted(report["protected_snapshot_ids"]["idempotency_key"]) == sorted([ids[0], ids[2]])
    assert _ids(ice.table()) == ids
    assert _main_keys(ice.table()) == {"K0", "K2"}

    # A zero-day horizon on the table puts the floor at now, so the keys may go.
    table = ice.table()
    with table.transaction() as txn:
        txn.set_properties({"ldp.idempotency.horizon-days": "0"})
    report = expire_snapshots(ice, older_than=_future(), retain_last=1)
    assert report["protected_snapshot_ids"]["idempotency_key"] == []
    assert _ids(ice.table()) == [ids[-1]]


def test_an_invalid_horizon_property_fails_closed(ice):
    ids = _build(ice, n=3, keys={0: "K0"})
    with ice.table().transaction() as txn:
        txn.set_properties({"ldp.idempotency.horizon-days": "a week"})
    with pytest.raises(MaintenanceError, match="horizon-days"):
        expire_snapshots(ice, older_than=_future(), retain_last=1)
    assert _ids(ice.table()) == ids


def test_a_floor_later_than_the_table_horizon_warns(ice, caplog):
    _build(ice, n=3, keys={0: "K0"})
    with ice.table().transaction() as txn:
        txn.set_properties({"ldp.idempotency.horizon-days": "7"})
    with caplog.at_level(logging.WARNING, logger="local_data_platform"):
        expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_future(), dry_run=True)
    assert "newer than the idempotency horizon" in caplog.text


def test_dry_run_plans_but_changes_nothing(ice):
    ids = _build(ice)
    before = ice.table().metadata_location
    report = expire_snapshots(ice, older_than=_future(), retain_last=2, protect_keys_since=_future(), dry_run=True)
    assert report["dry_run"] and not report["committed"]
    assert report["expired_snapshot_ids"] == sorted(ids[:4])
    assert report["snapshots_after"] == 2
    assert ice.table().metadata_location == before
    assert _ids(ice.table()) == ids


@needs_expiry
def test_older_than_bounds_what_can_expire(ice):
    ids = _build(ice)
    table = ice.table()
    report = expire_snapshots(ice, older_than=_at(table, ids[2]), retain_last=1, protect_keys_since=_future())
    assert sorted(report["expired_snapshot_ids"]) == sorted(ids[:2])
    assert _ids(ice.table()) == ids[2:]


def test_a_timedelta_older_than_counts_back_from_now(ice):
    ids = _build(ice)
    report = expire_snapshots(ice, older_than=dt.timedelta(days=1), retain_last=1)
    assert report["expired_snapshot_ids"] == [] and not report["committed"]
    assert _ids(ice.table()) == ids


def test_nothing_to_expire_is_a_no_op(ice):
    ids = _build(ice, n=3)
    before = ice.table().metadata_location
    report = expire_snapshots(ice, older_than=_future(), retain_last=20, protect_keys_since=_future())
    assert report["expired_snapshot_ids"] == [] and not report["committed"]
    assert ice.table().metadata_location == before and _ids(ice.table()) == ids


@needs_expiry
def test_branch_and_tag_heads_survive(ice):
    ids = _build(ice)
    ice.table().manage_snapshots().create_tag(ids[1], "v1").create_branch(ids[2], "audit").commit()
    report = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_future())
    table = ice.table()
    assert set(_ids(table)) == {ids[1], ids[2], ids[-1]}
    assert set(table.refs()) == {"main", "v1", "audit"}
    assert sorted(report["protected_snapshot_ids"]["refs"]) == sorted([ids[1], ids[2], ids[-1]])
    assert ice.get(snapshot_id=ids[1]).num_rows == 2


@needs_expiry
def test_a_branch_keeps_its_min_snapshots_to_keep(ice):
    ids = _build(ice)
    ice.table().manage_snapshots().create_branch(ids[3], "keep2", min_snapshots_to_keep=2).commit()
    report = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_future())
    assert set(_ids(ice.table())) == {ids[2], ids[3], ids[-1]}
    assert sorted(report["protected_snapshot_ids"]["branch_min_snapshots"]) == sorted(ids[2:4])


@needs_expiry
def test_keys_on_a_branch_keep_the_branch_ancestry(ice):
    ids = _build(ice, n=4)
    table = ice.table()
    table.manage_snapshots().create_branch(ids[1], "staged").commit()
    table = ice.table()
    table.append(_rows(99), snapshot_properties={IDEMPOTENCY_KEY: "K-staged"}, branch="staged")
    time.sleep(0.003)
    table.append(_rows(100), branch="staged")
    table = ice.table()
    staged_head = table.refs()["staged"].snapshot_id
    keyed = table.snapshot_by_id(staged_head).parent_snapshot_id

    report = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_at(table, ids[0]))
    remaining = set(_ids(ice.table()))
    assert {staged_head, keyed, ids[-1]} <= remaining
    assert keyed in report["protected_snapshot_ids"]["idempotency_key"]
    assert {staged_head, keyed} <= set(report["protected_snapshot_ids"]["key_ancestry"])


def test_raw_pyiceberg_expiry_drops_main_when_a_rollback_races_it(ice):
    """Why expire_snapshots asserts every ref: pyiceberg's own builder only asserts the table UUID."""
    ids = _build(ice, n=3)
    stale = ice.table()
    if not expiry_supported() or not hasattr(stale, "maintenance"):
        pytest.skip("this pyiceberg has no ExpireSnapshots builder")
    ice.table().manage_snapshots().set_current_snapshot(snapshot_id=ids[0]).commit()
    stale.maintenance.expire_snapshots().by_ids([ids[0]]).commit()
    damaged = ice.table()
    assert "main" not in damaged.refs() and damaged.current_snapshot() is None


@needs_expiry
def test_a_racing_rollback_forces_a_replan_and_main_survives(ice, monkeypatch):
    ids = _build(ice, n=4)
    catalog = ice.catalog
    original = catalog.commit_table
    calls = []

    def racing_commit(table, requirements, updates):
        if not calls:
            calls.append("raced")
            # Another writer rolls main back onto a snapshot our plan is about to expire.
            ice.table().manage_snapshots().set_current_snapshot(snapshot_id=ids[0]).commit()
        return original(table, requirements, updates)

    table = catalog.load_table(ice.identifier)
    monkeypatch.setattr(catalog, "commit_table", racing_commit)
    report = expire_snapshots(table, older_than=_future(), retain_last=1, protect_keys_since=_future())

    fresh = ice.table()
    assert report["attempts"] == 2
    assert fresh.refs()["main"].snapshot_id == ids[0]
    assert fresh.current_snapshot().snapshot_id == ids[0]
    assert ids[0] not in report["expired_snapshot_ids"]
    assert sorted(report["expired_snapshot_ids"]) == sorted(ids[1:])
    assert table.metadata_location == fresh.metadata_location, "the caller's pyiceberg table is refreshed"


@needs_expiry
def test_endless_conflicts_raise_and_expire_nothing(ice, monkeypatch):
    ids = _build(ice, n=3)

    def always_conflict(table, requirements, updates):
        raise CommitFailedException("someone else committed")

    table = ice.catalog.load_table(ice.identifier)
    monkeypatch.setattr(ice.catalog, "commit_table", always_conflict)
    with pytest.raises(MaintenanceError, match="conflicted with concurrent commits 3 times"):
        expire_snapshots(table, older_than=_future(), retain_last=1, protect_keys_since=_future())
    monkeypatch.undo()
    assert _ids(ice.table()) == ids


def test_pyiceberg_without_expiry_support_plans_but_refuses_to_apply(ice, monkeypatch):
    ids = _build(ice, n=3)
    monkeypatch.setattr(snapshots_module, "expiry_supported", lambda: False)
    plan = expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_future(), dry_run=True)
    assert sorted(plan["expired_snapshot_ids"]) == sorted(ids[:2])
    with pytest.raises(NotImplementedError, match="pyiceberg >= 0.10"):
        expire_snapshots(ice, older_than=_future(), retain_last=1, protect_keys_since=_future())
    assert _ids(ice.table()) == ids


@pytest.mark.parametrize("kwargs, error", [
    ({"retain_last": 0}, ValueError),
    ({"retain_last": True}, TypeError),
    ({"retain_last": 1.5}, TypeError),
    ({"older_than": 7}, TypeError),
    ({"older_than": dt.timedelta(days=-1)}, ValueError),
    ({"protect_keys_since": "2026-01-01"}, TypeError),
])
def test_expiry_argument_validation(ice, kwargs, error):
    _build(ice, n=2)
    arguments = {"older_than": _future(), **kwargs}
    with pytest.raises(error):
        expire_snapshots(ice, **arguments)


def test_expiry_rejects_something_that_is_not_a_table():
    with pytest.raises(TypeError, match="pyiceberg Table or an Iceberg format"):
        expire_snapshots(object(), older_than=_future())


def test_expiry_of_a_missing_table_raises_table_not_found(ice):
    with pytest.raises(TableNotFound):
        expire_snapshots(ice, older_than=_future())


def test_naive_datetimes_are_utc(ice):
    ids = _build(ice, n=3)
    table = ice.table()
    naive = _at(table, ids[1]).replace(tzinfo=None)
    report = expire_snapshots(ice, older_than=naive, retain_last=1, protect_keys_since=_future(), dry_run=True)
    assert report["expired_snapshot_ids"] == [ids[0]]


def test_plan_expiry_reads_the_metadata_without_changing_it(ice):
    ids = _build(ice, n=4, keys={2: "K"})
    table = ice.table()
    before = table.metadata.model_dump_json()
    plan = plan_expiry(table.metadata, older_than_ms=table.snapshot_by_id(ids[-1]).timestamp_ms + 1,
                       retain_last=1, floor_ms=0)
    assert plan.expire == sorted(ids[:2])
    assert plan.protected_ids() == set(ids[2:])
    assert table.metadata.model_dump_json() == before


# --------------------------------------------------------------------------- orphans


@pytest.fixture
def aged_table(ice):
    """A table with appends and overwrites (so older snapshots own data files), every file aged 100 hours."""
    table = ice.catalog.create_table(ice.identifier, schema=SCHEMA)
    for i in range(3):
        _commit(table, i)
    _commit(table, 10, mode="overwrite")
    _commit(table, 11)
    table = ice.table()
    _age_everything(_root(table))
    return ice


def test_find_orphans_reports_only_the_planted_files(aged_table):
    table = aged_table.table()
    root = _root(table)
    planted = sorted([_plant(root / "data" / "stray.parquet"), _plant(root / "metadata" / "stray-m0.avro"),
                      _plant(root / "data" / "id_bucket=3" / "deep.parquet")])
    before = sorted(str(path) for path in root.rglob("*") if path.is_file())

    assert find_orphans(table) == planted
    assert sorted(str(path) for path in root.rglob("*") if path.is_file()) == before, "a dry run deletes nothing"


def test_a_clean_table_has_no_orphans(aged_table):
    assert find_orphans(aged_table) == []
    assert find_orphans(aged_table, keep_metadata_versions=0) == []


def test_recent_files_are_never_orphans(aged_table):
    table = aged_table.table()
    fresh = _plant(_root(table) / "data" / "in-flight.parquet", hours=1)
    assert find_orphans(table) == []
    assert find_orphans(table, older_than_hours=0.5) == [fresh]


def test_remove_orphans_is_a_dry_run_by_default(aged_table):
    table = aged_table.table()
    planted = _plant(_root(table) / "data" / "stray.parquet")
    report = remove_orphans(table)
    assert report["dry_run"] is True
    assert report["orphans"] == [planted] and report["deleted"] == []
    assert report["orphan_bytes"] == len(b"not iceberg")
    assert Path(urlparse(planted).path).is_file()


def test_remove_orphans_apply_deletes_only_the_orphans(aged_table):
    table = aged_table.table()
    root = _root(table)
    planted = sorted([_plant(root / "data" / "stray.parquet"), _plant(root / "metadata" / "stray.avro")])
    files_before = {str(path) for path in root.rglob("*") if path.is_file()}
    rows_before = {snapshot.snapshot_id: aged_table.get(snapshot_id=snapshot.snapshot_id).num_rows
                   for snapshot in table.snapshots()}

    report = remove_orphans(table, dry_run=False)

    assert report["deleted"] == planted and report["failed"] == [] and report["missing"] == []
    files_after = {str(path) for path in root.rglob("*") if path.is_file()}
    assert files_before - files_after == {urlparse(uri).path for uri in planted}
    table = aged_table.table()
    _referenced_files_exist(table)
    assert {snapshot.snapshot_id: aged_table.get(snapshot_id=snapshot.snapshot_id).num_rows
            for snapshot in table.snapshots()} == rows_before
    assert sorted(aged_table.get().column("id").to_pylist()) == [10, 11]
    assert remove_orphans(table, dry_run=False)["deleted"] == []


@needs_expiry
def test_expire_then_remove_orphans_reclaims_the_expired_files(aged_table):
    table = aged_table.table()
    ids = _ids(table)
    expire_snapshots(table, older_than=_future(), retain_last=2, protect_keys_since=_future())
    _age_everything(_root(table))  # the expiry wrote a fresh metadata file; age it like the rest

    # The recent metadata versions still reference the expired snapshots, so by default they stay.
    assert find_orphans(table) == []

    freed = find_orphans(table, keep_metadata_versions=0)
    names = [Path(urlparse(uri).path).name for uri in freed]
    # ids: three appends, the overwrite's delete and append snapshots, then one more append.
    for expired in ids[:4]:
        assert any(name.startswith(f"snap-{expired}-") for name in names), f"manifest list of {expired} is freed"
    assert sum(name.endswith(".parquet") for name in names) == 3, "the three overwritten data files are freed"

    report = remove_orphans(table, dry_run=False, keep_metadata_versions=0)
    assert sorted(report["deleted"]) == sorted(freed)
    fresh = aged_table.table()
    _referenced_files_exist(fresh)
    assert sorted(aged_table.get().column("id").to_pylist()) == [10, 11]
    assert _ids(fresh) == ids[4:]
    assert aged_table.get(snapshot_id=ids[4]).column("id").to_pylist() == [10]


@needs_expiry
def test_files_a_kept_manifest_still_names_as_deleted_are_kept(ice):
    table = ice.catalog.create_table(ice.identifier, schema=SCHEMA)
    for i in range(2):
        _commit(table, i)
    _commit(table, 10, mode="overwrite")  # a delete snapshot naming both old files as deleted, then an append
    table = ice.table()
    ids = _ids(table)
    old_files = {normalize_location(entry.data_file.file_path)
                 for manifest in table.snapshot_by_id(ids[1]).manifests(table.io)
                 for entry in manifest.fetch_manifest_entry(table.io)}
    expire_snapshots(table, older_than=_at(table, ids[2]), retain_last=1, protect_keys_since=_future())
    assert _ids(ice.table()) == ids[2:]
    _age_everything(_root(table))

    freed = {normalize_location(uri) for uri in find_orphans(table, keep_metadata_versions=0)}
    # The expired snapshots' manifest lists and manifests are freed; the data files are not.
    assert sum("/snap-" in key for key in freed) == 2 and sum(key.endswith("-m0.avro") for key in freed) == 2
    assert not any(key.endswith(".parquet") for key in freed)
    assert not freed & old_files, "the kept delete snapshot's manifests still name the overwritten files"


def test_metadata_files_dropped_from_the_log_are_orphans(ice):
    table = ice.catalog.create_table(ice.identifier, schema=SCHEMA,
                                     properties={"write.metadata.previous-versions-max": "1"})
    for i in range(4):
        _commit(table, i)
    table = ice.table()
    _age_everything(_root(table))
    in_log = {normalize_location(entry.metadata_file) for entry in table.metadata.metadata_log}
    all_metadata = sorted((_root(table) / "metadata").glob("*.metadata.json"))
    expected = sorted("file://" + str(path) for path in all_metadata
                      if normalize_location(str(path)) not in in_log
                      and normalize_location(str(path)) != normalize_location(table.metadata_location))

    assert len(expected) == 3
    assert find_orphans(table) == expected


def test_hidden_files_and_nested_tables_are_skipped(aged_table, tmp_path):
    table = aged_table.table()
    root = _root(table)
    _plant(root / "data" / ".stray.parquet.crc")
    _plant(root / "_SUCCESS")
    _plant(root / ".ldp" / "lock")
    _plant(root / "nested" / "metadata" / "v1.metadata.json", content=b"{}")
    _plant(root / "nested" / "data" / "theirs.parquet")
    partition_orphan = _plant(root / "data" / "_col=1" / "part.parquet")

    report = remove_orphans(table)
    assert report["orphans"] == [partition_orphan]
    assert report["stats"]["hidden"] == 3
    assert report["stats"]["other_tables"] == 2


def test_deleting_young_files_needs_allow_recent(aged_table):
    table = aged_table.table()
    planted = _plant(_root(table) / "data" / "stray.parquet", hours=2)
    with pytest.raises(ValueError, match="allow_recent"):
        remove_orphans(table, older_than_hours=1, dry_run=False)
    assert Path(urlparse(planted).path).is_file()
    assert remove_orphans(table, older_than_hours=1, dry_run=True)["orphans"] == [planted]
    report = remove_orphans(table, older_than_hours=1, dry_run=False, allow_recent=True)
    assert report["deleted"] == [planted]


def test_a_missing_manifest_fails_closed(aged_table):
    table = aged_table.table()
    planted = _plant(_root(table) / "data" / "stray.parquet")
    manifest = table.current_snapshot().manifests(table.io)[0].manifest_path
    os.remove(urlparse(manifest).path)
    with pytest.raises(MaintenanceError, match="missing"):
        find_orphans(table)
    with pytest.raises(MaintenanceError):
        remove_orphans(table, dry_run=False)
    assert Path(urlparse(planted).path).is_file()


def test_a_commit_during_the_scan_is_rechecked(aged_table, monkeypatch):
    table = aged_table.table()
    root = _root(table)
    stray = _plant(root / "data" / "stray.parquet")
    adopted = root / "data" / "adopted.parquet"
    pq.write_table(_rows(42).cast(SCHEMA), adopted)
    _age_everything(root)  # an old file that a concurrent add_files commit adopts mid-scan
    original = orphans_module._list_files

    def listing_then_commit(tbl, location):
        listed = original(tbl, location)
        aged_table.table().add_files(["file://" + str(adopted)])
        return listed

    monkeypatch.setattr(orphans_module, "_list_files", listing_then_commit)
    report = remove_orphans(table, dry_run=False)
    assert report["deleted"] == [stray]
    assert report["stats"]["rechecks"] == 1
    assert adopted.is_file()
    assert 42 in aged_table.get().column("id").to_pylist()


def test_a_location_holding_the_warehouse_is_refused(ice, tmp_path):
    warehouse = ice.path
    bad = ice.catalog.create_table("ns.bad", schema=SCHEMA, location=f"file://{warehouse}")
    with pytest.raises(MaintenanceError, match="contains the catalog warehouse"):
        find_orphans(bad)
    assert Path(ice.catalog.database_path("ns", warehouse)).is_file()


@pytest.mark.parametrize("kwargs, error", [
    ({"older_than_hours": -1}, ValueError),
    ({"older_than_hours": "72"}, TypeError),
    ({"older_than_hours": True}, TypeError),
    ({"keep_metadata_versions": -1}, ValueError),
    ({"keep_metadata_versions": 1.0}, TypeError),
])
def test_orphan_argument_validation(aged_table, kwargs, error):
    with pytest.raises(error):
        find_orphans(aged_table, **kwargs)
    with pytest.raises(error):
        remove_orphans(aged_table, **kwargs)


def test_normalize_location_compares_equivalent_spellings(tmp_path):
    path = tmp_path / "x.parquet"
    real = os.path.realpath(path)
    assert normalize_location(str(path)) == normalize_location(f"file://{path}") == normalize_location(f"file:{path}")
    assert normalize_location(str(path)) == "file:" + os.path.normcase(real)
    assert normalize_location("s3a://bucket/k/x") == normalize_location("s3://bucket/k/x")
    assert normalize_location("s3://bucket/k/x") != normalize_location("s3://bucket/k/x2")


# --------------------------------------------------------------------------- object storage (moto)


@pytest.fixture
def moto_s3(monkeypatch):
    """An in-process moto S3 server on 127.0.0.1; yields ``(endpoint, boto3 client)``."""
    boto3 = pytest.importorskip("boto3")
    server_module = pytest.importorskip("moto.server")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    try:
        server.start()
    except OSError as exc:  # pragma: no cover - sandboxes that forbid binding
        pytest.skip(f"cannot start a local moto server: {exc}")
    for name, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                        "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(name, value)
    endpoint = f"http://127.0.0.1:{port}"
    client = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1")
    try:
        yield endpoint, client
    finally:
        server.stop()


def test_orphans_and_expiry_on_s3(moto_s3, tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    endpoint, s3 = moto_s3
    s3.create_bucket(Bucket="lake")
    catalog = SqlCatalog("lake", uri=f"sqlite:///{tmp_path / 'lake.db'}", warehouse="s3://lake/wh",
                         **{"s3.endpoint": endpoint, "s3.region": "us-east-1",
                            "s3.access-key-id": "testing", "s3.secret-access-key": "testing"})
    catalog.create_namespace("ns")
    table = catalog.create_table("ns.t", schema=SCHEMA)
    for i in range(3):
        _commit(table, i)
    s3.put_object(Bucket="lake", Key="wh/ns/t/data/stray.parquet", Body=b"x")
    s3.put_object(Bucket="lake", Key="wh/ns/t2/data/sibling.parquet", Body=b"x")  # a sibling table's prefix

    # moto stamps objects with the current time, so look at every age.
    assert find_orphans(table, older_than_hours=0) == ["s3://lake/wh/ns/t/data/stray.parquet"]
    assert find_orphans(table) == []

    report = remove_orphans(table, older_than_hours=0, dry_run=False, allow_recent=True)
    assert report["deleted"] == ["s3://lake/wh/ns/t/data/stray.parquet"]
    keys = {obj["Key"] for obj in s3.list_objects_v2(Bucket="lake")["Contents"]}
    assert "wh/ns/t/data/stray.parquet" not in keys and "wh/ns/t2/data/sibling.parquet" in keys

    ids = _ids(table)
    expired = expire_snapshots(table, older_than=_future(), retain_last=1, protect_keys_since=_future(),
                               dry_run=not expiry_supported())
    assert sorted(expired["expired_snapshot_ids"]) == sorted(ids[:2])
    assert sorted(table.scan().to_arrow().column("id").to_pylist()) == [0, 1, 2]


# --------------------------------------------------------------------------- CLI


def _config(folder: Path) -> Path:
    path = folder / "events.json"
    path.write_text(json.dumps({"identifier": "events", "metadata": {
        "source": {"name": "events", "format": "CSV", "path": "events.csv"},
        "target": {"name": "events", "format": "ICEBERG", "catalog": CATALOG},
    }}))
    return path


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ldp")
    add_cli(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    return args.handler(args)


@pytest.fixture
def cli_table(tmp_path, ice):
    """A config plus its table: six snapshots, every file aged, one planted orphan."""
    ids = _build(ice)
    table = ice.table()
    _age_everything(_root(table))
    stray = _plant(_root(table) / "data" / "stray.parquet")
    return _config(tmp_path), ice, ids, stray


def test_cli_is_a_dry_run_by_default(cli_table, capsys):
    config, ice, ids, stray = cli_table
    before = ice.table().metadata_location
    assert _run(["maintain", str(config)]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "ns.events" in out
    assert "would expire 0 of 6 snapshots" in out  # nothing is older than the default 7 days
    assert "would delete 1 files" in out and stray in out
    assert ice.table().metadata_location == before
    assert Path(urlparse(stray).path).is_file()


@needs_expiry
def test_cli_apply_expires_and_removes_orphans(cli_table, capsys):
    config, ice, ids, stray = cli_table
    code = _run(["maintain", str(config), "--expire", "0", "--retain-last", "2", "--orphans", "--apply"])
    assert code == 0
    out = capsys.readouterr().out
    assert "applied" in out and "expired 4 of 6 snapshots" in out and "deleted 1 files" in out
    assert _ids(ice.table()) == ids[4:]
    assert not Path(urlparse(stray).path).exists()
    assert ice.get().num_rows == 6


@needs_expiry
def test_cli_expire_alone_leaves_orphans_alone(cli_table, capsys):
    config, ice, ids, stray = cli_table
    assert _run(["maintain", str(config), "--expire", "0", "--retain-last", "1", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "Orphan files" not in out
    assert _ids(ice.table()) == [ids[-1]]
    assert Path(urlparse(stray).path).is_file()


def test_cli_json_output(cli_table, capsys):
    config, ice, ids, stray = cli_table
    assert _run(["maintain", str(config), "--orphans", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["table"] == "ns.events" and result["dry_run"] is True
    assert "expire" not in result
    assert result["orphans"]["orphans"] == [stray]


def test_cli_missing_catalog_creates_nothing(tmp_path):
    config = _config(tmp_path)
    with pytest.raises(TableNotFound, match="no catalog"):
        _run(["maintain", str(config)])
    assert not (tmp_path / "wh").exists()


def test_cli_needs_an_iceberg_table(tmp_path):
    path = tmp_path / "csv.json"
    path.write_text(json.dumps({"identifier": "x", "metadata": {
        "source": {"name": "a", "format": "CSV", "path": "a.csv"},
        "target": {"name": "b", "format": "CSV", "path": "b.csv"}}}))
    with pytest.raises(ConfigError, match="no Iceberg table"):
        _run(["maintain", str(path)])


@pytest.mark.parametrize("argv", [["--expire", "-1"], ["--retain-last", "0"], ["--orphan-age-hours", "x"]])
def test_cli_rejects_bad_numbers(tmp_path, argv, capsys):
    with pytest.raises(SystemExit):
        _run(["maintain", str(_config(tmp_path)), *argv])


def test_add_cli_inherits_parent_options():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")
    parser = argparse.ArgumentParser(prog="ldp")
    maintain = add_cli(parser.add_subparsers(dest="command"), parents=[common])
    args = parser.parse_args(["maintain", "c.json", "-v"])
    assert args.verbose and args.config == "c.json" and callable(args.handler)
    assert maintain.prog.endswith("maintain")
