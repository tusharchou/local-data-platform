"""Dataset configuration.

A config describes one dataset with the 4W1H questions (who, what, where, when, how)
plus a ``metadata`` block holding ``source``, ``target`` and optional ``quality``
sections. See ``docs/design/v0_1_1.md`` for the full schema.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ConfigError
from .paths import resolve_path

REQUIRED_METADATA_KEYS = ("source", "target")


@dataclass
class Config:
    """A dataset config.

    ``base_dir`` is the folder relative paths resolve against. ``Config.from_json``
    sets it to the config file's folder; when built by hand it defaults to the cwd.
    """

    identifier: str
    who: str = ""
    what: str = ""
    where: str = ""
    when: str = ""
    how: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    base_dir: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: str | os.PathLike | None = None) -> "Config":
        if not isinstance(data, dict):
            raise ConfigError(f"config must be a JSON object, got {type(data).__name__}")
        if "identifier" not in data:
            raise ConfigError("config is missing required key 'identifier'")
        known = {"identifier", "who", "what", "where", "when", "how", "metadata"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"config has unknown top-level keys: {sorted(unknown)}")
        config = cls(**data, base_dir=Path(base_dir) if base_dir else None)
        config.validate()
        return config

    @classmethod
    def from_json(cls, path: str | os.PathLike) -> "Config":
        resolved = resolve_path(path)
        if not resolved.is_file():
            raise ConfigError(f"config file not found: {resolved}")
        try:
            data = json.loads(resolved.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config file {resolved} is not valid JSON: {exc}") from exc
        return cls.from_dict(data, base_dir=resolved.parent)

    def validate(self) -> None:
        """Raise :class:`ConfigError` if the metadata block is incomplete."""
        if not isinstance(self.metadata, dict):
            raise ConfigError("config 'metadata' must be an object")
        for key in REQUIRED_METADATA_KEYS:
            if key not in self.metadata:
                raise ConfigError(f"config metadata is missing required key '{key}'")
            if not isinstance(self.metadata[key], dict):
                raise ConfigError(f"config metadata '{key}' must be an object")
        for key in REQUIRED_METADATA_KEYS:
            if "format" not in self.metadata[key]:
                raise ConfigError(f"config metadata '{key}' is missing 'format'")

    @property
    def source(self) -> dict[str, Any]:
        return self.metadata["source"]

    @property
    def target(self) -> dict[str, Any]:
        return self.metadata["target"]

    @property
    def quality(self) -> dict[str, Any]:
        """The quality block, normalised to ``{"on_failure": "fail" | "warn", "checks": [...]}``.

        ``metadata["quality"]`` may be a list of checks (fail on failure) or a dict with
        ``on_failure`` and ``checks`` keys.
        """
        raw = self.metadata.get("quality")
        if raw is None:
            return {"on_failure": "fail", "checks": []}
        if isinstance(raw, list):
            return {"on_failure": "fail", "checks": list(raw)}
        if isinstance(raw, dict):
            on_failure = raw.get("on_failure", "fail")
            if on_failure not in ("fail", "warn"):
                raise ConfigError(f"quality.on_failure must be 'fail' or 'warn', got {on_failure!r}")
            checks = raw.get("checks", [])
            if not isinstance(checks, list):
                raise ConfigError("quality.checks must be a list")
            return {"on_failure": on_failure, "checks": list(checks)}
        raise ConfigError("config metadata 'quality' must be a list or an object")

    def resolve(self, path: str | os.PathLike) -> Path:
        """Resolve a path from this config against its ``base_dir``."""
        return resolve_path(path, self.base_dir)
