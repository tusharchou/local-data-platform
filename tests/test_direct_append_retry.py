"""Direct-mode appends that lose a commit race are retried on the new head (#88).

pyiceberg 0.11 has no commit retry, so an append that lost the race used to fail with
``CommitFailedException``. pyiceberg 0.12 retries inside its own commit. These tests inject the
failure at ``Transaction.commit_transaction``, the outer method on both versions, so the retry
that runs is LDP's and the tests mean the same on 0.11 and 0.12.
"""

import math

import pytest
from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException
from pyiceberg.table import Transaction

from local_data_platform.format.iceberg import Iceberg
from local_data_platform.format.iceberg.commit import CommitConflict, CommitPolicy

FAST = CommitPolicy(max_publish_attempts=6, base_delay_s=0.0, max_delay_s=0.0)
LOST_RACE = "Table has been updated by another process"


@pytest.fixture
def table(catalog_config, make_table) -> Iceberg:
    """A table of 5 rides (ids 1 to 5)."""
    ice = Iceberg("rides", catalog_config)
    ice.put(make_table(5), mode="append")
    return ice


def fail_commits(monkeypatch, failures: float, error: Exception | None = None) -> list[int]:
    """Make the first ``failures`` commits raise ``error``, before they reach the catalog.

    Returns:
        A list that gets one item per commit attempted, so ``len(calls)`` counts the tries.
    """
    real = Transaction.commit_transaction
    calls: list[int] = []

    def flaky(self):
        calls.append(1)
        if len(calls) <= failures:
            raise error if error is not None else CommitFailedException(LOST_RACE)
        return real(self)

    monkeypatch.setattr(Transaction, "commit_transaction", flaky)
    return calls


def ride_ids(ice: Iceberg) -> list[int]:
    return sorted(ice.get().column("ride_id").to_pylist())


def test_an_append_that_does_not_race_takes_one_attempt(table, make_table):
    result = table.put(make_table(3, start_id=100), mode="append")

    assert result.attempts == 1 and result.rows_before == 5 and result.rows_after == 8


def test_a_lost_commit_race_is_retried_on_the_new_head(table, make_table, catalog_config, monkeypatch):
    rival = Iceberg("rides", catalog_config)
    real = Transaction.commit_transaction
    state = {"calls": 0, "rival_is_writing": False}

    def lose_once_to_a_rival(self):
        if state["rival_is_writing"]:
            return real(self)
        state["calls"] += 1
        if state["calls"] == 1:
            state["rival_is_writing"] = True
            try:
                rival.put(make_table(3, start_id=100), mode="append")  # another writer commits first
            finally:
                state["rival_is_writing"] = False
            raise CommitFailedException(LOST_RACE)
        return real(self)

    monkeypatch.setattr(Transaction, "commit_transaction", lose_once_to_a_rival)

    result = table.put(make_table(5, start_id=200), mode="append", policy=FAST)

    assert state["calls"] == 2 and result.attempts == 2
    # rows_before is the true base: the 5 seed rows plus the rival's 3.
    assert (result.rows_written, result.rows_before, result.rows_after) == (5, 8, 13)
    # Every row landed once: the failed attempt wrote nothing the table can see.
    assert ride_ids(table) == [1, 2, 3, 4, 5, 100, 101, 102, 200, 201, 202, 203, 204]


def test_a_retried_append_redoes_the_schema_union_on_the_new_schema(table, make_table, catalog_config, monkeypatch):
    rival = Iceberg("rides", catalog_config)
    real = Transaction.commit_transaction
    state = {"calls": 0, "rival_is_writing": False}

    def lose_once_to_a_rival(self):
        if state["rival_is_writing"]:
            return real(self)
        state["calls"] += 1
        if state["calls"] == 1:
            state["rival_is_writing"] = True
            try:
                rival.put(make_table(2, start_id=100).append_column("tip", [[1.0, 2.0]]), mode="append")
            finally:
                state["rival_is_writing"] = False
            raise CommitFailedException(LOST_RACE)
        return real(self)

    monkeypatch.setattr(Transaction, "commit_transaction", lose_once_to_a_rival)

    result = table.put(make_table(2, start_id=200).append_column("discount", [[0.1, 0.2]]), mode="append", policy=FAST)

    assert result.attempts == 2
    columns = table.get().column_names
    assert "tip" in columns and "discount" in columns  # the rival's new column and ours
    assert len(ride_ids(table)) == 9


def test_a_direct_append_gives_up_after_max_publish_attempts(table, make_table, monkeypatch):
    calls = fail_commits(monkeypatch, math.inf)
    policy = CommitPolicy(max_publish_attempts=3, base_delay_s=0.0, max_delay_s=0.0)

    with pytest.raises(CommitConflict, match=r"test_ns\.rides.*3 attempts") as error:
        table.put(make_table(2, start_id=100), mode="append", policy=policy)

    assert len(calls) == 3
    assert error.value.retriable is True
    assert isinstance(error.value.__cause__, CommitFailedException)
    assert ride_ids(table) == [1, 2, 3, 4, 5]  # nothing was written


def test_a_direct_append_stops_at_the_policy_deadline(table, make_table, monkeypatch):
    calls = fail_commits(monkeypatch, math.inf)
    policy = CommitPolicy(max_publish_attempts=50, base_delay_s=0.0, max_delay_s=0.0, deadline_s=1e-9)

    with pytest.raises(CommitConflict, match="deadline"):
        table.put(make_table(2, start_id=100), mode="append", policy=policy)

    assert len(calls) < 50


def test_a_direct_append_never_retries_an_unknown_outcome(table, make_table, monkeypatch):
    # The first commit may have landed, and a direct write has no idempotency key to find out.
    calls = fail_commits(monkeypatch, 1, CommitStateUnknownException("gateway timeout"))

    with pytest.raises(CommitStateUnknownException):
        table.put(make_table(2, start_id=100), mode="append", policy=FAST)

    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["overwrite", "upsert"])
def test_direct_overwrite_and_upsert_do_not_retry_a_lost_commit_race(catalog_config, make_table, monkeypatch, mode):
    ice = Iceberg("rides", catalog_config, join_cols=["ride_id"])
    ice.put(make_table(5), mode="append")
    calls = fail_commits(monkeypatch, 1)

    with pytest.raises(CommitFailedException):
        ice.put(make_table(5, fare_offset=1.0), mode=mode, policy=FAST)

    assert len(calls) == 1
