from local_data_platform.pipeline.egression import Egression
from pyarrow import parquet, Table
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config
from local_data_platform.logger import log


logger = log()


class CSVToIceberg(Egression):
    """
    CSVToIceberg is a class that handles the transformation of CSV data to Iceberg format.

    Attributes:
        source (CSV): The source CSV configuration.
        target (Iceberg): The target Iceberg configuration.

    Methods:
        __init__(config: Config, *args, **kwargs):
            Initializes the CSVToIceberg instance with the provided configuration.
            Logs the initialization process and sets up the source and target attributes.
    """
    def __init__(self, config: Config, *args, **kwargs):
        logger.info(
            f"""
            Initialising CSVToIceberg with config {config}
            """
        )
        self.source = config.metadata["source"]
        self.target = config.metadata["target"]
        self.source = CSV(name=self.source["name"], path=self.source["path"])
        self.target = Iceberg(name=self.target["name"], catalog=self.target["catalog"])
        logger.info(
            f"""
            CSVToIceberg initialised with
            source {self.source}
            target {self.target}
            """
        )
