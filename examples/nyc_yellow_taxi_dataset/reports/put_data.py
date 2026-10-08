"""Load a month of NYC yellow-taxi trips from Parquet into a local Iceberg table.

The Parquet file isn't in the repo. Download it first (about 48 MB)::

    mkdir -p examples/nyc_yellow_taxi_dataset/data
    curl -L -o examples/nyc_yellow_taxi_dataset/data/yellow_tripdata_2023-01.parquet \\
        https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2023-01.parquet
    python examples/nyc_yellow_taxi_dataset/reports/put_data.py

The data comes from https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page.
For a run that needs no download, use ``ldp demo`` instead.

``config/ingestion.json`` overwrites the table, partitioned by pickup day, so a
re-run replaces the rows instead of duplicating them. Its quality block is set to
``warn``: the real data has some negative fares, which are logged, not blocked.

The pipeline is picked by the registry from the config's source and target formats
(PARQUET to ICEBERG), so this script has no if/else on formats.
"""

import logging
from pathlib import Path

from local_data_platform import Config
from local_data_platform.pipeline.registry import create_pipeline

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ingestion.json"


def put_nyc_yellow_taxi_dataset(config_path: str | Path = CONFIG_PATH):
    """Run the Parquet to Iceberg pipeline described by ``config_path``.

    Args:
        config_path: Path to the dataset config. Paths inside it resolve against
            the config file's folder.

    Returns:
        The :class:`~local_data_platform.pipeline.PipelineResult` of the run.

    Raises:
        local_data_platform.exceptions.PipelineNotFound: If no pipeline is registered
            for the config's source and target.
        FileNotFoundError: If the Parquet file hasn't been downloaded.
    """
    config = Config.from_json(config_path)
    return create_pipeline(config).run()


def main() -> None:
    """Run the pipeline and print a one-line summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = put_nyc_yellow_taxi_dataset()
    print(f"{result.name}: read {result.rows_read} rows, wrote {result.rows_written} in {result.duration_s:.2f}s")


if __name__ == "__main__":
    main()
