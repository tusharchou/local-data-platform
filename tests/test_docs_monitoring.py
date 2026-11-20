"""The monitoring guide, ``docs/monitoring.md``, is tested: every command and block of it runs, in order, offline.

The page runs from the root of a clone of the repo. The test gives it a folder that holds the restaurant example and
nothing else, so a command that needs a file that the page did not say to create fails here, as it would for a reader.
The run needs the dbt and excel extras, so it carries the ``dbt`` marker and runs in the dbt CI job. The other tests
read the page and run everywhere.

The recipes of Part 8 are text, because a job that runs under cron or launchd is not something a test run can install.
They are tested against the code that prints the recipes of ``ldp schedule install``, so that they stay the same
recipes with ``monitor`` in the place of ``schedule tick``.
"""

import json
import plistlib
import re
import shutil
from pathlib import Path

import pytest

from local_data_platform.lake import open_lake
from local_data_platform.orchestration.recipes import cron_recipe, launchd_recipe
from local_data_platform.spec import plan
from tests.docs_runner import parse_blocks, run_page
from tests.support import load_example_module

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "monitoring.md"
EXAMPLE = REPO_ROOT / "examples" / "restaurant"

# The blocks that the run leaves alone, and why. A skipped block is a block that nothing tests, so a new one is a
# decision, and it is made here.
SKIPPED = {
    "it installs the package, and this run already has it",
    "it changes what your Mac runs in the background",
    "it changes your crontab",
    "it deletes rows of the history, and which rows depends on the day that you run it",
}

# What the five nights of the story do to the alert file, in the order that the guide prints them. The test of the
# example has the same list, which is how the guide and the walkthrough are held to one story.
ALERTS = [
    ("monitor.error", "freshness"),
    ("monitor.resolved", "freshness"),
    ("monitor.error", "volume.rows"),
    ("monitor.resolved", "volume.rows"),
    ("monitor.warn", "schema"),
    ("monitor.resolved", "schema"),
    ("monitor.error", "metric.revenue.value"),
    ("monitor.resolved", "metric.revenue.value"),
]


def rows(config: Path, sql: str) -> list[dict]:
    with open_lake(config) as lake:
        return lake.query(sql).to_pylist()


class Block:
    """A fenced block of the guide, with the text of the placeholder paths put in."""

    def __init__(self, language: str, code: str):
        self.language, self.code = language, code


def python_blocks_that_contain(text: str) -> list:
    return [block for block in parse_blocks(GUIDE) if block.language == "python" and text in block.code]


@pytest.mark.dbt
def test_every_block_of_the_guide_runs_offline_and_leaves_the_tables_it_describes(tmp_path, network_trap, monkeypatch):
    pytest.importorskip("openpyxl")
    monkeypatch.delenv("LDP_SLACK_WEBHOOK", raising=False)
    environment, connections = network_trap
    environment.pop("LDP_SLACK_WEBHOOK", None)
    shutil.copytree(EXAMPLE, tmp_path / "examples" / "restaurant",
                    ignore=shutil.ignore_patterns("data", "target", "logs", "__pycache__", ".ldp"))

    run = run_page(GUIDE, tmp_path, env=environment, timeout=600)

    assert connections == []  # not one request to a proxy, so dbt and ldp stayed offline
    assert {block.skip for block in run.skipped} == SKIPPED
    work = tmp_path / "ldp_demo" / "monitoring"
    assert rows(work / "config", "SELECT count(*) AS n FROM raw.orders") == [{"n": 35}]
    assert rows(work / "config", "SELECT count(*) AS n FROM raw.order_lines") == [{"n": 84}]
    assert rows(work / "config", "SELECT count(*) AS n FROM gold.gold_daily_sales") == [{"n": 23}]
    assert rows(work / "config", "SELECT round(sum(revenue), 2) AS amount FROM gold.gold_daily_sales "
                                 "WHERE day = DATE '2026-08-08'") == [{"amount": 633.0}]
    sent = [json.loads(line) for line in (work / ".ldp" / "alerts.jsonl").read_text().splitlines()]
    assert [(alert["event"], alert.get("monitor")) for alert in sent if alert["event"] != "alert.test"] == ALERTS
    assert [alert["event"] for alert in sent].count("alert.test") == 1
    heartbeats = [json.loads(line) for line in (work / ".ldp" / "heartbeat.jsonl").read_text().splitlines()]
    assert [(beat["event"], beat["monitors"], beat["results"]) for beat in heartbeats] == [
        ("monitor.heartbeat", 4, {"ok": 4})]
    assert (work / "status.html").is_file() and "orders" in (work / "status.html").read_text()
    assert len(run.blocks) >= 25


def test_the_guide_follows_the_learn_chapter_template():
    text = GUIDE.read_text()
    headings = re.findall(r"^## (.+)$", text, flags=re.MULTILINE)
    opening = text.split("\n## ", 1)[0]

    assert text.startswith("# ") and "**You will learn**" in opening and "**It takes**" in opening
    assert "**You need**" in opening
    assert headings[0] == "The story" and headings[1:3] == ["Install", "Make the data"]
    assert [heading.split(".")[0] for heading in headings if heading.startswith("Part ")] == [
        f"Part {number}" for number in range(1, 9)]
    assert headings[-1] == "What to read next"
    assert {"Exit statuses", "What the monitors cannot do", "When something goes wrong"} <= set(headings)


def test_the_guide_covers_what_the_milestone_promised():
    text = GUIDE.read_text()

    for words in ("launchd", "cron", "Healthchecks.io", "monitor.heartbeat", "ldp monitor", "ldp status", "--html",
                  "ldp alert test", "ldp maintain", "ldp plan", "every 15 minutes", "freshness", "volume.rows",
                  "schema", "metric.revenue.value", "monitor.resolved", "LDP_SLACK_WEBHOOK", "url_env",
                  "EnvironmentVariables", "RunAtLoad", "StartInterval"):
        assert words in text, words
    assert "Nothing alerts while the laptop is asleep or off" in text
    assert "Apple has deprecated cron, and cron jobs do not run while the Mac sleeps" in text
    table = text.split("## Exit statuses", 1)[1].split("\n## ", 1)[0]
    assert [row.split("|")[1].strip() for row in table.splitlines() if re.match(r"\| \d \|", row)] == [
        "0", "1", "2", "3"]


def test_the_numbers_the_guide_gives_for_the_nights_are_the_numbers_of_the_generator():
    generate = load_example_module("restaurant_generate_for_the_guide", EXAMPLE / "generate.py")
    dataset = generate.generate(generate.SEED)
    table = GUIDE.read_text().split("| File | Rows | What it is |", 1)[1].split("\n\n", 1)[0]

    counts, totals, lines = [], {}, []
    for story in generate.MONITORING_DAYS:
        rows_of_day, lines_of_day = dataset.monitoring_orders(story.day, story.orders, story.amount_scale)
        counts.append(len(rows_of_day))
        totals[story.day.isoformat()] = round(sum(row["total_amount"] for row in rows_of_day), 2)
        lines.append(len(lines_of_day))
    assert counts == [8, 3, 8, 8, 8]
    assert lines == [19, 8, 19, 19, 19]
    for day, count in zip(totals, counts):
        row = next(line for line in table.splitlines() if f"`monitor_orders_{day}.csv`" in line)
        assert f"| {count} |" in row, day
    for day, total in {"2026-08-03": 6330.0, "2026-08-06": 1950.0, "2026-08-08": 633.0}.items():
        assert totals[day] == total, day
        row = next(line for line in table.splitlines() if f"`monitor_orders_{day}.csv`" in line)
        assert f"add up to {total:g}" in row, day
    assert "| 19, 8, 19, 19 and 19 |" in table


def test_the_sla_the_guide_writes_is_the_sla_of_the_walkthrough(tmp_path, monkeypatch):
    monkeypatch.delenv("LDP_SLACK_WEBHOOK", raising=False)
    run = load_example_module("restaurant_run_for_the_guide", EXAMPLE / "run.py")
    by_guide, by_example = tmp_path / "guide", tmp_path / "example"
    for folder in (by_guide, by_example):
        shutil.copytree(EXAMPLE / "config", folder / "config")
    monkeypatch.chdir(by_guide)
    first = python_blocks_that_contain('spec["metadata"]["sla"] = {')
    second = python_blocks_that_contain('spec["metadata"]["sla"]["metrics"] =')
    assert len(first) == 1 and len(second) == 1
    for block in (*first, *second):
        exec(compile(block.code, block.where, "exec"), {"__name__": "__main__"})  # noqa: S102 - the guide's own code

    run.monitoring_config(by_example, revenue=True)

    ours, theirs = (json.loads((folder / "config" / "orders.json").read_text())["metadata"]
                    for folder in (by_guide, by_example))
    assert ours["sla"] == theirs["sla"]
    assert ours["observability"] == theirs["observability"]


def test_the_json_the_guide_shows_is_a_valid_part_of_a_config(tmp_path):
    shown = [block for block in parse_blocks(GUIDE) if block.language == "json"]

    assert len(shown) == 5  # every key of an SLA, the metric entries, a query entry, a Slack channel and a webhook
    for number, block in enumerate(shown):
        folder = tmp_path / f"case{number}"
        shutil.copytree(EXAMPLE / "config", folder)
        path = folder / "orders.json"
        spec = json.loads(path.read_text())
        metadata = spec["metadata"]
        code = block.code.strip()
        if code.startswith('"sla"'):
            metadata["sla"] = json.loads("{" + code + "}")["sla"]
        elif code.startswith('"metrics"'):
            metadata["sla"] = {"metrics": json.loads("{" + code + "}")["metrics"]}
        elif code.startswith('{"name"') and '"sql"' in code:
            (folder / "metrics.json").unlink()  # a folder that has a metrics file refuses a query
            metadata["sla"] = {"metrics": [json.loads(code)]}
        else:
            metadata["observability"] = {"sinks": ["iceberg"], "alerts": [json.loads(code)]}
        path.write_text(json.dumps(spec, indent=2))

        result = plan(path)

        assert result["valid"] and not result["errors"], (block.where, result["errors"])


def test_the_recipes_of_the_guide_are_the_recipes_of_the_tick_with_monitor_in_its_place(tmp_path):
    # The guide names /home/you and /Users/you. A real folder stands in for them, because the recipes resolve the
    # folder, and a path such as /home is a link on some machines.
    home = tmp_path.resolve() / "you"
    folder = home / "local-data-platform" / "ldp_demo" / "monitoring" / "config"
    folder.mkdir(parents=True)
    executable = home / "local-data-platform" / ".venv" / "bin" / "ldp"
    blocks = [Block(block.language, block.code.replace("/home/you", str(home)).replace("/Users/you", str(home)))
              for block in parse_blocks(GUIDE)]

    crons = [block for block in blocks if block.language == "text" and block.code.startswith("HEALTHCHECK_URL=")]
    assert len(crons) == 1
    guide_cron = crons[0].code.splitlines()
    tick = cron_recipe(folder, executable).replace("ldp schedule tick for", "ldp monitor for") \
        .replace(" schedule tick ", " monitor ").replace("every 5 minutes", "every 15 minutes") \
        .replace("*/5 ", "*/15 ").replace("schedule.log", "monitor.log")
    assert guide_cron[0] == "HEALTHCHECK_URL=https://hc-ping.com/your-check-uuid"
    assert "\n".join(guide_cron[1:]) + "\n" == tick

    plists = [block for block in blocks if block.language == "xml"]
    assert len(plists) == 1
    guide_plist = plistlib.loads(plists[0].code.encode("utf-8"))
    label = "local-data-platform.monitor.restaurant"
    expected = plistlib.loads(launchd_recipe(folder, executable, home=home).encode("utf-8"))
    expected["Label"] = label
    expected["ProgramArguments"] = [expected["ProgramArguments"][0], "monitor", expected["ProgramArguments"][3]]
    expected["StartInterval"] = 900
    expected["StandardOutPath"] = expected["StandardErrorPath"] = f"{home}/Library/Logs/{label}.log"
    assert guide_plist.pop("EnvironmentVariables") == {"HEALTHCHECK_URL": "https://hc-ping.com/your-check-uuid"}
    assert guide_plist == expected
    assert guide_plist["StartInterval"] == 900 and guide_plist["RunAtLoad"] is True

    weekly = [block for block in blocks if block.language == "text" and " maintain " in block.code]
    assert len(weekly) == 1
    fields = weekly[0].code.split(None, 5)
    assert fields[:5] == ["17", "3", "*", "*", "0"]  # at 03:17 on Sundays
    assert fields[5].startswith(f"{executable} maintain {folder}/orders.json --system --apply >> ")


def test_the_page_is_in_the_nav_the_readme_the_index_and_the_example_readme():
    nav = (REPO_ROOT / "mkdocs.yml").read_text().split("nav:", 1)[1].split("\ntheme:", 1)[0]
    guides = nav.split("  - Guides:", 1)[1].split("\n  - Design:", 1)[0].splitlines()
    names = [line.split(":")[-1].strip() for line in guides]

    assert names[names.index("observability.md") + 1] == "monitoring.md"  # next to the page on the run events
    assert "docs/monitoring.md" in (REPO_ROOT / "README.md").read_text()
    assert "(monitoring.md)" in (REPO_ROOT / "docs" / "index.md").read_text()
    assert "docs/monitoring.md" in (EXAMPLE / "README.md").read_text()
    assert "demo-monitor:" in (REPO_ROOT / "Makefile").read_text() and "make demo-monitor" in GUIDE.read_text()


def test_the_makefile_help_lists_demo_monitor_and_demo_all_runs_it():
    makefile = (REPO_ROOT / "Makefile").read_text()
    help_lines = makefile.split("\nhelp:\n", 1)[1].split("\n\n", 1)[0].splitlines()
    demo_all = makefile.split("\ndemo-all:", 1)[1].split("\n\n", 1)[0]

    assert any(re.search(r'echo "  demo-monitor +Run the restaurant example\'s monitoring layer', line)
               for line in help_lines)
    assert "$(MAKE) demo-monitor" in demo_all
    assert "demo-monitor" in makefile.split(".PHONY:", 1)[1].split("\n\n", 1)[0]
    assert "demo-monitor: $(DEMO_CONFIG)" in makefile
    assert "--layer monitoring" in makefile.split("\ndemo-monitor:", 1)[1].split("\n\n", 1)[0]


def test_the_changelog_lists_the_milestone():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text()
    section = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]

    for number in range(158, 168):  # the pull requests of the milestone
        assert f"#{number}" in section, number
    for words in ("ldp monitor", "ldp status", "ldp alert test", "ldp maintain", "--system", "metadata.sla",
                  "`monitor.heartbeat`", "`describe_table`", "make demo-monitor", "docs/monitoring.md",
                  "docs/alerts.md"):
        assert words in section, words
