"""BigQuery query to CSV file."""

from local_data_platform import Config
from local_data_platform.format.csv import CSV
from local_data_platform.pipeline.builders import bigquery_from_config, csv_from_config
from local_data_platform.pipeline.ingestion import Ingestion
from local_data_platform.pipeline.registry import register_pipeline
from local_data_platform.store.source.gcp.bigquery import BigQuery


@register_pipeline("JSON", "CSV", engine="BIGQUERY")
class BigQueryToCSV(Ingestion):
    """Run a BigQuery query once and save the result as a CSV file.

    Config: ``source`` is ``{"name", "format": "JSON", "engine": "BIGQUERY", "path",
    "credentials": {"name", "path"}}``, where ``path`` is a JSON file holding
    ``{"query": "..."}`` and ``credentials.path`` is a service-account key file; both
    resolve against the config's folder. ``target`` is ``{"name", "format": "CSV",
    "path"}``.

    The query is read from the file and run when the pipeline runs, exactly once.
    BigQuery bills for the bytes a query scans. The Google client libraries are an
    optional extra: ``pip install "local-data-platform[bigquery]"``.

    For tests, pass ``source=BigQuery(..., client=fake_client)``.
    """

    def build_source(self, config: Config) -> BigQuery:
        """Build the BigQuery source from ``config.source``."""
        return bigquery_from_config(config, "source")

    def build_target(self, config: Config) -> CSV:
        """Build the CSV target from ``config.target``."""
        return csv_from_config(config, "target")


__all__ = ["BigQueryToCSV"]
