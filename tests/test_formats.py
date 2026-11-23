"""Tests for the CSV and Parquet formats and the store re-exports."""

import logging
import os
import stat
import uuid

import pyarrow as pa
import pyarrow.csv
import pyarrow.parquet
import pytest

from local_data_platform import urls
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.csv import CSV
from local_data_platform.format.parquet import Parquet

FORMATS = [pytest.param(CSV, "rides.csv", id="csv"), pytest.param(Parquet, "rides.parquet", id="parquet")]
WRITERS = {CSV: (pyarrow.csv, "write_csv"), Parquet: (pyarrow.parquet, "write_table")}


def _plain_columns(df: pa.Table) -> list[dict]:
    """Rows without the timestamp column, whose CSV round trip changes type."""
    return df.select(["ride_id", "city", "fare"]).to_pylist()


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_round_trip_with_absolute_path(tmp_path, sample_table, cls, filename):
    path = tmp_path / filename
    table = cls("rides", str(path))

    assert table.path == path
    assert table.put(sample_table) == sample_table.num_rows
    assert _plain_columns(table.get()) == _plain_columns(sample_table)


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_relative_path_resolves_against_base_dir(tmp_path, sample_table, cls, filename):
    base = tmp_path / "config_dir"
    table = cls("rides", f"data/{filename}", base_dir=base)

    assert table.path == (base / "data" / filename).resolve()
    table.put(sample_table)
    assert (base / "data" / filename).is_file()
    assert table.get().num_rows == sample_table.num_rows


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_relative_path_without_base_dir_resolves_against_cwd(tmp_path, monkeypatch, sample_table, cls, filename):
    monkeypatch.chdir(tmp_path)
    table = cls("rides", filename)

    assert table.path == (tmp_path / filename).resolve()
    table.put(sample_table)
    assert cls("again", str(tmp_path / filename)).get().num_rows == sample_table.num_rows


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_legacy_leading_slash_path_warns_and_resolves(tmp_path, sample_table, cls, filename):
    name = f"ldp_legacy_{uuid.uuid4().hex}_{filename}"
    assert not os.path.exists(f"/{name}")
    cls("seed", str(tmp_path / name)).put(sample_table)

    with pytest.warns(DeprecationWarning, match="leading slash"):
        table = cls("rides", f"/{name}", base_dir=tmp_path)

    assert table.path == (tmp_path / name).resolve()
    assert table.get().num_rows == sample_table.num_rows


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_get_missing_file_names_resolved_path(tmp_path, cls, filename):
    table = cls("rides", filename, base_dir=tmp_path)

    with pytest.raises(FileNotFoundError, match=str(tmp_path / filename)):
        table.get()


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_put_none_raises_value_error(tmp_path, cls, filename):
    # Before 0.1.1, CSV.put(None) crashed with TypeError from `df is not None or len(df) > 0`.
    table = cls("rides", tmp_path / filename)

    with pytest.raises(ValueError, match="None"):
        table.put(None)
    assert not (tmp_path / filename).exists()


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_put_empty_raises_unless_allowed(tmp_path, sample_table, cls, filename):
    # Before 0.1.1, CSV.put wrote an empty table because the `or` short-circuited.
    empty = sample_table.slice(0, 0)
    table = cls("rides", tmp_path / filename)

    with pytest.raises(ValueError, match="0 rows"):
        table.put(empty)
    assert not (tmp_path / filename).exists()

    assert table.put(empty, allow_empty=True) == 0
    assert table.get().num_rows == 0


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_put_rejects_non_arrow_input(tmp_path, cls, filename):
    with pytest.raises(TypeError, match="pyarrow.Table"):
        cls("rides", tmp_path / filename).put([{"ride_id": 1}])


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_put_accepts_record_batch(tmp_path, sample_table, cls, filename):
    batch = sample_table.to_batches()[0]

    assert cls("rides", tmp_path / filename).put(batch) == batch.num_rows


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_put_creates_parent_folders(tmp_path, sample_table, cls, filename):
    path = tmp_path / "a" / "b" / "c" / filename

    cls("rides", path).put(sample_table)

    assert path.is_file()


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_put_overwrites_and_leaves_no_temp_files(tmp_path, make_table, cls, filename):
    table = cls("rides", tmp_path / filename)

    table.put(make_table(n=6))
    table.put(make_table(n=2))

    assert table.get().num_rows == 2
    assert os.listdir(tmp_path) == [filename]


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_failed_write_keeps_previous_file(tmp_path, monkeypatch, make_table, cls, filename):
    table = cls("rides", tmp_path / filename)
    table.put(make_table(n=6))
    before = (tmp_path / filename).read_bytes()
    module, attr = WRITERS[cls]

    def broken_writer(df, where, *args, **kwargs):
        with open(where, "wb") as handle:
            handle.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(module, attr, broken_writer)
    with pytest.raises(OSError, match="disk full"):
        table.put(make_table(n=2))

    assert (tmp_path / filename).read_bytes() == before
    assert os.listdir(tmp_path) == [filename]


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_written_file_has_default_permissions(tmp_path, sample_table, cls, filename):
    reference = tmp_path / "reference"
    reference.write_bytes(b"")
    path = tmp_path / filename

    cls("rides", path).put(sample_table)

    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)


@pytest.mark.parametrize("cls", [CSV, Parquet])
@pytest.mark.parametrize("path", [None, ""])
def test_missing_path_is_a_config_error(cls, path):
    with pytest.raises(ConfigError, match="needs a path"):
        cls("rides", path)


def test_parquet_round_trip_preserves_schema_and_values(tmp_path, sample_table_tz):
    table = Parquet("rides", tmp_path / "rides.parquet")

    table.put(sample_table_tz)

    assert table.get().equals(sample_table_tz)


def test_parquet_reads_a_folder_of_files(tmp_path, make_table):
    folder = tmp_path / "rides"
    Parquet("part", folder / "part-0.parquet").put(make_table(n=3, start_id=1))
    Parquet("part", folder / "part-1.parquet").put(make_table(n=4, start_id=10))

    df = Parquet("rides", folder).get()

    assert sorted(df.column("ride_id").to_pylist()) == [1, 2, 3, 10, 11, 12, 13]


def test_csv_empty_allowed_writes_header(tmp_path, sample_table):
    path = tmp_path / "empty.csv"

    CSV("rides", path).put(sample_table.slice(0, 0), allow_empty=True)

    assert path.read_text().splitlines() == ['"ride_id","city","fare","pickup_ts"']


def test_format_attribute_and_repr(tmp_path):
    table = CSV("rides", tmp_path / "rides.csv")

    assert table.format == "CSV"
    assert Parquet("rides", tmp_path / "rides.parquet").format == "PARQUET"
    assert "rides.csv" in repr(table)


def test_store_parquet_reexports_format_parquet():
    from local_data_platform.store.source.parquet import Parquet as StoreParquet

    assert StoreParquet is Parquet


def test_store_iceberg_reexports_format_iceberg():
    from local_data_platform.format.iceberg import Iceberg, WriteResult
    from local_data_platform.store.target.iceberg import Iceberg as StoreIceberg
    from local_data_platform.store.target.iceberg import WriteResult as StoreWriteResult

    assert StoreIceberg is Iceberg
    assert StoreWriteResult is WriteResult


# ====================================================================== a CSV that is read over http
#
# The sheet of a shop, shared by link, is a CSV at an https address. Here the server is on 127.0.0.1, the one place that
# plain http is allowed, so nothing leaves the machine. What only https can show is in tests/test_urls.py.

SHEET = "day,dish,orders,revenue\n2026-07-01,pad thai,12,143.5\n2026-07-02,green curry,9,117.0\n"


def test_csv_reads_an_address(web_server):
    web_server.serve("/sales.csv", SHEET)

    df = CSV("sales", f"{web_server.url}/sales.csv").get()

    assert df.column_names == ["day", "dish", "orders", "revenue"]
    assert df.schema.types == [pa.date32(), pa.string(), pa.int64(), pa.float64()]
    assert df.to_pylist()[1]["dish"] == "green curry" and df.num_rows == 2


def test_csv_reads_the_same_rows_from_an_address_and_from_a_file(web_server, tmp_path):
    web_server.serve("/sales.csv", SHEET)
    (tmp_path / "sales.csv").write_text(SHEET)

    assert CSV("sales", f"{web_server.url}/sales.csv").get().equals(CSV("sales", str(tmp_path / "sales.csv")).get())


def test_csv_reads_the_address_again_each_time_it_is_asked(web_server):
    table = CSV("sales", f"{web_server.url}/sales.csv")
    web_server.serve("/sales.csv", SHEET)
    assert table.get().num_rows == 2

    web_server.serve("/sales.csv", SHEET + "2026-07-03,tom yum,5,60.0\n")

    assert table.get().num_rows == 3
    assert len(web_server.seen) == 2


def test_csv_does_not_read_an_address_when_it_is_made(web_server):
    CSV("sales", f"{web_server.url}/sales.csv", timeout=5, max_bytes=1000)

    assert web_server.seen == []


def test_csv_keeps_the_address_and_its_limits(web_server):
    address = f"{web_server.url}/sales.csv?format=csv&gid=3"

    plain = CSV("sales", address)
    limited = CSV("sales", address, timeout=2.5, max_bytes=1000)

    assert plain.path == address and plain.format == "CSV" and plain.options == {}
    assert (plain.timeout, plain.max_bytes) == (urls.DEFAULT_TIMEOUT_S, urls.DEFAULT_MAX_BYTES)
    assert (limited.timeout, limited.max_bytes) == (2.5, 1000)


def test_csv_of_a_file_has_no_limits(tmp_path):
    table = CSV("sales", str(tmp_path / "sales.csv"))

    assert table.timeout is None and table.max_bytes is None


def test_csv_hands_its_limits_to_the_download(monkeypatch):
    given = {}

    def fetch(address, *, timeout, max_bytes):
        given.update(address=address, timeout=timeout, max_bytes=max_bytes)
        return SHEET.encode()

    monkeypatch.setattr(urls, "fetch", fetch)

    address = "https://docs.example.com/spreadsheets/d/ID/export?format=csv&gid=0"
    df = CSV("sales", address, timeout=7, max_bytes=999).get()

    assert given == {"address": address, "timeout": 7, "max_bytes": 999}
    assert df.num_rows == 2


def test_csv_repr_of_an_address_has_no_query(web_server):
    table = CSV("sales", f"{web_server.url}/sales.csv?format=csv&token=SECRET-VALUE")

    assert repr(table) == f"CSV(name='sales', path='{web_server.url}/sales.csv')"
    assert repr(CSV("rides", "/tmp/rides.csv")).startswith("CSV(name='rides', path='/")


def test_csv_put_to_an_address_is_refused_and_sends_nothing(web_server, sample_table):
    table = CSV("sales", f"{web_server.url}/sales.csv?token=SECRET-VALUE")

    with pytest.raises(ConfigError) as caught:
        table.put(sample_table)

    assert "reads" in str(caught.value) and "cannot be written" in str(caught.value)
    assert "SECRET-VALUE" not in str(caught.value)
    assert web_server.seen == []


@pytest.mark.parametrize("address, words", [
    ("http://example.com/sales.csv", "plain http"),
    ("http://192.168.0.7/sales.csv", "plain http"),
    ("https://user:pw@example.com/sales.csv", "user name or a password"),
    ("https://example.com:99999/sales.csv", "is not valid"),
    ("https:///sales.csv", "no host name"),
    ("https://example.com/sales .csv", "control character"),
])
def test_csv_refuses_an_address_that_is_not_allowed(address, words):
    with pytest.raises(ConfigError) as caught:
        CSV("sales", address + "?token=SECRET-VALUE")

    assert "CSV dataset 'sales' cannot read" in str(caught.value) and words in str(caught.value)
    assert ", because the address " in str(caught.value)
    assert "SECRET-VALUE" not in str(caught.value) and "pw@" not in str(caught.value)


def test_csv_says_exactly_why_an_address_is_not_allowed():
    with pytest.raises(ConfigError) as caught:
        CSV("sales", "http://example.com/sales.csv?token=SECRET-VALUE")

    assert str(caught.value) == ("CSV dataset 'sales' cannot read http://example.com/sales.csv, because the address "
                                 "is plain http, which is only for 127.0.0.1 and localhost")


def test_csv_logs_the_address_without_its_query(web_server, caplog):
    web_server.serve("/sales.csv", SHEET)
    caplog.set_level(logging.INFO)

    CSV("sales", f"{web_server.url}/sales.csv?format=csv&token=SECRET-VALUE").get()

    messages = [record.getMessage() for record in caplog.records if record.name.endswith("format.csv")]
    assert messages == [f"Read 2 rows from CSV {web_server.url}/sales.csv"]


@pytest.mark.parametrize("timeout", [0, -1, -0.5, True, False, "5", float("nan"), float("inf"), [5]])
def test_csv_timeout_is_a_number_of_seconds_above_zero(timeout):
    with pytest.raises(ConfigError, match="needs a timeout that is a number of seconds above 0"):
        CSV("sales", "https://example.com/sales.csv", timeout=timeout)


@pytest.mark.parametrize("max_bytes", [0, -1, 1.5, 1.0, True, False, "100", float("inf"), [5]])
def test_csv_max_bytes_is_a_whole_number_of_bytes_above_zero(max_bytes):
    with pytest.raises(ConfigError, match="needs a max_bytes that is a whole number of bytes above 0"):
        CSV("sales", "https://example.com/sales.csv", max_bytes=max_bytes)


@pytest.mark.parametrize("timeout, max_bytes", [(1, 1), (0.001, 10**12), (3600, 1)])
def test_csv_takes_small_and_large_limits(timeout, max_bytes):
    table = CSV("sales", "https://example.com/sales.csv", timeout=timeout, max_bytes=max_bytes)

    assert (table.timeout, table.max_bytes) == (timeout, max_bytes)


@pytest.mark.parametrize("options", [{"timeout": 5}, {"max_bytes": 100}, {"timeout": 5, "max_bytes": 100}])
def test_csv_of_a_file_refuses_limits_that_are_for_an_address(tmp_path, options):
    with pytest.raises(ConfigError, match="timeout or max_bytes, which are for an https:// path"):
        CSV("sales", str(tmp_path / "sales.csv"), **options)


@pytest.mark.parametrize("status, error", [(404, ConfigError), (403, ConfigError), (503, ConnectionError),
                                           (429, ConnectionError)])
def test_csv_get_of_an_address_that_answers_a_status_is_not_read(web_server, status, error):
    web_server.serve("/sales.csv", "no", status=status)

    with pytest.raises(error, match=f"answered HTTP {status}"):
        CSV("sales", f"{web_server.url}/sales.csv").get()


def test_csv_get_of_a_web_page_is_not_read_as_rows(web_server):
    web_server.serve("/sales.csv", "<html><body>Sign in</body></html>", content_type="text/html; charset=utf-8")

    with pytest.raises(ConfigError, match="answered with a web page, not a CSV file"):
        CSV("sales", f"{web_server.url}/sales.csv").get()


def test_csv_get_over_max_bytes_is_not_read(web_server):
    web_server.serve("/sales.csv", SHEET)

    with pytest.raises(ConfigError, match="has more than max_bytes, which is 20 bytes"):
        CSV("sales", f"{web_server.url}/sales.csv", max_bytes=20).get()
    assert CSV("sales", f"{web_server.url}/sales.csv", max_bytes=len(SHEET)).get().num_rows == 2


def test_csv_get_that_is_too_slow_times_out(web_server):
    web_server.serve("/sales.csv", SHEET, delay=1.0)

    with pytest.raises(TimeoutError):
        CSV("sales", f"{web_server.url}/sales.csv", timeout=0.2).get()


def test_csv_get_of_an_empty_answer_fails_as_an_empty_file_does(web_server, tmp_path):
    web_server.serve("/empty.csv", b"")
    (tmp_path / "empty.csv").write_bytes(b"")

    with pytest.raises(pa.ArrowInvalid, match="Empty CSV file") as from_file:
        CSV("empty", str(tmp_path / "empty.csv")).get()
    with pytest.raises(pa.ArrowInvalid, match="Empty CSV file") as from_address:
        CSV("empty", f"{web_server.url}/empty.csv").get()

    assert type(from_file.value) is type(from_address.value)


def test_csv_get_reads_a_header_only_answer_as_no_rows(web_server):
    web_server.serve("/head.csv", "day,dish\n")

    df = CSV("sales", f"{web_server.url}/head.csv").get()

    assert df.column_names == ["day", "dish"] and df.num_rows == 0


def test_csv_get_drops_the_byte_order_mark_of_a_sheet_saved_by_excel(web_server):
    web_server.serve("/sales.csv", b"\xef\xbb\xbfday,orders\n2026-07-01,12\n")

    assert CSV("sales", f"{web_server.url}/sales.csv").get().column_names == ["day", "orders"]
