"""The ``ldp/v1`` dataset spec: its JSON Schema, validation, hashing and idempotency keys.

A spec is today's dataset config (see ``docs/design/v0_1_1.md``) with an optional
``apiVersion: "ldp/v1"``, an optional ``kind`` and an optional ``metadata.observability`` block. The ``kind``
says what a JSON file is. ``Pipeline`` is the default, so every existing config is one, and ``Workflow`` is the
second kind. Each kind has a validator and a JSON Schema in a small registry, and ``register_kind`` adds more,
which the later milestones do for metrics, reports and profiles::

    from local_data_platform.spec import idempotency_key, spec_hash, validate_spec

    problems = validate_spec(data)           # every problem at once, as ConfigError objects
    digest = spec_hash(config)               # stable over key order, whitespace and checkouts
    key = idempotency_key(config, ("2026-09-01", "2026-09-02"))

``spec_hash`` is the sha256 of the canonical JSON of the pipeline-relevant fields:
``apiVersion``, ``identifier``, ``when``, ``how`` and ``metadata`` except
``observability`` (where events go doesn't change what a run does), ``schedule`` (when a run starts doesn't change
what it does, though ``window`` does, because its column picks the rows a run reads), ``retries`` (how often a failed
run is tried again doesn't change what it does), ``docs`` (what a table is called and described doesn't change what a
run writes), ``lineage`` (which tables a source was made from is for the run events, and doesn't change what a run
does) and ``kind`` (a pipeline hashes the same with ``"kind": "Pipeline"`` as without it). Keys are
sorted, there is no whitespace, formats are upper-case, write modes lower-case, the
quality block is normalised to ``{"on_failure", "checks"}``, and local paths are
made relative to the config's folder. The descriptive ``who``, ``what`` and
``where`` are left out.

``idempotency_key`` follows SaaS design section 7.3: it hashes the pipeline, the
target table and the logical window, never the spec hash, so redeploying a spec and
re-running a window finds the earlier commit instead of appending twice. ``replace_key`` is the key of the run that
replaces a window that is already there: it hashes that key and the spec hash.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from local_data_platform import contracts
from local_data_platform.exceptions import ConfigError, PipelineNotFound
from local_data_platform.orchestration import retries, windows

API_VERSION = "ldp/v1"
"""The spec version this library reads and writes."""

DEFAULT_KIND = "Pipeline"
"""The kind of a file that has no ``kind``: a dataset config."""

RESERVED_KINDS = {"Report": "0.1.6", "Profiles": "0.2.0"}
"""Kinds that later milestones register through :func:`register_kind`, and the version that does."""

TOP_LEVEL_KEYS = ("apiVersion", "kind", "identifier", "who", "what", "where", "when", "how", "metadata")
FORMATS = ("CSV", "PARQUET", "JSON", "ICEBERG", "DUCKDB", "XLSX")
WRITE_MODES = ("append", "overwrite", "upsert")
ON_FAILURE = ("fail", "warn")
HASH_EXCLUDED_METADATA = ("observability", "schedule", "retries", "docs", "lineage")
"""``metadata`` keys that don't change what a run does, so :func:`spec_hash` ignores them."""

_BUILTIN_CATALOG_TYPES = ("local", "sql", "rest", "glue")
_CATALOG_ALIASES = {"localiceberg": "local"}
_TRANSFORM = re.compile(r"^(identity|year|month|day|hour|(bucket|truncate)\[\s*[1-9]\d*\s*\])$", re.IGNORECASE)
_PATH_KEYS = {"path", "warehouse_path", "query_file", "inputs"}
_UPPER_KEYS = {"format", "engine"}
_LOWER_KEYS = {"write_mode", "on_failure", "transform", "type"}
_SECRET_WORDS = ("password", "passwd", "secret", "token", "credential", "api_key", "apikey", "private_key")
_REFERENCE_SUFFIXES = ("_env", "_path", "_file", "_ref")
_INLINE_SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b")
_UTC = dt.timezone.utc


# ---------------------------------------------------------------------- helpers


def _spec_dict(config: Any) -> Mapping[str, Any]:
    """Return a spec as a mapping: a dict as is, a :class:`~local_data_platform.Config` as its fields."""
    if isinstance(config, Mapping):
        return config
    if hasattr(config, "identifier") and hasattr(config, "metadata"):
        data: dict[str, Any] = {"identifier": config.identifier, "metadata": config.metadata}
        for key in ("who", "what", "where", "when", "how"):
            if getattr(config, key, None):
                data[key] = getattr(config, key)
        version = getattr(config, "api_version", None) or getattr(config, "apiVersion", None)
        if version:
            data["apiVersion"] = version
        return data
    raise TypeError(f"expected a spec dict or a local_data_platform.Config, got {type(config).__name__}")


def _base_dir(config: Any, base_dir: str | os.PathLike | None) -> Path | None:
    if base_dir is not None:
        return Path(base_dir)
    value = getattr(config, "base_dir", None)
    return Path(value) if value else None


def canonical_json(value: Any) -> bytes:
    """Encode ``value`` as canonical JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _relative_path(text: str, base: Path | None) -> str:
    if "://" in text and not text.startswith("file://"):
        return text  # an object-store or remote URI: already location-independent
    if text.startswith("file://"):
        text = text[len("file://"):]
    if text.startswith("~"):
        return PurePath(os.path.normpath(text)).as_posix()
    normalised = os.path.normpath(text)
    if os.path.isabs(normalised) and base is not None:
        root = os.path.normpath(os.path.abspath(base))
        try:
            inside = os.path.commonpath([root, normalised]) == root
        except ValueError:  # different drives on Windows
            inside = False
        if inside:
            normalised = os.path.relpath(normalised, root)
    return PurePath(normalised).as_posix()


def _canonical(value: Any, base: Path | None, key: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v, base, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item, base, key) for item in value]
    if isinstance(value, str):
        if key in _UPPER_KEYS:
            return value.strip().upper()
        if key in _LOWER_KEYS:
            return value.strip().lower().replace(" ", "")
        if key in _PATH_KEYS or key == "warehouse":
            return _relative_path(value, base)
    return value


def _normalise_quality(quality: Any) -> Any:
    if quality is None:
        return None
    if isinstance(quality, list):
        return {"on_failure": "fail", "checks": list(quality)}
    if isinstance(quality, Mapping):
        normalised = dict(quality)
        normalised.setdefault("on_failure", "fail")
        normalised.setdefault("checks", [])
        return normalised
    return quality


def canonical_spec(config: Any, *, base_dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """Return the canonical form of a spec that :func:`spec_hash` hashes.

    Args:
        config: A spec dict or a :class:`~local_data_platform.Config`.
        base_dir: Folder local paths are made relative to. Defaults to the config's
            ``base_dir``.

    Returns:
        ``{"apiVersion", "identifier", "when"?, "how"?, "metadata"}`` with the
        normalisations described in the module docstring. ``kind`` is not in it, so a config has the
        same hash with and without ``"kind": "Pipeline"``.
    """
    data = _spec_dict(config)
    base = _base_dir(config, base_dir)
    out: dict[str, Any] = {"apiVersion": data.get("apiVersion") or API_VERSION, "identifier": data.get("identifier")}
    for key in ("when", "how"):
        if data.get(key):
            out[key] = data[key]
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        metadata = {k: v for k, v in metadata.items() if k not in HASH_EXCLUDED_METADATA}
        if "quality" in metadata:
            quality = _normalise_quality(metadata["quality"])
            if quality is None:
                del metadata["quality"]
            else:
                metadata["quality"] = quality
        out["metadata"] = _canonical(metadata, base)
    else:
        out["metadata"] = metadata
    return out


def spec_hash(config: Any, *, base_dir: str | os.PathLike | None = None) -> str:
    """Return the sha256 hex digest of :func:`canonical_spec`.

    The hash is the same for specs that differ only in key order, whitespace, format
    case, the list or object form of ``quality``, the ``observability`` block, the
    descriptive ``who``/``what``/``where`` fields, or where the project is checked out.

    Args:
        config: A spec dict or a :class:`~local_data_platform.Config`.
        base_dir: Folder local paths are made relative to (default: the config's).

    Returns:
        64 lowercase hex characters.
    """
    return hashlib.sha256(canonical_json(canonical_spec(config, base_dir=base_dir))).hexdigest()


def _to_utc(value: Any, what: str) -> dt.datetime:
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        moment = dt.datetime(value.year, value.month, value.day)
    elif isinstance(value, str) and value.strip():
        try:
            moment = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            raise ConfigError(f"window {what} {value!r} is not an ISO-8601 date or datetime") from None
    else:
        raise ConfigError(f"window {what} must be a datetime, a date or an ISO-8601 string, got {value!r}")
    return moment.replace(tzinfo=_UTC) if moment.tzinfo is None else moment.astimezone(_UTC)


def parse_window(window: Any, *, timezone: Any = None,
                 now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime] | None:
    """Parse a logical window into two UTC datetimes.

    Args:
        window: ``None``; a ``"START/END"`` ISO-8601 interval string; a
            ``(start, end)`` pair; ``{"start", "end"}``; or one of the shortcuts ``yesterday``, ``last-week``
            (the last full week from Monday to Monday) and ``last-month``. Each bound is a datetime,
            a date (midnight) or an ISO string. Naive values are read as UTC.
        timezone: The time zone a shortcut counts its days in, as a name or a ``tzinfo``: UTC by default. A
            config's ``metadata.window.timezone`` is what :func:`window_bounds` passes.
        now: The current time, for the shortcuts. The clock by default.

    Returns:
        ``(start, end)`` in UTC, or ``None`` for no window.

    Raises:
        ConfigError: If the window is malformed or ``end`` is not after ``start``.
    """
    if window is None:
        return None
    if isinstance(window, str):
        if windows.is_shortcut(window):
            return windows.shortcut_window(window, timezone, now)
        parts = window.split("/")
        if len(parts) != 2:
            raise ConfigError(f"window {window!r} must be an ISO-8601 interval 'START/END', or one of "
                              f"{', '.join(windows.SHORTCUTS)}")
        start, end = parts
    elif isinstance(window, Mapping):
        if set(window) != {"start", "end"}:
            raise ConfigError(f"window must be {{'start', 'end'}}, got keys {sorted(window)}")
        start, end = window["start"], window["end"]
    elif isinstance(window, (list, tuple)) and len(window) == 2:
        start, end = window
    else:
        raise ConfigError(f"window must be 'START/END', a (start, end) pair or {{'start', 'end'}}, got {window!r}")
    bounds = _to_utc(start, "start"), _to_utc(end, "end")
    if bounds[1] <= bounds[0]:
        raise ConfigError(f"window end {bounds[1].isoformat()} must be after its start {bounds[0].isoformat()}")
    return bounds


def window_bounds(config: Any, window: Any, *,
                  now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime] | None:
    """Parse a window for a config, counting a shortcut such as ``yesterday`` in the config's time zone.

    It is :func:`parse_window` with the time zone of the config's ``metadata.window``, so ``ldp plan``,
    ``ldp run`` and :func:`idempotency_key` all read ``yesterday`` as the same days.

    Args:
        config: A spec dict, a :class:`~local_data_platform.Config`, or ``None`` for UTC.
        window: Anything :func:`parse_window` accepts.
        now: The current time, for the shortcuts.

    Raises:
        ConfigError: If the window is malformed, or the config's time zone is unknown.
    """
    timezone = None
    if windows.is_shortcut(window) and config is not None:
        timezone = windows.timezone_of(_spec_dict(config).get("metadata"))
    return parse_window(window, timezone=timezone, now=now)


def format_instant(moment: dt.datetime) -> str:
    """Format a datetime as ISO-8601 in UTC with a ``Z`` suffix."""
    return moment.astimezone(_UTC).isoformat().replace("+00:00", "Z")


def target_identity(config: Any, *, base_dir: str | os.PathLike | None = None) -> str:
    """Name the spec's target location, for keys: ``iceberg:<namespace>.<table>`` or ``<format>:<path>``.

    Raises:
        ConfigError: If the spec has no usable target block.
    """
    data = _spec_dict(config)
    metadata = data.get("metadata")
    target = metadata.get("target") if isinstance(metadata, Mapping) else None
    if not isinstance(target, Mapping):
        raise ConfigError("spec metadata is missing the 'target' object")
    fmt = str(target.get("format", "")).strip().upper()
    if fmt == "ICEBERG":
        catalog = target.get("catalog") if isinstance(target.get("catalog"), Mapping) else {}
        namespace = catalog.get("namespace") or catalog.get("identifier")
        if not namespace or not target.get("name"):
            raise ConfigError("an Iceberg target needs 'name' and a catalog 'identifier' or 'namespace'")
        return f"iceberg:{namespace}.{target['name']}"
    location = target.get("path") or target.get("name")
    if not location:
        raise ConfigError("the target needs a 'path' or a 'name'")
    return f"{fmt.lower()}:{_relative_path(str(location), _base_dir(config, base_dir))}"


def idempotency_key(config: Any, window: Any = None, *, table_uuid: str | None = None,
                    base_dir: str | os.PathLike | None = None) -> str:
    """Return the idempotency key ``K`` of one run of a spec over one logical window.

    ``K = sha256(canonical JSON of ["ldp/v1", "idempotency", pipeline, target,
    window_start, window_end])`` (SaaS design section 7.3). ``pipeline`` is the spec's
    ``identifier``. ``target`` is ``table_uuid`` when given (so dropping and
    recreating the table starts a new key space), else :func:`target_identity`. The
    spec hash is deliberately not part of the key.

    Args:
        config: A spec dict or a :class:`~local_data_platform.Config`.
        window: The logical window, in any form :func:`parse_window` accepts (a shortcut such as ``yesterday`` is
            counted in the config's time zone), or ``None`` for a run that owns the whole table.
        table_uuid: The target Iceberg table's UUID, if known.
        base_dir: Folder local paths are made relative to (default: the config's).

    Returns:
        64 lowercase hex characters.

    Raises:
        ConfigError: If the spec has no identifier or target, or the window is malformed.
    """
    data = _spec_dict(config)
    pipeline = data.get("identifier")
    if not pipeline or not isinstance(pipeline, str):
        raise ConfigError("the spec needs an 'identifier' to derive an idempotency key")
    target = str(table_uuid) if table_uuid else target_identity(config, base_dir=base_dir)
    bounds = window_bounds(config, window)
    start, end = (format_instant(bounds[0]), format_instant(bounds[1])) if bounds else ("", "")
    return hashlib.sha256(canonical_json([API_VERSION, "idempotency", pipeline, target, start, end])).hexdigest()


def replace_key(key: str, spec_hash: str) -> str:
    """Return the idempotency key of the run that replaces a window, from the key of the run it replaces.

    ``R = sha256(canonical JSON of ["ldp/v1", "replace", K, spec_hash])``, where ``K`` is the key that
    :func:`idempotency_key` gives the window and ``spec_hash`` is the :func:`spec_hash` of the config. A published
    window is skipped when it runs again, so redoing one needs a key that is not on the table. This one is the same
    for the same window under the same spec, so a window that was replaced is not replaced again, and it is new when
    the spec changes, so a window is replaced once more after the config was fixed.

    Args:
        key: The idempotency key of the window, 64 hex characters.
        spec_hash: The spec hash of the config that replaces it.

    Returns:
        64 lowercase hex characters.

    Raises:
        ConfigError: If ``key`` or ``spec_hash`` is not a non-empty string.
    """
    for name, value in (("key", key), ("spec_hash", spec_hash)):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{name} must be a non-empty string, got {value!r}")
    return hashlib.sha256(canonical_json([API_VERSION, "replace", key, spec_hash])).hexdigest()


# ---------------------------------------------------------------------- kinds


@dataclass(frozen=True)
class SpecKind:
    """What the registry holds for one kind of file.

    Attributes:
        name: The kind, as a file writes it in ``kind``.
        validate: Takes the spec as a mapping and returns its problems, as :func:`validate_spec` does.
        json_schema: Returns the kind's JSON Schema, which ``ldp schema --kind NAME`` prints.
        describe: Returns what ``ldp plan`` reports about a valid file of this kind, besides its name and
            kind, as a dict. Optional.
    """

    name: str
    validate: Callable[[Mapping[str, Any]], list[ConfigError]]
    json_schema: Callable[[], dict[str, Any]]
    describe: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None


_KINDS: dict[str, SpecKind] = {}
_KIND_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")


def register_kind(name: str, *, validate: Callable[[Mapping[str, Any]], list[ConfigError]],
                  json_schema: Callable[[], dict[str, Any]],
                  describe: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
                  replace: bool = False) -> SpecKind:
    """Add a kind of file to the registry, so ``validate_spec``, ``plan`` and ``ldp schema --kind`` know it.

    Args:
        name: The kind's name. It starts with a capital letter and has only letters and digits.
        validate: Checks a spec of this kind and returns its problems.
        json_schema: Returns the kind's JSON Schema.
        describe: Returns what ``plan`` reports about a valid file of this kind.
        replace: Replace a kind that is already registered, as a module does when it takes over the
            validator of a kind that a smaller one registered first.

    Returns:
        The registered kind.

    Raises:
        ValueError: If the name is not a kind name, or it is registered and ``replace`` is false.
    """
    if not isinstance(name, str) or not _KIND_NAME.match(name):
        raise ValueError(f"a kind name starts with a capital letter and has only letters and digits, got {name!r}")
    if name in _KINDS and not replace:
        raise ValueError(f"the kind {name!r} is already registered; pass replace=True to replace it")
    _KINDS[name] = SpecKind(name, validate, json_schema, describe)
    return _KINDS[name]


def registered_kinds() -> list[str]:
    """The registered kinds, in the order they were registered."""
    return list(_KINDS)


def _unknown_kind(value: Any) -> ConfigError:
    known = registered_kinds()
    if isinstance(value, str):
        for name in (*known, *RESERVED_KINDS):
            if name.lower() == value.strip().lower() and name != value:
                if name in _KINDS:
                    return ConfigError(f"unknown kind {value!r}; did you mean {name!r}? Kind names are written "
                                       f"exactly as in {known}")
                return ConfigError(f"unknown kind {value!r}; did you mean {name!r}? That kind is reserved for "
                                   f"{RESERVED_KINDS[name]} and is not available yet. The kinds are {known}")
        if value in RESERVED_KINDS:
            return ConfigError(f"kind {value!r} is reserved for {RESERVED_KINDS[value]} and is not available yet. "
                               f"The kinds are {known}")
    return ConfigError(f"unknown kind {value!r}; the kinds are {known}")


def kind_of(data: Any) -> str:
    """The kind a spec says it is: its ``kind``, or ``Pipeline`` when it has none.

    Args:
        data: A spec dict or a :class:`~local_data_platform.Config`.

    Returns:
        A registered kind.

    Raises:
        ConfigError: If ``kind`` is not a string or not a registered kind. A name in the wrong case fails with
            the right spelling, and a kind that a later milestone adds fails with the version.
        TypeError: If ``data`` is neither a spec dict nor a ``Config``.
    """
    value = _spec_dict(data).get("kind", DEFAULT_KIND)
    if not isinstance(value, str):
        raise ConfigError(f"kind must be a string, got {value!r}")
    if value not in _KINDS:
        raise _unknown_kind(value)
    return value


def kind_of_file(path: str | os.PathLike) -> str | None:
    """The kind of the JSON file at ``path``, or ``None`` when it cannot be read as a spec.

    A file that has no ``kind`` is a ``Pipeline``. ``None`` means the file is not JSON, is not an object, or names a
    kind that is not registered. Code that reads a folder of files uses it to pick the files of one kind and to let
    the rest report their own problem.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    try:
        return kind_of(data)
    except ConfigError:
        return None


def kind_schema(kind: str = DEFAULT_KIND) -> dict[str, Any]:
    """The JSON Schema of one kind of file.

    Raises:
        ConfigError: If ``kind`` is not a registered kind.
    """
    if not isinstance(kind, str) or kind not in _KINDS:
        raise _unknown_kind(kind)
    return _KINDS[kind].json_schema()


# ---------------------------------------------------------------------- JSON Schema


def json_schema() -> dict[str, Any]:
    """Return the ``ldp/v1`` JSON Schema (draft 2020-12) of a dataset spec.

    The top level is closed (unknown keys are errors, as in ``Config.from_dict``);
    source and target blocks allow extra keys, which the pipelines log and ignore.
    Formats are listed in upper and lower case; the library accepts any case.
    """
    from local_data_platform.events import SINK_TYPES
    from local_data_platform.quality.runner import CHECKS

    formats = [f for name in FORMATS for f in (name, name.lower())]
    string = {"type": "string"}
    non_empty = {"type": "string", "minLength": 1}
    check = {
        "type": "object",
        "required": ["check"],
        "properties": {"check": {"type": "string", "enum": sorted(CHECKS)}, "name": non_empty},
        "additionalProperties": True,
        "description": "A quality check; the other keys are the check's parameters.",
    }
    sink_names = list(SINK_TYPES) + ["none"]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local-data-platform.readthedocs.io/schemas/ldp-v1.json",
        "title": f"local-data-platform dataset spec ({API_VERSION})",
        "type": "object",
        "required": ["identifier", "metadata"],
        "additionalProperties": False,
        "properties": {
            "apiVersion": {"const": API_VERSION, "default": API_VERSION},
            "kind": {"const": DEFAULT_KIND, "default": DEFAULT_KIND,
                     "description": "What the file is. A dataset config is a Pipeline, which is the default."},
            "identifier": {**non_empty, "description": "The pipeline's id."},
            "who": {**string, "description": "Who owns the dataset."},
            "what": {**string, "description": "What the dataset holds."},
            "where": {**string, "description": "Where the data comes from."},
            "when": {**string, "description": "How often it runs, e.g. 'daily'."},
            "how": {**string, "description": "How it runs, e.g. 'batch'."},
            "metadata": {"$ref": "#/$defs/metadata"},
        },
        "$defs": {
            "metadata": {
                "type": "object",
                "required": ["source", "target"],
                "properties": {
                    "source": {"$ref": "#/$defs/source"},
                    "target": {"$ref": "#/$defs/target"},
                    "quality": {"$ref": "#/$defs/quality"},
                    "observability": {"$ref": "#/$defs/observability"},
                    "schedule": {
                        **non_empty,
                        "description": "When the config runs: a 5-field cron expression, or @hourly, @daily, @weekly "
                                       "or @monthly. Without it, a 'when' of hourly, daily, weekly or monthly means "
                                       "the alias. Editing it does not change the spec hash.",
                        "examples": ["@daily", "5 2 * * *"],
                    },
                    "window": {"$ref": "#/$defs/window"},
                    "retries": {"$ref": "#/$defs/retries"},
                    "docs": {"$ref": "#/$defs/docs"},
                    "lineage": {"$ref": "#/$defs/lineage"},
                },
            },
            "lineage": {
                "type": "array",
                "items": {"$ref": "#/$defs/source"},
                "description": "The Iceberg tables that the source was made from, for the run events. The Iceberg "
                               "blocks of the tables a model of a dbt project reads, written by ldp dbt build. A "
                               "DUCKDB source with 'inputs' needs none. Editing it does not change the spec hash.",
            },
            "docs": {
                "type": "object",
                "additionalProperties": False,
                "description": "The contract of the table: what it means, who owns it, what one row is and what each "
                               "column holds. A run writes it into the Iceberg table as the properties comment, "
                               "ldp.owner, ldp.layer, ldp.grain and ldp.primary-key and the doc of each column. A "
                               "gold table needs a description, an owner (or a 'who' in the config) and a grain, "
                               "and a description for every column it writes. Editing it does not change the spec "
                               "hash.",
                "properties": {
                    "layer": {"enum": list(contracts.LAYERS), "description": "Only gold is held to its contract."},
                    "description": {**non_empty, "description": "What the table means."},
                    "owner": {**non_empty, "description": "Who owns it. The config's 'who' by default."},
                    "grain": {**non_empty, "description": "What one row is, such as one row per restaurant per day."},
                    "primary_key": {"oneOf": [non_empty, {"type": "array", "items": non_empty, "minItems": 1}],
                                    "description": "The column or columns that identify a row."},
                    "columns": {"type": "object", "additionalProperties": non_empty,
                                "description": "Column name to what it holds."},
                    "tags": {"type": "array", "items": non_empty},
                    "refresh": {**non_empty, "description": "How often it is rebuilt. The config's 'when' by default."},
                },
                "allOf": [{"if": {"properties": {"layer": {"const": "gold"}}, "required": ["layer"]},
                           "then": {"required": ["description", "grain"]}}],
            },
            "retries": {**retries.retries_schema(),
                        "description": "How the run is tried again after a failure, when a workflow runs it. Editing "
                                       "it does not change the spec hash."},
            "format": {"type": "string", "enum": formats},
            "block": {
                "type": "object",
                "required": ["name", "format"],
                "properties": {
                    "name": non_empty,
                    "format": {"$ref": "#/$defs/format"},
                    "path": non_empty,
                    "engine": non_empty,
                    "catalog": {"$ref": "#/$defs/catalog"},
                    "credentials": {"type": "object", "properties": {"name": string, "path": non_empty}},
                    "relation": {**non_empty,
                                 "description": "DUCKDB source: a table or view, as table, schema.table or "
                                                "catalog.schema.table."},
                    "sql": {**non_empty, "description": "DUCKDB source: one SELECT or WITH query."},
                    "query_file": {**non_empty, "description": "DUCKDB source: a file that holds one SELECT or WITH "
                                                               "query, instead of 'sql'. A relative path is read from "
                                                               "the folder of the config."},
                    "inputs": {"type": "array", "items": non_empty, "minItems": 1, "uniqueItems": True,
                               "description": "DUCKDB source: dataset config files whose Iceberg target tables the "
                                              "query reads, each under its table name, in an in-memory DuckDB with the "
                                              "session time zone UTC. It replaces 'path', and a query that reads "
                                              "another table fails, so it is the whole lineage."},
                    "sheet": {"oneOf": [non_empty, {"type": "integer", "minimum": 0}],
                              "description": "XLSX source: the sheet's name, or its position from 0. The first "
                                             "sheet by default."},
                    "header_row": {"type": "integer", "minimum": 1,
                                   "description": "XLSX source: the row that holds the column names, counted "
                                                  "from 1. Rows above it are skipped."},
                },
                "allOf": [
                    {"if": {"properties": {"format": {"enum": ["ICEBERG", "iceberg"]}}, "required": ["format"]},
                     "then": {"required": ["catalog"]},
                     "else": {
                         "if": {"properties": {"format": {"enum": ["DUCKDB", "duckdb"]}}, "required": ["format"]},
                         "then": {"oneOf": [
                             {"required": ["path", "relation"],
                              "not": {"anyOf": [{"required": ["sql"]}, {"required": ["query_file"]},
                                                {"required": ["inputs"]}]}},
                             {"required": ["path"], "oneOf": [{"required": ["sql"]}, {"required": ["query_file"]}],
                              "not": {"anyOf": [{"required": ["relation"]}, {"required": ["inputs"]}]}},
                             {"required": ["inputs"], "oneOf": [{"required": ["sql"]}, {"required": ["query_file"]}],
                              "not": {"anyOf": [{"required": ["relation"]}, {"required": ["path"]}]}},
                         ]},
                         "else": {"required": ["path"]}}},
                ],
            },
            "source": {
                "allOf": [{"$ref": "#/$defs/block"}],
                "properties": {
                    "path": {**non_empty,
                             "description": "Where the source is. It may hold {window_start:FORMAT} and "
                                            "{window_end:FORMAT}, where FORMAT is a Python strftime format, e.g. "
                                            "drops/orders_{window_start:%Y-%m-%d}.csv. A run reads the path of its "
                                            "window, and a run without a window fails."},
                },
            },
            "window": {
                "description": "How the runs of a schedule are windowed: 'none', or the size of a window, the date or "
                               "timestamp column that selects the rows of a window, and the time zone its days are "
                               "counted in (UTC by default).",
                "oneOf": [
                    {"const": "none"},
                    {"type": "object", "required": ["size"], "additionalProperties": False,
                     "properties": {"size": {"enum": list(windows.WINDOW_SIZES)}, "column": non_empty,
                                    "timezone": {**non_empty, "examples": ["UTC", "Europe/Paris"]}}},
                ],
            },
            "target": {
                "allOf": [{"$ref": "#/$defs/block"}],
                "properties": {
                    "format": {"not": {"enum": ["DUCKDB", "duckdb", "XLSX", "xlsx"]},
                               "description": "A DUCKDB or XLSX block is a source only."},
                    "write_mode": {"type": "string", "enum": list(WRITE_MODES)},
                    "join_cols": {"oneOf": [non_empty, {"type": "array", "items": non_empty, "minItems": 1}]},
                    "partition_by": {"type": "array", "items": {"$ref": "#/$defs/partitionField"}},
                    "schema_evolution": {"type": "boolean"},
                },
            },
            "catalog": {
                "type": "object",
                "description": "The Iceberg catalog. 'type' defaults to 'local'. Secrets are referenced by the "
                               "name of an environment variable (token_env, credential_env), never inlined.",
                "properties": {
                    "type": {**non_empty, "examples": list(_BUILTIN_CATALOG_TYPES)},
                    "identifier": non_empty,
                    "namespace": non_empty,
                    "warehouse_path": non_empty,
                    "warehouse": non_empty,
                    "uri": non_empty,
                    "name": non_empty,
                    "token_env": non_empty,
                    "credential_env": non_empty,
                    "properties": {"type": "object", "additionalProperties": string},
                },
                "anyOf": [{"required": ["identifier"]}, {"required": ["namespace"]}],
            },
            "partitionField": {
                "type": "object",
                "required": ["column", "transform"],
                "additionalProperties": False,
                "properties": {
                    "column": non_empty,
                    "transform": {**non_empty, "examples": ["identity", "day", "bucket[8]", "truncate[4]"]},
                    "name": non_empty,
                },
            },
            "check": check,
            "quality": {
                "oneOf": [
                    {"type": "array", "items": {"$ref": "#/$defs/check"}},
                    {"type": "object",
                     "properties": {"on_failure": {"enum": list(ON_FAILURE)},
                                    "checks": {"type": "array", "items": {"$ref": "#/$defs/check"}}}},
                ],
            },
            "observability": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sinks": {"type": "array", "items": {"anyOf": [
                        {"type": "string", "enum": sink_names},
                        {"type": "object", "required": ["type"], "properties": {"type": {"enum": sink_names}}},
                    ]}},
                    "null": {"type": "object"},
                    "jsonl": {"type": "object", "additionalProperties": False, "properties": {"path": non_empty}},
                    "openlineage": {"type": "object", "additionalProperties": False, "properties": {
                        "path": non_empty, "url": non_empty, "namespace": non_empty, "api_key_env": non_empty,
                        "timeout": {"type": "number", "exclusiveMinimum": 0}}},
                    "iceberg": {"type": "object", "additionalProperties": False, "properties": {
                        "catalog": {"$ref": "#/$defs/catalog"}, "namespace": non_empty,
                        "batch_size": {"type": "integer", "minimum": 1}}},
                },
            },
        },
    }


# ---------------------------------------------------------------------- validation


def _catalog_types() -> list[str]:
    try:
        from local_data_platform.catalog import provider
    except ImportError:  # pragma: no cover - the provider ships with the package
        return list(_BUILTIN_CATALOG_TYPES)
    registered = getattr(provider, "registered_catalog_types", None)
    if callable(registered):
        return list(registered())
    return list(getattr(provider, "_REGISTRY", {}) or _BUILTIN_CATALOG_TYPES)


class _Problems:
    def __init__(self) -> None:
        self.errors: list[ConfigError] = []

    def add(self, where: str, message: str) -> None:
        self.errors.append(ConfigError(f"{where}: {message}" if where else message))


def _join(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _check_string(problems: _Problems, block: Mapping[str, Any], key: str, where: str, required: bool = True) -> None:
    value = block.get(key)
    if value is None:
        if required:
            problems.add(_join(where, key), "is required")
    elif not isinstance(value, str) or not value.strip():
        problems.add(_join(where, key), f"must be a non-empty string, got {value!r}")


def _check_catalog(problems: _Problems, catalog: Any, where: str) -> None:
    if not isinstance(catalog, Mapping):
        problems.add(where, f"must be an object, got {type(catalog).__name__}")
        return
    raw_type = catalog.get("type", "local")
    kind = _CATALOG_ALIASES.get(str(raw_type).lower(), str(raw_type).lower())
    known = _catalog_types()
    if kind not in known:
        problems.add(f"{where}.type", f"unknown catalog type {raw_type!r}; registered types: {sorted(known)}")
        return
    if kind == "local":
        for key in ("identifier", "warehouse_path"):
            _check_string(problems, catalog, key, where)
        return
    if not (catalog.get("namespace") or catalog.get("identifier")):
        problems.add(where, f"a {kind!r} catalog needs a 'namespace' (or 'identifier')")
    if kind in ("sql", "rest"):
        _check_string(problems, catalog, "uri", where)
    properties = catalog.get("properties")
    if properties is not None and not isinstance(properties, Mapping):
        problems.add(f"{where}.properties", "must be an object of string properties")


def _check_partition_by(problems: _Problems, items: Any, where: str) -> None:
    if not isinstance(items, (list, tuple)):
        problems.add(where, f"must be a list of {{'column', 'transform'}} objects, got {type(items).__name__}")
        return
    for position, item in enumerate(items):
        here = f"{where}[{position}]"
        if not isinstance(item, Mapping):
            problems.add(here, "must be an object with 'column' and 'transform'")
            continue
        unknown = sorted(set(item) - {"column", "transform", "name"})
        if unknown:
            problems.add(here, f"has unknown keys {unknown}")
        _check_string(problems, item, "column", here)
        transform = item.get("transform")
        if not isinstance(transform, str) or not _TRANSFORM.match(transform.strip()):
            problems.add(f"{here}.transform", f"unknown transform {transform!r}; expected identity, year, month, "
                                              "day, hour, bucket[N] or truncate[W]")
        if "name" in item:
            _check_string(problems, item, "name", here)


def _check_block(problems: _Problems, block: Any, section: str) -> None:
    where = f"metadata.{section}"
    if not isinstance(block, Mapping):
        problems.add(where, "is required and must be an object" if block is None else "must be an object")
        return
    raw_format = block.get("format")
    fmt = raw_format.strip().upper() if isinstance(raw_format, str) else None
    if fmt not in FORMATS:
        problems.add(f"{where}.format", f"must be one of {list(FORMATS)}, got {raw_format!r}")
        return
    _check_string(problems, block, "name", where)
    _check_string(problems, block, "engine", where, required=False)
    if fmt == "ICEBERG":
        name = block.get("name")
        if isinstance(name, str) and "." in name:
            problems.add(f"{where}.name", "must not contain '.'; set the namespace in the catalog")
        if "catalog" not in block:
            problems.add(f"{where}.catalog", "is required for an Iceberg table")
        else:
            _check_catalog(problems, block["catalog"], f"{where}.catalog")
        if section == "target":
            _check_iceberg_target(problems, block, where)
        return
    _check_string(problems, block, "path", where, required=not (fmt == "DUCKDB" and block.get("inputs") is not None))
    if fmt == "DUCKDB":
        _check_duckdb_block(problems, block, section, where)
    if fmt == "XLSX":
        _check_xlsx_block(problems, block, section, where)
    if fmt == "JSON" and str(block.get("engine", "")).strip().upper() == "BIGQUERY":
        credentials = block.get("credentials")
        if credentials is not None:
            if not isinstance(credentials, Mapping):
                problems.add(f"{where}.credentials", "must be an object with a 'path' to a key file")
            else:
                _check_string(problems, credentials, "path", f"{where}.credentials")


def _check_duckdb_block(problems: _Problems, block: Mapping[str, Any], section: str, where: str) -> None:
    if section == "target":
        problems.add(f"{where}.format", "DUCKDB is a source format only; write to ICEBERG instead")
    present = [key for key in ("relation", "sql", "query_file") if block.get(key) is not None]
    if len(present) != 1:
        problems.add(where, "needs exactly one of 'relation' (a table or view), 'sql' (one SELECT query) and "
                            "'query_file' (a file that holds one SELECT query), "
                            f"got {'more than one' if present else 'none'}")
    else:
        _check_string(problems, block, present[0], where)
    inputs = block.get("inputs")
    if inputs is None:
        return
    if block.get("path") is not None:
        problems.add(where, "has both 'path' and 'inputs'. A source reads a DuckDB file or the Iceberg tables of its "
                            "inputs, not both")
    if block.get("relation") is not None:
        problems.add(f"{where}.inputs", "cannot go with a 'relation', which is a table of a DuckDB file. Use 'sql' "
                                        "or 'query_file' over the inputs")
    if not isinstance(inputs, (list, tuple)) or not inputs or not all(
            isinstance(item, str) and item.strip() for item in inputs):
        problems.add(f"{where}.inputs", "must be a non-empty list of dataset config paths")
        return
    for position, item in enumerate(inputs):
        if inputs.index(item) != position:
            problems.add(f"{where}.inputs[{position}]", f"lists {item!r} a second time")


def _check_xlsx_block(problems: _Problems, block: Mapping[str, Any], section: str, where: str) -> None:
    if section == "target":
        problems.add(f"{where}.format", "XLSX is a source format only; write to CSV or ICEBERG instead")
    path = block.get("path")
    if isinstance(path, str) and path.strip().lower().endswith(".xls"):
        problems.add(f"{where}.path", "is an old .xls workbook, which is not supported; save it as .xlsx")
    sheet = block.get("sheet")
    if sheet is not None and not (isinstance(sheet, str) and sheet.strip()) and not (
            isinstance(sheet, int) and not isinstance(sheet, bool) and sheet >= 0):
        problems.add(f"{where}.sheet", f"must be a sheet name or a position from 0, got {sheet!r}")
    header_row = block.get("header_row")
    if header_row is not None and (not isinstance(header_row, int) or isinstance(header_row, bool)
                                   or header_row < 1):
        problems.add(f"{where}.header_row", f"must be a whole number from 1, got {header_row!r}")


def _check_iceberg_target(problems: _Problems, block: Mapping[str, Any], where: str) -> None:
    mode = block.get("write_mode")
    normalised = mode.strip().lower() if isinstance(mode, str) else mode
    if mode is not None and normalised not in WRITE_MODES:
        problems.add(f"{where}.write_mode", f"must be one of {list(WRITE_MODES)}, got {mode!r}")
    join_cols = block.get("join_cols")
    if join_cols is not None:
        columns = [join_cols] if isinstance(join_cols, str) else join_cols
        if not isinstance(columns, (list, tuple)) or not columns or not all(isinstance(c, str) and c for c in columns):
            problems.add(f"{where}.join_cols", f"must be a column name or a list of column names, got {join_cols!r}")
    if normalised == "upsert" and not join_cols:
        problems.add(f"{where}.join_cols", "is required when write_mode is 'upsert'")
    if block.get("partition_by") is not None:
        _check_partition_by(problems, block["partition_by"], f"{where}.partition_by")
    evolution = block.get("schema_evolution")
    if evolution is not None and not isinstance(evolution, bool):
        problems.add(f"{where}.schema_evolution", f"must be true or false, got {evolution!r}")


def _check_quality(problems: _Problems, quality: Any) -> None:
    where = "metadata.quality"
    if quality is None:
        return
    if isinstance(quality, list):
        checks = quality
    elif isinstance(quality, Mapping):
        on_failure = quality.get("on_failure", "fail")
        if on_failure not in ON_FAILURE:
            problems.add(f"{where}.on_failure", f"must be 'fail' or 'warn', got {on_failure!r}")
        checks = quality.get("checks", [])
        if not isinstance(checks, list):
            problems.add(f"{where}.checks", "must be a list")
            return
    else:
        problems.add(where, "must be a list of checks or an object with 'on_failure' and 'checks'")
        return
    from local_data_platform.quality import checks_from_config

    for index, item in enumerate(checks):
        try:
            checks_from_config([item])
        except ConfigError as exc:
            problems.add(f"{where}.checks[{index}]", str(exc).replace("quality check #1", "check", 1))


def _check_schedule_and_window(problems: _Problems, metadata: Mapping[str, Any]) -> None:
    """Check ``metadata.schedule``, ``metadata.window`` and the window placeholders of the source path."""
    if metadata.get("schedule") is not None:
        try:
            windows.parse_schedule(metadata["schedule"])
        except ConfigError as exc:
            problems.add("metadata.schedule", str(exc))
    for path, message in windows.window_problems(metadata.get("window")):
        problems.add(f"metadata.window{path}", message)
    source = metadata.get("source")
    for message in windows.placeholder_problems(source.get("path") if isinstance(source, Mapping) else None):
        problems.add("metadata.source.path", message)
    _check_placeholder_places(problems, metadata, "metadata")


def _check_lineage(problems: _Problems, lineage: Any) -> None:
    """Check ``metadata.lineage``, a list of Iceberg blocks: the tables that a source was made from."""
    if lineage is None:
        return
    if not isinstance(lineage, (list, tuple)):
        problems.add("metadata.lineage", f"must be a list of Iceberg table blocks, got {type(lineage).__name__}")
        return
    for position, block in enumerate(lineage):
        if isinstance(block, Mapping) and str(block.get("format", "")).strip().upper() != "ICEBERG":
            problems.add(f"metadata.lineage[{position}].format", "must be ICEBERG, because a lineage names tables")
            continue
        _check_block(problems, block, f"lineage[{position}]")


def _check_docs(problems: _Problems, metadata: Mapping[str, Any], who: Any) -> None:
    """Check ``metadata.docs``, the contract of the table, with the owner that ``who`` gives when it names none."""
    for path, message in contracts.docs_problems(metadata.get("docs"), who=who):
        problems.add(f"metadata.docs{path}", message)


def _check_placeholder_places(problems: _Problems, value: Any, where: str) -> None:
    """A window placeholder works only in ``metadata.source.path``, so flag one anywhere else."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if f"{where}.{key}" != "metadata.source.path":
                _check_placeholder_places(problems, child, f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_placeholder_places(problems, child, f"{where}[{index}]")
    elif windows.mentions_placeholder(value):
        problems.add(where, "has a window placeholder, which works only in metadata.source.path; "
                            "to select the rows of a window, set metadata.window.column")


def _check_secrets(problems: _Problems, value: Any, where: str, key: str = "") -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _check_secrets(problems, child, f"{where}.{child_key}" if where else str(child_key), str(child_key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_secrets(problems, child, f"{where}[{index}]", key)
    elif isinstance(value, str):
        lowered = key.lower()
        named_secret = (any(word in lowered for word in _SECRET_WORDS) and not lowered.endswith(_REFERENCE_SUFFIXES)
                        and not where.startswith("metadata.docs"))  # a column called token is described, not a secret
        if (named_secret and value.strip()) or _INLINE_SECRET.search(value):
            problems.add(where, "looks like an inline secret; keep secrets out of specs and reference an "
                                "environment variable instead (e.g. 'token_env': 'MY_TOKEN')")


def validate_spec(data: Any) -> list[ConfigError]:
    """Check a spec and return every problem found, without raising.

    It reads the spec's ``kind`` (``Pipeline`` when it has none) and checks the spec as that kind. For a
    pipeline it checks the shape of the spec (top-level keys, ``apiVersion``, the source and
    target blocks by format, catalogs by type, write modes, join columns, partition
    transforms), builds every quality check with the same code a run uses, parses
    ``metadata.observability``, and rejects values that look like inline secrets
    (SaaS design section 10.3). It reads no files and creates nothing; whether a
    pipeline is registered for the route is checked by :func:`plan`.

    Args:
        data: A spec dict (e.g. ``json.load``-ed) or a :class:`~local_data_platform.Config`.

    Returns:
        One :class:`~local_data_platform.exceptions.ConfigError` per problem, each
        message starting with the JSON path of the offending value. Empty when valid.
    """
    try:
        spec = _spec_dict(data)
    except TypeError as exc:
        return [ConfigError(f"spec must be a JSON object: {exc}")]
    try:
        kind = kind_of(spec)
    except ConfigError as exc:
        return [ConfigError(f"kind: {exc}")]
    return _KINDS[kind].validate(spec)


def _validate_pipeline(spec: Mapping[str, Any]) -> list[ConfigError]:
    """The problems of a dataset config, which is the kind ``Pipeline``."""
    problems = _Problems()
    unknown = sorted(set(map(str, spec)) - set(TOP_LEVEL_KEYS))
    if unknown:
        problems.add("", f"unknown top-level keys {unknown}; allowed: {list(TOP_LEVEL_KEYS)}")
    if "apiVersion" in spec and spec["apiVersion"] != API_VERSION:
        problems.add("apiVersion", f"must be {API_VERSION!r}, got {spec['apiVersion']!r}")
    _check_string(problems, spec, "identifier", "", required=True)
    for key in ("who", "what", "where", "when", "how"):
        if key in spec and not isinstance(spec[key], str):
            problems.add(key, f"must be a string, got {type(spec[key]).__name__}")
    metadata = spec.get("metadata")
    if not isinstance(metadata, Mapping):
        problems.add("metadata", "is required and must be an object with 'source' and 'target'")
    else:
        _check_block(problems, metadata.get("source"), "source")
        _check_block(problems, metadata.get("target"), "target")
        _check_quality(problems, metadata.get("quality"))
        _check_schedule_and_window(problems, metadata)
        _check_docs(problems, metadata, spec.get("who"))
        _check_lineage(problems, metadata.get("lineage"))
        if "retries" in metadata:
            for path, message in retries.retries_problems(metadata["retries"]):
                problems.add(f"metadata.retries{path}", message)
        from local_data_platform.events import sink_specs

        try:
            sink_specs(metadata)
        except ConfigError as exc:
            problems.add("", str(exc))
    _check_secrets(problems, spec, "")
    return problems.errors


WORKFLOW_KEYS = ("apiVersion", "kind", "identifier", "who", "what", "where", "when", "how", "metadata", "tasks")
"""The top-level keys of a ``Workflow`` file."""


def _validate_workflow(spec: Mapping[str, Any]) -> list[ConfigError]:
    """The problems of a workflow file, which is the kind ``Workflow``.

    The checks are in :mod:`local_data_platform.orchestration.workflow`, which is imported when it is needed, so that
    this module stays light.
    """
    from local_data_platform.orchestration.workflow import validate_workflow

    return validate_workflow(spec)


def _workflow_schema() -> dict[str, Any]:
    from local_data_platform.orchestration.workflow import workflow_schema

    return workflow_schema()


def _describe_workflow(spec: Mapping[str, Any]) -> dict[str, Any]:
    from local_data_platform.orchestration.workflow import describe_workflow

    return describe_workflow(spec)


def _validate_metrics(spec: Mapping[str, Any]) -> list[ConfigError]:
    """The problems of a metrics file, which is the kind ``Metrics``.

    The checks are in :mod:`local_data_platform.metrics.model`, which is imported when it is needed. They read no table.
    """
    from local_data_platform.metrics.model import validate_metrics

    return validate_metrics(spec)


def _metrics_schema() -> dict[str, Any]:
    from local_data_platform.metrics.model import metrics_schema

    return metrics_schema()


def _describe_metrics(spec: Mapping[str, Any]) -> dict[str, Any]:
    from local_data_platform.metrics.model import describe_metrics

    return describe_metrics(spec)


# ---------------------------------------------------------------------- plan


def _load_json(path: Path) -> tuple[Any, list[ConfigError]]:
    if not path.is_file():
        return None, [ConfigError(f"config file not found: {path}")]
    try:
        return json.loads(path.read_text(encoding="utf-8")), []
    except json.JSONDecodeError as exc:
        return None, [ConfigError(f"config file {path} is not valid JSON: {exc}")]


def plan(config: Any, *, window: Any = None, base_dir: str | os.PathLike | None = None) -> dict[str, Any]:
    """Validate a spec and describe what running it would do, without running anything.

    Args:
        config: A config file path, a spec dict or a :class:`~local_data_platform.Config`.
        window: A logical window; when given, the plan includes its idempotency key.
        base_dir: Folder relative paths resolve against (default: the file's folder,
            or the config's ``base_dir``).

    Returns:
        ``{"config", "identifier", "kind", "valid", "errors", "notes", "api_version", "spec_hash",
        "route", "pipeline", "target", "write_mode", "catalog_type", "checks",
        "on_failure", "sinks", "schedule", "schedule_from", "window_size", "window_column", "timezone",
        "source_path", "source_path_template", "window", "idempotency_key"}``. ``errors`` lists
        messages and ``notes`` lists things worth knowing that are not errors, such as a ``when`` text that is
        no schedule. ``schedule`` is the config's ``metadata.schedule`` or the one its ``when`` means (and
        ``schedule_from`` says which); ``window_size``, ``window_column`` and ``timezone`` come from
        ``metadata.window``; ``source_path`` is the source path, filled in for ``window`` when it has window
        placeholders, and ``source_path_template`` is the path as written when it has some. The other fields are
        ``None`` when they can't be worked out. ``window`` may be one of the shortcuts ``yesterday``, ``last-week``
        and ``last-month``, counted in the config's time zone. A file of another kind than ``Pipeline`` has no
        route, target or hash, and the kind adds what it reports, such as the ``tasks`` of a ``Workflow``.
    """
    label = None
    if isinstance(config, (str, os.PathLike)):
        from local_data_platform.paths import resolve_path

        path = resolve_path(config)
        label = str(path)
        data, errors = _load_json(path)
        base = Path(base_dir) if base_dir is not None else path.parent
    else:
        data, errors = config, []
        base = _base_dir(config, base_dir)
    result: dict[str, Any] = {
        "config": label, "identifier": None, "kind": None, "valid": False, "errors": [], "notes": [],
        "api_version": API_VERSION,
        "spec_hash": None, "route": None, "pipeline": None, "target": None, "write_mode": None,
        "catalog_type": None, "checks": 0, "on_failure": None, "sinks": [], "schedule": None,
        "schedule_from": None, "window_size": None, "window_column": None, "timezone": None, "source_path": None,
        "source_path_template": None, "window": None, "idempotency_key": None,
    }
    if not errors:
        errors = validate_spec(data)
    if isinstance(data, Mapping) or hasattr(data, "metadata"):
        spec = _spec_dict(data)
        result["identifier"] = spec.get("identifier")
        result["api_version"] = spec.get("apiVersion") or API_VERSION
        try:
            result["kind"] = kind_of(spec)
        except ConfigError:
            pass  # validate_spec reports it, and the kind stays None
    if not errors:
        kind = _KINDS[result["kind"]]
        if kind.name == DEFAULT_KIND:
            errors = _describe(result, _spec_dict(data), base, window)
        elif kind.describe is not None:
            result.update(kind.describe(_spec_dict(data)))
    result["errors"] = [str(error) for error in errors]
    result["valid"] = not errors
    return result


def _describe(result: dict[str, Any], spec: Mapping[str, Any], base: Path | None, window: Any) -> list[ConfigError]:
    from local_data_platform.events import sink_specs
    from local_data_platform.pipeline.registry import get_pipeline_class, make_route

    metadata = spec["metadata"]
    source, target = metadata["source"], metadata["target"]
    result["spec_hash"] = spec_hash(spec, base_dir=base)
    route = make_route(source["format"], target["format"], source.get("engine"))
    result["route"] = str(route)
    errors: list[ConfigError] = []
    try:
        result["pipeline"] = get_pipeline_class(*route).__name__
    except PipelineNotFound as exc:
        errors.append(ConfigError(str(exc)))
    window_error: ConfigError | None = None
    bounds = None
    try:
        bounds = window_bounds(spec, window)
    except ConfigError as exc:
        window_error = exc
    where = _describe_source_path(result, spec, base, bounds, route.source_format in ("DUCKDB", "XLSX"))
    if route.source_format == "DUCKDB" and where is not None:
        database = where
        if not database.is_file():
            errors.append(ConfigError(f"metadata.source.path: the DuckDB file {database} does not exist"))
    if route.source_format == "XLSX" and where is not None:
        workbook = where
        if not workbook.is_file():
            errors.append(ConfigError(f"metadata.source.path: the Excel file {workbook} does not exist"))
    if route.source_format == "DUCKDB":
        errors.extend(_describe_duckdb_files(source, base))
    if str(target["format"]).strip().upper() == "ICEBERG":
        catalog = target["catalog"]
        namespace = catalog.get("namespace") or catalog.get("identifier")
        result["target"] = f"{namespace}.{target['name']}"
        result["write_mode"] = str(target.get("write_mode") or "append").lower()
        result["catalog_type"] = str(catalog.get("type") or "local")
    else:
        result["target"] = target.get("path")
        result["write_mode"] = "overwrite"
    quality = _normalise_quality(metadata.get("quality")) or {"on_failure": "fail", "checks": []}
    result["checks"] = len(quality["checks"])
    result["on_failure"] = quality["on_failure"]
    result["sinks"] = [item["type"] for item in sink_specs(metadata)]
    errors.extend(_describe_schedule(result, spec))
    if window_error is not None:
        errors.append(window_error)
    elif bounds is not None:
        result["window"] = f"{format_instant(bounds[0])}/{format_instant(bounds[1])}"
        result["idempotency_key"] = idempotency_key(spec, bounds, base_dir=base)
    return errors


def _describe_duckdb_files(source: Mapping[str, Any], base: Path | None) -> list[ConfigError]:
    """The input configs and the query file of a DuckDB source that are not there."""
    from local_data_platform.paths import resolve_path

    errors = []
    inputs = source.get("inputs")
    for position, item in enumerate(inputs if isinstance(inputs, (list, tuple)) else ()):
        if isinstance(item, str) and not resolve_path(item, base).is_file():
            errors.append(ConfigError(f"metadata.source.inputs[{position}]: the config file "
                                      f"{resolve_path(item, base)} does not exist"))
    query_file = source.get("query_file")
    if isinstance(query_file, str) and not resolve_path(query_file, base).is_file():
        errors.append(ConfigError(f"metadata.source.query_file: the file {resolve_path(query_file, base)} "
                                  "does not exist"))
    return errors


def _describe_source_path(result: dict[str, Any], spec: Mapping[str, Any], base: Path | None,
                          bounds: tuple[dt.datetime, dt.datetime] | None, must_exist: bool) -> Path | None:
    """Fill in ``source_path`` and return the resolved local path to check, or ``None`` when there is none to check.

    A plain path is resolved only when the source must exist (a DuckDB file or a workbook). A path with window
    placeholders is resolved for the window when there is one. Without a window it is shown as written, there is no
    file to check, and a note says so.
    """
    from local_data_platform.paths import resolve_path

    path = spec["metadata"]["source"].get("path")
    result["source_path"] = path
    if not isinstance(path, str):
        return None
    if not windows.placeholders(path):
        return resolve_path(path, base) if must_exist else None
    result["source_path_template"] = path
    if bounds is None:
        result["notes"].append("the source path has a window placeholder, so it is shown as written; "
                               "pass --window to see the path of one window")
        return None
    result["source_path"] = windows.resolve_placeholders(path, bounds, windows.timezone_of(spec["metadata"]))
    if "://" in result["source_path"]:
        return None
    location = resolve_path(result["source_path"], base)
    if not location.exists():
        result["notes"].append(f"the source file {location} does not exist yet")
    return location


def _describe_schedule(result: dict[str, Any], spec: Mapping[str, Any]) -> list[ConfigError]:
    """Fill in the schedule and the window declaration, and return the one error a schedule can cause."""
    metadata, when = spec["metadata"], spec.get("when")
    schedule = windows.effective_schedule(metadata, when)
    if schedule is not None:
        result["schedule"], result["schedule_from"] = schedule.expression, schedule.source
    note = windows.ignored_when(metadata, when)
    if note:
        result["notes"].append(note)
    declared = windows.parse_window_spec(metadata.get("window"))
    if declared is not None:
        result["window_size"], result["window_column"], result["timezone"] = (
            declared.size, declared.column, declared.timezone)
    if windows.lacks_window_selection(metadata, when):
        return [ConfigError(
            "metadata: this config has a schedule and appends, but nothing selects the rows of a window, so every "
            "window would load the same rows again. Put {window_start:FORMAT} in metadata.source.path, set "
            "metadata.window.column, or change write_mode")]
    templated = bool(windows.placeholders(metadata["source"].get("path") or ""))
    has_column = declared is not None and declared.column is not None
    if schedule is not None and templated and not has_column and result["write_mode"] == "overwrite":
        result["notes"].append("write_mode overwrite replaces the whole table with the rows of each window; "
                               "set metadata.window.column to replace only the rows of the window")
    return []


# ---------------------------------------------------------------------- CLI: ldp schema, ldp plan


def _cmd_schema(args: argparse.Namespace) -> int:
    print(json.dumps(kind_schema(getattr(args, "kind", None) or DEFAULT_KIND), indent=2), file=sys.stdout)
    return 0


def _plan_targets(target: str) -> list[Path]:
    from local_data_platform.paths import resolve_path

    path = resolve_path(target)
    if path.is_dir():
        return sorted(item for item in path.glob("*.json") if item.is_file())
    return [path]


def _cmd_plan(args: argparse.Namespace) -> int:
    paths = _plan_targets(args.config)
    if not paths:
        raise ConfigError(f"no *.json configs in {args.config}")
    plans = [plan(path, window=args.window) for path in paths]
    if args.json:
        print(json.dumps(plans[0] if len(plans) == 1 and not Path(args.config).is_dir() else plans, indent=2),
              file=sys.stdout)
        return 0 if all(item["valid"] for item in plans) else 1
    if len(plans) == 1 and not Path(args.config).is_dir():
        _print_plan(plans[0])
    else:
        from local_data_platform.cli import format_table

        rows = [{"config": Path(item["config"]).name, "kind": item["kind"], "identifier": item["identifier"],
                 "valid": item["valid"], "route": item["route"], "pipeline": item["pipeline"],
                 "target": item["target"], "spec_hash": (item["spec_hash"] or "")[:12] or None}
                for item in plans]
        print(format_table(rows), file=sys.stdout)
        for item in plans:
            for error in item["errors"]:
                print(f"{Path(item['config']).name}: {error}", file=sys.stderr)
    return 0 if all(item["valid"] for item in plans) else 1


def _print_plan(item: Mapping[str, Any]) -> None:
    if not item["valid"]:
        count = len(item["errors"])
        print(f"{item['config']}: {count} problem{'s' if count != 1 else ''}:", file=sys.stderr)
        for error in item["errors"]:
            print(f"  - {error}", file=sys.stderr)
        return
    if item["kind"] != DEFAULT_KIND:
        lines = [("config", item["config"]), ("identifier", item["identifier"]), ("kind", item["kind"]),
                 ("apiVersion", item["api_version"])]
        if "tasks" in item:
            lines.append(("tasks", ", ".join(item["tasks"])))
        for key in ("semantic_models", "metrics"):
            if key in item:
                lines.append((key, ", ".join(item[key])))
        width = max(len(name) for name, _ in lines)
        for name, value in lines:
            print(f"{name.ljust(width)}  {value}", file=sys.stdout)
        return
    lines = [
        ("config", item["config"]),
        ("identifier", item["identifier"]),
        ("apiVersion", item["api_version"]),
        ("spec_hash", item["spec_hash"]),
        ("route", f"{item['route']} [{item['pipeline']}]"),
        ("target", item["target"]),
        ("write_mode", item["write_mode"]),
    ]
    if item["catalog_type"]:
        lines.append(("catalog", item["catalog_type"]))
    lines.append(("quality", f"{item['checks']} checks, on_failure={item['on_failure']}"))
    lines.append(("sinks", ", ".join(item["sinks"]) or "none (events are dropped)"))
    if item["schedule"]:
        lines.append(("schedule", f"{item['schedule']} (from {item['schedule_from']})"))
    if item["window_size"]:
        lines.append(("window_size", item["window_size"]))
        if item["window_column"]:
            lines.append(("window_column", item["window_column"]))
        lines.append(("timezone", item["timezone"]))
    if item["source_path_template"]:
        lines.append(("source_path", item["source_path"]))
    if item["window"]:
        lines += [("window", item["window"]), ("idempotency_key", item["idempotency_key"])]
    width = max(len(name) for name, _ in lines)
    for name, value in lines:
        print(f"{name.ljust(width)}  {value}", file=sys.stdout)
    for note in item["notes"]:
        print(f"note: {note}", file=sys.stdout)


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> None:
    """Register ``ldp schema`` and ``ldp plan`` on an ``argparse`` subparsers object.

    Args:
        subparsers: What ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (e.g. the CLI's ``-v``).
    """
    schema = subparsers.add_parser(
        "schema", parents=list(parents), help=f"print the {API_VERSION} JSON Schema",
        description=f"Print the JSON Schema of an {API_VERSION} file. With no --kind it is the schema of a dataset "
                    f"config, which is the kind {DEFAULT_KIND}.")
    schema.add_argument("--kind", metavar="KIND", default=DEFAULT_KIND,
                        help=f"the kind of file: {', '.join(registered_kinds())} (default {DEFAULT_KIND})")
    schema.set_defaults(handler=_cmd_schema)

    plan_parser = subparsers.add_parser(
        "plan", parents=list(parents), help="validate a config and show what a run would do",
        description="Validate CONFIG (a JSON dataset config, or a folder of them) without running it, then print "
                    "its spec hash, route, pipeline, target and sinks. Each file is validated as its kind, so a "
                    "workflow file in the folder is checked as a workflow. Exits with 1 if any file is invalid.")
    plan_parser.add_argument("config", metavar="CONFIG", help="a JSON dataset config, or a folder of *.json files")
    plan_parser.add_argument("--window", metavar="WINDOW",
                             help="a logical window: an ISO-8601 interval START/END, or yesterday, last-week or "
                                  "last-month in the time zone of the config. Prints its idempotency key, and the "
                                  "source path of the window")
    plan_parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    plan_parser.set_defaults(handler=_cmd_plan)


register_kind(DEFAULT_KIND, validate=_validate_pipeline, json_schema=json_schema)
register_kind("Workflow", validate=_validate_workflow, json_schema=_workflow_schema, describe=_describe_workflow)
register_kind("Metrics", validate=_validate_metrics, json_schema=_metrics_schema, describe=_describe_metrics)

__all__ = [
    "API_VERSION",
    "DEFAULT_KIND",
    "FORMATS",
    "HASH_EXCLUDED_METADATA",
    "RESERVED_KINDS",
    "SpecKind",
    "TOP_LEVEL_KEYS",
    "WORKFLOW_KEYS",
    "WRITE_MODES",
    "add_cli",
    "canonical_json",
    "canonical_spec",
    "format_instant",
    "idempotency_key",
    "json_schema",
    "kind_of",
    "kind_of_file",
    "kind_schema",
    "parse_window",
    "plan",
    "register_kind",
    "registered_kinds",
    "replace_key",
    "spec_hash",
    "target_identity",
    "validate_spec",
    "window_bounds",
]
