"""``docs/bi_tools.md``, the page that opens the gold DuckDB file in Superset, Metabase and Evidence.

The three recipes need Docker or Node.js and a server, and a test run starts none, so each recipe says that it is not
tested in CI and carries the date it was last verified by hand. These tests check what can be checked without the tools.
The page is in the nav, each recipe says what it must say, every command that is not about ``ldp`` is marked as not run,
the commands about ``ldp`` run on a gold layer and print what the page says, the SQLAlchemy URI of the Superset recipe
and the connection file of the Evidence recipe say what the page says they do, and what the page says about the file
is what the code writes.
"""

import datetime as dt
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from local_data_platform.gold import serve  # noqa: E402
from local_data_platform.gold.build import build_gold  # noqa: E402
from tests.docs_runner import parse_blocks, run_page  # noqa: E402
from tests.support import requires_iceberg_extension  # noqa: E402
from tests.test_gold_build import gold, silver  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "bi_tools.md"
RECIPES = ("Superset", "Metabase", "Evidence")


def _sections() -> dict[str, str]:
    """The text of each ``##`` section of the page, by its heading."""
    parts = re.split(r"^## ", PAGE.read_text(), flags=re.M)[1:]
    return {part.split("\n", 1)[0].strip(): part for part in parts}


def _layer(root: Path) -> Path:
    """A folder called ``config`` that has one gold table and a release, which is what the commands of the page name."""
    config = root / "config"
    silver(config)
    gold(config, "gold_revenue", docs={"owner": "finance", "primary_key": ["restaurant"]}, owner="finance")
    assert build_gold(config, release=True).exit_code == 0
    return config


# --------------------------------------------------------------------------- the page and its recipes


def test_the_page_is_in_the_nav_under_guides_and_its_links_exist():
    nav = (REPO / "mkdocs.yml").read_text().split("  - Guides:")[1].split("\n  - ")[0]

    assert '"BI tools on the gold DuckDB file": bi_tools.md' in nav
    for link in re.findall(r"\]\(([\w./-]+\.md)\)", PAGE.read_text()):
        assert (PAGE.parent / link).is_file(), link


def test_the_page_names_each_tool_in_a_recipe_of_its_own():
    assert [name for name in _sections() if name in RECIPES] == list(RECIPES)


@pytest.mark.parametrize("recipe", RECIPES)
def test_each_recipe_says_that_it_is_not_tested_in_ci_and_when_it_was_last_verified(recipe):
    section = _sections()[recipe]

    assert "Not tested in CI" in section
    found = re.search(r"Last verified (\d{4}-\d{2}-\d{2})", section)
    assert found, "a recipe carries the date that it was last verified"
    assert dt.date(2026, 11, 1) <= dt.date.fromisoformat(found.group(1)) <= dt.date.today()


def test_the_page_says_at_the_top_that_the_recipes_are_not_tested_in_ci_and_adds_no_export_command():
    text = PAGE.read_text()

    assert "not tested in CI" in text.split("\n## ")[0]
    assert "ldp export duckdb" not in text


def test_every_command_that_is_not_about_ldp_is_marked_as_not_tested_in_ci():
    bash = [block for block in parse_blocks(PAGE) if block.language == "bash"]

    for block in bash:
        if not block.code.startswith("ldp "):
            assert block.skip and "not tested in CI" in block.skip, block.where
    assert len([block for block in bash if block.skip and "not tested in CI" in block.skip]) == 4


# --------------------------------------------------------------------------- what runs


def test_the_commands_about_ldp_run_on_a_gold_layer_and_print_what_the_page_says(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _layer(work)

    run = run_page(PAGE, work, languages=("bash",))

    assert (work / "out" / "gold.duckdb").is_file()
    assert len(run.outcomes) == 1 and run.stdout.startswith("wrote out/gold.duckdb, 1 table, from release r1, cut ")
    assert len(run.skipped) == 5, "the views command and the four commands of the tools are not run here"
    assert not (work / "out" / "gold_views.duckdb").exists()


@requires_iceberg_extension
def test_the_views_command_of_the_page_runs_where_the_iceberg_extension_is_there(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _layer(work)
    (block,) = [block for block in parse_blocks(PAGE) if block.language == "bash" and "--views" in block.code]
    page = tmp_path / "views.md"
    page.write_text(f"```bash\n{block.code}```\n")

    run = run_page(page, work, languages=("bash",))

    assert len(run.outcomes) == 1 and (work / "out" / "gold_views.duckdb").is_file()
    assert "A view reads its Iceberg table where it is" in run.stdout


# --------------------------------------------------------------------------- what the recipes say


def test_the_sqlalchemy_uri_of_the_superset_recipe_is_the_absolute_path_that_the_page_says():
    sqlalchemy = pytest.importorskip("sqlalchemy")
    uris = re.findall(r"`(duckdb:////[^`]+)`", _sections()["Superset"])

    assert len(uris) == 1
    url = sqlalchemy.engine.make_url(uris[0])
    found = (url.drivername, url.database, dict(url.query))
    assert found == ("duckdb", "/data/gold.duckdb", {"access_mode": "read_only"})
    assert "-v \"$PWD/out:/data:ro\"" in _sections()["Superset"], "the container has the file at the path of the URI"


def test_the_dockerfile_of_the_superset_recipe_extends_the_image_and_installs_the_packages_of_the_superset_extra():
    section = _sections()["Superset"]
    (dockerfile,) = [block for block in parse_blocks(PAGE) if block.language == "dockerfile"]
    lines = dockerfile.code.splitlines()

    assert lines[0] == "FROM apache/superset:6.1.0" and "`apache/superset:6.1.0`" in section
    assert lines.index("USER root") < [i for i, line in enumerate(lines) if line.startswith("RUN ")][0] < lines.index(
        "USER superset")
    assert '"duckdb-engine>=0.17.0" "duckdb>=1.4.2,<2"' in dockerfile.code


def test_ldp_has_no_extra_for_a_bi_tool_and_the_page_says_why_superset_cannot_share_its_environment():
    packaging = pytest.importorskip("packaging.requirements")
    extras = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    section = _sections()["Superset"]

    assert not [name for name in extras if re.search("superset|metabase|evidence", name, re.I)]
    assert "`sqlalchemy<2,>=1.4`" in section and "`pyarrow<19,>=16.1.0`" in section
    asked = [packaging.Requirement(line) for line in importlib.metadata.requires("pyiceberg") or []]
    catalog = [item for item in asked if item.name == "sqlalchemy" and "sql-sqlite" in str(item.marker)]
    assert catalog and "2.0.18" in catalog[0].specifier and "1.4.54" not in catalog[0].specifier


def test_the_connection_file_of_the_evidence_recipe_is_valid_yaml_that_names_the_file_that_the_recipe_copies():
    yaml = pytest.importorskip("yaml")
    (connection,) = [block for block in parse_blocks(PAGE) if block.language == "yaml"]
    commands = " ".join(block.code for block in parse_blocks(PAGE) if block.language == "bash")

    loaded = yaml.safe_load(connection.code)

    assert loaded == {"name": "gold", "type": "duckdb", "options": {"filename": "gold.duckdb"}}
    assert f"sources/{loaded['name']}/{loaded['options']['filename']}" in commands
    assert "from gold.daily_sales" in _sections()["Evidence"], "a source query is asked for as the source and its file"


def test_the_evidence_recipe_loads_the_sources_before_it_starts_the_site():
    (commands,) = [block for block in parse_blocks(PAGE) if block.language == "bash" and "npm run dev" in block.code]

    assert commands.code.splitlines() == ["npm run sources", "npm run dev"]


def test_what_the_page_says_about_the_file_is_what_the_code_writes():
    text = PAGE.read_text()

    assert serve.STORAGE_VERSION == "v1.0.0" and "storage version of DuckDB 1.0" in text
    assert f"`{serve.CATALOG_TABLE}`" in text and f"`{serve.METRICS_TABLE}`" in text
    assert set(re.findall(r"`(_\w+)`", text)) == {serve.CATALOG_TABLE, serve.METRICS_TABLE}, "no other table is named"
    assert "`--views`" in text and serve.ICEBERG_HINT.startswith("Install DuckDB's iceberg extension")
