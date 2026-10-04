"""Runs every Python block in docs/recipes.md, in order, so the recipes can't drift from the API."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPES = REPO_ROOT / "docs" / "recipes.md"


def _python_blocks() -> list[str]:
    return re.findall(r"```python\n(.*?)```", RECIPES.read_text(), flags=re.DOTALL)


def test_recipes_has_python_blocks():
    assert len(_python_blocks()) >= 5


def test_every_recipe_runs_in_order(tmp_path):
    pytest.importorskip("duckdb")
    script = tmp_path / "recipes.py"
    # Each block runs in its own namespace, as a reader would paste it.
    script.write_text("\n".join(f"exec(compile({block!r}, 'recipe {i}', 'exec'), {{}})"
                                for i, block in enumerate(_python_blocks(), start=1)))
    # A subprocess keeps the recipe's registered pipeline out of this session's registry.
    done = subprocess.run([sys.executable, "-W", "error::ResourceWarning", str(script)], cwd=tmp_path,
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    assert "['NYC', 'BKK']" in out
    assert "2 2 3" in out
    assert "[{'city': 'BKK', 'revenue': 25.0}, {'city': 'NYC', 'revenue': 40.0}]" in out
    assert "rides_csv: read 3 rows, wrote 3 rows" in out
    assert (tmp_path / "exports" / "rides.csv").is_file()
