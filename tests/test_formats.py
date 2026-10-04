"""Tests for the CSV and Parquet formats and the store re-exports."""

import os
import stat
import uuid

import pyarrow as pa
import pyarrow.csv
import pyarrow.parquet
import pytest

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
