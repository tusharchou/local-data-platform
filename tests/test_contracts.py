"""Table contracts: the ``metadata.docs`` block, the gold gate, the metadata-only commit and ``ldp gold sync``.

Everything here is offline. The tables are in local SQLite catalogs under ``tmp_path``, and the tests run on every
pyiceberg that the project supports, so the metadata-only commit is checked on 0.11.0 and on the latest release.
The dbt side of the contract, which fills the block from a manifest, is in ``test_dbt_publish.py``.
"""

import json
import sys
from pathlib import Path

import pyarrow as pa
import pytest

from local_data_platform import Config, contracts
from local_data_platform.cli import main
from local_data_platform.events import MemorySink
from local_data_platform.exceptions import ConfigError, ContractError, DataQualityError
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import CommitContext, CommitPolicy, Iceberg
from local_data_platform.format.iceberg.commit import CommitConflict
from local_data_platform.gold.sync import find_model, sync_config
from local_data_platform.orchestration import run_workflow
from local_data_platform.orchestration.retries import is_retriable
from local_data_platform.pipeline import Pipeline
from local_data_platform.pipeline.builders import iceberg_from_config
from local_data_platform.pipeline.registry import create_pipeline
from local_data_platform.spec import canonical_spec, json_schema, spec_hash, validate_spec

SALES = "day,revenue,orders\n2026-08-01,10.5,3\n2026-08-02,20.0,4\n"
COLUMNS = {"day": "The day.", "revenue": "What the orders cost.", "orders": "How many orders."}
PROPERTIES = ("comment", "ldp.owner", "ldp.layer", "ldp.grain", "ldp.primary-key")


def gold(**changes) -> dict:
    """The complete contract of a gold table that has the columns day, revenue and orders."""
    return {"layer": "gold", "description": "Revenue for each day.", "grain": "one row per day",
            "primary_key": "day", "columns": dict(COLUMNS), **changes}


def spec(block=None, *, name="daily_sales", who="analytics", when=None, mode="append", quality=None) -> dict:
    """A config that loads ``sales.csv`` into the Iceberg table ``gold.<name>``, with ``block`` as its docs."""
    metadata = {"source": {"name": "sales", "format": "CSV", "path": "sales.csv"},
                "target": {"name": name, "format": "ICEBERG", "write_mode": mode,
                           "catalog": {"identifier": "gold", "warehouse_path": "warehouse"}}}
    if block is not None:
        metadata["docs"] = block
    if quality is not None:
        metadata["quality"] = quality
    return {"identifier": name, **({"who": who} if who else {}), **({"when": when} if when else {}),
            "metadata": metadata}


def write_config(root: Path, block=None, *, name="daily_sales", csv=SALES, **options) -> Path:
    """Write ``sales.csv`` and the config of ``spec`` into ``root``, and return the config's path."""
    (root / "sales.csv").write_text(csv)
    path = root / f"{name}.json"
    path.write_text(json.dumps(spec(block, name=name, **options)))
    return path


def run(path: Path, *, sink=None, commit=None, mode=None):
    return create_pipeline(Config.from_json(path)).run(mode, sink=sink, commit=commit)


def staged(key: str = "k1", run_id: str = "r1") -> CommitContext:
    return CommitContext(run_id=run_id, attempt=1, idempotency_key=key, spec_hash="h", search_since_ms=0)


def table_of(path: Path):
    return iceberg_from_config(Config.from_json(path), "target").table()


def contract_of(table) -> tuple[dict, dict]:
    """The contract properties of a table, and the doc of each column."""
    properties = {key: table.properties[key] for key in PROPERTIES if key in table.properties}
    return properties, {item.name: item.doc for item in table.schema().fields}


def docs_of(**changes) -> contracts.Docs:
    return contracts.parse_docs(gold(**changes), who="analytics")


@pytest.fixture
def sales(tmp_path) -> Iceberg:
    """The Iceberg table ``gold.sales`` with two rows and no contract."""
    target = Iceberg("sales", {"identifier": "gold", "warehouse_path": str(tmp_path / "warehouse")})
    target.put(pa.table({"day": ["2026-08-01", "2026-08-02"], "revenue": [10.5, 20.0], "orders": [3, 4]}))
    return target


# --------------------------------------------------------------------------- the block


def test_a_config_without_docs_has_none():
    assert Config.from_dict(spec()).docs is None


def test_the_block_is_normalised_and_takes_its_defaults_from_the_config():
    block = {"layer": "silver", "description": "  Orders.  ", "primary_key": "id", "columns": {"id": " The id. "},
             "tags": ["a", "b", "a"]}

    docs = Config.from_dict(spec(block, who="ops team", when="daily")).docs

    assert docs == contracts.Docs(layer="silver", description="Orders.", owner="ops team", grain=None,
                                  primary_key=("id",), columns={"id": "The id."}, tags=("a", "b"), refresh="daily")
    assert docs.gold is False


def test_what_the_block_says_wins_over_who_and_when():
    docs = Config.from_dict(spec({"owner": "finance", "refresh": "hourly"}, who="ops", when="daily")).docs

    assert (docs.owner, docs.refresh) == ("finance", "hourly")


def test_the_contract_sets_only_the_properties_it_has_a_value_for():
    full = docs_of(owner="finance")
    sparse = contracts.parse_docs({"description": "Orders."})

    assert full.properties() == {"comment": "Revenue for each day.", "ldp.owner": "finance", "ldp.layer": "gold",
                                 "ldp.grain": "one row per day", "ldp.primary-key": "day"}
    assert sparse.properties() == {"comment": "Orders."}
    assert docs_of(primary_key=["robot_id", "recorded_on"]).properties()["ldp.primary-key"] == "robot_id,recorded_on"


def test_the_docs_go_back_to_a_block_with_the_defaults_in_it():
    docs = contracts.parse_docs({"description": "Orders.", "tags": ["x"]}, who="ops", when="daily")

    assert docs.to_dict() == {"description": "Orders.", "owner": "ops", "tags": ["x"], "refresh": "daily"}


@pytest.mark.parametrize("block, path, fragment", [
    ("text", "", "must be an object, got str"),
    ({"layers": "gold"}, "", "unknown keys ['layers']"),
    ({"layer": "platinum"}, ".layer", "must be one of ['bronze', 'silver', 'gold']"),
    ({"description": ""}, ".description", "must be a non-empty string"),
    ({"owner": 5}, ".owner", "must be a non-empty string, got 5"),
    ({"grain": "  "}, ".grain", "must be a non-empty string"),
    ({"refresh": []}, ".refresh", "must be a non-empty string"),
    ({"primary_key": []}, ".primary_key", "non-empty list of column names"),
    ({"primary_key": ["a", ""]}, ".primary_key", "non-empty list of column names"),
    ({"columns": ["a"]}, ".columns", "must be an object of column name to description"),
    ({"columns": {"a": ""}}, ".columns.a", "must be a non-empty description"),
    ({"tags": "x"}, ".tags", "must be a list of non-empty strings"),
    ({"tags": ["ok", ""]}, ".tags", "must be a list of non-empty strings"),
])
def test_every_kind_of_mistake_is_named_with_its_path(block, path, fragment):
    problems = contracts.docs_problems(block)

    assert any(found == path and fragment in message for found, message in problems), problems


def test_a_null_is_no_value():
    assert contracts.docs_problems({"description": None, "owner": None, "columns": None, "tags": None}) == []
    assert contracts.docs_problems(None) == []


def test_a_gold_table_needs_a_description_a_grain_and_an_owner_and_every_gap_is_reported_at_once():
    problems = contracts.docs_problems({"layer": "gold"})

    assert [path for path, _ in problems] == [".description", ".grain", ".owner"]
    assert all("is required for a gold table" in message for _, message in problems)


def test_who_is_the_owner_of_a_gold_table_that_names_none():
    needs = {"layer": "gold", "description": "Orders.", "grain": "one row per order"}

    assert contracts.docs_problems(needs, who="ops") == []
    assert [path for path, _ in contracts.docs_problems(needs, who="  ")] == [".owner"]
    assert [path for path, _ in contracts.docs_problems(needs)] == [".owner"]


@pytest.mark.parametrize("layer", [None, "bronze", "silver"])
def test_a_table_of_another_layer_needs_nothing(layer):
    assert contracts.docs_problems({"layer": layer} if layer else {}) == []


def test_a_config_names_the_first_problem_of_its_block():
    with pytest.raises(ConfigError, match=r"metadata\.docs\.description: is required for a gold table"):
        Config.from_dict(spec({"layer": "gold"}, who=""))
    with pytest.raises(ConfigError, match=r"metadata\.docs\.layer: must be one of"):
        Config.from_dict(spec({"layer": "platinum"}))


# --------------------------------------------------------------------------- the spec


def test_validate_spec_reports_every_problem_of_the_block_at_once():
    errors = [str(error) for error in validate_spec(spec({"layer": "gold", "colums": {}, "tags": "x"}, who=""))]

    paths = [error.split(":")[0] for error in errors]
    assert sorted(paths) == ["metadata.docs", "metadata.docs.description", "metadata.docs.grain",
                             "metadata.docs.owner", "metadata.docs.tags"]


def test_a_complete_gold_block_is_valid_and_so_is_one_that_leaves_the_owner_to_who():
    assert validate_spec(spec(gold(owner="finance"))) == []
    assert validate_spec(spec(gold(), who="analytics")) == []


def test_ldp_schema_shows_the_block(capsys):
    assert main(["schema"]) == 0

    block = json.loads(capsys.readouterr().out)["$defs"]["docs"]
    assert set(block["properties"]) == set(contracts.DOCS_KEYS) and block["additionalProperties"] is False
    assert block["properties"]["layer"]["enum"] == ["bronze", "silver", "gold"]
    assert "Editing it does not change the spec hash" in block["description"]
    assert json_schema()["$defs"]["metadata"]["properties"]["docs"] == {"$ref": "#/$defs/docs"}


def test_the_json_schema_accepts_a_complete_block_and_rejects_a_gold_block_that_has_no_grain():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(json_schema())

    validator.validate(spec(gold()))
    assert list(validator.iter_errors(spec(gold(grain=None))))
    assert list(validator.iter_errors(spec({"layer": "platinum"})))
    assert list(validator.iter_errors(spec({"columns": {"a": ""}})))
    validator.validate(spec({"layer": "silver"}))


def test_editing_the_docs_does_not_change_the_spec_hash():
    base = spec(gold())
    changed = spec(gold(description="Another.", owner="finance", columns={"day": "Other."}, tags=["x"],
                        primary_key=["day", "orders"], layer="silver"))

    assert spec_hash(base) == spec_hash(changed) == spec_hash(spec())
    assert "docs" not in canonical_spec(base)["metadata"]
    assert spec_hash(spec(gold(), mode="overwrite")) != spec_hash(base), "what a run writes still counts"


def test_a_column_called_token_is_described_and_not_taken_for_a_secret():
    block = gold(columns={**COLUMNS, "api_token": "The token of the caller.", "password_hash": "The hash."})

    assert validate_spec(spec(block)) == []


def test_a_key_pasted_into_a_description_is_still_caught():
    errors = validate_spec(spec(gold(description="-----BEGIN PRIVATE KEY-----")))

    assert any("looks like an inline secret" in str(error) for error in errors)


# --------------------------------------------------------------------------- the check


def test_a_complete_gold_contract_holds():
    assert contracts.check_contract(docs_of(), ["day", "revenue", "orders"]) == []


def test_one_message_lists_every_column_without_a_description_and_every_column_the_table_does_not_have():
    schema = pa.schema([("day", pa.string()), ("revenue", pa.float64()), ("orders", pa.int64())])

    problems = contracts.check_contract(docs_of(columns={"day": "The day.", "reveneu": "A typo."}), schema)

    assert problems == ["2 columns written without a description in metadata.docs.columns: revenue, orders",
                        "metadata.docs.columns describes 1 column that the table does not have: "
                        "reveneu (did you mean revenue?)"]


def test_a_column_in_another_case_is_pointed_out():
    docs = docs_of(columns={**COLUMNS, "Revenue": "The same."})

    problems = contracts.check_contract(docs, ["day", "revenue", "orders"])

    assert problems == ["metadata.docs.columns describes 1 column that the table does not have: "
                        "Revenue (did you mean revenue?)"]


def test_a_column_that_the_table_keeps_may_be_described_though_the_data_no_longer_has_it():
    docs = docs_of(columns={**COLUMNS, "legacy": "Kept for old reports."})

    assert contracts.check_contract(docs, ["day", "revenue", "orders"]) == [
        "metadata.docs.columns describes 1 column that the table does not have: legacy"]
    assert contracts.check_contract(docs, ["day", "revenue", "orders"], existing=["day", "legacy"]) == []


@pytest.mark.parametrize("docs", [None, contracts.Docs(layer="silver"), contracts.Docs(),
                                  contracts.Docs(layer="bronze")])
def test_only_a_gold_table_is_held_to_its_contract(docs):
    assert contracts.check_contract(docs, ["anything"]) == []


def test_the_columns_of_a_schema_come_from_pyarrow_an_iceberg_table_or_a_list(sales):
    assert contracts.column_names(pa.schema([("a", pa.int64()), ("b", pa.string())])) == ["a", "b"]
    assert contracts.column_names(sales.table().schema()) == ["day", "revenue", "orders"]
    assert contracts.column_names(("x", "y")) == ["x", "y"]
    with pytest.raises(TypeError, match="not a string"):
        contracts.column_names("day")


def test_the_error_is_a_data_gate_that_lists_what_is_wrong():
    error = contracts.contract_error("gold.sales", ["one thing", "another"])

    assert isinstance(error, ContractError) and isinstance(error, DataQualityError)
    assert error.problems == ["one thing", "another"]
    assert str(error) == ("gold.sales does not meet its gold contract, and nothing was written:\n"
                          "  - one thing\n  - another")
    assert is_retriable(error) is False


# --------------------------------------------------------------------------- the commit


def test_the_contract_is_one_metadata_only_commit(sales):
    table = sales.table()
    snapshots, log, schema_id = len(table.snapshots()), len(table.metadata.metadata_log), table.schema().schema_id

    result = contracts.apply_contract(table, docs_of())

    after = sales.table()
    assert result.changed and result.attempts == 1 and result.documented == 3 and result.ignored == ()
    assert result.properties == PROPERTIES and result.columns == ("day", "revenue", "orders")
    assert len(after.snapshots()) == snapshots, "no snapshot is added"
    assert len(after.metadata.metadata_log) == log + 1, "one commit holds the properties and the docs"
    assert after.schema().schema_id == schema_id + 1
    assert contract_of(after) == (
        {"comment": "Revenue for each day.", "ldp.owner": "analytics", "ldp.layer": "gold",
         "ldp.grain": "one row per day", "ldp.primary-key": "day"}, COLUMNS)
    assert [item.field_id for item in after.schema().fields] == [item.field_id for item in table.schema().fields]
    assert sales.get().num_rows == 2


def test_a_table_that_has_the_contract_gets_no_commit(sales):
    contracts.apply_contract(sales.table(), docs_of())
    log = len(sales.table().metadata.metadata_log)

    again = contracts.apply_contract(sales.table(), docs_of())

    assert again.changed is False and again.properties == () and again.columns == () and again.documented == 3
    assert len(sales.table().metadata.metadata_log) == log


def test_only_what_differs_is_committed(sales):
    contracts.apply_contract(sales.table(), docs_of())

    renamed = contracts.apply_contract(sales.table(), docs_of(description="Takings for each day."))
    assert renamed.properties == ("comment",) and renamed.columns == ()
    same_but_orders = {**COLUMNS, "orders": "The number of orders."}
    reworded = contracts.apply_contract(sales.table(),
                                        docs_of(description="Takings for each day.", columns=same_but_orders))
    assert reworded.properties == () and reworded.columns == ("orders",)

    properties, docs = contract_of(sales.table())
    assert properties["comment"] == "Takings for each day." and docs["orders"] == "The number of orders."


def test_it_only_adds_and_changes_text_and_never_takes_anything_off(sales):
    table = sales.table()
    with table.transaction() as transaction:
        transaction.set_properties({"ldp.primary-key": "day", "owner-team": "kept", "write.something": "kept"})
    contracts.apply_contract(sales.table(), contracts.parse_docs({"description": "Orders.", "columns": {"day": "D."}}))

    contracts.apply_contract(sales.table(), contracts.parse_docs({"owner": "finance"}))

    properties = sales.table().properties
    assert properties["comment"] == "Orders." and properties["ldp.owner"] == "finance"
    assert properties["ldp.primary-key"] == "day" and properties["owner-team"] == "kept"
    assert contract_of(sales.table())[1]["day"] == "D."


def test_a_table_that_is_not_gold_ignores_a_column_it_does_not_have(sales):
    docs = contracts.parse_docs({"layer": "silver", "columns": {"day": "The day.", "missing": "Nothing."}})

    result = contracts.apply_contract(sales.table(), docs)

    assert result.columns == ("day",) and result.ignored == ("missing",)
    assert contract_of(sales.table())[1] == {"day": "The day.", "revenue": None, "orders": None}


def test_a_dry_run_says_what_would_change_and_changes_nothing(sales):
    log = len(sales.table().metadata.metadata_log)

    result = contracts.apply_contract(sales.table(), docs_of(), dry_run=True)

    assert result.changed and result.properties == PROPERTIES and result.documented == 3
    assert len(sales.table().metadata.metadata_log) == log
    assert contract_of(sales.table())[0] == {}


def test_a_lost_commit_race_reloads_the_table_and_works_the_difference_out_again(sales):
    stale = sales.table()
    contracts.apply_contract(sales.table(), contracts.parse_docs({"columns": {"orders": "Counted by the till."}}))
    pauses = []

    result = contracts.apply_contract(stale, contracts.parse_docs({"columns": {"revenue": "What it cost."}}),
                                      sleep=pauses.append)

    assert result.attempts == 2 and len(pauses) == 1 and pauses[0] >= 0
    assert contract_of(sales.table())[1] == {"day": None, "revenue": "What it cost.", "orders": "Counted by the till."}


class AlwaysRefused:
    """A table whose catalog refuses every commit."""

    properties: dict = {}

    def __init__(self):
        from pyiceberg.exceptions import CommitFailedException

        self.refused = CommitFailedException
        self.commits = 0
        self.reloads = 0

    def name(self):
        return ("gold", "sales")

    def schema(self):
        from pyiceberg.schema import Schema
        from pyiceberg.types import NestedField, StringType

        return Schema(NestedField(1, "day", StringType(), required=False))

    def transaction(self):
        self.commits += 1
        raise self.refused("the table changed")

    def refresh(self):
        self.reloads += 1


def test_when_the_catalog_keeps_refusing_it_stops_as_the_policy_says_and_the_error_may_be_tried_again():
    table, pauses = AlwaysRefused(), []

    with pytest.raises(CommitConflict) as error:
        contracts.apply_contract(table, docs_of(), policy=CommitPolicy(max_publish_attempts=3), sleep=pauses.append)

    assert table.commits == 3 and len(pauses) == 2 and table.reloads == 2
    assert error.value.retriable is True
    assert "gold.sales after 3 attempts" in str(error.value) and "ldp gold sync" in str(error.value)


# --------------------------------------------------------------------------- a run


def test_a_run_publishes_the_contract_with_the_data_and_says_so_in_its_events(tmp_path):
    path = write_config(tmp_path, gold())
    sink = MemorySink()

    result = run(path, sink=sink)

    table = table_of(path)
    assert result.status == "published" and result.rows_written == 2
    assert len(table.snapshots()) == 1, "the contract adds no snapshot"
    assert contract_of(table) == ({"comment": "Revenue for each day.", "ldp.owner": "analytics",
                                   "ldp.layer": "gold", "ldp.grain": "one row per day",
                                   "ldp.primary-key": "day"}, COLUMNS)
    [published] = [event for event in sink.events if event.type == "run.published"]
    assert published.payload["contract"] == {
        "layer": "gold", "applied": True, "changed": True, "properties": list(PROPERTIES),
        "columns": ["day", "revenue", "orders"], "documented": 3, "ignored": [], "attempts": 1}


def test_a_second_run_finds_the_contract_in_place_and_commits_nothing_for_it(tmp_path):
    path = write_config(tmp_path, gold())
    run(path)
    sink = MemorySink()

    run(path, sink=sink)

    table = table_of(path)
    assert len(table.snapshots()) == 2
    assert len(table.metadata.metadata_log) == 3, "two appends and one contract, and none for it the second time"
    [published] = [event for event in sink.events if event.type == "run.published"]
    assert published.payload["contract"]["changed"] is False and published.payload["contract"]["columns"] == []


def test_the_staged_protocol_writes_the_contract_too_and_a_duplicate_touches_nothing(tmp_path):
    path = write_config(tmp_path, gold())
    plain = write_config(tmp_path, None, name="plain_sales")
    run(plain, commit=staged())
    baseline = len(table_of(plain).snapshots())

    first = run(path, commit=staged())
    table = table_of(path)
    log = len(table.metadata.metadata_log)
    sink = MemorySink()
    again = run(path, commit=staged(run_id="r2"), sink=sink)

    assert first.status == "published" and again.status == "skipped_duplicate"
    assert contract_of(table)[0]["ldp.layer"] == "gold" and contract_of(table)[1] == COLUMNS
    assert len(table.snapshots()) == baseline, "the same snapshots as a run with no contract"
    assert len(table_of(path).metadata.metadata_log) == log, "a duplicate writes nothing"
    assert not [event for event in sink.events if "contract" in event.payload]


def test_a_gold_table_with_a_column_that_has_no_description_is_stopped_before_anything_is_written(tmp_path):
    path = write_config(tmp_path, gold(columns={"day": "The day.", "revenue": "What the orders cost."}))
    sink = MemorySink()

    with pytest.raises(ContractError) as error:
        run(path, sink=sink)

    assert str(error.value) == ("gold.daily_sales does not meet its gold contract, and nothing was written:\n"
                                "  - 1 column written without a description in metadata.docs.columns: orders")
    assert isinstance(error.value, DataQualityError)
    assert not (tmp_path / "warehouse").exists() or not any((tmp_path / "warehouse").rglob("*.metadata.json"))
    assert sink.types == ["run.started", "run.extracted", "quality.evaluated", "run.blocked_quality", "run.finished"]
    [blocked] = [event for event in sink.events if event.type == "run.blocked_quality"]
    assert blocked.payload["failures"] == ["contract"] and blocked.payload["checks_failed"] == 1
    assert blocked.payload["contract_problems"] == error.value.problems
    [finished] = [event for event in sink.events if event.type == "run.finished"]
    assert finished.payload["status"] == "blocked_quality" and finished.payload["contract_passed"] is False
    assert finished.payload["rows_written"] == 0


def test_a_description_of_a_column_that_is_not_there_stops_the_run_and_the_message_has_a_hint(tmp_path):
    path = write_config(tmp_path, gold(columns={**COLUMNS, "ordrs": "A typo."}))

    with pytest.raises(ContractError, match=r"does not have: ordrs \(did you mean orders\?\)"):
        run(path)


def test_a_failing_check_is_reported_first_and_the_contract_waits_for_the_next_run(tmp_path):
    quality = {"on_failure": "fail", "checks": [{"check": "row_count", "min": 100}]}
    path = write_config(tmp_path, gold(columns={"day": "The day."}), quality=quality)
    sink = MemorySink()

    with pytest.raises(DataQualityError) as error:
        run(path, sink=sink)

    assert not isinstance(error.value, ContractError)
    [blocked] = [event for event in sink.events if event.type == "run.blocked_quality"]
    assert "contract_problems" not in blocked.payload and blocked.payload["failures"] == ["row_count"]


def test_a_check_that_only_warns_does_not_let_a_broken_contract_through(tmp_path):
    quality = {"on_failure": "warn", "checks": [{"check": "row_count", "min": 100}]}
    path = write_config(tmp_path, gold(columns={"day": "The day."}), quality=quality)

    with pytest.raises(ContractError):
        run(path)


def test_a_run_that_a_check_blocks_leaves_the_docs_of_the_table_as_they_were(tmp_path):
    path = write_config(tmp_path, gold())
    run(path)
    before = contract_of(table_of(path))
    quality = {"on_failure": "fail", "checks": [{"check": "row_count", "min": 100}]}
    write_config(tmp_path, gold(description="Changed."), quality=quality)

    with pytest.raises(DataQualityError):
        run(path)

    assert contract_of(table_of(path)) == before


def test_a_table_may_describe_a_column_that_it_keeps_when_the_new_data_no_longer_has_it(tmp_path):
    path = write_config(tmp_path, gold(columns={**COLUMNS, "orders": "How many orders."}))
    run(path)
    shorter = "day,revenue\n2026-08-03,5.0\n"
    write_config(tmp_path, gold(), csv=shorter)  # the config still says nothing about a column that is gone

    result = run(path)

    assert result.status == "published"
    assert contract_of(table_of(path))[1] == COLUMNS


def test_a_new_column_is_documented_in_the_run_that_adds_it(tmp_path):
    path = write_config(tmp_path, gold(columns={**COLUMNS, "tip": "What the customers added."}),
                        csv="day,revenue,orders,tip\n2026-08-01,10.5,3,1.0\n")
    older = tmp_path / "older"
    older.mkdir()
    first = write_config(older, gold(), csv=SALES)
    run(first)
    # the same table gets the data with the new column and the new description
    (older / "sales.csv").write_text("day,revenue,orders,tip\n2026-08-03,5.0,1,0.5\n")
    described = gold(columns={**COLUMNS, "tip": "What the customers added."})
    (older / "daily_sales.json").write_text(json.dumps(spec(described)))

    run(first)

    assert contract_of(table_of(first))[1] == {**COLUMNS, "tip": "What the customers added."}
    assert path.exists()


def test_a_table_that_is_not_gold_runs_with_the_docs_it_has(tmp_path):
    path = write_config(tmp_path, {"layer": "silver", "description": "Daily sales.",
                                   "columns": {"day": "The day.", "missing": "Not here."}})
    sink = MemorySink()

    result = run(path, sink=sink)

    assert result.status == "published"
    assert contract_of(table_of(path)) == ({"comment": "Daily sales.", "ldp.owner": "analytics", "ldp.layer": "silver"},
                                           {"day": "The day.", "revenue": None, "orders": None})
    [published] = [event for event in sink.events if event.type == "run.published"]
    assert published.payload["contract"]["ignored"] == ["missing"]


def test_a_config_without_docs_runs_as_before_and_its_events_have_no_contract(tmp_path):
    path = write_config(tmp_path)
    sink = MemorySink()

    run(path, sink=sink)

    assert contract_of(table_of(path)) == ({}, {"day": None, "revenue": None, "orders": None})
    assert not [event for event in sink.events if "contract" in event.payload]


def test_a_file_target_has_nowhere_to_keep_a_contract_and_runs_on(tmp_path):
    (tmp_path / "sales.csv").write_text(SALES)
    config = Config.from_dict(spec(gold()), base_dir=tmp_path)
    sink = MemorySink()

    result = Pipeline(config, source=CSV("sales", tmp_path / "sales.csv"), target=CSV("out", tmp_path / "out.csv"),
                      sink=sink).run()

    assert result.status == "published" and (tmp_path / "out.csv").is_file()
    assert not [event for event in sink.events if "contract" in event.payload]
    broken = Config.from_dict(spec(gold(columns={"day": "The day."})), base_dir=tmp_path)
    with pytest.raises(ContractError):
        Pipeline(broken, source=CSV("sales", tmp_path / "sales.csv"), target=CSV("out", tmp_path / "again.csv")).run()
    assert not (tmp_path / "again.csv").exists()


def test_a_contract_that_cannot_be_written_after_the_publish_is_reported_and_does_not_fail_the_run(
        tmp_path, monkeypatch, caplog):
    path = write_config(tmp_path, gold())

    def refuse(*arguments, **keywords):
        raise OSError("the catalog went away")

    monkeypatch.setattr(contracts, "apply_contract", refuse)
    sink = MemorySink()

    result = run(path, sink=sink)

    assert result.status == "published" and table_of(path).current_snapshot() is not None
    [published] = [event for event in sink.events if event.type == "run.published"]
    assert published.payload["contract"] == {"layer": "gold", "applied": False, "error": "the catalog went away"}
    assert "could not write its contract" in caplog.text


# --------------------------------------------------------------------------- the commands and the workflow


def test_ldp_run_exits_with_3_for_a_broken_gold_contract(tmp_path, capsys):
    path = write_config(tmp_path, gold(columns={"day": "The day."}))

    assert main(["run", str(path)]) == 3

    error = capsys.readouterr().err
    assert "does not meet its gold contract" in error and "revenue, orders" in error


def test_a_workflow_marks_the_task_blocked_and_does_not_try_it_again(tmp_path):
    write_config(tmp_path, gold(columns={"day": "The day."}))
    workflow = tmp_path / "daily.json"
    workflow.write_text(json.dumps({
        "apiVersion": "ldp/v1", "kind": "Workflow", "identifier": "daily",
        "metadata": {"window": {"size": "day"}, "retries": {"max_attempts": 3, "delay_s": 0}},
        "tasks": [{"id": "sales", "pipeline": "daily_sales.json"}]}))

    result = run_workflow(workflow, window="2026-08-01/2026-08-02")

    assert result.tasks["sales"].status == "blocked_quality" and result.tasks["sales"].attempts == 1
    assert result.exit_code == 3


def test_gold_sync_writes_the_contract_of_a_table_that_exists_without_writing_data(tmp_path, capsys):
    path = write_config(tmp_path, None)
    run(path)
    path = write_config(tmp_path, gold())
    snapshots = len(table_of(path).snapshots())

    assert main(["gold", "sync", str(path)]) == 0

    assert capsys.readouterr().out == (
        "daily_sales: wrote the contract of gold.daily_sales, 5 properties and 3 column docs. No data was written.\n")
    table = table_of(path)
    assert len(table.snapshots()) == snapshots and contract_of(table)[1] == COLUMNS
    assert contract_of(table)[0]["ldp.layer"] == "gold"


def test_gold_sync_has_nothing_to_do_when_the_table_has_the_contract(tmp_path, capsys):
    path = write_config(tmp_path, gold())
    run(path)
    capsys.readouterr()
    log = len(table_of(path).metadata.metadata_log)

    assert main(["gold", "sync", str(path)]) == 0

    assert capsys.readouterr().out == (
        "daily_sales: gold.daily_sales already has its contract, with 3 columns described. Nothing was written.\n")
    assert len(table_of(path).metadata.metadata_log) == log


def test_gold_sync_dry_run_says_what_it_would_write(tmp_path, capsys):
    path = write_config(tmp_path, None)
    run(path)
    path = write_config(tmp_path, gold())

    assert main(["gold", "sync", str(path), "--dry-run"]) == 0

    assert "would write the contract of gold.daily_sales, 5 properties and 3 column docs" in capsys.readouterr().out
    assert contract_of(table_of(path))[0] == {}


def test_gold_sync_can_print_json(tmp_path, capsys):
    path = write_config(tmp_path, None)
    run(path)
    path = write_config(tmp_path, gold())

    assert main(["gold", "sync", str(path), "--json"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result == {"config": "daily_sales", "table": "gold.daily_sales", "layer": "gold", "dry_run": False,
                      "changed": True, "properties": list(PROPERTIES), "columns": ["day", "revenue", "orders"],
                      "documented": 3, "ignored": [], "attempts": 1}


def test_gold_sync_stops_a_gold_table_whose_columns_are_not_all_described_with_a_3(tmp_path, capsys):
    path = write_config(tmp_path, None)
    run(path)
    path = write_config(tmp_path, gold(columns={"day": "The day."}))

    assert main(["gold", "sync", str(path)]) == 3

    assert "daily_sales" not in capsys.readouterr().out
    assert contract_of(table_of(path))[0] == {}


def test_gold_sync_warns_about_a_column_that_a_table_of_another_layer_does_not_have(tmp_path, capsys):
    path = write_config(tmp_path, None)
    run(path)
    path = write_config(tmp_path, {"layer": "silver", "columns": {"day": "The day.", "nope": "Not there."}})

    assert main(["gold", "sync", str(path)]) == 0

    captured = capsys.readouterr()
    assert "warning: metadata.docs.columns describes 1 column that gold.daily_sales does not have: nope" in captured.err
    assert "wrote the contract" in captured.out


@pytest.mark.parametrize("make, message", [
    (lambda root: write_config(root, gold()), "does not exist"),
    (lambda root: (run(write_config(root, None)), write_config(root, None))[1], "has no metadata.docs"),
])
def test_gold_sync_says_what_is_missing_and_exits_with_1(tmp_path, capsys, make, message):
    path = make(tmp_path)

    assert main(["gold", "sync", str(path)]) == 1

    assert message in capsys.readouterr().err


def test_gold_sync_needs_an_iceberg_table(tmp_path, capsys):
    (tmp_path / "sales.csv").write_text(SALES)
    document = {"identifier": "export", "metadata": {
        "source": {"name": "sales", "format": "CSV", "path": "sales.csv"},
        "target": {"name": "out", "format": "CSV", "path": "out.csv"}, "docs": {"description": "Sales."}}}
    path = tmp_path / "export.json"
    path.write_text(json.dumps(document))

    assert main(["gold", "sync", str(path)]) == 1

    assert "only an Iceberg table can carry a contract" in capsys.readouterr().err


def test_ldp_gold_with_no_command_shows_its_help(capsys):
    assert main(["gold"]) == 1

    assert "sync" in capsys.readouterr().out


# --------------------------------------------------------------------------- reading a contract back


def test_a_table_that_carries_no_contract_reads_back_none(sales):
    assert contracts.read_contract(sales.table()) is None


def test_a_contract_that_was_written_is_read_back_as_it_was_written(sales):
    contracts.apply_contract(sales.table(), docs_of(owner="finance"))

    assert contracts.read_contract(sales.table()) == contracts.Docs(
        layer="gold", description="Revenue for each day.", owner="finance", grain="one row per day",
        primary_key=("day",), columns=COLUMNS)


def test_the_tags_and_the_refresh_are_only_in_the_config_so_they_are_not_read_back(sales):
    docs = docs_of(tags=["finance"], refresh="daily")
    contracts.apply_contract(sales.table(), docs)

    read = contracts.read_contract(sales.table())

    assert docs.tags == ("finance",) and docs.refresh == "daily"
    assert read.tags == () and read.refresh is None


def test_a_key_of_several_columns_is_read_back_in_order_whatever_the_spaces(sales):
    contracts.apply_contract(sales.table(), docs_of(primary_key=["day", "orders"]))
    assert contracts.read_contract(sales.table()).primary_key == ("day", "orders")

    with sales.table().transaction() as transaction:
        transaction.set_properties({contracts.PRIMARY_KEY: " day, orders ,, revenue "})
    assert contracts.read_contract(sales.table()).primary_key == ("day", "orders", "revenue")


def test_a_table_that_carries_part_of_a_contract_reads_back_that_part(sales):
    with sales.table().transaction() as transaction:
        transaction.set_properties({contracts.COMMENT: "Only a meaning."})

    assert contracts.read_contract(sales.table()) == contracts.Docs(description="Only a meaning.")
    with sales.table().transaction() as transaction:
        with transaction.update_schema() as update:
            update.update_column("day", doc="The day.")
    assert contracts.read_contract(sales.table()).columns == {"day": "The day."}


def test_properties_and_docs_that_are_only_blank_are_no_contract(sales):
    with sales.table().transaction() as transaction:
        transaction.set_properties({contracts.OWNER: "   ", contracts.GRAIN: ""})
        with transaction.update_schema() as update:
            update.update_column("day", doc="  ")

    assert contracts.read_contract(sales.table()) is None


@pytest.mark.parametrize("declared, properties, namespace, expected", [
    ("silver", {"ldp.layer": "gold"}, "gold", "silver"),  # the config of the table first
    (None, {"ldp.layer": "gold"}, "silver", "gold"),  # then what the table says of itself
    (None, {}, "silver", "silver"),  # then a namespace that is called after a layer
    (None, None, "Gold", "gold"),
    (None, {}, " bronze ", "bronze"),
    (None, {}, "robots", None),
    (None, {}, "gold.sub", None),  # a namespace of several parts is not the name of a layer
    (None, {}, None, None),
    ("  ", {"ldp.layer": " "}, "bronze", "bronze"),  # blank text says nothing
    ("platinum", {}, "gold", "platinum"),  # it is not judged here, the block of a config is checked when it is read
])
def test_the_layer_of_a_table_is_the_first_of_what_its_config_says_what_it_says_and_its_namespace(
        declared, properties, namespace, expected):
    assert contracts.resolve_layer(declared, properties, namespace) == expected


# --------------------------------------------------------------------------- a table that dbt built


def manifest(*models) -> dict:
    return {"nodes": {node["unique_id"]: node for node in models}}


def model(name, *, alias=None, meta=None, description="", columns=None, tags=(), unique_key=None) -> dict:
    return {"unique_id": f"model.shop.{name}", "resource_type": "model", "name": name, "alias": alias or name,
            "description": description, "meta": dict(meta or {}), "tags": list(tags),
            "config": {"meta": {}, "unique_key": unique_key},
            "columns": {column: {"name": column, "description": text} for column, text in (columns or {}).items()}}


def test_the_contract_of_a_table_that_dbt_built_comes_from_its_manifest(tmp_path, sales, capsys):
    document = manifest(
        model("other", columns={"day": "Not this one."}),
        model("sales", meta={"owner": "finance", "grain": "one row per day"}, description="Revenue for each day.",
              columns=COLUMNS, tags=["finance"], unique_key="day"))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(document))
    config = tmp_path / "sales.json"
    config.write_text(json.dumps(spec(None, name="sales", who="")))  # a pointer to the table, and no docs of its own
    before = set(sys.modules)

    assert main(["gold", "sync", str(config), "--dbt-manifest", str(manifest_path)]) == 0

    assert not {name for name in set(sys.modules) - before if name == "dbt" or name.startswith("dbt.")}, \
        "the manifest is read as JSON, and dbt is not imported"
    properties, docs = contract_of(sales.table())
    assert docs == COLUMNS
    assert properties == {"comment": "Revenue for each day.", "ldp.owner": "finance", "ldp.layer": "gold",
                          "ldp.grain": "one row per day", "ldp.primary-key": "day"}
    assert "wrote the contract of gold.sales" in capsys.readouterr().out


def test_what_the_config_says_is_written_over_what_the_manifest_says(tmp_path, sales):
    document = manifest(model("sales", description="From dbt.", columns={"day": "From dbt.", "orders": "Orders."},
                              meta={"owner": "dbt team", "grain": "one row per day"}, tags=["dbt"]))
    (tmp_path / "manifest.json").write_text(json.dumps(document))
    config = tmp_path / "sales.json"
    config.write_text(json.dumps(spec({"layer": "gold", "description": "From the config.", "tags": ["cfg"],
                                       "grain": "one row per day",
                                       "columns": {"day": "The day.", "revenue": "Money."}},
                                      name="sales", who="analytics")))

    result = sync_config(config, dbt_manifest=tmp_path / "manifest.json")

    assert result.docs.description == "From the config." and result.docs.owner == "dbt team"
    assert result.docs.columns == {"day": "The day.", "orders": "Orders.", "revenue": "Money."}
    assert result.docs.tags == ("dbt", "cfg")
    assert contract_of(sales.table())[1] == {"day": "The day.", "revenue": "Money.", "orders": "Orders."}


def test_the_manifest_has_to_have_exactly_one_model_for_the_table():
    document = manifest(model("a", alias="sales"), model("b", alias="sales"), model("c"))

    with pytest.raises(ConfigError, match=r"has no model that builds a table called 'nothing'"):
        find_model(document, "nothing")
    with pytest.raises(ConfigError, match=r"2 models that build a table called 'sales': model\.shop\.a, "):
        find_model(document, "sales")
    assert find_model(document, "c")["name"] == "c"


@pytest.mark.parametrize("content, message", [(None, "cannot read the dbt manifest"), ("{nope", "is not valid JSON")])
def test_a_manifest_that_cannot_be_read_is_named(tmp_path, sales, content, message):
    config = tmp_path / "sales.json"
    config.write_text(json.dumps(spec({"layer": "silver"}, name="sales")))
    manifest_path = tmp_path / "manifest.json"
    if content is not None:
        manifest_path.write_text(content)

    with pytest.raises(ConfigError, match=message):
        sync_config(config, dbt_manifest=manifest_path)


def test_a_dbt_model_stands_for_a_block_with_its_descriptions_its_meta_and_its_tags():
    node = model("sales", meta={"owner": "finance", "grain": "one row per day", "ldp_publish": True},
                 description="  Revenue for each day.  ", columns={**COLUMNS, "blank": "  "},
                 tags=["finance", "finance"], unique_key=["day", "orders"])

    block = contracts.docs_from_dbt_node(node, namespace="gold")

    assert block == {"layer": "gold", "description": "Revenue for each day.", "owner": "finance",
                     "grain": "one row per day", "primary_key": ["day", "orders"], "columns": COLUMNS,
                     "tags": ["finance"]}
    assert contracts.parse_docs(block).gold


def test_the_layer_of_a_dbt_model_is_its_meta_else_its_namespace_when_that_is_a_layer():
    assert contracts.docs_from_dbt_node(model("m"), namespace="Silver") == {"layer": "silver"}
    assert contracts.docs_from_dbt_node(model("m"), namespace="lake") == {}
    assert contracts.docs_from_dbt_node(model("m"), namespace=None) == {}
    bronze = model("m", meta={"ldp_layer": "bronze"})
    assert contracts.docs_from_dbt_node(bronze, namespace="gold") == {"layer": "bronze"}


def test_a_key_that_the_caller_gives_wins_over_the_unique_key_and_the_meta_of_the_config_block_counts_too():
    node = model("m", unique_key="id")
    node["config"]["meta"] = {"owner": "from the config block"}

    block = contracts.docs_from_dbt_node(node, primary_key=("a", "b"))

    assert block == {"owner": "from the config block", "primary_key": ["a", "b"]}


def test_a_value_in_the_meta_that_is_wrong_goes_in_as_it_is_so_that_the_validation_names_it():
    block = contracts.docs_from_dbt_node(model("m", meta={"owner": 5, "ldp_layer": "platinum"}))

    assert block == {"layer": "platinum", "owner": 5}
    assert [path for path, _ in contracts.docs_problems(block)] == [".layer", ".owner"]
