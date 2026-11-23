"""Runs every Python block in docs/recipes.md, in order, so the recipes can't drift from the API."""

import json
from pathlib import Path

import pytest

from local_data_platform import urls
from local_data_platform.spec import validate_spec
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
    # The Google Sheet recipe: a night that finds nothing new writes no rows, an edited cell is one row, a new row is
    # one row, and the table ends with the three rows of the sheet.
    assert "\n".join(["night 5: read 2 rows, wrote 2", "night 6: read 2 rows, wrote 0", "night 7: read 2 rows, wrote 1",
                      "night 8: read 3 rows, wrote 1", "2026-07-01 pad thai 12 143.5",
                      "2026-07-02 green curry 10 124.0", "2026-07-03 tom yum 5 60.0"]) in out
    assert (tmp_path / "shop" / "sheet_sales.json").is_file() and (tmp_path / "warehouse").is_dir()


def _sheet_recipe() -> str:
    text = RECIPES.read_text()
    return text.split("## A Google Sheet that stays in sync\n")[1].split("\n## ")[0]


def test_the_sheet_recipe_shows_the_export_address_of_a_tab():
    assert "https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=TAB_ID" in _sheet_recipe()


def test_the_sheet_recipe_config_is_valid_and_is_a_daily_upsert_on_the_https_address():
    blocks = [block for block in parse_blocks(RECIPES) if block.language == "json" and "docs.google.com" in block.code]
    assert len(blocks) == 1 and blocks[0].code in _sheet_recipe()

    config = json.loads(blocks[0].code)
    metadata = config["metadata"]

    assert validate_spec(config) == [], "the address, the keys and the schedule are the ones the code accepts"
    assert metadata["source"]["format"] == "CSV"
    assert metadata["source"]["path"].startswith(
        "https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=")
    assert metadata["target"]["format"] == "ICEBERG" and metadata["target"]["write_mode"] == "upsert"
    assert metadata["target"]["join_cols"] == ["day", "dish"]
    assert metadata["schedule"] == "@daily"


def test_the_sheet_recipe_gives_the_limits_that_the_code_has():
    recipe = " ".join(_sheet_recipe().split())

    assert urls.DEFAULT_TIMEOUT_S == 30 and urls.DEFAULT_MAX_BYTES == 50 * 1024 * 1024
    assert "A download may take 30 seconds and have 50 MiB after it is decompressed" in recipe
    assert "Set `timeout`, in seconds, and `max_bytes` in `metadata.source`" in recipe


def test_the_sheet_recipe_prints_the_cron_line_that_ldp_prints(tmp_path):
    # The recipes page runs its Python blocks only, so the bash block of the sheet recipe runs here, on a page of its
    # own.
    blocks = [block for block in parse_blocks(RECIPES) if block.language == "bash" and "schedule install" in block.code]
    assert len(blocks) == 1 and blocks[0].output
    page = tmp_path / "cron.md"
    page.write_text(f"```bash\n{blocks[0].code}```\n\n<!-- docs-runner: output -->\n```text\n{blocks[0].output}```\n")
    work = tmp_path / "work"
    (work / "shop").mkdir(parents=True)

    run = run_page(page, work, languages=("bash",))

    assert len(run.outcomes) == 1 and "schedule tick" in run.stdout and "/shop/.ldp/schedule.log" in run.stdout
