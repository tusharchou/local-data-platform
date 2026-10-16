"""The gold catalog: one entry for each table of a folder of configs, what it means, who owns it and how it is doing.

``ldp gold list``, ``show`` and ``docs`` print the entries, and 0.1.8 search uses them as its documents, one
:meth:`CatalogEntry.document` for each table. An entry says

* **what the table is**: its layer, description, owner, grain and key, the type and description of each column, and the
  tags and the refresh that its config promises. The contract is the one the config writes in ``metadata.docs`` and the
  one the Iceberg table carries, which ``ldp run`` and ``ldp gold sync`` write into it. The config wins where both have
  a value, because it is what the next run writes, and an entry says so when the table does not carry it yet. A table
  with no contract is an entry with no description and not an error.
* **how it is doing**: its rows, when its data last changed, the status of its last run, the verdict of its last
  quality check, and the gold release it is in. The run and quality status come from
  :func:`local_data_platform.gold.status.ldp_status`, which ``ldp mcp`` asks too.
* **what it lacks**: for a gold table, the parts of its contract that are missing. ``problems`` is what
  ``ldp gold list --strict`` fails on.

An entry made for ``show`` or ``docs`` also has the last runs, every release that holds the table, a few sample rows and
the ``ldp query`` line that reads it.

Tables are the Iceberg targets of the dataset configs of one folder, the tables that ``ldp query`` reads from it. The
catalogs are only read. A catalog that does not exist yet is not created, and a table that does not exist yet is an
entry that says so.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform.contracts import GOLD, Docs, pending_changes, read_contract, resolve_layer
from local_data_platform.exceptions import ConfigError
from local_data_platform.gold import release as releases
from local_data_platform.gold.build import GoldConfig, find_configs
from local_data_platform.gold.status import int_or_none, ldp_status, snapshot_operation, snapshot_summary
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path
from local_data_platform.quality.report import to_jsonable

logger = get_logger(__name__)

DEFAULT_RUNS = 5
"""How many of the last runs of a table ``show`` lists."""

DEFAULT_SAMPLE = 5
"""How many rows of a table ``show`` prints as a sample."""

NO_DESCRIPTION = "no description"
"""What a table with no description is shown as. In JSON its description is ``null``."""

_LAYER_ORDER = {GOLD: 0, "silver": 1, "bronze": 2}
_PLAIN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LIST_AT_MOST = 5


# --------------------------------------------------------------------------- the entries


@dataclass(frozen=True)
class ColumnEntry:
    """One column of a table.

    Attributes:
        name: The column name.
        type: The Iceberg type, or ``None`` for a table that does not exist yet.
        description: What the column holds, or ``None``.
        required: Whether the column may not be null, or ``None`` for a table that does not exist yet.
    """

    name: str
    type: str | None = None
    description: str | None = None
    required: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """The column as JSON-serialisable primitives."""
        return {"name": self.name, "type": self.type, "description": self.description, "required": self.required}


@dataclass(frozen=True)
class ReleaseEntry:
    """A release that holds a table.

    Attributes:
        release: ``r<number>``.
        cut_at: When it was cut, in UTC.
        snapshot_id: The snapshot of the table that the release names.
        rows: The rows that snapshot has.
        status: ``built`` or ``checked``, as the release says.
        current: Whether it is the release that readers see now.
        at_live: Whether the table as it is now is at the snapshot that the release names.
    """

    release: str
    cut_at: dt.datetime
    snapshot_id: int
    rows: int
    status: str
    current: bool
    at_live: bool

    def to_dict(self) -> dict[str, Any]:
        """The release as JSON-serialisable primitives."""
        return {"release": self.release, "cut_at": self.cut_at.astimezone(dt.timezone.utc).isoformat(),
                "snapshot_id": self.snapshot_id, "rows": self.rows, "status": self.status, "current": self.current,
                "at_live": self.at_live}


@dataclass(frozen=True)
class CatalogEntry:
    """One table of the catalog.

    Attributes:
        table: ``<namespace>.<name>``.
        namespace: The namespace.
        name: The table name.
        layer: ``bronze``, ``silver`` or ``gold``, or ``None``. See :func:`local_data_platform.contracts.resolve_layer`.
        description: What the table means, or ``None``.
        owner: Who owns it, or ``None``.
        grain: What one row is, or ``None``.
        primary_key: The columns that identify a row.
        tags: Free labels, from the config.
        refresh: How often it is rebuilt, from the config.
        config: The file name of the config that writes the table.
        exists: Whether the table exists yet.
        columns: Its columns, in order.
        snapshot_id: The current snapshot of the table.
        rows: The rows of the current snapshot.
        refreshed_at: When the data last changed, in UTC. It ignores the snapshots that maintenance makes.
        run: The latest run, as :func:`~local_data_platform.gold.status.latest_run` summarises it, or ``None``.
        quality: The latest quality verdict, or ``None``.
        release: The release that readers see now, when it holds the table.
        release_state: ``current`` when the table is at the snapshot of that release, and ``behind`` when it was built
            again after it, so that readers of the release do not see the newer data.
        problems: What a gold table lacks of its contract. Empty for a table of another layer.
        notes: Other things worth knowing, such as a table that does not exist yet.
        metrics: The metrics defined on the table. There are none until 0.1.4 item 5 defines them.
        detailed: Whether ``runs``, ``releases``, ``sample`` and ``query`` were collected.
        runs: The last runs, newest first.
        releases: Every release that holds the table, newest first.
        sample: A few rows, as ``{"columns": [...], "rows": [[...], ...]}``, or ``None``.
        query: The ``ldp query`` line that reads the table.
    """

    table: str
    namespace: str
    name: str
    layer: str | None
    description: str | None
    owner: str | None
    grain: str | None
    primary_key: tuple[str, ...]
    tags: tuple[str, ...]
    refresh: str | None
    config: str | None
    exists: bool
    columns: tuple[ColumnEntry, ...]
    snapshot_id: int | None = None
    rows: int | None = None
    refreshed_at: dt.datetime | None = None
    run: Mapping[str, Any] | None = None
    quality: Mapping[str, Any] | None = None
    release: str | None = None
    release_state: str | None = None
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    metrics: tuple[Mapping[str, Any], ...] = ()
    detailed: bool = False
    runs: tuple[Mapping[str, Any], ...] = ()
    releases: tuple[ReleaseEntry, ...] = ()
    sample: Mapping[str, Any] | None = None
    query: str = ""

    @property
    def gold(self) -> bool:
        """Whether the table is in the gold layer, which holds it to its contract."""
        return self.layer == GOLD

    @property
    def shown_description(self) -> str:
        """The description, or :data:`NO_DESCRIPTION`."""
        return self.description or NO_DESCRIPTION

    @property
    def run_text(self) -> str | None:
        """The status of the latest run, such as ``published``, or ``None`` when there is none."""
        return str(self.run["status"]) if self.run else None

    @property
    def quality_text(self) -> str | None:
        """The latest quality verdict in a few words, such as ``4 of 4 passed``, ``1 of 4 failed`` or ``no checks``."""
        quality = self.quality
        if not quality:
            return None
        if "checks_run" not in quality:
            return str(quality.get("status")) if quality.get("status") is not None else None
        run, failed = int_or_none(quality.get("checks_run")) or 0, int_or_none(quality.get("checks_failed")) or 0
        if run == 0:
            return "no checks"
        return f"{failed} of {run} failed" if failed else f"{run} of {run} passed"

    @property
    def lacks_text(self) -> str:
        """What the table lacks as a sentence, such as ``gold.extra has no description and no owner``, or ``""``."""
        return f"{self.table} has {_and(self.problems)}" if self.problems else ""

    @property
    def release_text(self) -> str | None:
        """The release that holds the table, and ``r3 (behind live)`` when the table was built again after it."""
        if self.release is None:
            return None
        return f"{self.release} (behind live)" if self.release_state == "behind" else self.release

    def document(self) -> str:
        """The text of the entry as one document, for search: the names, the meaning and the columns, and no numbers."""
        lines = [self.table, self.layer or "", self.description or "", f"owner {self.owner}" if self.owner else "",
                 f"grain {self.grain}" if self.grain else "",
                 f"key {', '.join(self.primary_key)}" if self.primary_key else "",
                 f"tags {', '.join(self.tags)}" if self.tags else "",
                 *(f"{column.name} {column.description or ''}".strip() for column in self.columns)]
        return "\n".join(line for line in lines if line)

    def to_dict(self) -> dict[str, Any]:
        """The entry as JSON-serialisable primitives. A table with no description has ``null``, not a placeholder."""
        data: dict[str, Any] = {
            "table": self.table, "namespace": self.namespace, "name": self.name, "layer": self.layer,
            "description": self.description, "owner": self.owner, "grain": self.grain,
            "primary_key": list(self.primary_key), "tags": list(self.tags), "refresh": self.refresh,
            "config": self.config, "exists": self.exists, "snapshot_id": self.snapshot_id, "rows": self.rows,
            "refreshed_at": self.refreshed_at, "run": self.run, "quality": self.quality, "release": self.release,
            "release_state": self.release_state, "problems": list(self.problems), "notes": list(self.notes),
            "columns": [column.to_dict() for column in self.columns], "metrics": list(self.metrics)}
        if self.detailed:
            data.update({"runs": list(self.runs), "releases": [item.to_dict() for item in self.releases],
                         "sample": self.sample, "query": self.query})
        return to_jsonable(data)


@dataclass(frozen=True)
class Catalog:
    """The entries of a folder.

    Attributes:
        folder: The folder as it was given, which the ``ldp query`` lines name.
        entries: The tables, gold first and then by name.
        generated_at: When it was made, in UTC.
        all_layers: Whether the entries are of every layer, and not only of the gold layer.
    """

    folder: str
    entries: tuple[CatalogEntry, ...]
    generated_at: dt.datetime
    all_layers: bool = False

    @property
    def lacking(self) -> list[CatalogEntry]:
        """The gold tables that lack some of their contract, which is what ``--strict`` fails on."""
        return [entry for entry in self.entries if entry.gold and entry.problems]

    @property
    def lacking_summary(self) -> str:
        """``1 gold table lacks part of its contract`` or ``2 gold tables lack part of their contract``."""
        number = len(self.lacking)
        return (f"{number} gold table lacks part of its contract" if number == 1
                else f"{number} gold tables lack part of their contract")

    def find(self, table: str) -> CatalogEntry:
        """The entry of ``table``, by its identifier such as ``gold.daily_sales`` or by its bare name.

        Raises:
            ConfigError: If there is no such table, or the bare name belongs to several. The message names the tables.
        """
        identifier = resolve_table([entry.table for entry in self.entries], table, self.folder)
        return next(entry for entry in self.entries if entry.table == identifier)

    def to_dict(self) -> dict[str, Any]:
        """The catalog as JSON-serialisable primitives."""
        layers: dict[str, int] = {}
        for entry in self.entries:
            layers[entry.layer or "none"] = layers.get(entry.layer or "none", 0) + 1
        return to_jsonable({"folder": self.folder, "generated_at": self.generated_at, "all_layers": self.all_layers,
                            "counts": {"tables": len(self.entries), "layers": layers, "lacking": len(self.lacking)},
                            "tables": [entry.to_dict() for entry in self.entries]})


# --------------------------------------------------------------------------- finding the tables


def resolve_table(identifiers: Iterable[str], text: str, folder: str = "") -> str:
    """The identifier that ``text`` names: the identifier itself, or a bare table name that only one table has.

    Raises:
        ConfigError: If it names none, or a bare name that several tables have. The message lists the tables.
    """
    known = list(identifiers)
    if text in known:
        return text
    bare = [item for item in known if item.rpartition(".")[2] == text]
    if len(bare) == 1:
        return bare[0]
    where = f" in {folder}" if folder else ""
    if bare:
        raise ConfigError(f"{text!r} is the name of {len(bare)} tables{where}, {', '.join(bare)}. "
                          "Give the namespace too")
    shown = ", ".join(known[:_LIST_AT_MOST]) + (", ..." if len(known) > _LIST_AT_MOST else "")
    raise ConfigError(f"there is no table {text!r}{where}. " + (f"It has {shown}" if known else "It has no table"))


def _and(items: Iterable[str]) -> str:
    """``a``, ``a and b``, or ``a, b and c``."""
    names = list(items)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _targets(configs: Iterable[tuple[Path, Any]]) -> dict[str, tuple[Path, Any]]:
    """The config of each Iceberg table that the configs write. When two write one, the one that has a contract wins."""
    chosen: dict[str, tuple[Path, Any]] = {}
    for path, config in configs:
        target = config.metadata.get("target") or {}
        if str(target.get("format", "")).strip().upper() != "ICEBERG" or not target.get("name"):
            continue
        if not isinstance(target.get("catalog"), Mapping):
            continue
        identifier = GoldConfig(path, config).table
        if identifier not in chosen or (chosen[identifier][1].docs is None and config.docs is not None):
            chosen[identifier] = (path, config)
    return chosen


# --------------------------------------------------------------------------- reading one table


def _moment(timestamp_ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp_ms / 1000, tz=dt.timezone.utc)


def _refreshed_at(table: Any) -> dt.datetime | None:
    """When the data of a table last changed: the newest snapshot of ``main`` that is not a maintenance rewrite."""
    first = snapshot = table.current_snapshot()
    seen: set[int] = set()
    while snapshot is not None and snapshot.snapshot_id not in seen:
        seen.add(snapshot.snapshot_id)
        if snapshot_operation(snapshot) != "replace":
            return _moment(snapshot.timestamp_ms)
        parent = snapshot.parent_snapshot_id
        snapshot = table.snapshot_by_id(parent) if parent is not None else None
    return _moment(first.timestamp_ms) if first is not None else None


def _rows(table: Any) -> int | None:
    snapshot = table.current_snapshot()
    if snapshot is None:
        return 0
    return int_or_none(snapshot_summary(snapshot).get("total-records"))


def _merge(declared: Docs | None, carried: Docs | None) -> Docs:
    """The contract of the config, with what the table carries for whatever the config leaves out."""
    first, second = declared or Docs(), carried or Docs()
    return Docs(layer=first.layer or second.layer, description=first.description or second.description,
                owner=first.owner or second.owner, grain=first.grain or second.grain,
                primary_key=first.primary_key or second.primary_key, columns={**second.columns, **first.columns},
                tags=first.tags, refresh=first.refresh)


def _problems(layer: str | None, docs: Docs, columns: Iterable[str]) -> tuple[str, ...]:
    """What a gold table lacks of its contract. A table of another layer lacks nothing, whatever it says."""
    if layer != GOLD:
        return ()
    found = [f"no {label}" for label, value in (("description", docs.description), ("owner", docs.owner),
                                                ("grain", docs.grain)) if not value]
    bare = [name for name in columns if name not in docs.columns]
    if bare:
        shown = ", ".join(bare[:_LIST_AT_MOST]) + (", ..." if len(bare) > _LIST_AT_MOST else "")
        found.append(f"{len(bare)} column{'' if len(bare) == 1 else 's'} without a description ({shown})")
    return tuple(found)


def _sample(table: Any, limit: int) -> dict[str, Any] | None:
    """The first rows of a table in scan order, as plain values, or ``None`` when they cannot be read."""
    try:
        data = table.scan(limit=limit).to_arrow()
    except Exception as error:  # noqa: BLE001 - a catalog page is worth having without its sample
        logger.warning("Could not read sample rows of %s: %s", ".".join(table.name()), error)
        return None
    names = list(data.column_names)
    rows = data.to_pylist()[:limit]
    return {"columns": names, "rows": [[to_jsonable(row[name]) for name in names] for row in rows]}


def _sql_name(identifier: str) -> str:
    return ".".join(part if _PLAIN_NAME.fullmatch(part) else '"' + part.replace('"', '""') + '"'
                    for part in identifier.split("."))


def _shell(text: str) -> str:
    """``text`` as one argument: in double quotes when that is safe, else as ``shlex`` quotes it."""
    return f'"{text}"' if not any(char in text for char in '"$`\\!') else shlex.quote(text)


def query_line(folder: str, identifier: str, limit: int = 10) -> str:
    """The ``ldp query`` line that reads a table of the folder, ready to copy."""
    return f"ldp query {shlex.quote(folder)} {_shell(f'SELECT * FROM {_sql_name(identifier)} LIMIT {limit}')}"


def _close(catalogs: Any) -> None:
    for handle in catalogs.handles.values():
        close = getattr(handle.catalog, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:  # noqa: BLE001 - a catalog that will not close must not fail a listing
                logger.debug("Could not close catalog %s: %s", handle.key, error)


def _releases_of(scope: releases.ReleaseScope, cache: dict[Any, list[releases.Release]]) -> list[releases.Release]:
    if scope not in cache:
        cache[scope] = releases.releases_in(scope)
    return cache[scope]


def _entry(identifier: str, path: Path, config: Any, *, folder: str, catalogs: Any, cache: dict[Any, Any],
           all_layers: bool, detail: bool, runs: int, sample: int) -> CatalogEntry | None:
    """The entry of one table, or ``None`` when it is not in a layer that was asked for."""
    from local_data_platform.mcp_server.discovery import Unavailable

    namespace_text, _, name = identifier.rpartition(".")
    namespace = tuple(part for part in namespace_text.split(".") if part)
    declared = config.docs
    iceberg: Any = None
    handle: Any = None
    notes: list[str] = []
    try:
        handle = catalogs.get(config.metadata["target"]["catalog"], config.base_dir)
        if handle.catalog.table_exists((*namespace, name)):
            iceberg = handle.catalog.load_table((*namespace, name))
    except Unavailable:
        pass
    except Exception as error:  # noqa: BLE001 - a catalog that cannot be reached is a note and not a failure
        logger.warning("Could not read %s: %s", identifier, error)
        notes.append(f"could not read the table, {type(error).__name__}: {error}")
    layer = resolve_layer(declared.layer if declared else None, iceberg.properties if iceberg is not None else None,
                          namespace_text)
    if not all_layers and layer != GOLD:
        return None

    carried = read_contract(iceberg) if iceberg is not None else None
    docs = _merge(declared, carried)
    if iceberg is not None:
        columns = tuple(ColumnEntry(item.name, str(item.field_type), docs.columns.get(item.name), item.required)
                        for item in iceberg.schema().fields)
    else:
        columns = tuple(ColumnEntry(column, description=text) for column, text in docs.columns.items())

    snapshot = iceberg.current_snapshot() if iceberg is not None else None
    live = snapshot.snapshot_id if snapshot is not None else None
    run = quality = None
    recent: tuple[Any, ...] = ()
    if iceberg is not None:
        status = ldp_status(handle.catalog, iceberg, identifier, recent=runs if detail else 0)
        run, quality, recent = status.get("latest_run"), status.get("quality"), tuple(status.get("recent_runs") or ())
        if status.get("error"):
            notes.append(f"could not read the run history, {status['error']}")
    if iceberg is not None and declared is not None:
        properties, described = pending_changes(iceberg, declared)
        if properties or described:
            notes.append(f"{path.name} says more than the table carries, so readers of the table do not see it yet. "
                         f"ldp gold sync {shlex.quote(str(Path(folder) / path.name))} writes it")

    held: list[releases.Release] = []
    current = None
    try:
        held = _releases_of(releases.scope_of(GoldConfig(path, config)), cache)
        current = held[0] if held else None
    except releases.ReleaseError as error:
        logger.debug("No releases for %s: %s", identifier, error)
    in_current = current.table(identifier) if current is not None else None
    state = None
    if in_current is not None and live is not None:
        state = "current" if in_current.snapshot_id == live else "behind"
        if state == "behind":
            notes.append(f"{identifier} was built after {current.name}, so readers of the release do not see the newer "
                         f"data yet. ldp gold build {shlex.quote(folder)} cuts the next release")

    extra: dict[str, Any] = {}
    if detail:
        entries = []
        for release in held:
            item = release.table(identifier)
            if item is not None:
                entries.append(ReleaseEntry(release.name, release.created_at, item.snapshot_id, item.rows, item.status,
                                            release is current, item.snapshot_id == live))
        extra = {"detailed": True, "runs": recent, "releases": tuple(entries),
                 "sample": _sample(iceberg, sample) if iceberg is not None and sample > 0 else None,
                 "query": query_line(folder, identifier)}
    return CatalogEntry(
        table=identifier, namespace=namespace_text, name=name, layer=layer, description=docs.description,
        owner=docs.owner, grain=docs.grain, primary_key=docs.primary_key, tags=docs.tags, refresh=docs.refresh,
        config=path.name, exists=iceberg is not None, columns=columns, snapshot_id=live,
        rows=_rows(iceberg) if iceberg is not None else None,
        refreshed_at=_refreshed_at(iceberg) if iceberg is not None else None, run=run, quality=quality,
        release=current.name if in_current is not None else None, release_state=state,
        problems=_problems(layer, docs, [column.name for column in columns]), notes=tuple(notes), **extra)


def _order(entry: CatalogEntry) -> tuple[int, str]:
    return _LAYER_ORDER.get(entry.layer or "", len(_LAYER_ORDER)), entry.table


def collect(folder: str | os.PathLike, *, all_layers: bool = False, only: str | None = None, detail: bool = False,
            runs: int = DEFAULT_RUNS, sample: int = DEFAULT_SAMPLE, now: dt.datetime | None = None) -> Catalog:
    """The catalog of a folder of dataset configs.

    Args:
        folder: A folder of ``*.json`` dataset configs.
        all_layers: Include the bronze and silver tables, and the tables with no layer. By default only gold tables.
        only: One table, by identifier or by bare name. The other tables are not read, and every layer is searched.
        detail: Also collect the last ``runs`` runs, the releases that hold each table, ``sample`` rows and the query.
        runs: How many runs to list when ``detail`` is set.
        sample: How many sample rows to read when ``detail`` is set. None when it is 0.
        now: The time to stamp the catalog with. The current time by default.

    Returns:
        The :class:`Catalog`. It does not raise for a table it cannot read, and says so in the entry.

    Raises:
        ConfigError: If ``folder`` is not a folder, a config is not valid, the folder writes no Iceberg table, or
            ``only`` names no table or an ambiguous one.
    """
    from local_data_platform.mcp_server.discovery import Catalogs

    text = str(folder)
    chosen = _targets(find_configs(folder))
    if not chosen:
        raise ConfigError(f"{resolve_path(folder)} has no dataset config that writes an Iceberg table")
    if only is not None:
        identifier = resolve_table(chosen, only, text)
        chosen = {identifier: chosen[identifier]}
        all_layers = True
    catalogs = Catalogs()
    cache: dict[Any, Any] = {}
    entries = []
    try:
        for identifier, (path, config) in chosen.items():
            entry = _entry(identifier, path, config, folder=text, catalogs=catalogs, cache=cache,
                           all_layers=all_layers, detail=detail, runs=runs, sample=sample)
            if entry is not None:
                entries.append(entry)
    finally:
        _close(catalogs)
    entries.sort(key=_order)
    return Catalog(text, tuple(entries), now or dt.datetime.now(dt.timezone.utc), all_layers)


__all__ = ["Catalog", "CatalogEntry", "ColumnEntry", "DEFAULT_RUNS", "DEFAULT_SAMPLE", "NO_DESCRIPTION",
           "ReleaseEntry", "collect", "query_line", "resolve_table"]
