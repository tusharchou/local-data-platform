# Examples

Two real-world datasets, each described by JSON configs and run by the pipeline
registry. Every path in a config resolves against the config file's own folder, so
you can run the examples from any working directory.

Install the package from the repo root first:

```bash
pip install -e ".[duckdb]"
```

If you only want to see the platform work, `ldp demo` needs no downloads or
credentials. It generates synthetic taxi rides and walks through every feature.

| Example | Config | Route | Needs |
|---|---|---|---|
| NEAR transactions | `near_data_lake/config/ingestion.json` | BigQuery (JSON query + `BIGQUERY` engine) → CSV | GCP key, `[bigquery]` extra, network |
| NEAR transactions | `near_data_lake/config/egression.json` | CSV → Iceberg, upsert on `transaction_hash`, quality checks that fail the load | nothing: the CSV is committed |
| NYC yellow taxi | `nyc_yellow_taxi_dataset/config/ingestion.json` | Parquet → Iceberg, overwrite, partitioned by pickup day, quality checks that warn | the Parquet download below |
| NYC yellow taxi | `nyc_yellow_taxi_dataset/config/egression.json` | Iceberg → CSV export | the ingestion run above |

The file names are historical: in the NEAR example, `egression.json` is the step
that loads the CSV into Iceberg.

## Run a config

Each config can be run with the CLI or with the report script next to it. Both call
`local_data_platform.pipeline.registry.create_pipeline`, which picks the pipeline
class from the config's source format, target format and engine.

```bash
# NEAR: CSV to Iceberg (works offline)
ldp run examples/near_data_lake/config/egression.json
python examples/near_data_lake/reports/put_data.py      # same thing, from Python

# Look at what was written
ldp snapshots examples/near_data_lake/config/egression.json
ldp query examples/near_data_lake/config/egression.json \
    "SELECT signer_account_id, count(*) AS n FROM transactions GROUP BY 1 ORDER BY n DESC"
```

The table lands in `near_data_lake/warehouse/`. Because the config upserts on
`transaction_hash`, running it twice leaves the row count unchanged.

## NEAR transactions from BigQuery

`near_data_lake/config/ingestion.json` runs the query in
`config/sample_queries/near_transaction.json` against the public
`bigquery-public-data.crypto_near_mainnet_us` dataset and overwrites
`near_transactions.csv` with the result.

1. Create a Google Cloud service account with BigQuery access and download its JSON key.
2. Save the key at `~/.config/local-data-platform/gcp-service-account.json`, or change
   `source.credentials.path` in the config. Keep keys outside the repo so they can't be
   committed by accident.
3. Install the extra and run:

   ```bash
   pip install -e ".[bigquery]"
   python examples/near_data_lake/reports/get_data.py
   ```

BigQuery bills for the bytes a query scans. Check the query before you run it; the
library runs it exactly once.

## NYC yellow taxi

The Parquet file (about 48 MB, about 3 million trips) isn't in the repo. Download it
from the NYC Taxi and Limousine Commission into the folder the config expects:

```bash
mkdir -p examples/nyc_yellow_taxi_dataset/data
curl -L -o examples/nyc_yellow_taxi_dataset/data/yellow_tripdata_2023-01.parquet \
    https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet

python examples/nyc_yellow_taxi_dataset/reports/put_data.py   # Parquet to Iceberg
python examples/nyc_yellow_taxi_dataset/reports/get_data.py   # Iceberg to CSV
```

Other months are listed at <https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page>;
change `source.path` to load one. The quality block in `ingestion.json` is set to
`warn` because real trip data contains some negative fares: they're logged, and the
load continues. Set `on_failure` to `fail` to block the load instead.

The export writes `data/exports/nyc_yellow_taxi_rides.csv`, which is several hundred MB
for a full month. Don't commit `data/` or `warehouse/`.

## Config reference

The full schema (write modes, partition transforms and every quality check) is in
[docs/design/v0_1_1.md](../docs/design/v0_1_1.md#config-schema).
