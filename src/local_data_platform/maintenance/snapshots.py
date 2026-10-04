"""Snapshot expiry that keeps the idempotency floor intact.

The staged publish protocol (SaaS design §7.4-§7.5) decides "was this key already published?" by walking
``main``'s ancestry with ``find_commit``. That answer is only right while every snapshot carrying a key
inside the idempotency horizon is still in the table and still reachable from ``main``. So on top of the
usual rules (keep the current snapshot, every branch and tag head, and the last ``retain_last`` snapshots of
``main``), :func:`expire_snapshots` keeps:

* every snapshot whose summary carries ``ldp.idempotency-key`` and whose timestamp is at or after the floor;
* every snapshot between a branch head and the deepest such keyed snapshot in that branch's ancestry.
  pyiceberg 0.12 sets ``parent-snapshot-id`` to null on the children of an expired snapshot
  (``pyiceberg/table/update/__init__.py``, the ``RemoveSnapshotsUpdate`` handler), so expiring a gap would
  silently cut ``find_commit``'s walk short and let a key be published twice.

The commit goes straight to ``Catalog.commit_table`` with pyiceberg's ``RemoveSnapshotsUpdate`` (the update the
``ExpireSnapshots`` builder emits), asserting the table UUID and the snapshot id of every ref. The builder's
own commit asserts only the table UUID, and ``RemoveSnapshotsUpdate`` also drops any ref that points at a
removed snapshot, so a rollback racing the expiry could otherwise delete ``main``. A conflicting commit
makes the expiry re-plan against the new metadata.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pyiceberg.exceptions import CommitFailedException
from pyiceberg.table.refs import MAIN_BRANCH, SnapshotRefType
from pyiceberg.table.snapshots import ancestors_of

from local_data_platform.logger import get_logger
from local_data_platform.maintenance._common import (
    DEFAULT_HORIZON_DAYS,
    HORIZON_PROPERTY,
    IDEMPOTENCY_KEY,
    MaintenanceError,
    as_table,
    check_count,
    iso_millis,
    resolve_instant,
    table_name,
    to_millis,
    utc_now,
)

logger = get_logger(__name__)

DEFAULT_RETAIN_LAST = 20
"""How many of ``main``'s most recent snapshots :func:`expire_snapshots` keeps by default."""

MAX_COMMIT_ATTEMPTS = 3
"""How many times :func:`expire_snapshots` re-plans after a concurrent commit before giving up."""

PROTECTION_REASONS = ("current", "refs", "retain_last", "branch_min_snapshots", "idempotency_key", "key_ancestry")
"""Why a snapshot was kept, as reported under ``protected_snapshot_ids``."""


@dataclass
class ExpiryPlan:
    """What an expiry would do to one version of the table metadata.

    Attributes:
        expire: Snapshot ids to expire, sorted.
        protected: For each reason in :data:`PROTECTION_REASONS`, the ids it keeps.
    """

    expire: list[int]
    protected: dict[str, set[int]] = field(default_factory=dict)

    def protected_ids(self) -> set[int]:
        """Every id kept for any reason."""
        return set().union(*self.protected.values()) if self.protected else set()


def expiry_supported() -> bool:
    """Whether this pyiceberg can apply snapshot removal (``ExpireSnapshots`` arrived in pyiceberg 0.10).

    pyiceberg 0.9 defines the ``RemoveSnapshotsUpdate`` model but can't apply it to metadata, so a SQL or
    local catalog would reject the commit.
    """
    try:
        from pyiceberg.table.update.snapshot import ExpireSnapshots  # noqa: F401
    except ImportError:
        return False
    return True


def _pyiceberg_version() -> str:
    try:
        from importlib.metadata import version

        return version("pyiceberg")
    except Exception:  # noqa: BLE001 - the version is only used in an error message
        return "unknown"


def horizon_days(properties: Mapping[str, str]) -> float:
    """The idempotency horizon of a table, from ``ldp.idempotency.horizon-days`` (default 7).

    Raises:
        MaintenanceError: If the property is set but isn't a non-negative number. Guessing a horizon could
            expire keys that must stay findable, so this fails closed.
    """
    raw = properties.get(HORIZON_PROPERTY)
    if raw is None:
        return DEFAULT_HORIZON_DAYS
    try:
        days = float(raw)
    except ValueError:
        days = -1.0
    if not days >= 0:
        raise MaintenanceError(f"table property {HORIZON_PROPERTY}={raw!r} is not a non-negative number of days")
    return days


def _is_keyed(snapshot: Any) -> bool:
    summary = snapshot.summary
    if summary is None:
        return False
    return bool(summary.additional_properties.get(IDEMPOTENCY_KEY))


def _branch_heads(metadata: Any) -> list[tuple[str, int, Any]]:
    """``(name, snapshot_id, ref)`` for every ref, with the current snapshot standing in for a missing main."""
    heads = [(name, ref.snapshot_id, ref) for name, ref in metadata.refs.items()]
    if MAIN_BRANCH not in metadata.refs and metadata.current_snapshot_id is not None:
        heads.append((MAIN_BRANCH, metadata.current_snapshot_id, None))
    return heads


def plan_expiry(metadata: Any, *, older_than_ms: int, retain_last: int, floor_ms: int) -> ExpiryPlan:
    """Decide which snapshots of ``metadata`` to expire. Pure: it reads the metadata and changes nothing.

    Args:
        metadata: A pyiceberg ``TableMetadata``.
        older_than_ms: Only snapshots with a timestamp before this (epoch milliseconds) may expire.
        retain_last: Keep this many of ``main``'s most recent snapshots, the current one included.
        floor_ms: Keep every snapshot carrying ``ldp.idempotency-key`` with a timestamp at or after this,
            and the ancestry that links it to its branch head.

    Returns:
        The plan.
    """
    snapshots = {snapshot.snapshot_id: snapshot for snapshot in metadata.snapshots}
    protected: dict[str, set[int]] = {reason: set() for reason in PROTECTION_REASONS}

    if metadata.current_snapshot_id in snapshots:
        protected["current"].add(metadata.current_snapshot_id)
    for snapshot in snapshots.values():
        if _is_keyed(snapshot) and snapshot.timestamp_ms >= floor_ms:
            protected["idempotency_key"].add(snapshot.snapshot_id)

    for name, head_id, ref in _branch_heads(metadata):
        if head_id in snapshots:
            protected["refs"].add(head_id)
        is_branch = ref is None or ref.snapshot_ref_type == SnapshotRefType.BRANCH
        if not is_branch or head_id not in snapshots:
            continue
        ancestry = [snapshot.snapshot_id for snapshot in ancestors_of(snapshots[head_id], metadata)]
        ref_minimum = (ref.min_snapshots_to_keep or 0) if ref is not None else 0
        if name == MAIN_BRANCH:
            protected["retain_last"].update(ancestry[:max(retain_last, ref_minimum)])
        else:
            protected["branch_min_snapshots"].update(ancestry[:max(1, ref_minimum)])
        keyed = [index for index, snapshot_id in enumerate(ancestry) if snapshot_id in protected["idempotency_key"]]
        if keyed:
            protected["key_ancestry"].update(ancestry[: keyed[-1] + 1])

    kept = set().union(*protected.values())
    expire = sorted(
        snapshot_id for snapshot_id, snapshot in snapshots.items()
        if snapshot.timestamp_ms < older_than_ms and snapshot_id not in kept
    )
    return ExpiryPlan(expire=expire, protected=protected)


def _floor_ms(table: Any, protect_keys_since: Any, now: dt.datetime) -> int:
    horizon = horizon_days(table.metadata.properties)
    default_floor = now - dt.timedelta(days=horizon)
    if protect_keys_since is None:
        return to_millis(default_floor)
    floor = resolve_instant(protect_keys_since, now, "protect_keys_since")
    if HORIZON_PROPERTY in table.metadata.properties and floor > default_floor:
        logger.warning(
            "protect_keys_since %s is newer than the idempotency horizon of %s (%s days); idempotency keys "
            "between the two can be expired, so a re-run of those windows could publish twice",
            floor.isoformat(), table_name(table), horizon,
        )
    return to_millis(floor)


def _report(table: Any, plan: ExpiryPlan, *, dry_run: bool, older_than_ms: int, floor_ms: int,
            retain_last: int, attempts: int) -> dict[str, Any]:
    before = len(table.metadata.snapshots)
    return {
        "table": table_name(table),
        "dry_run": dry_run,
        "committed": False,
        "older_than": iso_millis(older_than_ms),
        "protect_keys_since": iso_millis(floor_ms),
        "retain_last": retain_last,
        "snapshots_before": before,
        "snapshots_after": before - len(plan.expire),
        "expired_snapshot_ids": list(plan.expire),
        "protected_snapshot_ids": {reason: sorted(ids) for reason, ids in plan.protected.items()},
        "attempts": attempts,
        "metadata_location": table.metadata_location,
    }


def _commit_expiry(table: Any, plan: ExpiryPlan) -> None:
    """Remove ``plan.expire`` in one catalog CAS that also pins every ref to the planned snapshot."""
    from pyiceberg.table.update import AssertRefSnapshotId, AssertTableUUID, RemoveSnapshotsUpdate

    metadata = table.metadata
    requirements = [AssertTableUUID(uuid=metadata.table_uuid)]
    requirements += [AssertRefSnapshotId(ref=name, snapshot_id=ref.snapshot_id) for name, ref in metadata.refs.items()]
    if MAIN_BRANCH not in metadata.refs:
        requirements.append(AssertRefSnapshotId(ref=MAIN_BRANCH, snapshot_id=None))
    table.catalog.commit_table(table, tuple(requirements), (RemoveSnapshotsUpdate(snapshot_ids=list(plan.expire)),))


def expire_snapshots(
    table: Any,
    *,
    older_than: dt.datetime | dt.timedelta,
    retain_last: int = DEFAULT_RETAIN_LAST,
    protect_keys_since: dt.datetime | dt.timedelta | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Expire old snapshots without breaking the idempotency floor.

    A snapshot expires only when it is older than ``older_than`` and none of these keep it:

    * it is the current snapshot, or the head of any branch or tag;
    * it is one of ``main``'s last ``retain_last`` snapshots, or one of a branch's last
      ``min-snapshots-to-keep`` snapshots (1 when the branch doesn't set it; on ``main`` the larger of the
      two counts wins);
    * it carries ``ldp.idempotency-key`` and was committed at or after the floor, ``protect_keys_since``;
    * it lies between a branch head and the deepest keyed snapshot the previous rule keeps, so
      ``find_commit`` can still walk to that key.

    Only table metadata changes. The data and manifest files the expired snapshots referenced stay on disk
    until :func:`~local_data_platform.maintenance.remove_orphans` reclaims them.

    Args:
        table: A pyiceberg ``Table`` or an :class:`~local_data_platform.format.iceberg.Iceberg` format.
            A pyiceberg table is refreshed in place after the commit.
        older_than: Only snapshots committed before this may expire: a ``datetime`` (naive means UTC), or a
            ``timedelta`` measured back from now.
        retain_last: Keep this many of ``main``'s most recent snapshots. At least 1.
        protect_keys_since: The idempotency floor, as a ``datetime`` or a ``timedelta`` back from now.
            ``None`` uses the table's ``ldp.idempotency.horizon-days`` property (default 7 days), which is what
            keeps ``find_commit`` correct. A later floor than the table's horizon is honoured with a warning.
        dry_run: Plan only, and change nothing. Works on every supported pyiceberg.

    Returns:
        A JSON-ready report: ``table``, ``dry_run``, ``committed``, ``older_than`` and
        ``protect_keys_since`` (ISO UTC), ``retain_last``, ``snapshots_before``, ``snapshots_after``,
        ``expired_snapshot_ids`` (the ids expired, or that would be on a dry run),
        ``protected_snapshot_ids`` (reason -> ids), ``attempts`` and ``metadata_location``.

    Raises:
        TypeError: If an argument has the wrong type.
        ValueError: If ``retain_last`` is below 1 or a ``timedelta`` is negative.
        NotImplementedError: If the installed pyiceberg can't expire snapshots (0.9) and ``dry_run`` is false.
        MaintenanceError: If the horizon property is invalid, or concurrent commits kept invalidating the
            plan ``MAX_COMMIT_ATTEMPTS`` times.
    """
    retain_last = check_count(retain_last, "retain_last", 1)
    tbl = as_table(table)
    now = utc_now()
    older_than_ms = to_millis(resolve_instant(older_than, now, "older_than"))
    if protect_keys_since is not None:
        resolve_instant(protect_keys_since, now, "protect_keys_since")  # validate before any work
    current = tbl
    for attempt in range(1, MAX_COMMIT_ATTEMPTS + 1):
        floor_ms = _floor_ms(current, protect_keys_since, now)
        plan = plan_expiry(current.metadata, older_than_ms=older_than_ms, retain_last=retain_last, floor_ms=floor_ms)
        report = _report(current, plan, dry_run=dry_run, older_than_ms=older_than_ms, floor_ms=floor_ms,
                         retain_last=retain_last, attempts=attempt)
        if dry_run or not plan.expire:
            logger.info("%s snapshot expiry for %s: %d of %d snapshots %s", "Planned" if dry_run else "No-op",
                        report["table"], len(plan.expire), report["snapshots_before"],
                        "would expire" if dry_run else "expire")
            return report
        if not expiry_supported():
            raise NotImplementedError(
                f"expiring snapshots needs pyiceberg >= 0.10 (installed: {_pyiceberg_version()}); "
                "pass dry_run=True to see the plan, or upgrade pyiceberg"
            )
        try:
            _commit_expiry(current, plan)
        except CommitFailedException as exc:
            logger.info("Snapshot expiry of %s conflicted with a concurrent commit (attempt %d/%d): %s",
                        report["table"], attempt, MAX_COMMIT_ATTEMPTS, exc)
            if attempt == MAX_COMMIT_ATTEMPTS:
                raise MaintenanceError(
                    f"snapshot expiry of {report['table']} conflicted with concurrent commits "
                    f"{MAX_COMMIT_ATTEMPTS} times; nothing was expired"
                ) from exc
            current = tbl.catalog.load_table(tbl.name())
            continue
        tbl.refresh()
        report.update(committed=True, snapshots_after=len(tbl.metadata.snapshots),
                      metadata_location=tbl.metadata_location)
        logger.info("Expired %d of %d snapshots of %s (floor %s)", len(plan.expire), report["snapshots_before"],
                    report["table"], report["protect_keys_since"])
        return report
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = ["DEFAULT_RETAIN_LAST", "ExpiryPlan", "PROTECTION_REASONS", "expire_snapshots", "expiry_supported",
           "horizon_days", "plan_expiry"]
