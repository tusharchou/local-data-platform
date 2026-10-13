"""Helpers shared by test modules: a small lake, a dbt project writer and a dbt runner."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from local_data_platform.format.iceberg import Iceberg

# --------------------------------------------------------------------------- DuckDB's iceberg extension


def _iceberg_extension_loads() -> bool:
    connection = duckdb.connect()
    try:
        row = connection.execute(
            "SELECT installed FROM duckdb_extensions() WHERE extension_name = 'iceberg'").fetchone()
        if not row or not row[0]:
            return False
        connection.execute("LOAD iceberg")
        return True
    except duckdb.Error:
        return False
    finally:
        connection.close()


requires_iceberg_extension = pytest.mark.skipif(
    not _iceberg_extension_loads(),
    reason="DuckDB's iceberg extension is not installed; run duckdb.connect().execute('INSTALL iceberg') once",
)

# --------------------------------------------------------------------------- a small lake


def dataset_config(namespace: str, name: str) -> dict:
    """A dataset config whose target is the Iceberg table ``namespace.name`` (nothing needs to run it)."""
    return {
        "identifier": name,
        "metadata": {
            "source": {"name": name, "format": "CSV", "path": f"{name}.csv"},
            "target": {"name": name, "format": "ICEBERG",
                       "catalog": {"identifier": namespace, "warehouse_path": "../warehouse"}},
        },
    }


def add_table(root: Path, namespace: str, name: str, rows: int, *, start: int = 1) -> Iceberg:
    """Write ``rows`` rows into the Iceberg table ``namespace.name`` of the lake under ``root``.

    The lake is ``root/warehouse`` and its dataset configs are in ``root/config``, one per table, so
    ``root/config`` is a folder that ``discover_tables`` and ``register_sources`` can read.
    """
    table = Iceberg(name, {"identifier": namespace, "warehouse_path": str(root / "warehouse")})
    ids = list(range(start, start + rows))
    table.put(pa.table({"id": pa.array(ids, pa.int64()), "label": pa.array([f"{name}-{i}" for i in ids], pa.string()),
                        "amount": pa.array([i * 1.5 for i in ids], pa.float64())}))
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / f"{namespace}.{name}.json").write_text(json.dumps(dataset_config(namespace, name)))
    return table


def build_lake(root: Path) -> Path:
    """A lake of ``raw.orders`` (3 rows), ``raw.customers`` (2) and ``ref.cities`` (2). Returns its config folder."""
    add_table(root, "raw", "orders", 3)
    add_table(root, "raw", "customers", 2)
    add_table(root, "ref", "cities", 2)
    return root / "config"


def relation_rows(database: Path, relation: str) -> list[tuple]:
    """Every row of ``relation`` in the DuckDB file, read through a short-lived connection."""
    with duckdb.connect(str(database), read_only=True) as connection:
        return connection.execute(f"SELECT * FROM {relation} ORDER BY 1").fetchall()


def relations(database: Path) -> dict[str, str]:
    """``schema.name`` to ``VIEW`` or ``BASE TABLE`` for every user relation in the DuckDB file."""
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = connection.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog')").fetchall()
    return {f"{schema}.{name}": kind for schema, name, kind in rows}


# --------------------------------------------------------------------------- dbt

DBT_PROJECT = """\
name: {name}
version: "1.0.0"
profile: {name}
model-paths: ["models"]
flags:
  send_anonymous_usage_stats: false
  use_colors: false
"""
PROFILES = """\
{name}:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: {database}
      threads: 1
      settings:
        autoinstall_known_extensions: false
"""


def write_dbt_project(folder: Path, models: dict[str, str], *, schema_yml: str | None = None,
                      database: str = "target/ldp_build.duckdb", name: str = "smoke") -> Path:
    """Write a dbt project: ``models`` maps a model name to its SQL, ``schema_yml`` is the optional schema file."""
    (folder / "models").mkdir(parents=True)
    (folder / "dbt_project.yml").write_text(DBT_PROJECT.format(name=name))
    (folder / "profiles.yml").write_text(PROFILES.format(name=name, database=database))
    for model, sql in models.items():
        (folder / "models" / f"{model}.sql").write_text(sql)
    if schema_yml is not None:
        (folder / "models" / "schema.yml").write_text(schema_yml)
    return folder


def dbt_model(name, *, materialized="table", unique_key=None, meta=None, schema="main", alias=None, description="",
              depends_on=()):
    """A model node as dbt 1.12 writes it to ``manifest.json``."""
    return {
        "unique_id": f"model.shop.{name}", "resource_type": "model", "name": name, "alias": alias or name,
        "schema": schema, "description": description,
        "config": {"materialized": materialized, "unique_key": unique_key, "meta": dict(meta or {})},
        "meta": dict(meta or {}),
        "depends_on": {"macros": [], "nodes": [f"model.shop.{parent}" for parent in depends_on]},
    }


def dbt_artifacts(*models, statuses=None, args=None):
    """``(manifest, run_results)`` for ``models``; every model succeeded unless ``statuses`` says otherwise."""
    manifest = {"metadata": {"project_name": "shop"}, "nodes": {node["unique_id"]: node for node in models}}
    results = [{"unique_id": node["unique_id"], "status": (statuses or {}).get(node["name"], "success"),
                "message": "OK"} for node in models]
    return manifest, {"args": {"which": "build", "empty": False, **(args or {})}, "results": results}


def without_log_warnings(err: str) -> str:
    """``err`` without the log lines ``ldp`` writes to stderr, which depend on the machine.

    Without DuckDB's iceberg extension, registering sources in auto mode logs a warning that says how to install it,
    and an exact comparison of what ``ldp`` printed must not depend on whether the extension is installed.
    """
    return "".join(line for line in err.splitlines(keepends=True) if not line.startswith("WARNING "))


FIXTURE_PROJECT = Path(__file__).parent / "fixtures" / "dbt_project"


def copy_fixture_project(destination: Path) -> Path:
    """Copy ``tests/fixtures/dbt_project`` to ``destination``.

    The project reads the lake in ``../lake`` (made with :func:`build_lake` on ``destination.parent / "lake"``)
    and publishes to the same lake: ``orders`` becomes ``silver.fct_orders`` and ``gold.gold_totals``.
    """
    shutil.copytree(FIXTURE_PROJECT, destination)
    return destination


def dbt_executable() -> str:
    """The dbt next to the Python interpreter, as ``ldp dbt`` looks for it."""
    found = shutil.which("dbt", path=str(Path(sys.executable).parent))
    assert found, "the dbt extra is installed but there is no dbt next to the interpreter"
    return found


def run_dbt(env: dict, project: Path, command: str, *args: str) -> subprocess.CompletedProcess:
    """Run ``dbt COMMAND`` on ``project`` with the version check off."""
    arguments = ["--no-version-check", command, "--project-dir", str(project), "--profiles-dir", str(project), *args]
    return subprocess.run([dbt_executable(), *arguments], capture_output=True, text=True, env=env, cwd=project,
                          timeout=300, check=False)


def run_results(project: Path) -> dict[str, str]:
    """Node name to status, from ``target/run_results.json``."""
    results = json.loads((project / "target" / "run_results.json").read_text())["results"]
    # A unique id is "model.<project>.<name>" or "test.<project>.<name>.<hash>".
    return {result["unique_id"].split(".")[2]: result["status"] for result in results}
