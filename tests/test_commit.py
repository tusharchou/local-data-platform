"""Tests for the staged, exactly-once publish protocol (``format/iceberg/commit.py``).

Contract: ``docs/design/v0_1_1_platform.md`` C3 and SaaS design §7.1-§7.8. All tests run offline on the
local SQLite catalog. Concurrency across processes is in ``test_upsert_race.py``; here a
"concurrent writer" is injected deterministically through the ``audit`` hook, which
``write_once`` calls after staging and before publishing.
"""

import argparse
import dataclasses
import datetime as dt
import inspect
import json
import re
import time
import uuid

import pyarrow as pa
import pytest
from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException
from pyiceberg.table import Table as PyIcebergTable
from pyiceberg.table.snapshots import ancestors_of
from pyiceberg.table.update.snapshot import ManageSnapshots
from pyiceberg.types import StringType

import local_data_platform.format.iceberg.commit as commit
from local_data_platform.exceptions import ConfigError, DataQualityError, EngineNotFound, LDPError
from local_data_platform.format.iceberg import Iceberg, WriteResult
from local_data_platform.format.iceberg.commit import (
    CommitConflict,
    CommitContext,
    CommitPolicy,
    CommitSearchExhausted,
    StagedWrite,
    attempt_prefix,
    branch_name,
    ensure_base,
    fence,
    fence_marker,
    find_commit,
    publish,
    stage,
    staged_branches,
    write_once,
)
from local_data_platform.quality import CheckResult, QualityReport

FAST = CommitPolicy(max_publish_attempts=6, base_delay_s=0.0, max_delay_s=0.0)
BRANCH_RE = re.compile(r"ldp_r[0-9a-f]{32}_a\d+_\d+")


def rows(ids, tag="v", day="a"):
    ids = list(ids)
    return pa.table({
        "id": pa.array(ids, pa.int64()),
        "value": pa.array([f"{tag}{i}" for i in ids], pa.string()),
        "day": pa.array([day] * len(ids), pa.string()),
    })


def ctx(key="K", attempt=1, run_id=None):
    return CommitContext.create(key, run_id=run_id, attempt=attempt, spec_hash="spec-1")


def state(table: Iceberg) -> dict[int, str]:
    df = table.get()
    return dict(zip(df.column("id").to_pylist(), df.column("value").to_pylist()))


def snapshot_count(table: Iceberg) -> int:
    return len(table.table().snapshots())


def ldp_snapshots(table: Iceberg, key: str) -> list[dict]:
    """Snapshots in ``main``'s ancestry (not merely in the metadata) that carry ``key``, oldest first."""
    on_main = {s.snapshot_id for s in ancestors_of(table.table().current_snapshot(), table.table().metadata)}
    return [s for s in table.snapshots()
            if s["snapshot_id"] in on_main and s["summary"].get("ldp.idempotency-key") == key]


def added_files(pyiceberg_table, snapshot_id: int) -> list[str]:
    snapshot = pyiceberg_table.snapshot_by_id(snapshot_id)
    paths = []
    for manifest in snapshot.manifests(pyiceberg_table.io):
        if manifest.added_snapshot_id != snapshot_id:
            continue
        paths += [entry.data_file.file_path for entry in manifest.fetch_manifest_entry(pyiceberg_table.io)
                  if entry.snapshot_id == snapshot_id]
    return sorted(paths)


@pytest.fixture
def table(catalog_config):
    return Iceberg("entities", catalog_config, join_cols=["id"])


@pytest.fixture
def seeded(table):
    """``entities`` with ids 1-3 published under key ``seed``."""
    table.put(rows([1, 2, 3]), mode="append", commit=ctx("seed"))
    return table


def once(action):
    """An ``audit`` hook that runs ``action()`` on its first call only and always passes."""
    calls = []

    def audit(pyiceberg_table, staged_snapshot_id):
        calls.append(staged_snapshot_id)
        if len(calls) == 1:
            action()
        return QualityReport()

    audit.calls = calls
    return audit


# --------------------------------------------------------------------------- interfaces


def test_interfaces_match_the_saas_contract():
    fields = {cls: [f.name for f in dataclasses.fields(cls)] for cls in (CommitContext, CommitPolicy, StagedWrite)}
    assert fields[CommitContext] == ["run_id", "attempt", "idempotency_key", "spec_hash", "search_since_ms",
                                     "logical_window", "source_positions"]
    assert fields[CommitPolicy] == ["max_publish_attempts", "base_delay_s", "max_delay_s", "deadline_s",
                                    "max_search_depth"]
    assert fields[StagedWrite] == ["branch", "base_snapshot_id", "staged_snapshot_id", "schema_id", "rows_written"]
    assert CommitPolicy() == CommitPolicy(6, 0.2, 8.0, 300.0, 500)

    def params(func):
        return list(inspect.signature(func).parameters)

    assert params(branch_name) == ["run_id", "attempt", "n"]
    assert params(ensure_base) == ["table"]
    assert params(find_commit) == ["table", "key", "since_ms", "max_depth"]
    assert params(fence) == ["catalog", "table", "branches"]
    assert params(stage)[:7] == ["table", "df", "mode", "ctx", "join_cols", "overwrite_filter", "n"]
    assert params(publish) == ["catalog", "table", "staged"]
    assert params(write_once)[:7] == ["load", "catalog", "df", "mode", "ctx", "audit", "policy"]
    assert params(Iceberg.put)[:5] == ["self", "df", "mode", "commit", "overwrite_filter"]
    assert issubclass(CommitConflict, LDPError) and issubclass(CommitSearchExhausted, LDPError)
    assert CommitConflict("x").retriable is True
    assert CommitConflict("x", retriable=False).retriable is False
    for name in ("branch", "idempotency_key", "attempts", "skipped_duplicate"):
        assert name in {f.name for f in dataclasses.fields(WriteResult)}


def test_branch_names_avoid_slash_and_dash():
    run = uuid.uuid4()

    name = branch_name(str(run), 2, 3)

    assert name == f"ldp_r{run.hex}_a2_3"
    assert BRANCH_RE.fullmatch(name) and "-" not in name and "/" not in name
    assert branch_name(run.hex, 2, 3) == name
    hashed = branch_name("nightly-2024-01-01", 1)
    assert BRANCH_RE.fullmatch(hashed) and hashed == branch_name("nightly-2024-01-01", 1, 0)
    assert attempt_prefix(str(run), 2) == f"ldp_r{run.hex}_a2"
    assert fence_marker(str(run), 2) == f"ldp_r{run.hex}_a2_fenced"
    with pytest.raises(ValueError):
        branch_name(run.hex, 0)
    with pytest.raises(ValueError):
        branch_name(run.hex, 1, -1)


def test_commit_context_validates_and_builds_snapshot_properties():
    start, end = dt.datetime(2024, 1, 1), dt.datetime(2024, 1, 2)
    context = CommitContext.create("K", run_id="r1", attempt=2, spec_hash="h", now_ms=10 * 86_400_000,
                                   logical_window=(start, end), source_positions={"kafka": '{"0": 5}'})

    assert context.search_since_ms == 3 * 86_400_000  # 7-day idempotency horizon
    assert context.snapshot_properties() == {
        "ldp.idempotency-key": "K", "ldp.run-id": "r1", "ldp.attempt": "2", "ldp.spec-hash": "h",
        "ldp.logical-window": "2024-01-01T00:00:00/2024-01-02T00:00:00", "ldp.source.kafka": '{"0": 5}',
    }
    assert re.fullmatch(r"[0-9a-f]{32}", CommitContext.create("K").run_id)
    for bad in ({"attempt": 0}, {"attempt": True}, {"idempotency_key": ""}, {"run_id": ""}, {"search_since_ms": -1}):
        with pytest.raises(ValueError):
            CommitContext(**{"run_id": "r", "attempt": 1, "idempotency_key": "K", "spec_hash": "",
                             "search_since_ms": 0, **bad})
    with pytest.raises(ValueError):
        CommitPolicy(max_publish_attempts=0)


# --------------------------------------------------------------------------- bootstrap, stage, publish


def test_ensure_base_appends_one_empty_init_snapshot(table, catalog_config):
    pyiceberg_table = table.catalog.create_table(table.identifier, schema=rows([1]).schema)

    base = ensure_base(pyiceberg_table)
    again = ensure_base(table.table())

    assert again == base
    snapshots = table.snapshots()
    assert len(snapshots) == 1
    assert snapshots[0]["summary"]["ldp.init"] == "true"
    assert snapshots[0]["summary"]["total-records"] == "0"


def test_staged_put_publishes_with_ldp_snapshot_properties(table):
    context = ctx("K1")

    result = table.put(rows([1, 2, 3]), mode="append", commit=context)

    assert (result.mode, result.rows_written, result.rows_before, result.rows_after) == ("append", 3, 0, 3)
    assert result.table_identifier == "test_ns.entities"
    assert BRANCH_RE.fullmatch(result.branch) and result.branch == branch_name(context.run_id, 1, 0)
    assert (result.idempotency_key, result.attempts, result.skipped_duplicate) == ("K1", 1, False)
    published = ldp_snapshots(table, "K1")
    assert [s["snapshot_id"] for s in published] == [result.snapshot_id]
    assert published[0]["summary"]["ldp.run-id"] == context.run_id
    assert published[0]["summary"]["ldp.attempt"] == "1"
    assert published[0]["summary"]["ldp.spec-hash"] == "spec-1"
    # The first snapshot is the ldp.init base; the staging branch is gone after the publish.
    assert table.snapshots()[0]["summary"]["ldp.init"] == "true"
    assert list(table.table().metadata.refs) == ["main"]
    assert table.table().current_snapshot().snapshot_id == result.snapshot_id


@pytest.mark.parametrize("mode", ["append", "overwrite", "upsert"])
def test_rerunning_the_same_key_is_a_no_op(seeded, mode):
    first = seeded.put(rows([3, 4], tag="w"), mode=mode, commit=ctx("K2"))
    before = (snapshot_count(seeded), seeded.table().metadata_location, state(seeded))

    again = seeded.put(rows([3, 4], tag="w"), mode=mode, commit=ctx("K2"))  # a new run, same key

    assert again.skipped_duplicate is True
    assert (again.rows_written, again.attempts, again.branch) == (0, 0, None)
    assert again.snapshot_id == first.snapshot_id
    assert again.rows_before == again.rows_after == first.rows_after
    assert (snapshot_count(seeded), seeded.table().metadata_location, state(seeded)) == before


def test_upsert_that_changes_nothing_still_records_its_key(seeded):
    result = seeded.put(rows([1, 2]), mode="upsert", commit=ctx("same"))

    assert result.rows_written == 0 and result.rows_after == 3
    assert ldp_snapshots(seeded, "same")
    assert seeded.put(rows([1, 2]), mode="upsert", commit=ctx("same")).skipped_duplicate


def test_staged_counts_come_from_snapshot_summaries_not_scans(seeded, monkeypatch):
    def no_scan(*args, **kwargs):
        raise AssertionError("row counts must come from snapshot summaries")

    monkeypatch.setattr(PyIcebergTable, "scan", no_scan)

    appended = seeded.put(rows([4, 5]), mode="append", commit=ctx("a"))
    replaced = seeded.put(rows([9]), mode="overwrite", commit=ctx("o"))

    assert (appended.rows_before, appended.rows_after) == (3, 5)
    assert (replaced.rows_before, replaced.rows_after, replaced.rows_written) == (5, 1, 1)


def test_publish_asserts_both_main_and_the_branch(seeded):
    """SaaS §7.1 fact 4: a direct commit_table keeps every AssertRefSnapshotId."""
    catalog, pyiceberg_table = seeded.catalog, seeded.table()
    staged_a = stage(pyiceberg_table, rows([10]), "append", ctx("A"))
    staged_b = stage(seeded.table(), rows([20]), "append", ctx("B"))
    assert staged_a.base_snapshot_id == staged_b.base_snapshot_id

    published = publish(catalog, seeded.table(), staged_a)

    assert published.snapshot_id == staged_a.staged_snapshot_id
    assert staged_a.branch not in seeded.table().metadata.refs  # removed in the same commit
    with pytest.raises(CommitFailedException, match="main has changed"):
        publish(catalog, seeded.table(), staged_b)
    # A branch whose base is current but which was removed (fenced) cannot publish either.
    staged_c = stage(seeded.table(), rows([30]), "append", ctx("C"))
    seeded.table().manage_snapshots().remove_branch(staged_c.branch).commit()
    with pytest.raises(CommitFailedException, match="missing"):
        publish(catalog, seeded.table(), staged_c)
    assert state(seeded) == {1: "v1", 2: "v2", 3: "v3", 10: "v10"}


def test_transaction_stage_drops_a_repeated_requirement_type(seeded):
    """SaaS §7.1 fact 3, the reason publish() never goes through a Transaction.

    pyiceberg 0.12.0 ``Transaction._stage`` (pyiceberg/table/__init__.py lines 293-296) keeps
    only the first staged requirement of each type.
    """
    pyiceberg_table = seeded.table()
    base = pyiceberg_table.current_snapshot().snapshot_id
    pyiceberg_table.manage_snapshots().create_branch(base, "b").commit()
    tx = seeded.table().transaction()
    snapshots = ManageSnapshots(tx)
    snapshots.set_current_snapshot(snapshot_id=base)  # stages AssertRefSnapshotId(main)
    snapshots.remove_branch("b")                       # AssertRefSnapshotId(b) ...
    snapshots._commit_if_ref_updates_exist()           # ... is dropped by this second _stage call

    refs = [getattr(r, "ref", None) for r in tx._requirements]
    if refs.count("b") == 1:
        pytest.skip("this pyiceberg keeps repeated requirement types; publish() stays correct either way")
    assert refs == ["main"]


def test_stage_writes_only_to_its_branch_and_unions_the_schema_in_the_same_commit(seeded):
    before = len(seeded.table().metadata.metadata_log)
    wider = rows([4]).append_column("tip", pa.array([1.5]))

    staged = stage(seeded.table(), wider, "append", ctx("S"))

    after = seeded.table()
    assert BRANCH_RE.fullmatch(staged.branch) and staged.rows_written == 1
    assert after.current_snapshot().snapshot_id == staged.base_snapshot_id  # main untouched
    assert after.metadata.refs[staged.branch].snapshot_id == staged.staged_snapshot_id
    assert "tip" in after.schema().column_names and staged.schema_id == after.metadata.current_schema_id
    # One commit creates the branch, one holds the schema union and the data.
    assert len(after.metadata.metadata_log) == before + 2
    assert staged_branches(after) == {staged.branch: staged.staged_snapshot_id}


def test_a_branch_that_no_longer_descends_from_base_is_never_published(seeded):
    """pyiceberg's retry re-creates a removed branch with a parentless snapshot; stage must refuse it."""
    pyiceberg_table = seeded.table()
    base = pyiceberg_table.current_snapshot().snapshot_id
    with pyiceberg_table.transaction() as tx:
        tx.append(rows([99]), branch="orphan")  # a branch that did not exist: parentless snapshot
    orphan = seeded.table().metadata.refs["orphan"].snapshot_id
    assert seeded.table().snapshot_by_id(orphan).parent_snapshot_id is None

    with pytest.raises(CommitConflict, match="no longer descends") as error:
        commit._check_descends(seeded.table(), "orphan", orphan, base)
    assert error.value.retriable is False


# --------------------------------------------------------------------------- find_commit


def test_find_commit_walks_main_and_honours_time_and_depth_bounds(table):
    for i in range(4):
        table.put(rows([i]), mode="append", commit=ctx(f"K{i}"))
    pyiceberg_table = table.table()
    head = pyiceberg_table.current_snapshot()

    assert find_commit(pyiceberg_table, "K3", since_ms=0).snapshot_id == head.snapshot_id
    assert find_commit(pyiceberg_table, "K0", since_ms=0) is not None
    assert find_commit(pyiceberg_table, "absent", since_ms=0) is None  # reached the root
    # Snapshots older than since_ms end the search (the head is checked first).
    assert find_commit(pyiceberg_table, "K0", since_ms=head.timestamp_ms + 1) is None
    assert find_commit(pyiceberg_table, "K3", since_ms=head.timestamp_ms + 1) is not None
    with pytest.raises(CommitSearchExhausted, match="max_search_depth"):
        find_commit(pyiceberg_table, "K0", since_ms=0, max_depth=2)


def test_find_commit_fails_closed_on_truncated_history():
    class Snap:
        def __init__(self, snapshot_id, parent, ts):
            self.snapshot_id, self.parent_snapshot_id, self.timestamp_ms, self.summary = snapshot_id, parent, ts, None

    head = Snap(2, 1, 1_000)  # its parent 1 was expired

    class Metadata:
        @staticmethod
        def snapshot_by_id(snapshot_id):
            return None

    class Table:
        metadata = Metadata()

        @staticmethod
        def current_snapshot():
            return head

        @staticmethod
        def name():
            return ("ns", "t")

    with pytest.raises(CommitSearchExhausted, match="truncated"):
        find_commit(Table(), "K", since_ms=0)
    assert find_commit(Table(), "K", since_ms=5_000) is None


def test_write_once_refuses_to_publish_when_the_search_is_exhausted(seeded):
    seeded.put(rows([4]), mode="append", commit=ctx("K4"))
    before = snapshot_count(seeded)

    with pytest.raises(CommitSearchExhausted):
        seeded.put(rows([5]), mode="append", commit=ctx("K5"),
                   policy=CommitPolicy(max_search_depth=1, base_delay_s=0, max_delay_s=0))
    assert snapshot_count(seeded) == before and staged_branches(seeded.table()) == {}


# --------------------------------------------------------------------------- conflict matrix (SaaS §7.6)


def test_append_conflict_restages_with_the_data_files_it_already_wrote(seeded):
    other = Iceberg("entities", seeded.catalog_spec, join_cols=["id"])
    audit = once(lambda: other.put(rows([50]), mode="append"))

    result = write_once(seeded.table, seeded.catalog, rows([7, 8]), "append", ctx("A"), audit=audit, policy=FAST)

    assert result.attempts == 2 and not result.skipped_duplicate
    assert (result.rows_before, result.rows_after, result.rows_written) == (4, 6, 2)
    assert state(seeded).keys() == {1, 2, 3, 50, 7, 8}
    table = seeded.table()
    first, second = audit.calls
    assert added_files(table, second) == added_files(table, first)  # no data rewritten
    assert result.snapshot_id == second and len(ldp_snapshots(seeded, "A")) == 1


def test_full_overwrite_conflict_is_reapplied_from_the_new_base(seeded):
    other = Iceberg("entities", seeded.catalog_spec)
    audit = once(lambda: other.put(rows([50]), mode="append"))

    result = write_once(seeded.table, seeded.catalog, rows([9], tag="new"), "overwrite", ctx("O"), audit=audit,
                        policy=FAST)

    assert result.attempts == 2
    assert state(seeded) == {9: "new9"}  # last writer wins for a full overwrite
    assert (result.rows_before, result.rows_after) == (4, 1)


def test_window_overwrite_conflict_replaces_only_its_window(table):
    table.put(pa.concat_tables([rows([1, 2], day="a"), rows([3], day="b")]), mode="append", commit=ctx("seed"))
    other = Iceberg("entities", table.catalog_spec)
    audit = once(lambda: other.put(rows([4], day="b"), mode="append"))

    result = write_once(table.table, table.catalog, rows([10], tag="new", day="a"), "overwrite", ctx("W"),
                        audit=audit, policy=FAST, overwrite_filter="day == 'a'")

    assert result.attempts == 2
    assert state(table) == {10: "new10", 3: "v3", 4: "v4"}


def test_upsert_conflict_is_recomputed_from_the_new_base(seeded):
    other = Iceberg("entities", seeded.catalog_spec, join_cols=["id"])
    audit = once(lambda: other.put(rows([1, 6], tag="other"), mode="upsert"))

    result = write_once(seeded.table, seeded.catalog, rows([1, 2, 7], tag="ours"), "upsert", ctx("U"),
                        join_cols=["id"], audit=audit, policy=FAST)

    assert result.attempts == 2
    final = state(seeded)
    assert final == {1: "ours1", 2: "ours2", 3: "v3", 6: "other6", 7: "ours7"}
    assert seeded.table().scan().to_arrow().num_rows == len(final)  # no duplicate keys
    assert result.rows_written == 3 and (result.rows_before, result.rows_after) == (4, 5)


def test_concurrent_schema_change_is_unioned_again_and_restaged(seeded):
    def add_column():
        with seeded.table().update_schema() as update:
            update.add_column("note", StringType())

    audit = once(add_column)

    result = write_once(seeded.table, seeded.catalog, rows([4]), "append", ctx("S"), audit=audit, policy=FAST)

    assert result.attempts == 2
    table = seeded.table()
    assert "note" in table.schema().column_names
    first, second = audit.calls
    assert table.snapshot_by_id(second).schema_id == table.metadata.current_schema_id
    assert set(state(seeded)) == {1, 2, 3, 4}


def test_running_out_of_publish_attempts_is_a_retriable_conflict(seeded):
    other = Iceberg("entities", seeded.catalog_spec)
    counter = iter(range(100, 200))

    def always_conflict(pyiceberg_table, staged_snapshot_id):
        other.put(rows([next(counter)]), mode="append")
        return QualityReport()

    policy = CommitPolicy(max_publish_attempts=3, base_delay_s=0, max_delay_s=0)
    with pytest.raises(CommitConflict, match="attempt 2") as error:
        write_once(seeded.table, seeded.catalog, rows([7]), "append", ctx("X"), audit=always_conflict, policy=policy)

    assert error.value.retriable is True
    assert ldp_snapshots(seeded, "X") == [] and 7 not in state(seeded)
    assert staged_branches(seeded.table()) == {}


def test_a_spurious_cas_loss_republishes_the_same_staged_snapshot(seeded, monkeypatch):
    real = commit.publish
    calls = []

    def flaky(catalog, pyiceberg_table, staged):
        calls.append(staged.staged_snapshot_id)
        if len(calls) == 1:
            raise CommitFailedException("Table has been updated by another process")
        return real(catalog, pyiceberg_table, staged)

    monkeypatch.setattr(commit, "publish", flaky)

    result = seeded.put(rows([4]), mode="append", commit=ctx("R"), policy=FAST)

    assert calls[0] == calls[1] == result.snapshot_id and result.attempts == 2


def test_a_lost_publish_response_is_recognised_as_our_own_publish(seeded, monkeypatch):
    real = commit.publish

    def landed_but_unknown(catalog, pyiceberg_table, staged):
        real(catalog, pyiceberg_table, staged)
        raise CommitStateUnknownException("gateway timeout")

    monkeypatch.setattr(commit, "publish", landed_but_unknown)

    result = seeded.put(rows([4]), mode="append", commit=ctx("L"), policy=FAST)

    assert result.skipped_duplicate is False and result.rows_written == 1
    assert len(ldp_snapshots(seeded, "L")) == 1


# --------------------------------------------------------------------------- fencing (SaaS §7.8)


def test_zombie_attempt_is_fenced_and_its_successor_publishes_once(seeded):
    run = commit.new_run_id()
    zombie = stage(seeded.table(), rows([7], tag="zombie"), "append", ctx("Z", attempt=1, run_id=run))

    successor = seeded.put(rows([7], tag="succ"), mode="append", commit=ctx("Z", attempt=2, run_id=run), policy=FAST)

    refs = seeded.table().metadata.refs
    assert zombie.branch not in refs and fence_marker(run, 1) in refs
    assert successor.branch == branch_name(run, 2, 0)
    # The zombie wakes up: its publish fails, and re-running it is a no-op, not a second effect.
    with pytest.raises(CommitFailedException):
        publish(seeded.catalog, seeded.table(), zombie)
    replay = seeded.put(rows([7], tag="zombie"), mode="append", commit=ctx("Z", attempt=1, run_id=run), policy=FAST)
    assert replay.skipped_duplicate and replay.snapshot_id == successor.snapshot_id
    assert state(seeded)[7] == "succ7" and seeded.get().num_rows == 4
    assert len({(s["summary"]["ldp.run-id"], s["summary"]["ldp.attempt"]) for s in ldp_snapshots(seeded, "Z")}) == 1


def test_a_fenced_attempt_can_neither_publish_nor_stage_again(seeded):
    run = commit.new_run_id()
    zombie = stage(seeded.table(), rows([7]), "append", ctx("F", attempt=1, run_id=run))
    head = seeded.table().current_snapshot().snapshot_id

    fence(seeded.catalog, seeded.table(), [attempt_prefix(run, 1)])  # the successor's first commit

    with pytest.raises(CommitFailedException):
        publish(seeded.catalog, seeded.table(), zombie)
    with pytest.raises(CommitFailedException, match="fenced"):
        stage(seeded.table(), rows([7]), "append", ctx("F", attempt=1, run_id=run), n=1)
    with pytest.raises(CommitConflict, match="fenced") as error:
        seeded.put(rows([7]), mode="append", commit=ctx("F", attempt=1, run_id=run), policy=FAST)
    assert error.value.retriable is False
    assert seeded.table().current_snapshot().snapshot_id == head and ldp_snapshots(seeded, "F") == []


@pytest.mark.parametrize("landed", [True, False], ids=["fence-landed", "fence-lost"])
def test_a_fence_with_an_unknown_outcome_is_reconciled_and_the_write_goes_on(seeded, monkeypatch, landed):
    run = commit.new_run_id()
    zombie = stage(seeded.table(), rows([7], tag="zombie"), "append", ctx("G", attempt=1, run_id=run))
    real = seeded.catalog.commit_table
    fences = []

    def first_fence_times_out(pyiceberg_table, requirements, updates):
        if any(str(getattr(update, "ref_name", "")).endswith("_fenced") for update in updates):
            fences.append(updates)
            if len(fences) == 1:
                if landed:
                    real(pyiceberg_table, requirements, updates)
                raise CommitStateUnknownException("gateway timeout")
        return real(pyiceberg_table, requirements, updates)

    monkeypatch.setattr(seeded.catalog, "commit_table", first_fence_times_out)

    successor = seeded.put(rows([7], tag="succ"), mode="append", commit=ctx("G", attempt=2, run_id=run), policy=FAST)

    assert len(fences) == (1 if landed else 2)  # a fence that landed is not committed again
    refs = seeded.table().metadata.refs
    assert zombie.branch not in refs and fence_marker(run, 1) in refs
    assert not successor.skipped_duplicate and state(seeded)[7] == "succ7"
    with pytest.raises(CommitFailedException):
        publish(seeded.catalog, seeded.table(), zombie)


def test_fence_is_idempotent_covers_unstaged_attempts_and_never_removes_main(seeded):
    run = commit.new_run_id()
    targets = [attempt_prefix(run, 1), branch_name(run, 2, 0), "not_an_ldp_branch"]

    fence(seeded.catalog, seeded.table(), targets)
    location = seeded.table().metadata_location
    fence(seeded.catalog, seeded.table(), targets)

    refs = seeded.table().metadata.refs
    assert {fence_marker(run, 1), fence_marker(run, 2)} <= set(refs)
    assert seeded.table().metadata_location == location  # the second fence had nothing to do
    with pytest.raises(ConfigError):
        fence(seeded.catalog, seeded.table(), ["main"])


def test_superseded_run_is_fenced_through_fence_branches(seeded):
    old_run = commit.new_run_id()
    stale = stage(seeded.table(), rows([7], tag="old"), "overwrite", ctx("R1", run_id=old_run))

    seeded.put(rows([8]), mode="append", commit=ctx("R2"))  # a newer run lands first
    newer = write_once(seeded.table, seeded.catalog, rows([9]), "append", ctx("R3"), policy=FAST,
                       fence_branches=[attempt_prefix(old_run, 1)])

    assert not newer.skipped_duplicate and stale.branch not in seeded.table().metadata.refs
    with pytest.raises(CommitFailedException):
        publish(seeded.catalog, seeded.table(), stale)


# --------------------------------------------------------------------------- quality, validation, versions


def test_failed_audit_blocks_the_publish_and_keeps_the_branch(seeded):
    def failing(pyiceberg_table, staged_snapshot_id):
        return QualityReport([CheckResult("row_count", False, "too few rows")])

    head = seeded.table().current_snapshot().snapshot_id

    with pytest.raises(DataQualityError, match="kept for inspection") as error:
        write_once(seeded.table, seeded.catalog, rows([4]), "append", ctx("Q"), audit=failing, policy=FAST)

    assert error.value.report.passed is False
    assert seeded.table().current_snapshot().snapshot_id == head
    assert len(staged_branches(seeded.table())) == 1


def test_staged_put_validates_its_options(table):
    with pytest.raises(ConfigError, match="overwrite_filter"):
        table.put(rows([1]), mode="append", commit=ctx(), overwrite_filter="id > 0")
    with pytest.raises(ConfigError, match="join_cols"):
        Iceberg("other", table.catalog_spec).put(rows([1]), mode="upsert", commit=ctx())
    with pytest.raises(ConfigError, match="unknown write mode"):
        write_once(table.table, table.catalog, rows([1]), "merge", ctx())
    with pytest.raises(TypeError):
        write_once(lambda: None, table.catalog, rows([1]), "append", {"key": "K"})
    assert not table.exists()


def test_staged_protocol_needs_branch_writes(table, monkeypatch):
    monkeypatch.setattr(commit, "_branch_writes_supported", lambda: False)

    with pytest.raises(EngineNotFound, match="pyiceberg>=0.11"):
        table.put(rows([1]), commit=ctx())


def test_sequence_number_races_are_treated_as_lost_races():
    race = ValueError("Cannot add snapshot with sequence number 4 older than last sequence number 4")

    assert commit.is_sequence_race(race) and not commit.is_sequence_race(ValueError("other"))
    with pytest.raises(CommitFailedException, match="sequence number"):
        with commit.sequence_race_is_a_conflict():
            raise race
    with pytest.raises(ValueError, match="other"):
        with commit.sequence_race_is_a_conflict():
            raise ValueError("other")


def test_backoff_is_full_jitter_within_the_cap():
    policy = CommitPolicy(base_delay_s=0.2, max_delay_s=1.0)

    delays = [policy.backoff_s(attempt) for attempt in range(1, 10) for _ in range(20)]

    assert all(0.0 <= d <= 1.0 for d in delays)
    assert max(policy.backoff_s(1) for _ in range(50)) <= 0.2


def test_write_once_works_on_a_plain_sql_catalog(tmp_path):
    from pyiceberg.catalog.sql import SqlCatalog

    catalog = SqlCatalog("plain", uri=f"sqlite:///{tmp_path}/plain.db", warehouse=f"file://{tmp_path}")
    catalog.create_namespace("ns")
    table = Iceberg("t", {"type": "sql", "namespace": "ns"}, catalog_obj=catalog, join_cols=["id"])

    first = table.put(rows([1, 2]), mode="upsert", commit=ctx("P"))
    second = table.put(rows([1, 2]), mode="upsert", commit=ctx("P"))

    assert table.lock_path() is None and table.path is None
    assert first.rows_after == 2 and second.skipped_duplicate
    catalog.engine.dispose()


# --------------------------------------------------------------------------- CLI


def test_commits_cli_lists_publishes_and_branches(tmp_path, capsys):
    config = tmp_path / "entities.json"
    config.write_text(json.dumps({"identifier": "entities", "metadata": {
        "source": {"name": "entities", "format": "CSV", "path": "entities.csv"},
        "target": {"name": "entities", "format": "ICEBERG",
                   "catalog": {"identifier": "cli_ns", "warehouse_path": "warehouse"}},
    }}))
    table = Iceberg("entities", {"identifier": "cli_ns", "warehouse_path": "warehouse"}, base_dir=tmp_path)
    table.put(rows([1]), mode="append", commit=ctx("cli-key"))
    stage(table.table(), rows([2]), "append", ctx("pending"))
    parser = argparse.ArgumentParser()
    commit.add_cli(parser.add_subparsers())

    args = parser.parse_args(["commits", str(config), "--branches"])
    assert args.handler(args) == 0

    out = capsys.readouterr().out
    assert "cli-key" in out and "Staging branches" in out and "ldp_r" in out
    rows_ = commit.commit_log(table.table(), key="cli-key")
    assert len(rows_) == 1 and rows_[0]["attempt"] == "1"


def test_default_search_horizon_is_seven_days():
    assert CommitContext.create("K", now_ms=0).search_since_ms == 0
    expected = (time.time() - commit.IDEMPOTENCY_HORIZON_S) * 1000
    assert abs(CommitContext.create("K").search_since_ms - expected) < 60_000


def test_staged_upsert_batch_missing_a_table_column_is_a_config_error_and_leaves_no_branch(seeded):
    before = snapshot_count(seeded)

    with pytest.raises(ConfigError, match=r"missing columns \['day'\]"):
        seeded.put(rows([2, 9]).drop_columns(["day"]), mode="upsert", commit=ctx("partial"))

    assert snapshot_count(seeded) == before
    assert staged_branches(seeded.table()) == {}
    assert state(seeded) == {1: "v1", 2: "v2", 3: "v3"}
