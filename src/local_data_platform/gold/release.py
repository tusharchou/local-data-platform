"""Gold releases: every gold table of a build, published together as one consistent version.

A gold table is rebuilt on its own, so two tables that are read at the same moment can come from different builds. The
daily sales table already has today, and the restaurant scorecard does not. A release fixes that for readers. When every
gold table of a folder passes its gate, ``ldp gold build`` cuts release ``rN``. The release names the snapshot that each
table ended the build with, and a reader that reads "the current release" sees all of those snapshots and no other, so
the tables always agree. A build that a gate blocks cuts no release, and the last release stays current.

A release is a file in the warehouse of its tables::

    <warehouse>/.ldp/releases/<namespace>/r3.json

The file lists each table with its snapshot id, the dataset version that pins it, the run that built it and its row
count. It is written to a temporary path and renamed, so a reader sees all of it or none of it. Each table is pinned
with :func:`local_data_platform.datasets.pin` as the dataset ``<namespace>.<table>`` with the property ``release=r3``.
A pin tags its snapshot, and snapshot expiry never removes a tagged snapshot, so ``ldp maintain`` keeps every released
snapshot. A release covers one namespace of one warehouse, because that is where its file lives, so a folder whose gold
tables are in more than one namespace has to be split into a folder for each.

Readers use the functions at the end of this module. A table that is in no release is read at its current snapshot::

    release = current_release("config")                     # None until the first build has cut one
    snapshot = snapshot_for("gold.daily_sales", release)    # the snapshot id to read, or None
    with DuckDBEngine() as engine:
        register_release(engine, "config")                  # every gold table, each at its snapshot in the release

Cutting a release also records it as a ``release.cut`` event for each table in the run events of the table's catalog,
so ``ldp runs`` shows the release in the history of the table.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

from local_data_platform.exceptions import ConfigError, LDPError
from local_data_platform.filelock import file_lock
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

if TYPE_CHECKING:
    from local_data_platform.gold.build import GoldConfig, TableOutcome

logger = get_logger(__name__)

RELEASES_DIR = Path(".ldp") / "releases"
"""Where the releases are, relative to the warehouse folder. Each namespace has a folder in it."""

LOCKS_DIR = Path(".ldp") / "locks"
"""Where the lock of a namespace is, relative to the warehouse folder."""

RELEASE_FORMAT = 1
"""The version of the release file layout, stored as ``ldp_release`` in every release."""

RELEASE_EVENT = "release.cut"
"""The run event that records a release for each table that it holds."""

_NAME = re.compile(r"r([1-9][0-9]*)")
_SAFE_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class ReleaseError(LDPError):
    """Raised when a release cannot be cut, or when a release that a reader asked for does not exist."""


# --------------------------------------------------------------------------- the release


@dataclass(frozen=True)
class ReleasedTable:
    """One gold table of a release.

    Attributes:
        table: The Iceberg table, ``<namespace>.<name>``.
        config: The file name of the config that defines the table.
        status: ``built`` when the build wrote the table, and ``checked`` when something else builds it and the build
            only checked it.
        snapshot_id: The snapshot of the table that the release names.
        rows: The rows that snapshot has.
        dataset: The name of the pinned dataset, which is the table.
        dataset_version: The version of that dataset that pins the snapshot.
        run_id: The run that built the table, or ``None`` for a table that was only checked.
    """

    table: str
    config: str
    status: str
    snapshot_id: int
    rows: int
    dataset: str
    dataset_version: int
    run_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The table as JSON-serialisable primitives."""
        return {"table": self.table, "config": self.config, "status": self.status, "snapshot_id": self.snapshot_id,
                "rows": self.rows, "dataset": self.dataset, "dataset_version": self.dataset_version,
                "run_id": self.run_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ReleasedTable:
        """Build a table from :meth:`to_dict` output.

        Raises:
            ReleaseError: If a required key is missing or has the wrong type.
        """
        try:
            return cls(table=str(data["table"]), config=str(data.get("config") or ""),
                       status=str(data.get("status") or ""), snapshot_id=int(data["snapshot_id"]),
                       rows=int(data["rows"]), dataset=str(data.get("dataset") or data["table"]),
                       dataset_version=int(data.get("dataset_version") or 0),
                       run_id=None if data.get("run_id") is None else str(data["run_id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ReleaseError(f"a table of the release is not valid: {error!r}") from error


@dataclass(frozen=True)
class Release:
    """One release of the gold tables of a namespace.

    Attributes:
        name: ``r<number>``.
        number: The number, from 1, which counts up for each release of the namespace.
        namespace: The namespace of the tables.
        created_at: When the release was cut, in UTC.
        tables: The tables, by name.
        window: The window of the workflow run that cut the release, as UTC instants, or ``None`` for a build that
            ran outside a workflow.
        workflow: The workflow, its run and its task, for a release that a workflow cut, else ``None``.
        ldp_version: The version of LDP that cut it.
        path: The release file, when it was read from one or written to one.
    """

    name: str
    number: int
    namespace: str
    created_at: dt.datetime
    tables: tuple[ReleasedTable, ...]
    window: tuple[str, str] | None = None
    workflow: Mapping[str, str] | None = None
    ldp_version: str | None = None
    path: Path | None = field(default=None, compare=False)

    @property
    def rows(self) -> int:
        """The rows of every table of the release."""
        return sum(item.rows for item in self.tables)

    def table(self, identifier: str) -> ReleasedTable | None:
        """The entry for ``identifier``, such as ``gold.daily_sales``, or ``None`` when the release does not hold it."""
        return next((item for item in self.tables if item.table == identifier), None)

    def snapshots(self) -> dict[str, int]:
        """The snapshot id of each table, by table."""
        return {item.table: item.snapshot_id for item in self.tables}

    def to_dict(self) -> dict[str, Any]:
        """The release as JSON-serialisable primitives, which is what the release file holds."""
        data: dict[str, Any] = {
            "ldp_release": RELEASE_FORMAT, "release": self.name, "number": self.number, "namespace": self.namespace,
            "created_at": self.created_at.astimezone(dt.timezone.utc).isoformat(), "ldp_version": self.ldp_version,
            "tables": [item.to_dict() for item in self.tables]}
        if self.window is not None:
            data["window"] = list(self.window)
        if self.workflow is not None:
            data["workflow"] = dict(self.workflow)
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], path: str | os.PathLike | None = None) -> Release:
        """Build a release from :meth:`to_dict` output. Unknown keys are ignored.

        Raises:
            ReleaseError: If a required key is missing or has the wrong type.
        """
        where = f" in {path}" if path is not None else ""
        try:
            number = parse_release_name(data["release"])
            created = dt.datetime.fromisoformat(str(data["created_at"]))
            window = data.get("window")
            workflow = data.get("workflow")
            return cls(
                name=f"r{number}", number=number, namespace=str(data["namespace"]),
                created_at=created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc),
                tables=tuple(ReleasedTable.from_dict(item) for item in data["tables"]),
                window=(str(window[0]), str(window[1])) if window else None,
                workflow={str(key): str(value) for key, value in workflow.items()} if workflow else None,
                ldp_version=data.get("ldp_version"), path=Path(path) if path is not None else None)
        except ReleaseError as error:
            raise ReleaseError(f"invalid release{where}: {error}") from error
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise ReleaseError(f"invalid release{where}: {error!r}") from error


def parse_release_name(text: Any) -> int:
    """The number of a release called ``r3`` (or ``R3``).

    Raises:
        ReleaseError: If ``text`` does not name a release.
    """
    match = _NAME.fullmatch(str(text).strip().lower())
    if match is None:
        raise ReleaseError(f"{str(text).strip()!r} is not a release name. A release is called r1, r2, r3 and so on")
    return int(match.group(1))


# --------------------------------------------------------------------------- where a release lives


@dataclass(frozen=True)
class ReleaseScope:
    """The place the releases of some gold tables are kept: one namespace of one warehouse.

    Attributes:
        root: The warehouse folder that holds ``.ldp``.
        namespace: The namespace of the tables.
    """

    root: Path
    namespace: str

    @property
    def folder(self) -> Path:
        """``<warehouse>/.ldp/releases/<namespace>``."""
        return self.root / RELEASES_DIR / self.namespace

    @property
    def lock(self) -> Path:
        """The file that serialises the cutting of releases for the namespace, on one machine."""
        return self.root / LOCKS_DIR / f"releases-{self.namespace}.lock"

    def __str__(self) -> str:
        return f"{self.namespace} in {self.root}"


def _warehouse_root(gold: GoldConfig, catalog: Mapping[str, Any]) -> Path:
    """The folder that holds the ``.ldp`` of a gold table, which is the warehouse folder of its catalog, else
    ``$LDP_WAREHOUSE``."""
    from local_data_platform import fs
    from local_data_platform.datasets import WAREHOUSE_ENV

    where = catalog.get("warehouse_path")
    if not where:
        other = catalog.get("warehouse")
        if isinstance(other, str) and other.strip() and fs.uri_scheme(other) in (None, "file"):
            where = unquote(urlparse(other).path) if fs.uri_scheme(other) == "file" else other
    if where:
        return resolve_path(where, gold.config.base_dir)
    env = os.environ.get(WAREHOUSE_ENV)
    if env:
        return resolve_path(env)
    raise ReleaseError(f"{gold.table} is in a catalog that has no warehouse folder, so there is nowhere to keep its "
                       f"releases. Set {WAREHOUSE_ENV} to a folder")


def scope_of(gold: GoldConfig) -> ReleaseScope:
    """Where the releases of a gold table are kept.

    Raises:
        ReleaseError: If the table's catalog has no warehouse folder and ``$LDP_WAREHOUSE`` is not set.
    """
    from local_data_platform.catalog.provider import catalog_namespace

    catalog = gold.config.metadata["target"].get("catalog") or {}
    namespace = catalog_namespace(catalog)
    if not _SAFE_NAMESPACE.fullmatch(namespace) or ".." in namespace:
        raise ReleaseError(f"{gold.table} is in the namespace {namespace!r}, which cannot name a folder of releases. "
                           "Use letters, digits, '_', '-' and '.'")
    return ReleaseScope(_warehouse_root(gold, catalog), namespace)


def release_scope(golds: Sequence[GoldConfig]) -> ReleaseScope:
    """The one place where the releases of these gold tables are kept.

    Raises:
        ReleaseError: If there are no tables, or they are in more than one namespace or warehouse, or one of them is in
            a catalog with no warehouse folder.
    """
    if not golds:
        raise ReleaseError("there is no gold table to release")
    scopes: dict[ReleaseScope, list[str]] = {}
    for gold in golds:
        scopes.setdefault(scope_of(gold), []).append(gold.table)
    if len(scopes) > 1:
        places = "; ".join(f"{', '.join(tables[:3])}{', ...' if len(tables) > 3 else ''} in {scope.namespace} of "
                           f"{scope.root}" for scope, tables in scopes.items())
        raise ReleaseError(f"the gold tables are in more than one place ({places}), and a release covers one "
                           "namespace of one warehouse. Give each its own folder of configs")
    return next(iter(scopes))


def _numbered_files(folder: Path) -> list[tuple[int, Path]]:
    found = []
    if folder.is_dir():
        for path in folder.iterdir():
            match = re.fullmatch(r"r([1-9][0-9]*)\.json", path.name)
            if match and path.is_file():
                found.append((int(match.group(1)), path))
    return sorted(found)


def releases_in(scope: ReleaseScope) -> list[Release]:
    """Every release of a namespace, newest first. A file that cannot be read is skipped with a warning."""
    releases = []
    for number, path in reversed(_numbered_files(scope.folder)):
        try:
            release = Release.from_dict(json.loads(path.read_text(encoding="utf-8")), path)
            if release.number != number or release.namespace != scope.namespace:
                raise ReleaseError(f"invalid release in {path}: it says {release.name} of {release.namespace}")
        except (OSError, ValueError, ReleaseError) as error:
            logger.warning("Skipping the unreadable release file %s: %s", path, error)
            continue
        releases.append(release)
    return releases


def release_in(scope: ReleaseScope, name: str) -> Release:
    """One release of a namespace.

    Raises:
        ReleaseError: If the name is not a release name, or the namespace has no such release. The message lists the
            releases that it has.
    """
    number = parse_release_name(name)
    known = releases_in(scope)
    for release in known:
        if release.number == number:
            return release
    have = ", ".join(release.name for release in reversed(known)) or "none yet"
    raise ReleaseError(f"there is no release r{number} of {scope}. It has {have}")


def snapshot_for(table: str, release: Release | None) -> int | None:
    """The snapshot of ``table`` in ``release``, which is what a reader passes as ``snapshot_id``.

    Args:
        table: ``<namespace>.<name>``, such as ``gold.daily_sales``.
        release: A release, or ``None`` for a reader that reads live.

    Returns:
        The snapshot id, or ``None`` when there is no release or the release does not hold the table, so the reader
        reads the current snapshot of the table.
    """
    if release is None:
        return None
    item = release.table(table)
    return item.snapshot_id if item is not None else None


# --------------------------------------------------------------------------- cutting a release


@dataclass(frozen=True)
class ReleaseContext:
    """What a build knows about the run that cuts the release.

    Attributes:
        window: The window of the workflow run, as UTC ``(start, end)``, for a build that a workflow runs.
        workflow: The workflow, its run and its task, as ``{"identifier", "run_id", "task"}``.
    """

    window: tuple[dt.datetime, dt.datetime] | None = None
    workflow: Mapping[str, str] | None = None


@dataclass
class ReleaseOutcome:
    """What a build did about its release.

    Attributes:
        release: The release that was cut. When none was, the release that readers still see, or ``None`` if there is
            none yet.
        cut: Whether this build cut ``release``.
        note: Why none was cut, in a few words.
        error: Why the release could not be cut, when it failed.
    """

    release: Release | None = None
    cut: bool = False
    note: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The outcome as JSON-serialisable primitives."""
        release = self.release
        return {"cut": self.cut, "release": release.name if release else None,
                "namespace": release.namespace if release else None, "tables": len(release.tables) if release else 0,
                "path": str(release.path) if release and release.path else None, "note": self.note,
                "error": self.error}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _instants(window: tuple[dt.datetime, dt.datetime] | None) -> tuple[str, str] | None:
    if window is None:
        return None
    from local_data_platform.spec import format_instant

    return format_instant(window[0]), format_instant(window[1])


def _current_snapshot(iceberg: Any, table: str) -> int:
    snapshot = iceberg.table().current_snapshot()
    if snapshot is None:
        raise ReleaseError(f"{table} has no snapshot yet, so it cannot be in a release")
    return int(snapshot.snapshot_id)


def _write_release(scope: ReleaseScope, release: Release) -> Path:
    """Write the release file so that it appears whole, and never replaces a release of the same number."""
    folder = scope.folder
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"{release.name}.json"
    tmp = folder / f".{release.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(release.to_dict(), indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, final)
        except FileExistsError:
            raise ReleaseError(f"{release.name} of {scope} was cut by another process at the same time. Nothing "
                               "changed for readers, so build again") from None
        except OSError:  # a filesystem without hard links
            if final.exists():
                raise ReleaseError(f"{release.name} of {scope} was cut by another process at the same time. Nothing "
                                   "changed for readers, so build again") from None
            os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return final


def cut_release(tables: Sequence[TableOutcome], *, context: ReleaseContext | None = None,
                sink_for: Callable[[GoldConfig], Any] | None = None) -> ReleaseOutcome:
    """Cut the next release of the gold tables a build ended with, unless it would hold what the current one holds.

    Every table has to have been built or checked. Each is pinned at the snapshot that its run ended with, or at the
    one that was checked, and not at whatever the table has by the time the pin is made, so a writer that appends to a
    table meanwhile does not change what the release names. The numbers of a namespace count up under a lock, so two
    builds on one machine do not take the same one, and the release file is made last.

    Args:
        tables: The outcomes of the tables of the build, which are all in one namespace of one warehouse.
        context: The window and the workflow of the run that cuts the release.
        sink_for: Returns the event sink of a table. By default the sinks of its config, and the ``iceberg`` sink when
            it names none, so the release shows in ``ldp runs`` for the table.

    Returns:
        The outcome. ``cut`` is false and ``release`` is the current release when the tables are at the snapshots the
        current release already names.

    Raises:
        ReleaseError: If a table was not built or checked, the tables are not in one place, a table has no snapshot, or
            a snapshot could not be tagged, which would let expiry remove it. No release file is made.
    """
    from local_data_platform import __version__
    from local_data_platform.datasets import pin
    from local_data_platform.gold.build import BUILT, CHECKED
    from local_data_platform.pipeline.builders import iceberg_from_config

    context = context or ReleaseContext()
    ordered = sorted(tables, key=lambda item: item.gold.table)
    not_done = [item.gold.table for item in ordered if item.status not in (BUILT, CHECKED)]
    if not_done:
        raise ReleaseError(f"{', '.join(not_done)} {'was' if len(not_done) == 1 else 'were'} not built or checked, so "
                           "there is no release to cut")
    scope = release_scope([item.gold for item in ordered])
    opened = {item.gold.table: iceberg_from_config(item.gold.config, "target", must_exist=True) for item in ordered}
    snapshots = {item.gold.table: item.snapshot_id if item.snapshot_id is not None
                 else _current_snapshot(opened[item.gold.table], item.gold.table) for item in ordered}

    with file_lock(scope.lock):
        known = releases_in(scope)
        current = known[0] if known else None
        if current is not None and current.snapshots() == snapshots:
            return ReleaseOutcome(current, cut=False, note="the tables are at the snapshots of the current release")
        taken = max((number for number, _ in _numbered_files(scope.folder)), default=0)
        number = taken + 1  # a file that cannot be read still holds its number, so the next release does not collide
        name = f"r{number}"
        released = []
        for item in ordered:
            table = item.gold.table
            try:
                version = pin(opened[table], table, snapshot_id=snapshots[table], properties={"release": name},
                              warehouse=scope.root)
            except LDPError as error:
                raise ReleaseError(f"cannot pin {table} for release {name}: {error}") from error
            if version.tag is None:
                raise ReleaseError(f"the snapshot {version.snapshot_id} of {table} could not be tagged, so snapshot "
                                   f"expiry could remove it, and release {name} was not cut")
            run = item.result
            released.append(ReleasedTable(
                table=table, config=item.gold.path.name, status=item.status, snapshot_id=version.snapshot_id,
                rows=version.row_count, dataset=version.name, dataset_version=version.version,
                run_id=getattr(run, "run_id", None) if run is not None else None))
        release = Release(name=name, number=number, namespace=scope.namespace, created_at=_now(),
                          tables=tuple(released), window=_instants(context.window),
                          workflow=dict(context.workflow) if context.workflow else None, ldp_version=__version__)
        written = _write_release(scope, release)
    release = dataclasses.replace(release, path=written)
    logger.info("Cut release %s of %s with %d tables (%s)", name, scope, len(released), written)
    _record(release, ordered, sink_for)
    return ReleaseOutcome(release, cut=True)


def _record(release: Release, outcomes: Sequence[TableOutcome], sink_for: Callable[[GoldConfig], Any] | None) -> None:
    """Emit a ``release.cut`` event for each table, so ``ldp runs`` shows the release. Never fails the release."""
    from local_data_platform.events import RunEmitter, sink_from_config
    from local_data_platform.gold.build import with_events

    by_table = {item.gold.table: item.gold for item in outcomes}
    for entry in release.tables:
        gold = by_table[entry.table]
        try:
            sink = sink_for(gold) if sink_for is not None else sink_from_config(with_events(gold.config))
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ldp-release:{release.path}:{entry.table}"))
            emitter = RunEmitter(sink, run_id, context={"pipeline": gold.config.identifier, "table": entry.table})
            payload: dict[str, Any] = {
                "release": release.name, "namespace": release.namespace, "snapshot_id": entry.snapshot_id,
                "rows": entry.rows, "dataset": entry.dataset, "dataset_version": entry.dataset_version,
                "status": entry.status, "tables": len(release.tables)}
            if release.window is not None:
                payload["window"] = list(release.window)
            emitter.emit(RELEASE_EVENT, payload)
            emitter.flush()
        except Exception as error:  # noqa: BLE001 - the release is cut, and an event is only a record of it
            logger.warning("Could not record release %s for %s: %s", release.name, entry.table, error)


# --------------------------------------------------------------------------- what readers use


def _folder_scope(folder: str | os.PathLike) -> tuple[list[GoldConfig], ReleaseScope]:
    from local_data_platform.gold.build import find_gold

    found = find_gold(folder)
    if not found:
        raise ConfigError(f"{resolve_path(folder)} has no gold config. A gold config has metadata.docs.layer set to "
                          "gold")
    return found, release_scope(found)


def folder_scope(folder: str | os.PathLike) -> ReleaseScope:
    """Where the releases of the gold tables of a folder of configs are kept.

    Raises:
        ConfigError: If the folder has no gold config or a config is not valid.
        ReleaseError: If the gold tables are not in one namespace of one warehouse.
    """
    return _folder_scope(folder)[1]


def list_releases(folder: str | os.PathLike) -> list[Release]:
    """The releases of the gold tables of a folder of configs, newest first.

    Raises:
        ConfigError: If the folder has no gold config or a config is not valid.
        ReleaseError: If the gold tables are not in one namespace of one warehouse.
    """
    return releases_in(_folder_scope(folder)[1])


def current_release(folder: str | os.PathLike) -> Release | None:
    """The release that readers of the gold tables of a folder see now, or ``None`` before the first is cut."""
    known = list_releases(folder)
    return known[0] if known else None


def get_release(folder: str | os.PathLike, name: str) -> Release:
    """One release of the gold tables of a folder, by name such as ``r3``.

    Raises:
        ReleaseError: If there is no such release. The message lists the releases that exist.
    """
    return release_in(_folder_scope(folder)[1], name)


def _pick(scope: ReleaseScope, release: str | None, live: bool) -> Release | None:
    """The release a reader reads in one namespace: the one it names, else the current one, and none when it is live."""
    if release is not None and live:
        raise ConfigError("pass either a release or --live, not both")
    if live:
        return None
    if release is not None:
        return release_in(scope, release)
    known = releases_in(scope)
    return known[0] if known else None


def choose_release(folder: str | os.PathLike, *, release: str | None = None, live: bool = False) -> Release | None:
    """The release a reader should read, by the rule every reader of the gold layer follows.

    Args:
        folder: The folder of configs.
        release: A release name such as ``r1``, to read that one.
        live: Read the tables as they are now, which is ``main``, and no release.

    Returns:
        The release named, or the current one by default, or ``None`` for ``live`` and for a folder that has no release
        yet, so the tables are read as they are.

    Raises:
        ConfigError: If both ``release`` and ``live`` are given.
        ReleaseError: If ``release`` names a release that does not exist.
    """
    return _pick(_folder_scope(folder)[1], release, live)


def release_snapshots(folder: str | os.PathLike, *, release: str | None = None, live: bool = False) -> dict[str, int]:
    """The snapshot to read for each gold table of a folder, by table, under the rule of :func:`choose_release`.

    A table that is not in the release is left out, and a reader reads its current snapshot. The dict is empty for
    ``live``, and for a folder that has no release yet.
    """
    chosen = choose_release(folder, release=release, live=live)
    return dict(chosen.snapshots()) if chosen is not None else {}


def register_release(engine: Any, folder: str | os.PathLike, *, release: str | None = None,
                     live: bool = False) -> dict[str, int]:
    """Register every gold table of a folder in a ``DuckDBEngine``, each at its snapshot in the release.

    A table is registered as ``<namespace>.<table>``, and works in SQL as that and by its bare name. A table that the
    release does not hold, and every table when ``live`` is set or no release has been cut, is registered at its
    current snapshot.

    Returns:
        The snapshot that each table was registered at, for the tables that a release named.

    Raises:
        ReleaseError: If ``release`` names a release that does not exist, or a snapshot of it is no longer there.
        TableNotFound: If a gold table does not exist yet.
    """
    from local_data_platform.lake import name_views
    from local_data_platform.pipeline.builders import iceberg_from_config

    found, scope = _folder_scope(folder)
    chosen = _pick(scope, release, live)
    bare = Counter(gold.config.metadata["target"]["name"] for gold in found)
    used: dict[str, int] = {}
    for gold in found:
        table = iceberg_from_config(gold.config, "target", must_exist=True)
        snapshot = snapshot_for(gold.table, chosen)
        try:
            engine.register_iceberg(table, gold.table, snapshot_id=snapshot)
        except ValueError as error:  # the snapshot is gone, and the message lists the ones that exist
            raise ReleaseError(f"release {chosen.name if chosen else ''} names a snapshot of {gold.table} that no "
                               f"longer exists. {error}") from error
        name = str(gold.config.metadata["target"]["name"])
        name_views(engine, gold.table, scope.namespace, name, unique=bare[name] == 1)
        if snapshot is not None:
            used[gold.table] = snapshot
    return used


__all__ = [
    "LOCKS_DIR",
    "RELEASES_DIR",
    "RELEASE_EVENT",
    "RELEASE_FORMAT",
    "Release",
    "ReleaseContext",
    "ReleaseError",
    "ReleaseOutcome",
    "ReleaseScope",
    "ReleasedTable",
    "choose_release",
    "current_release",
    "cut_release",
    "folder_scope",
    "get_release",
    "list_releases",
    "parse_release_name",
    "register_release",
    "release_in",
    "release_scope",
    "release_snapshots",
    "releases_in",
    "scope_of",
    "snapshot_for",
]
