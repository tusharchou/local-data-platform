"""Finding the metrics files of a folder and reading them into one set of definitions.

A folder of configs may hold one metrics file or several. Each file stands on its own, so a metric refers only to
measures of its own file. The files are put together here, and a name that two files define is a problem, because
``ldp metrics query revenue`` has to mean one thing.
"""

from __future__ import annotations

import difflib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError
from local_data_platform.metrics.model import KIND, Metric, SemanticModel, build
from local_data_platform.paths import resolve_path
from local_data_platform.spec import kind_of_file, validate_spec

NO_FILES = "there is no metrics file in"
"""How the message that a folder has no metrics file starts."""


@dataclass(frozen=True)
class Definitions:
    """The semantic models and metrics of a folder.

    Attributes:
        folder: The folder of configs, which also holds the gold configs that the tables come from.
        files: The metrics files, in the order of their names.
        models: The semantic models of every file.
        metrics: The metrics of every file.
    """

    folder: Path
    files: tuple[Path, ...]
    models: tuple[SemanticModel, ...]
    metrics: tuple[Metric, ...]

    def metric(self, name: str) -> Metric:
        """The metric called ``name``.

        Raises:
            ConfigError: If there is none. The message lists the metrics, and the closest name when there is one.
        """
        found = next((item for item in self.metrics if item.name == name), None)
        if found is None:
            raise ConfigError(unknown_name("metric", name, [item.name for item in self.metrics]))
        return found

    def model(self, name: str) -> SemanticModel:
        """The semantic model called ``name``.

        Raises:
            ConfigError: If there is none.
        """
        found = next((item for item in self.models if item.name == name), None)
        if found is None:
            raise ConfigError(unknown_name("semantic model", name, [item.name for item in self.models]))
        return found

    def model_of(self, metric: Metric) -> SemanticModel:
        """The semantic model that the measures of a metric are in."""
        return self.model(metric.model)

    def metrics_of(self, model: SemanticModel) -> tuple[Metric, ...]:
        """The metrics made of the measures of a semantic model."""
        return tuple(item for item in self.metrics if item.model == model.name)

    def describe(self, metric: Metric) -> dict[str, Any]:
        """What a person or an agent needs to know to ask for a metric, as JSON-serialisable primitives."""
        model = self.model_of(metric)
        time = model.time_dimension
        info: dict[str, Any] = {
            "name": metric.name, "description": metric.description, "owner": metric.owner, "type": metric.type,
            "format": metric.format, "table": model.table, "semantic_model": model.name,
            "dimensions": [item.name for item in model.dimensions], "time_column": time.column if time else None,
            "grains": list(model.grains), "file": metric.file}
        if metric.type == "simple":
            info["measure"] = metric.measure
        else:
            info["numerator"], info["denominator"] = metric.numerator, metric.denominator
        return info

    def to_dict(self) -> dict[str, Any]:
        """The definitions as JSON-serialisable primitives."""
        return {
            "folder": str(self.folder), "files": [item.name for item in self.files],
            "semantic_models": [{
                "name": model.name, "table": model.table, "description": model.description, "file": model.file,
                "time_dimension": {"column": model.time_dimension.column, "grains": list(model.time_dimension.grains)}
                if model.time_dimension else None,
                "dimensions": [{"name": item.name, "column": item.column, "description": item.description}
                               for item in model.dimensions],
                "measures": [{"name": item.name, "agg": item.agg, "expr": item.expr, "description": item.description}
                             for item in model.measures]} for model in self.models],
            "metrics": [self.describe(item) for item in self.metrics]}


def unknown_name(noun: str, name: str, known: Sequence[str]) -> str:
    """The message for a name that is not known, with the closest known name and every known name."""
    close = difflib.get_close_matches(str(name), known, n=1)
    hint = f" Did you mean {close[0]!r}?" if close else ""
    return f"unknown {noun} {name!r}.{hint} The {noun}s are {list(known)}"


def resolve_table(reference: str, identifiers: Sequence[str]) -> str:
    """The gold table that a semantic model names.

    Args:
        reference: ``<namespace>.<table>``, or the bare name of a table when only one table has it.
        identifiers: The gold tables, each as ``<namespace>.<table>``.

    Raises:
        ConfigError: If there is no such table, or the bare name fits more than one.
    """
    wanted = reference.strip()
    if wanted in identifiers:
        return wanted
    bare = [item for item in identifiers if item.rsplit(".", 1)[-1] == wanted]
    if len(bare) == 1:
        return bare[0]
    if len(bare) > 1:
        raise ConfigError(f"{wanted!r} is the name of more than one gold table, {bare}. Write it as "
                          "<namespace>.<table>")
    raise ConfigError(f"{wanted!r} is not a gold table of this folder. The gold tables are {list(identifiers)}")


def metrics_files(path: str | os.PathLike) -> list[Path]:
    """The metrics files at ``path``: the ``*.json`` files of a folder whose kind is ``Metrics``, or the file itself.

    Raises:
        ConfigError: If ``path`` does not exist.
    """
    resolved = resolve_path(path)
    if resolved.is_dir():
        return sorted(item for item in resolved.glob("*.json") if item.is_file() and kind_of_file(item) == KIND)
    if resolved.is_file():
        return [resolved] if kind_of_file(resolved) == KIND else []
    raise ConfigError(f"path not found: {resolved}")


def _near_miss(folder: Path) -> str:
    """A hint for a folder that has a file that looks like a metrics file but names its kind wrongly."""
    for item in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kind = data.get("kind") if isinstance(data, Mapping) else None
        if isinstance(kind, str) and kind.strip().lower() == KIND.lower():
            return f" {item.name} says kind {kind!r}, and the kind is written {KIND!r}."
    return ""


def _read(files: Sequence[Path]) -> tuple[list[SemanticModel], list[Metric], list[str]]:
    """The semantic models and metrics of some metrics files, and the problems of the files, each with its file name."""
    problems: list[str] = []
    models: list[SemanticModel] = []
    metrics: list[Metric] = []
    defined: dict[tuple[str, str], str] = {}
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        errors = validate_spec(data)
        problems += [f"{file.name}: {error}" for error in errors]
        if errors:
            continue
        built_models, built_metrics = build(data, file.name)
        names = [("semantic model", item.name) for item in built_models]
        names += [("measure", measure.name) for item in built_models for measure in item.measures]
        names += [("metric", item.name) for item in built_metrics]
        for noun, name in names:
            earlier = defined.setdefault((noun, name), file.name)
            if earlier != file.name:
                problems.append(f"{file.name}: the {noun} {name!r} is also defined in {earlier}, and a name has to "
                                "mean one thing across the folder")
        models += built_models
        metrics += built_metrics
    return models, metrics, problems


def read_definitions(folder: str | os.PathLike) -> tuple[Definitions | None, list[str]]:
    """Read the metrics files of a folder and say what is wrong with them, without raising for a file.

    Args:
        folder: A folder of configs.

    Returns:
        The definitions, or ``None`` when there are problems, and the problems, each starting with its file name. A
        folder with no metrics file has one problem, which says so.

    Raises:
        ConfigError: If ``folder`` is not a folder.
    """
    root = resolve_path(folder)
    if not root.is_dir():
        raise ConfigError(f"{root} is not a folder of configs")
    files = metrics_files(root)
    if not files:
        return None, [f"{NO_FILES} {root}. A metrics file has \"kind\": \"{KIND}\".{_near_miss(root)}"]
    models, metrics, problems = _read(files)
    if problems:
        return None, problems
    return Definitions(root, tuple(files), tuple(models), tuple(metrics)), []


def _describe_problems(folder: Path, problems: Sequence[str]) -> str:
    count = len(problems)
    return "\n".join([f"the metrics of {folder} have {count} problem{'s' if count != 1 else ''}",
                      *[f"  - {item}" for item in problems]])


def load_metrics(folder: str | os.PathLike) -> Definitions:
    """Read the metrics files of a folder.

    Args:
        folder: A folder of configs.

    Returns:
        The definitions of every metrics file.

    Raises:
        ConfigError: If ``folder`` is not a folder, it has no metrics file, or a file is not valid. The message lists
            every problem.
    """
    found, problems = read_definitions(folder)
    if found is not None:
        return found
    if problems[0].startswith(NO_FILES):
        raise ConfigError(problems[0])
    raise ConfigError(_describe_problems(resolve_path(folder), problems))


def load_all(paths: Sequence[str | os.PathLike]) -> Definitions | None:
    """The definitions of the metrics files at some paths, or ``None`` when there are none.

    It is what a server that was given several config paths reads. A path is a folder or a metrics file, and the files
    may be in more than one folder.

    Raises:
        ConfigError: If a path does not exist or a file is not valid.
    """
    files: list[Path] = []
    for path in paths:
        files += [item for item in metrics_files(path) if item not in files]
    if not files:
        return None
    models, metrics, problems = _read(files)
    if problems:
        raise ConfigError(_describe_problems(files[0].parent, problems))
    return Definitions(files[0].parent, tuple(files), tuple(models), tuple(metrics))


__all__ = ["Definitions", "NO_FILES", "load_all", "load_metrics", "metrics_files", "read_definitions",
           "resolve_table", "unknown_name"]
