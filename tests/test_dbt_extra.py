"""The ``dbt`` extra installs a dbt that runs offline on DuckDB.

Every test is marked ``dbt``: it skips without the extra (see ``conftest.py``) and the ``dbt`` CI job
runs it with ``pytest -m dbt``. dbt runs as a subprocess, which is how ``ldp dbt`` will run it.

Offline is checked, not assumed. The subprocess gets an HTTP proxy that is a local socket (the
``network_trap`` fixture), and the tests fail if dbt connects to it. By default dbt asks PyPI for
its latest version, and for usage statistics unless the project turns them off, so the tests use
``--no-version-check`` and a project whose ``flags`` set ``send_anonymous_usage_stats: false``.
"""

import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from tests.support import run_dbt, run_results, write_dbt_project

pytestmark = pytest.mark.dbt

REPO_ROOT = Path(__file__).resolve().parents[1]

SCHEMA = """\
version: 2
models:
  - name: numbers
    columns:
      - name: id
        data_tests: [unique, not_null]
"""


def write_project(folder: Path, sql: str) -> Path:
    """A one-model dbt project with a unique and a not_null test on ``numbers.id``."""
    return write_dbt_project(folder, {"numbers": sql}, schema_yml=SCHEMA, database="target/smoke.duckdb")


def test_the_installed_versions_satisfy_the_dbt_extra():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    for text in pyproject["project"]["optional-dependencies"]["dbt"]:
        requirement = Requirement(text)
        installed = importlib.metadata.version(requirement.name)
        assert requirement.specifier.contains(installed, prereleases=True), (
            f"{requirement.name} {installed} is outside {text}")


def test_the_network_trap_sees_a_request_that_uses_the_proxy_variables(network_trap):
    # A control for the "no network" assertions below: they would pass for the wrong reason if the trap saw nothing.
    env, connections = network_trap
    subprocess.run([sys.executable, "-c", "import requests; requests.get('https://example.invalid', timeout=5)"],
                   env=env, capture_output=True, timeout=60, check=False)

    assert connections and connections[0].startswith(b"CONNECT example.invalid:443"), connections


def test_dbt_builds_a_project_on_duckdb_with_no_network(tmp_path, network_trap):
    env, connections = network_trap
    project = write_project(tmp_path / "project", "select 1 as id, 'a' as label union all select 2, 'b'")

    result = run_dbt(env, project, "build")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING" not in result.stdout, result.stdout  # the project uses dbt 1.12's own settings
    assert run_results(project) == {"numbers": "success", "unique_numbers_id": "pass", "not_null_numbers_id": "pass"}
    assert connections == [], f"dbt opened a network connection: {connections}"
    import duckdb

    with duckdb.connect(str(project / "target" / "smoke.duckdb"), read_only=True) as database:
        assert database.execute("select count(*) from main.numbers").fetchone() == (2,)


def test_dbt_exits_non_zero_and_names_the_failed_test_when_data_is_bad(tmp_path, network_trap):
    env, connections = network_trap
    project = write_project(tmp_path / "project", "select 1 as id union all select 1")  # a duplicate id

    result = run_dbt(env, project, "build")

    assert result.returncode == 1, result.stdout + result.stderr
    assert run_results(project)["unique_numbers_id"] == "fail"
    assert connections == []
