"""Packaging checks: one source of truth for the version, a src-only layout, and no
import-time side effects."""

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import Version

import local_data_platform

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
# Any bracketed heading, so "## [v0.2.0]" or "## [0.2.0rc1]" is checked too. Text that is not a
# version fails with InvalidVersion, which is also worth a failure.
CHANGELOG_HEADING = re.compile(r"^## \[v?([^\]]+)\]", re.MULTILINE)
# "removed in 0.2.0", "removed in v0.2.0", "removed in version 0.2.0" or "removed in 0.2", also when the words are
# split across adjacent string literals.
REMOVAL_NOTICE = re.compile(r"removed[\s\"']+in[\s\"']+(?:version[\s\"']+)?v?(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)

# Imports the package (and optionally every submodule) in a fresh interpreter and reports
# what changed. A fresh process matters: the test session has already imported the package.
_SIDE_EFFECT_PROBE = r"""
import contextlib, importlib, io, json, logging, os, pkgutil, sys, warnings

walk = sys.argv[1] == "all"
root = logging.getLogger()
env_before = {k: v for k, v in os.environ.items() if k.upper().startswith("PYICEBERG")}
handlers_before = list(root.handlers)
level_before = root.level
stdout = io.StringIO()

with contextlib.redirect_stdout(stdout), warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import local_data_platform
    imported = ["local_data_platform"]
    if walk:
        for info in pkgutil.walk_packages(local_data_platform.__path__, "local_data_platform."):
            importlib.import_module(info.name)
            imported.append(info.name)

env_after = {k: v for k, v in os.environ.items() if k.upper().startswith("PYICEBERG")}
print(json.dumps({
    "imported": imported,
    "new_env": sorted(set(env_after.items()) - set(env_before.items())),
    "new_root_handlers": [repr(h) for h in root.handlers if h not in handlers_before],
    "root_level_changed": root.level != level_before,
    "stdout": stdout.getvalue(),
}))
"""


def _probe(scope: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYICEBERG")}
    completed = subprocess.run(
        [sys.executable, "-c", _SIDE_EFFECT_PROBE, scope],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=120,
    )
    assert completed.returncode == 0, f"importing failed:\n{completed.stderr}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_version_matches_pyproject():
    assert local_data_platform.__version__ == PYPROJECT["project"]["version"]


def test_changelog_has_an_entry_for_the_current_version():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    assert f"## [{local_data_platform.__version__}]" in changelog


def test_changelog_names_no_version_newer_than_the_package():
    current = Version(local_data_platform.__version__)
    headings = CHANGELOG_HEADING.findall((REPO_ROOT / "CHANGELOG.md").read_text())
    newer = [version for version in headings if version.lower() != "unreleased" and Version(version) > current]
    assert newer == [], f"CHANGELOG.md has a section for a version newer than {current}: {newer}"


def test_removal_notices_name_a_later_version():
    # A deprecation that says "removed in X.Y.Z" must still be in the future: once the package
    # reaches X.Y.Z, either the code goes or the notice moves.
    current = Version(local_data_platform.__version__)
    due = [f"{path.relative_to(REPO_ROOT)}: removed in {version}"
           for path in sorted((REPO_ROOT / "src").rglob("*.py"))
           for version in REMOVAL_NOTICE.findall(path.read_text())
           if Version(version) <= current]
    assert due == [], f"removal notices for {current} or earlier: {due}"


def test_importing_the_package_has_no_side_effects():
    result = _probe("package")
    assert result["new_env"] == [], "importing local_data_platform set PYICEBERG environment variables"
    assert result["new_root_handlers"] == [], "importing local_data_platform added handlers to the root logger"
    assert not result["root_level_changed"], "importing local_data_platform changed the root logger level"
    assert result["stdout"] == "", "importing local_data_platform printed to stdout"


def test_importing_every_submodule_has_no_side_effects():
    # Submodules such as format.iceberg are not imported by the package itself, so the
    # package-level check alone would miss an os.environ write or basicConfig call there.
    result = _probe("all")
    assert len(result["imported"]) > 1
    assert result["new_env"] == [], f"a submodule set PYICEBERG environment variables: {result['new_env']}"
    assert result["new_root_handlers"] == [], f"a submodule configured the root logger: {result['new_root_handlers']}"
    assert not result["root_level_changed"], "a submodule changed the root logger level"
    assert result["stdout"] == "", f"a submodule printed at import time: {result['stdout']!r}"


def test_repo_has_only_the_src_package():
    for stale in ("local_data_platform", "local-data-platform"):
        assert not (REPO_ROOT / stale).exists(), f"stale top-level package copy {stale}/ is back"
    assert (REPO_ROOT / "src" / "local_data_platform" / "__init__.py").is_file()


def test_build_packages_only_src():
    assert PYPROJECT["tool"]["poetry"]["packages"] == [{"include": "local_data_platform", "from": "src"}]


def test_tests_import_the_src_package():
    # An editable install imports from src/, an installed wheel from site-packages. Anything
    # else under the repo root means a stray copy is shadowing the real package.
    package_dir = Path(local_data_platform.__file__).resolve().parent
    assert not package_dir.is_relative_to(REPO_ROOT) or package_dir == REPO_ROOT / "src" / "local_data_platform"


def test_ldp_console_script_is_declared():
    assert PYPROJECT["project"]["scripts"]["ldp"] == "local_data_platform.cli:main"


def test_extras_are_declared():
    assert set(PYPROJECT["project"]["optional-dependencies"]) >= {"duckdb", "bigquery", "dev", "docs"}


def test_read_the_docs_requirements_match_the_docs_extra():
    lines = (REPO_ROOT / "requirements.txt").read_text().splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert sorted(requirements) == sorted(PYPROJECT["project"]["optional-dependencies"]["docs"])
