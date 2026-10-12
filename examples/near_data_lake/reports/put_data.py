"""Load ``near_transactions.csv`` into a local Iceberg table.

Run it from anywhere; no credentials or network are needed::

    python examples/near_data_lake/reports/put_data.py

``config/egression.json`` upserts on ``transaction_hash``, so running the script
again leaves the table's row count unchanged. The quality checks in the config run
before the write and stop the load if they fail.

The pipeline is picked by the registry from the config's source and target formats
(CSV to ICEBERG), so this script has no if/else on formats.
"""

import logging
from pathlib import Path

from local_data_platform import Config
from local_data_platform.pipeline.registry import create_pipeline

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "egression.json"


def put_near_transaction_dataset(config_path: str | Path = CONFIG_PATH):
    """Run the CSV to Iceberg pipeline described by ``config_path``.

    Args:
        config_path: Path to the dataset config. Paths inside it resolve against
            the config file's folder.

    Returns:
        The :class:`~local_data_platform.pipeline.PipelineResult` of the run.

    Raises:
        local_data_platform.exceptions.PipelineNotFound: If no pipeline is registered
            for the config's source and target.
        local_data_platform.exceptions.DataQualityError: If a quality check fails.
    """
    config = Config.from_json(config_path)
    return create_pipeline(config).run()


def main() -> None:
    """Run the pipeline and print a one-line summary."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = put_near_transaction_dataset()
    print(f"{result.name}: read {result.rows_read} rows, wrote {result.rows_written} in {result.duration_s:.2f}s")


if __name__ == "__main__":
    main()
