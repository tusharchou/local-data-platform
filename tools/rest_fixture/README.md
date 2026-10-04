# Iceberg REST catalog fixture

A [scala-cli](https://scala-cli.virtuslab.org) project that runs the official Apache Iceberg REST
catalog test server on your laptop, without Docker. It is what `tests/test_rest_catalog.py`
(`LDP_RUN_REST=1`) and the `demo-rest` make target talk to.

```bash
scala-cli run tools/rest_fixture -- --port 8181 --warehouse /tmp/ldp-rest/warehouse
# LDP_REST_FIXTURE_READY http://127.0.0.1:8181
```

| Argument | Default | Meaning |
|---|---|---|
| `--warehouse DIR` | required | The local warehouse folder (created if missing); tables are written under `file://DIR` |
| `--port N` | `8181` | The HTTP port |
| `--catalog-db FILE` | `DIR/rest_catalog.db` | The SQLite file of the backend `JdbcCatalog`, so the catalog survives restarts |
| `--name NAME` | `rest_backend` | The backend catalog's name |

`CATALOG_*` environment variables still configure anything else, as in the upstream fixture
(`CATALOG_IO__IMPL=...` sets `io-impl`, for example). The server needs no authentication and
ignores any bearer token.

## What runs

`LdpRestFixture.java` is a small launcher for `org.apache.iceberg.rest.RESTCatalogServer`, the
main class of the `test-fixtures` jar of `org.apache.iceberg:iceberg-open-api:1.12.0` on Maven
Central and the same code as the `apache/iceberg-rest-fixture` Docker image. The launcher sits in
the same package, because the server's configuration constructor is package-private, and prints
one `LDP_REST_FIXTURE_READY <uri>` line once the server accepts requests.

The `iceberg-open-api` POM declares no dependencies (its test-fixture dependencies exist only in
Iceberg's Gradle build), so `LdpRestFixture.java` lists the runtime classpath by hand, from the
`project(':iceberg-open-api')` block of Iceberg's `build.gradle` at tag `apache-iceberg-1.12.0`:

- `iceberg-core`, plus its `tests` jar for `RESTCatalogAdapter` and `RESTCatalogServlet`. That jar
  is referenced by URL, because coursier keeps only one classifier of a module;
- `iceberg-aws`, `iceberg-gcp` and `iceberg-azure`, whose property classes the server's adapter
  uses (not the cloud-SDK bundles: a `file://` warehouse needs none);
- `hadoop-common` 3.4.3 (a Hadoop `Configuration` and `HadoopFileIO` for the file warehouse), with
  its Jetty 9 and logging dependencies excluded;
- Jetty 12.1.13 (`jetty-ee10-servlet`, gzip compression), `sqlite-jdbc` 3.53.4.0 and
  `slf4j-simple`.

scala-cli provisions Temurin 17 itself. The first run downloads about 76 MB of jars (plus the JDK
if scala-cli has not fetched one yet). Keep the Iceberg version in step with `ICEBERG_VERSION` in
`src/local_data_platform/engine/spark/__init__.py`.
