"""The ``ldp/v1`` dataset spec: its JSON Schema, validation, hashing and idempotency keys.

A spec is today's dataset config (see ``docs/design/v0_1_1.md``) with an optional
``apiVersion: "ldp/v1"`` and an optional ``metadata.observability`` block::

    from local_data_platform.spec import idempotency_key, spec_hash, validate_spec

    problems = validate_spec(data)           # every problem at once, as ConfigError objects
    digest = spec_hash(config)               # stable over key order, whitespace and checkouts
    key = idempotency_key(config, ("2026-09-01", "2026-09-02"))

``spec_hash`` is the sha256 of the canonical JSON of the pipeline-relevant fields:
``apiVersion``, ``identifier``, ``when``, ``how`` and ``metadata`` except
``observability`` (where events go doesn't change what a run does). Keys are
sorted, there is no whitespace, formats are upper-case, write modes lower-case, the
quality block is normalised to ``{"on_failure", "checks"}``, and local paths are
made relative to the config's folder. The descriptive ``who``, ``what`` and
``where`` are left out.

``idempotency_key`` follows SaaS design section 7.3: it hashes the pipeline, the
target table and the logical window, never the spec hash, so redeploying a spec and
re-running a window finds the earlier commit instead of appending twice.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePath
from typing import Any

from local_data_platform.exceptions import ConfigError, PipelineNotFound

API_VERSION = "ldp/v1"
"""The spec version this library reads and writes."""

TOP_LEVEL_KEYS = ("apiVersion", "identifier", "who", "what", "where", "when", "how", "metadata")
FORMATS = ("CSV", "PARQUET", "JSON", "ICEBERG")
WRITE_MODES = ("append", "overwrite", "upsert")
ON_FAILURE = ("fail", "warn")
HASH_EXCLUDED_METADATA = ("observability",)
"""``metadata`` keys that don't change what a run does, so :func:`spec_hash` ignores them."""

_BUILTIN_CATALOG_TYPES = ("local", "sql", "rest", "glue")
_CATALOG_ALIASES = {"localiceberg": "local"}
_TRANSFORM = re.compile(r"^(identity|year|month|day|hour|(bucket|truncate)\[\s*[1-9]\d*\s*\])$", re.IGNORECASE)
_PATH_KEYS = {"path", "warehouse_path"}
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
        normalisations described in the module docstring.
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


def parse_window(window: Any) -> tuple[dt.datetime, dt.datetime] | None:
    """Parse a logical window into two UTC datetimes.

    Args:
        window: ``None``; a ``"START/END"`` ISO-8601 interval string; a
            ``(start, end)`` pair; or ``{"start", "end"}``. Each bound is a datetime,
            a date (midnight) or an ISO string. Naive values are read as UTC.

    Returns:
        ``(start, end)`` in UTC, or ``None`` for no window.

    Raises:
        ConfigError: If the window is malformed or ``end`` is not after ``start``.
    """
    if window is None:
        return None
    if isinstance(window, str):
        parts = window.split("/")
        if len(parts) != 2:
            raise ConfigError(f"window {window!r} must be an ISO-8601 interval 'START/END'")
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
        window: The logical window, in any form :func:`parse_window` accepts, or
            ``None`` for a run that owns the whole table.
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
    bounds = parse_window(window)
    start, end = (format_instant(bounds[0]), format_instant(bounds[1])) if bounds else ("", "")
    return hashlib.sha256(canonical_json([API_VERSION, "idempotency", pipeline, target, start, end])).hexdigest()


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
                    "source": {"$ref": "#/$defs/block"},
                    "target": {"$ref": "#/$defs/target"},
                    "quality": {"$ref": "#/$defs/quality"},
                    "observability": {"$ref": "#/$defs/observability"},
                },
            },
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
                },
                "allOf": [
                    {"if": {"properties": {"format": {"enum": ["ICEBERG", "iceberg"]}}, "required": ["format"]},
                     "then": {"required": ["catalog"]},
                     "else": {"required": ["path"]}},
                ],
            },
            "target": {
                "allOf": [{"$ref": "#/$defs/block"}],
                "properties": {
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
    _check_string(problems, block, "path", where)
    if fmt == "JSON" and str(block.get("engine", "")).strip().upper() == "BIGQUERY":
        credentials = block.get("credentials")
        if credentials is not None:
            if not isinstance(credentials, Mapping):
                problems.add(f"{where}.credentials", "must be an object with a 'path' to a key file")
            else:
                _check_string(problems, credentials, "path", f"{where}.credentials")


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


def _check_secrets(problems: _Problems, value: Any, where: str, key: str = "") -> None:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _check_secrets(problems, child, f"{where}.{child_key}" if where else str(child_key), str(child_key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _check_secrets(problems, child, f"{where}[{index}]", key)
    elif isinstance(value, str):
        lowered = key.lower()
        named_secret = any(word in lowered for word in _SECRET_WORDS) and not lowered.endswith(_REFERENCE_SUFFIXES)
        if (named_secret and value.strip()) or _INLINE_SECRET.search(value):
            problems.add(where, "looks like an inline secret; keep secrets out of specs and reference an "
                                "environment variable instead (e.g. 'token_env': 'MY_TOKEN')")


def validate_spec(data: Any) -> list[ConfigError]:
    """Check a spec and return every problem found, without raising.

    It checks the shape of the spec (top-level keys, ``apiVersion``, the source and
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
    problems = _Problems()
    try:
        spec = _spec_dict(data)
    except TypeError as exc:
        return [ConfigError(f"spec must be a JSON object: {exc}")]
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
        from local_data_platform.events import sink_specs

        try:
            sink_specs(metadata)
        except ConfigError as exc:
            problems.add("", str(exc))
    _check_secrets(problems, spec, "")
    return problems.errors


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
        ``{"config", "identifier", "valid", "errors", "api_version", "spec_hash",
        "route", "pipeline", "target", "write_mode", "catalog_type", "checks",
        "on_failure", "sinks", "window", "idempotency_key"}``. ``errors`` lists
        messages; the other fields are ``None`` when they can't be worked out.
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
        "config": label, "identifier": None, "valid": False, "errors": [], "api_version": API_VERSION,
        "spec_hash": None, "route": None, "pipeline": None, "target": None, "write_mode": None,
        "catalog_type": None, "checks": 0, "on_failure": None, "sinks": [], "window": None, "idempotency_key": None,
    }
    if not errors:
        errors = validate_spec(data)
    if isinstance(data, Mapping) or hasattr(data, "metadata"):
        spec = _spec_dict(data)
        result["identifier"] = spec.get("identifier")
        result["api_version"] = spec.get("apiVersion") or API_VERSION
    if not errors:
        errors = _describe(result, _spec_dict(data), base, window)
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
    try:
        bounds = parse_window(window)
        if bounds is not None:
            result["window"] = f"{format_instant(bounds[0])}/{format_instant(bounds[1])}"
            result["idempotency_key"] = idempotency_key(spec, bounds, base_dir=base)
    except ConfigError as exc:
        errors.append(exc)
    return errors


# ---------------------------------------------------------------------- CLI: ldp schema, ldp plan


def _cmd_schema(args: argparse.Namespace) -> int:
    print(json.dumps(json_schema(), indent=2), file=sys.stdout)
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

        rows = [{"config": Path(item["config"]).name, "identifier": item["identifier"],
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
    if item["window"]:
        lines += [("window", item["window"]), ("idempotency_key", item["idempotency_key"])]
    width = max(len(name) for name, _ in lines)
    for name, value in lines:
        print(f"{name.ljust(width)}  {value}", file=sys.stdout)


def add_cli(subparsers: Any, parents: Sequence[argparse.ArgumentParser] = ()) -> None:
    """Register ``ldp schema`` and ``ldp plan`` on an ``argparse`` subparsers object.

    Args:
        subparsers: What ``ArgumentParser.add_subparsers()`` returned.
        parents: Parent parsers to inherit options from (e.g. the CLI's ``-v``).
    """
    schema = subparsers.add_parser("schema", parents=list(parents), help=f"print the {API_VERSION} JSON Schema",
                                   description=f"Print the JSON Schema of an {API_VERSION} dataset spec.")
    schema.set_defaults(handler=_cmd_schema)

    plan_parser = subparsers.add_parser(
        "plan", parents=list(parents), help="validate a config and show what a run would do",
        description="Validate CONFIG (a JSON dataset config, or a folder of them) without running it, then print "
                    "its spec hash, route, pipeline, target and sinks. Exits with 1 if any config is invalid.")
    plan_parser.add_argument("config", metavar="CONFIG", help="a JSON dataset config, or a folder of *.json configs")
    plan_parser.add_argument("--window", metavar="START/END",
                             help="a logical window (ISO-8601 interval); prints its idempotency key")
    plan_parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    plan_parser.set_defaults(handler=_cmd_plan)


__all__ = [
    "API_VERSION",
    "FORMATS",
    "HASH_EXCLUDED_METADATA",
    "TOP_LEVEL_KEYS",
    "WRITE_MODES",
    "add_cli",
    "canonical_json",
    "canonical_spec",
    "format_instant",
    "idempotency_key",
    "json_schema",
    "parse_window",
    "plan",
    "spec_hash",
    "target_identity",
    "validate_spec",
]
