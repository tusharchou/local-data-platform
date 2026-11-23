"""A sheet that is shared by link, kept in sync on a schedule: the https source, and ``ldp schedule tick`` that runs it.

A shop owner records the sales of each day in a Google Sheet. The export link of a tab is a CSV source, the target is an
Iceberg table that is upserted on the key columns of the sheet, and ``"schedule": "@daily"`` makes ``ldp schedule tick``
read the sheet again every day. The server here is on 127.0.0.1, where plain http is allowed, and it answers as Google
does, with a redirect from the export link to the file. Every test names the time of its tick with ``now``.
"""

import datetime as dt
import json
import logging
from pathlib import Path

import pytest

from local_data_platform import spec
from local_data_platform.cli import main
from local_data_platform.format.iceberg import Iceberg
from local_data_platform.orchestration import schedule as schedule_module
from local_data_platform.spec import kind_schema, plan, validate_spec

DAY = dt.date(2026, 8, 4)
SHEET = ("day,dish,orders,revenue\n"
         "2026-07-01,pad thai,12,143.5\n"
         "2026-07-02,green curry,9,117.0\n")
SECRET = "S3CR3T-VALUE"
CATALOG = {"identifier": "shop", "warehouse_path": "../warehouse"}


# ====================================================================== the spec of a sheet


def _spec(path, *, target=None, **source):
    return {
        "identifier": "sheet_sales",
        "what": "The daily sales of the shop, from its sheet",
        "metadata": {
            "source": {"name": "sales", "format": "CSV", "path": path, **source},
            "target": target or {"name": "sales", "format": "ICEBERG", "catalog": CATALOG, "write_mode": "upsert",
                                 "join_cols": ["day", "dish"]},
            "schedule": "@daily",
        },
    }


def _problems(data):
    return [str(error) for error in validate_spec(data)]


GOOGLE = "https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=0"


@pytest.mark.parametrize("path", [
    GOOGLE,
    "https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=1234567890",
    "https://example.com/sales.csv",
    "https://example.com:8443/sales.csv#gid=0",
    "https://example.com/orders_{window_start:%Y-%m-%d}.csv",
    "http://127.0.0.1:8000/sales.csv",
    "http://localhost:8000/sales.csv?format=csv",
    "https://example.com/export?format=csv&gid=0&single=true&output=csv&range=A1:D100",
    "https://example.com/export?primary_key=id&keyword=pad+thai",
])
def test_an_address_that_may_be_read_is_valid(path):
    assert _problems(_spec(path)) == []


@pytest.mark.parametrize("path, words", [
    ("http://example.com/sales.csv", "the address is plain http, which is only for 127.0.0.1 and localhost"),
    ("http://192.168.1.5/sales.csv", "the address is plain http"),
    ("https://user:pw@example.com/sales.csv", "the address has a user name or a password in it"),
    ("https://token@example.com/sales.csv", "the address has a user name or a password in it"),
    ("https://example.com:99999/sales.csv", "the address is not valid"),
    ("https:///sales.csv", "the address has no host name"),
    ("https://example.com/sales csv", "the address has a space, a line break or another control character in it"),
    ("https://example.com/sales.csv\n", "the address has a space, a line break or another control character in it"),
])
def test_an_address_that_may_not_be_read_is_a_problem_of_the_source_path(path, words):
    problems = _problems(_spec(path))

    assert len(problems) == 1 and problems[0].startswith("metadata.source.path: ") and words in problems[0]
    assert "pw@" not in problems[0]


@pytest.mark.parametrize("name", ["token", "key", "api_key", "apikey", "access_token", "sig", "signature", "password",
                                  "secret", "auth", "pwd", "X-Amz-Signature", "X-Amz-Credential", "private_key"])
def test_a_secret_in_the_query_of_an_address_is_an_inline_secret(name):
    problems = _problems(_spec(f"https://example.com/export?format=csv&{name}={SECRET}"))

    assert len(problems) == 1
    assert problems[0].startswith("metadata.source.path: looks like an inline secret, in its parameter ")
    assert f"parameter '{name}'" in problems[0]
    assert "anyone with the link can read" in problems[0]
    assert SECRET not in problems[0]


def test_a_secret_in_the_fragment_of_an_address_is_an_inline_secret():
    problems = _problems(_spec(f"https://example.com/sales.csv#access_token={SECRET}"))

    assert len(problems) == 1 and "parameter 'access_token'" in problems[0] and SECRET not in problems[0]


def test_an_address_with_two_problems_says_both():
    problems = _problems(_spec(f"http://example.com/sales.csv?token={SECRET}"))

    assert len(problems) == 2
    assert any("plain http" in text for text in problems) and any("inline secret" in text for text in problems)


def test_validating_a_spec_with_an_address_makes_no_request(web_server):
    assert _problems(_spec(f"{web_server.url}/sales.csv?format=csv")) == []
    assert _problems(_spec(f"{web_server.url}/sales.csv?token={SECRET}"))

    assert web_server.seen == []


def test_an_address_is_not_a_target():
    target = {"name": "sales", "format": "CSV", "path": "https://example.com/sales.csv"}

    problems = _problems({"identifier": "x", "metadata": {"source": {"name": "s", "format": "CSV", "path": "in.csv"},
                                                          "target": target}})

    assert problems == ["metadata.target.path: is an http(s) address, which can be read and not written, so it cannot "
                        "be a target"]


@pytest.mark.parametrize("fmt, extra", [("PARQUET", {}), ("JSON", {}), ("XLSX", {}),
                                        ("DUCKDB", {"relation": "orders"})])
def test_only_a_csv_source_can_be_an_address(fmt, extra):
    problems = _problems(_spec("https://example.com/sales.data", **{"format": fmt, **extra}))

    assert "metadata.source.path: is an http(s) address, which only a CSV source can read" in problems


@pytest.mark.parametrize("timeout", [1, 30, 2.5, 0.001, 3600])
def test_a_timeout_of_a_source_that_is_an_address_is_valid(timeout):
    assert _problems(_spec(GOOGLE, timeout=timeout)) == []


@pytest.mark.parametrize("max_bytes", [1, 1000, 52428800, 10**12])
def test_a_max_bytes_of_a_source_that_is_an_address_is_valid(max_bytes):
    assert _problems(_spec(GOOGLE, max_bytes=max_bytes)) == []


@pytest.mark.parametrize("timeout", [0, -1, -0.5, True, "5", [5], float("inf"), float("nan")])
def test_a_timeout_has_to_be_a_number_of_seconds_above_zero(timeout):
    assert _problems(_spec(GOOGLE, timeout=timeout)) == [
        f"metadata.source.timeout: must be a number of seconds above 0, got {timeout!r}"]


@pytest.mark.parametrize("max_bytes", [0, -1, 1.5, 1.0, True, "100", [5], float("inf")])
def test_a_max_bytes_has_to_be_a_whole_number_of_bytes_above_zero(max_bytes):
    assert _problems(_spec(GOOGLE, max_bytes=max_bytes)) == [
        f"metadata.source.max_bytes: must be a whole number of bytes above 0, got {max_bytes!r}"]


@pytest.mark.parametrize("key, value", [("timeout", 5), ("max_bytes", 100)])
def test_a_limit_of_a_source_that_is_a_file_is_a_problem(key, value):
    assert _problems(_spec("sales.csv", **{key: value})) == [
        f"metadata.source.{key}: only applies to a CSV source whose path is an http(s) address"]


@pytest.mark.parametrize("key, value", [("timeout", 5), ("max_bytes", 100)])
def test_a_limit_of_a_source_that_is_not_a_csv_is_a_problem(key, value):
    problems = _problems(_spec("sales.parquet", format="PARQUET", **{key: value}))

    assert problems == [f"metadata.source.{key}: only applies to a CSV source whose path is an http(s) address"]


@pytest.mark.parametrize("key, value", [("timeout", 5), ("max_bytes", 100)])
def test_a_limit_of_a_target_is_a_problem(key, value):
    target = {"name": "sales", "format": "CSV", "path": "out.csv", key: value}

    problems = _problems({"identifier": "x", "metadata": {"source": {"name": "s", "format": "CSV", "path": GOOGLE},
                                                          "target": target}})

    assert problems == [f"metadata.target.{key}: only applies to a CSV source whose path is an http(s) address"]


def test_a_target_that_is_an_address_has_a_problem_for_the_address_and_one_for_each_limit():
    target = {"name": "sales", "format": "CSV", "path": "https://example.com/out.csv", "timeout": 5, "max_bytes": 100}

    problems = _problems({"identifier": "x", "metadata": {"source": {"name": "s", "format": "CSV", "path": GOOGLE},
                                                          "target": target}})

    assert problems == [
        "metadata.target.path: is an http(s) address, which can be read and not written, so it cannot be a target",
        "metadata.target.timeout: only applies to a CSV source whose path is an http(s) address",
        "metadata.target.max_bytes: only applies to a CSV source whose path is an http(s) address"]


@pytest.mark.parametrize("fmt, extra", [("PARQUET", {}), ("JSON", {}), ("XLSX", {})])
def test_a_source_that_is_not_a_csv_and_is_an_address_has_a_problem_for_the_address_and_one_for_a_limit(fmt, extra):
    problems = _problems(_spec("https://example.com/sales.data", **{"format": fmt, "timeout": 5, **extra}))

    assert problems == [
        "metadata.source.path: is an http(s) address, which only a CSV source can read",
        "metadata.source.timeout: only applies to a CSV source whose path is an http(s) address"]


def test_a_limit_with_a_null_value_is_no_limit():
    assert _problems(_spec(GOOGLE, timeout=None, max_bytes=None)) == []
    assert _problems(_spec("sales.csv", timeout=None, max_bytes=None)) == []


def test_the_schema_has_the_limits_of_a_block_and_says_what_a_csv_path_may_be():
    schema = kind_schema("Pipeline")
    block = schema["$defs"]["block"]["properties"]

    assert block["timeout"]["type"] == "number" and block["timeout"]["exclusiveMinimum"] == 0
    assert block["max_bytes"]["type"] == "integer" and block["max_bytes"]["minimum"] == 1
    assert "52428800" in block["max_bytes"]["description"] and "30 by default" in block["timeout"]["description"]
    assert "A CSV source may be an https:// address" in schema["$defs"]["source"]["properties"]["path"]["description"]


def test_a_sheet_config_is_valid_for_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    validator = jsonschema.Draft202012Validator(spec.json_schema())

    good = _spec(GOOGLE, timeout=60, max_bytes=1000000)
    bad = _spec(GOOGLE, timeout=0, max_bytes=1.5)

    assert list(validator.iter_errors(good)) == []
    assert len(list(validator.iter_errors(bad))) == 2


def test_plan_of_a_sheet_config_is_valid_and_shows_the_address_as_written(tmp_path):
    path = tmp_path / "sheet_sales.json"
    path.write_text(json.dumps(_spec(GOOGLE)))

    result = plan(path)

    assert result["valid"] is True and result["errors"] == []
    assert result["route"] == "CSV -> ICEBERG" and result["write_mode"] == "upsert"
    assert result["schedule"] == "@daily" and result["source_path"] == GOOGLE


def test_plan_of_an_address_that_is_not_allowed_is_not_valid(tmp_path):
    path = tmp_path / "sheet_sales.json"
    path.write_text(json.dumps(_spec("http://example.com/sales.csv")))

    result = plan(path)

    assert result["valid"] is False and len(result["errors"]) == 1 and "plain http" in result["errors"][0]


def test_plan_fills_a_window_into_an_address(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps(_spec("https://example.com/orders_{window_start:%Y-%m-%d}.csv", target={
        "name": "orders", "format": "ICEBERG", "catalog": CATALOG, "write_mode": "append"})))

    result = plan(path, window="2026-08-04T00:00:00Z/2026-08-05T00:00:00Z")

    assert result["valid"] is True
    assert result["source_path"] == "https://example.com/orders_2026-08-04.csv"
    assert result["source_path_template"] == "https://example.com/orders_{window_start:%Y-%m-%d}.csv"


# ====================================================================== the sheet, the tick and the table


class Shop:
    """A folder with the config of a sheet, and the server that answers for the sheet as Google does."""

    def __init__(self, tmp_path: Path, server) -> None:
        self.server = server
        self.project = tmp_path / "shop"
        self.project.mkdir()
        self.warehouse = tmp_path / "warehouse"
        self.sheet(SHEET)
        self.config = self.write_config()

    def address(self, gid: int = 0) -> str:
        return f"{self.server.url}/spreadsheets/d/SHEET_ID/export?format=csv&gid={gid}"

    def sheet(self, body, gid: int = 0, **reply) -> None:
        """The export link of tab ``gid`` redirects to the file, as Google does, and the file is ``body``."""
        self.server.serve(f"/spreadsheets/d/SHEET_ID/export?format=csv&gid={gid}", b"", status=307,
                          headers={"Location": f"/export/tab{gid}.csv"})
        self.server.serve(f"/export/tab{gid}.csv", body, **reply)

    def write_config(self, name: str = "sheet_sales", table: str = "sales", gid: int = 0, **changes) -> Path:
        data = _spec(self.address(gid))
        data["identifier"] = name
        data["metadata"]["target"]["name"] = table
        for key, value in changes.items():
            if key in ("write_mode", "join_cols"):
                data["metadata"]["target"][key] = value
            elif key == "path":
                data["metadata"]["source"]["path"] = value
            elif key == "metadata":
                data["metadata"].update(value)
            else:
                data["metadata"]["source"][key] = value
        path = self.project / f"{name}.json"
        path.write_text(json.dumps(data, indent=2))
        return path

    def tick(self, number: int = 1, clock: str = "02:05", **options):
        """Tick on day ``number`` after 3 August, at ``clock``. The fire of that day owns the day before it."""
        now = f"{(DAY + dt.timedelta(days=number - 1)).isoformat()}T{clock}:00Z"
        return schedule_module.tick(self.project, now=now, sleep=lambda seconds: None, **options)

    def events(self) -> list[dict]:
        log = self.project / ".ldp" / "events.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def finished(self, name: str = "sheet_sales") -> list[dict]:
        return [event["payload"] for event in self.events() if event["type"] == "run.finished"
                and event["payload"].get("pipeline") == name]

    def failed(self) -> list[dict]:
        return [event["payload"] for event in self.events() if event["type"] == "run.failed"]

    def rows(self, table: str = "sales") -> list[dict]:
        catalog = {"identifier": "shop", "warehouse_path": str(self.warehouse)}
        found = Iceberg(table, catalog=catalog, base_dir=self.project).get().to_pylist()
        return sorted(found, key=lambda row: (row["day"], row["dish"]))

    def data_files(self) -> int:
        return len(list(self.warehouse.rglob("*.parquet")))


@pytest.fixture
def shop(tmp_path, web_server):
    return Shop(tmp_path, web_server)


def test_the_first_tick_loads_the_sheet(shop):
    result = shop.tick(1)

    assert result.exit_code == 0 and result.counts()["published"] == 1
    assert [(row["day"].isoformat(), row["dish"], row["orders"], row["revenue"]) for row in shop.rows()] == [
        ("2026-07-01", "pad thai", 12, 143.5), ("2026-07-02", "green curry", 9, 117.0)]
    assert [(entry["rows_read"], entry["rows_written"], entry["status"]) for entry in shop.finished()] == [
        (2, 2, "published")]


def test_the_sheet_is_read_through_its_redirect(shop):
    shop.tick(1)

    assert [path for path, _ in shop.server.seen] == [
        "/spreadsheets/d/SHEET_ID/export?format=csv&gid=0", "/export/tab0.csv"]


def test_a_tick_the_next_day_on_a_sheet_that_has_not_changed_writes_no_rows(shop):
    shop.tick(1)
    before, files = shop.rows(), shop.data_files()

    result = shop.tick(2)

    assert result.exit_code == 0 and result.counts()["published"] == 1
    assert shop.rows() == before and shop.data_files() == files
    assert [(entry["rows_read"], entry["rows_written"]) for entry in shop.finished()] == [(2, 2), (2, 0)]


def test_an_edited_row_is_updated_and_the_others_are_left_alone(shop):
    shop.tick(1)
    shop.sheet(SHEET.replace("2026-07-02,green curry,9,117.0", "2026-07-02,green curry,10,124.0"))

    shop.tick(2)

    assert [(row["dish"], row["orders"], row["revenue"]) for row in shop.rows()] == [
        ("pad thai", 12, 143.5), ("green curry", 10, 124.0)]
    assert [entry["rows_written"] for entry in shop.finished()] == [2, 1]


def test_a_new_row_is_added(shop):
    shop.tick(1)
    shop.sheet(SHEET + "2026-07-03,tom yum,5,60.0\n")

    shop.tick(2)

    assert [row["dish"] for row in shop.rows()] == ["pad thai", "green curry", "tom yum"]
    assert [(entry["rows_read"], entry["rows_written"]) for entry in shop.finished()] == [(2, 2), (3, 1)]


def test_a_row_that_is_deleted_from_the_sheet_stays_in_the_table(shop):
    shop.tick(1)
    shop.sheet("day,dish,orders,revenue\n2026-07-01,pad thai,12,143.5\n")

    shop.tick(2)

    assert [row["dish"] for row in shop.rows()] == ["pad thai", "green curry"]
    assert [(entry["rows_read"], entry["rows_written"]) for entry in shop.finished()] == [(2, 2), (1, 0)]


def test_write_mode_overwrite_mirrors_a_deleted_row_and_writes_every_row_each_time(tmp_path, web_server):
    shop = Shop(tmp_path, web_server)
    shop.write_config(write_mode="overwrite")
    shop.tick(1)
    shop.sheet("day,dish,orders,revenue\n2026-07-01,pad thai,12,143.5\n")

    shop.tick(2)
    shop.tick(3)

    assert [row["dish"] for row in shop.rows()] == ["pad thai"]
    assert [(entry["rows_read"], entry["rows_written"]) for entry in shop.finished()] == [(2, 2), (1, 1), (1, 1)]


def test_a_second_tick_in_the_same_window_does_not_read_the_sheet_again(shop):
    shop.tick(1, "02:05")
    asked = len(shop.server.seen)

    result = shop.tick(1, "02:10")

    assert result.counts() == {"published": 0, "skipped": 1, "blocked": 0, "failed": 0}
    assert len(shop.server.seen) == asked and len(shop.finished()) == 1


def test_every_tab_of_a_sheet_has_a_config_and_a_table_of_its_own(shop):
    shop.sheet("day,dish,orders,revenue\n2026-07-01,rice,3,9.0\n", gid=1)
    shop.write_config(name="sheet_rice", table="rice", gid=1)

    result = shop.tick(1)

    assert result.counts()["published"] == 2 and result.exit_code == 0
    assert [row["dish"] for row in shop.rows("sales")] == ["pad thai", "green curry"]
    assert [row["dish"] for row in shop.rows("rice")] == ["rice"]
    assert sorted(path for path, _ in shop.server.seen if "export?" in path) == [
        "/spreadsheets/d/SHEET_ID/export?format=csv&gid=0", "/spreadsheets/d/SHEET_ID/export?format=csv&gid=1"]


def test_a_sheet_whose_keys_are_two_columns_is_upserted_on_both(shop):
    shop.tick(1)
    shop.sheet(SHEET + "2026-07-01,green curry,4,52.0\n")  # the same day as pad thai, another dish

    shop.tick(2)

    assert [(row["day"].isoformat(), row["dish"]) for row in shop.rows()] == [
        ("2026-07-01", "green curry"), ("2026-07-01", "pad thai"), ("2026-07-02", "green curry")]


def test_the_events_name_the_sheet_without_its_query(shop):
    shop.tick(1)

    started = [event["payload"] for event in shop.events() if event["type"] == "run.started"]
    text = json.dumps(shop.events())

    assert started[0]["source"] == {"format": "CSV", "name": "spreadsheets/d/SHEET_ID/export",
                                    "namespace": shop.server.url}
    assert "format=csv" not in text and "gid=0" not in text


# ---------------------------------------------------------------------- when the sheet cannot be read


def test_a_sheet_that_cannot_be_reached_fails_the_window_and_the_next_tick_carries_it_on(shop):
    shop.server.serve("/export/tab0.csv", "busy", status=503)

    failed = shop.tick(1, "02:05")

    assert failed.exit_code == 1 and failed.counts()["failed"] == 1
    assert shop.failed()[0]["error_type"] == "ConnectionError"
    assert "answered HTTP 503" in shop.failed()[0]["message"] and "format=csv" not in shop.failed()[0]["message"]
    assert [entry["status"] for entry in shop.finished()] == ["failed"]

    shop.sheet(SHEET)
    done = shop.tick(1, "02:10")

    assert done.exit_code == 0 and done.counts()["published"] == 1
    assert [row["dish"] for row in shop.rows()] == ["pad thai", "green curry"]
    attempts = [event["attempt"] for event in shop.events() if event["type"] == "run.started"]
    assert attempts == [1, 2], "the window is the same run, and its attempts go up by one"


def test_retries_of_the_config_try_a_503_again_within_the_tick(shop):
    shop.write_config(metadata={"retries": {"max_attempts": 3, "delay_s": 0.01}})
    shop.server.serve_once("/export/tab0.csv", "busy", status=503)
    shop.server.serve_once("/export/tab0.csv", "busy", status=429)

    result = shop.tick(1)

    assert result.exit_code == 0 and result.counts()["published"] == 1
    assert [path for path, _ in shop.server.seen].count("/export/tab0.csv") == 3
    assert [row["dish"] for row in shop.rows()] == ["pad thai", "green curry"]


def test_without_retries_one_503_fails_the_window(shop):
    shop.server.serve_once("/export/tab0.csv", "busy", status=503)

    result = shop.tick(1)

    assert result.exit_code == 1 and [path for path, _ in shop.server.seen].count("/export/tab0.csv") == 1


def test_a_sheet_that_is_not_shared_fails_with_what_to_do_and_is_not_retried_in_the_tick(shop):
    shop.write_config(metadata={"retries": {"max_attempts": 3, "delay_s": 0.01}})
    shop.sheet("<html><body>Sign in to continue</body></html>", content_type="text/html; charset=utf-8")

    result = shop.tick(1)

    assert result.exit_code == 1 and result.counts()["failed"] == 1
    message = shop.failed()[0]["message"]
    assert shop.failed()[0]["error_type"] == "ConfigError"
    assert "answered with a web page, not a CSV file" in message and "not shared by link" in message
    assert "Sign in" not in message
    assert [path for path, _ in shop.server.seen].count("/export/tab0.csv") == 1


def test_a_sheet_that_is_shared_after_a_failure_is_loaded_by_the_next_tick(shop):
    shop.sheet("<html><body>Sign in to continue</body></html>", content_type="text/html")
    assert shop.tick(1, "02:05").exit_code == 1

    shop.sheet(SHEET)
    result = shop.tick(1, "02:10")

    assert result.exit_code == 0 and result.counts()["published"] == 1 and len(shop.rows()) == 2


def test_a_sheet_that_is_gone_is_a_config_error(shop):
    shop.server.serve("/export/tab0.csv", "gone", status=404)

    result = shop.tick(1)

    assert result.exit_code == 1
    assert shop.failed()[0]["error_type"] == "ConfigError" and "answered HTTP 404" in shop.failed()[0]["message"]


def test_text_in_a_number_column_writes_nothing_and_the_next_tick_tries_again(shop):
    shop.tick(1)
    broken = SHEET.replace("2026-07-02,green curry,9,117.0", "2026-07-02,green curry,TBD,117.0")
    shop.sheet(broken)

    failed = shop.tick(2, "02:05")

    assert failed.exit_code == 1
    assert shop.failed()[0]["message"] == "Cannot change column type: orders: long -> string"
    assert [row["orders"] for row in shop.rows()] == [12, 9]

    shop.sheet(SHEET.replace("2026-07-02,green curry,9,117.0", "2026-07-02,green curry,11,117.0"))
    fixed = shop.tick(2, "02:10")

    assert fixed.exit_code == 0 and [row["orders"] for row in shop.rows()] == [12, 11]


def test_an_empty_key_cell_fails_the_run_and_writes_nothing(shop):
    shop.tick(1)
    files = shop.data_files()
    shop.sheet(SHEET.replace("2026-07-02,green curry", ",green curry"))

    result = shop.tick(2)

    assert result.exit_code == 1
    assert shop.failed()[0]["message"] == "join column 'day' has null values; upsert keys must be non-null"
    assert len(shop.rows()) == 2 and shop.data_files() == files


def test_a_key_that_appears_twice_fails_the_run_and_writes_nothing(shop):
    shop.tick(1)
    files = shop.data_files()
    shop.sheet(SHEET + "2026-07-02,green curry,5,58.0\n")

    result = shop.tick(2)

    assert result.exit_code == 1
    assert shop.failed()[0]["message"] == ("upsert data for shop.sales has 1 duplicate rows on join_cols "
                                           "['day', 'dish']; keys must be unique")
    assert [row["orders"] for row in shop.rows()] == [12, 9] and shop.data_files() == files


def test_a_header_that_is_renamed_fails_the_run_because_a_key_column_is_missing(shop):
    shop.tick(1)
    shop.sheet(SHEET.replace("day,dish", "date,dish"))

    result = shop.tick(2)

    assert result.exit_code == 1
    assert "join_cols ['day'] are not columns of the data written to shop.sales" in shop.failed()[0]["message"]
    assert [row["dish"] for row in shop.rows()] == ["pad thai", "green curry"]


def test_an_empty_cell_is_a_null_and_a_sheet_with_a_header_only_is_not_loaded(shop):
    shop.tick(1)
    shop.sheet(SHEET.replace("12,143.5", ",143.5"))
    shop.tick(2)

    assert [row["orders"] for row in shop.rows()] == [None, 9]

    shop.sheet("day,dish,orders,revenue\n")
    result = shop.tick(3)

    assert result.exit_code == 1
    assert "0 rows" in shop.failed()[0]["message"]


def test_timeout_and_max_bytes_are_keys_that_a_source_block_uses(shop, caplog):
    shop.write_config(timeout=20, max_bytes=100000)
    caplog.set_level(logging.WARNING)

    assert shop.tick(1).exit_code == 0

    assert not [record for record in caplog.records if "are not used and are ignored" in record.getMessage()]


def test_max_bytes_of_the_config_is_the_limit_of_the_download(shop):
    shop.write_config(max_bytes=30)

    result = shop.tick(1)

    assert result.exit_code == 1
    assert "has more than max_bytes, which is 30 bytes" in shop.failed()[0]["message"]

    shop.write_config(max_bytes=len(SHEET))
    assert shop.tick(1, "02:10").exit_code == 0


def test_timeout_of_the_config_is_the_limit_of_the_wait(shop):
    shop.sheet(SHEET, delay=1.0)
    shop.write_config(timeout=0.2)

    result = shop.tick(1)

    assert result.exit_code == 1
    assert shop.failed()[0]["error_type"] == "TimeoutError" and "did not answer in 0.2 seconds" in (
        shop.failed()[0]["message"])


def test_a_config_with_a_secret_in_its_address_is_not_run(shop):
    shop.write_config(path=f"{shop.server.url}/export/tab0.csv?token={SECRET}")

    result = shop.tick(1)

    assert result.exit_code == 1 and result.counts()["published"] == 0
    error = result.workflows[0].error
    assert "is not valid, so no window ran" in error and "metadata.source.path: looks like an inline secret" in error
    assert SECRET not in error
    assert shop.server.seen == []


# ---------------------------------------------------------------------- a daily file over http


def test_a_file_for_each_day_over_http_uses_the_window_in_its_address(tmp_path, web_server):
    shop = Shop(tmp_path, web_server)
    shop.write_config(path=f"{web_server.url}/orders_{{window_start:%Y-%m-%d}}.csv", write_mode="upsert")
    web_server.serve("/orders_2026-08-03.csv", SHEET)
    web_server.serve("/orders_2026-08-04.csv", SHEET + "2026-07-03,tom yum,5,60.0\n")

    shop.tick(1)  # the fire of 4 August owns 3 August
    shop.tick(2)

    assert [path for path, _ in web_server.seen] == ["/orders_2026-08-03.csv", "/orders_2026-08-04.csv"]
    assert [row["dish"] for row in shop.rows()] == ["pad thai", "green curry", "tom yum"]


# ====================================================================== the commands


def test_ldp_schedule_tick_runs_the_sync(shop, capsys):
    code = main(["schedule", "tick", str(shop.project), "--now", "2026-08-05T02:05:00Z"])

    out = capsys.readouterr().out
    assert code == 0
    assert "sheet_sales  2026-08-04T00:00:00Z/2026-08-05T00:00:00Z  published" in out
    assert "1 published, 0 skipped, 0 blocked, 0 failed" in out
    assert len(shop.rows()) == 2

    again = main(["schedule", "tick", str(shop.project), "--now", "2026-08-06T02:05:00Z"])

    assert again == 0 and [entry["rows_written"] for entry in shop.finished()] == [2, 0]


def test_ldp_schedule_tick_exits_with_1_when_the_sheet_cannot_be_read(shop, capsys):
    shop.server.serve("/export/tab0.csv", "busy", status=500)

    code = main(["schedule", "tick", str(shop.project), "--now", "2026-08-05T02:05:00Z"])

    assert code == 1 and "1 failed" in capsys.readouterr().out


def test_ldp_schedule_list_shows_the_schedule_of_the_sheet(shop, capsys):
    code = main(["schedule", "list", str(shop.project), "--now", "2026-08-05T02:05:00Z"])

    out = capsys.readouterr().out
    assert code == 0 and "sheet_sales  Pipeline" in out and "schedule     @daily, in UTC, windows of one day" in out


def test_ldp_run_reads_the_sheet_and_says_how_many_rows_it_wrote(shop, capsys):
    assert main(["run", str(shop.config)]) == 0
    assert "sheet_sales: read 2 rows, wrote 2 rows" in capsys.readouterr().out

    assert main(["run", str(shop.config)]) == 0
    assert "sheet_sales: read 2 rows, wrote 0 rows" in capsys.readouterr().out


def test_ldp_plan_of_a_sheet_config_exits_with_0_and_with_1_for_an_address_that_is_not_allowed(shop, capsys):
    assert main(["plan", str(shop.config)]) == 0
    capsys.readouterr()

    shop.write_config(path="http://example.com/sales.csv")

    assert main(["plan", str(shop.config)]) == 1
