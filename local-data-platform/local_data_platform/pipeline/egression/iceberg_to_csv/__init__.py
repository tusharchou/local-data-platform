from local_data_platform.pipeline.egression import Egression
from pyarrow import parquet, Table
from local_data_platform.format.csv import CSV
from local_data_platform.format.iceberg import Iceberg
from local_data_platform import Config
from local_data_platform.logger import log


logger = log()


class IcebergToCSV(Egression):

    def __init__(self, config: Config, *args, **kwargs):
        logger.info(
            f"""
            Initialising IcebergToCSV with config {config}
            """
        )
        self.source = config.metadata["source"]
        self.target = config.metadata["target"]
        self.target = CSV(name=self.target["name"], path=self.target["path"])
        self.source = Iceberg(name=self.source["name"], catalog=self.source["catalog"])
        logger.info(
            f"""
            IcebergToCSV initialised with
            source {self.source}
            target {self.target}
            """
        )
