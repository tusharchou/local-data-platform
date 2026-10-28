"""Dataset configuration.

A config describes one dataset with the 4W1H questions (who, what, where, when, how)
plus a ``metadata`` block holding ``source``, ``target`` and optional ``quality``, ``docs`` and ``sla``
sections. See ``docs/design/v0_1_1.md`` for the full schema.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contracts
from .exceptions import ConfigError
from .monitoring import sla as sla_settings
from .orchestration import retries, windows
from .paths import resolve_path
from .spec import API_VERSION, DEFAULT_KIND, kind_of

REQUIRED_METADATA_KEYS = ("source", "target")


@dataclass
class Config:
    """A dataset config.

    ``base_dir`` is the folder relative paths resolve against. ``Config.from_json``
    sets it to the config file's folder; when built by hand it defaults to the cwd.
    ``api_version`` is the config's ``apiVersion``, when it has one, and ``kind`` is
    ``Pipeline``, the only kind that is a dataset config.
    """

    identifier: str
    who: str = ""
    what: str = ""
    where: str = ""
    when: str = ""
    how: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    base_dir: Path | None = None
    api_version: str | None = None
    kind: str = DEFAULT_KIND

    @classmethod
    def from_dict(cls, data: dict[str, Any], base_dir: str | os.PathLike | None = None) -> "Config":
        """Build a config from a dict.

        Raises:
            ConfigError: If the dict is not a dataset config. That includes a file of another kind, such as a
                ``Workflow``, a ``kind`` that is not written exactly as the registry has it, an ``apiVersion``
                other than ``ldp/v1``, unknown top-level keys, and a missing or incomplete ``metadata``.
        """
        if not isinstance(data, dict):
            raise ConfigError(f"config must be a JSON object, got {type(data).__name__}")
        if "identifier" not in data:
            raise ConfigError("config is missing required key 'identifier'")
        kind = kind_of(data)
        if kind != DEFAULT_KIND:
            raise ConfigError(f"{data['identifier']!r} is a {kind}, not a dataset config (kind {DEFAULT_KIND})")
        known = {"identifier", "who", "what", "where", "when", "how", "metadata", "apiVersion", "kind"}
        unknown = set(data) - known
        if unknown:
            raise ConfigError(f"config has unknown top-level keys: {sorted(unknown)}")
        api_version = data.get("apiVersion")
        if api_version is not None and api_version != API_VERSION:
            raise ConfigError(f"config apiVersion must be {API_VERSION!r}, got {api_version!r}")
        fields = {key: value for key, value in data.items() if key not in ("apiVersion", "kind")}
        config = cls(**fields, base_dir=Path(base_dir) if base_dir else None, api_version=api_version, kind=kind)
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
        """Raise :class:`ConfigError` if the metadata block is incomplete, or its schedule or window is invalid."""
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
        if self.metadata.get("schedule") is not None:
            try:
                windows.parse_schedule(self.metadata["schedule"])
            except ConfigError as exc:
                raise ConfigError(f"metadata.schedule: {exc}") from None
        windows.parse_window_spec(self.metadata.get("window"))
        if "retries" in self.metadata:
            for path, message in retries.retries_problems(self.metadata["retries"]):
                raise ConfigError(f"metadata.retries{path}: {message}")
        for path, message in contracts.docs_problems(self.metadata.get("docs"), who=self.who):
            raise ConfigError(f"metadata.docs{path}: {message}")
        for path, message in sla_settings.sla_problems(self.metadata.get("sla")):
            raise ConfigError(f"metadata.sla{path}: {message}")

    @property
    def source(self) -> dict[str, Any]:
        return self.metadata["source"]

    @property
    def target(self) -> dict[str, Any]:
        return self.metadata["target"]

    @property
    def schedule(self) -> windows.Schedule | None:
        """When the config runs: its ``metadata.schedule``, else the schedule its ``when`` text means, else ``None``.

        A ``when`` of ``hourly``, ``daily``, ``weekly`` or ``monthly`` means ``@hourly``, ``@daily``, ``@weekly`` or
        ``@monthly``, and any other ``when`` text is a label that nothing reads. ``Schedule.source`` says which of the
        two the schedule came from.
        """
        return windows.effective_schedule(self.metadata, self.when)

    @property
    def window(self) -> windows.WindowSpec | None:
        """The ``metadata.window`` declaration: the size of a window, its column and its time zone.

        It is ``None`` when the config has no ``window`` or sets it to ``"none"``.
        """
        return windows.parse_window_spec(self.metadata.get("window"))

    @property
    def docs(self) -> contracts.Docs | None:
        """The contract of the table: its ``metadata.docs`` block, normalised, or ``None`` when there is none.

        ``owner`` is the config's ``who`` when the block names no owner, and ``refresh`` is its ``when`` when the block
        names no refresh. See :mod:`local_data_platform.contracts`.
        """
        return contracts.parse_docs(self.metadata.get("docs"), who=self.who, when=self.when)

    @property
    def sla(self) -> sla_settings.Sla | None:
        """How fresh, how big and how stable the table should be: its ``metadata.sla`` block, normalised.

        The defaults are applied, so ``volume.lookback_runs`` is 7 and ``schema.on_change`` is ``warn`` when the block
        sets none, and it is ``None`` when the config has no block. See :mod:`local_data_platform.monitoring.sla`.
        """
        return sla_settings.parse_sla(self.metadata.get("sla"))

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
