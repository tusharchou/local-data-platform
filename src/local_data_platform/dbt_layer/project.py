"""The dbt project that LDP runs: ``ldp_dbt.json``, ``ldp dbt init``, and running dbt.

The contract is ``docs/design/warehousing.md``. dbt runs as a subprocess, so nothing in this
module imports dbt, and ``ldp --help`` lists the ``dbt`` command without loading it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_data_platform.exceptions import ConfigError, EngineNotFound
from local_data_platform.logger import get_logger
from local_data_platform.paths import resolve_path

logger = get_logger(__name__)

PROJECT_FILE = "ldp_dbt.json"
DEFAULT_DATABASE = "target/ldp_build.duckdb"
DEFAULT_CONFIG_DIR = "target/ldp"
SOURCES_FILE = "models/_ldp_sources.yml"
INSTALL_HINT = 'pip install "local-data-platform[dbt]"'

_TOP_KEYS = ("database", "sources", "publish", "observability")
_SOURCES_KEYS = ("configs", "catalogs")
_PUBLISH_KEYS = ("catalog", "config_dir")


@dataclass(frozen=True)
class DbtProject:
    """What ``ldp_dbt.json`` says, with every path made absolute.

    Attributes:
        directory: The dbt project folder, which also holds ``ldp_dbt.json``.
        database: The DuckDB build file dbt writes to.
        source_configs: Dataset config files and folders that name the tables to use as sources.
        source_catalogs: Catalog spec files whose tables are sources.
        publish_catalog: The LDP catalog block published models are written to. Relative paths in
            it are relative to ``directory``.
        config_dir: Where the generated dataset configs are written.
        observability: The ``observability`` block copied into every generated config, or ``None``.
        sources_file: The dbt sources YAML that ``register_sources`` writes.
    """

    directory: Path
    database: Path
    source_configs: tuple[Path, ...]
    source_catalogs: tuple[Path, ...]
    publish_catalog: dict[str, Any]
    config_dir: Path
    observability: dict[str, Any] | None
    sources_file: Path

    @classmethod
    def load(cls, directory: str | os.PathLike) -> DbtProject:
        """Read and check ``<directory>/ldp_dbt.json``.

        Raises:
            ConfigError: If the folder or file is missing, or the file has problems. All problems are
                reported at once, each starting with its JSON path, as ``ldp plan`` does.
        """
        folder = resolve_path(directory)
        if not folder.is_dir():
            raise ConfigError(f"dbt project folder not found: {folder}")
        path = folder / PROJECT_FILE
        if not path.is_file():
            raise ConfigError(f"{folder} has no {PROJECT_FILE}. Create a project with: ldp dbt init {directory}")
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as error:
            raise ConfigError(f"{path} is not valid JSON: {error}") from error
        problems = validate_project(data)
        if problems:
            lines = "\n".join(f"  - {problem}" for problem in problems)
            raise ConfigError(f"{path} has {len(problems)} problem{'s' if len(problems) != 1 else ''}:\n{lines}")

        sources = data.get("sources") or {}
        if isinstance(sources, list):
            sources = {"configs": sources}
        publish = data["publish"]
        return cls(
            directory=folder,
            database=resolve_path(data.get("database") or DEFAULT_DATABASE, folder),
            source_configs=tuple(resolve_path(item, folder) for item in sources.get("configs", [])),
            source_catalogs=tuple(resolve_path(item, folder) for item in sources.get("catalogs", [])),
            publish_catalog=dict(publish["catalog"]),
            config_dir=resolve_path(publish.get("config_dir") or DEFAULT_CONFIG_DIR, folder),
            observability=dict(data["observability"]) if data.get("observability") else None,
            sources_file=folder / SOURCES_FILE,
        )


def validate_project(data: Any) -> list[str]:
    """Return the problems in an ``ldp_dbt.json`` document, each as ``"<json path>: <message>"``."""
    if not isinstance(data, Mapping):
        return [f"must be a JSON object, got {type(data).__name__}"]
    problems: list[str] = []
    unknown = sorted(set(map(str, data)) - set(_TOP_KEYS))
    if unknown:
        problems.append(f"unknown keys {unknown}; allowed: {list(_TOP_KEYS)}")
    if data.get("database") is not None and not _text(data["database"]):
        problems.append(f"database: must be a non-empty string, got {data['database']!r}")

    sources = data.get("sources")
    if isinstance(sources, list):
        sources = {"configs": sources}
    if sources is not None and not isinstance(sources, Mapping):
        problems.append("sources: must be a list of config paths or an object with 'configs' and 'catalogs'")
    elif isinstance(sources, Mapping):
        unknown = sorted(set(map(str, sources)) - set(_SOURCES_KEYS))
        if unknown:
            problems.append(f"sources: unknown keys {unknown}; allowed: {list(_SOURCES_KEYS)}")
        for key in _SOURCES_KEYS:
            items = sources.get(key, [])
            if not isinstance(items, list) or not all(_text(item) for item in items):
                problems.append(f"sources.{key}: must be a list of non-empty path strings")

    publish = data.get("publish")
    if not isinstance(publish, Mapping):
        problems.append("publish: is required and must be an object with a 'catalog'")
    else:
        unknown = sorted(set(map(str, publish)) - set(_PUBLISH_KEYS))
        if unknown:
            problems.append(f"publish: unknown keys {unknown}; allowed: {list(_PUBLISH_KEYS)}")
        if publish.get("config_dir") is not None and not _text(publish["config_dir"]):
            problems.append(f"publish.config_dir: must be a non-empty string, got {publish['config_dir']!r}")
        if "catalog" not in publish:
            problems.append("publish.catalog: is required")
        else:
            problems += _catalog_problems(publish["catalog"])

    observability = data.get("observability")
    if observability is not None and not isinstance(observability, Mapping):
        problems.append(f"observability: must be an object, got {type(observability).__name__}")
    return problems


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _catalog_problems(catalog: Any) -> list[str]:
    """Check a catalog block with the same code ``ldp plan`` uses, reporting it as ``publish.catalog``."""
    from local_data_platform.spec import validate_spec

    dummy = {"identifier": "check", "metadata": {
        "source": {"name": "check", "format": "CSV", "path": "check.csv"},
        "target": {"name": "check", "format": "ICEBERG", "catalog": catalog}}}
    prefix = "metadata.target.catalog"
    return [str(error).replace(prefix, "publish.catalog", 1) for error in validate_spec(dummy)]


# --------------------------------------------------------------------------- running dbt


def find_dbt(dbt_bin: str | os.PathLike | None = None) -> str:
    """The ``dbt`` to run: ``dbt_bin`` if given, else the one next to the Python interpreter, else the one on ``PATH``.

    Raises:
        EngineNotFound: If there is none, with the command that installs it.
    """
    if dbt_bin:
        found = shutil.which(str(dbt_bin))
        if found is None:
            raise EngineNotFound(f"dbt was not found at {dbt_bin}. Install it with: {INSTALL_HINT}")
        return found
    for name in ("dbt.exe", "dbt") if os.name == "nt" else ("dbt",):
        beside = Path(sys.executable).parent / name
        if beside.is_file() and os.access(beside, os.X_OK):
            return str(beside)
    found = shutil.which("dbt")
    if found is None:
        raise EngineNotFound(f"dbt was not found. Install it with: {INSTALL_HINT}")
    return found


def run_dbt(project: DbtProject, binary: str, arguments: Sequence[str], *, stdout: int | None = None) -> int:
    """Run ``dbt ARGUMENTS`` on the project and return its exit status.

    ``arguments`` start with the dbt command, such as ``["build", "--select", "orders"]``. The project
    flags come last, so they are valid for a command with a subcommand too, as in ``docs generate``.
    dbt runs with the project folder as its working directory, because dbt resolves a relative profile
    ``path`` against it. Its output goes straight to the terminal, in colour only when that is a
    terminal. ``--no-version-check`` stops dbt from contacting PyPI on every run.

    ``stdout`` is a file descriptor that dbt's standard output goes to instead, such as ``2`` for the standard error
    of this process. A caller that prints a result of its own on standard output, as ``ldp workflow run --json`` does,
    uses it to keep dbt's progress lines out of that result.
    """
    flags = ["--no-version-check"]
    if not (sys.stdout.isatty() if stdout is None else os.isatty(stdout)):
        flags.append("--no-use-colors")  # keep escape codes out of log files and pipes
    argv = [binary, *flags, *arguments, "--project-dir", str(project.directory),
            "--profiles-dir", str(project.directory)]
    logger.info("Running %s in %s", " ".join(argv), project.directory)
    # dbt writes to the same terminal or pipe, so what LDP printed before it must come out first.
    sys.stdout.flush()
    sys.stderr.flush()
    return subprocess.run(argv, cwd=project.directory, stdout=stdout, check=False).returncode


def find_target_path(project: DbtProject, extra: Sequence[str] = ()) -> str:
    """The folder dbt writes ``manifest.json`` and ``run_results.json`` to, resolved as dbt resolves it.

    ``--target-path`` in ``extra`` wins, then the ``DBT_TARGET_PATH`` variable, then ``target-path``
    in ``dbt_project.yml``, then ``target``. A relative path is relative to the project folder.
    """
    for index, argument in enumerate(extra):
        if argument == "--target-path" and index + 1 < len(extra):
            return extra[index + 1]
        if argument.startswith("--target-path="):
            return argument.partition("=")[2]
    if os.environ.get("DBT_TARGET_PATH"):
        return os.environ["DBT_TARGET_PATH"]
    try:
        import yaml
    except ImportError:
        return "target"
    try:
        configured = yaml.safe_load((project.directory / "dbt_project.yml").read_text()).get("target-path")
    except (OSError, AttributeError, yaml.YAMLError):
        configured = None
    return str(configured) if configured else "target"


def check_profile(project: DbtProject) -> None:
    """Fail clearly when ``profiles.yml`` points dbt at another build file than ``ldp_dbt.json`` does.

    The check is best effort: it needs PyYAML (installed with dbt), and it stands aside when the
    path is a Jinja expression or the profile cannot be read.

    Raises:
        ConfigError: With both paths, when they differ.
    """
    try:
        import yaml
    except ImportError:
        return
    try:
        profile_name = yaml.safe_load((project.directory / "dbt_project.yml").read_text())["profile"]
        profile = yaml.safe_load((project.directory / "profiles.yml").read_text())[profile_name]
        output = profile["outputs"][profile.get("target") or next(iter(profile["outputs"]))]
        path = str(output["path"])
    except (OSError, KeyError, TypeError, AttributeError, StopIteration, yaml.YAMLError):
        return
    if "{{" in path:
        return
    resolved = Path(path) if os.path.isabs(path) else (project.directory / path)
    if resolved.resolve() != project.database:
        raise ConfigError(f"ldp_dbt.json says dbt builds in {project.database}, but profiles.yml sets path to "
                          f"{resolved.resolve()}. Make the two match.")


# --------------------------------------------------------------------------- init

DBT_PROJECT_YML = """\
name: {name}
version: "1.0.0"
profile: {name}
model-paths: ["models"]

# dbt sends anonymous usage statistics unless told not to.
flags:
  send_anonymous_usage_stats: false

models:
  {name}:
    staging:
      +materialized: view
    gold:
      +materialized: table
"""
PROFILES_YML = """\
{name}:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: {database}
      threads: 4
      settings:
        # The views over Iceberg tables need DuckDB's iceberg extension. dbt must never download one.
        autoinstall_known_extensions: false
"""
STAGING_SQL = """\
-- A staging model reads a source and tidies it. {note}
select * from {source}
"""
GOLD_SQL = """\
-- ldp_publish writes this model to Iceberg after a successful build. Remove it to keep the model in dbt only.
-- A table in the gold namespace is a gold table, so the model names its owner and its grain here, and schema.yml
-- describes the model and each of its columns. LDP writes all of it into the Iceberg table.
{{ config(meta={'ldp_publish': true, 'owner': 'data team', 'grain': 'one row for the whole table'}) }}

select count(*) as row_count from {{ ref('stg_example') }}
"""
SCHEMA_YML = """\
version: 2

models:
  - name: gold_example
    description: How many rows the staging model has. Replace it with what your table means.
    columns:
      - name: row_count
        description: How many rows stg_example has.
"""
GITIGNORE = "target/\nlogs/\ndbt_packages/\n"


def init_project(directory: str | os.PathLike, sources: str | os.PathLike, warehouse: str | os.PathLike) -> list[Path]:
    """Write a dbt project that works as soon as the sources exist.

    Args:
        directory: The new project folder. It must be empty or missing.
        sources: The folder of dataset configs whose Iceberg tables become sources.
        warehouse: The folder the published tables are written to (a ``local`` catalog's warehouse).

    Returns:
        The files written.

    Raises:
        ConfigError: If the folder exists and is not empty. ``init`` never overwrites anything.
    """
    folder = resolve_path(directory)
    if folder.exists() and not folder.is_dir():
        raise ConfigError(f"{folder} is a file, not a folder")
    if folder.is_dir() and any(folder.iterdir()):
        raise ConfigError(f"{folder} is not empty. Run ldp dbt init on an empty or missing folder, and edit "
                          f"{PROJECT_FILE} to change what an existing project uses")
    sources_dir, warehouse_dir = resolve_path(sources), resolve_path(warehouse)
    name = re.sub(r"[^a-z0-9_]+", "_", folder.name.lower()).strip("_") or "ldp_dbt"
    if name[0].isdigit():
        name = f"ldp_{name}"

    source = _first_source(sources_dir)
    staging = STAGING_SQL.format(
        source="{{ source('%s', '%s') }}" % source if source else "(select 1 as id, 'example' as label)",
        note="Replace it with a model on your own tables." if source else
             "There were no tables yet, so it reads a constant. Point it at a source once ldp run has made some.")
    project = {
        "database": DEFAULT_DATABASE,
        "sources": {"configs": [_relative(sources_dir, folder)], "catalogs": []},
        "publish": {"catalog": {"identifier": "gold", "warehouse_path": _relative(warehouse_dir, folder)},
                    "config_dir": DEFAULT_CONFIG_DIR},
    }
    files = {
        "dbt_project.yml": DBT_PROJECT_YML.format(name=name),
        "profiles.yml": PROFILES_YML.format(name=name, database=DEFAULT_DATABASE),
        PROJECT_FILE: json.dumps(project, indent=2) + "\n",
        "models/staging/stg_example.sql": staging,
        "models/gold/gold_example.sql": GOLD_SQL,
        "models/schema.yml": SCHEMA_YML,
        ".gitignore": GITIGNORE,
    }
    written = []
    for relative, text in files.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    return written


def _first_source(sources_dir: Path) -> tuple[str, str] | None:
    """The first Iceberg table the configs in ``sources_dir`` name, as ``(namespace, table)``, if any."""
    from local_data_platform.mcp_server.discovery import discover_tables

    if not sources_dir.exists():
        return None
    try:
        discovery = discover_tables([sources_dir])
    except Exception:  # noqa: BLE001 - a folder without usable configs just means no example table
        return None
    try:
        return (".".join(discovery.tables[0].namespace), discovery.tables[0].name) if discovery.tables else None
    finally:
        discovery.close()


def _relative(path: Path, start: Path) -> str:
    """``path`` relative to ``start`` with forward slashes, or absolute when they are on different drives."""
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:  # pragma: no cover - only on Windows, across drives
        return path.as_posix()


__all__ = [
    "DEFAULT_CONFIG_DIR", "DEFAULT_DATABASE", "INSTALL_HINT", "PROJECT_FILE", "SOURCES_FILE", "DbtProject",
    "check_profile", "find_dbt", "find_target_path", "init_project", "run_dbt", "validate_project",
]
