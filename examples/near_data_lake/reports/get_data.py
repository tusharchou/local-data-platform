"""Pull NEAR Protocol transactions from BigQuery into ``near_transactions.csv``.

This needs Google Cloud credentials and the BigQuery extra::

    pip install -e ".[bigquery]"
    python examples/near_data_lake/reports/get_data.py

The service-account key path is set in ``config/ingestion.json``. BigQuery bills
for the bytes a query scans, so check the query in
``config/sample_queries/near_transaction.json`` before you run it.

The pipeline is picked by the registry from the config's source format, engine and
target format (JSON + BIGQUERY to CSV), so this script has no if/else on formats.
"""

import logging
from pathlib import Path

from local_data_platform import Config
from local_data_platform.pipeline.registry import create_pipeline

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ingestion.json"


def get_near_transaction_dataset(config_path: str | Path = CONFIG_PATH):
    """Run the BigQuery to CSV pipeline described by ``config_path``.

    Args:
        config_path: Path to the dataset config. Paths inside it resolve against
            the config file's folder.

    Returns:
        The :class:`~local_data_platform.pipeline.PipelineResult` of the run.

    Raises:
        local_data_platform.exceptions.PipelineNotFound: If no pipeline is registered
            for the config's source and target.
    """
    config = Config.from_json(config_path)
    return create_pipeline(config).run()


def main() -> None:
    """Run the pipeline and print a one-line summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = get_near_transaction_dataset()
    print(f"{result.name}: read {result.rows_read} rows, wrote {result.rows_written} in {result.duration_s:.2f}s")


if __name__ == "__main__":
    main()
