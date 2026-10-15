"""The warehousing guide, ``docs/warehousing.md``, is tested: every command and block of it runs, in order, offline.

The page runs from the root of a clone of the repo. The test gives it a folder that holds the restaurant example and
nothing else, so a command that needs a file that the page did not say to create fails here, as it would for a reader.
The run needs the dbt and excel extras, so it carries the ``dbt`` marker and runs in the dbt CI job. The other tests
read the page and run everywhere.
"""

import re
import shutil
from pathlib import Path

import pytest

from local_data_platform.lake import open_lake
from tests.docs_runner import parse_blocks, run_page

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "warehousing.md"
EXAMPLE = REPO_ROOT / "examples" / "restaurant"

# The blocks that the run leaves alone, and why. A skipped block is a block that nothing tests, so a new one is a
# decision, and it is made here.
SKIPPED = {
    "it installs the package, and this run already has it",
    "it serves the docs until Ctrl-C",
}


def rows(config: Path, sql: str) -> list[dict]:
    with open_lake(config) as lake:
        return lake.query(sql).to_pylist()


@pytest.mark.dbt
def test_every_block_of_the_guide_runs_offline_and_leaves_the_tables_it_describes(tmp_path, network_trap):
    pytest.importorskip("openpyxl")
    environment, connections = network_trap
    shutil.copytree(EXAMPLE, tmp_path / "examples" / "restaurant",
                    ignore=shutil.ignore_patterns("data", "target", "logs", "__pycache__"))

    run = run_page(GUIDE, tmp_path, env=environment, timeout=300)

    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert {block.skip for block in run.skipped} == SKIPPED
    work = tmp_path / "ldp_demo" / "warehousing"
    config = work / "config"
    assert rows(config, "SELECT count(*) AS n FROM raw.orders") == [{"n": 853}]
    assert rows(config, "SELECT count(*) AS n FROM silver.fct_orders") == [{"n": 853}]
    assert rows(config, "SELECT churned FROM gold.churn_by_month WHERE month = DATE '2026-06-01'") == [{"churned": 16}]
    assert rows(work / "first_project" / "target" / "ldp",
                "SELECT sum(orders) AS orders FROM gold.orders_by_channel") == [{"orders": 745}]
    assert (work / "restaurant_dbt" / "target" / "catalog.json").is_file()
    assert len(run.blocks) >= 20


def test_the_guide_follows_the_learn_chapter_template():
    text = GUIDE.read_text()
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    opening = text.split("\n## ", 1)[0]

    assert text.startswith("# ") and "**You will learn**" in opening and "**It takes**" in opening
    assert headings[-1] == "What to read next"
    assert "(orchestration.md)" in text  # scheduling dbt runs is covered there, and the guide says so


def test_the_sql_yaml_and_json_the_guide_shows_are_excerpts_of_the_example():
    files = [path.read_text() for path in EXAMPLE.rglob("*") if path.suffix in (".sql", ".yml", ".json")]
    shown = [block for block in parse_blocks(GUIDE) if block.language in ("sql", "yaml", "json")]

    assert shown  # the guide shows some of the example
    for block in shown:
        assert any(block.code.strip() in text for text in files), f"{block.where} is not in a file of the example"
