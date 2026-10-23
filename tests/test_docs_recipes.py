"""Runs every Python block in docs/recipes.md, in order, so the recipes can't drift from the API."""

from pathlib import Path

import pytest

from tests.docs_runner import parse_blocks, run_page

REPO_ROOT = Path(__file__).resolve().parents[1]
RECIPES = REPO_ROOT / "docs" / "recipes.md"


def test_recipes_has_python_blocks():
    assert len([block for block in parse_blocks(RECIPES) if block.language == "python"]) >= 5


def test_every_recipe_runs_in_order(tmp_path):
    pytest.importorskip("duckdb")
    pytest.importorskip("openpyxl")
    # Each block runs in its own namespace, as a reader would paste it, and the process keeps the recipe's
    # registered pipeline out of this session's registry.
    out = run_page(RECIPES, tmp_path, languages=("python",), timeout=300).stdout
    assert "['NYC', 'BKK']" in out
    assert "2 2 3" in out
    assert "[{'city': 'BKK', 'revenue': 25.0}, {'city': 'NYC', 'revenue': 40.0}]" in out
    assert "rides_csv: read 3 rows, wrote 3 rows" in out
    assert (tmp_path / "exports" / "rides.csv").is_file()
    # The churn recipe: the churn of the restaurant example for the orders of issue #47, and then the number that the
    # pandas snippet of that issue gives for the same orders, which counts inactive rows and not lost customers.
    assert "\n".join(["2023-02-01 3 3 1.0", "2023-03-01 1 0 0.0", "2023-04-01 2 1 0.5", "2023-05-01 1 1 1.0",
                      "2023-06-01 0 0 None", "[25.0, 75.0, 50.0, 75.0, 100.0, 75.0]"]) in out
    assert "2023-01-01" not in out, "January, the first month, has no row"
    # The Excel recipe: a sheet below a title row, as a CSV in the folder that stands for Google Drive.
    assert (tmp_path / "drive" / "sales.csv").read_text().splitlines() == [
        '"day","dish","orders","revenue"',
        '2026-07-01 00:00:00.000000,"pad thai",12,143.5',
        '2026-07-02 00:00:00.000000,"green curry",9,117']
