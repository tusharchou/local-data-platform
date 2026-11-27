"""``docs/dashboard.md``, the page about ``ldp dashboard``.

The command that starts the server is not run, because a test run starts none, and every other command of the page
runs. The rest of what can be checked is checked here. The page is in the nav, the options of the command are the
options of the table, the audit records are the ones that the code writes, the versions are the ranges of the extra,
the date of the last look is a date, and the links go somewhere.
"""

import datetime as dt
import re
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from local_data_platform import cli as ldp_cli  # noqa: E402
from local_data_platform.dashboard import cli as dashboard_cli  # noqa: E402
from local_data_platform.dashboard import service  # noqa: E402
from tests.docs_runner import parse_blocks, run_page  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "dashboard.md"


def _sections() -> dict[str, str]:
    parts = re.split(r"^## ", PAGE.read_text(), flags=re.M)[1:]
    return {part.split("\n", 1)[0].strip(): part for part in parts}


def test_the_page_is_in_the_nav_under_guides_and_its_links_exist():
    nav = (REPO / "mkdocs.yml").read_text().split("  - Guides:")[1].split("\n  - ")[0]

    assert '"A local dashboard in the browser": dashboard.md' in nav
    for link in re.findall(r"\]\(([\w./-]+\.md)(?:#[\w-]+)?\)", PAGE.read_text()):
        assert (PAGE.parent / link).is_file(), link


def test_the_readme_and_the_agents_guide_point_to_the_page():
    readme = (REPO / "README.md").read_text()
    assert "(docs/dashboard.md)" in readme and "### A local dashboard" in readme
    assert "| `notebook` | marimo, Altair and DuckDB |" in readme
    assert "`ldp dashboard --config PATH" in readme
    assert "(dashboard.md)" in (REPO / "docs" / "agents.md").read_text()


def test_every_command_of_the_page_that_starts_a_server_or_installs_is_marked_as_not_run_and_the_rest_runs(tmp_path):
    bash = [block for block in parse_blocks(PAGE) if block.language == "bash"]

    for block in bash:
        if block.code.startswith(("ldp dashboard", "pip install")):
            assert block.skip, block.where
        else:
            assert not block.skip, block.where
    run = run_page(PAGE, tmp_path, languages=("bash",))
    assert len(run.outcomes) == 1 and (tmp_path / "ldp_demo" / "rides.json").is_file()
    assert len(run.skipped) == 2


def test_the_options_of_the_table_are_the_options_of_the_command():
    parser = ldp_cli.build_parser()
    [commands] = [action for action in parser._actions if action.choices and "dashboard" in action.choices]
    flags = {flag for action in commands.choices["dashboard"]._actions for flag in action.option_strings
             if flag.startswith("--") and flag != "--help"} - {"--verbose"}
    table = _sections()["Start it"]

    listed = set(re.findall(r"^\| `(--[a-z-]+)", table, flags=re.M))

    assert listed == flags == {"--config", "--catalog", "--allow", "--reports", "--port", "--no-browser", "--edit"}


def test_the_audit_records_that_the_page_names_are_the_ones_that_the_code_writes():
    text = _sections()["What it can and cannot do"]
    named = set(re.findall(r"`(dashboard\.[a-z_]+)`", text))

    assert named == {f"{service.TOOL_PREFIX}{tool}" for tool in (
        "list_tables", "describe_table", "sample_rows", "table_history", "query", "report")}
    assert "`<warehouse>/.ldp/audit/mcp_audit.jsonl`" in text and dashboard_cli.HOST == "127.0.0.1"
    assert f"`--host {dashboard_cli.HOST}`" in text


def test_the_page_names_the_limits_and_the_settings_variable_that_the_code_has():
    from local_data_platform.mcp_server.tools import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S

    text = PAGE.read_text()

    assert f"the row cap of {DEFAULT_MAX_ROWS} rows and the timeout of {DEFAULT_TIMEOUT_S:g} seconds" in text
    assert f"its {service.HISTORY_ROWS} newest snapshots" in text and f"first {service.SAMPLE_ROWS} rows" in text
    assert f"`{service.SETTINGS_ENV}`" in text and f"`{dashboard_cli.EDIT_COPY}`" in text
    for window in service.WINDOWS:
        assert f"`{window}`" in text


def test_the_versions_on_the_page_are_the_ranges_of_the_extra_and_the_date_of_the_last_look_is_a_date():
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    text = _sections()["Versions"]

    assert "`marimo>=0.26,<0.27`" in text and "`altair>=6.3,<6.4`" in text
    assert set(extras["notebook"]) == {"marimo>=0.26,<0.27", "altair>=6.3,<6.4", "duckdb>=1.1"}
    found = re.search(r"Last looked at by hand (\d{4}-\d{2}-\d{2})\*\* with marimo ([\d.]+) and Altair ([\d.]+)", text)
    assert found, "the page says when it was last looked at, and with which versions"
    assert dt.date(2026, 11, 1) <= dt.date.fromisoformat(found.group(1)) <= dt.date.today()
    assert found.group(2).startswith("0.26.") and found.group(3).startswith("6.3.")


def test_a_report_that_the_page_shows_is_a_report_that_the_checks_accept(tmp_path):
    import json

    from local_data_platform.report.spec import validate_report

    (block,) = [item for item in parse_blocks(PAGE) if item.language == "json"]

    assert validate_report(json.loads(block.code)) == []
