"""Staged, exactly-once publishing to Iceberg tables.

This is the publish protocol of ``docs/design/saas_architecture.md`` sections 7.3 to 7.8, built
to the interfaces in ``docs/design/v0_1_1_platform.md`` (C3). A write never touches ``main`` directly:

1. **Fence** (:func:`fence`): one commit removes the branches of superseded attempts and records a
   fence marker for each, so a zombie attempt can never publish afterwards.
2. **Base** (:func:`ensure_base`): a new table gets an empty ``ldp.init`` snapshot, so there is
   always a ``main`` snapshot to branch from.
3. **Find** (:func:`find_commit`): walk ``main``'s ancestry for the idempotency key. If it is
   there, the write already happened and nothing is written again.
4. **Stage** (:func:`stage`): create a private branch at ``base`` and write to it, with the
   additive schema union in the same transaction as the data.
5. **Audit**: optional table-level checks on the staged snapshot.
6. **Publish** (:func:`publish`): fast-forward ``main`` to the staged snapshot in ONE catalog
   compare-and-swap that asserts both ``main == base`` and ``branch == staged``.

If ``main`` moved before the publish, :func:`write_once` resolves the conflict per the SaaS
§7.6 matrix (an ``append`` re-uses its data files; ``overwrite`` is re-applied; ``upsert`` is
recomputed) and tries again, with full-jitter backoff, inside the :class:`CommitPolicy`.

Every snapshot a publish adds carries the snapshot properties ``ldp.idempotency-key``,
``ldp.run-id`` and ``ldp.attempt`` (plus ``ldp.spec-hash``, ``ldp.logical-window`` and
``ldp.source.<name>`` when set). One publish can add more than one snapshot (an ``overwrite``
adds a delete and an append), so "one effect" means one contiguous run of snapshots in
``main``'s history that all carry the same key, run id and attempt.

Why publish goes straight to ``Catalog.commit_table``: in pyiceberg 0.12.0
``Transaction._stage`` (``pyiceberg/table/__init__.py`` lines 293-296) builds the set of
requirement *types* already staged and drops any later requirement of a type in that set, so a
transaction that stages ``AssertRefSnapshotId`` for ``main`` and then for a branch silently keeps
only the first. ``Catalog.commit_table`` validates every requirement it is given
(``Catalog._update_and_stage_table``). It also bypasses ``Transaction.commit_transaction``'s
retry, which would rebase the publish blindly.

The staged protocol needs branch writes, which arrived in pyiceberg 0.10. This package needs 0.11
or newer, so every supported pyiceberg has them.
"""

import contextlib
import functools
import hashlib
import inspect
import random
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pyarrow as pa
from pyiceberg.exceptions import CommitFailedException, CommitStateUnknownException
from pyiceberg.expressions import AlwaysTrue
from pyiceberg.table import Transaction
from pyiceberg.table.refs import SnapshotRefType
from pyiceberg.table.update import (
    AssertCurrentSchemaId,
    AssertRefSnapshotId,
    AssertTableUUID,
    RemoveSnapshotRefUpdate,
    SetSnapshotRefUpdate,
)

from local_data_platform.exceptions import ConfigError, DataQualityError, EngineNotFound, LDPError
from local_data_platform.logger import get_logger

logger = get_logger(__name__)

MAIN_BRANCH = "main"
"""The branch readers see."""

IDEMPOTENCY_KEY_PROPERTY = "ldp.idempotency-key"
RUN_ID_PROPERTY = "ldp.run-id"
ATTEMPT_PROPERTY = "ldp.attempt"
SPEC_HASH_PROPERTY = "ldp.spec-hash"
LOGICAL_WINDOW_PROPERTY = "ldp.logical-window"
SOURCE_PROPERTY_PREFIX = "ldp.source."
INIT_PROPERTY = "ldp.init"
"""Snapshot-summary keys LDP writes (SaaS §6.3)."""

IDEMPOTENCY_HORIZON_S = 7 * 24 * 3600
"""How far back :meth:`CommitContext.create` searches for a key by default: 7 days, the idempotency horizon."""

STAGED_MODES = ("append", "overwrite", "upsert")
"""The write modes the staged protocol supports."""

_BRANCH_RE = re.compile(r"ldp_r([0-9a-f]{32})_a(\d+)_(\d+)")
_ATTEMPT_RE = re.compile(r"ldp_r([0-9a-f]{32})_a(\d+)")
_HEX_RE = re.compile(r"[0-9a-f]{32}")
_FENCE_TRIES = 10
_CLEANUP_TRIES = 3


# --------------------------------------------------------------------------- data types


@dataclass(frozen=True)
class WriteResult:
    """What an Iceberg write did.

    Attributes:
        table_identifier: ``"<namespace>.<name>"``.
        mode: The write mode that ran: ``append``, ``overwrite`` or ``upsert``.
        rows_written: Rows the write added or changed. For ``append`` and ``overwrite`` this
            is the input row count. For ``upsert`` it is rows updated plus rows inserted;
            input rows identical to the stored ones are not rewritten and are not counted.
            0 when ``skipped_duplicate`` is true.
        rows_before: Rows in the table before the write, from the parent snapshot's
            ``total-records`` summary (0 for a new table).
        rows_after: Rows in the table after the write, from the written snapshot's
            ``total-records`` summary.
        snapshot_id: The snapshot the write produced. When ``skipped_duplicate`` is true it
            is the snapshot that already carries the idempotency key.
        branch: The staged branch that was published (staged protocol only).
        idempotency_key: The key the write ran under (staged protocol only).
        attempts: Stage and publish cycles used. A direct write is always 1; a duplicate
            found before any staging is 0.
        skipped_duplicate: True when the key was already published, so nothing was written.
    """

    table_identifier: str
    mode: str
    rows_written: int
    rows_before: int
    rows_after: int
    snapshot_id: int | None
    branch: str | None = None
    idempotency_key: str | None = None
    attempts: int = 1
    skipped_duplicate: bool = False


@dataclass(frozen=True)
class CommitContext:
    """Who is writing, and under which idempotency key.

    Attributes:
        run_id: The run's id, ideally a uuid7 (with or without dashes). Any other string is
            hashed into the branch name.
        attempt: The attempt number, starting at 1. A coordinator that retries a run uses
            ``attempt + 1``; the new attempt fences every older one.
        idempotency_key: ``K``. At most one publish per key is ever reachable from ``main``.
        spec_hash: Hash of the pinned spec, recorded as ``ldp.spec-hash`` (may be empty).
        search_since_ms: :func:`find_commit` stops at snapshots older than this (epoch ms).
            The SaaS coordinator passes the first attempt's start minus 10 minutes of clock
            skew; :meth:`create` uses the 7-day idempotency horizon.
        logical_window: The scheduler's ``(start, end)`` window, recorded as
            ``ldp.logical-window``.
        source_positions: Source positions (Kafka offsets JSON, LSN, ETag), recorded as
            ``ldp.source.<name>``.

    Raises:
        ValueError: If a field is empty or out of range.
    """

    run_id: str
    attempt: int
    idempotency_key: str
    spec_hash: str
    search_since_ms: int
    logical_window: tuple[datetime, datetime] | None = None
    source_positions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError("CommitContext.run_id must be a non-empty string")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool) or self.attempt < 1:
            raise ValueError(f"CommitContext.attempt must be an integer >= 1, got {self.attempt!r}")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            raise ValueError("CommitContext.idempotency_key must be a non-empty string")
        if not isinstance(self.spec_hash, str):
            raise ValueError("CommitContext.spec_hash must be a string")
        if not isinstance(self.search_since_ms, int) or self.search_since_ms < 0:
            raise ValueError(f"CommitContext.search_since_ms must be epoch milliseconds, got {self.search_since_ms!r}")
        if self.logical_window is not None and len(self.logical_window) != 2:
            raise ValueError("CommitContext.logical_window must be a (start, end) pair")

    @classmethod
    def create(
        cls,
        idempotency_key: str,
        *,
        run_id: str | None = None,
        attempt: int = 1,
        spec_hash: str = "",
        horizon_s: float = IDEMPOTENCY_HORIZON_S,
        now_ms: int | None = None,
        logical_window: tuple[datetime, datetime] | None = None,
        source_positions: Mapping[str, str] | None = None,
    ) -> "CommitContext":
        """Build a context for a local run: a fresh uuid7 run id and a 7-day key search.

        Args:
            idempotency_key: The key.
            run_id: The run id; a new uuid7 hex by default.
            attempt: The attempt number.
            spec_hash: Hash of the pinned spec.
            horizon_s: How far back to search for the key.
            now_ms: The current time in epoch ms (for tests).
            logical_window: The ``(start, end)`` window.
            source_positions: Source positions to record.

        Returns:
            The context.
        """
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return cls(
            run_id=run_id or new_run_id(),
            attempt=attempt,
            idempotency_key=idempotency_key,
            spec_hash=spec_hash,
            search_since_ms=max(0, now - int(horizon_s * 1000)),
            logical_window=logical_window,
            source_positions=dict(source_positions or {}),
        )

    def snapshot_properties(self) -> dict[str, str]:
        """The ``ldp.*`` snapshot-summary properties every staged snapshot carries."""
        properties = {
            IDEMPOTENCY_KEY_PROPERTY: self.idempotency_key,
            RUN_ID_PROPERTY: self.run_id,
            ATTEMPT_PROPERTY: str(self.attempt),
        }
        if self.spec_hash:
            properties[SPEC_HASH_PROPERTY] = self.spec_hash
        if self.logical_window is not None:
            start, end = self.logical_window
            properties[LOGICAL_WINDOW_PROPERTY] = f"{_iso(start)}/{_iso(end)}"
        for name, position in self.source_positions.items():
            properties[f"{SOURCE_PROPERTY_PREFIX}{name}"] = str(position)
        return properties


@dataclass(frozen=True)
class CommitPolicy:
    """Retry limits for one attempt (SaaS §7.4 step 6).

    Attributes:
        max_publish_attempts: Stage-and-publish cycles before :class:`CommitConflict`.
        base_delay_s: Full-jitter backoff base.
        max_delay_s: Full-jitter backoff cap.
        deadline_s: Wall-clock budget for the attempt.
        max_search_depth: How many snapshots :func:`find_commit` may walk.
    """

    max_publish_attempts: int = 6
    base_delay_s: float = 0.2
    max_delay_s: float = 8.0
    deadline_s: float = 300.0
    max_search_depth: int = 500

    def __post_init__(self) -> None:
        if self.max_publish_attempts < 1:
            raise ValueError("CommitPolicy.max_publish_attempts must be at least 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.deadline_s <= 0:
            raise ValueError("CommitPolicy delays must be >= 0 and deadline_s > 0")
        if self.max_search_depth < 1:
            raise ValueError("CommitPolicy.max_search_depth must be at least 1")

    def backoff_s(self, attempt: int) -> float:
        """Full-jitter delay before cycle ``attempt + 1``: uniform in ``[0, min(cap, base * 2**attempt)]``."""
        return random.uniform(0.0, min(self.max_delay_s, self.base_delay_s * (2 ** max(0, attempt - 1))))


@dataclass(frozen=True)
class StagedWrite:
    """A write staged on a private branch, ready for :func:`publish`.

    Attributes:
        branch: The branch holding the staged snapshot.
        base_snapshot_id: ``main``'s head when the branch was created.
        staged_snapshot_id: The branch head after the write.
        schema_id: The table's current schema id after the write's schema union.
        rows_written: Rows the staged write added or changed.
    """

    branch: str
    base_snapshot_id: int
    staged_snapshot_id: int
    schema_id: int
    rows_written: int


class CommitConflict(LDPError):
    """The write could not be published.

    Attributes:
        retriable: True when a new attempt (``attempt + 1``) may succeed, for example after
            the publish attempts ran out under contention. False when this attempt was
            fenced by a newer one and must stop.
    """

    def __init__(self, message: str, *, retriable: bool = True):
        super().__init__(message)
        self.retriable = retriable


class CommitSearchExhausted(LDPError):
    """:func:`find_commit` could not prove the key is absent, so the write fails closed."""


# --------------------------------------------------------------------------- names


def new_run_id() -> str:
    """A new uuid7 run id as 32 hex characters (``uuid.uuid7`` where available)."""
    maker = getattr(uuid, "uuid7", None)
    if maker is not None:
        return maker().hex
    # RFC 9562 uuid7: 48-bit ms timestamp, version 7, variant 10, random rest.
    value = (int(time.time() * 1000) & ((1 << 48) - 1)) << 80 | random.getrandbits(80)
    value = (value & ~(0xF << 76)) | (0x7 << 76)
    value = (value & ~(0x3 << 62)) | (0x2 << 62)
    return f"{value:032x}"


def _run_hex(run_id: str) -> str:
    text = str(run_id).strip().lower()
    if _HEX_RE.fullmatch(text):
        return text
    try:
        return uuid.UUID(text).hex
    except ValueError:
        return hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()[:32]


def branch_name(run_id: str, attempt: int, n: int = 0) -> str:
    """The staging branch name ``ldp_r<32-hex run id>_a<attempt>_<n>`` (no ``/`` or ``-``, SaaS §6.3).

    Args:
        run_id: The run id. A uuid (with or without dashes) is used as its hex; any other
            string is hashed.
        attempt: The attempt number (>= 1).
        n: The stage number within the attempt (>= 0); it grows on every re-stage.

    Returns:
        The branch name.
    """
    if attempt < 1 or n < 0:
        raise ValueError(f"branch_name needs attempt >= 1 and n >= 0, got attempt={attempt}, n={n}")
    return f"ldp_r{_run_hex(run_id)}_a{int(attempt)}_{int(n)}"


def attempt_prefix(run_id: str, attempt: int) -> str:
    """``ldp_r<hex>_a<attempt>``: passed to :func:`fence`, it fences every branch of that attempt."""
    return f"ldp_r{_run_hex(run_id)}_a{int(attempt)}"


def fence_marker(run_id: str, attempt: int) -> str:
    """The tag :func:`fence` leaves behind for a fenced attempt: ``ldp_r<hex>_a<attempt>_fenced``."""
    return f"{attempt_prefix(run_id, attempt)}_fenced"


def staged_branches(table) -> dict[str, int]:
    """The LDP staging branches on ``table``, as ``{branch: head snapshot id}``.

    Branches that remain were rejected by quality checks (kept for inspection), belong to a
    crashed attempt, or are being staged right now.
    """
    return {
        name: ref.snapshot_id
        for name, ref in table.metadata.refs.items()
        if ref.snapshot_ref_type == SnapshotRefType.BRANCH and _BRANCH_RE.fullmatch(name)
    }


# --------------------------------------------------------------------------- protocol steps


def ensure_base(table) -> int:
    """Return ``main``'s head, first appending an empty ``ldp.init=true`` snapshot if there is none.

    pyiceberg refuses to branch from a table with no snapshots (SaaS §6.3), so every staged
    write needs a base. If two writers bootstrap at once, both empty snapshots may land; they
    hold no rows, so that is harmless.

    Args:
        table: A pyiceberg table. It is refreshed in place when a snapshot is appended.

    Returns:
        The snapshot id at the head of ``main``.
    """
    for _ in range(_CLEANUP_TRIES + 2):
        snapshot = table.current_snapshot()
        if snapshot is not None:
            return snapshot.snapshot_id
        try:
            with sequence_race_is_a_conflict():
                table.append(table.schema().as_arrow().empty_table(), snapshot_properties={INIT_PROPERTY: "true"})
        except CommitFailedException as exc:
            # Another writer bootstrapped (and maybe staged) first; its snapshot is our base.
            logger.debug("Bootstrap of %s lost a race (%s); reloading", _name(table), exc)
            table.refresh()
            continue
        snapshot = table.current_snapshot()
        if snapshot is not None:
            logger.debug("Bootstrapped %s with ldp.init snapshot %s", _name(table), snapshot.snapshot_id)
            return snapshot.snapshot_id
    raise LDPError(f"could not create the ldp.init base snapshot for {_name(table)}")


def find_commit(table, key: str, *, since_ms: int, max_depth: int = 500):
    """Find the snapshot on ``main`` that carries idempotency key ``key``.

    Walks ``main``'s ancestry from the head, newest first. The walk stops at the first
    snapshot older than ``since_ms`` (after checking it) or at the root.

    Args:
        table: A pyiceberg table; its loaded metadata is searched as is.
        key: The idempotency key.
        since_ms: Epoch ms; older snapshots end the search.
        max_depth: The most snapshots to examine.

    Returns:
        The newest snapshot carrying ``key``, or ``None`` if the key is provably absent.

    Raises:
        CommitSearchExhausted: The walk reached ``max_depth``, or a missing (expired) parent,
            before reaching ``since_ms`` or the root. The caller must not publish.
    """
    snapshot = table.current_snapshot()
    depth = 0
    while snapshot is not None:
        if _summary(snapshot).get(IDEMPOTENCY_KEY_PROPERTY) == key:
            return snapshot
        if snapshot.timestamp_ms < since_ms or snapshot.parent_snapshot_id is None:
            return None
        depth += 1
        if depth >= max_depth:
            raise CommitSearchExhausted(
                f"searched {depth} snapshots of {_name(table)} for idempotency key {key!r} without reaching "
                f"the time bound {since_ms}; refusing to publish (raise CommitPolicy.max_search_depth)"
            )
        parent = table.metadata.snapshot_by_id(snapshot.parent_snapshot_id)
        if parent is None:
            raise CommitSearchExhausted(
                f"the history of {_name(table)} is truncated at snapshot {snapshot.parent_snapshot_id} (expired?) "
                f"before the time bound {since_ms}; cannot prove idempotency key {key!r} is absent"
            )
        snapshot = parent
    return None


def fence(catalog, table, branches: Iterable[str]) -> None:
    """Fence superseded attempts in ONE catalog commit.

    Each item is a branch name, or an attempt prefix from :func:`attempt_prefix`, which covers
    every branch of that attempt, including ones it has not created yet. The commit removes the
    matching branches and, for every LDP attempt named, adds the tag :func:`fence_marker`, which
    :func:`stage` and :func:`publish` assert is absent. So once the fence commits, the fenced
    attempt can neither publish what it staged nor stage again (invariant I3). Branches that are
    already gone need nothing, so a repeated fence is a no-op, and a fence commit whose outcome is
    unknown is reconciled by reloading and fencing what is still missing.

    Args:
        catalog: The table's catalog.
        table: The pyiceberg table (reloaded here).
        branches: Branch names or attempt prefixes.

    Raises:
        ConfigError: If ``main`` is listed.
        CommitConflict: If the fence could not commit under contention, or its outcome stayed
            unknown, within the tries (retriable).
    """
    targets = list(dict.fromkeys(str(branch) for branch in branches))
    if not targets:
        return
    if MAIN_BRANCH in targets:
        raise ConfigError("fence() never removes 'main'")
    identifier = table.name()
    for attempt in range(1, _FENCE_TRIES + 1):
        current = catalog.load_table(identifier)
        refs = current.metadata.refs
        remove: dict[str, int] = {}
        markers: list[str] = []
        for target in targets:
            prefix, full = _ATTEMPT_RE.fullmatch(target), _BRANCH_RE.fullmatch(target)
            if prefix:
                names = [name for name in refs if name.startswith(target + "_") and _BRANCH_RE.fullmatch(name)]
                run_hex, fenced_attempt = prefix.group(1), prefix.group(2)
            else:
                names = [target] if target in refs else []
                run_hex, fenced_attempt = (full.group(1), full.group(2)) if full else (None, None)
            for name in names:
                if refs[name].snapshot_ref_type == SnapshotRefType.BRANCH:
                    remove[name] = refs[name].snapshot_id
            if run_hex is not None:
                marker = f"ldp_r{run_hex}_a{fenced_attempt}_fenced"
                if marker not in refs and marker not in markers:
                    markers.append(marker)
        head = current.metadata.current_snapshot_id
        if head is None:
            markers = []  # nothing can be staged on a table without a snapshot
        if not remove and not markers:
            return
        requirements = [AssertTableUUID(uuid=current.metadata.table_uuid)]
        requirements += [AssertRefSnapshotId(ref=name, snapshot_id=sid) for name, sid in remove.items()]
        requirements += [AssertRefSnapshotId(ref=marker, snapshot_id=None) for marker in markers]
        updates: list[Any] = [RemoveSnapshotRefUpdate(ref_name=name) for name in remove]
        updates += [
            SetSnapshotRefUpdate(ref_name=marker, type=SnapshotRefType.TAG, snapshot_id=head,
                                 max_ref_age_ms=IDEMPOTENCY_HORIZON_S * 1000)
            for marker in markers
        ]
        try:
            catalog.commit_table(current, tuple(requirements), tuple(updates))
        except (CommitFailedException, CommitStateUnknownException) as exc:
            # The next try reloads: a fence that landed leaves nothing to do, one that did not is redone.
            logger.debug("Fence of %s on %s failed (%s); reloading", targets, _name(current), exc)
            time.sleep(random.uniform(0.0, min(1.0, 0.02 * 2 ** attempt)))
            continue
        logger.info("Fenced %s on %s: removed branches %s, markers %s",
                    targets, _name(current), sorted(remove), markers)
        return
    raise CommitConflict(f"could not fence {targets} on {_name(table)} after {_FENCE_TRIES} tries", retriable=True)


def stage(
    table,
    df: pa.Table,
    mode: str,
    ctx: CommitContext,
    *,
    join_cols: list[str] | None = None,
    overwrite_filter: Any = None,
    n: int = 0,
    schema_evolution: bool = True,
) -> StagedWrite:
    """Stage a write on a new branch cut from ``main``'s head.

    One catalog commit creates the branch at ``base`` while asserting that ``main`` is still
    ``base`` and that this attempt has not been fenced. Then ONE transaction writes the data
    to the branch, with the additive schema union first. ``overwrite`` and ``upsert`` on a base
    with no rows are a plain append, as in direct mode. The branch is removed again if staging
    fails.

    Args:
        table: The pyiceberg table; ``main`` must have a snapshot (see :func:`ensure_base`).
        df: The rows. Timestamps are cast to microseconds.
        mode: ``append``, ``overwrite`` or ``upsert``.
        ctx: The commit context; its ``ldp.*`` properties go on every staged snapshot.
        join_cols: Key columns for ``upsert``.
        overwrite_filter: For ``overwrite``: a pyiceberg expression or string limiting the
            rows replaced (a window overwrite). ``None`` replaces every row.
        n: The stage number, used in the branch name.
        schema_evolution: Union new columns into the table schema before writing.

    Returns:
        The :class:`StagedWrite`.

    Raises:
        CommitFailedException: The branch or the write lost a race; stage again.
        CommitConflict: This attempt has been fenced (not retriable).
        ConfigError: If the mode or its options are invalid.
    """
    _require_branch_writes()
    mode = _check_mode(mode, join_cols, overwrite_filter)
    df = _prepare(df)
    catalog = table.catalog
    identifier = table.name()
    base = table.current_snapshot()
    if base is None:
        raise LDPError(f"{_name(table)} has no snapshot to branch from; call ensure_base(table) first")
    branch = branch_name(ctx.run_id, ctx.attempt, n)
    _create_branch(catalog, table, branch, base.snapshot_id, fence_marker(ctx.run_id, ctx.attempt))
    try:
        current = catalog.load_table(identifier)
        ref = current.metadata.refs.get(branch)
        if ref is None or ref.snapshot_id != base.snapshot_id:
            raise CommitConflict(f"branch {branch} was removed as soon as it was created: attempt fenced",
                                 retriable=False)
        with sequence_race_is_a_conflict():
            rows_written = _write_branch(current, df, mode, ctx, branch, base, join_cols, overwrite_filter,
                                         schema_evolution)
        staged_id = current.metadata.refs[branch].snapshot_id
        _check_descends(current, branch, staged_id, base.snapshot_id)
        schema_id = current.metadata.current_schema_id
    except Exception:
        _drop_branch(catalog, identifier, branch)
        raise
    logger.debug("Staged %s %d rows on %s (base %s -> %s)", mode, rows_written, branch, base.snapshot_id, staged_id)
    return StagedWrite(branch=branch, base_snapshot_id=base.snapshot_id, staged_snapshot_id=staged_id,
                       schema_id=schema_id, rows_written=rows_written)


def publish(catalog, table, staged: StagedWrite):
    """Fast-forward ``main`` to the staged snapshot in ONE catalog compare-and-swap.

    The commit asserts the table uuid, ``main == staged.base_snapshot_id``,
    ``branch == staged.staged_snapshot_id``, the schema id and, for LDP branches, that the
    attempt's fence marker is absent. It sets ``main`` and removes the branch in the same
    commit. It goes straight to ``Catalog.commit_table``: ``Transaction._stage`` would drop the
    second ``AssertRefSnapshotId`` (pyiceberg 0.12.0 ``table/__init__.py`` lines 293-296), and
    ``Transaction.commit_transaction`` would retry by rebasing.

    Args:
        catalog: The table's catalog.
        table: The pyiceberg table.
        staged: What :func:`stage` returned.

    Returns:
        The published snapshot.

    Raises:
        CommitFailedException: A requirement failed (``main`` moved, the branch was removed,
            the schema changed, the attempt was fenced) or the catalog CAS lost a race.
    """
    requirements: list[Any] = [
        AssertTableUUID(uuid=table.metadata.table_uuid),
        AssertRefSnapshotId(ref=MAIN_BRANCH, snapshot_id=staged.base_snapshot_id),
        AssertRefSnapshotId(ref=staged.branch, snapshot_id=staged.staged_snapshot_id),
        AssertCurrentSchemaId(current_schema_id=staged.schema_id),
    ]
    match = _BRANCH_RE.fullmatch(staged.branch)
    if match:
        requirements.append(AssertRefSnapshotId(ref=f"ldp_r{match.group(1)}_a{match.group(2)}_fenced",
                                                snapshot_id=None))
    updates = (
        SetSnapshotRefUpdate(ref_name=MAIN_BRANCH, type=SnapshotRefType.BRANCH, snapshot_id=staged.staged_snapshot_id),
        RemoveSnapshotRefUpdate(ref_name=staged.branch),
    )
    catalog.commit_table(table, tuple(requirements), updates)
    published = catalog.load_table(table.name())
    logger.info("Published %s to main of %s (snapshot %s)", staged.branch, _name(published), staged.staged_snapshot_id)
    return published.snapshot_by_id(staged.staged_snapshot_id)


def write_once(
    load: Callable[[], Any],
    catalog,
    df: pa.Table,
    mode: str,
    ctx: CommitContext,
    *,
    audit: Callable[[Any, int], Any] | None = None,
    policy: CommitPolicy = CommitPolicy(),
    join_cols: list[str] | None = None,
    overwrite_filter: Any = None,
    schema_evolution: bool = True,
    fence_branches: Iterable[str] = (),
) -> WriteResult:
    """Write ``df`` so that at most one effect per idempotency key reaches ``main``.

    Runs the loop of SaaS §7.4: fence older attempts of the run, bootstrap a base, search for
    the key, stage, audit, publish. When the publish loses to a moved ``main`` it re-stages per
    the §7.6 conflict matrix: an ``append`` re-uses the data files it already wrote, an
    ``overwrite`` is re-applied from the new base, an ``upsert`` is recomputed from it, and a
    concurrent schema change is unioned again.

    Args:
        load: Returns the pyiceberg table (creating it if needed).
        catalog: The table's catalog.
        df: The rows.
        mode: ``append``, ``overwrite`` or ``upsert``.
        ctx: The commit context.
        audit: Optional table-level checks, called as ``audit(table, staged_snapshot_id)`` and
            returning a :class:`~local_data_platform.quality.QualityReport`. A failed report
            stops the write and keeps the branch for inspection.
        policy: Retry limits.
        join_cols: Key columns for ``upsert``.
        overwrite_filter: Rows an ``overwrite`` replaces (a window); every row by default.
        schema_evolution: Union new columns into the table schema.
        fence_branches: Extra branches or attempt prefixes to fence first (superseded runs).

    Returns:
        A :class:`WriteResult`; ``skipped_duplicate`` is true when the key was already on ``main``.

    Raises:
        CommitConflict: Publish attempts or the deadline ran out (``retriable=True``: retry the
            run as ``attempt + 1``), or this attempt was fenced (``retriable=False``).
        CommitSearchExhausted: The key search could not prove the key is absent.
        DataQualityError: ``audit`` failed; nothing was published.
    """
    _require_branch_writes()
    mode = _check_mode(mode, join_cols, overwrite_filter)
    if not isinstance(ctx, CommitContext):
        raise TypeError(f"ctx must be a CommitContext, got {type(ctx).__name__}")
    df = _prepare(df)
    deadline = time.monotonic() + policy.deadline_s
    table = load()
    identifier = table.name()
    ensure_base(table)
    table = catalog.load_table(identifier)
    targets = [attempt_prefix(ctx.run_id, older) for older in range(1, ctx.attempt)] + list(fence_branches)
    if targets:
        fence(catalog, table, targets)
    n = _next_stage_number(catalog.load_table(identifier), ctx)
    pending: StagedWrite | None = None
    previous: StagedWrite | None = None
    attempts = 0
    while True:
        table = catalog.load_table(identifier)
        found = find_commit(table, ctx.idempotency_key, since_ms=ctx.search_since_ms, max_depth=policy.max_search_depth)
        if found is not None:
            return _found_result(catalog, table, found, mode, ctx, attempts, pending)
        _raise_if_fenced(table, ctx, pending)
        if pending is not None and not _publishable(table, pending):
            # main or the schema moved: this staged write can never publish (SaaS §7.6).
            _drop_branch(catalog, identifier, pending.branch)
            previous, pending = pending, None
        attempts += 1
        if attempts > policy.max_publish_attempts or time.monotonic() > deadline:
            if pending is not None:
                _drop_branch(catalog, identifier, pending.branch)
            raise CommitConflict(
                f"could not publish {mode} to {_name(table)} for key {ctx.idempotency_key!r} within "
                f"{policy.max_publish_attempts} attempts / {policy.deadline_s}s; retry as attempt {ctx.attempt + 1}",
                retriable=True,
            )
        if pending is None:
            try:
                pending = _stage_again(catalog, catalog.load_table(identifier), df, mode, ctx, previous, n,
                                       join_cols, overwrite_filter, schema_evolution)
            except (CommitFailedException, CommitStateUnknownException) as exc:
                logger.debug("Staging %s on %s lost a race (%s); retrying", mode, _name(table), exc)
                n += 1
                time.sleep(policy.backoff_s(attempts))
                continue
            n += 1
            if audit is not None:
                report = audit(catalog.load_table(identifier), pending.staged_snapshot_id)
                if not getattr(report, "passed", True):
                    raise DataQualityError(
                        f"table-level checks failed on staged snapshot {pending.staged_snapshot_id} of "
                        f"{_name(table)}; nothing was published and branch {pending.branch} is kept for inspection",
                        report,
                    )
        try:
            publish(catalog, table, pending)
        except (CommitFailedException, CommitStateUnknownException) as exc:
            logger.info("Publish of %s to %s failed (%s); re-checking", pending.branch, _name(table), exc)
            time.sleep(policy.backoff_s(attempts))
            continue
        return _published_result(catalog, identifier, pending, mode, ctx, attempts)


# --------------------------------------------------------------------------- helpers


@functools.cache
def _branch_writes_supported() -> bool:
    return "branch" in inspect.signature(Transaction.append).parameters and hasattr(Transaction, "upsert")


@functools.cache
def _upsert_takes_properties() -> bool:
    return "snapshot_properties" in inspect.signature(Transaction.upsert).parameters


def _require_branch_writes() -> None:
    if not _branch_writes_supported():
        raise EngineNotFound(
            "the staged commit protocol writes to Iceberg branches, and this package needs pyiceberg>=0.11; "
            "upgrade with: pip install -U \"pyiceberg>=0.11\""
        )


def is_sequence_race(exc: BaseException) -> bool:
    """Whether ``exc`` is pyiceberg rejecting a snapshot whose sequence number a concurrent commit took.

    Every commit, on any branch, advances the table's ``last-sequence-number``. A snapshot built
    against older metadata passes the requirements of a write to a *different* ref, and then
    ``update_table_metadata`` refuses it with a ``ValueError`` ("Cannot add snapshot with sequence
    number N older than last sequence number N") before anything is written. With several
    staging branches on one table that is an ordinary lost race, so it is retried like a
    ``CommitFailedException``.
    """
    return isinstance(exc, ValueError) and "sequence number" in str(exc)


@contextlib.contextmanager
def sequence_race_is_a_conflict():
    """Re-raise :func:`is_sequence_race` errors as ``CommitFailedException`` (nothing was committed)."""
    try:
        yield
    except ValueError as exc:
        if is_sequence_race(exc):
            raise CommitFailedException(f"a concurrent commit took the next sequence number: {exc}") from exc
        raise


def _check_mode(mode: str, join_cols, overwrite_filter) -> str:
    normalised = mode.strip().lower() if isinstance(mode, str) else mode
    if normalised not in STAGED_MODES:
        raise ConfigError(f"unknown write mode {mode!r}; expected one of {list(STAGED_MODES)}")
    if normalised == "upsert" and not join_cols:
        raise ConfigError("write mode 'upsert' needs join_cols")
    if overwrite_filter is not None and normalised != "overwrite":
        raise ConfigError(f"overwrite_filter only applies to mode 'overwrite', not {normalised!r}")
    return normalised


def _prepare(df) -> pa.Table:
    from local_data_platform.format.iceberg import cast_timestamps_to_us

    if df is None:
        raise ValueError("No data to write: got None instead of a pyarrow.Table")
    if isinstance(df, pa.RecordBatch):
        df = pa.Table.from_batches([df])
    if not isinstance(df, pa.Table):
        raise TypeError(f"Expected a pyarrow.Table, got {type(df).__name__}")
    return cast_timestamps_to_us(df)


def _iso(value: datetime) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _name(table) -> str:
    try:
        return ".".join(table.name())
    except Exception:  # noqa: BLE001 - only used in messages
        return repr(table)


def _summary(snapshot) -> dict[str, str]:
    summary = snapshot.summary
    return dict(summary.additional_properties) if summary is not None else {}


def check_upsert_columns(table, df: pa.Table) -> None:
    """Raise ``ConfigError`` if an upsert batch lacks columns that ``table`` has.

    An upsert replaces whole rows, so pyiceberg needs every column of the table in the batch;
    without this check it fails deep inside with "Target schema's field names are not matching".
    Filling the gaps with nulls would silently erase those values in every matched row, so the
    batch must carry them. Extra columns are fine: schema evolution adds them.
    """
    missing = [field.name for field in table.schema().fields if field.name not in df.column_names]
    if missing:
        raise ConfigError(f"upsert data for {_name(table)} is missing columns {missing} of the table; an upsert "
                          "replaces whole rows, so the batch needs every column (read the current rows and "
                          "update them, or use mode 'append')")


def snapshot_records(table, snapshot) -> int:
    """Rows in ``snapshot``, from its ``total-records`` summary, else by scanning it (0 for ``None``)."""
    if snapshot is None:
        return 0
    total = _summary(snapshot).get("total-records")
    if total is not None:
        try:
            return int(total)
        except ValueError:
            pass
    return table.scan(snapshot_id=snapshot.snapshot_id).to_arrow().num_rows


def _next_stage_number(table, ctx: CommitContext) -> int:
    """One past the highest stage number a crashed run of this same attempt left behind."""
    prefix = attempt_prefix(ctx.run_id, ctx.attempt) + "_"
    numbers = [int(match.group(3)) for name in table.metadata.refs
               if name.startswith(prefix) and (match := _BRANCH_RE.fullmatch(name))]
    return max(numbers) + 1 if numbers else 0


def _create_branch(catalog, table, branch: str, base_id: int, marker: str) -> None:
    catalog.commit_table(
        table,
        (
            AssertTableUUID(uuid=table.metadata.table_uuid),
            AssertRefSnapshotId(ref=MAIN_BRANCH, snapshot_id=base_id),
            AssertRefSnapshotId(ref=branch, snapshot_id=None),
            AssertRefSnapshotId(ref=marker, snapshot_id=None),
        ),
        (SetSnapshotRefUpdate(ref_name=branch, type=SnapshotRefType.BRANCH, snapshot_id=base_id),),
    )


def _drop_branch(catalog, identifier, branch: str) -> None:
    """Remove ``branch`` if it still exists. Best effort: a failure is logged, never raised."""
    for _ in range(_CLEANUP_TRIES):
        try:
            current = catalog.load_table(identifier)
            ref = current.metadata.refs.get(branch)
            if ref is None:
                return
            catalog.commit_table(current, (AssertRefSnapshotId(ref=branch, snapshot_id=ref.snapshot_id),),
                                 (RemoveSnapshotRefUpdate(ref_name=branch),))
            return
        except (CommitFailedException, CommitStateUnknownException):
            continue
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the original error
            logger.debug("Could not remove branch %s: %s", branch, exc)
            return
    logger.debug("Gave up removing branch %s; maintenance will remove it", branch)


def _write_branch(table, df, mode, ctx, branch, base, join_cols, overwrite_filter, schema_evolution) -> int:
    """Write ``df`` to ``branch`` in ONE transaction; ``table`` is updated by the commit."""
    properties = ctx.snapshot_properties()
    effective = mode if snapshot_records(table, base) > 0 else "append"
    if effective == "upsert":
        check_upsert_columns(table, df)
    with table.transaction() as tx:
        if schema_evolution:
            with tx.update_schema() as update:
                update.union_by_name(df.schema)
        if effective == "append":
            tx.append(df, snapshot_properties=properties, branch=branch)
            return df.num_rows
        if effective == "overwrite":
            tx.overwrite(df, overwrite_filter=AlwaysTrue() if overwrite_filter is None else overwrite_filter,
                         snapshot_properties=properties, branch=branch)
            return df.num_rows
        if _upsert_takes_properties():
            result = tx.upsert(df, join_cols=list(join_cols), branch=branch, snapshot_properties=properties)
        else:
            result = tx.upsert(df, join_cols=list(join_cols), branch=branch)
        rows = result.rows_updated + result.rows_inserted
        if rows == 0 or not _upsert_takes_properties():
            # Record the key even when nothing changed (or this pyiceberg can't tag upsert snapshots).
            tx.append(df.slice(0, 0), snapshot_properties=properties, branch=branch)
        return rows


def _check_descends(table, branch: str, staged_id: int, base_id: int) -> None:
    """Fail if the staged snapshot is not a strict descendant of ``base``.

    pyiceberg's commit retry re-creates a branch that was removed while the write was in
    flight, with a parentless snapshot. Publishing that would drop every existing row, so a
    staged snapshot that does not descend from ``base`` means the attempt was fenced.
    """
    if staged_id == base_id:
        raise LDPError(f"staging on {branch} produced no snapshot")
    snapshot = table.metadata.snapshot_by_id(staged_id)
    while snapshot is not None and snapshot.parent_snapshot_id is not None:
        if snapshot.parent_snapshot_id == base_id:
            return
        snapshot = table.metadata.snapshot_by_id(snapshot.parent_snapshot_id)
    raise CommitConflict(f"branch {branch} no longer descends from base {base_id}: the attempt was fenced while "
                         "staging", retriable=False)


def _stage_again(catalog, table, df, mode, ctx, previous, n, join_cols, overwrite_filter, schema_evolution):
    """Stage (or re-stage) per the SaaS §7.6 conflict matrix."""
    if mode == "append" and previous is not None:
        reused = _restage_append(catalog, table, previous, ctx, n)
        if reused is not None:
            return reused
    return stage(table, df, mode, ctx, join_cols=join_cols, overwrite_filter=overwrite_filter, n=n,
                 schema_evolution=schema_evolution)


def _restage_append(catalog, table, previous: StagedWrite, ctx: CommitContext, n: int) -> StagedWrite | None:
    """Re-stage an append from the new base with the data files it already wrote (no rewrite).

    Returns ``None`` (stage from ``df`` instead) when the schema or partition spec changed, or
    the old snapshot's files cannot be read back.
    """
    from pyiceberg.manifest import ManifestContent, ManifestEntryStatus

    old = table.metadata.snapshot_by_id(previous.staged_snapshot_id)
    if old is None or table.metadata.current_schema_id != previous.schema_id:
        return None
    files = []
    for manifest in old.manifests(table.io):
        if manifest.content != ManifestContent.DATA or manifest.added_snapshot_id != old.snapshot_id:
            continue
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
            if entry.status == ManifestEntryStatus.ADDED and entry.snapshot_id == old.snapshot_id:
                files.append(entry.data_file)
    default_spec = table.metadata.default_spec_id
    if any(data_file.spec_id != default_spec for data_file in files):
        return None
    base = table.current_snapshot()
    branch = branch_name(ctx.run_id, ctx.attempt, n)
    _create_branch(catalog, table, branch, base.snapshot_id, fence_marker(ctx.run_id, ctx.attempt))
    identifier = table.name()
    try:
        current = catalog.load_table(identifier)
        if branch not in current.metadata.refs:
            raise CommitConflict(f"branch {branch} was removed as soon as it was created: attempt fenced",
                                 retriable=False)
        with sequence_race_is_a_conflict(), current.transaction() as tx:
            with tx.update_snapshot(snapshot_properties=ctx.snapshot_properties(), branch=branch).fast_append() as add:
                for data_file in files:
                    add.append_data_file(data_file)
        staged_id = current.metadata.refs[branch].snapshot_id
        _check_descends(current, branch, staged_id, base.snapshot_id)
    except Exception:
        _drop_branch(catalog, identifier, branch)
        raise
    logger.debug("Re-staged append on %s with %d existing data files", branch, len(files))
    return StagedWrite(branch=branch, base_snapshot_id=base.snapshot_id, staged_snapshot_id=staged_id,
                       schema_id=current.metadata.current_schema_id, rows_written=previous.rows_written)


def _publishable(table, pending: StagedWrite) -> bool:
    ref = table.metadata.refs.get(pending.branch)
    return (table.metadata.current_snapshot_id == pending.base_snapshot_id
            and ref is not None and ref.snapshot_id == pending.staged_snapshot_id
            and table.metadata.current_schema_id == pending.schema_id)


def _raise_if_fenced(table, ctx: CommitContext, pending: StagedWrite | None) -> None:
    refs = table.metadata.refs
    if fence_marker(ctx.run_id, ctx.attempt) in refs:
        raise CommitConflict(f"attempt {ctx.attempt} of run {ctx.run_id} was fenced by a newer attempt; stopping",
                             retriable=False)
    if pending is not None and pending.branch not in refs:
        raise CommitConflict(f"staged branch {pending.branch} was removed by a fence; stopping", retriable=False)


def _found_result(catalog, table, found, mode, ctx, attempts, pending) -> WriteResult:
    if pending is not None and pending.staged_snapshot_id == found.snapshot_id:
        # Our own publish landed although its response was lost.
        return _published_result(catalog, table.name(), pending, mode, ctx, attempts)
    if pending is not None:
        _drop_branch(catalog, table.name(), pending.branch)
    rows = snapshot_records(table, table.current_snapshot())
    logger.info("Idempotency key %r is already published to %s (snapshot %s); nothing written",
                ctx.idempotency_key, _name(table), found.snapshot_id)
    return WriteResult(table_identifier=_name(table), mode=mode, rows_written=0, rows_before=rows, rows_after=rows,
                       snapshot_id=found.snapshot_id, branch=None, idempotency_key=ctx.idempotency_key,
                       attempts=attempts, skipped_duplicate=True)


def _published_result(catalog, identifier, pending: StagedWrite, mode, ctx, attempts) -> WriteResult:
    table = catalog.load_table(identifier)
    base = table.metadata.snapshot_by_id(pending.base_snapshot_id)
    published = table.metadata.snapshot_by_id(pending.staged_snapshot_id)
    return WriteResult(
        table_identifier=_name(table),
        mode=mode,
        rows_written=pending.rows_written,
        rows_before=snapshot_records(table, base),
        rows_after=snapshot_records(table, published),
        snapshot_id=pending.staged_snapshot_id,
        branch=pending.branch,
        idempotency_key=ctx.idempotency_key,
        attempts=attempts,
        skipped_duplicate=False,
    )


# --------------------------------------------------------------------------- CLI


def add_cli(subparsers, parents: Iterable[Any] = ()) -> None:
    """Register ``ldp commits CONFIG [--key KEY] [--branches]`` on an argparse subparsers object.

    ``ldp commits`` lists the LDP publishes on ``main`` of a config's Iceberg target (the
    snapshots carrying ``ldp.idempotency-key``), newest first. ``--key`` shows only one key;
    ``--branches`` lists the staging branches still present (rejected or in flight).

    Args:
        subparsers: What ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to share options with (for example ``-v``).
    """
    parser = subparsers.add_parser(
        "commits", parents=list(parents), help="list the idempotent (staged) publishes on a config's Iceberg table",
        description="List the snapshots on main that carry ldp.idempotency-key, newest first.")
    parser.add_argument("config", metavar="CONFIG", help="path to a JSON dataset config")
    parser.add_argument("--key", help="show only the publish carrying this idempotency key")
    parser.add_argument("--branches", action="store_true", help="also list staging branches still present")
    parser.set_defaults(handler=_cmd_commits)


def commit_log(table, key: str | None = None) -> list[dict[str, Any]]:
    """The LDP publishes in ``main``'s ancestry, newest first, one row per snapshot carrying a key."""
    from pyiceberg.table.snapshots import ancestors_of

    rows = []
    for snapshot in ancestors_of(table.current_snapshot(), table.metadata):
        summary = _summary(snapshot)
        found = summary.get(IDEMPOTENCY_KEY_PROPERTY)
        if found is None or (key is not None and found != key):
            continue
        rows.append({
            "snapshot_id": snapshot.snapshot_id,
            "timestamp_ms": snapshot.timestamp_ms,
            "operation": snapshot.summary.operation.value if snapshot.summary is not None else None,
            "idempotency_key": found,
            "run_id": summary.get(RUN_ID_PROPERTY),
            "attempt": summary.get(ATTEMPT_PROPERTY),
            "total_records": summary.get("total-records"),
        })
    return rows


def _cmd_commits(args) -> int:
    import sys

    from local_data_platform.cli import format_table
    from local_data_platform.etl import load_config
    from local_data_platform.pipeline.builders import iceberg_from_config

    config = load_config(args.config)
    target = iceberg_from_config(config, "target", must_exist=True)
    table = target.table()
    rows = commit_log(table, key=args.key)
    lines = [f"LDP publishes on main of {target.identifier}, newest first:", format_table(rows) if rows else "(none)"]
    if args.branches:
        branches = [{"branch": name, "snapshot_id": sid} for name, sid in sorted(staged_branches(table).items())]
        lines += ["Staging branches:", format_table(branches) if branches else "(none)"]
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


__all__ = [
    "ATTEMPT_PROPERTY",
    "CommitConflict",
    "CommitContext",
    "CommitPolicy",
    "CommitSearchExhausted",
    "IDEMPOTENCY_HORIZON_S",
    "IDEMPOTENCY_KEY_PROPERTY",
    "INIT_PROPERTY",
    "LOGICAL_WINDOW_PROPERTY",
    "MAIN_BRANCH",
    "RUN_ID_PROPERTY",
    "SOURCE_PROPERTY_PREFIX",
    "SPEC_HASH_PROPERTY",
    "STAGED_MODES",
    "StagedWrite",
    "WriteResult",
    "add_cli",
    "check_upsert_columns",
    "attempt_prefix",
    "branch_name",
    "commit_log",
    "ensure_base",
    "fence",
    "fence_marker",
    "find_commit",
    "is_sequence_race",
    "new_run_id",
    "publish",
    "sequence_race_is_a_conflict",
    "snapshot_records",
    "stage",
    "staged_branches",
    "write_once",
]
