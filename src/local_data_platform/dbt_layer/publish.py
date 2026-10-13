"""Publish the dbt models marked ``ldp_publish`` to Iceberg.

After a successful ``dbt build`` this reads ``target/manifest.json`` (what each model is) and
``target/run_results.json`` (what ran), decides which models to publish and how, writes an ordinary
dataset config for each one, and runs it. The contract is ``docs/design/warehousing.md``.

Nothing here imports dbt. Planning is pure: it reads two dicts, so it is tested without dbt.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform.catalog.provider import catalog_namespace, catalog_type
from local_data_platform.dbt_layer.project import DbtProject
from local_data_platform.exceptions import ConfigError

WRITE_MODES = ("append", "overwrite", "upsert")
META_KEYS = ("ldp_publish", "ldp_write_mode", "ldp_join_cols", "ldp_namespace", "ldp_partition_by", "ldp_quality")
PUBLISHABLE = ("table", "view", "incremental")
FAILED_STATUSES = ("error", "fail", "runtime error")


@dataclass(frozen=True)
class PublishItem:
    """One model to publish.

    Attributes:
        unique_id: The dbt node id, such as ``model.shop.fct_orders``.
        model: The model's name.
        table: The Iceberg table, ``<namespace>.<alias>``.
        relation: The DuckDB relation the model built, ``<schema>.<alias>``.
        mode: The write mode: ``append``, ``overwrite`` or ``upsert``.
        join_cols: The key columns of an ``upsert``.
        config: The generated dataset config, with paths relative to ``path``'s folder.
        path: Where the config is written.
    """

    unique_id: str
    model: str
    table: str
    relation: str
    mode: str
    join_cols: tuple[str, ...]
    config: dict[str, Any]
    path: Path


@dataclass(frozen=True)
class Published:
    """A model that was written to Iceberg: what ``ldp dbt build`` prints one line for."""

    model: str
    table: str
    mode: str
    rows_before: int
    rows_after: int
    snapshot_id: int | None
    config: Path
    quality: tuple[str, ...] = ()


@dataclass
class PublishOutcome:
    """What :func:`publish` did.

    Attributes:
        published: The models written, in order.
        failed: ``(model, error)`` of the write that failed, or ``None``.
        failed_config: The config of the model that failed, which ``ldp run`` can run by hand.
        pending: The models after the failed one, which were not tried.
    """

    published: list[Published]
    failed: tuple[str, BaseException] | None = None
    failed_config: Path | None = None
    pending: list[str] | None = None


# --------------------------------------------------------------------------- dbt's artifacts


def read_artifacts(project: DbtProject, target_path: str | os.PathLike = "target", *,
                   since: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read ``manifest.json`` and ``run_results.json`` from dbt's target folder.

    Args:
        project: The project; a relative ``target_path`` is relative to its folder.
        target_path: dbt's target folder (``--target-path``).
        since: When given, a file last written before this time (seconds since the epoch) is
            stale, which means this dbt run did not write it.

    Raises:
        ConfigError: If a file is missing or stale.
    """
    folder = Path(target_path)
    folder = folder if folder.is_absolute() else project.directory / folder
    loaded = []
    for name in ("manifest.json", "run_results.json"):
        file = folder / name
        if not file.is_file():
            raise ConfigError(f"dbt did not write {file}, so there is nothing to publish. Was the command a build?")
        if since is not None and file.stat().st_mtime < since:
            raise ConfigError(f"{file} was not written by this dbt run. Do not pass --no-write-json, and use a "
                              f"command that builds models.")
        loaded.append(json.loads(file.read_text()))
    return loaded[0], loaded[1]


def failed_nodes(run_results: Mapping[str, Any]) -> list[dict[str, str]]:
    """The nodes of a dbt run that failed, each ``{"node", "status", "message"}`` (the message's first line)."""
    failed = []
    for result in run_results.get("results", []):
        if result.get("status") in FAILED_STATUSES:
            message = str(result.get("message") or "").strip().splitlines()
            failed.append({"node": str(result.get("unique_id")), "status": str(result["status"]),
                           "message": message[0] if message else ""})
    return failed


# --------------------------------------------------------------------------- planning


def plan_publish(project: DbtProject, manifest: Mapping[str, Any], run_results: Mapping[str, Any]) -> list[PublishItem]:
    """Decide which models to publish and build the config of each.

    A model is published when its meta has ``ldp_publish: true`` and it ran in this invocation with
    status ``success``. The result is in dependency order.

    Args:
        project: The project, for the build file, the publish catalog and the config folder.
        manifest: The parsed ``manifest.json``.
        run_results: The parsed ``run_results.json``.

    Returns:
        The items, upstream models first. Empty when no executed model is marked.

    Raises:
        ConfigError: If any model's meta is wrong. Every problem is reported at once, each naming
            its model, before anything is written.
    """
    from local_data_platform.spec import validate_spec

    nodes = manifest.get("nodes", {})
    statuses = {result["unique_id"]: result.get("status") for result in run_results.get("results", [])}
    project_name = str(manifest.get("metadata", {}).get("project_name") or "")
    problems: list[str] = []
    items: list[PublishItem] = []
    for unique_id in _model_order(nodes):
        node = nodes[unique_id]
        if unique_id not in statuses:
            continue  # --select left it out
        meta = _meta(node)
        name = str(node["name"])
        unknown = sorted(key for key in meta if str(key).startswith("ldp_") and key not in META_KEYS)
        if unknown:
            problems.append(f"model {name}: unknown meta keys {unknown}; known: {list(META_KEYS)}")
        flag = meta.get("ldp_publish")
        if flag is None or flag is False:
            continue
        if flag is not True:
            problems.append(f"model {name}: meta ldp_publish must be true or false, got {flag!r}")
            continue
        if statuses[unique_id] != "success":
            continue
        found = len(problems)
        item = _plan_one(project, node, meta, project_name, problems)
        if item is not None:
            problems += [f"model {name}: {error}" for error in validate_spec(item.config)]
        if item is not None and len(problems) == found:
            items.append(item)
    problems += _collisions(items)
    if items and (run_results.get("args") or {}).get("empty") is True:
        problems.append("dbt ran with --empty, which builds every model with no rows, and publishing those would "
                        "overwrite Iceberg tables with nothing; build without --empty")
    if problems:
        lines = "\n".join(f"  - {problem}" for problem in problems)
        raise ConfigError(f"cannot publish, {len(problems)} problem{'s' if len(problems) != 1 else ''} "
                          f"in the dbt models' meta, and nothing was published:\n{lines}")
    return items


def _collisions(items: list[PublishItem]) -> list[str]:
    """One problem for each Iceberg table, and each config file, that two models would share."""
    problems = []
    for what, key_of in (("table", lambda item: item.table), ("config file", lambda item: str(item.path))):
        owners: dict[str, list[str]] = {}
        for item in items:
            owners.setdefault(key_of(item), []).append(item.model)
        problems += [f"models {', '.join(models)} would all write the {what} {key}; give them different aliases "
                     f"or meta ldp_namespace" for key, models in owners.items() if len(models) > 1]
    return problems


def _meta(node: Mapping[str, Any]) -> dict[str, Any]:
    meta = dict(node.get("meta") or {})
    meta.update((node.get("config") or {}).get("meta") or {})
    return meta


def _model_order(nodes: Mapping[str, Any]) -> list[str]:
    """Every model, each after the models it depends on, otherwise in the manifest's order."""
    def parents(unique_id: str) -> list[str]:
        return [parent for parent in (nodes[unique_id].get("depends_on") or {}).get("nodes", [])
                if parent in nodes and nodes[parent].get("resource_type") == "model"]

    order: list[str] = []
    seen: set[str] = set()
    for root in (unique_id for unique_id, node in nodes.items() if node.get("resource_type") == "model"):
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(parents(root)))]
        while stack:
            current, remaining = stack[-1]
            for parent in remaining:
                if parent not in seen:
                    seen.add(parent)
                    stack.append((parent, iter(parents(parent))))
                    break
            else:
                order.append(current)
                stack.pop()
    return order


def _plan_one(project: DbtProject, node: Mapping[str, Any], meta: Mapping[str, Any], project_name: str,
              problems: list[str]) -> PublishItem | None:
    name = str(node["name"])
    config = node.get("config") or {}
    materialized = config.get("materialized")
    if materialized not in PUBLISHABLE:
        problems.append(f"model {name}: a {materialized!r} model has no relation to publish; "
                        f"use one of {list(PUBLISHABLE)}")
        return None
    unique_key = _columns(config.get("unique_key"))
    override = meta.get("ldp_write_mode")
    if override is not None:
        mode = str(override).strip().lower()
        if mode not in WRITE_MODES:
            problems.append(f"model {name}: meta ldp_write_mode must be one of {list(WRITE_MODES)}, got {override!r}")
            return None
    elif materialized == "incremental":
        if not unique_key:
            problems.append(f"model {name}: an incremental model without a unique_key would append the whole "
                            f"relation on every build; set meta ldp_write_mode (overwrite publishes the whole table)")
            return None
        mode = "upsert"
    else:
        mode = "overwrite"
    join_cols = _columns(meta.get("ldp_join_cols")) or unique_key
    if mode == "upsert" and not join_cols:
        problems.append(f"model {name}: an upsert needs key columns; set unique_key, or meta ldp_join_cols")
        return None
    namespace = meta.get("ldp_namespace")
    if namespace is not None and not (isinstance(namespace, str) and namespace.strip()):
        problems.append(f"model {name}: meta ldp_namespace must be a non-empty string, got {namespace!r}")
        return None

    alias = str(node.get("alias") or name)
    relation = f"{node['schema']}.{alias}"
    catalog = _catalog_for(project, namespace.strip() if namespace else None)
    target: dict[str, Any] = {"name": alias, "format": "ICEBERG", "write_mode": mode, "catalog": catalog}
    if mode == "upsert":
        target["join_cols"] = list(join_cols)
    if meta.get("ldp_partition_by") is not None:
        target["partition_by"] = meta["ldp_partition_by"]
    metadata: dict[str, Any] = {
        "source": {"name": name, "format": "DUCKDB", "path": _relative(project.database, project.config_dir),
                   "relation": relation},
        "target": target,
    }
    if meta.get("ldp_quality") is not None:
        metadata["quality"] = meta["ldp_quality"]
    if project.observability:
        metadata["observability"] = project.observability
    dataset: dict[str, Any] = {"identifier": name}
    if node.get("description"):
        dataset["what"] = str(node["description"])
    dataset.update({"where": f"dbt project {project_name}" if project_name else "a dbt project",
                    "how": "ldp dbt build", "metadata": metadata})

    return PublishItem(unique_id=str(node["unique_id"]), model=name, table=f"{catalog_namespace(catalog)}.{alias}",
                       relation=relation, mode=mode, join_cols=tuple(join_cols), config=dataset,
                       path=project.config_dir / f"{name}.json")


def _columns(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _catalog_for(project: DbtProject, namespace: str | None) -> dict[str, Any]:
    """The publish catalog as the generated config writes it: paths relative to the config folder, in ``namespace``."""
    catalog = _rebase_catalog(project.publish_catalog, project.directory, project.config_dir)
    if namespace:
        if catalog_type(catalog) == "local":
            catalog["identifier"] = namespace  # for a local catalog the identifier is the namespace
            catalog.pop("namespace", None)
        else:
            catalog["namespace"] = namespace
    return catalog


def _rebase_catalog(catalog: Mapping[str, Any], start: Path, end: Path) -> dict[str, Any]:
    """Rewrite the relative paths of a catalog block, written for ``start``, to be relative to ``end``."""
    def rebase(value: Any) -> Any:
        if not isinstance(value, str) or "://" in value or os.path.isabs(value):
            return value
        return _relative((start / value).resolve(), end)

    rebased = dict(catalog)
    if "warehouse_path" in rebased:
        rebased["warehouse_path"] = rebase(rebased["warehouse_path"])
    if isinstance(rebased.get("warehouse"), str):
        rebased["warehouse"] = rebase(rebased["warehouse"])
    uri = rebased.get("uri")
    if isinstance(uri, str) and uri.startswith("sqlite:///") and not uri.startswith("sqlite:////"):
        rebased["uri"] = "sqlite:///" + rebase(uri[len("sqlite:///"):])
    return rebased


def _relative(path: Path, start: Path) -> str:
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:  # pragma: no cover - only on Windows, across drives
        return path.as_posix()


# --------------------------------------------------------------------------- publishing


def publish(plan: list[PublishItem], report: Callable[[Published], None] | None = None) -> PublishOutcome:
    """Write every config in ``plan`` to its folder, then run them in order.

    The configs are all written first, so they exist, and can be run with ``ldp run``, even when
    a write fails. The first failure, such as a blocked quality check, stops the run: the models
    before it stay published, and the rest are reported as pending.

    Args:
        plan: What :func:`plan_publish` returned.
        report: Called with each model right after it was written, so a long run shows progress.
    """
    from local_data_platform.etl import run_config

    for item in plan:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(json.dumps(item.config, indent=2) + "\n")
    published: list[Published] = []
    for position, item in enumerate(plan):
        try:
            result = run_config(item.path)
        except Exception as error:  # noqa: BLE001 - reported with the models that were and were not published
            return PublishOutcome(published, (item.model, error), item.path,
                                  [later.model for later in plan[position + 1:]])
        write = result.write_result
        quality = tuple(result.quality.summary().splitlines()) if len(result.quality) else ()
        published.append(Published(model=item.model, table=write.table_identifier, mode=write.mode,
                                   rows_before=write.rows_before, rows_after=write.rows_after,
                                   snapshot_id=write.snapshot_id, config=item.path, quality=quality))
        if report is not None:
            report(published[-1])
    return PublishOutcome(published)


__all__ = ["FAILED_STATUSES", "META_KEYS", "PublishItem", "PublishOutcome", "Published", "failed_nodes",
           "plan_publish", "publish", "read_artifacts"]
