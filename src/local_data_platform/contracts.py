"""Table contracts: what a table means, who owns it, what one row is and what each column holds.

A config writes the contract down in an optional ``metadata.docs`` block, next to the pipeline that builds the table::

    "docs": {
      "layer": "gold",
      "description": "Orders and revenue for each restaurant on each day it had an order.",
      "owner": "restaurant operations",
      "grain": "one row per restaurant per day",
      "primary_key": ["restaurant_id", "day"],
      "columns": {"restaurant_id": "The restaurant.", "day": "The day.", "orders": "How many orders."},
      "tags": ["finance"],
      "refresh": "daily"
    }

``owner`` is the config's ``who`` when the block does not name one, and ``refresh`` is its ``when``. After a run
publishes, :func:`apply_contract` writes the contract into the Iceberg table itself, where every client finds it
whoever built the table: the table properties ``comment``, ``ldp.owner``, ``ldp.layer``, ``ldp.grain`` and
``ldp.primary-key``, and the ``doc`` of each column. That is one metadata-only commit, so it adds no snapshot, and it is
not made at all when the table already carries what the block says. It only ever adds and changes text. A key that is
taken out of the block stays on the table.

A ``gold`` table has to keep its promise. Its block needs a ``description``, an ``owner`` (or a ``who``) and a
``grain``, and :func:`check_contract` stops a write that has a column without a description, or a ``columns`` entry
for a column that the table does not have. A table of another layer, or with no layer, may document as much or as
little as it wants.

This module imports nothing outside the standard library when it is imported, so the spec and the config can use it.
``apply_contract`` imports pyiceberg when it runs.
"""

from __future__ import annotations

import difflib
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from local_data_platform.exceptions import ConfigError, ContractError

LAYERS = ("bronze", "silver", "gold")
"""The layers a table can be in. Only ``gold`` is held to its contract."""

GOLD = "gold"

DOCS_KEYS = ("layer", "description", "owner", "grain", "primary_key", "columns", "tags", "refresh")
"""The keys of a ``metadata.docs`` block."""

COMMENT = "comment"
OWNER = "ldp.owner"
LAYER = "ldp.layer"
GRAIN = "ldp.grain"
PRIMARY_KEY = "ldp.primary-key"
PROPERTY_KEYS = (COMMENT, OWNER, LAYER, GRAIN, PRIMARY_KEY)
"""The table properties that the contract sets."""


# ---------------------------------------------------------------------- the block


def _text(value: Any) -> str | None:
    """The text of ``value`` without the space around it, or ``None`` when it is not text or has none."""
    return value.strip() or None if isinstance(value, str) else None


def _names(value: Any) -> tuple[str, ...]:
    """The names in a string or in a list of strings, as a tuple, and nothing else."""
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


@dataclass(frozen=True)
class Docs:
    """A normalised ``metadata.docs`` block, with the defaults of its config applied.

    Attributes:
        layer: ``bronze``, ``silver`` or ``gold``, or ``None``.
        description: What the table means.
        owner: Who owns it. The ``owner`` of the block, else the ``who`` of the config.
        grain: What one row is.
        primary_key: The columns that identify a row.
        columns: Column name to description.
        tags: Free labels.
        refresh: How often it is rebuilt. The ``refresh`` of the block, else the ``when`` of the config.
    """

    layer: str | None = None
    description: str | None = None
    owner: str | None = None
    grain: str | None = None
    primary_key: tuple[str, ...] = ()
    columns: Mapping[str, str] = field(default_factory=dict)
    tags: tuple[str, ...] = ()
    refresh: str | None = None

    @property
    def gold(self) -> bool:
        """Whether the table is in the ``gold`` layer, which holds it to its contract."""
        return self.layer == GOLD

    def properties(self) -> dict[str, str]:
        """The table properties that this contract sets: only the ones that it has a value for."""
        values = {COMMENT: self.description, OWNER: self.owner, LAYER: self.layer, GRAIN: self.grain,
                  PRIMARY_KEY: ",".join(self.primary_key) or None}
        return {key: value for key, value in values.items() if value}

    def to_dict(self) -> dict[str, Any]:
        """The contract as a block again, with the defaults in it and without the keys that it has no value for."""
        values = {"layer": self.layer, "description": self.description, "owner": self.owner, "grain": self.grain,
                  "primary_key": list(self.primary_key), "columns": dict(self.columns), "tags": list(self.tags),
                  "refresh": self.refresh}
        return {key: value for key, value in values.items() if value}


def docs_problems(block: Any, *, who: Any = "") -> list[tuple[str, str]]:
    """Every problem of a ``metadata.docs`` block, as ``(path, message)`` with the path relative to the block.

    Args:
        block: The value of ``metadata.docs``. ``None`` is no block, and has no problem.
        who: The ``who`` of the config, which is the owner when the block names none.

    Returns:
        The problems, which are empty when the block is fine. The path is ``""`` for the block, ``".layer"`` for a key
        and ``".columns.revenue"`` for a column.
    """
    if block is None:
        return []
    if not isinstance(block, Mapping):
        return [("", f"must be an object, got {type(block).__name__}")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(str(key) for key in block if key not in DOCS_KEYS)
    if unknown:
        problems.append(("", f"has unknown keys {unknown}; allowed: {list(DOCS_KEYS)}"))
    layer = block.get("layer")
    if layer is not None and layer not in LAYERS:
        problems.append((".layer", f"must be one of {list(LAYERS)}, got {layer!r}"))
    for key in ("description", "owner", "grain", "refresh"):
        value = block.get(key)
        if value is not None and _text(value) is None:
            problems.append((f".{key}", f"must be a non-empty string, got {value!r}"))
    primary = block.get("primary_key")
    if primary is not None:
        items = [primary] if isinstance(primary, str) else primary
        if not isinstance(items, (list, tuple)) or not items or any(_text(item) is None for item in items):
            problems.append((".primary_key", f"must be a column name or a non-empty list of column names, "
                                             f"got {primary!r}"))
    columns = block.get("columns")
    if columns is not None:
        if not isinstance(columns, Mapping):
            problems.append((".columns", f"must be an object of column name to description, got {columns!r}"))
        else:
            for name, text in columns.items():
                if _text(name) is None:
                    problems.append((".columns", f"has a column name that is not text, {name!r}"))
                elif _text(text) is None:
                    problems.append((f".columns.{name}", f"must be a non-empty description, got {text!r}"))
    tags = block.get("tags")
    if tags is not None and (not isinstance(tags, (list, tuple)) or any(_text(tag) is None for tag in tags)):
        problems.append((".tags", f"must be a list of non-empty strings, got {tags!r}"))
    if layer == GOLD:
        for name, what in (("description", "what the table means"), ("grain", "what one row is")):
            if block.get(name) is None:
                problems.append((f".{name}", f"is required for a gold table, to say {what}"))
        if block.get("owner") is None and _text(who) is None:
            problems.append((".owner", "is required for a gold table, to say who owns it. Set it here, or set "
                                       "'who' in the config"))
    return problems


def parse_docs(block: Any, *, who: Any = "", when: Any = "") -> Docs | None:
    """Normalise a ``metadata.docs`` block and apply the defaults of its config.

    Args:
        block: The value of ``metadata.docs``.
        who: The ``who`` of the config, the owner when the block names none.
        when: The ``when`` of the config, the refresh when the block names none.

    Returns:
        The :class:`Docs`, or ``None`` when there is no block.

    Raises:
        ConfigError: If the block is not valid. The message names the first problem, as ``metadata.docs.layer: ...``.
    """
    problems = docs_problems(block, who=who)
    if problems:
        path, message = problems[0]
        raise ConfigError(f"metadata.docs{path}: {message}")
    if block is None:
        return None
    columns = {str(name).strip(): text.strip() for name, text in (block.get("columns") or {}).items()}
    tags = tuple(dict.fromkeys(tag.strip() for tag in (block.get("tags") or ())))
    return Docs(layer=block.get("layer"), description=_text(block.get("description")),
                owner=_text(block.get("owner")) or _text(who), grain=_text(block.get("grain")),
                primary_key=_names(block.get("primary_key")), columns=columns, tags=tags,
                refresh=_text(block.get("refresh")) or _text(when))


# ---------------------------------------------------------------------- the check


def column_names(schema: Any) -> list[str]:
    """The top-level column names of a pyarrow schema, of an Iceberg schema, or of a list of names."""
    if isinstance(schema, str):
        raise TypeError("a schema is a pyarrow or Iceberg schema or a list of column names, not a string")
    names = getattr(schema, "names", None)
    if names is not None:  # pyarrow.Schema
        return list(names)
    fields = getattr(schema, "fields", None)
    if fields is not None:  # an Iceberg schema
        return [item.name for item in fields]
    return [str(name) for name in schema]


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _hint(name: str, candidates: Iterable[str]) -> str:
    """`` (did you mean x?)`` when a candidate is the same name in another case or a near miss, else nothing."""
    by_lower = {candidate.lower(): candidate for candidate in candidates}
    close = difflib.get_close_matches(name.lower(), list(by_lower), n=1, cutoff=0.8)
    return f" (did you mean {by_lower[close[0]]}?)" if close else ""


def check_contract(docs: Docs | None, schema: Any, *, existing: Iterable[str] = ()) -> list[str]:
    """What is wrong with a gold table's contract for the columns that are about to be written.

    Only a ``gold`` table is held to its contract. For any other table, and for no docs, nothing is wrong.

    Args:
        docs: The contract, or ``None``.
        schema: The schema of the data that is written: a pyarrow schema, an Iceberg schema or a list of names.
        existing: The column names that the table has already. A column that the data no longer has but the table
            keeps is still a column of the table, so describing it is no mistake.

    Returns:
        One sentence for each problem, which lists every column it is about. Empty when the contract holds.
    """
    if docs is None or not docs.gold:
        return []
    written = column_names(schema)
    present = [*written, *(name for name in existing if name not in written)]
    problems = []
    missing = [name for name in written if name not in docs.columns]
    if missing:
        problems.append(f"{_count(len(missing), 'column')} written without a description in metadata.docs.columns: "
                        f"{', '.join(missing)}")
    unknown = [name for name in docs.columns if name not in present]
    if unknown:
        problems.append(f"metadata.docs.columns describes {_count(len(unknown), 'column')} that the table does not "
                        f"have: {', '.join(name + _hint(name, present) for name in unknown)}")
    return problems


def contract_error(table: str, problems: Iterable[str]) -> ContractError:
    """The error that a failed :func:`check_contract` raises, with one line for each problem."""
    found = list(problems)
    lines = [f"{table} does not meet its gold contract, and nothing was written:"]
    lines += [f"  - {problem}" for problem in found]
    return ContractError("\n".join(lines), found)


# ---------------------------------------------------------------------- the table


@dataclass(frozen=True)
class ContractResult:
    """What :func:`apply_contract` did to a table.

    Attributes:
        changed: Whether it made a commit. It does not when the table already carries the contract.
        properties: The table properties that it set or changed.
        columns: The columns whose doc it set or changed.
        documented: How many columns of the table carry a doc afterwards.
        ignored: The columns of the contract that the table does not have, which a gold table may not name.
        attempts: How many times it tried to commit, which is 1 unless it lost a race.
    """

    changed: bool
    properties: tuple[str, ...] = ()
    columns: tuple[str, ...] = ()
    documented: int = 0
    ignored: tuple[str, ...] = ()
    attempts: int = 1

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-serialisable primitives."""
        return {"changed": self.changed, "properties": list(self.properties), "columns": list(self.columns),
                "documented": self.documented, "ignored": list(self.ignored), "attempts": self.attempts}


def pending_changes(table: Any, docs: Docs) -> tuple[dict[str, str], dict[str, str]]:
    """What the contract would change on a table: the properties to set and the column docs to set.

    Args:
        table: A pyiceberg table.
        docs: The contract.

    Returns:
        ``(properties, column docs)``, each only with what differs from the table now. A column that the table does
        not have is left out.
    """
    held = table.properties
    properties = {key: value for key, value in docs.properties().items() if held.get(key) != value}
    fields = {item.name: item for item in table.schema().fields}
    columns = {name: text for name, text in docs.columns.items() if name in fields and fields[name].doc != text}
    return properties, columns


def apply_contract(table: Any, docs: Docs, *, policy: Any = None, dry_run: bool = False,
                   sleep: Any = time.sleep) -> ContractResult:
    """Write a contract into an Iceberg table, in one metadata-only commit.

    It sets the table properties and the doc of each column together, so a reader never sees half of it, and it adds
    no snapshot. When the table already carries every value there is nothing to commit, and it commits nothing. When
    the catalog refuses the commit because another writer changed the table meanwhile, it reloads the table, works out
    the difference again and tries again after a pause, as the ``policy`` allows.

    Args:
        table: A pyiceberg table.
        docs: The contract.
        policy: The :class:`~local_data_platform.format.iceberg.commit.CommitPolicy` that bounds the tries. Its
            defaults when ``None``.
        dry_run: Work out what would change, and commit nothing.
        sleep: How to pause between tries. Tests pass a function that does not wait.

    Returns:
        The :class:`ContractResult`. With ``dry_run`` it says what would change, and ``changed`` is whether
        anything would.

    Raises:
        CommitConflict: If another writer kept changing the table, and the policy ran out of tries. It is retriable.
    """
    from pyiceberg.exceptions import CommitFailedException

    from local_data_platform.format.iceberg.commit import CommitConflict, CommitPolicy

    policy = policy or CommitPolicy()
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        properties, columns = pending_changes(table, docs)
        have = {item.name for item in table.schema().fields}
        ignored = tuple(name for name in docs.columns if name not in have)
        if dry_run or not (properties or columns):
            return ContractResult(changed=bool(properties or columns), properties=tuple(properties),
                                  columns=tuple(columns), documented=_documented(table, columns),
                                  ignored=ignored, attempts=attempt)
        try:
            with table.transaction() as transaction:
                if properties:
                    transaction.set_properties(properties)
                if columns:
                    with transaction.update_schema() as update:
                        for name, text in columns.items():
                            update.update_column(name, doc=text)
        except CommitFailedException as error:
            if attempt >= policy.max_publish_attempts or time.monotonic() - started >= policy.deadline_s:
                raise CommitConflict(
                    f"could not write the contract of {'.'.join(table.name())} after {attempt} "
                    f"attempt{'' if attempt == 1 else 's'}, because other writers kept changing the table. "
                    f"Run it again, or run ldp gold sync. The last error was: {error}", retriable=True) from error
            sleep(policy.backoff_s(attempt))
            table.refresh()
            continue
        return ContractResult(changed=True, properties=tuple(properties), columns=tuple(columns),
                              documented=_documented(table, columns), ignored=ignored, attempts=attempt)


def _documented(table: Any, pending: Mapping[str, str]) -> int:
    """How many columns of the table have a doc, counting the ones that are about to get one."""
    return sum(1 for item in table.schema().fields if item.doc or item.name in pending)


# ---------------------------------------------------------------------- dbt


def docs_from_dbt_node(node: Mapping[str, Any], *, namespace: str | None = None,
                       primary_key: Iterable[str] = ()) -> dict[str, Any]:
    """The ``metadata.docs`` block that a dbt model stands for, read from its node in ``manifest.json``.

    It takes the description of the model, the description of each column that has one, the tags, the ``owner`` and
    ``grain`` of the model's ``meta``, the key columns and the layer. The layer is the ``ldp_layer`` of the ``meta``,
    else the ``namespace`` when that is ``bronze``, ``silver`` or ``gold``. The values go in as they are written, so
    that a wrong one is reported by the validation of the block, which names it.

    Args:
        node: The model's entry of ``manifest.json``.
        namespace: The Iceberg namespace the model is published to.
        primary_key: The key columns of the model. Without them, the ``unique_key`` of the model is used.

    Returns:
        The block, which has only the keys that the model has a value for.
    """
    meta = dict(node.get("meta") or {})
    meta.update((node.get("config") or {}).get("meta") or {})
    block: dict[str, Any] = {}
    layer = meta.get("ldp_layer")
    if layer is None and isinstance(namespace, str) and namespace.strip().lower() in LAYERS:
        layer = namespace.strip().lower()
    if layer is not None:
        block["layer"] = layer
    if _text(node.get("description")):
        block["description"] = _text(node.get("description"))
    for key in ("owner", "grain"):
        if meta.get(key) is not None:
            block[key] = meta[key]
    keys = list(primary_key) or list(_names((node.get("config") or {}).get("unique_key")))
    if keys:
        block["primary_key"] = keys
    columns = {}
    for name, column in (node.get("columns") or {}).items():
        text = _text(column.get("description")) if isinstance(column, Mapping) else None
        if text:
            columns[str(name)] = text
    if columns:
        block["columns"] = columns
    tags = [tag for tag in dict.fromkeys(node.get("tags") or ()) if isinstance(tag, str) and tag.strip()]
    if tags:
        block["tags"] = tags
    return block


__all__ = ["COMMENT", "DOCS_KEYS", "Docs", "ContractResult", "GOLD", "GRAIN", "LAYER", "LAYERS", "OWNER",
           "PRIMARY_KEY", "PROPERTY_KEYS", "apply_contract", "check_contract", "column_names", "contract_error",
           "docs_from_dbt_node", "docs_problems", "parse_docs", "pending_changes"]
