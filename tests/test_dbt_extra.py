"""The ``dbt`` extra installs a dbt that runs offline on DuckDB.

Every test is marked ``dbt``: it skips without the extra (see ``conftest.py``) and the ``dbt`` CI job
runs it with ``pytest -m dbt``. dbt runs as a subprocess, which is how ``ldp dbt`` will run it.

Offline is checked, not assumed. The subprocess gets an HTTP proxy that is a local socket, and the
tests fail if dbt connects to it. By default dbt asks PyPI for its latest version, and for usage
statistics unless the project turns them off, so the tests use ``--no-version-check`` and a project
whose ``flags`` set ``send_anonymous_usage_stats: false``.
"""

import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.dbt

REPO_ROOT = Path(__file__).resolve().parents[1]

DBT_PROJECT = """\
name: smoke
version: "1.0.0"
profile: smoke
model-paths: ["models"]
flags:
  send_anonymous_usage_stats: false
  use_colors: false
"""
PROFILES = """\
smoke:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: target/smoke.duckdb
      threads: 1
"""
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
    (folder / "models").mkdir(parents=True)
    (folder / "dbt_project.yml").write_text(DBT_PROJECT)
    (folder / "profiles.yml").write_text(PROFILES)
    (folder / "models" / "numbers.sql").write_text(sql)
    (folder / "models" / "schema.yml").write_text(SCHEMA)
    return folder


def dbt_executable() -> str:
    """The dbt next to the Python interpreter, as ``ldp dbt`` will look for it."""
    found = shutil.which("dbt", path=str(Path(sys.executable).parent))
    assert found, "the dbt extra is installed but there is no dbt next to the interpreter"
    return found


@pytest.fixture
def network_trap():
    """A fake HTTP proxy on localhost. Yields ``(environment, connections)``.

    ``connections`` gets one item per connection the subprocess opens, so an empty list means dbt
    made no network request that honours the proxy variables (requests, urllib and httpx do).
    """
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    connections: list[bytes] = []

    def serve() -> None:
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return  # the fixture closed the socket
            connections.append(client.recv(200))
            client.close()

    threading.Thread(target=serve, daemon=True).start()
    proxy = f"http://127.0.0.1:{server.getsockname()[1]}"
    env = dict(os.environ, NO_PROXY="", no_proxy="")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[name] = proxy
    try:
        yield env, connections
    finally:
        server.close()


def dbt(env: dict, project: Path, command: str) -> subprocess.CompletedProcess:
    """Run ``dbt COMMAND`` on ``project`` with the version check off."""
    arguments = ["--no-version-check", command, "--project-dir", str(project), "--profiles-dir", str(project)]
    return subprocess.run([dbt_executable(), *arguments], capture_output=True, text=True, env=env, cwd=project,
                          timeout=300, check=False)


def run_results(project: Path) -> dict[str, str]:
    """Node name to status, from ``target/run_results.json``."""
    results = json.loads((project / "target" / "run_results.json").read_text())["results"]
    # A unique id is "model.<project>.<name>" or "test.<project>.<name>.<hash>".
    return {result["unique_id"].split(".")[2]: result["status"] for result in results}


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

    result = dbt(env, project, "build")

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

    result = dbt(env, project, "build")

    assert result.returncode == 1, result.stdout + result.stderr
    assert run_results(project)["unique_numbers_id"] == "fail"
    assert connections == []
