# Catalogs and object storage

An Iceberg table lives in a **catalog**, which records where the table's current metadata file is,
and a **warehouse**, the folder or bucket that holds the data and metadata files. In 0.1.1 the
catalog was always a SQLite file next to a local warehouse. From 0.2.0 the `catalog` block of a
dataset config picks one of four catalog types, and CSV, Parquet and JSON paths can point at S3 or
Google Cloud Storage.

Every 0.1.1 config keeps working unchanged: a catalog block without `type` is a `local` catalog.

## Catalog types

The catalog block is the `catalog` object of an Iceberg `target` (or `source`):

```json
"target": {
  "format": "ICEBERG",
  "name": "rides",
  "catalog": {"type": "sql", "uri": "sqlite:///catalog.db", "warehouse": "warehouse", "namespace": "nyc"}
}
```

| `type` | Keys | What it builds |
|---|---|---|
| `local` (the default; alias `LocalIceberg`) | `identifier`, `warehouse_path` | A `LocalIcebergCatalog`, exactly as in 0.1.1: `<warehouse_path>/<identifier>_catalog.db`, with `identifier` as both the catalog name and the namespace |
| `sql` (alias `sqlite`) | `uri`, `warehouse`, `name`, `password_env` | A pyiceberg `SqlCatalog` on any SQLAlchemy URI, such as `sqlite:///catalog.db` or `postgresql+psycopg://ldp@db:5432/lake` |
| `rest` | `uri`, `warehouse`, `name`, `token_env`, `credential_env` | A pyiceberg `RestCatalog`: Apache Polaris, Lakekeeper, Nessie, Unity Catalog, S3 Tables, R2 Data Catalog, BigLake, ... |
| `glue` | `name`, `warehouse` | A pyiceberg `GlueCatalog` (AWS Glue Data Catalog). Needs boto3: `pip install "local-data-platform[glue]"` |

All types except `local` also take:

- `namespace`: the namespace tables live in. It falls back to `identifier`.
- `properties`: extra pyiceberg catalog properties, passed through unchanged, such as
  `s3.endpoint`, `s3.region`, `glue.region`, `header.X-Custom`, `oauth2-server-uri`, `scope` or
  `prefix`.
- `properties_env`: properties whose values come from environment variables, as
  `{"<property>": "<ENV_VAR_NAME>"}`.

Details per type:

- **`sql`.** A relative SQLite path (`sqlite:///catalog.db`, three slashes) resolves against the
  config file's folder, like every other relative path; `sqlite:////abs/catalog.db` (four
  slashes) is absolute. A local `warehouse` becomes a `file://` URI; `s3://` and `gs://`
  warehouses pass through. `name` is the `catalog_name` column of the `iceberg_tables` table, the
  layout Java's `JdbcCatalog` reads with `jdbc.schema-version=V1`, so Spark can share the catalog
  when it uses the same name. Without `name`, the catalog is named after its namespace, so a
  `local` catalog's SQLite file also opens as `{"type": "sql", "uri": "sqlite:///<warehouse>/<identifier>_catalog.db", "identifier": "<identifier>", ...}`.
- **`rest`.** Building the catalog calls the server's `GET /v1/config`, so a wrong `uri` fails
  straight away. `warehouse` goes to the server unchanged, because many REST catalogs expect a
  catalog name there rather than a path. `name` defaults to `rest` and is only a local label.
- **`glue`.** AWS credentials and the region come from boto3's usual sources (environment,
  profile, instance role), or from `glue.*` properties. `name` defaults to `glue`.

In Python, `create_catalog` builds the catalog from the same block:

```python
from local_data_platform.catalog import catalog_namespace, create_catalog
from local_data_platform.format.iceberg import Iceberg

spec = {"type": "sql", "uri": "sqlite:///catalog.db", "warehouse": "warehouse", "namespace": "nyc"}
catalog = create_catalog(spec, base_dir="configs")   # relative paths resolve against configs/
catalog.create_namespace_if_not_exists(catalog_namespace(spec))

rides = Iceberg("rides", spec, base_dir="configs")   # Iceberg builds its catalog the same way
same = Iceberg("rides", spec, catalog_obj=catalog)   # or reuses one you already have
```

Plugins add catalog types with `register_catalog_type`:

```python
from local_data_platform.catalog import register_catalog_type

@register_catalog_type("my_catalog")
def my_catalog(spec, base_dir):
    return build_a_pyiceberg_catalog(spec)
```

## Secrets

Secrets never go in a config file and never reach a log line.

- A config names the **environment variable** that holds a secret: `token_env` (a REST bearer
  token), `credential_env` (a REST OAuth2 client credential, `client_id:client_secret`),
  `password_env` (the database password for `sql`) and `properties_env` (any other secret
  property, such as `s3.secret-access-key`).
- The variable is read from the environment each time the catalog is built. A missing variable is
  a `ConfigError` that names the variable, not its value.
- A spec key or property whose name contains `token`, `secret`, `password` or `credential` and
  holds a literal value is rejected, and so is a password written into a `uri`.
- Logs print specs through `local_data_platform.catalog.redact`, which replaces those keys'
  values with `***` and masks URI passwords.

```json
{
  "type": "rest",
  "uri": "https://polaris.example.com/api/catalog",
  "warehouse": "lake",
  "namespace": "nyc",
  "credential_env": "POLARIS_CREDENTIAL",
  "properties": {"scope": "PRINCIPAL_ROLE:ALL"}
}
```

```json
{
  "type": "sql",
  "uri": "postgresql+psycopg://ldp@db.internal:5432/lake",
  "password_env": "LDP_CATALOG_PASSWORD",
  "warehouse": "s3://my-bucket/warehouse",
  "name": "lake",
  "namespace": "nyc",
  "properties": {"s3.region": "eu-west-1"}
}
```

A Postgres URI needs a SQLAlchemy driver such as `psycopg` installed alongside.

## Checking a catalog

Building a catalog and listing its namespaces is the quickest check that a block is right. From
Python, with the secrets redacted when you print the spec:

```python
from local_data_platform.catalog import create_catalog, redact

spec = {"type": "sql", "uri": "sqlite:///catalog.db", "warehouse": "warehouse", "namespace": "nyc"}
catalog = create_catalog(spec)
print(redact(spec))               # keys that look secret print as ***
print(catalog.list_namespaces())  # and catalog.list_tables("nyc") once the namespace exists
```

From the command line, `ldp catalog test SPEC` does the same for a catalog spec file or a whole
dataset config (it uses the target's catalog block, else the source's), and also lists the tables of
the spec's namespace. It prints the spec with secrets redacted and ends with `ok`; any failure exits
with 1. For a `local` catalog, or a `sql` catalog on a SQLite file, that doesn't exist yet it fails
instead of creating an empty one:

```bash
ldp catalog test rides.json
```

## Object storage for CSV, Parquet and JSON

`CSV`, `Parquet` and `Json` open their files through `local_data_platform.fs`, which uses
`pyarrow.fs`, so a dataset `path` can be:

| Path | Filesystem |
|---|---|
| `rides.csv`, `data/rides.csv` | Local, relative to the config file's folder |
| `/abs/rides.csv`, `file:///abs/rides.csv` | Local, absolute |
| `s3://bucket/key` (also `s3a://`, `s3n://`) | `pyarrow.fs.S3FileSystem` |
| `gs://bucket/key` (also `gcs://`) | `pyarrow.fs.GcsFileSystem` |

Parquet also reads a folder or an object-store prefix of Parquet files. A `.gz`, `.bz2` or `.zst`
suffix on a CSV or JSON file is decompressed on read.

Object-store settings come from the environment, never from the config:

| Variable | Effect |
|---|---|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_PROFILE` | S3 credentials, read by the AWS SDK itself |
| `AWS_ENDPOINT_URL_S3`, else `AWS_ENDPOINT_URL` | Another S3 endpoint, such as MinIO or LocalStack: `http://127.0.0.1:9000` |
| `AWS_REGION`, else `AWS_DEFAULT_REGION` | The S3 region |
| `STORAGE_EMULATOR_HOST` | A GCS emulator (anonymous access) |
| `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT` | GCS credentials and project |

A URI with credentials in it (`s3://key:secret@bucket/...`) is rejected.

**Writes are atomic.** Locally, a file is written to a temporary name in the same folder and then
renamed over the target, so readers see the old file or the new one. On S3 and GCS, the data is
buffered in memory and uploaded only after the writer finishes without error: one PUT for objects
below pyarrow's multipart threshold (an S3 PUT is atomic per object), a multipart upload above it
(visible only once complete). A write that fails uploads nothing and leaves the old object in
place.

The same functions are available directly:

```python
from local_data_platform import fs

filesystem, path = fs.filesystem_for("s3://my-bucket/raw/rides.parquet")
with fs.open_output_atomic("s3://my-bucket/raw/notes.txt") as out:
    out.write(b"written in one PUT")
with fs.open_input("s3://my-bucket/raw/notes.txt") as handle:
    print(handle.read())
print(fs.exists("s3://my-bucket/raw"))   # an object or a key prefix
```

The Iceberg warehouse on object storage is handled by pyiceberg's own FileIO, configured with the
catalog's `properties` (`s3.endpoint`, `s3.region`, ...) or the same AWS environment variables.

## A real REST catalog on your laptop

`tools/rest_fixture` is a scala-cli project that runs the official Apache Iceberg REST catalog
test server, `org.apache.iceberg.rest.RESTCatalogServer` from
`org.apache.iceberg:iceberg-open-api:1.12.0:test-fixtures` (the code behind the
`apache/iceberg-rest-fixture` Docker image), with no Docker. It serves a SQLite-backed
`JdbcCatalog` with a local file warehouse:

```bash
scala-cli run tools/rest_fixture -- --port 8181 --warehouse /tmp/ldp-rest/warehouse
# prints: LDP_REST_FIXTURE_READY http://127.0.0.1:8181
```

The first run downloads about 76 MB of jars, plus a JDK if scala-cli hasn't fetched one yet. Then
point a `rest` catalog block at it:
`{"type": "rest", "uri": "http://127.0.0.1:8181", "namespace": "nyc"}`. The fixture needs no
token.

`tests/test_rest_catalog.py` starts the fixture on a free port and runs pyiceberg and the
`Iceberg` format against it. Its tests are marked `rest` and are opt-in:

```bash
make rest-test        # LDP_RUN_REST=1 pytest -m rest
```

`make demo-rest` starts the fixture, runs the demo config against it (`ldp catalog test`, two runs,
the snapshots and a query) and stops it again.

## Testing against S3 without AWS

The tests run S3 against an in-process [moto](https://github.com/getmoto/moto) server
(`pip install "moto[server]"`, part of the `dev` extra), with `AWS_ENDPOINT_URL` pointing at it:

```python
from moto.server import ThreadedMotoServer

server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
server.start()
host, port = server.get_host_and_port()
# export AWS_ENDPOINT_URL=http://{host}:{port} AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing
```

The Glue catalog is tested the same way, with `glue.endpoint` and `s3.endpoint` properties set to
the moto server.
