from local_data_platform.pipeline.egression import Egression
from pyarrow import parquet, Table
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config
from local_data_platform.logger import log


logger = log()


class IcebergToCSV(Egression):
    """
    IcebergToCSV is a class that handles the transformation of data from an Iceberg source to a CSV target.

    Attributes:
        source (Iceberg): The source Iceberg configuration.
        target (CSV): The target CSV configuration.

    Methods:
        __init__(config: Config, *args, **kwargs):
            Initializes the IcebergToCSV instance with the provided configuration.
            Logs the initialization process and sets up the source and target configurations.

    Args:
        config (Config): Configuration object containing metadata for source and target.
        *args: Additional positional arguments.
        **kwargs: Additional keyword arguments.
    """
    def __init__(self, config: Config, *args, **kwargs):
        logger.info(
            f"""
            Initialising IcebergToCSV with config {config}
            """
        )
        self.source = config.metadata["source"]
        self.target = config.metadata["target"]
        self.target = CSV(name=self.target["name"], path=self.target["path"])
        self.source = Iceberg(name=self.source["name"], config=self.source["catalog"])
        logger.info(
            f"""
            IcebergToCSV initialised with
            source {self.source}
            target {self.target}
            """
        )
