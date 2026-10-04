"""Export the NYC yellow-taxi Iceberg table to ``data/exports/nyc_yellow_taxi_rides.csv``.

Run ``put_data.py`` first so the table exists::

    python examples/nyc_yellow_taxi_dataset/reports/get_data.py

A full month is about 3 million rows, so the CSV is large (hundreds of MB).

The pipeline is picked by the registry from the config's source and target formats
(ICEBERG to CSV), so this script has no if/else on formats.
"""

import logging
from pathlib import Path

from local_data_platform import Config
from local_data_platform.pipeline.registry import create_pipeline

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "egression.json"


def get_nyc_yellow_taxi_dataset(config_path: str | Path = CONFIG_PATH):
    """Run the Iceberg to CSV pipeline described by ``config_path``.

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
    result = get_nyc_yellow_taxi_dataset()
    print(f"{result.name}: read {result.rows_read} rows, wrote {result.rows_written} in {result.duration_s:.2f}s")


if __name__ == "__main__":
    main()
