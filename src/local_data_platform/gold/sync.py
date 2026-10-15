"""``ldp gold sync``: write the contract of a config into its existing table, without writing data.

A pipeline writes its contract when it publishes, so most tables never need this. It is for a table that exists and
does not carry its contract yet: one that was loaded before the config had a ``metadata.docs`` block, one whose docs
changed and which has no new data to publish, or one that another tool built, such as dbt or Spark.

For a table built outside LDP, the contract can come from the ``manifest.json`` of a dbt project. The file is read as
JSON, and dbt is not imported.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform import contracts
from local_data_platform.exceptions import ConfigError


@dataclass(frozen=True)
class SyncResult:
    """What :func:`sync_config` did.

    Attributes:
        config: The config's identifier.
        table: The Iceberg table, ``<namespace>.<name>``.
        docs: The contract that was written, or that would be.
        result: What :func:`~local_data_platform.contracts.apply_contract` reported.
        dry_run: Whether nothing was written because it was only asked what would change.
    """

    config: str
    table: str
    docs: contracts.Docs
    result: contracts.ContractResult
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"config": self.config, "table": self.table, "layer": self.docs.layer, "dry_run": self.dry_run,
                **self.result.to_dict()}


def find_model(manifest: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    """The model of a dbt manifest that builds the table called ``name``.

    The table is named by the ``alias`` of the model, which is its name when it has no alias.

    Raises:
        ConfigError: If no model, or more than one model, builds a table with that name.
    """
    found = [node for node in (manifest.get("nodes") or {}).values()
             if node.get("resource_type") == "model" and (node.get("alias") or node.get("name")) == name]
    if not found:
        raise ConfigError(f"the dbt manifest has no model that builds a table called {name!r}")
    if len(found) > 1:
        ids = ", ".join(sorted(str(node.get("unique_id")) for node in found))
        raise ConfigError(f"the dbt manifest has {len(found)} models that build a table called {name!r}: {ids}")
    return found[0]


def _merged(from_manifest: Mapping[str, Any], from_config: Mapping[str, Any]) -> dict[str, Any]:
    """The block of a manifest with the block of a config on top: what the config says wins, key by key."""
    merged = {**from_manifest, **{key: value for key, value in from_config.items() if key not in ("columns", "tags")}}
    columns = {**(from_manifest.get("columns") or {}), **(from_config.get("columns") or {})}
    if columns:
        merged["columns"] = columns
    tags = list(dict.fromkeys([*(from_manifest.get("tags") or ()), *(from_config.get("tags") or ())]))
    if tags:
        merged["tags"] = tags
    return merged


def sync_config(path: str | os.PathLike, *, dbt_manifest: str | os.PathLike | None = None, dry_run: bool = False,
                policy: Any = None) -> SyncResult:
    """Write the contract of a config into the Iceberg table that its target names.

    The contract is the config's ``metadata.docs``. With ``dbt_manifest``, what the manifest says about the model that
    builds the table is the base, and the config's block is written over it. The table has to exist, and nothing is
    written to its data. When the table already has the contract, nothing is committed at all.

    Args:
        path: The config file.
        dbt_manifest: A dbt ``manifest.json``, for a table that dbt built.
        dry_run: Say what would change, and change nothing.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` that bounds the tries.

    Returns:
        The :class:`SyncResult`.

    Raises:
        ConfigError: If the config is not valid, its target is not an Iceberg table, there is no contract to write, or
            the manifest cannot be read or has no model for the table.
        TableNotFound: If the table does not exist.
        ContractError: If the table is gold and its contract does not hold for the columns the table has now.
        CommitConflict: If other writers kept changing the table, and the policy ran out of tries.
    """
    from local_data_platform.etl import load_config
    from local_data_platform.pipeline.builders import iceberg_from_config

    config = load_config(path)
    target = config.metadata["target"]
    if str(target.get("format", "")).strip().upper() != "ICEBERG":
        raise ConfigError(f"config {config.identifier} writes to a {target.get('format')} target, and only an "
                          "Iceberg table can carry a contract")
    iceberg = iceberg_from_config(config, "target", must_exist=True)
    table = iceberg.table()

    block: dict[str, Any] = dict(config.metadata.get("docs") or {})
    if dbt_manifest is not None:
        manifest_path = Path(dbt_manifest)
        try:
            manifest = json.loads(manifest_path.read_text())
        except OSError as error:
            raise ConfigError(f"cannot read the dbt manifest {manifest_path}: {error}") from None
        except json.JSONDecodeError as error:
            raise ConfigError(f"the dbt manifest {manifest_path} is not valid JSON: {error}") from None
        node = find_model(manifest, str(target["name"]))
        block = _merged(contracts.docs_from_dbt_node(node, namespace=iceberg.namespace), block)
    if not block:
        raise ConfigError(f"config {config.identifier} has no metadata.docs, so there is no contract to write. "
                          "Add one, or give --dbt-manifest to read it from a dbt project")
    docs = contracts.parse_docs(block, who=config.who, when=config.when)
    problems = contracts.check_contract(docs, table.schema())
    if problems:
        raise contracts.contract_error(iceberg.identifier, problems)
    result = contracts.apply_contract(table, docs, policy=policy, dry_run=dry_run)
    return SyncResult(config=config.identifier, table=iceberg.identifier, docs=docs, result=result, dry_run=dry_run)


__all__ = ["SyncResult", "find_model", "sync_config"]
