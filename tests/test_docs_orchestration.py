"""The orchestration guide, ``docs/orchestration.md``, is tested: every command and block of it runs, in order, offline.

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
GUIDE = REPO_ROOT / "docs" / "orchestration.md"
EXAMPLE = REPO_ROOT / "examples" / "restaurant"

# The blocks that the run leaves alone, and why. A skipped block is a block that nothing tests, so a new one is a
# decision, and it is made here.
SKIPPED = {
    "it installs the package, and this run already has it",
    "it changes what your Mac runs in the background",
    "it changes your crontab",
    "it needs Dagster, which this repo does not install",
    "it needs Prefect, which this repo does not install",
}


def rows(config: Path, sql: str) -> list[dict]:
    with open_lake(config) as lake:
        return lake.query(sql).to_pylist()


@pytest.mark.dbt
def test_every_block_of_the_guide_runs_offline_and_leaves_the_tables_it_describes(tmp_path, network_trap):
    pytest.importorskip("openpyxl")
    environment, connections = network_trap
    shutil.copytree(EXAMPLE, tmp_path / "examples" / "restaurant",
                    ignore=shutil.ignore_patterns("data", "target", "logs", "__pycache__", ".ldp"))

    run = run_page(GUIDE, tmp_path, env=environment, timeout=600)

    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert {block.skip for block in run.skipped} == SKIPPED
    work = tmp_path / "ldp_demo" / "orchestration"
    assert rows(work / "workflows", "SELECT count(*) AS n FROM raw.orders") == [{"n": 22}]
    assert rows(work / "workflows", "SELECT count(*) AS n FROM raw.order_lines") == [{"n": 58}]
    assert rows(work / "workflows", "SELECT sum(total_amount) AS amount FROM raw.orders "
                                    "WHERE cast(order_ts AS date) = DATE '2026-08-02'") == [{"amount": 4100.0}]
    assert rows(work / "config", "SELECT count(*) AS n FROM gold.revenue_by_restaurant_day") == [{"n": 15}]
    assert len(run.blocks) >= 25


def test_the_guide_follows_the_learn_chapter_template():
    text = GUIDE.read_text()
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    opening = text.split("\n## ", 1)[0]

    assert text.startswith("# ") and "**You will learn**" in opening and "**It takes**" in opening
    assert headings[-1] == "What to read next"
    assert {"Exit statuses", "When something goes wrong"} <= set(headings)


def test_the_guide_covers_what_the_milestone_promised():
    text = GUIDE.read_text()

    for words in ("launchd", "cron", "Airflow", "Dagster", "Prefect", "run_workflow", "--replace", "--dry-run",
                  "retries", "ldp schedule tick", "ldp schedule install", "ldp backfill", "ldp workflow plan"):
        assert words in text, words
    assert "Apple has deprecated cron, and cron jobs do not run while the Mac sleeps" in text
    table = text.split("## Exit statuses", 1)[1].split("\n## ", 1)[0]
    assert [row.split("|")[1].strip() for row in table.splitlines() if re.match(r"\| \d \|", row)] == [
        "0", "1", "2", "3"]


def test_the_json_the_guide_shows_is_in_the_files_of_the_example():
    files = [path.read_text() for path in (EXAMPLE / "workflows").glob("*.json")]
    shown = [block for block in parse_blocks(GUIDE) if block.language == "json"]

    assert len(shown) == 3  # the source path and the window of the orders config, and the workflow
    for block in shown:
        assert any(block.code.strip() in text for text in files), f"{block.where} is not in a file of the example"
    assert any(block.code.strip() == (EXAMPLE / "workflows" / "daily_orders.json").read_text().strip()
               for block in shown), "the guide shows the whole workflow file"


def test_the_keys_the_guide_prints_are_the_keys_of_the_example_for_those_days():
    from local_data_platform.spec import plan

    text = GUIDE.read_text()
    for day, after in (("2026-08-01", "2026-08-02"), ("2026-08-02", "2026-08-03"), ("2026-08-03", "2026-08-04"),
                       ("2026-08-04", "2026-08-05")):
        key = plan(EXAMPLE / "workflows" / "orders.json", window=f"{day}/{after}")["idempotency_key"]
        assert key in text or key[:12] in text, day


def test_the_makefile_target_and_the_example_files_the_guide_names_exist():
    text = GUIDE.read_text()

    assert "make demo-orchestration" in text and "demo-orchestration:" in (REPO_ROOT / "Makefile").read_text()
    for name in ("daily_orders.json", "orders.json", "order_lines.json"):
        assert (EXAMPLE / "workflows" / name).is_file() and name in text
