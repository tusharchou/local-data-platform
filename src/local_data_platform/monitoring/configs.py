"""The dataset configs of a path, for the commands that look at many tables: ``ldp monitor`` and ``ldp status``.

A path is one config file or a folder. A folder gives its dataset configs, which are the ``.json`` files that are of the
kind ``Pipeline`` or of no kind at all, so a workflow file or a metrics file that lives next to them is left alone. A
file that cannot be read is reported with the reason and does not hide the others.

This module needs only the standard library when it is imported.
"""

import json
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError


def is_a_config(file: Path) -> bool:
    """Whether a JSON file of a folder is a dataset config, so that a file of another kind or sort is left alone.

    A file that cannot be read as JSON counts as a config, so that it is reported. A table must not leave the monitors
    without a word because of a typo.
    """
    from local_data_platform.spec import DEFAULT_KIND, kind_of_file

    if kind_of_file(file) not in (DEFAULT_KIND, None):
        return False
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return isinstance(data, dict) and "identifier" in data and "metadata" in data


def load_configs(target: str | Path) -> tuple[list[tuple[Path, Any]], list[tuple[Path, str]]]:
    """The dataset configs of a file or a folder, and the files that could not be read as one.

    Args:
        target: A config file, or a folder of them.

    Returns:
        ``(configs, failures)``. ``configs`` is ``(file, Config)`` in the order of the file names, and ``failures`` is
        ``(file, message)`` for each file that is not a valid config.

    Raises:
        ConfigError: If the path is not there, or a folder has no dataset config.
    """
    from local_data_platform.config import Config
    from local_data_platform.paths import resolve_path

    path = resolve_path(target)
    if path.is_dir():
        files = [file for file in sorted(path.glob("*.json")) if file.is_file() and is_a_config(file)]
        if not files:
            raise ConfigError(f"no dataset configs in {path}")
    elif path.is_file():
        files = [path]
    else:
        raise ConfigError(f"config path not found: {path}")
    configs, failures = [], []
    for file in files:
        try:
            configs.append((file, Config.from_json(file)))
        except ConfigError as error:
            failures.append((file, str(error)))
    return configs, failures


__all__ = [
    "is_a_config",
    "load_configs",
]
