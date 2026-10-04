"""N processes upserting the same keys into one table on the local SQLite catalog.

Contract: ``docs/design/v0_2_0.md`` C3 and SaaS design §7.15. Three runs of the same workload:

* **raw pyiceberg** ``Table.upsert``, with no lock and no protocol. Every writer reads the same
  base, finds the round's keys missing and inserts them. The catalog CAS lets one commit win;
  pyiceberg 0.12's commit retry then *rebases* each loser's append onto the new head instead of
  failing it, so every key lands once per writer. That is the race, and the test proves it exists.
* **direct mode** ``Iceberg.put(mode="upsert")``, which takes the table's exclusive file lock on a
  ``local`` catalog: the read-modify-write is serialised, so there are no duplicates.
* **staged mode** ``Iceberg.put(mode="upsert", commit=CommitContext(...))``, i.e.
  ``write_once``: no lock at all; each publish asserts ``main == base``, losers recompute their
  upsert from the new ``main``. It must lose 0 rows and duplicate 0 keys, and each idempotency key
  must be exactly one effect on ``main``.

To make the race deterministic rather than timing-dependent, a barrier holds every writer just
before its commit (raw) or its first publish of a round (staged), so all writers in a round work
from the same base. The barrier only changes timing; it never changes what a writer does.

The workload size is ``LDP_RACE_PROCS`` processes (default 4) x ``LDP_RACE_ROUNDS`` rounds
(default 5). Everything is offline; processes use the ``spawn`` start method.
"""

import multiprocessing
import os
import queue
import threading
import traceback
import warnings
from pathlib import Path

import pyarrow as pa
import pytest

PROCS = max(4, int(os.environ.get("LDP_RACE_PROCS", "4")))
ROUNDS = max(5, int(os.environ.get("LDP_RACE_ROUNDS", "5")))
KEYS = 6
NAMESPACE = "race"
TABLE = "entities"
BARRIER_TIMEOUT_S = 60.0
JOIN_TIMEOUT_S = 600.0

SCHEMA = pa.schema([("id", pa.int64()), ("writer", pa.string()), ("round", pa.int64())])


# --------------------------------------------------------------------------- workload


def batch(workload: str, round_no: int, proc: int) -> pa.Table:
    """The rows process ``proc`` upserts in round ``round_no``; every process uses the same keys.

    ``insert``: round r upserts keys ``[r*KEYS, (r+1)*KEYS)``, all new to the table.
    ``mixed``: round r upserts keys ``[r*KEYS/2, r*KEYS/2 + KEYS)``: half of them were written in
    round r-1 (updates), half are new (inserts).
    """
    start = round_no * KEYS if workload == "insert" else round_no * (KEYS // 2)
    ids = list(range(start, start + KEYS))
    return pa.table({
        "id": pa.array(ids, pa.int64()),
        "writer": pa.array([f"p{proc}r{round_no}"] * len(ids), pa.string()),
        "round": pa.array([round_no] * len(ids), pa.int64()),
    }, schema=SCHEMA)


def expected_ids(workload: str, rounds: int) -> set[int]:
    return {i for r in range(rounds) for i in batch(workload, r, 0).column("id").to_pylist()}


def key_for(proc: int, round_no: int) -> str:
    return f"race-p{proc}-r{round_no}"


# --------------------------------------------------------------------------- worker (runs in a child process)


class _OncePerRound:
    """Wraps ``func`` so its first call after :meth:`arm` waits for every writer at a barrier."""

    def __init__(self, func, barrier):
        self.func, self.barrier, self.armed = func, barrier, False

    def arm(self):
        self.armed = True

    def __call__(self, *args, **kwargs):
        if self.armed:
            self.armed = False
            try:
                self.barrier.wait(BARRIER_TIMEOUT_S)
            except threading.BrokenBarrierError:
                pass  # a writer failed early; carry on unsynchronised
        return self.func(*args, **kwargs)


def race_worker(kind: str, workload: str, warehouse: str, proc: int, rounds: int, barrier, results) -> None:
    """One writer: ``rounds`` upserts of ``batch(workload, r, proc)`` using path ``kind``."""
    warnings.simplefilter("ignore")
    stats = {"proc": proc, "published": [], "skipped": 0, "run_retries": 0, "attempts": 0, "errors": [],
             "replayed_skipped": 0}
    try:
        if kind == "raw":
            _raw_rounds(workload, warehouse, proc, rounds, barrier, stats)
        elif kind == "locked":
            _locked_rounds(workload, warehouse, proc, rounds, barrier, stats)
        else:
            _staged_rounds(workload, warehouse, proc, rounds, barrier, stats)
    except BaseException as exc:  # noqa: BLE001 - reported to the parent
        stats["errors"].append(f"{type(exc).__name__}: {exc}")
        stats["traceback"] = traceback.format_exc()
    results.put(stats)


def _raw_rounds(workload, warehouse, proc, rounds, barrier, stats):
    from pyiceberg.table import Transaction

    from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog

    gate = _OncePerRound(Transaction.commit_transaction, barrier)
    Transaction.commit_transaction = lambda self: gate(self)
    catalog = LocalIcebergCatalog(NAMESPACE, path=warehouse)
    for round_no in range(rounds):
        gate.arm()
        try:
            catalog.load_table(f"{NAMESPACE}.{TABLE}").upsert(batch(workload, round_no, proc), join_cols=["id"])
            stats["published"].append(key_for(proc, round_no))
        except Exception as exc:  # noqa: BLE001 - a failed raw write is part of the evidence
            stats["errors"].append(f"{type(exc).__name__}: {exc}")


def _locked_rounds(workload, warehouse, proc, rounds, barrier, stats):
    from local_data_platform.format.iceberg import Iceberg

    table = Iceberg(TABLE, {"identifier": NAMESPACE, "warehouse_path": warehouse}, join_cols=["id"])
    for round_no in range(rounds):
        try:
            barrier.wait(BARRIER_TIMEOUT_S)
        except threading.BrokenBarrierError:
            pass
        table.put(batch(workload, round_no, proc), mode="upsert")
        stats["published"].append(key_for(proc, round_no))


def _staged_rounds(workload, warehouse, proc, rounds, barrier, stats):
    import local_data_platform.format.iceberg.commit as commit
    from local_data_platform.format.iceberg import Iceberg

    gate = _OncePerRound(commit.publish, barrier)
    commit.publish = gate
    policy = commit.CommitPolicy(max_publish_attempts=40, base_delay_s=0.01, max_delay_s=0.25, deadline_s=240.0)
    table = Iceberg(TABLE, {"identifier": NAMESPACE, "warehouse_path": warehouse}, join_cols=["id"])
    run_ids = {}
    for round_no in range(rounds):
        gate.arm()
        key = key_for(proc, round_no)
        run_ids[key] = commit.new_run_id()
        result = _coordinate(table, batch(workload, round_no, proc), key, run_ids[key], policy, stats)
        if result.skipped_duplicate:
            stats["skipped"] += 1
        else:
            stats["published"].append(key)
        stats["attempts"] += result.attempts
    # Re-running every key (a new run of the same windows) must be a no-op.
    for round_no in range(rounds):
        key = key_for(proc, round_no)
        replay = table.put(batch(workload, round_no, proc), mode="upsert",
                           commit=commit.CommitContext.create(key), policy=policy)
        stats["replayed_skipped"] += int(replay.skipped_duplicate)


def _coordinate(table, df, key, run_id, policy, stats):
    """What a coordinator does: retry a retriable conflict as the next attempt, up to 4 attempts."""
    from local_data_platform.format.iceberg.commit import CommitConflict, CommitContext

    for attempt in range(1, 5):
        ctx = CommitContext.create(key, run_id=run_id, attempt=attempt)
        try:
            return table.put(df, mode="upsert", commit=ctx, policy=policy)
        except CommitConflict as exc:
            if not exc.retriable or attempt == 4:
                raise
            stats["run_retries"] += 1
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- harness (parent process)


def run_race(kind: str, workload: str, workdir: Path, procs: int = PROCS, rounds: int = ROUNDS) -> dict:
    """Run ``procs`` writers for ``rounds`` rounds and return what landed in the table."""
    from local_data_platform.catalog.local.iceberg import LocalIcebergCatalog
    from local_data_platform.format.iceberg import Iceberg

    warehouse = str(workdir / f"{kind}_{workload}")
    if kind == "raw":
        with LocalIcebergCatalog(NAMESPACE, path=warehouse) as catalog:
            catalog.create_namespace_if_not_exists(NAMESPACE)
            catalog.create_table(f"{NAMESPACE}.{TABLE}", schema=SCHEMA)
    else:
        Iceberg(TABLE, {"identifier": NAMESPACE, "warehouse_path": warehouse}).catalog.close()

    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(procs), ctx.Queue()
    workers = [ctx.Process(target=race_worker, args=(kind, workload, warehouse, proc, rounds, barrier, results))
               for proc in range(procs)]
    for worker in workers:
        worker.start()
    stats = []
    try:
        for _ in workers:
            stats.append(results.get(timeout=JOIN_TIMEOUT_S))
    except queue.Empty:
        pytest.fail(f"{kind} race workers did not finish within {JOIN_TIMEOUT_S}s")
    finally:
        for worker in workers:
            worker.join(timeout=30)
            if worker.is_alive():
                worker.terminate()

    with LocalIcebergCatalog(NAMESPACE, path=warehouse) as catalog:
        table = catalog.load_table(f"{NAMESPACE}.{TABLE}")
        rows = table.scan().to_arrow()
        ids = rows.column("id").to_pylist()
        final = dict(zip(ids, rows.column("writer").to_pylist()))
        effects = main_effects(table)
    expected = expected_ids(workload, rounds)
    return {
        "kind": kind,
        "workload": workload,
        "procs": procs,
        "rounds": rounds,
        "writes": procs * rounds,
        "rows": len(ids),
        "distinct_keys": len(set(ids)),
        "duplicate_rows": len(ids) - len(set(ids)),
        "expected_keys": len(expected),
        "lost_keys": len(expected - set(ids)),
        "errors": [error for s in stats for error in s["errors"]],
        "tracebacks": [s["traceback"] for s in stats if s.get("traceback")],
        "published": sorted(key for s in stats for key in s["published"]),
        "skipped": sum(s["skipped"] for s in stats),
        "replayed_skipped": sum(s["replayed_skipped"] for s in stats),
        "run_retries": sum(s["run_retries"] for s in stats),
        "attempts": sum(s["attempts"] for s in stats),
        "effects": effects,
        "final": final,
    }


def main_effects(table) -> list[tuple[str, str, str]]:
    """``main``'s LDP effects, oldest first, as ``(key, run id, attempt)``.

    One publish may add several snapshots (an upsert adds an overwrite and appends), all
    carrying the same key, run id and attempt, so contiguous snapshots with the same triple are
    one effect.
    """
    from pyiceberg.table.snapshots import ancestors_of

    effects = []
    for snapshot in reversed(list(ancestors_of(table.current_snapshot(), table.metadata))):
        summary = snapshot.summary.additional_properties if snapshot.summary is not None else {}
        key = summary.get("ldp.idempotency-key")
        if key is None:
            continue
        effect = (key, summary.get("ldp.run-id"), summary.get("ldp.attempt"))
        if not effects or effects[-1] != effect:
            effects.append(effect)
    return effects


def replay(effects, workload: str) -> dict[int, str]:
    """Apply the published batches in ``main``'s commit order to a dict: the serial outcome."""
    state = {}
    for key, _, _ in effects:
        _, proc, round_no = key.split("-")
        df = batch(workload, int(round_no[1:]), int(proc[1:]))
        state.update(zip(df.column("id").to_pylist(), df.column("writer").to_pylist()))
    return state


# --------------------------------------------------------------------------- tests


def test_workload_shape():
    assert PROCS >= 4 and ROUNDS >= 5
    assert batch("insert", 1, 0).column("id").to_pylist() == list(range(KEYS, 2 * KEYS))
    mixed = [batch("mixed", r, 0).column("id").to_pylist() for r in (0, 1)]
    assert set(mixed[0]) & set(mixed[1]) and set(mixed[1]) - set(mixed[0])
    assert batch("insert", 0, 1).column("id") == batch("insert", 0, 2).column("id")


def test_raw_pyiceberg_upserts_race_and_duplicate_keys(tmp_path):
    report = run_race("raw", "insert", tmp_path)

    # The race exists: each round's keys land once per writer.
    assert report["duplicate_rows"] > 0, report
    assert report["duplicate_rows"] == report["rows"] - report["expected_keys"]
    assert report["distinct_keys"] == report["expected_keys"]
    assert report["rows"] == PROCS * ROUNDS * KEYS - sum(KEYS for e in report["errors"])


def test_direct_upsert_on_a_local_catalog_is_serialised_by_the_file_lock(tmp_path):
    report = run_race("locked", "insert", tmp_path)

    assert report["errors"] == []
    assert report["duplicate_rows"] == 0
    assert report["lost_keys"] == 0
    assert report["rows"] == report["expected_keys"]
    assert (tmp_path / "locked_insert" / ".ldp" / "locks" / f"{NAMESPACE}.{TABLE}.lock").is_file()


@pytest.mark.parametrize("workload", ["insert", "mixed"])
def test_write_once_loses_no_rows_and_duplicates_no_keys(tmp_path, workload):
    report = run_race("staged", workload, tmp_path)
    all_keys = sorted(key_for(p, r) for p in range(PROCS) for r in range(ROUNDS))

    assert report["errors"] == []
    # 0 duplicate keys, 0 lost rows.
    assert report["duplicate_rows"] == 0, report
    assert report["lost_keys"] == 0, report
    assert report["rows"] == report["expected_keys"]
    # Every write published exactly once: one effect per idempotency key on main (invariant I2).
    effect_keys = [key for key, _, _ in report["effects"]]
    assert sorted(effect_keys) == all_keys
    assert report["published"] == all_keys and report["skipped"] == 0
    # No lost update: the table equals the published batches applied serially in main's order.
    assert report["final"] == replay(report["effects"], workload)
    # The barrier forced conflicts, which write_once resolved by re-staging.
    assert report["attempts"] > PROCS * ROUNDS
    # Re-running every key is a no-op.
    assert report["replayed_skipped"] == PROCS * ROUNDS
