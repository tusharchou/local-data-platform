"""Tests for ``local_data_platform.fs`` and the file formats on object storage.

Local tests use ``tmp_path``. S3 tests run against an in-process moto server on a free port on
127.0.0.1, so they are offline; they skip if ``moto[server]`` or ``boto3`` is missing.
"""

import gzip
import json
import logging
import os
import uuid

import pyarrow as pa
import pyarrow.csv
import pyarrow.parquet
import pytest
from pyarrow import fs as pafs

from local_data_platform import fs
from local_data_platform.exceptions import ConfigError
from local_data_platform.format.csv import CSV
from local_data_platform.format.parquet import Parquet
from local_data_platform.store.source.json import Json

FORMATS = [pytest.param(CSV, "rides.csv", id="csv"), pytest.param(Parquet, "rides.parquet", id="parquet")]
WRITERS = {CSV: (pyarrow.csv, "write_csv"), Parquet: (pyarrow.parquet, "write_table")}


def _plain(df: pa.Table) -> list[dict]:
    """Rows without the timestamp column, whose CSV round trip changes type."""
    return df.select(["ride_id", "city", "fare"]).to_pylist()


# ---------------------------------------------------------------------- locations


def test_relative_path_resolves_against_base_dir(tmp_path):
    fs_, path = fs.filesystem_for("data/rides.csv", base_dir=tmp_path)

    assert isinstance(fs_, pafs.LocalFileSystem)
    assert path == str((tmp_path / "data" / "rides.csv").resolve())
    assert fs.resolve_location("data/rides.csv", tmp_path) == (tmp_path / "data" / "rides.csv").resolve()


@pytest.mark.parametrize("prefix", ["file://", "file:", "file://localhost"])
def test_file_uris_are_local_paths(tmp_path, prefix):
    target = tmp_path / "a b.txt"
    target.write_text("x")
    uri = prefix + str(target).replace(" ", "%20")

    assert fs.resolve_location(uri) == target
    assert fs.is_file(uri)
    assert fs.uri_scheme(uri) == "file"
    assert not fs.is_remote(uri)


def test_file_uri_on_another_host_is_rejected():
    with pytest.raises(ConfigError, match="only local files"):
        fs.resolve_location("file://fileserver/share/rides.csv")


@pytest.mark.parametrize("uri", ["http://example.com/rides.csv", "hdfs://nn/rides.csv", "abfs://c/rides.csv"])
def test_unsupported_scheme_is_a_config_error(uri):
    with pytest.raises(ConfigError, match="unsupported URI scheme"):
        fs.filesystem_for(uri)


def test_credentials_in_uri_are_rejected_without_echoing_them():
    with pytest.raises(ConfigError, match="embeds credentials") as info:
        fs.resolve_location("s3://AKIAEXAMPLE:sup3rs3cret@bucket/rides.csv")

    assert "sup3rs3cret" not in str(info.value)
    assert "AKIAEXAMPLE" not in str(info.value)


def test_uri_without_bucket_is_rejected():
    with pytest.raises(ConfigError, match="no bucket"):
        fs.resolve_location("s3:///rides.csv")


@pytest.mark.parametrize("uri, scheme, remote", [
    ("s3://b/k", "s3", True), ("S3A://b/k", "s3a", True), ("gs://b/k", "gs", True),
    ("/tmp/x", None, False), ("rel/x", None, False), ("file:///x", "file", False),
])
def test_uri_scheme_and_is_remote(uri, scheme, remote):
    assert fs.uri_scheme(uri) == scheme
    assert fs.is_remote(uri) is remote


def test_remote_location_is_normalised():
    assert fs.resolve_location("s3://bucket/folder/") == "s3://bucket/folder"
    assert fs.resolve_location("gs://bucket") == "gs://bucket"


def test_empty_path_is_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        fs.resolve_location("")


# ---------------------------------------------------------------------- environment


def test_s3_options_come_from_the_environment():
    options = fs.s3_options({"AWS_ENDPOINT_URL": "http://127.0.0.1:9000/", "AWS_DEFAULT_REGION": "eu-west-1",
                             "AWS_ACCESS_KEY_ID": "AKIA", "AWS_SECRET_ACCESS_KEY": "shh"})

    assert options == {"endpoint_override": "127.0.0.1:9000", "scheme": "http", "region": "eu-west-1"}


def test_s3_specific_endpoint_and_region_take_precedence():
    options = fs.s3_options({"AWS_ENDPOINT_URL": "http://generic:1", "AWS_ENDPOINT_URL_S3": "https://s3only:2",
                             "AWS_DEFAULT_REGION": "eu-west-1", "AWS_REGION": "ap-southeast-1"})

    assert options == {"endpoint_override": "s3only:2", "scheme": "https", "region": "ap-southeast-1"}


def test_s3_options_without_environment_are_empty():
    assert fs.s3_options({}) == {}
    assert fs.s3_options({"AWS_ENDPOINT_URL": "minio:9000"}) == {"endpoint_override": "minio:9000"}


def test_gcs_options_honour_the_storage_emulator():
    options = fs.gcs_options({"STORAGE_EMULATOR_HOST": "http://localhost:4443", "GOOGLE_CLOUD_PROJECT": "p"})

    assert options == {"endpoint_override": "localhost:4443", "scheme": "http", "anonymous": True, "project_id": "p"}


def test_gs_uri_builds_a_gcs_filesystem(monkeypatch):
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://127.0.0.1:1")

    filesystem, path = fs.filesystem_for("gs://bucket/data/rides.parquet")

    assert isinstance(filesystem, pafs.GcsFileSystem)
    assert path == "bucket/data/rides.parquet"


# ---------------------------------------------------------------------- local atomic output


def test_open_output_atomic_replaces_the_file_and_leaves_no_temp(tmp_path):
    target = tmp_path / "out" / "data.bin"

    with fs.open_output_atomic(target) as stream:
        stream.write(b"first")
    with fs.open_output_atomic(str(target)) as stream:
        stream.write(b"second")

    assert target.read_bytes() == b"second"
    assert os.listdir(target.parent) == ["data.bin"]


def test_open_output_atomic_failure_keeps_the_old_file(tmp_path):
    target = tmp_path / "data.bin"
    target.write_bytes(b"old")

    with pytest.raises(RuntimeError, match="boom"):
        with fs.open_output_atomic(target) as stream:
            stream.write(b"partial")
            raise RuntimeError("boom")

    assert target.read_bytes() == b"old"
    assert os.listdir(tmp_path) == ["data.bin"]


def test_open_output_atomic_allows_closing_inside_the_block(tmp_path):
    target = tmp_path / "data.bin"

    with fs.open_output_atomic(target) as stream:
        stream.write(b"done")
        stream.close()

    assert target.read_bytes() == b"done"


def test_write_atomic_hands_local_writers_a_temporary_path(tmp_path):
    seen = []

    def write(where):
        seen.append(where)
        with open(where, "wb") as handle:
            handle.write(b"ok")

    fs.write_atomic("nested/out.bin", write, base_dir=tmp_path)

    assert isinstance(seen[0], str)
    assert os.path.dirname(seen[0]) == str(tmp_path / "nested")
    assert (tmp_path / "nested" / "out.bin").read_bytes() == b"ok"
    assert os.listdir(tmp_path / "nested") == ["out.bin"]


def test_exists_and_is_file_locally(tmp_path):
    (tmp_path / "f.txt").write_text("x")

    assert fs.exists(tmp_path / "f.txt") and fs.is_file(tmp_path / "f.txt")
    assert fs.exists(tmp_path) and not fs.is_file(tmp_path)
    assert not fs.exists(tmp_path / "missing")


def test_open_input_and_stream(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"hello")
    with gzip.open(tmp_path / "f.txt.gz", "wb") as handle:
        handle.write(b"zipped")

    with fs.open_input(tmp_path / "f.txt") as handle:
        assert handle.size() == 5 and handle.read() == b"hello"
    with fs.open_input_stream(tmp_path / "f.txt.gz") as handle:
        assert handle.read() == b"zipped"
    with pytest.raises(FileNotFoundError):
        fs.open_input(tmp_path / "missing.txt")


def test_csv_reads_a_gzipped_file(tmp_path, sample_table):
    plain = tmp_path / "rides.csv"
    CSV("rides", plain).put(sample_table)
    with gzip.open(tmp_path / "rides.csv.gz", "wb") as handle:
        handle.write(plain.read_bytes())

    assert _plain(CSV("rides", tmp_path / "rides.csv.gz").get()) == _plain(sample_table)


def test_formats_accept_file_uris(tmp_path, sample_table):
    uri = f"file://{tmp_path}/rides.parquet"

    Parquet("rides", uri).put(sample_table)

    assert Parquet("rides", tmp_path / "rides.parquet").get().equals(sample_table)
    assert Parquet("rides", uri).path == tmp_path / "rides.parquet"


# ---------------------------------------------------------------------- S3 (moto server)


@pytest.fixture(scope="module")
def moto_endpoint():
    """A moto server on a free port of 127.0.0.1, for the whole module."""
    server_module = pytest.importorskip("moto.server", reason="moto[server] is not installed")
    werkzeug = logging.getLogger("werkzeug")
    level = werkzeug.level
    werkzeug.setLevel(logging.ERROR)
    server = server_module.ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.stop()
        werkzeug.setLevel(level)


@pytest.fixture
def s3_bucket(moto_endpoint, monkeypatch, tmp_path):
    """A fresh bucket on the moto server, with the AWS environment pointing at it."""
    boto3 = pytest.importorskip("boto3", reason="boto3 is not installed")
    for name in ("AWS_ENDPOINT_URL_S3", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ENDPOINT_URL", moto_endpoint)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    bucket = f"ldp-test-{uuid.uuid4().hex[:12]}"
    client = boto3.client("s3", endpoint_url=moto_endpoint, region_name="us-east-1")
    client.create_bucket(Bucket=bucket)
    return bucket, client


def test_s3_filesystem_honours_aws_endpoint_url(s3_bucket):
    bucket, client = s3_bucket
    client.put_object(Bucket=bucket, Key="hello.txt", Body=b"hi")

    filesystem, path = fs.filesystem_for(f"s3://{bucket}/hello.txt")

    assert isinstance(filesystem, pafs.S3FileSystem)
    assert path == f"{bucket}/hello.txt"
    with fs.open_input(f"s3://{bucket}/hello.txt") as handle:
        assert handle.read() == b"hi"


def test_s3_open_output_atomic_round_trip(s3_bucket):
    bucket, client = s3_bucket
    uri = f"s3://{bucket}/out/data.bin"

    with fs.open_output_atomic(uri) as stream:
        stream.write(b"payload")

    assert client.get_object(Bucket=bucket, Key="out/data.bin")["Body"].read() == b"payload"
    assert fs.exists(uri) and fs.is_file(uri)
    assert fs.exists(f"s3://{bucket}/out") and not fs.is_file(f"s3://{bucket}/out")
    assert not fs.exists(f"s3://{bucket}/missing.bin")
    # One PUT: no multipart upload was left behind.
    assert not client.list_multipart_uploads(Bucket=bucket).get("Uploads")


def test_s3_failed_write_uploads_nothing(s3_bucket):
    bucket, client = s3_bucket
    uri = f"s3://{bucket}/data.bin"
    with fs.open_output_atomic(uri) as stream:
        stream.write(b"old")

    with pytest.raises(RuntimeError, match="boom"):
        with fs.open_output_atomic(uri) as stream:
            stream.write(b"partial")
            raise RuntimeError("boom")

    def broken(where):
        where.write(b"partial")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        fs.write_atomic(uri, broken)
    with pytest.raises(OSError, match="disk full"):
        fs.write_atomic(f"s3://{bucket}/never.bin", broken)

    assert client.get_object(Bucket=bucket, Key="data.bin")["Body"].read() == b"old"
    assert not fs.exists(f"s3://{bucket}/never.bin")


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_formats_round_trip_on_s3(s3_bucket, sample_table, cls, filename):
    bucket, _ = s3_bucket
    table = cls("rides", f"s3://{bucket}/data/{filename}", base_dir="/ignored/for/uris")

    assert table.put(sample_table) == sample_table.num_rows

    assert table.path == f"s3://{bucket}/data/{filename}"
    assert f"s3://{bucket}/data/{filename}" in repr(table)
    assert _plain(table.get()) == _plain(sample_table)


def test_parquet_on_s3_preserves_schema_and_reads_a_prefix(s3_bucket, make_table, sample_table_tz):
    bucket, _ = s3_bucket
    single = Parquet("rides", f"s3://{bucket}/single.parquet")
    single.put(sample_table_tz)
    Parquet("part", f"s3://{bucket}/rides/part-0.parquet").put(make_table(n=3, start_id=1))
    Parquet("part", f"s3://{bucket}/rides/part-1.parquet").put(make_table(n=4, start_id=10))

    assert single.get().equals(sample_table_tz)
    df = Parquet("rides", f"s3://{bucket}/rides/").get()
    assert sorted(df.column("ride_id").to_pylist()) == [1, 2, 3, 10, 11, 12, 13]


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_missing_s3_object_raises_file_not_found(s3_bucket, cls, filename):
    bucket, _ = s3_bucket

    with pytest.raises(FileNotFoundError, match=f"s3://{bucket}/nope/{filename}"):
        cls("rides", f"s3://{bucket}/nope/{filename}").get()


@pytest.mark.parametrize("cls, filename", FORMATS)
def test_failed_put_keeps_the_previous_s3_object(s3_bucket, monkeypatch, make_table, cls, filename):
    bucket, client = s3_bucket
    table = cls("rides", f"s3://{bucket}/{filename}")
    table.put(make_table(n=6))
    before = client.get_object(Bucket=bucket, Key=filename)["Body"].read()
    module, attr = WRITERS[cls]

    def broken_writer(df, where, *args, **kwargs):
        where.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(module, attr, broken_writer)
    with pytest.raises(OSError, match="disk full"):
        table.put(make_table(n=2))

    assert client.get_object(Bucket=bucket, Key=filename)["Body"].read() == before


def test_json_reads_from_s3(s3_bucket):
    bucket, client = s3_bucket
    client.put_object(Bucket=bucket, Key="q.json", Body=json.dumps({"query": "SELECT 1"}).encode())
    client.put_object(Bucket=bucket, Key="bad.json", Body=b"{not json")

    assert Json("q", f"s3://{bucket}/q.json").get() == {"query": "SELECT 1"}
    with pytest.raises(json.JSONDecodeError, match="bad.json"):
        Json("bad", f"s3://{bucket}/bad.json").get()
    with pytest.raises(FileNotFoundError, match="missing.json"):
        Json("missing", f"s3://{bucket}/missing.json").get()


def test_missing_bucket_is_an_error_not_a_silent_write(s3_bucket, sample_table):
    with pytest.raises(OSError):
        CSV("rides", f"s3://ldp-no-such-bucket-{uuid.uuid4().hex[:8]}/rides.csv").put(sample_table)


def test_pipelines_read_and_write_s3_paths(s3_bucket, tmp_path, sample_table):
    from local_data_platform.etl import run_config

    bucket, _ = s3_bucket
    CSV("seed", f"s3://{bucket}/in/rides.csv").put(sample_table)
    catalog = {"identifier": "demo", "warehouse_path": str(tmp_path / "warehouse")}
    ingest = {"identifier": "ingest", "metadata": {
        "source": {"format": "CSV", "name": "rides", "path": f"s3://{bucket}/in/rides.csv"},
        "target": {"format": "ICEBERG", "name": "rides", "catalog": catalog}}}
    export = {"identifier": "export", "metadata": {
        "source": {"format": "ICEBERG", "name": "rides", "catalog": catalog},
        "target": {"format": "PARQUET", "name": "rides", "path": f"s3://{bucket}/out/rides.parquet"}}}

    run_config(ingest)
    run_config(export)

    exported = Parquet("out", f"s3://{bucket}/out/rides.parquet").get()
    assert sorted(exported.column("ride_id").to_pylist()) == sample_table.column("ride_id").to_pylist()
