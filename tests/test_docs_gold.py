"""The gold layer guide, ``docs/gold.md``, is tested: every command and block of it runs, in order, offline.

The page runs from the root of a clone of the repo. The test gives it a folder that holds the restaurant example and
nothing else, so a command that needs a file that the page did not say to create fails here, as it would for a reader.
The run needs the dbt and excel extras, so it carries the ``dbt`` marker and runs in the dbt CI job. The other tests
read the page and run everywhere.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from local_data_platform.gold.release import list_releases
from local_data_platform.lake import open_lake
from tests.docs_runner import parse_blocks, run_page

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "gold.md"
EXAMPLES = REPO_ROOT / "examples"
RESTAURANT = EXAMPLES / "restaurant"

# The blocks that the run leaves alone, and why. A skipped block is a block that nothing tests, so a new one is a
# decision, and it is made here.
SKIPPED = {
    "it installs the package, and this run already has it",
}


def rows(config: Path, sql: str) -> list[dict]:
    with open_lake(config) as lake:
        return lake.query(sql).to_pylist()


def gold_commands() -> list[str]:
    """The names of the ``ldp gold`` commands, as ``ldp gold --help`` lists them."""
    done = subprocess.run([sys.executable, "-m", "local_data_platform.cli", "gold", "--help"], capture_output=True,
                          text=True, check=True)
    listed = done.stdout.split("positional arguments:", 1)[1].split("\n\n", 1)[0]
    return re.findall(r"^    (\w+)  ", listed, flags=re.MULTILINE)


@pytest.mark.dbt
def test_every_block_of_the_guide_runs_offline_and_leaves_the_tables_it_describes(tmp_path, network_trap):
    pytest.importorskip("openpyxl")
    pytest.importorskip("mcp")
    environment, connections = network_trap
    shutil.copytree(RESTAURANT, tmp_path / "examples" / "restaurant",
                    ignore=shutil.ignore_patterns("data", "target", "logs", "__pycache__", ".ldp"))
    shutil.copytree(EXAMPLES / "robot_episodes", tmp_path / "examples" / "robot_episodes",
                    ignore=shutil.ignore_patterns("__pycache__"))

    run = run_page(GUIDE, tmp_path, env=environment, timeout=600)

    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert {block.skip for block in run.skipped} == SKIPPED
    work = tmp_path / "ldp_demo" / "gold"
    config = work / "config"
    assert rows(config, "SELECT count(*) AS n FROM raw.orders") == [{"n": 883}]
    assert rows(config, "SELECT count(*) AS n FROM raw.order_lines") == [{"n": 2195}]
    assert rows(config, "SELECT count(*) AS n FROM silver.fct_orders") == [{"n": 883}]
    for table, count in (("churn_by_month", 7), ("gold_daily_sales", 643), ("gold_menu_item_weekly", 953),
                         ("gold_restaurant_scorecard", 6)):
        assert rows(config, f"SELECT count(*) AS n FROM gold.{table}") == [{"n": count}], table
    assert [release.name for release in list_releases(config)] == ["r2", "r1"]  # the blocked build cut no third
    assert not (work / "trial").exists()  # the folders that the guide throws away are gone
    for name in ("scorecard.xlsx", "daily_sales.csv", "gold.duckdb", "catalog.html", "catalog.md"):
        assert (work / "out" / name).is_file(), name
    page = (work / "out" / "catalog.html").read_text()
    assert re.findall(r'(?:src|href|action)="https?://', page) == [] and "default-src" in page  # it asks for nothing
    assert len(run.blocks) >= 30
    # the two queries of the section for people who are not engineers run on the file that the guide wrote
    queries = next(block for block in parse_blocks(GUIDE) if block.language == "sql" and "_catalog" in block.code)
    with duckdb.connect(str(work / "out" / "gold.duckdb"), read_only=True) as file:
        for statement in [part for part in queries.code.split(";") if part.strip()]:
            assert file.execute(statement).fetchall(), statement


def test_the_guide_follows_the_learn_chapter_template():
    text = GUIDE.read_text()
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    opening = text.split("\n## ", 1)[0]

    assert text.startswith("# ")
    assert all(label in opening for label in ("**You will learn**", "**It takes**", "**You need**"))
    assert headings[0] == "The story" and headings[-1] == "What to read next"
    parts = [heading.split(".")[0] for heading in headings if heading.startswith("Part ")]
    assert parts == [f"Part {number}" for number in range(1, 9)]
    assert {"When something goes wrong", "For people who are not engineers"} <= set(headings)
    assert headings.index("For people who are not engineers") > headings.index(
        "Part 8. Let an agent read the gold layer")


def test_the_guide_covers_what_the_issue_promised():
    text = GUIDE.read_text()

    commands = gold_commands()
    assert set(commands) == {"list", "show", "docs", "sync", "build", "export", "duckdb", "releases"}
    for command in commands:
        assert f"ldp gold {command} " in text, command
    for words in ("ldp metrics list", "ldp metrics validate", "ldp metrics query",
                  "ldp mcp --config config --layer gold", "ldp query config --release r1", "--live", "--format md",
                  "--no-samples", "--strict", "--dry-run", "ldp gold build config --duckdb", "metrics.json",
                  "`DUCKDB` source", "query_file", "inputs", "ldp_quality", "ldp_namespace", "catalog.html",
                  "gold.duckdb", "xlsx", "csv", "release r1", "release r2"):
        assert words in text, words
    for table in ("churn_by_month", "gold_daily_sales", "gold_menu_item_weekly", "gold_restaurant_scorecard"):
        assert (RESTAURANT / "dbt" / "models" / "gold" / f"{table}.sql").is_file() and table in text, table


def test_the_section_for_people_who_are_not_engineers_stands_on_its_own():
    text = GUIDE.read_text()
    section = text.split("\n## For people who are not engineers", 1)[1].split("\n## ", 1)[0]

    for words in ("catalog.html", "Double-click", "search box", "Excel", "gold.duckdb", "duckdb -ui gold.duckdb",
                  "_catalog", "_metrics", "1,048,575", "ldp gold export CONFIG_FOLDER"):
        assert words in section, words
    assert not re.search(r"```(bash|python)", section)  # nothing in it is for the runner, or needs a clone
    assert not re.search(r"Part \d", section)  # and it does not send the reader back to the parts


def test_the_code_the_guide_shows_is_in_a_file_of_the_examples():
    text = GUIDE.read_text()
    # the section for people who are not engineers has queries of its own, which are not from a file
    before_the_section = text.split("\n## For people who are not engineers", 1)[0].count("\n")
    files = [path.read_text() for path in EXAMPLES.rglob("*") if path.suffix in (".sql", ".yml", ".json")]
    shown = [block for block in parse_blocks(GUIDE)
             if block.language in ("sql", "yaml", "json") and block.line < before_the_section]

    assert len(shown) == 6  # the model, its schema, the robot source and query, and two metric definitions
    for block in shown:
        assert any(block.code.strip() in file for file in files), f"{block.where} is not in a file of the examples"


def test_the_page_is_in_the_nav_the_readme_the_index_and_the_example_readme():
    nav = (REPO_ROOT / "mkdocs.yml").read_text().split("nav:", 1)[1].split("\ntheme:", 1)[0]
    guides = nav.split("  - Guides:", 1)[1].split("\n  - Design:", 1)[0]

    assert "gold.md" in guides
    assert "docs/gold.md" in (REPO_ROOT / "README.md").read_text()
    assert "(gold.md)" in (REPO_ROOT / "docs" / "index.md").read_text()
    assert "docs/gold.md" in (RESTAURANT / "README.md").read_text()
    assert "demo-gold:" in (REPO_ROOT / "Makefile").read_text() and "make demo-gold" in GUIDE.read_text()


def test_the_changelog_lists_the_milestone():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    # under Unreleased until the release pull request, and under its own version after it
    section = changelog.split("Milestone 0.1.4 Self Serving Gold Layer", 1)[1].split("\n## [", 1)[0]

    for number in (142, 143, 145, 148, 149, 151, 152, 153, 154, 155, 156):  # the pull requests of the milestone
        assert f"#{number}" in section, number
    for command in gold_commands():
        assert f"ldp gold {command}" in section or f"`{command}`" in section, command
    for words in ("ldp metrics", "ldp mcp", "--layer gold", "`list_metrics`", "`query_metric`", "make demo-gold",
                  "docs/gold.md", "`DUCKDB` source"):
        assert words in section, words


def test_the_makefile_help_lists_demo_gold():
    makefile = (REPO_ROOT / "Makefile").read_text()
    help_lines = makefile.split("\nhelp:\n", 1)[1].split("\n\n", 1)[0].splitlines()

    assert any(re.search(r'echo "  demo-gold +Run the restaurant example\'s gold layer', line) for line in help_lines)
